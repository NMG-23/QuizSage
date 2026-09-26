import os
import json
import shutil
import sys
import threading
from pathlib import Path

DEFAULT_SETTINGS = {
    "keys": {"groq": [], "gemini": [], "openrouter": []},
    "models": {
        "groq_primary": "openai/gpt-oss-120b",
        "groq_fallback": "qwen/qwen3.8-27b",
        "groq_tertiary": "openai/gpt-oss-20b",
        "gemini": "gemini-3.6-flash",
        "openrouter": "anthropic/claude-3-5-haiku"
    },
    "custom_providers": [],
    "behaviour": {
        "auto_submit": False,
        "auto_close_browser": False,
        "human_delay": False,
        "confidence_warn_threshold": 0.70,
        "subject_context": "",
        "min_action_delay": 0.30,
        "max_action_delay": 1.50,
        "page_transition_wait": 2.0,
        "quota_warn_groq": None,
        "quota_warn_gemini": None
    },
    "student": {
        "name": "",
        "roll": "",
        "branch": "",
        "email": "",
        "section": ""
    },
    "appearance": {
        "dark_mode": True,
        "primary": "#7c3aed",
        "secondary": "#26A69A",
        "accent": "#9C27B0"
    },
    "launch": {
        "native_window": False
    }
}

def get_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "QuizSage_Data"
    else:
        return Path(__file__).parent / "QuizSage_Data"

_lock = threading.Lock()
_settings_cache = None

def migrate_legacy_data():
    """Migrate loose state into QuizSage_Data."""
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    
    files_to_move = [
        "solved.json", "solved_history.json", "key_health.json", 
        "quota.json", "answer_cache.json"
    ]
    dirs_to_move = ["notes", "google_profile"]
    
    base_dir = Path(__file__).parent
    
    for item in files_to_move + dirs_to_move:
        src = base_dir / item
        dst = data_dir / item
        if src.exists() and not dst.exists():
            try:
                shutil.move(str(src), str(dst))
            except Exception as e:
                print(f"Failed to migrate {item}: {e}")

def load_settings():
    """Loads settings and returns (settings_dict, is_fresh)"""
    global _settings_cache
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_file = data_dir / "settings.json"
    
    is_fresh = not settings_file.exists()
    current = {}
    if not is_fresh:
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                current = json.load(f)
        except Exception as e:
            import time
            backup = data_dir / f"settings.json.corrupt.{int(time.time())}"
            try:
                shutil.copy(str(settings_file), str(backup))
                print(f"Corrupt settings file backed up to {backup}")
            except Exception:
                pass
            current = {}
            is_fresh = True
    
    # Deep merge
    def merge(default, cur):
        res = default.copy() if isinstance(default, dict) else default
        if isinstance(cur, dict) and isinstance(res, dict):
            for k, v in cur.items():
                if k in res and isinstance(res[k], dict):
                    res[k] = merge(res[k], v)
                else:
                    res[k] = v
        return res
        
    _settings_cache = merge(DEFAULT_SETTINGS, current)
    return _settings_cache, is_fresh

def save_settings(new_settings=None):
    global _settings_cache
    if new_settings is not None:
        _settings_cache = new_settings
    if _settings_cache is None:
        return
        
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_file = data_dir / "settings.json"
    tmp_file = data_dir / "settings.json.tmp"
    
    with _lock:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(_settings_cache, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, settings_file)

def get_keys(provider: str) -> list[str]:
    """Return keys from settings if present, otherwise empty list."""
    if _settings_cache and provider in _settings_cache.get("keys", {}):
        keys = _settings_cache["keys"][provider]
        if keys:
            return keys
    return []
