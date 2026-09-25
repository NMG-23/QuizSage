import os
import json
import threading
from datetime import datetime, timezone

QUOTA_FILE = "quota.json"
_quota_lock = threading.Lock()

def _load_quota() -> dict:
    if os.path.exists(QUOTA_FILE):
        try:
            with open(QUOTA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"date": "", "gemini_calls": 0, "groq_calls": 0}

def _save_quota(data: dict):
    tmp = QUOTA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, QUOTA_FILE)

def record_call(provider: str):
    """provider: 'gemini' or 'groq'"""
    with _quota_lock:
        data = _load_quota()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        if data.get("date") != today:
            data["date"] = today
            data["gemini_calls"] = 0
            data["groq_calls"] = 0
            
        key = f"{provider}_calls"
        data[key] = data.get(key, 0) + 1
        _save_quota(data)

def get_counts() -> tuple[int, int]:
    """Returns (gemini_today, groq_today)"""
    with _quota_lock:
        data = _load_quota()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if data.get("date") != today:
            return (0, 0)
        return (data.get("gemini_calls", 0), data.get("groq_calls", 0))
