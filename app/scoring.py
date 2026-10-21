"""Factor scores outside the pure price technicals, the weighted composite, and the trade levels."""
from . import indicators as ind

PEERS = {"XAUUSD": "XAGUSD", "XAGUSD": "XAUUSD", "EURUSD": "GBPUSD", "GBPUSD": "EURUSD"}
COT_SYMBOLS = {"XAUUSD", "XAGUSD", "EURUSD", "GBPUSD"}


def macro_score(symbol: str, macro: dict | None) -> tuple[float | None, str]:
    """USD-quoted symbols rise when the dollar weakens; metals also rise when real yields fall."""
    if not macro:
        return None, "macro data not loaded"
    s = symbol.upper()
    usd, ry = macro.get("usd_index"), macro.get("real_yield")
    usd_part = ind.clip(-usd["pct5"] / 0.6 * 50, -70, 70) if usd else None
    ry_part = ind.clip(-ry["chg5"] / 0.15 * 50, -70, 70) if ry else None
    if s.startswith(("XAU", "XAG")):
        parts = [(ry_part, 0.6), (usd_part, 0.4)]
    elif s.endswith("USD"):
        parts = [(usd_part, 1.0)]
    elif s.startswith("USD"):
        parts = [(-usd_part if usd_part is not None else None, 1.0)]
    else:
        return None, "no dollar leg to score"
    parts = [(v, w) for v, w in parts if v is not None]
    if not parts:
        return None, "macro series missing"
    score = sum(v * w for v, w in parts) / sum(w for _, w in parts)
    bits = []
    if ry:
        bits.append(f"10y real yield {ry['latest']:.2f}% ({ry['chg5']:+.2f} pts over 5 obs)")
    if usd:
        bits.append(f"broad dollar index {usd['pct5']:+.2f}% over 5 obs")
    return ind.clip(score), "; ".join(bits)


def news_score(symbol: str, items: list) -> tuple[float | None, str]:
    """`items` are NewsItem rows from the last 24 hours."""
    tagged = [i for i in items if i.tags is not None]
    if not tagged:
        return None, "no tagged headlines yet"
    vals = [i.tags.get(symbol, 0) for i in tagged]
    bull, bear = vals.count(1), vals.count(-1)
    neutral = len(vals) - bull - bear
    directional = bull + bear
    score = ind.clip(100.0 * (bull - bear) / max(directional + 2, 5))
    return score, f"{len(tagged)} tagged headlines in 24h: {bull} bullish, {bear} bearish, {neutral} neutral"


def positioning_score(symbol: str, cot: dict | None) -> tuple[float | None, str]:
    row = (cot or {}).get(symbol)
    if not row:
        return None, "no COT reading for this symbol"
    p = row["percentile"]
    if p >= 90:
        score, note = -60.0, "crowded long, contrarian drag"
    elif p <= 10:
        score, note = 60.0, "crowded short, contrarian lift"
    else:
        score, note = (p - 50) / 50 * 20, "not at an extreme, mild trend following"
    return score, f"funds net {row['net']:,.0f}, {p:.0f}th percentile of {row['weeks']} weeks ({row['date']}): {note}"


def intermarket_score(symbol: str, techs: dict) -> tuple[float | None, str]:
    peer = PEERS.get(symbol)
    if not peer or peer not in techs:
        return None, "no peer symbol to confirm against"
    t = techs[peer]["trend"]["score"]
    return ind.clip(t * 0.6), f"{peer} trend score {t:+.0f} {'confirms' if t > 0 else 'contradicts' if t < 0 else 'is flat'}"


def combine(factors: dict, weights: dict, threshold: float, min_agree: int, min_agree_pct: float | None = None) -> dict:
    avail = {k: v for k, v in factors.items() if v["score"] is not None}
    wsum = sum(weights.get(k, 0) for k in avail)
    comp = sum(weights.get(k, 0) * v["score"] for k, v in avail.items()) / wsum if wsum else 0.0
    d = 1 if comp > 0 else -1 if comp < 0 else 0
    agree = sum(1 for v in avail.values() if d and v["score"] * d >= 20)
    bias = "NONE"
    ok = agree >= min_agree if min_agree_pct is None else (len(avail) >= 3 and agree / len(avail) * 100 >= min_agree_pct - 1e-9)
    if ok:
        bias = "BUY" if comp >= threshold else "SELL" if comp <= -threshold else "NONE"
    return {"score": round(comp, 2), "agree": agree, "available": len(avail), "bias": bias}


def build_levels(bias: str, tech: dict, tick: dict, digits: int, stop_mult: float, rr: float) -> dict:
    """Entry at market, stop beyond a nearby swing when one exists (1 to 2.5 ATR away), else a fixed ATR multiple."""
    a = tech["atr"]
    buy = bias == "BUY"
    entry = tick["ask"] if buy else tick["bid"]
    sl = entry - stop_mult * a if buy else entry + stop_mult * a
    if buy:
        cands = [p for p in tech["swing_lows"] if 1.0 * a <= entry - p <= 2.5 * a]
        if cands:
            sl = max(cands) - 0.25 * a
    else:
        cands = [p for p in tech["swing_highs"] if 1.0 * a <= p - entry <= 2.5 * a]
        if cands:
            sl = min(cands) + 0.25 * a
    stop = abs(entry - sl)
    tp = entry + rr * stop if buy else entry - rr * stop
    return {"entry": round(entry, digits), "sl": round(sl, digits), "tp": round(tp, digits),
            "atr": round(a, digits), "stop": round(stop, digits), "rr": round(rr, 2)}
