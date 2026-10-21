"""Broker access. RemoteBroker talks to MT5 running under Wine over RPyC; MockBroker is for tests and dry runs.

The client speaks plain RPyC "classic" mode, the protocol the Wine image's bridge on port 8001 serves, so it
does not depend on the mt5linux package (newer releases of that package try to launch Docker containers).
"""
import math
import threading
import time
import zlib
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .config import get_settings

# MetaTrader5 constants (stable values from the MT5 API)
TIMEFRAME_H1 = 16385
TRADE_ACTION_DEAL = 1
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2
SYMBOL_FILLING_FOK = 1   # bit flags in symbol_info.filling_mode
SYMBOL_FILLING_IOC = 2
RETCODE_DONE = 10009
RETCODE_PLACED = 10008
POSITION_TYPE_BUY = 0
ACCOUNT_TRADE_MODE_DEMO = 0

RATE_COLS = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]


class BrokerError(Exception):
    pass


class RemoteBroker:
    def __init__(self, host: str, port: int, timeout: int = 60):
        self.host, self.port, self.timeout = host, port, timeout
        self.conn = None
        self.offset_s = 0  # broker server time minus UTC, in seconds
        self._lock = threading.RLock()  # one RPyC link, shared by the scheduler and web threads

    # ---- connection ----
    def connect(self):
        import rpyc

        with self._lock:
            if self.conn is not None and not self.conn.closed:
                return
            self._connect(rpyc)

    def _connect(self, rpyc):
        try:
            self.conn = rpyc.classic.connect(self.host, self.port)
            self.conn._config["sync_request_timeout"] = self.timeout
            self.conn.execute("import MetaTrader5 as mt5")
            ok = self.conn.eval("mt5.initialize()")
        except Exception as e:  # noqa: BLE001
            self.conn = None
            raise BrokerError(f"cannot reach the MT5 bridge at {self.host}:{self.port}: {e}") from e
        if not ok:
            raise BrokerError(f"MT5 terminal not ready: {self._eval('mt5.last_error()')}. Log in to the account in the MT5 web desktop first.")

    def _eval(self, expr: str):
        """Evaluate on the MT5 side and copy the plain result back. Reconnects once if the link dropped."""
        from rpyc.utils.classic import obtain

        with self._lock:
            for attempt in (1, 2):
                try:
                    if self.conn is None or self.conn.closed:
                        self.connect()
                    return obtain(self.conn.eval(expr))
                except BrokerError:
                    raise
                except (EOFError, ConnectionError, OSError, TimeoutError):
                    self.conn = None
                    if attempt == 2:
                        raise BrokerError("MT5 bridge connection dropped")
                except Exception as e:  # noqa: BLE001
                    raise BrokerError(f"MT5 call failed: {e}") from e

    def ping(self) -> bool:
        try:
            return bool(self._eval("mt5.terminal_info() is not None"))
        except BrokerError:
            return False

    # ---- data ----
    def account(self) -> dict:
        keys = ("login", "server", "trade_mode", "balance", "equity", "currency", "leverage", "margin_free", "name")
        r = self._eval("(lambda a: None if a is None else {k: getattr(a, k) for k in %r})(mt5.account_info())" % (keys,))
        if r is None:
            raise BrokerError("no account is logged in on the MT5 terminal")
        return r

    def symbol_info(self, symbol: str) -> dict:
        keys = ("digits", "point", "spread", "trade_contract_size", "volume_min", "volume_max", "volume_step",
                "currency_profit", "currency_base", "trade_stops_level", "filling_mode", "trade_mode", "visible")
        self._eval("mt5.symbol_select(%r, True)" % symbol)
        r = self._eval("(lambda s: None if s is None else {k: getattr(s, k) for k in %r})(mt5.symbol_info(%r))" % (keys, symbol))
        if r is None:
            raise BrokerError(f"symbol {symbol} not found on this broker (set SYMBOL_MAP if it has a suffix)")
        return r

    def tick(self, symbol: str) -> dict:
        r = self._eval("(lambda t: None if t is None else {'bid': t.bid, 'ask': t.ask, 'time': t.time})(mt5.symbol_info_tick(%r))" % symbol)
        if r is None:
            raise BrokerError(f"no tick for {symbol}")
        return r

    def rates(self, symbol: str, count: int = 500) -> pd.DataFrame:
        """H1 candles in UTC, oldest first. The last row is the candle still forming."""
        expr = "(lambda r: None if r is None else r.tolist())(mt5.copy_rates_from_pos(%r, %d, 0, %d))" % (symbol, TIMEFRAME_H1, count)
        rows = self._eval(expr)
        if not rows:
            raise BrokerError(f"no candles for {symbol}: {self._eval('mt5.last_error()')}")
        df = pd.DataFrame(rows, columns=RATE_COLS)
        df["time"] = pd.to_datetime(df["time"] - self.offset_s, unit="s", utc=True)
        return df

    def detect_offset_s(self, symbol: str, now: datetime | None = None) -> int | None:
        """Broker server time minus UTC from the newest candle open. Only valid while the market is open."""
        rows = self._eval("(lambda r: None if r is None else r.tolist())(mt5.copy_rates_from_pos(%r, %d, 0, 1))" % (symbol, TIMEFRAME_H1))
        if not rows:
            return None
        now = now or datetime.now(timezone.utc)
        d_hours = (rows[0][0] - now.timestamp()) / 3600.0
        off_h = math.ceil(d_hours - 1e-9)
        return off_h * 3600 if -12 <= off_h <= 14 else None

    def positions(self, magic: int) -> list[dict]:
        keys = ("ticket", "symbol", "type", "volume", "price_open", "sl", "tp", "price_current", "profit", "swap", "magic", "time", "identifier")
        rows = self._eval("[{k: getattr(p, k) for k in %r} for p in (mt5.positions_get() or ())]" % (keys,))
        return [p for p in rows if p["magic"] == magic]

    def deals_for_position(self, ticket: int) -> list[dict]:
        keys = ("ticket", "time", "type", "entry", "volume", "price", "commission", "swap", "fee", "profit", "reason")
        return self._eval("[{k: getattr(d, k) for k in %r} for d in (mt5.history_deals_get(position=%d) or ())]" % (keys, ticket))

    # ---- orders ----
    def _filling(self, info: dict) -> int:
        mode = info.get("filling_mode", 0)
        if mode & SYMBOL_FILLING_IOC:
            return ORDER_FILLING_IOC
        if mode & SYMBOL_FILLING_FOK:
            return ORDER_FILLING_FOK
        return ORDER_FILLING_RETURN

    def send_market(self, symbol: str, side: str, lots: float, sl: float, tp: float, magic: int, deviation: int, comment: str = "anands-fino") -> dict:
        info = self.symbol_info(symbol)
        tick = self.tick(symbol)
        buy = side == "BUY"
        req = {
            "action": TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(lots),
            "type": ORDER_TYPE_BUY if buy else ORDER_TYPE_SELL,
            "price": float(tick["ask"] if buy else tick["bid"]),
            "sl": float(sl), "tp": float(tp), "deviation": int(deviation), "magic": int(magic),
            "comment": comment[:30], "type_time": ORDER_TIME_GTC, "type_filling": self._filling(info),
        }
        chk = self._eval("(lambda r: None if r is None else {'retcode': r.retcode, 'comment': r.comment})(mt5.order_check(%r))" % (req,))
        if chk is None or chk["retcode"] != 0:
            raise BrokerError(f"order check failed: {chk}")
        res = self._eval("(lambda r: None if r is None else {'retcode': r.retcode, 'order': r.order, 'deal': r.deal, 'volume': r.volume, 'price': r.price, 'comment': r.comment})(mt5.order_send(%r))" % (req,))
        if res is None or res["retcode"] not in (RETCODE_DONE, RETCODE_PLACED):
            raise BrokerError(f"order rejected: {res}")
        return res

    def close_position(self, ticket: int, magic: int, deviation: int) -> dict:
        pos = next((p for p in self.positions(magic) if p["ticket"] == ticket), None)
        if pos is None:
            raise BrokerError(f"position {ticket} is not open")
        info = self.symbol_info(pos["symbol"])
        tick = self.tick(pos["symbol"])
        was_buy = pos["type"] == POSITION_TYPE_BUY
        req = {
            "action": TRADE_ACTION_DEAL, "symbol": pos["symbol"], "volume": float(pos["volume"]),
            "type": ORDER_TYPE_SELL if was_buy else ORDER_TYPE_BUY,
            "position": int(ticket), "price": float(tick["bid"] if was_buy else tick["ask"]),
            "deviation": int(deviation), "magic": int(magic), "comment": "anands-fino close",
            "type_time": ORDER_TIME_GTC, "type_filling": self._filling(info),
        }
        res = self._eval("(lambda r: None if r is None else {'retcode': r.retcode, 'price': r.price, 'comment': r.comment})(mt5.order_send(%r))" % (req,))
        if res is None or res["retcode"] not in (RETCODE_DONE, RETCODE_PLACED):
            raise BrokerError(f"close rejected: {res}")
        return res


class MockBroker:
    """Synthetic market for tests and dry runs. It never leaves the process, so no real order can come out of it."""

    def __init__(self):
        self.offset_s = 0
        self._ticket = 90000
        self._positions: dict[int, dict] = {}
        self._closed: dict[int, list[dict]] = {}
        self.balance = 20000.0
        self.orders: list[dict] = []

    def connect(self):
        return None

    def ping(self) -> bool:
        return True

    def account(self) -> dict:
        floating = sum(p["profit"] for p in self._positions.values())
        return {"login": 1, "server": "Mock-Demo", "trade_mode": ACCOUNT_TRADE_MODE_DEMO, "balance": self.balance,
                "equity": self.balance + floating, "currency": "USD", "leverage": 100, "margin_free": self.balance, "name": "mock"}

    def symbol_info(self, symbol: str) -> dict:
        gold = symbol.startswith("XAU")
        silver = symbol.startswith("XAG")
        return {"digits": 2 if gold else 3 if silver else 5, "point": 0.01 if gold else 0.001 if silver else 0.00001,
                "spread": 18 if gold else 25, "trade_contract_size": 100.0 if gold else 5000.0 if silver else 100000.0,
                "volume_min": 0.01, "volume_max": 50.0, "volume_step": 0.01, "currency_profit": "USD",
                "currency_base": symbol[:3], "trade_stops_level": 0, "filling_mode": 3, "trade_mode": 4, "visible": True}

    def _series(self, symbol: str, n: int) -> np.ndarray:
        base = {"XAUUSD": 4100.0, "XAGUSD": 50.0, "EURUSD": 1.165, "GBPUSD": 1.34}.get(symbol, 1.0)
        rng = np.random.default_rng(zlib.crc32(symbol.encode()))
        steps = rng.normal(0.0004, 0.0035, n)  # gentle upward drift
        return base * np.exp(np.cumsum(steps) - steps.sum())

    def rates(self, symbol: str, count: int = 500) -> pd.DataFrame:
        close = self._series(symbol, count)
        rng = np.random.default_rng(7)
        open_ = np.concatenate([[close[0]], close[:-1]])
        wick = np.abs(rng.normal(0, close * 0.0012))
        high = np.maximum(open_, close) + wick
        low = np.minimum(open_, close) - wick
        now = pd.Timestamp.now(tz="UTC").floor("h")
        times = [now - pd.Timedelta(hours=count - 1 - i) for i in range(count)]
        return pd.DataFrame({"time": times, "open": open_, "high": high, "low": low, "close": close,
                             "tick_volume": 100, "spread": 20, "real_volume": 0})

    def detect_offset_s(self, symbol: str, now=None):
        return 0

    def tick(self, symbol: str) -> dict:
        last = float(self._series(symbol, 500)[-1])
        sp = self.symbol_info(symbol)["spread"] * self.symbol_info(symbol)["point"]
        return {"bid": last, "ask": last + sp, "time": int(time.time())}

    def positions(self, magic: int) -> list[dict]:
        return list(self._positions.values())

    def deals_for_position(self, ticket: int) -> list[dict]:
        return self._closed.get(ticket, [])

    def send_market(self, symbol, side, lots, sl, tp, magic, deviation, comment="anands-fino") -> dict:
        self._ticket += 1
        t = self.tick(symbol)
        price = t["ask"] if side == "BUY" else t["bid"]
        self._positions[self._ticket] = {
            "ticket": self._ticket, "symbol": symbol, "type": 0 if side == "BUY" else 1, "volume": lots,
            "price_open": price, "sl": sl, "tp": tp, "price_current": price, "profit": 0.0, "swap": 0.0,
            "magic": magic, "time": int(time.time()), "identifier": self._ticket}
        self.orders.append({"symbol": symbol, "side": side, "lots": lots, "sl": sl, "tp": tp})
        return {"retcode": RETCODE_DONE, "order": self._ticket, "deal": self._ticket, "volume": lots, "price": price, "comment": "mock"}

    def close_position(self, ticket: int, magic: int, deviation: int) -> dict:
        p = self._positions.pop(ticket, None)
        if p is None:
            raise BrokerError(f"position {ticket} is not open")
        profit = -12.5
        self.balance += profit
        self._closed[ticket] = [{"ticket": ticket, "time": int(time.time()), "type": 1, "entry": 1, "volume": p["volume"],
                                 "price": p["price_open"], "commission": 0.0, "swap": 0.0, "fee": 0.0, "profit": profit, "reason": 0}]
        return {"retcode": RETCODE_DONE, "price": p["price_open"], "comment": "mock"}


_broker = None


def get_broker():
    global _broker
    if _broker is None:
        s = get_settings()
        _broker = MockBroker() if s.mt5_mock else RemoteBroker(s.mt5_host, s.mt5_port)
    return _broker


def set_broker(b) -> None:
    global _broker
    _broker = b
