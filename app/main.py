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

from . import collectors, engine, sentiment, venues
from .broker import BrokerError, get_broker
from .config import get_settings
from .db import Activity, Position, Signal, init_db, iso, kv_set, log, session

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
        log(f"Anands Fino started. Venues on: {', '.join(venues.enabled()) or 'none'}, symbols {', '.join(cfg.symbol_list)}"
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
        "levels": s.levels, "mode": s.mode, "venue": s.venue, "expires_at": iso(s.expires_at),
        "confidence": (s.claude or {}).get("confidence"), "decision": (s.claude or {}).get("decision"),
    }
    if full:
        d.update({"factors": s.factors, "claude": s.claude, "gate": s.gate, "context": s.context})
    return d


def pos_row(p: Position) -> dict:
    return {"ticket": p.ticket, "symbol": p.symbol, "side": p.side, "lots": p.lots, "entry": p.entry, "sl": p.sl, "tp": p.tp,
            "risk_usd": p.risk_usd, "floating": p.floating, "last_price": p.last_price, "status": p.status,
            "opened_at": iso(p.opened_at), "closed_at": iso(p.closed_at), "profit": p.profit, "close_price": p.close_price,
            "signal_id": p.signal_id, "venue": p.venue}


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

    out = []
    with session() as ses:
        for s in latest_signals_per_symbol(get_settings().symbol_list):
            row = sig_row(s)
            same = ses.scalars(select(Signal).where(Signal.cycle_id == s.cycle_id, Signal.symbol == s.symbol, Signal.venue != "").order_by(Signal.id)).all()
            row["venues"] = [{"venue": x.venue, "status": x.status, "id": x.id, "lots": x.lots, "reason": x.reason} for x in same]
            out.append(row)
    return out


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


@app.get("/api/report")
def api_report(venue: str = "", days: int = 0):
    """Trading report from closed trades (plus open floating P/L), per venue or for all three."""
    from datetime import timedelta

    rows = []
    with session() as s:
        q = select(Position).where(Position.status == "closed").order_by(Position.closed_at)
        if venue:
            q = q.where(Position.venue == venue)
        if days:
            q = q.where(Position.closed_at >= engine.utcnow() - timedelta(days=days))
        rows = s.scalars(q).all()
        open_q = select(Position).where(Position.status == "open")
        if venue:
            open_q = open_q.where(Position.venue == venue)
        opens = s.scalars(open_q).all()
    def stats(items):
        pl = [p.profit or 0.0 for p in items]
        wins, losses = [x for x in pl if x > 0], [x for x in pl if x < 0]
        cum, peak, dd, curve = 0.0, 0.0, 0.0, []
        for p, x in zip(items, pl):
            cum += x
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
            curve.append({"t": iso(p.closed_at), "pl": round(cum, 2)})
        gl = -sum(losses)
        return {"trades": len(pl), "wins": len(wins), "losses": len(losses), "win_rate": (len(wins) / len(pl) * 100) if pl else 0.0,
                "net": round(sum(pl), 2), "gross_profit": round(sum(wins), 2), "gross_loss": round(-gl, 2),
                "profit_factor": round(sum(wins) / gl, 2) if gl else (None if not wins else 999.0),
                "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0, "avg_loss": round(-gl / len(losses), 2) if losses else 0.0,
                "best": round(max(pl), 2) if pl else 0.0, "worst": round(min(pl), 2) if pl else 0.0,
                "expectancy": round(sum(pl) / len(pl), 2) if pl else 0.0, "max_drawdown": round(dd, 2), "curve": curve}
    out = {"venues": {}, "days": days}
    for v in ([venue] if venue else list(venues.VENUES)):
        mine = [p for p in rows if p.venue == v]
        st = stats(mine)
        now = engine.utcnow()
        st["today"] = round(sum(p.profit or 0 for p in mine if p.closed_at and p.closed_at.date() == now.date()), 2)
        st["week"] = round(sum(p.profit or 0 for p in mine if p.closed_at and (now - (p.closed_at if p.closed_at.tzinfo else p.closed_at.replace(tzinfo=now.tzinfo))).days < 7), 2)
        st["open_count"] = sum(1 for p in opens if p.venue == v)
        st["floating"] = round(sum(p.floating for p in opens if p.venue == v), 2)
        by = {}
        for p in mine:
            by.setdefault(p.symbol, []).append(p)
        st["by_symbol"] = sorted(({"symbol": k, "trades": len(x), "net": round(sum(i.profit or 0 for i in x), 2),
                                   "win_rate": sum(1 for i in x if (i.profit or 0) > 0) / len(x) * 100} for k, x in by.items()), key=lambda r: -r["net"])
        st["recent"] = [pos_row(p) for p in reversed(mine[-25:])]
        out["venues"][v] = st
    return out


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


@app.post("/api/venue/{name}")
def api_venue(name: str, body: dict = Body(...)):
    """Switch a venue on or off and set auto-approve. Send {"on": bool} and/or {"auto": bool}."""
    try:
        on = body.get("on")
        auto = body.get("auto")
        return venues.set_venue(name, None if on is None else bool(on), None if auto is None else bool(auto))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/allow-live")
def api_allow_live(body: dict = Body(...)):
    return {"allow_live": venues.set_allow_live(bool(body.get("on")))}


@app.post("/api/paper/reset")
def api_paper_reset():
    from .paper import get_paper

    if engine.open_positions_view("paper"):
        raise HTTPException(400, "Close the open paper positions first")
    get_paper().reset()
    venues.set_loss_streak("paper", 0)
    kv_set("day_start:paper", None)
    log("Paper account reset", "warn")
    return {"ok": True}


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


@app.post("/api/close/{venue}/{ticket}")
def api_close(venue: str, ticket: int):
    if venue not in venues.VENUES:
        raise HTTPException(400, "unknown venue")
    try:
        engine.close_position(ticket, venue)
        return {"status": "closed"}
    except BrokerError as e:
        raise HTTPException(400, str(e))


@app.get("/api/diagnostics")
def api_diagnostics(venue: str = ""):
    """Read-only check of a venue's connection, symbol names and data feeds. Run this first after deploying."""
    cfg = get_settings()
    v = venue or next((x for x in ("demo", "paper", "live") if venues.venue_cfg(x)["on"]), "demo")
    broker = get_broker(v)
    out = {"mock": cfg.mt5_mock, "venue": v, "feed": cfg.paper_feed if v == "paper" else None, "broker": cfg.broker,
           "bridge": {"host": cfg.mt5_host, "port": cfg.mt5_port} if cfg.broker == "mt5" else {"env": v if v != "paper" else "paper"}, "symbols": {}}
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
    for v in ("demo", "live"):
        broker = get_broker(v)
        if hasattr(broker, "_drop"):
            broker._drop()  # reconnect with the new token on the next call
    log("cTrader access granted", "info")
    return RedirectResponse("/")


# ---------------------------------------------------------------- UI
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
