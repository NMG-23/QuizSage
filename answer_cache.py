import os
import json
import hashlib
import re
import threading
from datetime import datetime, timezone

CACHE_FILE = "answer_cache.json"
_cache_lock = threading.Lock()

# Regex to remove [Question X] block headers and leading numbering
_NUMBERING_RE = re.compile(
    r"^(?:\[question\s*\d+\]\s*\([a-z]+\)\s*prompt:\s*|q\d+[\.\)\:]\s*|question\s*\d+[\.\)\:]\s*|\d+[\.\)\:]\s*)",
    re.IGNORECASE
)

def _normalize_question(text: str) -> str:
    text = text.lower().strip()
    text = _NUMBERING_RE.sub("", text)
    # collapse all whitespace to single spaces
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def _get_key(text: str) -> str:
    norm = _normalize_question(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

def _load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_cache(data: dict):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, CACHE_FILE)

def get(question_text: str) -> dict | None:
    key = _get_key(question_text)
    with _cache_lock:
        data = _load_cache()
        return data.get(key)

def set_answer(question_text: str, answer_obj, subject: str, threshold: float = 0.70):
    if answer_obj.confidence < threshold:
        return
        
    key = _get_key(question_text)
    entry = {
        "question": question_text,
        "selected_options": answer_obj.selected_options,
        "short_answer_text": answer_obj.short_answer_text,
        "confidence": answer_obj.confidence,
        "reasoning": answer_obj.reasoning,
        "subject": subject,
        "saved_at": datetime.now(timezone.utc).isoformat()
    }
    
    with _cache_lock:
        data = _load_cache()
        data[key] = entry
        _save_cache(data)
