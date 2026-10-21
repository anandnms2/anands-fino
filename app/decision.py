"""Claude's final yes or no. Fails closed: any error, bad reply or exhausted budget means NO."""
import json

from .llm import LLMError, claude_chat, extract_json

SYSTEM = """You are the final decision-maker on a forex and metals desk. Code has already scored the setup and checked the hard risk limits. You decide whether to take the trade.

Default to NO. Answer YES only when the evidence agrees, the levels make sense, and you cannot name a strong reason to wait.
You cannot change entry, stop or target. Judge the setup as given.

Before deciding, think about: the strongest case against the trade; whether the stop sits beyond a real level; event risk in the next hours; news that contradicts the score; crowded positioning; whether price is stretched.

Reply with JSON only, no other text:
{"decision": "YES" or "NO", "confidence": number from 0 to 1, "thesis": "one or two sentences", "against": "the strongest reason not to trade", "invalidation": "what would prove the idea wrong", "main_risk": "one sentence"}"""


def build_prompt(ctx: dict) -> str:
    return "Setup for your decision (all data is up to the last closed candle; the chart timeframe is in the data, and a trade here lasts minutes to hours):\n" + json.dumps(ctx, indent=1, default=str)


def decide(ctx: dict, model: str | None = None) -> dict:
    """Returns {decision, confidence, thesis, against, invalidation, main_risk, model, error?}."""
    try:
        raw = claude_chat(SYSTEM, build_prompt(ctx), model=model, max_tokens=700)
        obj = extract_json(raw)
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
