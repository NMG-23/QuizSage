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

import io
import json
import os
import sys
import time
import threading
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from nicegui import ui, run, app

import config
import history_manager
import form_parser
from form_parser import ParsedQuestion
from solver import solve_questions, AnswerItem


# ════════════════════════════════════════════════════════════════
#  Constants
# ════════════════════════════════════════════════════════════════

PROFILE_FILE = "student_profile.json"

STATUS_IDLE      = ("Idle",      "gray")
STATUS_RUNNING   = ("Running…",  "amber")
STATUS_COMPLETED = ("Completed", "green")
STATUS_ERROR     = ("Error",     "red")


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


# ════════════════════════════════════════════════════════════════
#  Core solve workflow (runs in a worker thread)
# ════════════════════════════════════════════════════════════════

def _run_solve_pipeline(
    url: str,
    log_writer: _LogWriter,
    wait_for_user_action: threading.Event = None,
    user_decision: dict = None,
    show_manual_actions: callable = None,
) -> tuple[list[ParsedQuestion], list[AnswerItem], str]:
    """
    Execute the full QuizSage pipeline.  This function is called
    inside a background thread via ``run.io_bound()`` so it never
    blocks the NiceGUI event loop.

    Returns (all_questions, all_answers, final_status).
    """
    from playwright.sync_api import sync_playwright
    from urllib.parse import urlparse as _urlparse
    import httpx

    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = log_writer
    sys.stderr = log_writer

    all_questions: list[ParsedQuestion] = []
    all_answers:   list[AnswerItem]     = []
    status = "completed"

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
                resp = httpx.head(url, follow_redirects=True, timeout=10)
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
                submit_btn.scroll_into_view_if_needed()
                time.sleep(0.5)
                submit_btn.click()
                time.sleep(3)

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
                            submit_btn.scroll_into_view_if_needed()
                            time.sleep(0.5)
                            submit_btn.click()
                            time.sleep(3)
                            
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

            # Context closes when 'with' block exits.
            print("\nBrowser context closing gracefully.")

    except Exception as exc:
        print(f"\n❌ FATAL ERROR: {exc}")
        status = "error"
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    return all_questions, all_answers, status


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
            ui.label("Controls").classes(
                "text-lg font-semibold text-zinc-300 mb-2"
            )
            ui.separator().classes("mb-3")

            # URL row
            with ui.row().classes("w-full items-end gap-3"):
                url_input = ui.input(
                    label="Google Forms URL",
                    placeholder="https://docs.google.com/forms/d/e/…/viewform",
                ).classes("flex-grow").props('outlined clearable color="purple"')

            # Toggles row
            with ui.row().classes("w-full items-center gap-6 mt-2 flex-wrap"):
                auto_submit_switch = ui.switch(
                    "Auto-Submit", value=config.AUTO_SUBMIT
                ).classes("text-zinc-400")

                human_delay_switch = ui.switch(
                    "Human Emulation Delays", value=config.HUMAN_DELAY
                ).classes("text-zinc-400")

                subject_input = ui.input(
                    label="Subject Context",
                    placeholder='e.g. "Physics", "DBMS", "Operating Systems"',
                    value=config.SUBJECT_CONTEXT,
                ).classes("flex-grow min-w-[200px]").props(
                    'outlined dense color="purple"'
                )

            # Solve button
            solve_btn = ui.button(
                "🧙 Solve with Sage",
                color="#7c3aed",
            ).classes(
                "w-full mt-4 text-lg font-semibold tracking-wide py-2"
            ).props('rounded unelevated')

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
                )
                roll_input = ui.input(
                    "Roll Number", value=profile.get("roll", "")
                ).classes("flex-grow min-w-[150px]").props(
                    'outlined dense color="purple"'
                )

            with ui.row().classes("w-full gap-4 flex-wrap mt-2"):
                branch_input = ui.input(
                    "Branch", value=profile.get("branch", "")
                ).classes("flex-grow min-w-[150px]").props(
                    'outlined dense color="purple"'
                )
                section_input = ui.input(
                    "Section", value=profile.get("section", "")
                ).classes("flex-grow min-w-[150px]").props(
                    'outlined dense color="purple"'
                )
                email_input = ui.input(
                    "Email", value=profile.get("email", "")
                ).classes("flex-grow min-w-[200px]").props(
                    'outlined dense color="purple"'
                )

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
            ).classes("mt-2").props("rounded unelevated size=sm")

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
                            ui.button("Cancel", on_click=lambda: confirm_dialog.submit(False)).props("flat color=grey")
                            ui.button("Yes, Submit", on_click=lambda: confirm_dialog.submit(True), color="green").props("unelevated rounded")
                    
                    confirmed = await confirm_dialog
                    if confirmed:
                        user_decision["submit"] = True
                        wait_for_user_action.set()
                        manual_action_container.classes(add="hidden")
                        _set_status(STATUS_RUNNING)

                ui.button("Discard & Close", on_click=on_discard, color="grey").props("outline rounded")
                ui.button("Submit Form", on_click=on_submit, color="green").props("unelevated rounded")

        # ─── Form History ───────────────────────────────
        with ui.card().classes(
            "w-full bg-zinc-900/80 border border-zinc-800 backdrop-blur"
        ):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Form History").classes("text-lg font-semibold text-zinc-300 mb-2")
                ui.button(icon="refresh", on_click=lambda: refresh_history()).props("flat round size=sm color=grey")
                
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
                    <q-btn v-if="props.row.status !== 'submitted'" size="sm" color="purple" outline label="Re-solve" @click="() => $emit('resolve', props.row)" />
                </q-td>
                """,
            )

            async def handle_resolve(e):
                row = e.args
                raw_url = row.get("raw_url", "")
                if not raw_url: return
                
                confirmed = await resolve_dialog
                if confirmed:
                    url_input.value = raw_url
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
            ui.button("Cancel", on_click=lambda: resolve_dialog.submit(False)).props("flat color=grey")
            ui.button("Yes, Re-solve", on_click=lambda: resolve_dialog.submit(True), color="amber").props("unelevated rounded")

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
            ).props("flat color=grey")
            ui.button(
                "Solve Anyway", on_click=lambda: dup_dialog.submit(True),
                color="amber"
            ).props("unelevated rounded")

    # ════════════════════════════════════════════════════════════
    #  Solve button handler
    # ════════════════════════════════════════════════════════════

    async def on_solve():
        raw_url = (url_input.value or "").strip()
        if not raw_url:
            ui.notify("Please enter a Google Forms URL.", type="warning")
            return

        # ── Sync toggles to config ─────────────────────────────
        config.AUTO_SUBMIT    = auto_submit_switch.value
        config.HUMAN_DELAY    = human_delay_switch.value
        config.SUBJECT_CONTEXT = (subject_input.value or "").strip()

        # ── Duplicate pre-check ────────────────────────────────
        prev = _check_history(raw_url)
        if prev:
            ts = prev.get("timestamp", "unknown")
            qs = prev.get("questions_solved", "?")
            st = prev.get("status", "unknown")
            dup_info.text = (
                f"This form was already attempted.\n"
                f"When: {ts}\n"
                f"Questions: {qs}\n"
                f"Status: {st}"
            )
            proceed = await dup_dialog
            if not proceed:
                ui.notify("Aborted.", type="info")
                return

        # ── Clear previous results ─────────────────────────────
        results_table.rows.clear()
        results_table.update()
        log_box.clear()

        # ── Run in background thread ───────────────────────────
        _set_status(STATUS_RUNNING)
        solve_btn.disable()
        manual_action_container.classes(add="hidden")

        log_writer = _LogWriter(log_box)

        try:
            wait_for_user_action.clear()
            user_decision.clear()
            
            def show_manual_actions():
                manual_action_container.classes(remove="hidden")
                
            all_q, all_a, final_status = await run.io_bound(
                _run_solve_pipeline, raw_url, log_writer,
                wait_for_user_action, user_decision, show_manual_actions
            )

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
                    "qnum":       str(q.index + 1),
                    "type":       q.q_type,
                    "confidence": f"{a.confidence * 100:.0f}%",
                    "answer":     ans_text,
                    "reasoning":  a.reasoning[:150],
                })

            results_table.rows = rows
            results_table.update()

            if final_status == "error":
                _set_status(STATUS_ERROR)
            else:
                _set_status(STATUS_COMPLETED)
                
            refresh_history()

        except Exception as exc:
            log_box.push(f"❌ Unexpected error: {exc}")
            _set_status(STATUS_ERROR)
        finally:
            solve_btn.enable()

    solve_btn.on_click(on_solve)


# ════════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════════

ui.run(
    title="QuizSage",
    port=8080,
    reload=False,
    show=True,
    favicon="🧙",
)
