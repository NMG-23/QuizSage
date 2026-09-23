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
# Model: openai/gpt-oss-120b
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL: str = "openai/gpt-oss-120b"
GROQ_FALLBACK_MODEL: str = "qwen/qwen3.8-27b"
GROQ_TERTIARY_MODEL: str = "openai/gpt-oss-20b"

# Gemini — used for multimodal questions (images) and as a
# cascade failover when Groq rate-limits.
# Model: gemini-3.6-flash
# Provide multiple keys for round-robin rotation to dodge per-key
# rate limits.
GEMINI_API_KEYS: list[str] = [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()]
GEMINI_MODEL: str = "gemini-3.6-flash"

# ════════════════════════════════════════════════════════════════
#  STUDENT INFO — used by the auto-fill feature
# ════════════════════════════════════════════════════════════════

STUDENT_NAME: str  = "Your Name"
STUDENT_ROLL: str  = "101"
STUDENT_BRANCH: str = "Computer Science"
STUDENT_EMAIL: str = "student@example.com"
STUDENT_SECTION: str = "Section A"

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
BROWSER_PROFILE_DIR: str = "google_profile"

# Local JSON database for duplicate tracking.
HISTORY_FILE: str = "solved_history.json"
