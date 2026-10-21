"""Paper trading: real market prices from a free feed, a virtual account kept in the database, simulated fills.

No order ever leaves the process. Same interface as the real brokers, so the scoring, risk gate, Claude decision and
dashboard behave exactly as they would with a real account. Fills are simulated, so results are optimistic compared with
a real broker (no slippage, no requotes, spreads are a fixed estimate).
"""
import json
import threading
import time
from datetime import datetime, timezone

import httpx
import pandas as pd

from .broker import ACCOUNT_TRADE_MODE_DEMO, RATE_COLS, RETCODE_DONE, BrokerError
from .config import get_settings
from .db import kv_get, kv_set

UA = {"User-Agent": "Mozilla/5.0 (compatible; AnandsFino/1.0)"}

# Yahoo symbols to try in order (spot first, futures as a fallback; the futures price differs slightly from spot)
YAHOO = {"XAUUSD": ["XAUUSD=X", "GC=F"], "XAGUSD": ["XAGUSD=X", "SI=F"]}
DEFAULT_SPREAD = {"XAUUSD": 0.25, "XAGUSD": 0.03, "USDJPY": 0.008, "EURJPY": 0.012, "GBPJPY": 0.020, "AUDJPY": 0.016,
                  "EURGBP": 0.00010, "USDCHF": 0.00010, "USDCAD": 0.00012, "EURUSD": 0.00007, "GBPUSD": 0.00009}  # price units; other pairs use 0.00010. Override with PAPER_SPREADS
CONTRACT = {"XAUUSD": 100.0, "XAGUSD": 5000.0}
DIGITS = {"XAUUSD": 2, "XAGUSD": 3}


def resample(df: pd.DataFrame, tf_min: int) -> pd.DataFrame:
    """Aggregate 1-minute candles (columns time[s], open, high, low, close, tick_volume) into tf_min candles."""
    if tf_min <= 1:
        return df
    step = tf_min * 60
    g = df.assign(b=(df["time"].astype("int64") // step) * step).sort_values("time").groupby("b")
    out = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), tick_volume=("tick_volume", "sum"))
    return out.reset_index().rename(columns={"b": "time"})


# Yahoo: native intervals where they exist, 1-minute data resampled for M3
YAHOO_IV = {1: ("1m", "5d"), 3: ("1m", "5d"), 5: ("5m", "1mo"), 15: ("15m", "1mo"), 30: ("30m", "1mo"), 60: ("60m", "2mo")}
TD_IV = {1: "1min", 3: "1min", 5: "5min", 15: "15min", 30: "30min", 60: "1h"}


def _yahoo(symbol: str, tf_min: int) -> pd.DataFrame:
    last_err = "no data"
    for ysym in YAHOO.get(symbol, [f"{symbol}=X"]):
        try:
            r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}", params=dict(zip(("interval", "range"), YAHOO_IV.get(tf_min, ("60m", "2mo")))),
                          headers=UA, timeout=30, follow_redirects=True)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            df = pd.DataFrame({"time": res["timestamp"], "open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"],
                               "tick_volume": q.get("volume") or [0] * len(res["timestamp"])})
            df = df.dropna(subset=["open", "high", "low", "close"])
            if len(df) >= 50:
                return resample(df, tf_min) if tf_min == 3 else df
            last_err = f"{ysym} returned only {len(df)} candles"
        except Exception as e:  # noqa: BLE001
            last_err = f"{ysym}: {type(e).__name__}: {e}"
    raise BrokerError(f"Yahoo Finance gave no usable candles for {symbol} ({last_err})")


def _twelvedata(symbol: str, tf_min: int) -> pd.DataFrame:
    key = get_settings().twelvedata_api_key
    if not key:
        raise BrokerError("TWELVEDATA_API_KEY is not set")
    pair = f"{symbol[:3]}/{symbol[3:6]}"
    r = httpx.get("https://api.twelvedata.com/time_series", timeout=30,
                  params={"symbol": pair, "interval": TD_IV.get(tf_min, "1h"), "outputsize": 5000 if tf_min in (1, 3) else 500, "timezone": "UTC", "order": "ASC", "apikey": key})
    data = r.json()
    if data.get("status") == "error" or "values" not in data:
        raise BrokerError(f"Twelve Data: {data.get('message', 'no values')} for {pair}")
    rows = data["values"]
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["datetime"], utc=True).astype("int64") // 10**9
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df["tick_volume"] = 0
    df = df[["time", "open", "high", "low", "close", "tick_volume"]]
    return resample(df, tf_min) if tf_min == 3 else df


def fetch_candles(symbol: str, tf_min: int = 60) -> pd.DataFrame:
    """Candles of tf_min minutes, oldest first, last row is the candle still forming. Overridden in tests."""
    feed = get_settings().paper_feed
    return _twelvedata(symbol, tf_min) if feed == "twelvedata" else _yahoo(symbol, tf_min)


# ------------------------------------------------------------------ the broker
class PaperBroker:
    def __init__(self):
        self._lock = threading.RLock()
        self._cache: dict[tuple, tuple[float, pd.DataFrame]] = {}
        self.offset_s = 0

    # ---- feed with cache ----
    def _ttl(self) -> float:
        cfg = get_settings()
        if cfg.paper_feed == "twelvedata":  # free plan: 800 calls a day shared by all symbols
            return max(cfg.paper_price_ttl_s, len(cfg.symbol_list) * 86400 / 700)
        return cfg.paper_price_ttl_s

    def _tf(self) -> int:
        from .venues import venue_tf

        return venue_tf("paper")

    def _candles(self, symbol: str, tf: int | None = None) -> pd.DataFrame:
        tf = tf or self._tf()
        with self._lock:
            hit = self._cache.get((symbol, tf))
            if hit and time.time() - hit[0] < self._ttl():
                return hit[1]
            try:
                raw = fetch_candles(symbol, tf)
            except BrokerError:
                if hit and time.time() - hit[0] < 6 * 3600:
                    return hit[1]  # keep using the last good copy for a while
                raise
            df = raw.copy()
            step = tf * 60
            df["time"] = (df["time"].astype("int64") // step) * step
            df = df.drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)
            self._cache[(symbol, tf)] = (time.time(), df)
            return df

    def _spread(self, symbol: str) -> float:
        try:
            custom = json.loads(get_settings().paper_spreads or "{}")
        except ValueError:
            custom = {}
        return float(custom.get(symbol, DEFAULT_SPREAD.get(symbol, 0.00010)))

    # ---- state ----
    def _state(self) -> dict:
        st = kv_get("paper_state")
        if not st:
            st = {"balance": float(get_settings().paper_balance), "next": 800000, "positions": {}, "deals": {}}
            kv_set("paper_state", st)
        return st

    def reset(self) -> None:
        with self._lock:
            kv_set("paper_state", {"balance": float(get_settings().paper_balance), "next": 800000, "positions": {}, "deals": {}})

    # ---- interface ----
    def connect(self):
        return None

    def ping(self) -> bool:
        return True

    def detect_offset_s(self, symbol: str, now=None):
        return 0

    def symbol_info(self, symbol: str) -> dict:
        s = symbol.upper()
        if len(s) < 6 or not s.isalpha():
            raise BrokerError(f"unknown symbol {symbol}")
        digits = DIGITS.get(s, 3 if s.endswith("JPY") else 5)
        return {"digits": digits, "point": 10.0 ** -digits, "spread": 0, "trade_contract_size": CONTRACT.get(s, 100000.0),
                "volume_min": 0.01, "volume_max": 50.0, "volume_step": 0.01, "currency_profit": s[3:6], "currency_base": s[:3],
                "trade_stops_level": 0, "filling_mode": 3, "trade_mode": 4, "visible": True}

    def rates(self, symbol: str, count: int = 500, tf: int | None = None) -> pd.DataFrame:
        df = self._candles(symbol.upper(), tf).tail(count).copy()
        out = pd.DataFrame({"time": pd.to_datetime(df["time"], unit="s", utc=True), "open": df["open"], "high": df["high"], "low": df["low"],
                            "close": df["close"], "tick_volume": df["tick_volume"], "spread": 0, "real_volume": 0})
        return out[RATE_COLS].reset_index(drop=True)

    def tick(self, symbol: str) -> dict:
        s = symbol.upper()
        df = self._candles(s)
        mid, sp = float(df["close"].iloc[-1]), self._spread(s)
        return {"bid": mid - sp / 2, "ask": mid + sp / 2, "time": int(df["time"].iloc[-1])}

    # ---- simulated fills ----
    def _pnl(self, p: dict, exit_price: float) -> float:
        d = 1 if p["type"] == 0 else -1
        raw = d * (exit_price - p["price_open"]) * p["volume"] * CONTRACT.get(p["symbol"], 100000.0)
        return raw * self._to_usd(p["symbol"][3:6])

    def _to_usd(self, ccy: str) -> float:
        """Value of one unit of the profit currency in USD, from the feed's own prices."""
        if ccy == "USD":
            return 1.0
        pairs = [(f"USD{ccy}", True), (f"{ccy}USD", False)] if ccy in ("JPY", "CHF", "CAD") else [(f"{ccy}USD", False), (f"USD{ccy}", True)]
        for pair, inv in pairs:
            try:
                t = self.tick(pair)
            except BrokerError:
                continue
            mid = (t["bid"] + t["ask"]) / 2
            return 1.0 / mid if inv else mid
        raise BrokerError(f"no price to convert {ccy} to USD")

    def _book_close(self, st: dict, p: dict, price: float, why: str) -> None:
        profit = self._pnl(p, price)
        st["balance"] += profit
        st["positions"].pop(str(p["ticket"]), None)
        now = int(time.time())
        st["next"] += 1
        st["deals"][str(p["ticket"])] = [
            {"ticket": p["ticket"], "time": p["time"], "type": p["type"], "entry": 0, "volume": p["volume"], "price": p["price_open"],
             "commission": 0.0, "swap": 0.0, "fee": 0.0, "profit": 0.0, "reason": 0},
            {"ticket": st["next"], "time": now, "type": 1 - p["type"], "entry": 1, "volume": p["volume"], "price": float(price),
             "commission": 0.0, "swap": 0.0, "fee": 0.0, "profit": float(profit), "reason": 3 if why == "tp" else 4 if why == "sl" else 0}]

    def _sweep(self, st: dict) -> bool:
        """Close positions whose stop or target was touched. A candle touching both counts as the stop (pessimistic)."""
        changed = False
        for p in list(st["positions"].values()):
            try:
                df = self._candles(p["symbol"])
            except BrokerError:
                continue
            sp = self._spread(p["symbol"])
            step = self._tf() * 60
            open_hour = (p["time"] // step) * step
            buy = p["type"] == 0
            hit = None
            for row in df[df["time"] > open_hour].itertuples():
                if buy:
                    sl_hit, tp_hit = row.low <= p["sl"], row.high >= p["tp"]
                else:
                    sl_hit, tp_hit = row.high + sp >= p["sl"], row.low + sp <= p["tp"]
                if sl_hit:
                    hit = ("sl", p["sl"]); break
                if tp_hit:
                    hit = ("tp", p["tp"]); break
            if hit is None:  # the entry candle: only its current close is known
                last = float(df["close"].iloc[-1])
                lo, hi = (last, last) if buy else (last + sp, last + sp)
                if (buy and lo <= p["sl"]) or (not buy and hi >= p["sl"]):
                    hit = ("sl", p["sl"])
                elif (buy and hi >= p["tp"]) or (not buy and lo <= p["tp"]):
                    hit = ("tp", p["tp"])
            if hit:
                try:
                    self._book_close(st, p, hit[1], hit[0])
                except BrokerError:
                    continue
                changed = True
        return changed

    def account(self) -> dict:
        with self._lock:
            st = self._state()
            if st["positions"] and self._sweep(st):
                kv_set("paper_state", st)
            floating = 0.0
            for p in st["positions"].values():
                try:
                    t = self.tick(p["symbol"])
                    floating += self._pnl(p, t["bid"] if p["type"] == 0 else t["ask"])
                except BrokerError:
                    pass
            return {"login": 1, "server": "Paper (simulated)", "trade_mode": ACCOUNT_TRADE_MODE_DEMO, "balance": st["balance"],
                    "equity": st["balance"] + floating, "currency": "USD", "leverage": 100, "margin_free": st["balance"] + floating, "name": "paper"}

    def positions(self, magic: int) -> list[dict]:
        with self._lock:
            st = self._state()
            if st["positions"] and self._sweep(st):
                kv_set("paper_state", st)
            out = []
            for p in st["positions"].values():
                try:
                    t = self.tick(p["symbol"])
                    cur = t["bid"] if p["type"] == 0 else t["ask"]
                except BrokerError:
                    cur = p["price_open"]
                out.append({**p, "price_current": cur, "profit": self._pnl(p, cur), "magic": magic})
            return out

    def deals_for_position(self, ticket: int) -> list[dict]:
        return list(self._state()["deals"].get(str(ticket), []))

    def send_market(self, symbol, side, lots, sl, tp, magic, deviation, comment="anands-fino") -> dict:
        with self._lock:
            st = self._state()
            info = self.symbol_info(symbol)
            if lots < info["volume_min"] or lots > info["volume_max"]:
                raise BrokerError(f"volume {lots:g} outside {info['volume_min']:g}..{info['volume_max']:g}")
            t = self.tick(symbol)
            price = t["ask"] if side == "BUY" else t["bid"]
            st["next"] += 1
            tk = st["next"]
            st["positions"][str(tk)] = {"ticket": tk, "symbol": symbol.upper(), "type": 0 if side == "BUY" else 1, "volume": float(lots),
                                        "price_open": float(price), "sl": float(sl), "tp": float(tp), "swap": 0.0,
                                        "time": int(time.time()), "identifier": tk}
            kv_set("paper_state", st)
            return {"retcode": RETCODE_DONE, "order": tk, "deal": tk, "volume": lots, "price": float(price), "comment": "paper"}

    def close_position(self, ticket: int, magic: int, deviation: int) -> dict:
        with self._lock:
            st = self._state()
            p = st["positions"].get(str(ticket))
            if p is None:
                raise BrokerError(f"position {ticket} is not open")
            t = self.tick(p["symbol"])
            price = t["bid"] if p["type"] == 0 else t["ask"]
            self._book_close(st, p, price, "manual")
            kv_set("paper_state", st)
            return {"retcode": RETCODE_DONE, "price": float(price), "comment": "paper"}


_paper = None


def get_paper() -> PaperBroker:
    global _paper
    if _paper is None:
        _paper = PaperBroker()
    return _paper
