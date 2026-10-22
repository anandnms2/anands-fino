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


# ---------------------------------------------------------------- session and volatility strategies
def _clock(df: pd.DataFrame):
    """(UTC day, hour of day as a fraction) for every candle."""
    t = df["time"]
    dt = pd.to_datetime(t, unit="s", utc=True) if pd.api.types.is_numeric_dtype(t) else pd.to_datetime(t, utc=True)
    return dt.dt.date, dt.dt.hour + dt.dt.minute / 60


def _range_break(df: pd.DataFrame, r0: float, r1: float, w0: float, w1: float) -> pd.Series:
    """Build the high-low range of the hours r0..r1 (UTC) each day; the first close beyond it between w0 and w1 is the signal."""
    day, hr = _clock(df)
    h, l, c = df["high"], df["low"], df["close"]
    inr = (hr >= r0) & (hr < r1)
    hi, lo = h.where(inr).groupby(day).transform("max"), l.where(inr).groupby(day).transform("min")
    win = (hr >= w0) & (hr < w1) & hi.notna()
    up, dn = c > hi, c < lo
    buy = win & up & ~up.shift(1, fill_value=False)       # only the first close beyond the range, not every candle after it
    sell = win & dn & ~dn.shift(1, fill_value=False)
    return buy.astype(int) - sell.astype(int)


def orb_london(df: pd.DataFrame, p: dict | None = None) -> pd.Series:
    """London open range breakout: the 07:00-08:00 UTC range, traded when price closes beyond it before 13:00."""
    return _range_break(df, 7, 8, 8, 13)


def asian_break(df: pd.DataFrame, p: dict | None = None) -> pd.Series:
    """Asian range breakout: the quiet 00:00-07:00 UTC range, traded when London closes beyond it before 12:00."""
    return _range_break(df, 0, 7, 7, 12)


def _bands(c: pd.Series, n: int = 20, k: float = 2.0):
    m, sd = c.rolling(n).mean(), c.rolling(n).std()
    return m - k * sd, m, m + k * sd


def squeeze(df: pd.DataFrame, p: dict | None = None) -> pd.Series:
    """Volatility squeeze: bands were among the tightest of the last 100 candles within the last 5, then a close outside them, with the 50 average."""
    p = p or _prep(df)
    lo, mid, hi = _bands(p["c"])
    bw = (hi - lo) / mid
    tight = (bw <= bw.rolling(100).quantile(0.2)).rolling(5).max().shift(1) > 0
    buy = tight & (p["c"] > hi) & (p["c"] > p["e50"])
    sell = tight & (p["c"] < lo) & (p["c"] < p["e50"])
    return buy.astype(int) - sell.astype(int)


def _adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, dn = df["high"].diff(), -df["low"].diff()
    pdm, mdm = up.where((up > dn) & (up > 0), 0.0), dn.where((dn > up) & (dn > 0), 0.0)
    a = ind.atr(df, n)
    pdi, mdi = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / a, 100 * mdm.ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, float("nan"))
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def bbrev(df: pd.DataFrame, p: dict | None = None) -> pd.Series:
    """Range fade: when the market is not trending (ADX under 20), buy a close below the lower band with RSI under 35, sell the mirror."""
    p = p or _prep(df)
    lo, _, hi = _bands(p["c"])
    flat = _adx(df) < 20
    buy = flat & (p["c"] < lo) & (p["rsi"] < 35)
    sell = flat & (p["c"] > hi) & (p["rsi"] > 65)
    return buy.astype(int) - sell.astype(int)


def supertrend(df: pd.DataFrame, p: dict | None = None) -> pd.Series:
    """Supertrend (ATR 10, x3): trade the candle where the trend line flips."""
    import numpy as np

    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    a = ind.atr(df, 10).to_numpy(float)
    mid = (h + l) / 2
    ub, lb = mid + 3 * a, mid - 3 * a
    n = len(df)
    fu, fl, d = np.full(n, np.nan), np.full(n, np.nan), np.zeros(n, dtype=int)
    for i in range(1, n):
        if np.isnan(ub[i]) or np.isnan(ub[i - 1]):
            continue
        fu[i] = ub[i] if (np.isnan(fu[i - 1]) or ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (np.isnan(fl[i - 1]) or lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        d[i] = 1 if c[i] > fu[i] else -1 if c[i] < fl[i] else d[i - 1]
    s = pd.Series(d, index=df.index)
    flip = s != s.shift(1, fill_value=0)
    prev = s.shift(1, fill_value=0)
    return ((s == 1) & flip & (prev == -1)).astype(int) - ((s == -1) & flip & (prev == 1)).astype(int)


def donchian55(df: pd.DataFrame, p: dict | None = None) -> pd.Series:
    """Turtle breakout: a close beyond the last 55 candles' high or low, in the direction of the 200 average."""
    p = p or _prep(df)
    hi, lo = p["h"].rolling(55).max().shift(1), p["l"].rolling(55).min().shift(1)
    buy = (p["c"] > hi) & (p["c"] > p["e200"])
    sell = (p["c"] < lo) & (p["c"] < p["e200"])
    return buy.astype(int) - sell.astype(int)


STRATEGIES = {"pullback": pullback, "breakout": breakout, "meanrev": meanrev, "macdturn": macdturn, "orb_london": orb_london, "asian_break": asian_break,
              "squeeze": squeeze, "bbrev": bbrev, "supertrend": supertrend, "donchian55": donchian55, "stackstate": stackstate}
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
