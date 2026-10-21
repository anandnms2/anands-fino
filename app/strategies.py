"""Candidate trading strategies, written once and used twice: the backtester replays them on history, and the live engine asks the
same code for a signal on the last closed candle. Each takes closed candles (columns time, open, high, low, close) and returns a
Series of +1 (buy), -1 (sell) or 0 on every bar, using only data up to and including that bar."""
import pandas as pd

from . import indicators as ind


def _prep(df: pd.DataFrame) -> dict:
    c, h, l = df["close"], df["high"], df["low"]
    return {"c": c, "h": h, "l": l, "e20": ind.ema(c, 20), "e50": ind.ema(c, 50), "e200": ind.ema(c, 200), "rsi": ind.rsi(c), "rsi2": ind.rsi(c, 2),
            "hist": ind.macd_hist(c), "atr": ind.atr(df)}


def pullback(df: pd.DataFrame, p: dict | None = None) -> pd.Series:
    """Trend pullback: with the 50 over the 200, buy when price dips to the 20 and closes back above it (mirror for sells)."""
    p = p or _prep(df)
    up = (p["e50"] > p["e200"]) & (p["c"] > p["e200"])
    dn = (p["e50"] < p["e200"]) & (p["c"] < p["e200"])
    buy = up & (p["l"] <= p["e20"]) & (p["c"] > p["e20"]) & p["rsi"].between(35, 65)
    sell = dn & (p["h"] >= p["e20"]) & (p["c"] < p["e20"]) & p["rsi"].between(35, 65)
    return buy.astype(int) - sell.astype(int)


def breakout(df: pd.DataFrame, p: dict | None = None) -> pd.Series:
    """Trend breakout: a close beyond the last 20 candles' high or low, in the direction of the 50/200 trend."""
    p = p or _prep(df)
    hi, lo = p["h"].rolling(20).max().shift(1), p["l"].rolling(20).min().shift(1)
    buy = (p["c"] > hi) & (p["e50"] > p["e200"])
    sell = (p["c"] < lo) & (p["e50"] < p["e200"])
    return buy.astype(int) - sell.astype(int)


def meanrev(df: pd.DataFrame, p: dict | None = None) -> pd.Series:
    """Short-term snap-back: buy an extreme RSI(2) dip inside an uptrend, sell an extreme spike inside a downtrend."""
    p = p or _prep(df)
    buy = (p["rsi2"] < 10) & (p["c"] > p["e200"])
    sell = (p["rsi2"] > 90) & (p["c"] < p["e200"])
    return buy.astype(int) - sell.astype(int)


def macdturn(df: pd.DataFrame, p: dict | None = None) -> pd.Series:
    """Momentum turn with the trend: MACD histogram crosses zero in the direction of the 50/200 trend."""
    p = p or _prep(df)
    cross_up = (p["hist"] > 0) & (p["hist"].shift(1) <= 0)
    cross_dn = (p["hist"] < 0) & (p["hist"].shift(1) >= 0)
    buy = cross_up & (p["e50"] > p["e200"]) & (p["c"] > p["e50"])
    sell = cross_dn & (p["e50"] < p["e200"]) & (p["c"] < p["e50"])
    return buy.astype(int) - sell.astype(int)


def stackstate(df: pd.DataFrame, p: dict | None = None) -> pd.Series:
    """The old approach, as a baseline: enter whenever EMAs are stacked, MACD agrees and RSI is on the right side of 50."""
    p = p or _prep(df)
    buy = (p["e20"] > p["e50"]) & (p["e50"] > p["e200"]) & (p["hist"] > 0) & (p["rsi"] > 50)
    sell = (p["e20"] < p["e50"]) & (p["e50"] < p["e200"]) & (p["hist"] < 0) & (p["rsi"] < 50)
    return buy.astype(int) - sell.astype(int)


STRATEGIES = {"pullback": pullback, "breakout": breakout, "meanrev": meanrev, "macdturn": macdturn, "stackstate": stackstate}
BASELINE = "stackstate"   # not allowed to trade live: it only shows what the old style of entry earns on the same data
SESSIONS = {"all": None, "london_ny": (7, 17)}   # UTC hours


def in_session(times: pd.Series, session: str) -> pd.Series:
    rng = SESSIONS.get(session)
    if rng is None:
        return pd.Series(True, index=times.index)
    hrs = pd.to_datetime(times, unit="s", utc=True).dt.hour if pd.api.types.is_numeric_dtype(times) else pd.to_datetime(times, utc=True).dt.hour
    return (hrs >= rng[0]) & (hrs < rng[1])


def signal(df: pd.DataFrame, name: str, session: str = "all") -> pd.Series:
    s = STRATEGIES[name](df)
    return s.where(in_session(df["time"], session), 0)
