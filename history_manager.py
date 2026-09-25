"""
history_manager.py — Duplicate-run tracking for QuizSage.

Maintains a local JSON database (solved_history.json) that logs every
form URL that has been attempted.  Before navigating to a new URL the
caller should invoke ``check_and_prompt()`` which:

  1. Normalises the URL (strips query-string noise, trailing slashes).
  2. Looks it up in the history file.
  3. If found, prints the previous run timestamp and asks the user
     for a [y/N] CLI override.
  4. Returns True  → "proceed with solving"
             False → "user chose to abort"

After a run completes, ``record_run()`` appends the result to the
history file.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import config


# ────────────────────────────────────────────
#  URL normalisation
# ────────────────────────────────────────────

def _normalise_url(raw_url: str) -> str:
    """
    Strip tracking params, fragments, and trailing slashes so that
    the same logical form always produces the same key.

    Example:
        https://docs.google.com/forms/d/e/XXXX/viewform?usp=sf_link
        → https://docs.google.com/forms/d/e/XXXX/viewform
    """
    parsed = urlparse(raw_url)
    # Keep scheme + netloc + path; drop query & fragment.
    clean = urlunparse((parsed.scheme, parsed.netloc,
                        parsed.path.rstrip("/"), "", "", ""))
    return clean


# ────────────────────────────────────────────
#  File I/O helpers
# ────────────────────────────────────────────

def _load_history() -> dict:
    """Load the JSON history file, returning {} on first run."""
    path = config.HISTORY_FILE
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError:
            # Corrupted file — start fresh.
            return {}


def _save_history(data: dict) -> None:
    """Atomically write the history dict back to disk."""
    path = config.HISTORY_FILE
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


# ────────────────────────────────────────────
#  Public API
# ────────────────────────────────────────────

def check_and_prompt(raw_url: str) -> bool:
    """
    Return True if the caller should proceed with solving.

    If the URL has been solved before, warn the user in the terminal
    and ask for an explicit ``y`` to continue.  Any other input
    (including just pressing Enter) aborts.
    """
    key = _normalise_url(raw_url)
    history = _load_history()

    if key in history:
        prev = history[key]
        ts   = prev.get("timestamp", "unknown time")
        qs   = prev.get("questions_solved", "?")
        stat = prev.get("status", "unknown")
        print(
            f"\n⚠️  This form was already attempted!\n"
            f"   URL:        {key}\n"
            f"   When:       {ts}\n"
            f"   Questions:  {qs}\n"
            f"   Status:     {stat}\n"
        )
        answer = input("Run again anyway? [y/N]: ").strip().lower()
        return answer == "y"

    # Never seen before — proceed.
    return True


def record_run(
    raw_url: str,
    questions_solved: int,
    status: str = "submitted",
    score: str | None = None,
) -> None:
    """
    Append (or overwrite) a run entry in the history file.

    Parameters
    ----------
    raw_url : str
        The original Google Forms URL.
    questions_solved : int
        Number of questions the solver handled.
    status : str
        Free-text status, e.g. "submitted", "aborted", "error".
    """
    key = _normalise_url(raw_url)
    history = _load_history()
    history[key] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "questions_solved": questions_solved,
        "status": status,
        "score": score,
    }
    _save_history(history)
