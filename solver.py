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
import textwrap
from typing import Optional

from pydantic import BaseModel, Field

import config
from form_parser import ParsedQuestion


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
#  Gemini key-pool rotation
# ════════════════════════════════════════════════════════════════

class _GeminiKeyPool:
    """Simple round-robin iterator over the Gemini API keys."""
    def __init__(self) -> None:
        self._keys = list(config.GEMINI_API_KEYS)
        self._idx = 0

    def next_key(self) -> str:
        key = self._keys[self._idx % len(self._keys)]
        self._idx += 1
        return key

_gemini_pool = _GeminiKeyPool()


# ════════════════════════════════════════════════════════════════
#  LLM callers
# ════════════════════════════════════════════════════════════════

def _call_groq(prompt: str, model_override: str | None = None) -> str:
    """Send a text-only prompt to Groq and return the raw response."""
    from groq import Groq  # lazy import to avoid load-time crash

    client = Groq(api_key=config.GROQ_API_KEY)
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


def _call_gemini(
    prompt: str,
    image_parts: list[dict] | None = None,
) -> str:
    """
    Send a (possibly multimodal) prompt to Gemini using the next
    key in the round-robin pool.

    ``image_parts`` is a list of dicts like:
        {"mime_type": "image/png", "data": <base64-str>}
    """
    from google import genai  # lazy import
    from google.genai import types

    api_key = _gemini_pool.next_key()
    client = genai.Client(api_key=api_key)

    # Build the content parts list.
    contents = [prompt]
    if image_parts:
        for img in image_parts:
            # We received base64 encoded strings, so decode them for the new SDK
            import base64
            contents.append(
                types.Part.from_bytes(
                    data=base64.b64decode(img["data"]),
                    mime_type=img["mime_type"]
                )
            )

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

    # ── Step 1: Groq for text-only questions ────────────────
    groq_failed = False
    if text_qs:
        prompt = "Solve the following questions:\n\n"
        prompt += "\n\n".join(_build_question_block(q) for q in text_qs)

        try:
            raw = _call_groq(prompt)
            batch = _parse_llm_json(raw)
            results.extend(batch.answers)
            print(f"  ✅ Groq ({config.GROQ_MODEL}) solved {len(batch.answers)} text question(s)")
        except Exception as exc:
            print(f"  ⚠️  Groq ({config.GROQ_MODEL}) failed: {exc}")
            try:
                print(f"  🔄 Retrying Groq with fallback model ({config.GROQ_FALLBACK_MODEL})...")
                raw = _call_groq(prompt, model_override=config.GROQ_FALLBACK_MODEL)
                batch = _parse_llm_json(raw)
                results.extend(batch.answers)
                print(f"  ✅ Groq fallback ({config.GROQ_FALLBACK_MODEL}) solved {len(batch.answers)} text question(s)")
            except Exception as exc_fallback:
                print(f"  ⚠️  Groq fallback also failed ({exc_fallback})")
                try:
                    print(f"  🔄 Retrying Groq with tertiary fallback model ({config.GROQ_TERTIARY_MODEL})...")
                    raw = _call_groq(prompt, model_override=config.GROQ_TERTIARY_MODEL)
                    batch = _parse_llm_json(raw)
                    results.extend(batch.answers)
                    print(f"  ✅ Groq tertiary fallback ({config.GROQ_TERTIARY_MODEL}) solved {len(batch.answers)} text question(s)")
                except Exception as exc_tertiary:
                    print(f"  ⚠️  Groq tertiary fallback also failed ({exc_tertiary}), cascading to Gemini …")
                    groq_failed = True

    # ── Step 2: Gemini for multimodal + cascade ─────────────
    gemini_qs = list(image_qs)
    if groq_failed:
        gemini_qs.extend(text_qs)

    if gemini_qs:
        prompt = "Solve the following questions:\n\n"
        prompt += "\n\n".join(_build_question_block(q) for q in gemini_qs)

        # Collect image parts.
        img_parts: list[dict] = []
        for q in gemini_qs:
            if q.image_bytes:
                b64 = base64.b64encode(q.image_bytes).decode()
                img_parts.append({
                    "mime_type": "image/png",
                    "data": b64,
                })
                prompt += (
                    f"\n\n[Attached image for Question {q.index} follows "
                    f"as inline_data]"
                )

        try:
            raw = _call_gemini(prompt, img_parts if img_parts else None)
            batch = _parse_llm_json(raw)
            results.extend(batch.answers)
            print(f"  ✅ Gemini solved {len(batch.answers)} question(s)")
        except Exception as exc:
            print(f"  ❌ Gemini also failed ({exc})")

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
