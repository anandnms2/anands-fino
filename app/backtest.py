"""Backtester: replays each strategy on real history with the spread paid on every trade, chooses a setup on the first 60% of the data
and judges it on the last 40%, which it never saw. A setup is switched on for live trading only if it also earns on that unseen part."""
import threading
import time

import httpx
import numpy as np
import pandas as pd

from . import indicators as ind
from . import strategies as S
from .config import get_settings
from .db import kv_get, kv_set, log, utcnow
from .paper import UA, YAHOO, paper_spec

RANGE = {5: ("5m", "60d"), 15: ("15m", "60d"), 30: ("30m", "60d"), 60: ("60m", "730d")}
TIMEFRAMES = (60,)               # only hourly candles go back far enough (about 24 months); Yahoo gives just 60 days of 5m/15m/30m
MIN_DAYS = 180                   # a backtest must cover at least 6 months or it cannot be called proven
STOPS = (1.0, 1.5, 2.0)          # stop distance in ATRs
RRS = (1.2, 1.5, 2.0)            # target as a multiple of the stop
SESSIONS = ("all", "london_ny")
MAX_BARS = 48                    # a trade still open after this many candles is closed at the market
MIN_TRAIN, MIN_TEST = 25, 12
# what a setup must show on the unseen 40% before live trading may use it
OK_TEST_EXPECTANCY, OK_TEST_PF = 0.05, 1.15
MIN_TEST_STRONG = 30            # a setup judged on fewer unseen trades than this is too thin to trust
MIN_T = 1.0                     # and the average win must be at least one standard error above zero, else it is indistinguishable from luck
MIN_ROBUST = 0.4                # and at least this share of ALL tested settings must also be positive on the unseen part: an edge that exists only for one lucky setting is noise


def fetch_history(symbol: str, tf: int) -> pd.DataFrame:
    iv, rng = RANGE[tf]
    last = "no data"
    for ysym in YAHOO.get(symbol, [f"{symbol}=X"]):
        try:
            r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}", params={"interval": iv, "range": rng}, headers=UA, timeout=40, follow_redirects=True)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            df = pd.DataFrame({"time": res["timestamp"], "open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"]}).dropna()
            if len(df) >= 400:
                return df.reset_index(drop=True)
            last = f"{ysym}: only {len(df)} candles"
        except Exception as e:  # noqa: BLE001
            last = f"{ysym}: {type(e).__name__}"
    raise RuntimeError(f"no history for {symbol} {tf}m ({last})")


def simulate(df: pd.DataFrame, sig: pd.Series, stop_k: float, rr: float, spread: float, lo: int, hi: int) -> np.ndarray:
    """Net result of every trade in R (1R = the stop), for signals on candles lo..hi-1. One trade at a time, entry at the next open,
    spread paid on entry, a stop and a target that touch in the same candle count as the stop (the pessimistic order)."""
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    atr = ind.atr(df).to_numpy(float)
    s = sig.to_numpy(int)
    out, i, n = [], max(lo, 210), len(df)
    hi = min(hi, n - 2)
    while i < hi:
        d = s[i]
        if d == 0 or not np.isfinite(atr[i]) or atr[i] <= 0:
            i += 1
            continue
        stop = stop_k * atr[i]
        if stop < 2 * spread:      # the spread alone would be half the risk
            i += 1
            continue
        hs = spread / 2                      # candles are mid prices: buy at the ask (mid + half), sell at the bid (mid - half)
        entry = o[i + 1] + hs if d > 0 else o[i + 1] - hs
        sl, tp = (entry - stop, entry + rr * stop) if d > 0 else (entry + stop, entry - rr * stop)
        res, j = None, i + 1
        while j < min(n, i + 1 + MAX_BARS):
            if d > 0:           # a long closes on the bid
                if l[j] - hs <= sl:
                    res = -1.0
                elif h[j] - hs >= tp:
                    res = rr
            else:               # a short closes on the ask
                if h[j] + hs >= sl:
                    res = -1.0
                elif l[j] + hs <= tp:
                    res = rr
            if res is not None:
                break
            j += 1
        if res is None:
            j = min(n - 1, i + MAX_BARS)
            res = (c[j] - hs - entry) / stop if d > 0 else (entry - (c[j] + hs)) / stop
        out.append(res)
        i = j + 1
    return np.array(out)


def stats(r: np.ndarray) -> dict:
    n = len(r)
    if n == 0:
        return {"n": 0, "win": 0.0, "exp": 0.0, "pf": 0.0, "net": 0.0, "t": 0.0}
    w, lo = r[r > 0].sum(), -r[r < 0].sum()
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    t = float(r.mean() / (sd / n ** 0.5)) if sd > 0 else 0.0     # how many standard errors the average result sits above zero: small means it could be luck
    return {"t": round(t, 2), "n": n, "win": round(float((r > 0).mean() * 100), 1), "exp": round(float(r.mean()), 3), "pf": round(float(w / lo) if lo > 0 else 9.99, 2), "net": round(float(r.sum()), 2)}


def test_symbol(symbol: str, tf: int, df: pd.DataFrame | None = None) -> dict:
    """Every strategy and setting on this symbol and candle size: pick the best on the first 60%, judge it on the last 40%."""
    df = fetch_history(symbol, tf) if df is None else df
    spread = float(paper_spec(symbol)["spread"])
    days = (float(df["time"].iloc[-1]) - float(df["time"].iloc[0])) / 86400
    if days < MIN_DAYS:
        raise RuntimeError(f"{symbol} {tf}m history covers only {days:.0f} days, {MIN_DAYS} needed")
    cut = int(len(df) * 0.6)
    results, base = [], None
    for name in S.STRATEGIES:
        for sess in SESSIONS:
            sg = S.signal(df, name, sess)
            for k in STOPS:
                for rr in RRS:
                    tr, te = stats(simulate(df, sg, k, rr, spread, 0, cut)), stats(simulate(df, sg, k, rr, spread, cut, len(df)))
                    results.append({"strategy": name, "session": sess, "stop_k": k, "rr": rr, "train": tr, "test": te})
    pool = [x for x in results if x["strategy"] != S.BASELINE and x["train"]["n"] >= MIN_TRAIN and x["train"]["exp"] > 0]
    best = max(pool, key=lambda x: x["train"]["exp"]) if pool else None
    bl = [x for x in results if x["strategy"] == S.BASELINE and x["stop_k"] == 1.5 and x["rr"] == 1.5 and x["session"] == "all"]
    base = bl[0]["test"] if bl else None
    tested = [x for x in results if x["test"]["n"] >= MIN_TEST]
    row = {"symbol": symbol, "tf": tf, "candles": len(df), "days": round(days), "baseline_test": base, "best": best,
           "share_positive_test": round(sum(x["test"]["exp"] > 0 for x in tested) / len(tested), 2) if tested else 0.0, "ok": False}
    if best:
        t = best["test"]
        row["ok"] = t["n"] >= MIN_TEST_STRONG and t["exp"] >= OK_TEST_EXPECTANCY and t["pf"] >= OK_TEST_PF and t.get("t", 0) >= MIN_T and row["share_positive_test"] >= MIN_ROBUST
    return row


# ---------------------------------------------------------------- the background run over every symbol and candle size
_run = {"lock": threading.Lock()}


def status() -> dict:
    return kv_get("backtest") or {"state": "never run", "rows": []}


def run_all() -> bool:
    """Start the full run in the background. False if one is already going."""
    if not _run["lock"].acquire(blocking=False):
        return False

    def work():
        try:
            cfg = get_settings()
            jobs = [(s, tf) for s in cfg.symbol_list for tf in TIMEFRAMES]
            state = {"state": "running", "started": utcnow().isoformat(), "total": len(jobs), "done": 0, "rows": [], "errors": []}
            kv_set("backtest", state)
            for s, tf in jobs:
                try:
                    state["rows"].append(test_symbol(s, tf))
                except Exception as e:  # noqa: BLE001
                    state["errors"].append(f"{s} {tf}m: {e}")
                state["done"] += 1
                kv_set("backtest", state)
                time.sleep(0.5)
            state["state"], state["finished"] = "done", utcnow().isoformat()
            state["chosen"] = choose(state["rows"])
            kv_set("backtest", state)
            kv_set("strategies", state["chosen"])
            log(f"Backtest finished: {len(state['chosen'])} of {len(cfg.symbol_list)} symbols have a setup that also earned on unseen data", "info")
        except Exception as e:  # noqa: BLE001
            kv_set("backtest", {"state": "failed", "error": str(e), "rows": []})
        finally:
            _run["lock"].release()

    threading.Thread(target=work, name="backtest", daemon=True).start()
    return True


def choose(rows: list[dict]) -> dict:
    """For each symbol, the validated setup with the best result on unseen data (one candle size per symbol)."""
    best: dict = {}
    for r in rows:
        if not r["ok"]:
            continue
        b, t = r["best"], r["best"]["test"]
        cur = best.get(r["symbol"])
        if cur is None or t["exp"] > cur["test"]["exp"]:
            best[r["symbol"]] = {"tf": r["tf"], "strategy": b["strategy"], "session": b["session"], "stop_k": b["stop_k"], "rr": b["rr"], "train": b["train"], "test": t}
    return best
