"""
gui.py — NiceGUI-based web frontend for QuizSage.

Launch:
    python gui.py

Opens a local browser dashboard at http://localhost:8080 with:
  • Persistent student identity profile (saved to student_profile.json)
  • Subject context injection for domain-aware LLM answers
  • Auto-submit / human-delay toggle switches
  • Duplicate-run pre-check with override dialog
  • Non-blocking live execution with streaming log terminal
  • Audit results table with low-confidence flagging
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import threading
import asyncio
import re
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

from nicegui import ui, run, app

import config
import history_manager
import form_parser
import quota_tracker
from form_parser import ParsedQuestion
from solver import solve_questions, AnswerItem, QuotaExhaustedError


# ════════════════════════════════════════════════════════════════
#  Constants
# ════════════════════════════════════════════════════════════════

PROFILE_FILE = "student_profile.json"
SOLVED_FILE  = "solved.json"

STATUS_IDLE      = ("Idle",      "gray")
STATUS_RUNNING   = ("Running…",  "amber")
STATUS_COMPLETED = ("Completed", "green")
STATUS_ERROR     = ("Error",     "red")

# Asyncio lock for atomic solved.json writes (must NOT be threading.Lock —
# on_solve() is async and runs on NiceGUI's event loop).
_solved_lock = asyncio.Lock()


# ════════════════════════════════════════════════════════════════
#  Shared URL normalization
# ════════════════════════════════════════════════════════════════

def normalize_url(url: str) -> str:
    """Canonical form: strip whitespace, query, fragment, trailing slash."""
    if not url:
        return ""
    return url.strip().split("?")[0].split("#")[0].rstrip("/")


# ════════════════════════════════════════════════════════════════
#  Student profile persistence
# ════════════════════════════════════════════════════════════════

def _load_profile() -> dict:
    """Load student_profile.json if it exists, else return defaults."""
    if os.path.exists(PROFILE_FILE):
        try:
            with open(PROFILE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "name": config.STUDENT_NAME,
        "roll": config.STUDENT_ROLL,
        "branch": getattr(config, "STUDENT_BRANCH", ""),
        "section": getattr(config, "STUDENT_SECTION", ""),
        "email": config.STUDENT_EMAIL,
    }


def _save_profile(data: dict) -> None:
    """Persist student identity to disk."""
    with open(PROFILE_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _apply_profile_to_config(data: dict) -> None:
    """Push saved profile values into the live config module."""
    config.STUDENT_NAME   = data.get("name",    config.STUDENT_NAME)
    config.STUDENT_ROLL   = data.get("roll",    config.STUDENT_ROLL)
    config.STUDENT_BRANCH = data.get("branch", getattr(config, "STUDENT_BRANCH", ""))
    config.STUDENT_SECTION = data.get("section", getattr(config, "STUDENT_SECTION", ""))
    config.STUDENT_EMAIL  = data.get("email",   config.STUDENT_EMAIL)

    # Clear the lazily-cached info map so form_parser picks up new values.
    form_parser._INFO_MAP.clear()


# ════════════════════════════════════════════════════════════════
#  Thread-safe log writer
# ════════════════════════════════════════════════════════════════

class _LogWriter(io.TextIOBase):
    """
    A file-like object that pushes every write() into a NiceGUI
    ui.log component.  Thread-safe via a simple lock.
    """
    def __init__(self, log_widget: ui.log) -> None:
        super().__init__()
        self._log = log_widget
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        if not text or text == "\n":
            return len(text) if text else 0
        with self._lock:
            for line in text.rstrip("\n").split("\n"):
                self._log.push(line)
        return len(text)

    def flush(self) -> None:
        pass


# ════════════════════════════════════════════════════════════════
#  History lookup helper (non-blocking, no CLI prompt)
# ════════════════════════════════════════════════════════════════

def _check_history(url: str) -> dict | None:
    """
    Return the history entry dict if this URL was solved before,
    otherwise None.  Does NOT prompt the user — the GUI handles that.
    """
    key = history_manager._normalise_url(url)
    hist = history_manager._load_history()
    return hist.get(key)


_URL_RE = re.compile(r"https?://\S+")

def parse_subject_file(content: str) -> list[tuple[str, str]]:
    result = []
    current_subject = "General"
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _URL_RE.search(line)
        if m:
            url = m.group(0).rstrip(".,;:)]}")
            result.append((current_subject, url))
        else:
            current_subject = line
    return result


# ════════════════════════════════════════════════════════════════
#  Multi-format queue file parser
# ════════════════════════════════════════════════════════════════

def _detect_column(headers: list[str], candidates: list[str], default: str | None = None) -> str | None:
    """Case-insensitively find the first header matching any candidate."""
    lower_headers = {h.lower().strip(): h for h in headers}
    for c in candidates:
        if c.lower() in lower_headers:
            return lower_headers[c.lower()]
    return default


def parse_queue_file(content_bytes: bytes, filename: str) -> list[tuple[str, str, str]]:
    """
    Parse an uploaded file into a list of (subject, url, title) 3-tuples.
    Supports .txt, .csv, and .xlsx formats.
    Every URL is normalized; rows with empty/invalid URLs are skipped.
    """
    ext = os.path.splitext(filename)[1].lower()

    if ext == ".txt":
        text = content_bytes.decode("utf-8", errors="ignore")
        pairs = parse_subject_file(text)  # returns (subject, url) 2-tuples
        items = [(subj, normalize_url(url), "") for subj, url in pairs]
        return [(s, u, t) for s, u, t in items if u]

    elif ext == ".csv":
        text = content_bytes.decode("utf-8", errors="ignore")
        return _parse_tabular_rows_csv(text)

    elif ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content_bytes), read_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        # First row is header
        try:
            header_row = next(rows_iter)
        except StopIteration:
            wb.close()
            return []
        headers = [str(h or "").strip() for h in header_row]
        data_rows = []
        for row in rows_iter:
            data_rows.append({headers[i]: str(cell or "").strip() for i, cell in enumerate(row) if i < len(headers)})
        wb.close()
        return _parse_tabular_dicts(headers, data_rows)

    return []


def _parse_tabular_rows_csv(text: str) -> list[tuple[str, str, str]]:
    """Parse CSV text with DictReader, applying column-detection logic."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        # No header — fall back to URL-regex search across all text
        return _url_regex_fallback(text)
    headers = list(reader.fieldnames)
    rows = list(reader)
    return _parse_tabular_dicts(headers, rows)


def _parse_tabular_dicts(
    headers: list[str], rows: list[dict]
) -> list[tuple[str, str, str]]:
    """Shared column-detection for CSV and XLSX tabular data."""
    url_col     = _detect_column(headers, ["url", "form_url", "link"])
    subject_col = _detect_column(headers, ["subject", "class"])
    title_col   = _detect_column(headers, ["title"])

    if not url_col:
        # No recognizable URL column — fall back to URL-regex across all cells
        all_text = "\n".join(
            " ".join(str(v) for v in row.values()) for row in rows
        )
        return _url_regex_fallback(all_text)

    items = []
    for row in rows:
        raw_url = str(row.get(url_col, "")).strip()
        url = normalize_url(raw_url)
        if not url or not url.startswith("http"):
            continue
        subject = str(row.get(subject_col, "General")).strip() if subject_col else "General"
        title   = str(row.get(title_col, "")).strip() if title_col else ""
        items.append((subject, url, title))
    return items


def _url_regex_fallback(text: str) -> list[tuple[str, str, str]]:
    """Extract URLs via _URL_RE when column headers are unrecognizable."""
    items = []
    for m in _URL_RE.finditer(text):
        raw = m.group(0).rstrip(".,;:)]}")
        url = normalize_url(raw)
        if url:
            items.append(("General", url, ""))
    return items


# ════════════════════════════════════════════════════════════════
#  solved.json helpers
# ════════════════════════════════════════════════════════════════

def _load_solved() -> set[str]:
    """Load solved.json defensively — missing or corrupt = empty set."""
    if not os.path.exists(SOLVED_FILE):
        return set()
    try:
        with open(SOLVED_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return set(data.get("solved_urls", []))
    except (json.JSONDecodeError, OSError, TypeError):
        return set()


def _save_solved_url(url: str) -> None:
    """
    Atomically append a normalized URL to solved.json.
    Write to solved.json.tmp then os.replace() over solved.json.
    """
    solved = _load_solved()
    norm = normalize_url(url)
    if norm in solved:
        return
    solved.add(norm)
    data = {
        "solved_urls": sorted(solved),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = SOLVED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, SOLVED_FILE)

# ════════════════════════════════════════════════════════════════
#  Core solve workflow (runs in a worker thread)
# ════════════════════════════════════════════════════════════════

def _run_solve_pipeline(
    url: str,
    subject: str,
    log_writer: _LogWriter,
    wait_for_user_action: threading.Event = None,
    user_decision: dict = None,
    show_manual_actions: callable = None,
) -> tuple[list[ParsedQuestion], list[AnswerItem], str, Optional[str], Optional[str]]:
    """
    Execute the full QuizSage pipeline.  This function is called
    inside a background thread via ``run.io_bound()`` so it never
    blocks the NiceGUI event loop.

    Returns (all_questions, all_answers, final_status, score, scraped_title).
    """
    from playwright.sync_api import sync_playwright
    from urllib.parse import urlparse as _urlparse
    import httpx

    config.SUBJECT_CONTEXT = subject

    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = log_writer
    sys.stderr = log_writer

    all_questions: list[ParsedQuestion] = []
    all_answers:   list[AnswerItem]     = []
    status = "completed"
    score: str | None = None
    scraped_title: str | None = None

    try:
        # ── Resolve short links ────────────────────────────────
        SHORT_DOMAINS = {
            "forms.gle", "bit.ly", "tinyurl.com", "t.co",
            "goo.gl", "is.gd", "rb.gy", "shorturl.at",
        }
        parsed = _urlparse(url)
        if parsed.netloc.lower() in SHORT_DOMAINS:
            print(f"Resolving short link: {url}")
            try:
                resp = httpx.get(url, follow_redirects=True, timeout=10)
                url = str(resp.url)
                print(f"  → {url}")
            except Exception as exc:
                print(f"Could not resolve short link ({exc}), using as-is")

        # ── Launch browser ─────────────────────────────────────
        print("Launching stealth browser…")
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=config.BROWSER_PROFILE_DIR,
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = context.pages[0] if context.pages else context.new_page()

            print(f"Opening form: {url}")
            page.goto(url, wait_until="networkidle", timeout=30000)
            time.sleep(1.5)

            # ── Guard-page check ───────────────────────────────
            guard = form_parser.check_guard_page(page)
            if guard:
                print(f'Form blocked: "{guard}"')
                history_manager.record_run(url, 0, status="blocked")
                context.close()
                return [], [], "blocked"

            # ── Auto-fill student info ─────────────────────────
            filled = form_parser.auto_fill_student_info(page)
            if filled:
                print(f"Auto-filled {filled} student info field(s)")

            # ── Page-by-page solving loop ──────────────────────
            global_idx = 0
            page_num = 1

            while True:
                print(f"\n═══ Page {page_num} ═══")

                questions = form_parser.parse_current_page(page, global_idx)
                if not questions:
                    print("  (no questions found on this page)")
                else:
                    print(f"  Parsed {len(questions)} question(s)")
                    print("  Sending to LLM…")
                    answers = solve_questions(questions)

                    print("  Applying answers…")
                    for q, a in zip(questions, answers):
                        form_parser.apply_answer(
                            q, a.selected_options, a.short_answer_text, page
                        )

                    all_questions.extend(questions)
                    all_answers.extend(answers)
                    global_idx += len(questions)

                nav = form_parser.click_next_or_submit(page)

                if nav == "next":
                    print("  Clicked Next — waiting for page…")
                    page_num += 1
                    if form_parser.check_validation_errors(page):
                        print("  ⚠️  Validation error detected! "
                              "Please fix manually in the browser.")
                    continue

                if nav == "submit":
                    print("  Submit button found — stopping solve loop.")
                    break

                print("  No Next/Submit button found. Ending loop.")
                break

            # ── Submit ─────────────────────────────────────────
            total_solved = len(all_questions)
            submit_btn = form_parser.find_submit_button(page)

            if submit_btn and config.AUTO_SUBMIT:
                print("AUTO_SUBMIT is ON — submitting…")
                try:
                    submit_btn.scroll_into_view_if_needed()
                    time.sleep(0.5)
                    submit_btn.click()
                    time.sleep(3)
                except Exception as e:
                    if "has been closed" in str(e) or "Target closed" in str(e):
                        print("\n⚠️  Browser was closed before the submit click — answers were filled but the form was NOT submitted.")
                        status = "browser_closed"
                        history_manager.record_run(url, total_solved, status)
                        print(f"Form {status}!")
                    else:
                        raise
                else:
                    if form_parser.check_validation_errors(page):
                        print("⚠️  Validation error on submit! Fix manually.")
                        status = "validation_error"
                    else:
                        status = "submitted"

                    history_manager.record_run(url, total_solved, status)
                    print(f"Form {status}!")
            elif submit_btn:
                print("AUTO_SUBMIT is OFF — answers filled, not submitted.")
                print("Review the audit table and use the UI buttons to submit or discard.")
                history_manager.record_run(url, total_solved, "filled")
                status = "filled"

                if show_manual_actions and wait_for_user_action is not None and user_decision is not None:
                    show_manual_actions()
                    
                    # Wait for UI interaction (up to 10 mins)
                    waited = wait_for_user_action.wait(timeout=600)
                    
                    if not waited:
                        print("\n⏳ Timeout reached (10 mins). Discarding and closing browser.")
                        status = "timeout"
                    else:
                        if user_decision.get("submit") is True:
                            print("\n✅ User confirmed submission from UI. Submitting...")
                            try:
                                submit_btn.scroll_into_view_if_needed()
                                time.sleep(0.5)
                                submit_btn.click()
                                time.sleep(3)
                            except Exception as e:
                                if "has been closed" in str(e) or "Target closed" in str(e):
                                    print("\n⚠️  Browser was closed before the submit click — answers were filled but the form was NOT submitted.")
                                    status = "browser_closed"
                                    history_manager.record_run(url, total_solved, status)
                                    print(f"Form {status}!")
                                else:
                                    raise
                            else:
                                if form_parser.check_validation_errors(page):
                                    print("⚠️ Validation error on submit! Check browser.")
                                    status = "validation_error"
                                else:
                                    status = "submitted"
                                
                                history_manager.record_run(url, total_solved, status)
                                print(f"Form {status}!")
                        else:
                            print("\n🚫 User discarded the run. Closing browser.")
                            status = "discarded"
            else:
                history_manager.record_run(url, total_solved, "no_submit_btn")
                print("No Submit button found — answers filled but not submitted.")
                status = "no_submit_btn"

            if status == "submitted":
                print("\nAttempting to extract title and score...")
                try:
                    title_loc = page.locator('h1').first
                    if title_loc.count() == 0:
                        title_loc = page.locator('div[role="heading"]').first
                    if title_loc.count() > 0:
                        scraped_title = title_loc.inner_text(timeout=2000).strip()
                except Exception:
                    scraped_title = None

                try:
                    locator = page.get_by_text("View score")
                    if locator.count() > 0:
                        try:
                            with page.context.expect_page(timeout=10000) as new_page_info:
                                locator.first.click(timeout=5000)
                            score_page = new_page_info.value
                        except Exception:
                            score_page = page
                        
                        m = re.search(r"(\d+)\s*/\s*(\d+)", score_page.inner_text("body", timeout=5000))
                        if m:
                            score = f"{m.group(1)}/{m.group(2)}"
                except Exception:
                    score = None
                
                # Update history run with score
                history_manager.record_run(url, total_solved, status, score=score)

            if config.AUTO_CLOSE_BROWSER and status == "submitted":
                print("\nAuto-close enabled — closing browser context.")
            else:
                print("\nWaiting for user to close the browser window manually...")
                try:
                    page.wait_for_event("close", timeout=0)
                except Exception:
                    pass
            
            # Context closes when 'with' block exits.
            print("\nBrowser context closing gracefully.")

    except Exception as exc:
        print(f"\n❌ FATAL ERROR: {exc}")
        status = "error"
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    return all_questions, all_answers, status, score, scraped_title


# ════════════════════════════════════════════════════════════════
#  UI construction
# ════════════════════════════════════════════════════════════════

# Load profile once at import time so defaults are ready.
_initial_profile = _load_profile()
_apply_profile_to_config(_initial_profile)


@ui.page("/")
async def index():
    """Build the entire QuizSage dashboard."""

    # ── Page-level dark mode & custom CSS ──────────────────────
    ui.dark_mode().enable()
    ui.add_head_html("""
    <style>
        body { font-family: 'Inter', 'Segoe UI', sans-serif; }
        .q-card { border-radius: 12px !important; }
    </style>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    """)

    # ── Reactive state ─────────────────────────────────────────
    status_label = STATUS_IDLE
    results_rows: list[dict] = []
    
    wait_for_user_action = threading.Event()
    user_decision = {}
    
    batch_queue = {"items": []}

    def _update_preview():
        """Refresh the preview label to reflect current batch_queue state."""
        items = batch_queue["items"]
        subjects_count = len({s for s, _u, _t in items})
        preview_label.text = f"{len(items)} URLs across {subjects_count} subjects"

    def handle_file_upload(e):
        content_bytes = e.content.read()
        filename = e.name if hasattr(e, 'name') else 'upload.txt'
        items = parse_queue_file(content_bytes, filename)

        # ── Skip already-solved URLs ────────────────────────────
        solved = _load_solved()
        before = len(items)
        items = [(s, u, t) for s, u, t in items if normalize_url(u) not in solved]
        skipped = before - len(items)
        if skipped:
            ui.notify(f"Skipped {skipped} already-solved URLs.", type="info")

        # ── Deduplicate by URL (keep first) ─────────────────────
        seen: set[str] = set()
        deduped: list[tuple[str, str, str]] = []
        for s, u, t in items:
            norm = normalize_url(u)
            if norm not in seen:
                seen.add(norm)
                deduped.append((s, u, t))
        removed = len(items) - len(deduped)
        if removed:
            ui.notify(f"Removed {removed} duplicate URLs.", type="info")
        items = deduped

        if not items:
            ui.notify("No new URLs after filtering. Upload aborted.", type="warning")
            batch_queue["items"] = []
            preview_label.text = ""
            return

        batch_queue["items"] = items
        _update_preview()

    # ── HEADER ─────────────────────────────────────────────────
    with ui.header().classes("bg-[#0f0f0f] border-b border-zinc-800"):
        with ui.row().classes("w-full items-center justify-between px-6 py-2"):
            with ui.row().classes("items-center gap-3"):
                ui.icon("auto_fix_high").classes("text-3xl text-purple-400")
                ui.label("QuizSage").classes(
                    "text-2xl font-bold bg-gradient-to-r "
                    "from-purple-400 to-pink-400 bg-clip-text "
                    "text-transparent"
                )
                ui.label("Google Forms Auto-Solver").classes(
                    "text-sm text-zinc-500 hidden sm:block"
                )

            # Live status badge
            status_badge = ui.badge(
                status_label[0], color=status_label[1]
            ).classes("text-sm px-3 py-1")

    def _set_status(new: tuple[str, str]):
        nonlocal status_label
        status_label = new
        status_badge.text = new[0]
        status_badge._props["color"] = new[1]
        status_badge.update()

    # ── MAIN CONTENT ───────────────────────────────────────────
    with ui.column().classes("w-full max-w-5xl mx-auto px-4 py-6 gap-6"):

        # ─── Controls Card ─────────────────────────────────────
        with ui.card().classes(
            "w-full bg-zinc-900/80 border border-zinc-800 backdrop-blur"
        ):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Controls").classes(
                    "text-lg font-semibold text-zinc-300 mb-2"
                )
                quota_ui = ui.html().classes("mb-2").tooltip("API calls made today per provider. Resets at midnight.")
                
                def _update_quota_label():
                    gemini, groq = quota_tracker.get_counts()
                    g_color = "orange" if config.QUOTA_WARN_GEMINI and gemini >= config.QUOTA_WARN_GEMINI else "inherit"
                    gr_color = "orange" if config.QUOTA_WARN_GROQ and groq >= config.QUOTA_WARN_GROQ else "inherit"
                    quota_ui.content = f'<span class="text-sm text-zinc-400">API today — Gemini: <span style="color: {g_color}">{gemini}</span> &middot; Groq: <span style="color: {gr_color}">{groq}</span></span>'
                
                _update_quota_label()

            ui.separator().classes("mb-3")

            # URL row
            with ui.row().classes("w-full items-end gap-3"):
                ui.upload(label="Upload queue (.txt, .csv, .xlsx)", auto_upload=True, on_upload=handle_file_upload).props('accept=".txt,.csv,.xlsx"').tooltip("Upload .txt, .csv, or .xlsx with form URLs. Already-solved URLs are skipped automatically.")
                preview_label = ui.label()
                url_textarea = ui.textarea('Or paste form URLs (one per line)', placeholder="https://forms.gle/... — appended after any uploaded file's URLs").classes("flex-grow").props('outlined clearable color="purple"').tooltip("Paste one URL per line; these are appended after any uploaded file's URLs.")

            # Toggles row
            with ui.row().classes("w-full items-center gap-6 mt-2 flex-wrap"):
                auto_submit_switch = ui.switch(
                    "Auto-Submit", value=config.AUTO_SUBMIT
                ).classes("text-zinc-400").tooltip("When ON, each solved form is submitted automatically. When OFF, answers are filled in but the form is left open for manual review.")

                auto_close_switch = ui.switch(
                    "Auto-Close Browser", value=config.AUTO_CLOSE_BROWSER
                ).classes("text-zinc-400").tooltip("When ON, the browser window closes automatically after submission instead of staying open for review.")

                human_delay_switch = ui.switch(
                    "Human Emulation Delays", value=config.HUMAN_DELAY
                ).classes("text-zinc-400").tooltip("Adds random human-like pauses between actions to reduce bot-detection risk. Slightly slower.")

                subject_input = ui.input(
                    label="Subject (fallback when file has no headings)",
                    placeholder='e.g. "Physics", "DBMS", "Operating Systems"',
                    value=config.SUBJECT_CONTEXT,
                ).classes("flex-grow min-w-[200px]").props(
                    'outlined dense color="purple"'
                ).tooltip("Fallback subject label for forms that arrive without a heading (shown as General).")

            # Solve button
            solve_btn = ui.button(
                "🧙 Solve with Sage",
                color="#7c3aed",
            ).classes(
                "w-full mt-4 text-lg font-semibold tracking-wide py-2"
            ).props('rounded unelevated').tooltip("Start solving the queued forms one by one.")

        # ─── Student Profile Card ──────────────────────────────
        with ui.card().classes(
            "w-full bg-zinc-900/80 border border-zinc-800 backdrop-blur"
        ):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Student Profile").classes(
                    "text-lg font-semibold text-zinc-300"
                )
                profile_status = ui.label("").classes("text-sm text-green-400")

            ui.separator().classes("my-2")

            profile = _load_profile()

            with ui.row().classes("w-full gap-4 flex-wrap"):
                name_input = ui.input(
                    "Name", value=profile.get("name", "")
                ).classes("flex-grow min-w-[200px]").props(
                    'outlined dense color="purple"'
                ).tooltip("Your full name, auto-filled into form fields that ask for it.")
                roll_input = ui.input(
                    "Roll Number", value=profile.get("roll", "")
                ).classes("flex-grow min-w-[150px]").props(
                    'outlined dense color="purple"'
                ).tooltip("Your roll/enrollment number, auto-filled into matching form fields.")

            with ui.row().classes("w-full gap-4 flex-wrap mt-2"):
                branch_input = ui.input(
                    "Branch", value=profile.get("branch", "")
                ).classes("flex-grow min-w-[150px]").props(
                    'outlined dense color="purple"'
                ).tooltip("Your branch or department, auto-filled into matching form fields.")
                section_input = ui.input(
                    "Section", value=profile.get("section", "")
                ).classes("flex-grow min-w-[150px]").props(
                    'outlined dense color="purple"'
                ).tooltip("Your class section, auto-filled into matching form fields.")
                email_input = ui.input(
                    "Email", value=profile.get("email", "")
                ).classes("flex-grow min-w-[200px]").props(
                    'outlined dense color="purple"'
                ).tooltip("Your email address, auto-filled into matching form fields.")

            def save_profile():
                data = {
                    "name":    name_input.value.strip(),
                    "roll":    roll_input.value.strip(),
                    "branch":  branch_input.value.strip(),
                    "section": section_input.value.strip(),
                    "email":   email_input.value.strip(),
                }
                _save_profile(data)
                _apply_profile_to_config(data)
                profile_status.text = "✓ Saved"
                ui.notify("Profile saved!", type="positive", position="bottom")

            # Forward declaration so history table can trigger solving
            async def on_solve(): pass 
            
            ui.button(
                "💾 Save Profile", on_click=save_profile, color="#7c3aed"
            ).classes("mt-2").props("rounded unelevated size=sm").tooltip("Save your student details to disk so they persist across sessions.")

            # Show saved indicator if profile file exists
            if os.path.exists(PROFILE_FILE):
                profile_status.text = "✓ Loaded from disk"

        # ─── Live Log Terminal ─────────────────────────────────
        with ui.card().classes(
            "w-full bg-zinc-900/80 border border-zinc-800 backdrop-blur"
        ):
            ui.label("Live Terminal").classes(
                "text-lg font-semibold text-zinc-300 mb-2"
            )
            ui.separator().classes("mb-3")

            log_box = ui.log(max_lines=500).classes(
                "w-full bg-zinc-950 font-mono text-green-400 "
                "p-4 rounded-lg h-72 text-sm"
            )
            log_box.push("QuizSage terminal ready. Paste a URL and hit Solve.")

        # ─── Audit Results Table ───────────────────────────────
        with ui.card().classes(
            "w-full bg-zinc-900/80 border border-zinc-800 backdrop-blur"
        ):
            ui.label("Audit Results").classes(
                "text-lg font-semibold text-zinc-300 mb-2"
            )
            ui.separator().classes("mb-3")

            results_table = ui.table(
                columns=[
                    {"name": "form",       "label": "Form / Subject", "field": "form",       "align": "left", "sortable": True},
                    {"name": "qnum",       "label": "Q#",         "field": "qnum",       "align": "center", "sortable": True},
                    {"name": "type",       "label": "Type",       "field": "type",       "align": "center"},
                    {"name": "confidence", "label": "Confidence", "field": "confidence", "align": "center", "sortable": True},
                    {"name": "answer",     "label": "Answer",     "field": "answer"},
                    {"name": "reasoning",  "label": "Reasoning",  "field": "reasoning"},
                ],
                rows=[],
            ).classes("w-full").props(
                'dense flat bordered separator="cell" '
                'row-key="qnum" wrap-cells'
            )

            # Slot template for confidence column — red if < 70%
            results_table.add_slot(
                "body-cell-confidence",
                r"""
                <q-td :props="props">
                    <q-badge
                        :color="parseFloat(props.value) < 70 ? 'red' : 'green'"
                        :label="props.value"
                        class="text-sm px-2 py-1"
                    />
                </q-td>
                """,
            )
            
            # Manual Action Buttons
            with ui.row().classes("w-full justify-end gap-4 mt-4 hidden") as manual_action_container:
                
                def on_discard():
                    user_decision["submit"] = False
                    wait_for_user_action.set()
                    manual_action_container.classes(add="hidden")
                    
                async def on_submit():
                    with ui.dialog() as confirm_dialog, ui.card().classes("bg-zinc-900 border border-zinc-700"):
                        ui.label("Submit Form?").classes("text-lg font-bold text-zinc-200")
                        ui.label("Are you sure you want to submit this form in the browser?").classes("text-sm text-zinc-400 mt-2")
                        with ui.row().classes("w-full justify-end gap-3 mt-4"):
                            ui.button("Cancel", on_click=lambda: confirm_dialog.submit(False)).props("flat color=grey").tooltip("Go back without submitting.")
                            ui.button("Yes, Submit", on_click=lambda: confirm_dialog.submit(True), color="green").props("unelevated rounded").tooltip("Confirm and submit the form in the browser now.")
                    
                    confirmed = await confirm_dialog
                    if confirmed:
                        user_decision["submit"] = True
                        wait_for_user_action.set()
                        manual_action_container.classes(add="hidden")
                        _set_status(STATUS_RUNNING)

                ui.button("Discard & Close", on_click=on_discard, color="grey").props("outline rounded").tooltip("Throw away the filled answers and close the browser window.")
                ui.button("Submit Form", on_click=on_submit, color="green").props("unelevated rounded").tooltip("Submit the filled answers in the browser after a confirmation prompt.")

        # ─── Form History ───────────────────────────────
        with ui.card().classes(
            "w-full bg-zinc-900/80 border border-zinc-800 backdrop-blur"
        ):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Form History").classes("text-lg font-semibold text-zinc-300 mb-2")
                ui.button(icon="refresh", on_click=lambda: refresh_history()).props("flat round size=sm color=grey").tooltip("Reload the history table from disk.")
                
            ui.separator().classes("mb-3")
            
            history_table = ui.table(
                columns=[
                    {"name": "date", "label": "Date", "field": "date", "align": "left", "sortable": True},
                    {"name": "url", "label": "Form URL", "field": "url", "align": "left"},
                    {"name": "questions", "label": "Questions", "field": "questions", "align": "center"},
                    {"name": "status", "label": "Status", "field": "status", "align": "center", "sortable": True},
                    {"name": "action", "label": "Action", "field": "action", "align": "center"},
                ],
                rows=[]
            ).classes("w-full").props('dense flat bordered separator="cell" row-key="url"')

            def refresh_history():
                hist = history_manager._load_history()
                rows = []
                sorted_hist = sorted(hist.items(), key=lambda x: x[1].get("timestamp", ""), reverse=True)
                for k, v in sorted_hist:
                    display_url = k
                    if len(display_url) > 60:
                        display_url = display_url[:57] + "..."
                    
                    ts = v.get("timestamp", "")
                    if ts:
                        try:
                            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                            ts_display = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            ts_display = ts
                    else:
                        ts_display = "Unknown"
                        
                    rows.append({
                        "date": ts_display,
                        "url": display_url,
                        "raw_url": k,
                        "questions": str(v.get("questions_solved", "?")),
                        "status": v.get("status", "unknown")
                    })
                history_table.rows = rows
                history_table.update()

            history_table.add_slot(
                "body-cell-status",
                r"""
                <q-td :props="props">
                    <q-badge
                        :color="props.value === 'submitted' ? 'green' : (props.value === 'filled' ? 'amber' : 'red')"
                        :label="props.value"
                        class="text-sm px-2 py-1"
                    />
                </q-td>
                """,
            )
            
            history_table.add_slot(
                "body-cell-action",
                r"""
                <q-td :props="props">
                    <q-btn v-if="props.row.status !== 'submitted'" size="sm" color="purple" outline label="Re-solve" @click="() => $parent.$emit('resolve', props.row)" />
                </q-td>
                """,
            )

            async def handle_resolve(e):
                row = e.args
                raw_url = row.get("raw_url", "")
                if not raw_url: return
                
                resolve_dialog.open()
                confirmed = await resolve_dialog
                if confirmed:
                    batch_queue["items"] = [("Retry from History", raw_url, "")]
                    preview_label.text = "1 URLs across 1 subjects (from History)"
                    url_textarea.value = ""
                    # Scroll up to the top naturally
                    ui.run_javascript("window.scrollTo({top: 0, behavior: 'smooth'});")
                    await on_solve()
                    
            history_table.on("resolve", handle_resolve)

            # Initial load
            refresh_history()

    # ════════════════════════════════════════════════════════════
    #  Re-solve dialog
    # ════════════════════════════════════════════════════════════
    
    with ui.dialog() as resolve_dialog, ui.card().classes("bg-zinc-900 border border-zinc-700"):
        ui.label("Re-evaluate Draft?").classes("text-lg font-bold text-amber-400")
        ui.label("Are you sure you want to re-evaluate this form? Any previous draft answers will be overwritten.").classes("text-sm text-zinc-300 mt-2")
        with ui.row().classes("w-full justify-end gap-3 mt-4"):
            ui.button("Cancel", on_click=lambda: resolve_dialog.submit(False)).props("flat color=grey").tooltip("Go back without re-solving.")
            ui.button("Yes, Re-solve", on_click=lambda: resolve_dialog.submit(True), color="amber").props("unelevated rounded").tooltip("Re-queue this form and overwrite any previous draft answers.")

    # ════════════════════════════════════════════════════════════
    #  Duplicate-check dialog
    # ════════════════════════════════════════════════════════════

    with ui.dialog() as dup_dialog, ui.card().classes(
        "bg-zinc-900 border border-zinc-700 min-w-[380px]"
    ):
        ui.label("⚠️ Duplicate Detected").classes(
            "text-lg font-bold text-amber-400"
        )
        dup_info = ui.label("").classes("text-zinc-300 mt-2 text-sm")

        with ui.row().classes("w-full justify-end gap-3 mt-4"):
            ui.button(
                "Cancel", on_click=lambda: dup_dialog.submit(False)
            ).props("flat color=grey").tooltip("Skip this form and move to the next one.")
            ui.button(
                "Solve Anyway", on_click=lambda: dup_dialog.submit(True),
                color="amber"
            ).props("unelevated rounded").tooltip("Proceed to solve even though this form was attempted before.")

    # ════════════════════════════════════════════════════════════
    #  Solve button handler
    # ════════════════════════════════════════════════════════════

    async def on_solve():
        file_items = batch_queue.get("items", [])
        # Pasted URLs → parse as .txt, producing 2-tuples; widen to 3-tuples
        pasted_pairs = parse_subject_file(url_textarea.value or "")
        pasted_items = [(s, u, "") for s, u in pasted_pairs]
        items = list(file_items) + pasted_items
        # Ensure every item is a 3-tuple
        items = [(s, u, t) for s, u, t in items]
        # Dedupe by normalized URL, preserving order (file first)
        seen: set[str] = set()
        deduped: list[tuple[str, str, str]] = []
        for subj, url, title in items:
            norm = normalize_url(url)
            if norm and norm not in seen:
                seen.add(norm)
                deduped.append((subj, url, title))
        items = deduped
        if not items:
            ui.notify("Upload a queue file or paste at least one form URL.", type="warning")
            return

        # ── Sync toggles to config ─────────────────────────────
        config.AUTO_SUBMIT        = auto_submit_switch.value
        config.HUMAN_DELAY        = human_delay_switch.value
        config.AUTO_CLOSE_BROWSER = auto_close_switch.value

        _set_status(STATUS_RUNNING)
        solve_btn.disable()
        manual_action_container.classes(add="hidden")
        manual_subject = (subject_input.value or "").strip()

        # ── Clear previous results ─────────────────────────────
        results_table.rows.clear()
        results_table.update()
        log_box.clear()

        solved_count = 0
        total_count = len(items)
        quota_hit = False

        try:
            for i, (subject, raw_url, title) in enumerate(items):
                eff_subject = subject if subject != "General" else manual_subject
                ui.notify(f"Processing ({i+1}/{total_count}): {eff_subject}", type="info")
                # ── Duplicate pre-check ────────────────────────────────
                prev = _check_history(raw_url)
                if prev:
                    ts = prev.get("timestamp", "unknown")
                    qs = prev.get("questions_solved", "?")
                    st = prev.get("status", "unknown")
                    dup_info.text = (
                        f"URL: {raw_url}\n"
                        f"This form was already attempted.\n"
                        f"When: {ts}\n"
                        f"Questions: {qs}\n"
                        f"Status: {st}"
                    )
                    dup_dialog.open()
                    proceed = await dup_dialog
                    if not proceed:
                        ui.notify(f"Aborted {raw_url}.", type="info")
                        continue

                log_box.push(f"Processing URL {i+1}/{total_count}: {raw_url} (Subject: {eff_subject})")

                # ── Run in background thread ───────────────────────────
                log_writer = _LogWriter(log_box)

                wait_for_user_action.clear()
                user_decision.clear()

                def show_manual_actions():
                    manual_action_container.classes(remove="hidden")

                try:
                    all_q, all_a, final_status, form_score, form_scraped_title = await run.io_bound(
                        _run_solve_pipeline, raw_url, eff_subject, log_writer,
                        wait_for_user_action, user_decision, show_manual_actions
                    )
                except QuotaExhaustedError:
                    quota_hit = True
                    ui.notify(
                        f"Stopped: API quota exhausted — solved {solved_count} of {total_count}. Re-run later to continue.",
                        type="negative",
                        timeout=10000,
                    )
                    log_box.push(f"⛔ API quota exhausted after solving {solved_count}/{total_count}. Stopping batch.")
                    break

                # ── Save solved.json immediately on confirmed success ──
                if final_status == "submitted":
                    async with _solved_lock:
                        _save_solved_url(raw_url)
                    solved_count += 1
                    
                    import hashlib
                    notes_title = title or form_scraped_title or f"Untitled_Form_{hashlib.sha256(normalize_url(raw_url).encode()).hexdigest()[:8]}"
                    
                    def sanitize_filename(name):
                        return re.sub(r'[<>:"/\\|?*]', '', name).strip()[:80]
                    
                    sanitized_subj = sanitize_filename(eff_subject)
                    sanitized_title = sanitize_filename(notes_title)
                    notes_dir = os.path.join("notes", sanitized_subj)
                    os.makedirs(notes_dir, exist_ok=True)
                    notes_path = os.path.join(notes_dir, f"{sanitized_title}.md")
                    
                    score_str = f'"{form_score}"' if form_score else "null"
                    
                    notes_content = f"---\ntitle: \"{notes_title}\"\nsubject: \"{eff_subject}\"\nsource_url: \"{raw_url}\"\ndate_solved: \"{datetime.now(timezone.utc).isoformat()}\"\nscore: {score_str}\nquestion_count: {len(all_q)}\n---\n# {notes_title}\n## Q&A\n"
                    for q, a in zip(all_q, all_a):
                        notes_content += f"### {q.index + 1}. {q.title} (confidence {a.confidence:.2f})\n"
                        ans_text = " | ".join(a.selected_options) if a.selected_options else (a.short_answer_text or "—")
                        notes_content += f"**Answer:** {ans_text}\n\n"
                    
                    try:
                        with open(notes_path, "w", encoding="utf-8") as f:
                            f.write(notes_content)
                    except Exception as e:
                        log_box.push(f"❌ Failed to save notes: {e}")
                        
                    cache_hits = sum(1 for a in all_a if getattr(a, "from_cache", False))
                    ui.notify(f"Solved {len(all_q)} questions ({cache_hits} from cache)", type="positive")
                    _update_quota_label()

                # ── Populate audit table ───────────────────────────
                rows = []
                for q, a in zip(all_q, all_a):
                    if a.selected_options:
                        ans_text = " | ".join(a.selected_options)
                    elif a.short_answer_text:
                        ans_text = a.short_answer_text[:120]
                    else:
                        ans_text = "—"

                    rows.append({
                        "form":       f"{eff_subject} (Form {i+1})",
                        "qnum":       str(q.index + 1),
                        "type":       q.q_type,
                        "confidence": f"{a.confidence * 100:.0f}%",
                        "answer":     ans_text,
                        "reasoning":  a.reasoning[:150],
                    })

                results_table.rows.extend(rows)
                results_table.update()

                if final_status == "error":
                    _set_status(STATUS_ERROR)
                else:
                    _set_status(STATUS_COMPLETED)

                refresh_history()
                manual_action_container.classes(add="hidden")

                if i < total_count - 1:
                    await asyncio.sleep(3)

            if not quota_hit:
                ui.notify("Batch processing complete! Notes saved in notes/", type="positive")

        except Exception as exc:
            log_box.push(f"❌ Unexpected error: {exc}")
            _set_status(STATUS_ERROR)
        finally:
            # ── Step 6: Prune in-memory queue against solved.json ──
            solved = _load_solved()
            batch_queue["items"] = [
                (s, u, t) for s, u, t in batch_queue.get("items", [])
                if normalize_url(u) not in solved
            ]
            _update_preview()
            solve_btn.enable()

    solve_btn.on_click(on_solve)


# ════════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════════

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="QuizSage",
        port=8080,
        reload=False,
        show=True,
        favicon="🧙",
    )
