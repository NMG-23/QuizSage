"""
config.py — Central configuration for QuizSage.

All tunables live here: API keys, student info for auto-fill,
behavioural flags, and timing parameters.
"""

from __future__ import annotations
import os
from dotenv import load_dotenv
from pathlib import Path
import settings

# Migrate legacy data to QuizSage_Data
settings.migrate_legacy_data()

# Load settings dict
_current_settings, _is_fresh = settings.load_settings()

load_dotenv()

# First-run seeding
if _is_fresh:
    _current_settings["keys"]["groq"] = [k.strip() for k in os.getenv("GROQ_API_KEY", "").split(",") if k.strip()]
    _current_settings["keys"]["gemini"] = [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()]
    _current_settings["keys"]["openrouter"] = [k.strip() for k in os.getenv("OPENROUTER_API_KEYS", "").split(",") if k.strip()]
    settings.save_settings()

# Define all exported variables
GROQ_API_KEYS: list[str] = []
GROQ_MODEL: str = ""
GROQ_FALLBACK_MODEL: str = ""
GROQ_TERTIARY_MODEL: str = ""
QUOTA_WARN_GROQ: int | None = None

GEMINI_API_KEYS: list[str] = []
GEMINI_MODEL: str = ""
QUOTA_WARN_GEMINI: int | None = None

OPENROUTER_API_KEYS: list[str] = []
OPENROUTER_MODEL: str = ""
CUSTOM_PROVIDERS: list[dict] = []

STUDENT_NAME: str = ""
STUDENT_ROLL: str = ""
STUDENT_BRANCH: str = ""
STUDENT_EMAIL: str = ""
STUDENT_SECTION: str = ""

AUTO_SUBMIT: bool = False
AUTO_CLOSE_BROWSER: bool = False
HUMAN_DELAY: bool = False
CONFIDENCE_WARN_THRESHOLD: float = 0.70
SUBJECT_CONTEXT: str = ""

MIN_ACTION_DELAY: float = 0.30
MAX_ACTION_DELAY: float = 1.50
PAGE_TRANSITION_WAIT: float = 2.0

LAUNCH_NATIVE_WINDOW: bool = False

def reload_from_settings():
    global GROQ_API_KEYS, GEMINI_API_KEYS, OPENROUTER_API_KEYS
    global GROQ_MODEL, GROQ_FALLBACK_MODEL, GROQ_TERTIARY_MODEL, GEMINI_MODEL, OPENROUTER_MODEL
    global CUSTOM_PROVIDERS
    global AUTO_SUBMIT, AUTO_CLOSE_BROWSER, HUMAN_DELAY, CONFIDENCE_WARN_THRESHOLD, SUBJECT_CONTEXT
    global MIN_ACTION_DELAY, MAX_ACTION_DELAY, PAGE_TRANSITION_WAIT, QUOTA_WARN_GROQ, QUOTA_WARN_GEMINI
    global STUDENT_NAME, STUDENT_ROLL, STUDENT_BRANCH, STUDENT_EMAIL, STUDENT_SECTION
    global LAUNCH_NATIVE_WINDOW

    s, _ = settings.load_settings()

    GROQ_API_KEYS = settings.get_keys("groq") or [k.strip() for k in os.getenv("GROQ_API_KEY", "").split(",") if k.strip()]
    GEMINI_API_KEYS = settings.get_keys("gemini") or [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()]
    OPENROUTER_API_KEYS = settings.get_keys("openrouter") or [k.strip() for k in os.getenv("OPENROUTER_API_KEYS", "").split(",") if k.strip()]

    m = s.get("models", {})
    GROQ_MODEL = m.get("groq_primary", "openai/gpt-oss-120b")
    GROQ_FALLBACK_MODEL = m.get("groq_fallback", "qwen/qwen3.8-27b")
    GROQ_TERTIARY_MODEL = m.get("groq_tertiary", "openai/gpt-oss-20b")
    GEMINI_MODEL = m.get("gemini", "gemini-3.6-flash")
    OPENROUTER_MODEL = m.get("openrouter", "anthropic/claude-3-5-haiku")

    CUSTOM_PROVIDERS = s.get("custom_providers", [])

    b = s.get("behaviour", {})
    AUTO_SUBMIT = b.get("auto_submit", False)
    AUTO_CLOSE_BROWSER = b.get("auto_close_browser", False)
    HUMAN_DELAY = b.get("human_delay", False)
    CONFIDENCE_WARN_THRESHOLD = b.get("confidence_warn_threshold", 0.70)
    SUBJECT_CONTEXT = b.get("subject_context", "")
    MIN_ACTION_DELAY = b.get("min_action_delay", 0.30)
    MAX_ACTION_DELAY = b.get("max_action_delay", 1.50)
    PAGE_TRANSITION_WAIT = b.get("page_transition_wait", 2.0)
    QUOTA_WARN_GROQ = b.get("quota_warn_groq")
    QUOTA_WARN_GEMINI = b.get("quota_warn_gemini")

    stu = s.get("student", {})
    STUDENT_NAME = stu.get("name", "")
    STUDENT_ROLL = stu.get("roll", "")
    STUDENT_BRANCH = stu.get("branch", "")
    STUDENT_EMAIL = stu.get("email", "")
    STUDENT_SECTION = stu.get("section", "")

    LAUNCH_NATIVE_WINDOW = s.get("launch", {}).get("native_window", False)

reload_from_settings()

# PATHS
_data_dir = settings.get_data_dir()
BROWSER_PROFILE_DIR: str = str(_data_dir / "google_profile")
HISTORY_FILE: str = str(_data_dir / "solved_history.json")
