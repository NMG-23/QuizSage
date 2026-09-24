"""
main.py - QuizSage entry point & orchestrator.

Usage:
    python main.py <google-forms-url>

Workflow
--------
  1. Duplicate check  (history_manager)
  2. Launch stealth browser (persistent Chromium profile)
  3. Guard-page detection (already responded / closed form)
  4. Auto-fill student info
  5. Per-page loop:
       a. Parse questions  (form_parser)
       b. Solve via LLM    (solver)
       c. Apply answers    (form_parser)
       d. Click Next / detect Submit
  6. Print audit table (rich)
  7. Submit (if AUTO_SUBMIT) or pause for human
  8. Record run (history_manager)
"""

from __future__ import annotations

import sys
import time
from urllib.parse import urlparse

import httpx

from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.table import Table

import config
import history_manager
import form_parser
from form_parser import ParsedQuestion
from solver import solve_questions, AnswerItem


console = Console()


# ================================================================
#  Audit table
# ================================================================

def _print_audit_table(
    questions: list[ParsedQuestion],
    answers: list[AnswerItem],
) -> None:
    """
    Render a pretty ASCII table in the terminal showing every
    question, its type, the AI's confidence (flagged if low),
    the chosen answer, and the reasoning.
    """
    table = Table(
        title="QuizSage - Audit Report",
        show_lines=True,
        header_style="bold cyan",
        title_style="bold magenta",
    )
    table.add_column("Q#",         justify="center", width=4)
    table.add_column("Type",       justify="center", width=10)
    table.add_column("Confidence", justify="center", width=12)
    table.add_column("Answer",     max_width=40)
    table.add_column("Reasoning",  max_width=50)

    for q, a in zip(questions, answers):
        # Confidence badge.
        conf_pct = f"{a.confidence * 100:.0f}%"
        if a.confidence < config.CONFIDENCE_WARN_THRESHOLD:
            conf_display = f"[yellow]!! {conf_pct}[/yellow]"
        else:
            conf_display = f"[green]{conf_pct}[/green]"

        # Answer text.
        if a.selected_options:
            answer_text = " | ".join(a.selected_options)
        elif a.short_answer_text:
            answer_text = a.short_answer_text[:80]
        else:
            answer_text = "-"

        table.add_row(
            str(q.index + 1),
            q.q_type,
            conf_display,
            answer_text,
            a.reasoning[:100],
        )

    console.print()
    console.print(table)
    console.print()


# ================================================================
#  Main orchestrator
# ================================================================

def _resolve_short_url(raw_url: str) -> str:
    """
    If ``raw_url`` looks like a short link (forms.gle, bit.ly,
    tinyurl.com, etc.), follow redirects and return the final
    destination URL.  Otherwise return the input unchanged.
    """
    SHORT_DOMAINS = {
        "forms.gle", "bit.ly", "tinyurl.com", "t.co",
        "goo.gl", "is.gd", "rb.gy", "shorturl.at",
    }
    parsed = urlparse(raw_url)
    if parsed.netloc.lower() not in SHORT_DOMAINS:
        return raw_url

    console.print(f"[dim]Resolving short link: {raw_url}[/dim]")
    try:
        resp = httpx.get(raw_url, follow_redirects=True, timeout=10)
        final = str(resp.url)
        console.print(f"[dim]  -> {final}[/dim]")
        return final
    except Exception as exc:
        console.print(f"[yellow]Could not resolve short link ({exc}), using as-is[/yellow]")
        return raw_url


def main(url: str) -> None:
    """Run the full QuizSage pipeline for a given Google Forms URL."""

    # -- Step 0: Resolve short links -----------------------------
    url = _resolve_short_url(url)

    # -- Step 1: Duplicate check ---------------------------------
    console.print("\n[bold]Checking history...[/bold]")
    if not history_manager.check_and_prompt(url):
        console.print("[yellow]Aborted by user.[/yellow]")
        return

    # -- Step 2: Launch stealth browser --------------------------
    console.print("[bold]Launching browser...[/bold]")

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

        # Navigate.
        console.print(f"[bold]Opening form:[/bold] {url}")
        page.goto(url, wait_until="networkidle", timeout=30000)
        time.sleep(1.5)

        # -- Step 3: Guard-page check ----------------------------
        guard = form_parser.check_guard_page(page)
        if guard:
            console.print(
                f'[bold red]Form blocked:[/bold red] "{guard}"'
            )
            history_manager.record_run(url, 0, status="blocked")
            context.close()
            return

        # -- Step 4: Auto-fill student info ----------------------
        filled = form_parser.auto_fill_student_info(page)
        if filled:
            console.print(
                f"[green]Auto-filled {filled} student info field(s)[/green]"
            )

        # -- Step 5: Page-by-page solving loop -------------------
        all_questions: list[ParsedQuestion] = []
        all_answers:   list[AnswerItem]     = []
        global_idx = 0
        page_num = 1

        while True:
            console.print(
                f"\n[bold cyan]=== Page {page_num} ===[/bold cyan]"
            )

            # 5a. Parse
            questions = form_parser.parse_current_page(page, global_idx)
            if not questions:
                console.print("[dim]  (no questions found on this page)[/dim]")
            else:
                console.print(
                    f"  Parsed {len(questions)} question(s)"
                )

                # 5b. Solve
                console.print("  Sending to LLM...")
                answers = solve_questions(questions)

                # 5c. Apply answers
                console.print("  Applying answers...")
                for q, a in zip(questions, answers):
                    form_parser.apply_answer(
                        q, a.selected_options, a.short_answer_text, page
                    )

                all_questions.extend(questions)
                all_answers.extend(answers)
                global_idx += len(questions)

            # 5d. Navigate (Next / Submit)
            nav = form_parser.click_next_or_submit(page)

            if nav == "next":
                console.print("  Clicked Next - waiting for page...")
                page_num += 1

                # Check for validation errors after clicking Next.
                if form_parser.check_validation_errors(page):
                    console.print(
                        "[bold red]Validation error detected![/bold red]  "
                        "A required question may have been missed.\n"
                        "   Please fix it manually in the browser, "
                        "then press Enter here to continue..."
                    )
                    input()
                continue

            if nav == "submit":
                console.print(
                    "  Submit button found - stopping solve loop."
                )
                break

            # Neither button found - could be a single-page form
            # or the form ended.
            console.print(
                "[yellow]  No Next/Submit button found. "
                "Ending loop.[/yellow]"
            )
            break

        # -- Step 6: Audit table ---------------------------------
        if all_questions:
            _print_audit_table(all_questions, all_answers)
        else:
            console.print(
                "[yellow]No questions were solved.[/yellow]"
            )

        # -- Step 7: Submit / pause ------------------------------
        total_solved = len(all_questions)
        submit_btn = form_parser.find_submit_button(page)

        if submit_btn:
            if config.AUTO_SUBMIT:
                console.print(
                    "[bold green]AUTO_SUBMIT is ON - submitting...[/bold green]"
                )
                submit_btn.scroll_into_view_if_needed()
                time.sleep(0.5)
                submit_btn.click()
                time.sleep(3)

                # Final validation-error trap.
                if form_parser.check_validation_errors(page):
                    console.print(
                        "[bold red]Validation error on submit![/bold red]  "
                        "Fix manually, then press Enter..."
                    )
                    input()

                history_manager.record_run(url, total_solved, "submitted")
                console.print("[bold green]Form submitted![/bold green]")
            else:
                console.print(
                    "[bold yellow]AUTO_SUBMIT is OFF.[/bold yellow]\n"
                    "   Review the audit table above.\n"
                    "   Press Enter to submit, or Ctrl+C to abort."
                )
                try:
                    input()
                    submit_btn.scroll_into_view_if_needed()
                    time.sleep(0.5)
                    submit_btn.click()
                    time.sleep(3)

                    if form_parser.check_validation_errors(page):
                        console.print(
                            "[bold red]Validation error![/bold red]  "
                            "Fix manually, then press Enter..."
                        )
                        input()

                    history_manager.record_run(url, total_solved, "submitted")
                    console.print(
                        "[bold green]Form submitted![/bold green]"
                    )
                except KeyboardInterrupt:
                    history_manager.record_run(url, total_solved, "aborted")
                    console.print(
                        "\n[yellow]Aborted - form NOT submitted.[/yellow]"
                    )
        else:
            # No submit button - single-page form may have been
            # submitted via the pagination handler, or it's a
            # survey with no submit.
            history_manager.record_run(url, total_solved, "no_submit_btn")
            console.print(
                "[yellow]No Submit button found - "
                "answers were filled but not submitted.[/yellow]"
            )

        # Keep browser open until user closes it
        console.print(
            "\n[dim]Waiting for you to close the browser window manually...[/dim]"
        )
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass

        context.close()

    console.print("[bold]QuizSage finished.[/bold]\n")


# ================================================================
#  CLI entry point
# ================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        console.print(
            "[bold red]Usage:[/bold red]  python main.py "
            "<google-forms-url>",
        )
        console.print(
            "\nExample:\n  python main.py "
            '"https://docs.google.com/forms/d/e/XXXX/viewform"'
        )
        sys.exit(1)

    target_url = sys.argv[1]
    
    # Prompt for subject context
    context = console.input("\n[bold cyan]Enter subject context (e.g. 'DBMS', 'Physics') or press Enter to skip:[/bold cyan] ")
    import config
    config.SUBJECT_CONTEXT = context.strip()

    main(target_url)
