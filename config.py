"""
config.py — Central configuration for QuizSage.

All tunables live here: API keys, student info for auto-fill,
behavioural flags, and timing parameters.
"""

from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()

# ════════════════════════════════════════════════════════════════
#  LLM API KEYS
# ════════════════════════════════════════════════════════════════

# Groq — used for ultra-fast pure-text question solving.
# Model: llama3-70b-8192
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL: str = "llama3-70b-8192"

# Gemini — used for multimodal questions (images) and as a
# cascade failover when Groq rate-limits.
# Model: gemini-2.0-flash (aliased from spec's "gemini-3.8-flash")
# Provide multiple keys for round-robin rotation to dodge per-key
# rate limits.
GEMINI_API_KEYS: list[str] = [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()]
GEMINI_MODEL: str = "gemini-2.0-flash"

# ════════════════════════════════════════════════════════════════
#  STUDENT INFO — used by the auto-fill feature
# ════════════════════════════════════════════════════════════════

STUDENT_NAME: str  = "Yugank Bhende"
STUDENT_ROLL: str  = "398"
STUDENT_BRANCH: str = "CSE"
STUDENT_EMAIL: str = "yugankrbhende.cse25f@kdkce.edu.in"
STUDENT_SECTION: str = "B"

# ════════════════════════════════════════════════════════════════
#  BEHAVIOUR FLAGS
# ════════════════════════════════════════════════════════════════

# If True, the bot will click "Submit" automatically after the
# audit table is printed. If False, it pauses for human review.
AUTO_SUBMIT: bool = False

# If True, random human-like delays are injected between UI
# actions.  Disable for faster (but less stealthy) runs.
HUMAN_DELAY: bool = True

# Confidence threshold (0.0-1.0).  Answers below this get a ⚠️
# flag in the audit table to draw your attention.
CONFIDENCE_WARN_THRESHOLD: float = 0.70

# Subject / domain context hint sent to the LLM so it answers
# within the right academic discipline (e.g. "Physics", "DBMS").
SUBJECT_CONTEXT: str = ""

# ════════════════════════════════════════════════════════════════
#  TIMING (human-emulation)
# ════════════════════════════════════════════════════════════════

# Random delay range (seconds) injected between UI actions.
MIN_ACTION_DELAY: float = 0.30   # 300 ms
MAX_ACTION_DELAY: float = 1.50   # 1500 ms

# Extra wait after clicking "Next" to let Google slide animations
# finish before re-parsing the DOM.
PAGE_TRANSITION_WAIT: float = 2.0

# ════════════════════════════════════════════════════════════════
#  PATHS
# ════════════════════════════════════════════════════════════════

# Playwright persistent browser profile directory.
BROWSER_PROFILE_DIR: str = "Yugank - College"

# Local JSON database for duplicate tracking.
HISTORY_FILE: str = "solved_history.json"
