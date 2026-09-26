import os
import json
import hashlib
import time
from collections import deque
from datetime import datetime, timedelta, timezone
import threading

import config

KEY_HEALTH_FILE = "key_health.json"
_lock = threading.Lock()
_events = deque(maxlen=50)

class NoHealthyKeysError(Exception):
    pass

def _get_digest(key: str) -> str:
    return hashlib.sha256(key.encode('utf-8')).hexdigest()

def key_id(key: str) -> str:
    return _get_digest(key)[:8]

def is_401(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 401:
        return True
    return "invalid_api_key" in str(exc).lower()

def is_quota_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    err_str = str(exc).lower()
    return "rate_limit" in err_str or "resource_exhausted" in err_str or "quota" in err_str

def _load_state() -> dict:
    if not os.path.exists(KEY_HEALTH_FILE):
        return {"groq": {}, "gemini": {}}
    try:
        with open(KEY_HEALTH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"groq": {}, "gemini": {}}

def _save_state(state: dict):
    # Prune unknown keys here
    active_digests = {
        "groq": set(_get_digest(k) for k in config.GROQ_API_KEYS),
        "gemini": set(_get_digest(k) for k in config.GEMINI_API_KEYS)
    }
    
    for provider in ["groq", "gemini"]:
        if provider in state:
            digests = list(state[provider].keys())
            for digest in digests:
                if digest not in active_digests.get(provider, set()):
                    del state[provider][digest]
                    
    tmp = KEY_HEALTH_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, KEY_HEALTH_FILE)
    except Exception:
        pass # missing/corrupt file = all usable, never crash

def _sweep(state: dict) -> bool:
    changed = False
    now = datetime.now(timezone.utc)
    for provider, keys in state.items():
        for digest, info in keys.items():
            if info.get("status") == "exhausted":
                reset_at_str = info.get("reset_at")
                if reset_at_str:
                    try:
                        reset_at = datetime.fromisoformat(reset_at_str)
                        if now >= reset_at:
                            info["status"] = "healthy"
                            info["reason"] = None
                            info["reset_at"] = None
                            changed = True
                    except Exception:
                        pass
    return changed

def is_usable(provider: str, key: str) -> bool:
    with _lock:
        state = _load_state()
        if _sweep(state):
            _save_state(state)
        
        digest = _get_digest(key)
        provider_state = state.get(provider, {})
        key_info = provider_state.get(digest)
        
        if not key_info:
            return True
            
        status = key_info.get("status")
        if status == "healthy":
            return True
        if status == "dead":
            return False
        if status == "exhausted":
            # Just to be safe if sweep missed it or future reset
            reset_at_str = key_info.get("reset_at")
            if reset_at_str:
                try:
                    reset_at = datetime.fromisoformat(reset_at_str)
                    if datetime.now(timezone.utc) >= reset_at:
                        return True
                except Exception:
                    pass
            return False
            
        return True

def mark_dead(provider: str, key: str, reason: str = "401"):
    with _lock:
        state = _load_state()
        _sweep(state)
        
        digest = _get_digest(key)
        provider_state = state.setdefault(provider, {})
        provider_state[digest] = {
            "status": "dead",
            "reason": reason,
            "marked_at": datetime.now(timezone.utc).isoformat(),
            "reset_at": None
        }
        _save_state(state)
        
        _events.append({
            "provider": provider,
            "key_id": digest[:8],
            "reason": reason,
            "at": datetime.now(timezone.utc).isoformat()
        })

def mark_exhausted(provider: str, key: str):
    with _lock:
        state = _load_state()
        _sweep(state)
        
        digest = _get_digest(key)
        provider_state = state.setdefault(provider, {})
        
        reset_at = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        
        provider_state[digest] = {
            "status": "exhausted",
            "reason": "quota",
            "marked_at": datetime.now(timezone.utc).isoformat(),
            "reset_at": reset_at
        }
        _save_state(state)
        
        _events.append({
            "provider": provider,
            "key_id": digest[:8],
            "reason": "quota-exhausted",
            "at": datetime.now(timezone.utc).isoformat()
        })

def reset_provider(provider: str):
    with _lock:
        state = _load_state()
        if provider in state:
            for info in state[provider].values():
                info["status"] = "healthy"
                info["reason"] = None
                info["reset_at"] = None
        _save_state(state)

def reset_all():
    with _lock:
        state = _load_state()
        for provider in state:
            for info in state[provider].values():
                info["status"] = "healthy"
                info["reason"] = None
                info["reset_at"] = None
        _save_state(state)

def healthy_count(provider: str, keys: list[str]) -> tuple[int, int]:
    with _lock:
        state = _load_state()
        if _sweep(state):
            _save_state(state)
        
        usable_now = 0
        total = len(keys)
        
        for key in keys:
            digest = _get_digest(key)
            provider_state = state.get(provider, {})
            key_info = provider_state.get(digest)
            
            if not key_info or key_info.get("status") == "healthy":
                usable_now += 1
                continue
                
            status = key_info.get("status")
            if status == "exhausted":
                reset_at_str = key_info.get("reset_at")
                if reset_at_str:
                    try:
                        reset_at = datetime.fromisoformat(reset_at_str)
                        if datetime.now(timezone.utc) >= reset_at:
                            usable_now += 1
                    except Exception:
                        pass
        return usable_now, total

def drain_events() -> list[dict]:
    with _lock:
        events = list(_events)
        _events.clear()
        return events

def next_healthy_key(provider: str, keys: list[str], pool):
    with _lock:
        state = _load_state()
        if _sweep(state):
            _save_state(state)
        
        if not keys:
            raise NoHealthyKeysError(provider)
            
        for _ in range(len(keys)):
            key = next(pool)
            digest = _get_digest(key)
            provider_state = state.get(provider, {})
            key_info = provider_state.get(digest)
            
            usable = True
            if key_info:
                status = key_info.get("status")
                if status == "dead":
                    usable = False
                elif status == "exhausted":
                    reset_at_str = key_info.get("reset_at")
                    if reset_at_str:
                        try:
                            reset_at = datetime.fromisoformat(reset_at_str)
                            if datetime.now(timezone.utc) < reset_at:
                                usable = False
                        except Exception:
                            usable = False
                            
            if usable:
                return key
                
        raise NoHealthyKeysError(provider)
