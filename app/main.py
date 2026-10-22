"""Anands Fino: web app, JSON API and the scheduler."""
import base64
import json
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import collectors, engine, sentiment, venues
from .broker import BrokerError, get_broker
from .config import get_settings
from .db import Activity, Position, Signal, init_db, iso, kv_get, kv_set, log, session

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


def _purge_paper() -> None:
    """Paper trading was removed: delete its trades, signals, journal lines and settings once (demo and live history is not touched)."""
    if kv_get("paper_removed"):
        return
    from sqlalchemy import delete

    with session() as s:
        ids = list(s.scalars(select(Signal.id).where(Signal.venue == "paper")))
        s.execute(delete(Position).where(Position.venue == "paper"))
        for k in range(0, len(ids), 500):
            s.execute(delete(Activity).where(Activity.signal_id.in_(ids[k:k + 500])))
        s.execute(delete(Signal).where(Signal.venue == "paper"))
        s.commit()
    for key in ("paper_state", "paper_start_balance", "paper_specs", "day_start:paper"):
        kv_set(key, None)
    v = dict(kv_get("venues") or {})
    if v.pop("paper", None) is not None:
        kv_set("venues", v)
    kv_set("paper_removed", True)
    log("Paper trading removed: its history and settings were deleted", "warn")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    init_db()
    if cfg.scheduler_enabled:
        # every minute; each venue only trades when a candle of its own timeframe has just closed
        scheduler.add_job(job("cycle", engine.run_cycle), "cron", minute="*", second=cfg.cycle_delay_seconds, max_instances=1, coalesce=True)
        scheduler.add_job(job("watch", engine.watch_positions), "interval", seconds=cfg.watch_every_s, max_instances=1, coalesce=True)
        scheduler.add_job(job("reconcile", engine.reconcile), "interval", seconds=5, max_instances=1, coalesce=True)
        scheduler.add_job(job("news", refresh_news), "interval", minutes=5, max_instances=1, coalesce=True)
        scheduler.add_job(job("calendar", collectors.fetch_calendar), "interval", minutes=60, max_instances=1, coalesce=True)
        scheduler.add_job(job("macro", collectors.fetch_macro), "interval", hours=6, max_instances=1, coalesce=True)
        scheduler.add_job(job("cot", collectors.fetch_cot), "interval", hours=12, max_instances=1, coalesce=True)
        from . import backtest
        from datetime import datetime as _dt, timedelta as _td

        scheduler.add_job(backtest.run_all, "interval", days=2, id="backtest_regular", replace_existing=True)   # re-test on fresh data every 2 days
        if backtest.needs_run():   # first start, a run cut short by a restart, new rules, or results older than 3 days
            scheduler.add_job(backtest.run_all, "date", run_date=_dt.now() + _td(seconds=45), id="backtest_first", replace_existing=True)
        scheduler.start()
        _purge_paper()
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
        "levels": s.levels, "mode": s.mode, "venue": s.venue, "timeframe": s.timeframe, "expires_at": iso(s.expires_at),
        "confidence": (s.claude or {}).get("confidence"), "decision": (s.claude or {}).get("decision"),
    }
    if full:
        d.update({"factors": s.factors, "claude": s.claude, "gate": s.gate, "context": s.context})
    return d


def _tf_minutes(label: str | None) -> int | None:
    """'M3' -> 3, 'H1' -> 60."""
    import re

    m = re.fullmatch(r"([MH])(\d+)", label or "")
    return None if not m else int(m.group(2)) * (60 if m.group(1) == "H" else 1)


def pos_row(p: Position) -> dict:
    from sqlalchemy.orm import object_session

    sig = object_session(p).get(Signal, p.signal_id) if p.signal_id and object_session(p) else None
    return {"tf": _tf_minutes(sig.timeframe) if sig else None, "ticket": p.ticket, "symbol": p.symbol, "side": p.side, "lots": p.lots, "entry": p.entry, "sl": p.sl, "tp": p.tp,
            "risk_usd": p.risk_usd, "floating": p.floating, "last_price": p.last_price, "status": p.status,
            "opened_at": iso(p.opened_at), "closed_at": iso(p.closed_at), "profit": p.profit, "close_price": p.close_price,
            "signal_id": p.signal_id, "venue": p.venue, "sl0": p.sl0 or p.sl, "watch": engine._watch.get((p.venue, p.ticket))}


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


@app.get("/api/candles")
def api_candles(symbol: str, tf: int = 1, venue: str = "demo", n: int = 120):
    """Recent candles [time, open, high, low, close] (the last one is still forming) for the terminal chart."""
    cfg = get_settings()
    if venue not in venues.VENUES or symbol.upper() not in cfg.symbol_list or tf not in venues.TIMEFRAMES:
        raise HTTPException(400, "unknown venue, symbol or timeframe")
    try:
        b = get_broker(venue)
        b.connect()
        df = b.rates(cfg.broker_symbol(symbol.upper()), max(n + 60, 200), tf)
    except BrokerError as e:
        raise HTTPException(502, str(e)) from e
    df = df.tail(n)
    return {"symbol": symbol.upper(), "tf": tf, "venue": venue,
            "candles": [[int(t.timestamp()), float(o), float(h), float(l), float(c)] for t, o, h, l, c in zip(df["time"], df["open"], df["high"], df["low"], df["close"])]}


@app.get("/api/analysis")
def api_analysis(symbol: str, venue: str = "demo"):
    """Every indicator and factor behind the verdict for a symbol, live."""
    cfg = get_settings()
    if venue not in venues.VENUES or symbol.upper() not in cfg.symbol_list:
        raise HTTPException(400, "unknown venue or symbol")
    try:
        return engine.analysis_for(symbol.upper(), venue)
    except BrokerError as e:
        raise HTTPException(502, str(e)) from e


@app.post("/api/watch")
def api_watch(body: dict = Body(...)):
    """Switch the reversal watch on or off."""
    on = bool(body.get("on"))
    kv_set("watch", on)
    log("Reversal watch " + ("ON: open trades are re-scored every 30 s and protected or closed when the view flips" if on else "OFF"), "info")
    return {"watch": on}


@app.get("/api/stream")
def api_stream(sym: str = ""):
    """Server-sent events: live bid/ask for every symbol on each active venue, plus the live value of every open trade, about 4 times a second."""
    wanted = sym

    def gen():
        cfg = get_settings()
        focus = [x for x in wanted.upper().split(",") if x in cfg.symbol_list][:3]
        opens, opens_at, pvs = [], 0.0, {}
        while True:
            t0 = time.time()
            try:
                if t0 - opens_at > 1.5:
                    opens, opens_at = engine.open_positions_view(), t0
                active = [v for v in venues.VENUES if not venues.locked(v) and (venues.venue_cfg(v)["on"] or any(p["venue"] == v for p in opens))]
                px, brokers = {}, {}
                for v in active:
                    try:
                        b = get_broker(v)
                        b.connect()
                        brokers[v] = b
                    except BrokerError:
                        continue
                    row = {}
                    for sym in cfg.symbol_list:
                        try:
                            tk = (b.tick_fast if hasattr(b, "tick_fast") else b.tick)(cfg.broker_symbol(sym))
                            row[sym] = [tk["bid"], tk["ask"]]
                        except BrokerError:
                            continue
                    px[v] = row
                pos = []
                for p in opens:
                    row = px.get(p["venue"], {}).get(p["symbol"])
                    b = brokers.get(p["venue"])
                    if not row or b is None:
                        continue
                    key = (p["venue"], p["symbol"])
                    if key not in pvs or t0 - pvs[key][1] > 30:
                        try:
                            info = b.symbol_info(cfg.broker_symbol(p["symbol"]))
                            pvs[key] = (info["trade_contract_size"] * (engine.profit_rate(b, info, b.account()) or 0.0), t0)
                        except BrokerError:
                            continue
                    d = 1 if p["side"] == "BUY" else -1
                    live = row[0] if d == 1 else row[1]
                    risk = abs(p["entry"] - p["sl0"]) or 1.0
                    pos.append({"venue": p["venue"], "ticket": p["ticket"], "live": live, "floating": d * (live - p["entry"]) * p["lots"] * pvs[key][0], "r": d * (live - p["entry"]) / risk})
                yield "data: " + json.dumps({"ts": int(time.time() * 1000), "px": px, "pos": pos}) + "\n\n"
            except Exception:  # noqa: BLE001  (never let one bad cycle end the stream)
                yield ": hiccup\n\n"
            time.sleep(max(0.05, 0.25 - (time.time() - t0)))
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
        tf = body.get("tf")
        return venues.set_venue(name, None if on is None else bool(on), None if auto is None else bool(auto), None if tf is None else int(tf))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/profile")
def api_profile(body: dict = Body(...)):
    from .config import PROFILES, profile_name
    p = body.get("profile")
    if p not in PROFILES:
        raise HTTPException(400, "profile must be fast or normal")
    kv_set("profile", p)
    log(f"Trading profile: {p}" + (" (looser filters, more trades)" if p == "fast" else " (stricter filters)"), "info")
    return {"profile": profile_name()}


@app.post("/api/trail")
def api_trail(body: dict = Body(...)):
    on = bool(body.get("on"))
    kv_set("trail", on)
    log("Trailing stop " + ("ON: profit is locked as trades move our way" if on else "OFF: stops stay where they were set"), "info")
    return {"trail": on}


@app.post("/api/claude-veto")
def api_claude_veto(body: dict = Body(...)):
    on = bool(body.get("on"))
    kv_set("claude_veto", on)
    log("Claude has the final say again" if on else "Claude's veto is OFF: trades are taken on the score and the risk gate alone", "warn")
    return {"claude_veto": on}


@app.post("/api/symbols")
def api_symbols(body: dict = Body(...)):
    """Set the traded symbols from the dashboard ({"symbols": "EURUSD,XAUUSD"}) or go back to the environment list ({"reset": true})."""
    import re

    if body.get("reset"):
        kv_set("symbols", None)
        log("Symbols reset to the environment list", "warn")
    else:
        raw = body.get("symbols") or ""
        syms = list(dict.fromkeys(s.strip().upper() for s in (raw if isinstance(raw, str) else ",".join(raw)).replace(";", ",").replace(" ", ",").split(",") if s.strip()))
        bad = [s for s in syms if not re.fullmatch(r"[A-Z0-9]{6,12}", s)]
        if not syms or bad or len(syms) > 30:
            raise HTTPException(400, "Give 1 to 30 symbols such as EURUSD, comma separated" + (f" (not valid: {', '.join(bad)})" if bad else ""))
        kv_set("symbols", syms)
        log(f"Symbols set from the dashboard: {', '.join(syms)}", "warn")
    return {"symbols": get_settings().symbol_list}


_specs_cache: dict = {}


@app.get("/api/specs")
def api_specs(venue: str = "demo"):
    """Contract specification and live spread of every traded symbol, as the chosen venue's broker reports them (read in parallel, kept 10 s)."""
    from concurrent.futures import ThreadPoolExecutor

    if venue not in venues.VENUES:
        raise HTTPException(400, "unknown venue")
    hit = _specs_cache.get(venue)
    if hit and time.time() - hit[0] < 10:
        return hit[1]
    cfg = get_settings()
    broker = get_broker(venue)
    err = ""
    try:
        broker.connect()
    except BrokerError as e:
        err = str(e)

    def one(sym: str) -> dict:
        row = {"symbol": sym, "ok": False}
        if err:
            return row
        try:
            info = broker.symbol_info(cfg.broker_symbol(sym))
            tk = broker.tick(cfg.broker_symbol(sym))
            sp = tk["ask"] - tk["bid"]
            pt = info["point"] or 10.0 ** -info["digits"]
            row.update({"ok": True, "broker_symbol": cfg.broker_symbol(sym), "digits": info["digits"], "point": pt, "contract": info["trade_contract_size"],
                        "volume_min": info["volume_min"], "volume_step": info["volume_step"], "volume_max": info["volume_max"],
                        "profit_ccy": info["currency_profit"], "base_ccy": info.get("currency_base", ""), "spread": round(sp, info["digits"] + 1),
                        "spread_points": round(sp / pt, 1) if pt else None, "bid": round(tk["bid"], info["digits"]), "ask": round(tk["ask"], info["digits"]),
                        "cost_per_lot": round(sp * info["trade_contract_size"], 2)})
        except BrokerError as e:
            row["error"] = str(e)
        return row

    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(one, cfg.symbol_list))
    out = {"venue": venue, "editable": False, "error": err, "rows": rows}
    _specs_cache[venue] = (time.time(), out)
    return out


@app.post("/api/allow-live")
def api_allow_live(body: dict = Body(...)):
    return {"allow_live": venues.set_allow_live(bool(body.get("on")))}


@app.post("/api/tune")
@app.post("/api/risk")
def api_tune(body: dict = Body(...)):
    """Set any tunable rule by hand ({"daily_loss_cap_pct": 3, "w_trend": 30}). {"clear": ["max_open"]} drops single overrides, {"reset": true} drops all."""
    from .config import TUNABLES, profile_settings, tune_table

    if body.get("reset"):
        kv_set("risk", None)
        get_settings.cache_clear()
        log("All rules reset to the profile values", "warn")
        return {"ok": True}
    cur = dict(kv_get("risk") or {})
    for k in body.get("clear") or []:
        cur.pop(k, None)
    base = {r["key"]: r["base"] for r in tune_table()}
    changed = []
    for k, v in body.items():
        if k in ("clear", "reset"):
            continue
        if k not in TUNABLES:
            raise HTTPException(400, f"unknown rule {k}")
        _g, label, lo, hi, _st, whole = TUNABLES[k]
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{label} must be a number")
        if not (lo <= x <= hi):
            raise HTTPException(400, f"{label} must be between {lo:g} and {hi:g}")
        x = int(round(x)) if whole else round(x, 4)
        if abs(x - float(base[k])) < 1e-9:
            if cur.pop(k, None) is not None:
                changed.append(f"{label} back to the profile value")
        elif cur.get(k) != x:
            cur[k] = x
            changed.append(f"{label} {x:g}")
    kv_set("risk", cur or None)
    if changed:
        log("Rules set by hand: " + "; ".join(changed), "warn")
    return {"ok": True, "custom": cur}


@app.get("/api/backtest")
def api_backtest():
    from . import backtest

    st = backtest.status()
    return {**st, "strategy_mode": engine.strategy_mode(), "proven_only": engine.proven_only(), "live_set": kv_get("strategies") or {}}


@app.post("/api/backtest/run")
def api_backtest_run():
    """Replay every strategy on the last weeks or months of real candles (spread paid) and keep what also earns on data it was not tuned on."""
    from . import backtest

    return {"started": backtest.run_all()}


@app.post("/api/strategy-mode")
def api_strategy_mode(body: dict = Body(...)):
    on = bool(body.get("on"))
    kv_set("strategy_mode", on)
    log("Strategy mode " + ("ON: only setups proven on past data trade" if on else "OFF: the old score-only entries trade"), "warn")
    return {"strategy_mode": on}


@app.post("/api/proven-only")
def api_proven_only(body: dict = Body(...)):
    on = bool(body.get("on"))
    kv_set("proven_only", on)
    log("Proven only " + ("ON: symbols with no proven setup do not trade" if on else "OFF: symbols with no proven setup trade on the score rules (UNPROVEN)"), "warn")
    return {"proven_only": on}


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


@app.get("/api/trade/ticket")
def api_trade_ticket(venue: str, symbol: str, side: str = "BUY"):
    """Starting values for the manual order ticket."""
    try:
        return engine.manual_ticket(venue, symbol.upper(), side.upper())
    except (ValueError, BrokerError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/trade")
def api_trade(body: dict = Body(...)):
    """Open a trade by hand on any traded symbol."""
    try:
        return engine.manual_trade(str(body.get("venue", "")), str(body.get("symbol", "")).upper(), str(body.get("side", "")).upper(),
                                   float(body.get("lots") or 0), float(body["sl"]), float(body.get("tp") or 0), bool(body.get("confirm_live")))
    except (KeyError, TypeError):
        raise HTTPException(400, "stop loss is required and every value must be a number")
    except (ValueError, BrokerError) as e:
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
    v = venue or next((x for x in ("demo", "live") if venues.venue_cfg(x)["on"]), "demo")
    broker = get_broker(v)
    out = {"mock": cfg.mt5_mock, "venue": v, "broker": cfg.broker,
           "bridge": {"host": cfg.mt5_host, "port": cfg.mt5_port} if cfg.broker == "mt5" else {"env": v}, "symbols": {}}
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
