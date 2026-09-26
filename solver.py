"""
solver.py — Dual-API LLM Router with cascade failover.

Routing rules
─────────────
  • Pure-text questions  →  Groq  (llama-3.3-70b-versatile)
  • Multimodal questions →  Gemini (round-robin key pool)
  • If Groq fails (429 / any exception), ALL remaining text
    questions are appended to the Gemini queue and retried.

Output schema (enforced via Pydantic):
  question_index   : int
  selected_options : list[str]
  short_answer_text: str | None
  confidence       : float (0.0 – 1.0)
  reasoning        : str  (one-sentence explanation)
"""

from __future__ import annotations

import base64
import json
import re
import time
import textwrap
import itertools
import hashlib
from typing import Optional

from pydantic import BaseModel, Field

import config
import quota_tracker
import answer_cache
import key_health
from form_parser import ParsedQuestion


# ════════════════════════════════════════════════════════════════
#  Quota exhaustion sentinel
# ════════════════════════════════════════════════════════════════

class QuotaExhaustedError(Exception):
    """Raised when the API quota is fatally exhausted (not a transient 429)."""
    pass


# ════════════════════════════════════════════════════════════════
#  Pydantic schema
# ════════════════════════════════════════════════════════════════

class AnswerItem(BaseModel):
    """Schema for a single question's AI-generated answer."""
    question_index: int
    selected_options: list[str] = Field(default_factory=list)
    short_answer_text: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    from_cache: bool = False


class AnswerBatch(BaseModel):
    """Top-level wrapper returned by every LLM call."""
    answers: list[AnswerItem]


# ════════════════════════════════════════════════════════════════
#  Prompt construction
# ════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert quiz solver.  You will receive one or more
    Google-Forms questions.  For each question return a JSON object
    that EXACTLY matches this schema (no markdown fences):

    {
      "answers": [
        {
          "question_index": <int>,
          "selected_options": ["option text", ...],
          "short_answer_text": "<string or null>",
          "confidence": <float 0.0-1.0>,
          "reasoning": "<one sentence>"
        }
      ]
    }

    Rules:
    • For radio questions choose exactly ONE option in selected_options.
    • For checkbox questions choose ONE or MORE options.
    • For dropdown questions choose exactly ONE option.
    • For text/short-answer questions put the answer in short_answer_text
      and leave selected_options empty.
    • Use the exact option text provided — do NOT add letters or numbers.
    • If uncertain, still pick the best guess and set a low confidence.
    • Return ONLY valid JSON. No explanation outside the JSON.

    CRITICAL INSTRUCTION FOR SHORT_ANSWER_TEXT: 
    If the question asks for code, a formula, or a specific value, output ONLY the raw code or exact value in the `short_answer_text` field. 
    - DO NOT include conversational filler (e.g., "Use function overloading...").
    - DO NOT use markdown code blocks (e.g., ```cpp).
    - DO NOT explain the code in this field. 
    - COMPLETENESS: If a question asks you to "develop", "write a program", or "create a module", you MUST output a fully complete, runnable script. Include all necessary `#include` headers, `using namespace std;`, class/struct definitions, and an `int main()` block demonstrating the code in action. Do not output bare snippets.
    - For maths, show working step-by-step in the reasoning field before giving the final value.
    All explanations and conversational text MUST go exclusively into the `reasoning` field.
""")


def _get_system_prompt() -> str:
    """Return the system prompt, optionally enriched with subject context."""
    base = _SYSTEM_PROMPT
    if config.SUBJECT_CONTEXT:
        base += (
            f"\nIMPORTANT: These questions are from the subject/domain: "
            f'"{config.SUBJECT_CONTEXT}". Answer accordingly.\n'
        )
    return base


def _build_question_block(q: ParsedQuestion) -> str:
    """Render one question as a text block for the LLM prompt."""
    lines = [f"[Question {q.index}] ({q.q_type})"]
    lines.append(f"  Prompt: {q.title}")
    if q.options:
        lines.append("  Options:")
        for opt in q.options:
            lines.append(f"    - {opt.text}")
    else:
        lines.append("  (free-text answer expected)")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
#  API key-pool rotation
# ════════════════════════════════════════════════════════════════

gemini_pool = None
groq_pool = None
openrouter_pool = None
custom_pools = {}

def rebuild_pools():
    global gemini_pool, groq_pool, openrouter_pool, custom_pools
    gemini_pool = itertools.cycle(config.GEMINI_API_KEYS) if config.GEMINI_API_KEYS else None
    groq_pool = itertools.cycle(config.GROQ_API_KEYS) if config.GROQ_API_KEYS else None
    openrouter_pool = itertools.cycle(config.OPENROUTER_API_KEYS) if getattr(config, 'OPENROUTER_API_KEYS', []) else None
    
    custom_pools = {}
    for p in getattr(config, 'CUSTOM_PROVIDERS', []):
        slug = re.sub(r'[^a-z0-9]+', '-', p.get('name', '').lower()).strip('-')
        if not slug: continue
        keys = p.get('keys', [])
        if keys:
            custom_pools[slug] = itertools.cycle(keys)

rebuild_pools()


# ════════════════════════════════════════════════════════════════
#  LLM callers
# ════════════════════════════════════════════════════════════════

def _call_groq(prompt: str, model_override: str | None = None) -> str:
    """Send a text-only prompt to Groq and return the raw response."""
    from groq import Groq  # lazy import to avoid load-time crash

    if not config.GROQ_API_KEYS:
        raise ValueError("Missing GROQ_API_KEY in .env file.")

    for _ in range(len(config.GROQ_API_KEYS)):
        try:
            key = key_health.next_healthy_key("groq", config.GROQ_API_KEYS, groq_pool)
        except key_health.NoHealthyKeysError:
            break
            
        for retry in range(3):
            quota_tracker.record_call("groq")
            try:
                client = Groq(api_key=key)
                chat = client.chat.completions.create(
                    model=model_override or config.GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": _get_system_prompt()},
                        {"role": "user",   "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=4096,
                )
                return chat.choices[0].message.content or ""
            except Exception as exc:
                if key_health.is_401(exc):
                    key_health.mark_dead("groq", key)
                    print(f"  ⚠️  Groq key …{key_health.key_id(key)} retired (401 invalid key)")
                    break
                if key_health.is_quota_error(exc):
                    if retry < 2:
                        time.sleep(2 ** (retry + 1))
                        continue
                    key_health.mark_exhausted("groq", key)
                    print(f"  ⚠️  Groq key …{key_health.key_id(key)} quota exhausted — trying next key")
                    break
                raise
    raise key_health.NoHealthyKeysError("groq")


def _call_gemini(
    contents_list: list,
) -> str:
    """
    Send a (possibly multimodal) prompt to Gemini using the next
    key in the round-robin pool.

    ``contents_list`` is a list of strings or dicts like:
        {"mime_type": "image/png", "data": <base64-str>}
    """
    from google import genai  # lazy import
    from google.genai import types
    import base64

    if not config.GEMINI_API_KEYS:
        raise ValueError("Missing GEMINI_API_KEYS in .env file.")

    # Build the content parts list.
    contents = []
    for item in contents_list:
        if isinstance(item, str):
            contents.append(item)
        else:
            contents.append(
                types.Part.from_bytes(
                    data=base64.b64decode(item["data"]),
                    mime_type=item["mime_type"]
                )
            )

    last_err = None
    for _ in range(len(config.GEMINI_API_KEYS)):
        try:
            current_key = key_health.next_healthy_key("gemini", config.GEMINI_API_KEYS, gemini_pool)
        except key_health.NoHealthyKeysError:
            break
            
        for attempt in range(3):
            client = genai.Client(api_key=current_key)
            quota_tracker.record_call("gemini")
            try:
                response = client.models.generate_content(
                    model=config.GEMINI_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=_get_system_prompt(),
                        temperature=0.1,
                        max_output_tokens=4096,
                    ),
                )
                return response.text or ""
            except Exception as e:
                if key_health.is_401(e):
                    key_health.mark_dead("gemini", current_key)
                    print(f"  ⚠️  Gemini key …{key_health.key_id(current_key)} retired (invalid key)")
                    last_err = e
                    break
                    
                msg = str(e)
                if key_health.is_quota_error(e) or "503" in msg or "UNAVAILABLE" in msg:
                    last_err = e
                    if attempt < 2:
                        wait = 5 * (attempt + 1)
                        print(f"  ⚠️  Gemini transient error ({msg[:80]}). Retrying in {wait}s (attempt {attempt+1}/3)...")
                        time.sleep(wait)
                        continue
                        
                    if key_health.is_quota_error(e):
                        key_health.mark_exhausted("gemini", current_key)
                        print(f"  ⚠️  Gemini key …{key_health.key_id(current_key)} quota exhausted — trying next key")
                    break
                raise

    if last_err:
        err_msg = str(last_err)
        if key_health.is_quota_error(last_err) or "quota" in err_msg.lower():
            raise QuotaExhaustedError(f"API quota exhausted for all keys: {err_msg[:120]}") from last_err
        raise last_err
    raise RuntimeError("Failed to call Gemini")

def _call_openrouter(prompt: str, model_override: str | None = None) -> str:
    from openai import OpenAI
    
    if not getattr(config, 'OPENROUTER_API_KEYS', []):
        raise ValueError("Missing OPENROUTER_API_KEYS in settings.")
        
    for _ in range(len(config.OPENROUTER_API_KEYS)):
        try:
            key = key_health.next_healthy_key("openrouter", config.OPENROUTER_API_KEYS, openrouter_pool)
        except key_health.NoHealthyKeysError:
            break
            
        for retry in range(3):
            quota_tracker.record_call("openrouter")
            try:
                client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=key)
                response = client.chat.completions.create(
                    model=model_override or getattr(config, 'OPENROUTER_MODEL', "openrouter/auto"),
                    messages=[
                        {"role": "system", "content": _get_system_prompt()},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.1,
                    max_tokens=4096,
                )
                return response.choices[0].message.content or ""
            except Exception as exc:
                if key_health.is_401(exc):
                    key_health.mark_dead("openrouter", key)
                    print(f"  ⚠️  OpenRouter key …{key_health.key_id(key)} retired (401 invalid key)")
                    break
                if key_health.is_quota_error(exc):
                    if retry < 2:
                        time.sleep(2 ** (retry + 1))
                        continue
                    key_health.mark_exhausted("openrouter", key)
                    print(f"  ⚠️  OpenRouter key …{key_health.key_id(key)} quota exhausted — trying next key")
                    break
                raise
    raise key_health.NoHealthyKeysError("openrouter")


def _call_custom(cfg: dict, prompt: str) -> str:
    from openai import OpenAI
    
    slug = re.sub(r'[^a-z0-9]+', '-', cfg.get('name', '').lower()).strip('-')
    keys = cfg.get('keys', [])
    if not keys:
        raise ValueError(f"Missing keys for custom provider {slug}.")
        
    pool = custom_pools.get(slug)
    if not pool:
        raise ValueError(f"Provider pool missing for {slug}.")
    
    for _ in range(len(keys)):
        try:
            key = key_health.next_healthy_key(slug, keys, pool)
        except key_health.NoHealthyKeysError:
            break
            
        for retry in range(3):
            quota_tracker.record_call(slug)
            try:
                client = OpenAI(base_url=cfg.get("base_url"), api_key=key)
                response = client.chat.completions.create(
                    model=cfg.get("model"),
                    messages=[
                        {"role": "system", "content": _get_system_prompt()},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.1,
                    max_tokens=4096,
                )
                return response.choices[0].message.content or ""
            except Exception as exc:
                if key_health.is_401(exc):
                    key_health.mark_dead(slug, key)
                    print(f"  ⚠️  {slug} key …{key_health.key_id(key)} retired (401 invalid key)")
                    break
                if key_health.is_quota_error(exc):
                    if retry < 2:
                        time.sleep(2 ** (retry + 1))
                        continue
                    key_health.mark_exhausted(slug, key)
                    print(f"  ⚠️  {slug} key …{key_health.key_id(key)} quota exhausted — trying next key")
                    break
                raise
    raise key_health.NoHealthyKeysError(slug)


# ════════════════════════════════════════════════════════════════
#  JSON parsing helper
# ════════════════════════════════════════════════════════════════

def _parse_llm_json(raw: str) -> AnswerBatch:
    """
    Extract a valid AnswerBatch from the LLM's raw text output.

    Handles common quirks:
      • Response wrapped in ```json ... ``` fences.
      • Leading/trailing whitespace or BOM characters.
    """
    # Strip markdown code fences if present.
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()

    # Sometimes the model returns a bare list instead of the wrapper
    # object — handle that gracefully.
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Last-ditch: find the first '{' and last '}'.
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start != -1 and end > start:
            data = json.loads(cleaned[start:end])
        else:
            raise

    if isinstance(data, list):
        data = {"answers": data}

    return AnswerBatch.model_validate(data)


def _retry_low_confidence(batch: AnswerBatch, text_qs: list[ParsedQuestion], threshold: float = 0.70) -> AnswerBatch:
    """Re-solve low-confidence answers with Gemini; returns updated batch."""
    for i, ans in enumerate(batch.answers):
        if ans.confidence >= threshold:
            continue
        q = next((x for x in text_qs if x.index == ans.question_index), None)
        if q is None:
            print(f"  ⚠️  Could not match low-confidence answer (question_index={ans.question_index}). Skipping retry.")
            continue

        print(f"  ⚠️  Low confidence ({ans.confidence*100:.0f}%) on Q{q.index+1}. Retrying with Gemini...")
        retry_prompt = [_build_question_block(q) + "\n\nAnalyze this carefully. Previous reasoning was uncertain."]
        try:
            retry_raw = _call_gemini(retry_prompt)
            parsed = _parse_llm_json(retry_raw)
            if parsed and parsed.answers:
                retry_ans = parsed.answers[0]
                retry_ans.question_index = q.index  # Crucial: ensure original index is preserved
                batch.answers[i] = retry_ans
        except Exception as e:
            print(f"  ❌ Retry failed: {e}")
    return batch


# ════════════════════════════════════════════════════════════════
#  Public API — the main solver entry point
# ════════════════════════════════════════════════════════════════

def solve_questions(
    questions: list[ParsedQuestion],
) -> list[AnswerItem]:
    """
    Route ``questions`` through the dual-API LLM pipeline and
    return a list of validated ``AnswerItem`` results.

    Routing logic:
      1. Partition into text-only vs. multimodal (has image_bytes).
      2. Send text-only batch → Groq.
      3. Send multimodal batch → Gemini.
      4. CASCADE FAILOVER: if Groq raises any exception, merge
         text-only questions into the Gemini queue and retry.
    """
    if not questions:
        return []

    text_qs:  list[ParsedQuestion] = []
    image_qs: list[ParsedQuestion] = []

    for q in questions:
        if q.image_bytes:
            image_qs.append(q)
        else:
            text_qs.append(q)

    results: list[AnswerItem] = []

    # ── Step 0: Cache Check for text-only questions ─────────
    text_misses: list[ParsedQuestion] = []
    cached_answers: list[AnswerItem] = []
    for q in text_qs:
        block = _build_question_block(q)
        ans_dict = answer_cache.get(block)
        if ans_dict:
            cached_answers.append(AnswerItem(
                question_index=q.index,
                selected_options=ans_dict.get("selected_options", []),
                short_answer_text=ans_dict.get("short_answer_text"),
                confidence=ans_dict.get("confidence", 1.0),
                reasoning=ans_dict.get("reasoning", ""),
                from_cache=True
            ))
            print(f"  ⚡ Cache hit for Q{q.index+1}")
        else:
            text_misses.append(q)
    
    text_qs = text_misses
    results.extend(cached_answers)

    # ── Step 1: Groq for text-only questions ────────────────
    groq_failed = False
    if text_qs:
        prompt = "Solve the following questions:\n\n"
        prompt += "\n\n".join(_build_question_block(q) for q in text_qs)

        try:
            raw = _call_groq(prompt)
            batch = _parse_llm_json(raw)
            batch = _retry_low_confidence(batch, text_qs)
            results.extend(batch.answers)
            print(f"  ✅ Groq ({config.GROQ_MODEL}) solved {len(batch.answers)} text question(s)")
        except key_health.NoHealthyKeysError:
            print("  ⚠️  No usable Groq keys left — cascading to Gemini")
            groq_failed = True
        except Exception as exc:
            print(f"  ⚠️  Groq ({config.GROQ_MODEL}) failed: {exc}")
            try:
                print(f"  🔄 Retrying Groq with fallback model ({config.GROQ_FALLBACK_MODEL})...")
                raw = _call_groq(prompt, model_override=config.GROQ_FALLBACK_MODEL)
                batch = _parse_llm_json(raw)
                batch = _retry_low_confidence(batch, text_qs)
                results.extend(batch.answers)
                print(f"  ✅ Groq fallback ({config.GROQ_FALLBACK_MODEL}) solved {len(batch.answers)} text question(s)")
            except Exception as exc_fallback:
                print(f"  ⚠️  Groq fallback also failed ({exc_fallback})")
                try:
                    print(f"  🔄 Retrying Groq with tertiary fallback model ({config.GROQ_TERTIARY_MODEL})...")
                    raw = _call_groq(prompt, model_override=config.GROQ_TERTIARY_MODEL)
                    batch = _parse_llm_json(raw)
                    batch = _retry_low_confidence(batch, text_qs)
                    results.extend(batch.answers)
                    print(f"  ✅ Groq tertiary fallback ({config.GROQ_TERTIARY_MODEL}) solved {len(batch.answers)} text question(s)")
                except Exception as exc_tertiary:
                    print(f"  ⚠️  Groq tertiary fallback also failed ({exc_tertiary}), cascading to OpenRouter …")
                    groq_failed = True

    # ── Step 1.1: OpenRouter ────────────────────────────────
    openrouter_failed = groq_failed
    if openrouter_failed and getattr(config, 'OPENROUTER_API_KEYS', []) and text_qs:
        try:
            print(f"  🔄 Retrying text questions with OpenRouter ({getattr(config, 'OPENROUTER_MODEL', '')})...")
            raw = _call_openrouter(prompt)
            batch = _parse_llm_json(raw)
            batch = _retry_low_confidence(batch, text_qs)
            results.extend(batch.answers)
            print(f"  ✅ OpenRouter solved {len(batch.answers)} text question(s)")
            openrouter_failed = False
        except key_health.NoHealthyKeysError:
            print("  ⚠️  No usable OpenRouter keys left")
        except Exception as exc:
            print(f"  ⚠️  OpenRouter failed: {exc}")

    # ── Step 1.2: Custom Providers ──────────────────────────
    custom_failed = openrouter_failed
    if custom_failed and getattr(config, 'CUSTOM_PROVIDERS', []) and text_qs:
        for cfg in config.CUSTOM_PROVIDERS:
            if not cfg.get("keys"): continue
            slug = re.sub(r'[^a-z0-9]+', '-', cfg.get('name', '').lower()).strip('-')
            try:
                print(f"  🔄 Retrying text questions with Custom Provider ({slug} - {cfg.get('model')})...")
                raw = _call_custom(cfg, prompt)
                batch = _parse_llm_json(raw)
                batch = _retry_low_confidence(batch, text_qs)
                results.extend(batch.answers)
                print(f"  ✅ {slug} solved {len(batch.answers)} text question(s)")
                custom_failed = False
                break
            except key_health.NoHealthyKeysError:
                print(f"  ⚠️  No usable keys left for {slug}")
            except Exception as exc:
                print(f"  ⚠️  {slug} failed: {exc}")

    # ── Step 2: Gemini for multimodal + cascade ─────────────
    gemini_qs = list(image_qs)
    if custom_failed:
        gemini_qs.extend(text_qs)

    if gemini_qs:
        contents_list = ["Solve the following questions:\n\n"]
        for q in gemini_qs:
            # Add text
            contents_list.append(_build_question_block(q))
            # Add image immediately after text if present
            if q.image_bytes:
                b64 = base64.b64encode(q.image_bytes).decode()
                contents_list.append({
                    "mime_type": "image/png",
                    "data": b64,
                })
            contents_list.append("\n\n")

        try:
            raw = _call_gemini(contents_list)
            batch = _parse_llm_json(raw)
            results.extend(batch.answers)
            print(f"  ✅ Gemini solved {len(batch.answers)} question(s)")
        except Exception as exc:
            print(f"  ❌ Gemini also failed ({exc})")

    # ── Save fresh text answers to cache ────────────────────
    for item in results:
        if not getattr(item, "from_cache", False):
            # Only cache answers for original text questions (not multimodal)
            orig_q = next((x for x in questions if x.index == item.question_index and not x.image_bytes), None)
            if orig_q:
                answer_cache.set_answer(_build_question_block(orig_q), item, config.SUBJECT_CONTEXT, config.CONFIDENCE_WARN_THRESHOLD)

    # ── Build an index map for quick lookup ─────────────────
    # If the LLM returned duplicate indices, keep the last one.
    index_map: dict[int, AnswerItem] = {}
    for item in results:
        index_map[item.question_index] = item

    # Ensure every input question has an answer (even if dummy).
    final: list[AnswerItem] = []
    for q in questions:
        if q.index in index_map:
            final.append(index_map[q.index])
        else:
            # Fallback: return empty selection to preserve any existing draft choices.
            final.append(AnswerItem(
                question_index=q.index,
                selected_options=[],
                short_answer_text=None,
                confidence=0.0,
                reasoning="No LLM answer received — fallback applied (preserving draft).",
            ))
    return final
