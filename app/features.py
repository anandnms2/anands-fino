"""Turns closed H1 candles into the three technical factor scores, each from -100 to +100, with a plain-language reading."""
import pandas as pd

from . import indicators as ind


def compute_technicals(df: pd.DataFrame, digits: int = 5) -> dict:
    """`df` holds CLOSED candles only, oldest first."""
    f = f"{{:.{digits}f}}"
    close = df["close"]
    c = float(close.iloc[-1])
    e20, e50, e200 = (float(ind.ema(close, n).iloc[-1]) for n in (20, 50, 200))
    e50_prev = float(ind.ema(close, 50).iloc[-11])
    a = float(ind.atr(df).iloc[-1])
    r = float(ind.rsi(close).iloc[-1])
    hist = ind.macd_hist(close)
    h_now, h_prev = float(hist.iloc[-1]), float(hist.iloc[-2])
    highs, lows = ind.swings(df)

    # ---- trend ----
    parts = []
    if e20 > e50 > e200:
        stack = 45
        parts.append("EMA 20/50/200 stacked bullish")
    elif e20 < e50 < e200:
        stack = -45
        parts.append("EMA 20/50/200 stacked bearish")
    else:
        stack = 10 * (1 if c > e50 else -1) + 10 * (1 if c > e200 else -1)
        parts.append("EMAs mixed, price %s the 50 and %s the 200" % ("above" if c > e50 else "below", "above" if c > e200 else "below"))
    slope = (e50 - e50_prev) / a if a else 0.0
    slope_part = ind.clip(slope * 10, -15, 15)
    h4 = ind.resample_h4(df)
    h4_part = 0.0
    if len(h4) >= 55:
        h4e20, h4e50 = ind.ema(h4["close"], 20).iloc[-1], ind.ema(h4["close"], 50).iloc[-1]
        h4_part = 20.0 if h4e20 > h4e50 else -20.0
        parts.append("H4 trend " + ("up" if h4_part > 0 else "down"))
    structure = 0.0
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        if hh and hl:
            structure = 20.0
            parts.append("higher highs and higher lows")
        elif (not hh) and (not hl):
            structure = -20.0
            parts.append("lower highs and lower lows")
    trend = ind.clip(stack + slope_part + h4_part + structure)

    # ---- pattern and levels ----
    pparts, pattern = [], 0.0
    pat = ind.candle_pattern(df)
    if pat:
        pattern += 40 * pat[1]
        pparts.append(pat[0])
    recent = df.iloc[-4:-1]
    prior_highs = [p for i, p in highs if i < len(df) - 4]
    prior_lows = [p for i, p in lows if i < len(df) - 4]
    if prior_highs and c > prior_highs[-1] and (recent["high"] >= prior_highs[-1] * 0.999).any():
        pattern += 35
        pparts.append("broke resistance %s" % f.format(prior_highs[-1]))
    elif prior_lows and c < prior_lows[-1] and (recent["low"] <= prior_lows[-1] * 1.001).any():
        pattern -= 35
        pparts.append("broke support %s" % f.format(prior_lows[-1]))
    if lows and 0 <= c - lows[-1][1] <= 0.5 * a:
        pattern += 20
        pparts.append("sitting on support %s" % f.format(lows[-1][1]))
    if highs and 0 <= highs[-1][1] - c <= 0.5 * a:
        pattern -= 20
        pparts.append("pressing resistance %s" % f.format(highs[-1][1]))
    pattern = ind.clip(pattern)

    # ---- momentum ----
    rsi_part = (r - 50) * 1.2
    if r > 75:
        rsi_part -= (r - 75) * 3
    elif r < 25:
        rsi_part += (25 - r) * 3
    if h_now > 0:
        macd_part = 40 if h_now > h_prev else 20
    else:
        macd_part = -40 if h_now < h_prev else -20
    momentum = ind.clip(rsi_part + macd_part)
    mparts = [f"RSI {r:.0f}", "MACD histogram " + ("positive" if h_now > 0 else "negative") + (" and rising" if h_now > h_prev else " and falling")]
    if r > 75 or r < 25:
        mparts.append("stretched, fades the signal")

    return {
        "close": c, "atr": a, "ema20": e20, "ema50": e50, "ema200": e200, "rsi": r,
        "swing_highs": [p for _, p in highs[-4:]], "swing_lows": [p for _, p in lows[-4:]],
        "trend": {"score": trend, "reading": "; ".join(parts)},
        "pattern": {"score": pattern, "reading": "; ".join(pparts) or "no pattern or level event on the last candles"},
        "momentum": {"score": momentum, "reading": "; ".join(mparts)},
    }
