"""Indicators and pattern detection. Plain pandas, no TA-Lib, so it installs anywhere."""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0).where(loss != 0, 100.0)


def macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    line = ema(close, fast) - ema(close, slow)
    return line - ema(line, signal)


def swings(df: pd.DataFrame, k: int = 3) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Confirmed pivot highs and lows: a bar that is the extreme of k bars on each side."""
    highs, lows = [], []
    h, l = df["high"].values, df["low"].values
    for i in range(k, len(df) - k):
        if h[i] == h[i - k:i + k + 1].max():
            highs.append((i, float(h[i])))
        if l[i] == l[i - k:i + k + 1].min():
            lows.append((i, float(l[i])))
    return highs, lows


def candle_pattern(df: pd.DataFrame) -> tuple[str, int] | None:
    """Engulfing and pin bars on the last closed candle. Returns (name, +1 bullish / -1 bearish)."""
    if len(df) < 3:
        return None
    c, p = df.iloc[-1], df.iloc[-2]
    body, rng = abs(c.close - c.open), c.high - c.low
    if rng <= 0:
        return None
    pbody = abs(p.close - p.open)
    if p.close < p.open and c.close > c.open and c.open <= p.close and c.close >= p.open and body > pbody:
        return "bullish engulfing", 1
    if p.close > p.open and c.close < c.open and c.open >= p.close and c.close <= p.open and body > pbody:
        return "bearish engulfing", -1
    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low
    if lower >= 2 * max(body, rng * 0.05) and lower >= 0.55 * rng and min(c.open, c.close) >= c.low + 0.6 * rng:
        return "bullish pin bar", 1
    if upper >= 2 * max(body, rng * 0.05) and upper >= 0.55 * rng and max(c.open, c.close) <= c.high - 0.6 * rng:
        return "bearish pin bar", -1
    return None


def resample_h4(df: pd.DataFrame) -> pd.DataFrame:
    """H4 candles from H1, dropping the bucket that is still forming."""
    x = df.set_index("time")[["open", "high", "low", "close"]]
    h4 = x.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    cnt = x["close"].resample("4h").count()
    h4 = h4[cnt.reindex(h4.index) >= 4]
    return h4.reset_index()


def clip(x: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, x)))
