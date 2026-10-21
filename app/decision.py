"""Claude's final yes or no. Fails closed: any error, bad reply or exhausted budget means NO."""
import json

from .llm import LLMError, claude_chat, extract_json

SYSTEM = """You are the final decision-maker on a forex and metals desk. Code has already scored the setup and checked the hard risk limits. You decide whether to take the trade.

Be a sceptic, not an obstructionist. The score, factor agreement, spread cost and risk limits are already checked by code, and trades here are short scalps lasting minutes to an hour or two on the chart given.
Answer NO only for a concrete flaw: the target is out of reach for the chart's ATR, the stop is on the wrong side of a clear level, a high-impact event is due within about 90 minutes, news clearly contradicts the direction, or price is already exhausted at the far end of its range. Do not say NO for "momentum is fading", "price is near support" or events many hours away, and do not penalise the idea for the spread-driven stop distance. When the setup is coherent, say YES and give your confidence honestly (0.55 or more).
You cannot change entry, stop or target. Judge the setup as given.

Reply with JSON only, no other text:
{"decision": "YES" or "NO", "confidence": number from 0 to 1, "thesis": "max 25 words", "against": "max 25 words", "invalidation": "max 20 words", "main_risk": "max 20 words"}
Keep every field short: a long reply is cut off and counts as NO."""


def build_prompt(ctx: dict) -> str:
    return "Setup for your decision (all data is up to the last closed candle; the chart timeframe is in the data, and a trade here lasts minutes to hours):\n" + json.dumps(ctx, indent=1, default=str)


def decide(ctx: dict, model: str | None = None) -> dict:
    """Returns {decision, confidence, thesis, against, invalidation, main_risk, model, error?}."""
    try:
        for attempt in (1, 2):   # a cut-off or malformed reply is retried once before it counts as NO
            raw = claude_chat(SYSTEM, build_prompt(ctx), model=model, max_tokens=1200)
            try:
                obj = extract_json(raw)
                break
            except LLMError:
                if attempt == 2:
                    raise
        decision = str(obj.get("decision", "")).strip().upper()
        conf = float(obj.get("confidence", 0))
        if decision not in ("YES", "NO") or not (0.0 <= conf <= 1.0):
            raise LLMError("reply had an invalid decision or confidence")
        return {
            "decision": decision, "confidence": conf,
            "thesis": str(obj.get("thesis", ""))[:600], "against": str(obj.get("against", ""))[:400],
            "invalidation": str(obj.get("invalidation", ""))[:400], "main_risk": str(obj.get("main_risk", ""))[:400],
        }
    except (LLMError, ValueError, TypeError, AttributeError) as e:
        return {"decision": "NO", "confidence": 0.0, "thesis": "", "against": "", "invalidation": "",
                "main_risk": "", "error": str(e)}
