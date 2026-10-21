"""Anands Fino: web app, JSON API and the scheduler."""
import base64
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import collectors, engine, sentiment
from .broker import BrokerError, get_broker
from .config import get_settings
from .db import Activity, Position, Signal, init_db, iso, log, session

STATIC = Path(__file__).parent / "static"
scheduler = BackgroundScheduler(timezone="UTC")
_last_warn: dict[str, float] = {}


def job(name: str, fn):
    """Run a scheduled task, journal failures (at most once an hour per task), never crash the scheduler."""
    def run():
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            if time.time() - _last_warn.get(name, 0) > 3600:
                _last_warn[name] = time.time()
                log(f"{name} failed: {type(e).__name__}: {e}", "warn")
    return run


def refresh_news():
    collectors.fetch_news()
    sentiment.tag_untagged()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    init_db()
    if cfg.scheduler_enabled:
        scheduler.add_job(job("cycle", engine.run_cycle), "cron", minute=0, second=cfg.cycle_delay_seconds, max_instances=1, coalesce=True)
        scheduler.add_job(job("reconcile", engine.reconcile), "interval", seconds=60, max_instances=1, coalesce=True)
        scheduler.add_job(job("news", refresh_news), "interval", minutes=15, max_instances=1, coalesce=True)
        scheduler.add_job(job("calendar", collectors.fetch_calendar), "interval", minutes=60, max_instances=1, coalesce=True)
        scheduler.add_job(job("macro", collectors.fetch_macro), "interval", hours=6, max_instances=1, coalesce=True)
        scheduler.add_job(job("cot", collectors.fetch_cot), "interval", hours=12, max_instances=1, coalesce=True)
        scheduler.start()
        for name, fn in (("calendar", collectors.fetch_calendar), ("macro", collectors.fetch_macro),
                         ("cot", collectors.fetch_cot), ("news", refresh_news)):
            scheduler.add_job(job(name, fn), "date", run_date=None)
        if cfg.run_cycle_on_start:
            scheduler.add_job(job("cycle", lambda: engine.run_cycle(force=True)), "date", run_date=None)
        log(f"Anands Fino started. Level {engine.get_mode()}, symbols {', '.join(cfg.symbol_list)}"
            + (" (MOCK broker)" if cfg.mt5_mock else ""), "info")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Anands Fino", lifespan=lifespan)


# ---------------------------------------------------------------- access control
@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    cfg = get_settings()
    if not cfg.admin_password:
        return JSONResponse({"error": "ADMIN_PASSWORD is not set on the server"}, status_code=503)
    hdr = request.headers.get("authorization", "")
    ok = False
    if hdr.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
            ok = secrets.compare_digest(user, cfg.admin_user) and secrets.compare_digest(pw, cfg.admin_password)
        except Exception:  # noqa: BLE001
            ok = False
    if not ok:
        return JSONResponse({"error": "login required"}, status_code=401, headers={"WWW-Authenticate": 'Basic realm="Anands Fino"'})
    # state-changing calls must carry a header a foreign site cannot add (blocks cross-site form posts)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-requested-with") != "fino":
        return JSONResponse({"error": "missing X-Requested-With header"}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------- serializers
def sig_row(s: Signal, full: bool = False) -> dict:
    d = {
        "id": s.id, "ts": iso(s.ts), "symbol": s.symbol, "candle_time": s.candle_time, "bias": s.bias, "score": s.score,
        "agree": s.agree, "available": s.available, "status": s.status, "reason": s.reason, "lots": s.lots, "ticket": s.ticket,
        "levels": s.levels, "mode": s.mode, "expires_at": iso(s.expires_at),
        "confidence": (s.claude or {}).get("confidence"), "decision": (s.claude or {}).get("decision"),
    }
    if full:
        d.update({"factors": s.factors, "claude": s.claude, "gate": s.gate, "context": s.context})
    return d


def pos_row(p: Position) -> dict:
    return {"ticket": p.ticket, "symbol": p.symbol, "side": p.side, "lots": p.lots, "entry": p.entry, "sl": p.sl, "tp": p.tp,
            "risk_usd": p.risk_usd, "floating": p.floating, "last_price": p.last_price, "status": p.status,
            "opened_at": iso(p.opened_at), "closed_at": iso(p.closed_at), "profit": p.profit, "close_price": p.close_price,
            "signal_id": p.signal_id}


# ---------------------------------------------------------------- API
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/state")
def api_state():
    return engine.state()


@app.get("/api/signals/latest")
def api_latest():
    from .db import latest_signals_per_symbol

    return [sig_row(s) for s in latest_signals_per_symbol(get_settings().symbol_list)]


@app.get("/api/signals")
def api_signals(limit: int = 50, symbol: str = ""):
    with session() as s:
        q = select(Signal).order_by(Signal.id.desc()).limit(min(limit, 200))
        if symbol:
            q = q.where(Signal.symbol == symbol.upper())
        return [sig_row(x) for x in s.scalars(q)]


@app.get("/api/signal/{sid}")
def api_signal(sid: int):
    with session() as s:
        sig = s.get(Signal, sid)
        if not sig:
            raise HTTPException(404, "signal not found")
        out = sig_row(sig, full=True)
        pos = s.scalars(select(Position).where(Position.signal_id == sid)).first()
        out["position"] = pos_row(pos) if pos else None
        return out


@app.get("/api/positions")
def api_positions():
    with session() as s:
        opened = s.scalars(select(Position).where(Position.status == "open").order_by(Position.opened_at.desc())).all()
        closed = s.scalars(select(Position).where(Position.status == "closed").order_by(Position.closed_at.desc()).limit(15)).all()
        return {"open": [pos_row(p) for p in opened], "closed": [pos_row(p) for p in closed]}


@app.get("/api/approvals")
def api_approvals():
    with session() as s:
        rows = s.scalars(select(Signal).where(Signal.status == "pending_approval").order_by(Signal.id.desc())).all()
        return [sig_row(r, full=True) for r in rows]


@app.get("/api/activity")
def api_activity(limit: int = 40):
    with session() as s:
        rows = s.scalars(select(Activity).order_by(Activity.id.desc()).limit(min(limit, 200))).all()
        return [{"id": a.id, "ts": iso(a.ts), "level": a.level, "symbol": a.symbol, "text": a.text, "signal_id": a.signal_id} for a in rows]


def _bg(fn, *a):
    threading.Thread(target=lambda: _safe(fn, *a), daemon=True).start()


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception as e:  # noqa: BLE001
        log(f"{getattr(fn, '__name__', 'task')} failed: {type(e).__name__}: {e}", "error")


@app.post("/api/cycle")
def api_cycle(force: bool = False):
    _bg(engine.run_cycle, force)
    return {"started": True}


@app.post("/api/refresh")
def api_refresh():
    for fn in (collectors.fetch_calendar, collectors.fetch_macro, collectors.fetch_cot, refresh_news):
        _bg(fn)
    return {"started": True}


@app.post("/api/halt")
def api_halt(body: dict = Body(default={})):
    engine.halt(str(body.get("reason") or "halted from the dashboard")[:200])
    return engine.halted()


@app.post("/api/resume")
def api_resume():
    engine.resume()
    return engine.halted()


@app.post("/api/mode")
def api_mode(body: dict = Body(...)):
    try:
        return {"mode": engine.set_mode(str(body.get("mode", "")))}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/approve/{sid}")
def api_approve(sid: int):
    try:
        return {"status": engine.approve(sid)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/reject/{sid}")
def api_reject(sid: int):
    try:
        engine.reject(sid)
        return {"status": "rejected"}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/close/{ticket}")
def api_close(ticket: int):
    try:
        engine.close_position(ticket)
        return {"status": "closed"}
    except BrokerError as e:
        raise HTTPException(400, str(e))


@app.get("/api/diagnostics")
def api_diagnostics():
    """Read-only check of the MT5 bridge, symbol names and data feeds. Run this first after deploying."""
    cfg = get_settings()
    broker = get_broker()
    out = {"mock": cfg.mt5_mock, "broker": cfg.broker, "bridge": {"host": cfg.mt5_host, "port": cfg.mt5_port} if cfg.broker == "mt5" else {"env": cfg.ctrader_env}, "symbols": {}}
    try:
        broker.connect()
        a = broker.account()
        out["bridge"]["ok"] = True
        out["account"] = {"login": a["login"], "server": a["server"], "currency": a["currency"],
                          "type": {0: "demo", 1: "contest", 2: "REAL"}.get(a["trade_mode"], str(a["trade_mode"])), "balance": a["balance"]}
        engine.sync_offset(broker, cfg)
        out["broker_utc_offset_hours"] = broker.offset_s / 3600
        for sym in cfg.symbol_list:
            try:
                info = broker.symbol_info(cfg.broker_symbol(sym))
                df = broker.rates(cfg.broker_symbol(sym), 300)
                out["symbols"][sym] = {"ok": True, "candles": len(df), "digits": info["digits"], "contract": info["trade_contract_size"],
                                       "min_lot": info["volume_min"], "step": info["volume_step"], "profit_ccy": info["currency_profit"],
                                       "last_candle": iso(df["time"].iloc[-1].to_pydatetime())}
            except BrokerError as e:
                out["symbols"][sym] = {"ok": False, "error": str(e)}
    except BrokerError as e:
        out["bridge"]["ok"] = False
        out["bridge"]["error"] = str(e)
    out["keys"] = {"anthropic": bool(cfg.anthropic_api_key), "freellmapi": bool(cfg.freellmapi_base_url), "fred": bool(cfg.fred_api_key)}
    out["feeds_age_min"] = {n: collectors.snapshot_age_min(n) for n in ("macro", "cot", "calendar", "news_run")}
    return out


# ---------------------------------------------------------------- cTrader authorisation (one-time, in the browser)
def _redirect_uri(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "")).split(",")[0].strip()
    return f"{proto}://{host}/ctrader/callback"


@app.get("/ctrader/connect")
def ctrader_connect(request: Request):
    from . import ctrader

    if not get_settings().ctrader_client_id:
        raise HTTPException(400, "CTRADER_CLIENT_ID is not set")
    return RedirectResponse(ctrader.auth_url(_redirect_uri(request)))


@app.get("/ctrader/callback")
def ctrader_callback(request: Request, code: str = "", error: str = ""):
    from . import ctrader

    if error or not code:
        raise HTTPException(400, f"cTrader did not grant access: {error or 'no code returned'}")
    try:
        ctrader.exchange_code(code, _redirect_uri(request))
    except BrokerError as e:
        raise HTTPException(400, str(e))
    broker = get_broker()
    if hasattr(broker, "_drop"):
        broker._drop()  # reconnect with the new token on the next call
    log("cTrader access granted", "info")
    return RedirectResponse("/")


# ---------------------------------------------------------------- UI
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
