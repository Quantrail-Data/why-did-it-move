"""Every call into this module is narration or intent-parsing over ALREADY
COMPUTED structured data - never raw event rows, never SQL generation. That
split is the trustworthiness guarantee: the LLM cannot invent a number that
isn't already in the JSON we hand it, because it never sees anything else.
"""
import json

from . import config, metrics

NARRATE_SYSTEM_PROMPT = (
    "You are a root-cause analyst narrating a pre-computed ad-metrics investigation. "
    "You are given ONLY structured, already-computed numbers as JSON - never invent, "
    "estimate, or round a number that isn't present in the input. Write 2-4 sentences: "
    "state what moved, by how much (cite the actual percentages/values given), the "
    "specific segment or factor responsible, and what was checked and ruled out. If "
    "responsible_segment has a 'refined_by' field, that's a more specific intersection "
    "found within the segment (e.g. segment=country/IN refined_by=device_model/iPhone) - "
    "mention that tighter localization instead of just the outer segment. Plain "
    "language, no jargon, written for a product manager, not a database engineer."
)

ASK_SYSTEM_PROMPT = (
    "You translate a free-text question into a structured lookup, for a system that answers "
    "questions about this ad-metrics dataset: revenue, fill rate, render rate, eCPM, CTR, "
    "and the app/device/geo/advertiser segments and days behind them. "
    'Respond with ONLY a JSON object, no markdown fences, no commentary: '
    '{"in_scope": true or false, '
    '"metric": one of [' + ", ".join(f'"{m}"' for m in metrics.HEADLINE_METRICS) + '] or null, '
    '"day": "YYYY-MM-DD" or null, '
    '"dimension": one of the listed dimension names or null, "value": the segment value '
    "as a string or null}. "
    "DEFAULT TO in_scope=true. Judge scope by TOPIC ONLY, never by grammar, spelling, "
    "punctuation, or how casually the question is phrased - a badly-worded or informal "
    "question about deviations, causes, percentages, or 'what happened' is still in scope. "
    "If the prompt below says an investigation is currently open, ANY vague or short "
    "follow-up ('why', 'what caused this', 'explain', 'give me an RCA', 'how much', 'what "
    "about X') is in scope and refers to that investigation, even with no metric/day/segment "
    "named explicitly - resolve it from context, don't refuse it. "
    "Only set in_scope to false when the question is CLEARLY about something with no "
    "plausible connection to ad-metrics or the open investigation at all - e.g. the weather, "
    "cooking, sports scores, personal advice, or an unrelated company/product. When (and "
    "only when) you set in_scope to false, also set metric/day/dimension/value to null. "
    "Use null for day if the question doesn't specify one."
)


def _call_openai(system_prompt: str, user_prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model=config.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content


def _call_anthropic(system_prompt: str, user_prompt: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=600,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return resp.content[0].text


def _call_gemini(system_prompt: str, user_prompt: str) -> str:
    import google.generativeai as genai

    genai.configure(api_key=config.GEMINI_API_KEY)
    model = genai.GenerativeModel(config.GEMINI_MODEL, system_instruction=system_prompt)
    resp = model.generate_content(user_prompt)
    return resp.text


_PROVIDERS = {
    "openai": _call_openai,
    "anthropic": _call_anthropic,
    "gemini": _call_gemini,
}


def _call_llm(system_prompt: str, user_prompt: str) -> str:
    fn = _PROVIDERS.get(config.ACTIVE_LLM_PROVIDER)
    if fn is None:
        raise ValueError(
            f"Unknown ACTIVE_LLM_PROVIDER={config.ACTIVE_LLM_PROVIDER!r}, "
            f"expected one of {list(_PROVIDERS)}"
        )
    return fn(system_prompt, user_prompt)


def narrate(findings: dict) -> str:
    """findings must already be fully computed - this call cannot add facts."""
    prompt = "Investigation findings (JSON):\n" + json.dumps(findings, default=str, indent=2)
    return _call_llm(NARRATE_SYSTEM_PROMPT, prompt)


def parse_question(question: str, schema_hint: str) -> dict:
    """Free-text question -> {metric, day, dimension, value}. Used by /api/ask
    to decide WHAT to look up; the lookup itself still runs as a ClickHouse
    query, never as something the LLM computes directly."""
    prompt = f"{schema_hint}\n\nUser question: {question}"
    raw = _call_llm(ASK_SYSTEM_PROMPT, prompt)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    return json.loads(cleaned.strip())
