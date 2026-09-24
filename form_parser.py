"""
form_parser.py — Playwright-based Google Forms DOM extractor.

Responsibilities:
  • Extract every question on the current page/section.
  • Detect question types: radio, checkbox, dropdown, text-input.
  • Capture embedded images as PNG bytes for multimodal LLM calls.
  • Strip enumeration prefixes ("A. ", "1) ", etc.) from options.
  • Auto-fill student-info fields (Name / Roll / Branch / Email).
  • Apply AI-generated answers back into the live DOM.
  • Handle pagination (Next / Submit), validation errors, and
    "already responded" guard pages.
"""

from __future__ import annotations

import re
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import Page, Locator, TimeoutError as PWTimeout

import config


# ════════════════════════════════════════════════════════════════
#  Data structures
# ════════════════════════════════════════════════════════════════

@dataclass
class QuestionOption:
    """A single selectable option inside a question."""
    text: str               # Cleaned display text (no "A. " prefix)
    locator: Locator        # Playwright handle for clicking


@dataclass
class ParsedQuestion:
    """Everything we know about one question on the page."""
    index: int                                  # 0-based position
    title: str                                  # Question prompt text
    q_type: str                                 # "radio" | "checkbox" | "dropdown" | "text"
    options: list[QuestionOption] = field(default_factory=list)
    image_bytes: Optional[bytes] = None         # PNG screenshot if an image is present
    text_locator: Optional[Locator] = None      # Handle for textarea / input[type=text]
    container: Optional[Locator] = None         # The enclosing question div


# ════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════

def _human_delay() -> None:
    """Sleep for a random duration to emulate human interaction."""
    if not config.HUMAN_DELAY:
        return
    time.sleep(random.uniform(config.MIN_ACTION_DELAY,
                              config.MAX_ACTION_DELAY))


_ENUM_RE = re.compile(
    r"^(?:[A-Za-z][.)]\s*|[0-9]+[.)]\s*|\([A-Za-z0-9]+\)\s*)"
)

def _strip_enum(text: str) -> str:
    """Remove leading enumeration markers like 'A. ', '1) ', '(b) '."""
    return _ENUM_RE.sub("", text).strip()


# ════════════════════════════════════════════════════════════════
#  Guard-page detection
# ════════════════════════════════════════════════════════════════

_ABORT_PHRASES = [
    "you've already responded",
    "your response has been recorded",
    "no longer accepting responses",
]

def check_guard_page(page: Page) -> Optional[str]:
    """
    Return a reason string if the form is closed / already done,
    otherwise return None (safe to proceed).
    """
    try:
        body_text = page.inner_text("body", timeout=5000).lower()
    except PWTimeout:
        return None

    for phrase in _ABORT_PHRASES:
        if phrase in body_text:
            return phrase
    return None


# ════════════════════════════════════════════════════════════════
#  Student-info auto-fill
# ════════════════════════════════════════════════════════════════

_INFO_MAP: dict[str, str] = {}  # populated lazily from config

def _get_info_map() -> dict[str, str]:
    """Build a lowercase-keyword → value mapping from config."""
    if not _INFO_MAP:
        _INFO_MAP.update({
            "name": config.STUDENT_NAME,
            "full name": config.STUDENT_NAME,
            "student name": config.STUDENT_NAME,
            "your name": config.STUDENT_NAME,
            "first name": config.STUDENT_NAME,
            "last name": config.STUDENT_NAME,
            "enter your full name": config.STUDENT_NAME,
            
            "roll": config.STUDENT_ROLL,
            "roll no": config.STUDENT_ROLL,
            "roll number": config.STUDENT_ROLL,
            "enter your roll number": config.STUDENT_ROLL,
            
            "branch": config.STUDENT_BRANCH,
            "enter your branch": config.STUDENT_BRANCH,
            
            "section": config.STUDENT_SECTION,
            "sec": config.STUDENT_SECTION,
            "select your section": config.STUDENT_SECTION,
            
            "email": config.STUDENT_EMAIL,
            "email address": config.STUDENT_EMAIL,
            "e-mail": config.STUDENT_EMAIL,
            "mail": config.STUDENT_EMAIL,
        })
    return _INFO_MAP


def auto_fill_student_info(page: Page) -> int:
    """
    Scan every question container for personal-info headers
    (Name, Roll, Branch, Email) and type the values from config.

    Returns the number of fields that were auto-filled.
    """
    filled = 0
    info = _get_info_map()

    # Each top-level question block in Google Forms.
    containers = page.locator('div[role="listitem"]').all()

    for container in containers:
        # Grab the question header text.
        header_els = container.locator(
            'div[role="heading"], span[class*="M7eMe"]'
        ).all()
        if not header_els:
            continue
        header_text = header_els[0].inner_text().strip().lower()

        # Guard 1: The "Points" Check.
        # Google Forms personal info fields NEVER have point values.
        if "point" in header_text:
            continue

        # Guard 2: Strict First-Line Matching.
        # Playwright extracts the whole container (e.g., "Name \n * \n Your answer").
        # We split by newline to look ONLY at the actual question title.
        first_line = header_text.split('\n')[0].replace('*', '').strip()

        matched_value: Optional[str] = None
        for keyword, value in info.items():
            if first_line == keyword:
                matched_value = value
                break
        
        # Broad match for email since it rarely appears as a paragraph starting word
        if matched_value is None and "email" in first_line:
            matched_value = config.STUDENT_EMAIL

        if matched_value is None:
            continue

        # Check for radio buttons or checkboxes matching the value
        options = container.locator('div[role="radio"], div[role="checkbox"]')
        if options.count() > 0:
            for opt in options.all():
                opt_text = (opt.get_attribute("aria-label") or opt.get_attribute("data-value") or opt.inner_text()).strip().lower()
                if matched_value.lower() in opt_text:
                    if opt.get_attribute("aria-checked") != "true":
                        opt.scroll_into_view_if_needed()
                        _human_delay()
                        opt.click()
                        _human_delay()
                    
                    container.evaluate("el => el.setAttribute('data-quizsage-ignore', 'true')")
                    filled += 1
                    break
            continue

        # Find a text input inside this container.
        text_input = container.locator('textarea, input[type="text"]')
        if text_input.count() > 0:
            target = text_input.first
            target.click()
            _human_delay()
            target.fill(matched_value)
            _human_delay()
            
            # Tag this container so we skip parsing it as a quiz question
            container.evaluate("el => el.setAttribute('data-quizsage-ignore', 'true')")
            
            filled += 1

    # Also globally search for the "Record email" checkbox and tick it.
    try:
        email_checkboxes = page.locator('div[role="checkbox"]').all()
        for cb in email_checkboxes:
            label = (cb.get_attribute("aria-label") or cb.inner_text(timeout=500)).lower()
            if "record" in label and "email" in label:
                if cb.get_attribute("aria-checked") != "true":
                    cb.scroll_into_view_if_needed()
                    _human_delay()
                    cb.click()
                    _human_delay()
                    filled += 1
                break
    except Exception:
        pass

    return filled


# ════════════════════════════════════════════════════════════════
#  Question extraction
# ════════════════════════════════════════════════════════════════

def _is_email_collection_widget(container: Locator) -> bool:
    """
    Return True if ``container`` is Google's native "Record email"
    checkbox — this should NOT be parsed as a quiz question.
    """
    try:
        text = container.inner_text(timeout=1000).lower()
    except PWTimeout:
        return False
    return "collect email" in text or "record my email" in text


def _extract_image(container: Locator) -> Optional[bytes]:
    """
    Look for an <img> or div[role="img"] inside the question container that is NOT a
    structural icon (Google's small UI icons are typically < 40 px
    wide).  If found, screenshot it and return raw PNG bytes.
    """
    images = container.locator('img, div[role="img"]').all()
    for img in images:
        try:
            # Skip tiny structural icons.
            box = img.bounding_box()
            if box and box["width"] > 10 and box["height"] > 10:
                return img.screenshot(type="png")
        except Exception:
            continue
    return None


def parse_current_page(page: Page, start_index: int = 0) -> list[ParsedQuestion]:
    """
    Parse every visible question on the current page/section of the
    Google Form and return a list of ``ParsedQuestion`` objects.

    Parameters
    ----------
    page : Page
        The active Playwright page.
    start_index : int
        Global question counter offset (for multi-page forms).

    Returns
    -------
    list[ParsedQuestion]
    """
    questions: list[ParsedQuestion] = []

    # Batch preload images to avoid O(N) wait times
    all_images = page.locator('img, div[role="img"]').all()
    found_image = False
    for img in all_images:
        try:
            img.scroll_into_view_if_needed()
            found_image = True
        except Exception:
            pass
    if found_image:
        page.wait_for_timeout(500)

    # Google wraps each question in a role="listitem" div.
    containers = page.locator('div[role="listitem"]').all()

    idx = start_index
    for container in containers:
        # Skip the native email-collection widget and auto-filled student info fields.
        if _is_email_collection_widget(container) or container.get_attribute("data-quizsage-ignore") == "true":
            continue

        # ── Image extraction ────────────────────────────────
        image_bytes = _extract_image(container)

        # ── Question title ──────────────────────────────────
        header_els = container.locator(
            'div[role="heading"], span[class*="M7eMe"]'
        ).all()
        if not header_els:
            continue  # Not a real question — likely a section header.
        title = header_els[0].inner_text().strip()
        if not title and not image_bytes:
            continue

        # ── Detect type & gather options ────────────────────
        q_type = "text"  # default fallback
        options: list[QuestionOption] = []
        text_locator: Optional[Locator] = None

        # Radio buttons
        radios = container.locator('div[role="radio"]').all()
        if radios:
            q_type = "radio"
            for r in radios:
                label = r.get_attribute("aria-label") or ""
                if not label:
                    # Fallback: grab visible text inside the radio's parent.
                    label = r.inner_text()
                options.append(QuestionOption(
                    text=_strip_enum(label.strip()),
                    locator=r,
                ))

        # Checkboxes
        if not options:
            checks = container.locator('div[role="checkbox"]').all()
            if checks:
                q_type = "checkbox"
                for c in checks:
                    label = c.get_attribute("aria-label") or c.inner_text()
                    options.append(QuestionOption(
                        text=_strip_enum(label.strip()),
                        locator=c,
                    ))

        # Dropdowns
        if not options:
            listbox = container.locator('div[role="listbox"]')
            if listbox.count() > 0:
                q_type = "dropdown"
                # We store the listbox handle; options will be read
                # after opening the menu during answer-apply phase.
                # For now, try to pre-read the options from the DOM.
                opts = listbox.locator('div[role="option"], span[class*="vRMGwf"]').all()
                for o in opts:
                    label = o.inner_text().strip()
                    if label and label.lower() != "choose":
                        options.append(QuestionOption(
                            text=_strip_enum(label),
                            locator=o,
                        ))

        # Text inputs (short answer / paragraph)
        if not options:
            txt = container.locator('textarea, input[type="text"]')
            if txt.count() > 0:
                q_type = "text"
                text_locator = txt.first

        questions.append(ParsedQuestion(
            index=idx,
            title=title,
            q_type=q_type,
            options=options,
            image_bytes=image_bytes,
            text_locator=text_locator,
            container=container,
        ))
        idx += 1

    return questions


# ════════════════════════════════════════════════════════════════
#  Answer application
# ════════════════════════════════════════════════════════════════

def apply_answer(
    question: ParsedQuestion,
    selected_options: list[str],
    short_answer_text: Optional[str],
    page: Page,
) -> None:
    """
    Click / type the AI-chosen answer into the live Google Form.

    Matching strategy:
      1. Exact match (case-insensitive).
      2. Substring containment.
      3. Fallback: select the first option to avoid crashing.

    For checkboxes, we check ``aria-checked`` before clicking to
    avoid toggling an already-correct answer OFF.
    """

    if question.q_type in ("radio", "checkbox"):
        _apply_choice(question, selected_options)

    elif question.q_type == "dropdown":
        _apply_dropdown(question, selected_options, page)

    elif question.q_type == "text":
        _apply_text(question, short_answer_text)


def _find_best_match(
    options: list[QuestionOption],
    target: str,
) -> QuestionOption:
    """
    Find the option whose text best matches ``target``.
    Falls back to the first option if nothing matches.
    """
    target_lower = target.strip().lower()

    # Pass 1 — exact match
    for opt in options:
        if opt.text.strip().lower() == target_lower:
            return opt

    # Pass 2 — target is a substring of option (or vice-versa)
    for opt in options:
        opt_lower = opt.text.strip().lower()
        if target_lower in opt_lower or opt_lower in target_lower:
            return opt

    # Pass 3 — fallback to first option
    return options[0]


def _apply_choice(
    question: ParsedQuestion,
    selected_texts: list[str],
) -> None:
    """Handle radio and checkbox selections."""
    if not question.options:
        return

    for answer_text in selected_texts:
        match = _find_best_match(question.options, answer_text)

        if question.q_type in ("checkbox", "radio"):
            # Only click if NOT already checked.
            already_checked = match.locator.get_attribute("aria-checked")
            if already_checked == "true":
                continue

        match.locator.scroll_into_view_if_needed()
        _human_delay()
        match.locator.click()
        _human_delay()


def _apply_dropdown(
    question: ParsedQuestion,
    selected_texts: list[str],
    page: Page,
) -> None:
    """Open the dropdown menu, then click the matching option."""
    if not question.container:
        return

    listbox = question.container.locator('div[role="listbox"]')
    if listbox.count() == 0:
        return

    # Click to open the dropdown.
    listbox.first.scroll_into_view_if_needed()
    _human_delay()
    listbox.first.click()
    _human_delay()

    # After opening, re-read the inner options (they may only now
    # be injected into the DOM by Google's JS).
    option_locators = page.locator(
        'div[role="option"], div[data-value]'
    ).all()

    # Build a fresh QuestionOption list from the live DOM.
    live_options: list[QuestionOption] = []
    for ol in option_locators:
        label = ol.inner_text().strip()
        if label and label.lower() not in ("choose", ""):
            live_options.append(QuestionOption(text=_strip_enum(label), locator=ol))

    if not live_options:
        # Nothing to select — press Escape to close the menu.
        page.keyboard.press("Escape")
        return

    target = selected_texts[0] if selected_texts else ""
    match = _find_best_match(live_options, target)
    match.locator.click()
    _human_delay()


def _apply_text(
    question: ParsedQuestion,
    answer: Optional[str],
) -> None:
    """Type a short-answer or paragraph response."""
    if not question.text_locator or not answer:
        return
    question.text_locator.scroll_into_view_if_needed()
    _human_delay()
    question.text_locator.click()
    _human_delay()
    question.text_locator.fill(answer)
    _human_delay()


# ════════════════════════════════════════════════════════════════
#  Pagination helpers
# ════════════════════════════════════════════════════════════════

def click_next_or_submit(page: Page) -> str:
    """
    Look for "Next" or "Submit" buttons on the current page.

    Returns
    -------
    "next"    — clicked Next successfully.
    "submit"  — found Submit (does NOT click it — that's the
                caller's job after the audit table).
    "none"    — neither button found.
    """
    # Google Forms button text varies by locale; we match common
    # English labels.  The buttons are typically <span> inside
    # <div role="button">.

    all_buttons = page.locator('div[role="button"], button').all()

    submit_btn = None
    next_btn = None

    for btn in all_buttons:
        try:
            label = btn.inner_text(timeout=1000).strip().lower()
        except PWTimeout:
            continue

        if label in ("submit", "soumettre", "enviar", "senden"):
            submit_btn = btn
        elif label in ("next", "suivant", "siguiente", "weiter"):
            next_btn = btn

    if submit_btn is not None:
        return "submit"

    if next_btn is not None:
        next_btn.scroll_into_view_if_needed()
        _human_delay()
        next_btn.click()
        # Wait for slide animation.
        time.sleep(config.PAGE_TRANSITION_WAIT)
        return "next"

    return "none"


def find_submit_button(page: Page) -> Optional[Locator]:
    """Return the Submit button locator, or None."""
    all_buttons = page.locator('div[role="button"], button').all()
    for btn in all_buttons:
        try:
            label = btn.inner_text(timeout=1000).strip().lower()
        except PWTimeout:
            continue
        if label in ("submit", "soumettre", "enviar", "senden"):
            return btn
    return None


def check_validation_errors(page: Page) -> bool:
    """
    Return True if Google shows a validation alert (e.g. "This is
    a required question").  The caller should pause for human
    intervention.
    """
    alerts = page.locator('div[role="alert"]').all()
    for alert in alerts:
        try:
            text = alert.inner_text(timeout=500).strip()
            if text:
                return True
        except PWTimeout:
            continue
    return False
