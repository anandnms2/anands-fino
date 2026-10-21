"""cTrader Open API broker (JSON over WebSocket). Same interface as RemoteBroker, so the engine does not care which is used.

Protocol facts used here come from Spotware's published docs and proto files:
  * endpoint  wss://{demo|live}.ctraderapi.com:5036  (JSON), one connection per environment
  * envelope  {"clientMsgId": str, "payloadType": int, "payload": {...}}
  * auth      application auth (client id + secret), then account auth (ctidTraderAccountId + access token)
  * prices    spot and trendbar prices are integers scaled by 1e5; order and position prices are plain doubles
  * volume    integers in 1/100 of a unit (1 lot of EURUSD = 100000 units = 10,000,000); lotSize comes with the symbol
  * money     integers scaled by 10**moneyDigits
Nothing here has been run against a real cTrader server yet, so test on a demo account first (see /api/diagnostics).
"""
import json
import threading
import time
import uuid
from datetime import datetime, timezone

import httpx
import pandas as pd

from .broker import ACCOUNT_TRADE_MODE_DEMO, RATE_COLS, RETCODE_DONE, BrokerError
from .config import get_settings
from .db import kv_get, kv_set

# payload types (ProtoOAPayloadType and the common ones)
APP_AUTH_REQ, APP_AUTH_RES = 2100, 2101
ACCOUNT_AUTH_REQ, ACCOUNT_AUTH_RES = 2102, 2103
NEW_ORDER_REQ = 2106
AMEND_SLTP_REQ = 2110
CLOSE_POSITION_REQ = 2111
ASSET_LIST_REQ, ASSET_LIST_RES = 2112, 2113
SYMBOLS_LIST_REQ, SYMBOLS_LIST_RES = 2114, 2115
SYMBOL_BY_ID_REQ, SYMBOL_BY_ID_RES = 2116, 2117
TRADER_REQ, TRADER_RES = 2121, 2122
RECONCILE_REQ, RECONCILE_RES = 2124, 2125
EXECUTION_EVENT = 2126
SUBSCRIBE_SPOTS_REQ, SUBSCRIBE_SPOTS_RES = 2127, 2128
SPOT_EVENT = 2131
ORDER_ERROR_EVENT = 2132
DEAL_LIST_REQ, DEAL_LIST_RES = 2133, 2134
GET_TRENDBARS_REQ, GET_TRENDBARS_RES = 2137, 2138
ERROR_RES = 2142
ACCOUNT_DISCONNECT_EVENT = 2164
ACCOUNTS_TOKEN_INVALIDATED_EVENT = 2147
CLIENT_DISCONNECT_EVENT = 2148
GET_ACCOUNTS_REQ, GET_ACCOUNTS_RES = 2149, 2150
UNREALIZED_PNL_REQ, UNREALIZED_PNL_RES = 2187, 2188
HEARTBEAT = 51
COMMON_ERROR_RES = 50

PERIOD_H1 = 9
PERIODS = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 10: 6, 15: 7, 30: 8, 60: 9}  # minutes -> ProtoOATrendbarPeriod
SIDE_BUY, SIDE_SELL = 1, 2
ORDER_MARKET = 1
EXEC_FILLED, EXEC_REJECTED, EXEC_CANCELLED, EXEC_EXPIRED = 3, 7, 5, 6
PRICE_SCALE = 100000.0
WEEK_MS = 7 * 24 * 3600 * 1000

OAUTH_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"


# ------------------------------------------------------------------ OAuth tokens (stored in the database)
def auth_url(redirect_uri: str) -> str:
    cfg = get_settings()
    from urllib.parse import urlencode

    q = urlencode({"client_id": cfg.ctrader_client_id, "redirect_uri": redirect_uri, "scope": "trading", "product": "web"})
    return f"{OAUTH_URL}?{q}"


def _store_tokens(data: dict) -> dict:
    if data.get("errorCode") or not data.get("accessToken"):
        raise BrokerError(f"cTrader token request failed: {data.get('errorCode')} {data.get('description')}")
    tok = {"access": data["accessToken"], "refresh": data.get("refreshToken", ""),
           "expires_at": time.time() + float(data.get("expiresIn") or 0)}
    kv_set("ctrader_tokens", tok)
    return tok


def exchange_code(code: str, redirect_uri: str) -> dict:
    cfg = get_settings()
    r = httpx.get(TOKEN_URL, timeout=30, params={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                                                 "client_id": cfg.ctrader_client_id, "client_secret": cfg.ctrader_client_secret})
    try:
        data = r.json()
    except ValueError:
        raise BrokerError(f"cTrader token endpoint returned HTTP {r.status_code}, not JSON")
    return _store_tokens(data)


def refresh_tokens() -> dict:
    cfg = get_settings()
    tok = kv_get("ctrader_tokens") or {}
    refresh = tok.get("refresh") or cfg.ctrader_refresh_token
    if not refresh:
        raise BrokerError("no cTrader refresh token; open /ctrader/connect to authorise again")
    r = httpx.get(TOKEN_URL, timeout=30, params={"grant_type": "refresh_token", "refresh_token": refresh,
                                                 "client_id": cfg.ctrader_client_id, "client_secret": cfg.ctrader_client_secret})
    try:
        data = r.json()
    except ValueError:
        raise BrokerError(f"cTrader token endpoint returned HTTP {r.status_code}, not JSON")
    return _store_tokens(data)


def current_access_token() -> str:
    tok = kv_get("ctrader_tokens") or {}
    return tok.get("access") or get_settings().ctrader_access_token


# ------------------------------------------------------------------ broker
class CTraderBroker:
    def __init__(self, timeout: float = 30.0, env: str | None = None):
        self.timeout = timeout
        self.env = env or get_settings().ctrader_env
        self._lock = threading.RLock()        # connect / reconnect
        self._plock = threading.Lock()        # pending requests and waiters
        self._ws = None
        self._reader = None
        self._beat = None
        self._alive = False
        self._ready = False
        self._pending: dict[str, dict] = {}   # clientMsgId -> {"event", "msg"}
        self._waiters: list[dict] = []        # execution-event listeners
        self._spots: dict[int, dict] = {}     # symbolId -> {"bid","ask","ts"}
        self._subscribed: set[int] = set()
        self._ctid: int | None = None
        self._account_info: dict = {}
        self._assets: dict[int, str] = {}
        self._light: dict[str, dict] = {}     # UPPER name -> light symbol
        self._by_id: dict[int, str] = {}
        self._sym_cache: dict[int, dict] = {}
        self._money_digits = 2

    # offset is always zero: cTrader timestamps are UTC. The engine assigns to it, so accept and ignore.
    @property
    def offset_s(self) -> int:
        return 0

    @offset_s.setter
    def offset_s(self, _v) -> None:
        pass

    # ---- plumbing ----
    def _url(self) -> str:
        cfg = get_settings()
        if cfg.ctrader_url:
            return cfg.ctrader_url
        return f"wss://{'live' if self.env == 'live' else 'demo'}.ctraderapi.com:5036"

    def _send(self, payload_type: int, payload: dict, msg_id: str | None = None) -> str:
        msg_id = msg_id or uuid.uuid4().hex[:16]
        try:
            self._ws.send(json.dumps({"clientMsgId": msg_id, "payloadType": payload_type, "payload": payload}))
        except Exception as e:  # noqa: BLE001
            self._drop()
            raise BrokerError(f"cTrader connection dropped while sending: {e}") from e
        return msg_id

    def _request(self, payload_type: int, payload: dict, timeout: float | None = None) -> dict:
        """Send a request and return the payload of the reply carrying the same clientMsgId."""
        msg_id = uuid.uuid4().hex[:16]
        slot = {"event": threading.Event(), "msg": None}
        with self._plock:
            self._pending[msg_id] = slot
        try:
            self._send(payload_type, payload, msg_id)
            if not slot["event"].wait(timeout or self.timeout):
                raise BrokerError(f"cTrader did not answer request {payload_type} in time")
            msg = slot["msg"]
        finally:
            with self._plock:
                self._pending.pop(msg_id, None)
        if msg is None:
            raise BrokerError("cTrader connection dropped")
        if msg.get("payloadType") in (ERROR_RES, COMMON_ERROR_RES, ORDER_ERROR_EVENT):
            p = msg.get("payload") or {}
            raise BrokerError(f"cTrader error {p.get('errorCode')}: {p.get('description') or ''}".strip())
        return msg.get("payload") or {}

    def _read_loop(self, ws) -> None:
        try:
            while self._alive and ws is self._ws:
                raw = ws.recv()
                if raw is None or raw == "":
                    raise ConnectionError("closed")
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self._dispatch(msg)
        except Exception:  # noqa: BLE001
            pass
        finally:
            if ws is self._ws:
                self._drop()

    def _dispatch(self, msg: dict) -> None:
        pt, payload, mid = msg.get("payloadType"), msg.get("payload") or {}, msg.get("clientMsgId")
        if pt == HEARTBEAT:
            return
        if pt == SPOT_EVENT:
            sid = payload.get("symbolId")
            if sid is not None:
                s = self._spots.setdefault(int(sid), {"bid": None, "ask": None, "ts": 0})
                if payload.get("bid"):
                    s["bid"] = payload["bid"] / PRICE_SCALE
                if payload.get("ask"):
                    s["ask"] = payload["ask"] / PRICE_SCALE
                s["ts"] = time.time()
            return
        if pt in (EXECUTION_EVENT, ORDER_ERROR_EVENT):
            with self._plock:
                waiters = list(self._waiters)
            for w in waiters:
                w["queue"].append(msg)
                w["event"].set()
        if pt in (ACCOUNTS_TOKEN_INVALIDATED_EVENT, CLIENT_DISCONNECT_EVENT, ACCOUNT_DISCONNECT_EVENT):
            self._drop()
            return
        with self._plock:
            slot = self._pending.get(mid) if mid else None
        if slot is not None:
            slot["msg"] = msg
            slot["event"].set()

    def _drop(self) -> None:
        self._alive = False
        self._ready = False
        ws, self._ws = self._ws, None
        try:
            if ws is not None:
                ws.close()
        except Exception:  # noqa: BLE001
            pass
        with self._plock:
            for slot in self._pending.values():
                slot["event"].set()
            for w in self._waiters:
                w["event"].set()

    def _heartbeat_loop(self, ws) -> None:
        while self._alive and ws is self._ws:
            time.sleep(10)
            try:
                if self._alive and ws is self._ws:
                    ws.send(json.dumps({"payloadType": HEARTBEAT, "payload": {}}))
            except Exception:  # noqa: BLE001
                return

    # ---- connection ----
    def connect(self) -> None:
        with self._lock:
            if self._ready and self._ws is not None:
                return
            self._connect()

    def _connect(self) -> None:
        import websocket  # websocket-client

        cfg = get_settings()
        if not cfg.ctrader_client_id or not cfg.ctrader_client_secret:
            raise BrokerError("CTRADER_CLIENT_ID and CTRADER_CLIENT_SECRET are not set")
        if not current_access_token():
            raise BrokerError("cTrader is not authorised yet: open /ctrader/connect once to grant access")
        self._drop()
        try:
            ws = websocket.create_connection(self._url(), timeout=15, enable_multithread=True)
            ws.settimeout(None)
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"cannot reach cTrader at {self._url()}: {e}") from e
        self._ws, self._alive = ws, True
        self._reader = threading.Thread(target=self._read_loop, args=(ws,), daemon=True)
        self._reader.start()
        self._beat = threading.Thread(target=self._heartbeat_loop, args=(ws,), daemon=True)
        self._beat.start()
        try:
            self._request(APP_AUTH_REQ, {"clientId": cfg.ctrader_client_id, "clientSecret": cfg.ctrader_client_secret})
            self._select_and_auth_account(cfg)
            self._load_static()
            for sid in list(self._subscribed):
                self._request(SUBSCRIBE_SPOTS_REQ, {"ctidTraderAccountId": self._ctid, "symbolId": [sid]})
        except BrokerError:
            self._drop()
            raise
        self._ready = True

    def _select_and_auth_account(self, cfg) -> None:
        token = current_access_token()
        for attempt in (1, 2):
            try:
                res = self._request(GET_ACCOUNTS_REQ, {"accessToken": token})
                break
            except BrokerError as e:
                if attempt == 1 and "TOKEN" in str(e).upper():
                    token = refresh_tokens()["access"]
                    continue
                raise
        accounts = res.get("ctidTraderAccount") or []
        if not accounts:
            raise BrokerError("this cTrader ID has no trading accounts. Open a cTrader account at the broker first")
        want_live = self.env == "live"
        wanted = (cfg.ctrader_live_account_id if want_live else cfg.ctrader_demo_account_id) or (cfg.ctrader_account_id if cfg.ctrader_env == self.env else "")
        wanted = wanted.strip()
        pick = None
        if wanted:
            pick = next((a for a in accounts if str(a.get("ctidTraderAccountId")) == wanted
                         or str(a.get("traderLogin")) == wanted), None)
            if pick is None:
                raise BrokerError(f"cTrader account {wanted} not found. Available: "
                                  + ", ".join(f"{a.get('traderLogin')} ({'live' if a.get('isLive') else 'demo'})" for a in accounts))
        else:
            pick = next((a for a in accounts if bool(a.get("isLive")) == want_live), None)
            if pick is None:
                raise BrokerError(f"no {'live' if want_live else 'demo'} cTrader account on this cTrader ID. Found: "
                                  + ", ".join(f"{a.get('traderLogin')} ({'live' if a.get('isLive') else 'demo'})" for a in accounts))
        if bool(pick.get("isLive")) != want_live:
            raise BrokerError(f"account {pick.get('traderLogin')} is {'live' if pick.get('isLive') else 'demo'} but this is the {self.env} venue")
        self._ctid = int(pick["ctidTraderAccountId"])
        self._account_info = pick
        self._request(ACCOUNT_AUTH_REQ, {"ctidTraderAccountId": self._ctid, "accessToken": token})

    def _load_static(self) -> None:
        a = self._request(ASSET_LIST_REQ, {"ctidTraderAccountId": self._ctid})
        self._assets = {int(x["assetId"]): x.get("name", "") for x in a.get("asset", [])}
        s = self._request(SYMBOLS_LIST_REQ, {"ctidTraderAccountId": self._ctid})
        self._light = {str(x.get("symbolName", "")).upper(): x for x in s.get("symbol", [])}
        self._by_id = {int(x["symbolId"]): str(x.get("symbolName", "")) for x in s.get("symbol", [])}
        t = self._request(TRADER_REQ, {"ctidTraderAccountId": self._ctid})
        self._money_digits = int((t.get("trader") or {}).get("moneyDigits") or 2)

    def ping(self) -> bool:
        return bool(self._ready and self._ws is not None)

    def _ensure(self) -> None:
        if not self._ready:
            self.connect()

    def _call(self, payload_type: int, payload: dict, timeout: float | None = None) -> dict:
        """Request with one automatic reconnect if the link had dropped."""
        for attempt in (1, 2):
            self._ensure()
            try:
                return self._request(payload_type, payload, timeout)
            except BrokerError as e:
                if attempt == 1 and not self._ready:
                    continue
                raise
        raise BrokerError("cTrader request failed")

    # ---- symbols ----
    def _light_symbol(self, symbol: str) -> dict:
        self._ensure()
        s = self._light.get(symbol.upper())
        if s is None:
            raise BrokerError(f"symbol {symbol} not found on this cTrader account (set SYMBOL_MAP if it is named differently)")
        return s

    def _symbol_detail(self, symbol: str) -> dict:
        light = self._light_symbol(symbol)
        sid = int(light["symbolId"])
        if sid not in self._sym_cache:
            res = self._call(SYMBOL_BY_ID_REQ, {"ctidTraderAccountId": self._ctid, "symbolId": [sid]})
            syms = res.get("symbol") or []
            if not syms:
                raise BrokerError(f"cTrader returned no details for {symbol}")
            self._sym_cache[sid] = syms[0]
        return {"id": sid, "light": light, "d": self._sym_cache[sid]}

    def symbol_info(self, symbol: str) -> dict:
        x = self._symbol_detail(symbol)
        d, light = x["d"], x["light"]
        lot = float(d.get("lotSize") or 10000000)
        digits = int(d.get("digits", 5))
        return {"digits": digits, "point": 10.0 ** -digits, "spread": 0,
                "trade_contract_size": lot / 100.0,
                "volume_min": float(d.get("minVolume") or 0) / lot, "volume_max": float(d.get("maxVolume") or 0) / lot,
                "volume_step": float(d.get("stepVolume") or 0) / lot,
                "currency_profit": self._assets.get(int(light.get("quoteAssetId", 0)), ""),
                "currency_base": self._assets.get(int(light.get("baseAssetId", 0)), ""),
                "trade_stops_level": 0, "filling_mode": 0,
                "trade_mode": 4 if d.get("tradingMode", "ENABLED") in ("ENABLED", 0) else 0, "visible": True}

    # ---- prices ----
    def tick(self, symbol: str) -> dict:
        sid = int(self._light_symbol(symbol)["symbolId"])
        if sid not in self._subscribed:
            self._call(SUBSCRIBE_SPOTS_REQ, {"ctidTraderAccountId": self._ctid, "symbolId": [sid]})
            self._subscribed.add(sid)
        deadline = time.time() + 10
        while time.time() < deadline:
            s = self._spots.get(sid)
            if s and s["bid"] and s["ask"]:
                return {"bid": s["bid"], "ask": s["ask"], "time": int(s["ts"])}
            time.sleep(0.1)
        raise BrokerError(f"no price received for {symbol}")

    def rates(self, symbol: str, count: int = 500, tf: int | None = None) -> pd.DataFrame:
        """Candles of the configured timeframe in UTC, oldest first. The last row is the candle still forming, as with MT5."""
        sid = int(self._light_symbol(symbol)["symbolId"])
        now_ms = int(time.time() * 1000)
        tf = tf or get_settings().timeframe_min
        if tf not in PERIODS:
            raise BrokerError(f"TIMEFRAME_MIN {tf} is not a cTrader candle size. Use one of {sorted(PERIODS)}")
        span_ms = int(count * 1.8 * tf * 60 * 1000) + 3 * 24 * 3600 * 1000  # allows for weekends
        bars: list[dict] = []
        to_ms = now_ms
        for _ in range(8):
            res = self._call(GET_TRENDBARS_REQ, {"ctidTraderAccountId": self._ctid, "symbolId": sid, "period": PERIODS[tf],
                                                 "fromTimestamp": now_ms - span_ms, "toTimestamp": to_ms})
            got = res.get("trendbar") or []
            bars.extend(got)
            if not res.get("hasMore") or not got:
                break
            to_ms = min(int(b["utcTimestampInMinutes"]) for b in got) * 60000 - 1
        if not bars:
            raise BrokerError(f"no candles for {symbol}")
        rows = {}
        for b in bars:
            low = int(b["low"])
            t = int(b["utcTimestampInMinutes"]) * 60
            rows[t] = [t, (low + int(b.get("deltaOpen", 0))) / PRICE_SCALE, (low + int(b.get("deltaHigh", 0))) / PRICE_SCALE,
                       low / PRICE_SCALE, (low + int(b.get("deltaClose", 0))) / PRICE_SCALE, int(b.get("volume", 0)), 0, 0]
        data = [rows[k] for k in sorted(rows)][-count:]
        df = pd.DataFrame(data, columns=RATE_COLS)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df

    def detect_offset_s(self, symbol: str, now: datetime | None = None) -> int | None:
        return 0

    # ---- account ----
    def account(self) -> dict:
        self._ensure()
        t = (self._call(TRADER_REQ, {"ctidTraderAccountId": self._ctid})).get("trader") or {}
        md = int(t.get("moneyDigits") or self._money_digits or 2)
        self._money_digits = md
        scale = 10.0 ** md
        balance = float(t.get("balance", 0)) / scale
        floating = 0.0
        try:
            pnl = self._call(UNREALIZED_PNL_REQ, {"ctidTraderAccountId": self._ctid})
            pscale = 10.0 ** int(pnl.get("moneyDigits") or md)
            floating = sum(float(p.get("netUnrealizedPnL", p.get("grossUnrealizedPnL", 0))) for p in pnl.get("positionUnrealizedPnL", [])) / pscale
        except BrokerError:
            floating = 0.0  # equity then equals balance; the daily loss cap is slightly late while positions are open
        live = bool(self._account_info.get("isLive"))
        return {"login": int(t.get("traderLogin") or self._account_info.get("traderLogin") or self._ctid),
                "server": str(t.get("brokerName") or self._account_info.get("brokerTitleShort") or "cTrader"),
                "trade_mode": 2 if live else ACCOUNT_TRADE_MODE_DEMO, "balance": balance, "equity": balance + floating,
                "currency": self._assets.get(int(t.get("depositAssetId", 0)), ""),
                "leverage": float(t.get("leverageInCents", 0)) / 100.0, "margin_free": balance + floating,
                "name": str(self._account_info.get("brokerTitleShort") or "")}

    # ---- positions and history ----
    def positions(self, magic: int) -> list[dict]:
        res = self._call(RECONCILE_REQ, {"ctidTraderAccountId": self._ctid})
        mine = [p for p in res.get("position", []) if str((p.get("tradeData") or {}).get("label", "")) == str(magic)
                and p.get("positionStatus") in (1, 3, "POSITION_STATUS_OPEN", "POSITION_STATUS_CREATED", None)]
        if not mine:
            return []
        pnl_map, md = {}, self._money_digits
        try:
            pnl = self._call(UNREALIZED_PNL_REQ, {"ctidTraderAccountId": self._ctid})
            ps = 10.0 ** int(pnl.get("moneyDigits") or md)
            pnl_map = {int(p["positionId"]): float(p.get("netUnrealizedPnL", p.get("grossUnrealizedPnL", 0))) / ps
                       for p in pnl.get("positionUnrealizedPnL", [])}
        except BrokerError:
            pass
        out = []
        for p in mine:
            td = p["tradeData"]
            sid = int(td["symbolId"])
            name = self._by_id.get(sid, str(sid))
            info = self.symbol_info(name)
            lot = info["trade_contract_size"] * 100.0
            side_buy = td.get("tradeSide") in (SIDE_BUY, "BUY")
            try:
                tk = self.tick(name)
                cur = tk["bid"] if side_buy else tk["ask"]
            except BrokerError:
                cur = float(p.get("price", 0))
            pm = 10.0 ** int(p.get("moneyDigits") or md)
            out.append({"ticket": int(p["positionId"]), "symbol": name, "type": 0 if side_buy else 1,
                        "volume": float(td["volume"]) / lot, "price_open": float(p.get("price", 0)),
                        "sl": float(p.get("stopLoss") or 0), "tp": float(p.get("takeProfit") or 0), "price_current": cur,
                        "profit": pnl_map.get(int(p["positionId"]), 0.0), "swap": float(p.get("swap", 0)) / pm,
                        "magic": magic, "time": int(td.get("openTimestamp", 0)) // 1000, "identifier": int(p["positionId"])})
        return out

    def deals_for_position(self, ticket: int) -> list[dict]:
        now_ms = int(time.time() * 1000)
        found: list[dict] = []
        for w in range(6):  # the API accepts at most a week per request
            to_ms = now_ms - w * WEEK_MS
            res = self._call(DEAL_LIST_REQ, {"ctidTraderAccountId": self._ctid, "fromTimestamp": to_ms - WEEK_MS + 1,
                                             "toTimestamp": to_ms, "maxRows": 1000})
            found.extend(d for d in res.get("deal", []) if int(d.get("positionId", 0)) == int(ticket))
            if any(d.get("closePositionDetail") for d in found):
                break
        out, seen = [], set()
        for d in sorted(found, key=lambda x: int(x.get("executionTimestamp", 0))):
            if d["dealId"] in seen:
                continue
            seen.add(d["dealId"])
            cpd = d.get("closePositionDetail")
            dm = 10.0 ** int(d.get("moneyDigits") or (cpd or {}).get("moneyDigits") or self._money_digits)
            sid = int(d.get("symbolId", 0))
            lot = self.symbol_info(self._by_id[sid])["trade_contract_size"] * 100.0 if sid in self._by_id else 1.0
            out.append({"ticket": int(d["dealId"]), "time": int(d.get("executionTimestamp", 0)) // 1000,
                        "type": 0 if d.get("tradeSide") in (SIDE_BUY, "BUY") else 1, "entry": 1 if cpd else 0,
                        "volume": float(d.get("filledVolume", 0)) / lot, "price": float(d.get("executionPrice", 0)),
                        "commission": float(d.get("commission", 0)) / dm, "swap": float((cpd or {}).get("swap", 0)) / dm,
                        "fee": 0.0, "profit": float((cpd or {}).get("grossProfit", 0)) / dm, "reason": 0})
        return out

    # ---- orders ----
    def _wait_exec(self, waiter: dict, done, timeout: float):
        """Block until done(msg) returns 'ok' or an error string for one of the queued execution events."""
        deadline = time.time() + timeout
        seen = 0
        while time.time() < deadline:
            waiter["event"].wait(0.5)
            waiter["event"].clear()
            while seen < len(waiter["queue"]):
                msg = waiter["queue"][seen]
                seen += 1
                r = done(msg)
                if r is not None:
                    return r, msg
            if not self._alive:
                return "cTrader connection dropped", None
        return "timeout", None

    def _register(self) -> dict:
        w = {"event": threading.Event(), "queue": []}
        with self._plock:
            self._waiters.append(w)
        return w

    def _unregister(self, w: dict) -> None:
        with self._plock:
            if w in self._waiters:
                self._waiters.remove(w)

    def send_market(self, symbol: str, side: str, lots: float, sl: float, tp: float, magic: int, deviation: int, comment: str = "anands-fino") -> dict:
        self._ensure()
        x = self._symbol_detail(symbol)
        d = x["d"]
        lot = float(d.get("lotSize") or 10000000)
        step = int(d.get("stepVolume") or 1) or 1
        vol = int(round(lots * lot))
        vol -= vol % step
        if vol < int(d.get("minVolume") or 1) or vol <= 0:
            raise BrokerError(f"volume {lots:g} lots is below the minimum for {symbol}")
        if d.get("maxVolume") and vol > int(d["maxVolume"]):
            raise BrokerError(f"volume {lots:g} lots is above the maximum for {symbol}")
        digits = int(d.get("digits", 5))
        buy = side == "BUY"
        cid = "fino-" + uuid.uuid4().hex[:12]
        msg_id = uuid.uuid4().hex[:16]
        waiter = self._register()
        try:
            self._send(NEW_ORDER_REQ, {"ctidTraderAccountId": self._ctid, "symbolId": x["id"], "orderType": ORDER_MARKET,
                                       "tradeSide": SIDE_BUY if buy else SIDE_SELL, "volume": vol, "label": str(magic),
                                       "comment": comment[:30], "clientOrderId": cid}, msg_id)

            def done(m):
                p = m.get("payload") or {}
                mine = m.get("clientMsgId") == msg_id or (p.get("order") or {}).get("clientOrderId") == cid
                if m.get("payloadType") == ORDER_ERROR_EVENT:
                    return f"order rejected: {p.get('errorCode')} {p.get('description') or ''}".strip() if mine else None
                if not mine:
                    return None
                et = p.get("executionType")
                if et in (EXEC_FILLED, "ORDER_FILLED"):
                    return "ok"
                if et in (EXEC_REJECTED, EXEC_CANCELLED, EXEC_EXPIRED, "ORDER_REJECTED", "ORDER_CANCELLED", "ORDER_EXPIRED"):
                    return f"order {str(et).lower()}: {p.get('errorCode') or ''}".strip()
                return None

            result, ev = self._wait_exec(waiter, done, 25)
        finally:
            self._unregister(waiter)
        if result != "ok":
            pos = self._find_position_by_cid(x["id"], magic, cid) if result == "timeout" else None
            if pos is None:
                raise BrokerError(f"order failed: {result}")
            position, fill, deal_id = pos, float(pos.get("price", 0)), 0
        else:
            p = ev["payload"]
            position = p.get("position") or {}
            deal = p.get("deal") or {}
            fill = float(deal.get("executionPrice") or position.get("price") or 0)
            deal_id = int(deal.get("dealId", 0))
        pid = int(position["positionId"])
        # market orders cannot carry an absolute stop, so set it on the open position at once; if that fails, close it
        try:
            self._set_protection(pid, round(sl, digits), round(tp, digits))
        except BrokerError as e:
            try:
                self.close_position(pid, magic, deviation)
                closed = "the position was closed again"
            except BrokerError as e2:
                closed = f"CLOSING IT ALSO FAILED ({e2}), check the account now"
            raise BrokerError(f"filled #{pid} but the stop/target could not be set ({e}); {closed}") from e
        return {"retcode": RETCODE_DONE, "order": pid, "deal": deal_id, "volume": lots, "price": fill, "comment": "cTrader"}

    def _find_position_by_cid(self, sid: int, magic: int, cid: str):
        try:
            res = self._call(RECONCILE_REQ, {"ctidTraderAccountId": self._ctid})
        except BrokerError:
            return None
        now = time.time() * 1000
        for p in res.get("position", []):
            td = p.get("tradeData") or {}
            if int(td.get("symbolId", 0)) == sid and str(td.get("label", "")) == str(magic) and now - int(td.get("openTimestamp", 0)) < 60000:
                return p
        return None

    def _set_protection(self, position_id: int, sl: float, tp: float) -> None:
        msg_id = uuid.uuid4().hex[:16]
        waiter = self._register()
        try:
            self._send(AMEND_SLTP_REQ, {"ctidTraderAccountId": self._ctid, "positionId": position_id, "stopLoss": sl, "takeProfit": tp}, msg_id)

            def done(m):
                p = m.get("payload") or {}
                if m.get("payloadType") == ORDER_ERROR_EVENT:
                    return f"{p.get('errorCode')} {p.get('description') or ''}".strip() if (m.get("clientMsgId") == msg_id or int(p.get("positionId") or 0) == position_id) else None
                pos = p.get("position") or {}
                if int(pos.get("positionId", 0)) == position_id and (pos.get("stopLoss") or pos.get("takeProfit")):
                    return "ok"
                return None

            result, _ev = self._wait_exec(waiter, done, 15)
        finally:
            self._unregister(waiter)
        if result != "ok":
            raise BrokerError(result if result != "timeout" else "no confirmation from cTrader")

    def modify_sltp(self, ticket: int, sl: float, tp: float) -> None:
        self._set_protection(int(ticket), sl, tp)

    def close_position(self, ticket: int, magic: int, deviation: int) -> dict:
        pos = next((p for p in self.positions(magic) if p["ticket"] == ticket), None)
        if pos is None:
            raise BrokerError(f"position {ticket} is not open")
        lot = self.symbol_info(pos["symbol"])["trade_contract_size"] * 100.0
        vol = int(round(pos["volume"] * lot))
        msg_id = uuid.uuid4().hex[:16]
        waiter = self._register()
        try:
            self._send(CLOSE_POSITION_REQ, {"ctidTraderAccountId": self._ctid, "positionId": int(ticket), "volume": vol}, msg_id)

            def done(m):
                p = m.get("payload") or {}
                if m.get("payloadType") == ORDER_ERROR_EVENT:
                    return f"{p.get('errorCode')} {p.get('description') or ''}".strip() if (m.get("clientMsgId") == msg_id or int(p.get("positionId") or 0) == ticket) else None
                position = p.get("position") or {}
                if int(position.get("positionId", 0)) != ticket:
                    return None
                et = p.get("executionType")
                if et in (EXEC_FILLED, "ORDER_FILLED") and (position.get("positionStatus") in (2, "POSITION_STATUS_CLOSED") or (p.get("deal") or {}).get("closePositionDetail")):
                    return "ok"
                if et in (EXEC_REJECTED, "ORDER_REJECTED"):
                    return f"rejected {p.get('errorCode') or ''}".strip()
                return None

            result, ev = self._wait_exec(waiter, done, 25)
        finally:
            self._unregister(waiter)
        if result != "ok":
            raise BrokerError(f"close rejected: {result}")
        deal = (ev["payload"].get("deal") or {})
        return {"retcode": RETCODE_DONE, "price": float(deal.get("executionPrice", 0)), "comment": "cTrader"}
