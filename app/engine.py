"""The hourly cycle, order execution, reconciliation and the kill switch."""
import threading
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from . import collectors, scoring
from .broker import ACCOUNT_TRADE_MODE_DEMO, BrokerError, get_broker
from .config import LIVE_MODES, MODES, Settings, get_settings
from .db import NewsItem, Position, Signal, iso, kv_get, kv_set, log, session, utcnow
from .decision import decide
from .features import compute_technicals
from .gate import evaluate

_lock = threading.Lock()
MIN_CANDLES = 260


# ---------------------------------------------------------------- small state helpers
def get_mode() -> str:
    return kv_get("mode") or get_settings().mode


def set_mode(mode: str) -> str:
    cfg = get_settings()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if mode in LIVE_MODES and not cfg.allow_live:
        raise ValueError("live modes are disabled. Set ALLOW_LIVE=true in the environment first.")
    kv_set("mode", mode)
    log(f"Autonomy level changed to {mode}", "warn")
    return mode


def halted() -> dict:
    return kv_get("halted") or {"on": False, "reason": "", "since": None}


def halt(reason: str) -> None:
    kv_set("halted", {"on": True, "reason": reason, "since": iso(utcnow())})
    log(f"TRADING HALTED: {reason}", "error")
    notify(f"Anands Fino halted: {reason}")


def resume() -> None:
    kv_set("halted", {"on": False, "reason": "", "since": None})
    kv_set("loss_streak", 0)
    log("Trading resumed by user", "warn")


def notify(text: str, payload: dict | None = None) -> None:
    url = get_settings().notify_webhook_url
    if not url:
        return
    try:
        httpx.post(url, json={"text": text, "source": "anands-fino", **(payload or {})}, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def market_open(now: datetime | None = None) -> bool:
    """Forex trades from Sunday 22:00 UTC to Friday 22:00 UTC."""
    now = now or utcnow()
    wd, hr = now.weekday(), now.hour  # Monday is 0
    if wd == 5:
        return False
    if wd == 6 and hr < 22:
        return False
    if wd == 4 and hr >= 22:
        return False
    return True


def day_start_equity(account: dict) -> float:
    today = utcnow().strftime("%Y-%m-%d")
    row = kv_get("day_start")
    if not row or row.get("day") != today:
        row = {"day": today, "equity": account["equity"]}
        kv_set("day_start", row)
    return row["equity"]


def open_positions_view() -> list[dict]:
    with session() as s:
        rows = s.scalars(select(Position).where(Position.status == "open")).all()
        return [{"ticket": p.ticket, "symbol": p.symbol, "side": p.side, "lots": p.lots, "entry": p.entry, "sl": p.sl,
                 "tp": p.tp, "risk_usd": p.risk_usd, "floating": p.floating, "last_price": p.last_price,
                 "opened_at": iso(p.opened_at), "signal_id": p.signal_id} for p in rows]


def account_is_demo(account: dict) -> bool:
    return account.get("trade_mode") == ACCOUNT_TRADE_MODE_DEMO


# ---------------------------------------------------------------- broker offset
def sync_offset(broker, cfg: Settings) -> None:
    if cfg.broker_utc_offset_hours.strip():
        broker.offset_s = int(float(cfg.broker_utc_offset_hours) * 3600)
        return
    if market_open():
        off = broker.detect_offset_s(cfg.broker_symbol(cfg.symbol_list[0]))
        if off is not None:
            broker.offset_s = off
            kv_set("broker_offset_s", off)
            return
    broker.offset_s = int(kv_get("broker_offset_s", 0) or 0)


# ---------------------------------------------------------------- the cycle
def run_cycle(force: bool = False) -> dict:
    """Score every symbol on the last closed candle, then take each candidate through Claude, the gate and execution."""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "note": "a cycle is already running"}
    try:
        return _run_cycle(force)
    finally:
        _lock.release()


def _run_cycle(force: bool) -> dict:
    cfg = get_settings()
    cycle_id = utcnow().strftime("%Y%m%d%H") + "-" + uuid.uuid4().hex[:4]
    if halted()["on"]:
        log(f"Cycle skipped: halted ({halted()['reason']})", "warn")
        return {"ok": False, "note": "halted"}
    if not force and not market_open():
        log("Cycle skipped: forex market is closed", "info")
        return {"ok": False, "note": "market closed"}

    broker = get_broker()
    try:
        broker.connect()
        sync_offset(broker, cfg)
        account = broker.account()
    except BrokerError as e:
        log(f"Cycle aborted: {e}", "error")
        return {"ok": False, "note": str(e)}

    start_eq = day_start_equity(account)
    mode = get_mode()
    macro = (collectors.snapshot("macro") or {}).get("data")
    cot = (collectors.snapshot("cot") or {}).get("data")
    positions = open_positions_view()

    # 1. technicals for every symbol
    techs, infos, ticks, closes, candle_times, stale = {}, {}, {}, {}, {}, {}
    for sym in cfg.symbol_list:
        bsym = cfg.broker_symbol(sym)
        try:
            info = broker.symbol_info(bsym)
            df = broker.rates(bsym, 500)
            tick = broker.tick(bsym)
        except BrokerError as e:
            log(f"{sym}: {e}", "error", sym)
            continue
        closed = df.iloc[:-1].reset_index(drop=True)
        if len(closed) < MIN_CANDLES:
            log(f"{sym}: only {len(closed)} candles, need {MIN_CANDLES}", "warn", sym)
            continue
        last_open = closed["time"].iloc[-1]
        age_h = (utcnow() - last_open.to_pydatetime()).total_seconds() / 3600
        stale[sym] = (not force) and age_h > 2.5
        techs[sym] = compute_technicals(closed, info["digits"])
        infos[sym], ticks[sym] = info, tick
        closes[sym] = [round(float(x), info["digits"]) for x in closed["close"].tail(48)]
        candle_times[sym] = iso(last_open.to_pydatetime())

    since = utcnow() - timedelta(hours=24)
    with session() as s:
        news24 = list(s.scalars(select(NewsItem).where(NewsItem.published >= since)))
        cb_items = [{"source": n.source, "title": n.title, "age_h": round((utcnow() - _aware(n.published)).total_seconds() / 3600, 1)}
                    for n in s.scalars(select(NewsItem).where(NewsItem.central_bank == 1, NewsItem.published >= utcnow() - timedelta(hours=48)).order_by(NewsItem.published.desc()).limit(6))]

    results = {}
    for sym, tech in techs.items():
        info, tick = infos[sym], ticks[sym]
        factors = {
            "trend": {**tech["trend"], "source": "code"},
            "pattern": {**tech["pattern"], "source": "code"},
            "momentum": {**tech["momentum"], "source": "code"},
        }
        sc, rd = scoring.macro_score(sym, macro)
        factors["macro"] = {"score": sc, "reading": rd, "source": "FRED"}
        sc, rd = scoring.news_score(sym, news24)
        factors["news"] = {"score": sc, "reading": rd, "source": "FreeLLMAPI"}
        sc, rd = scoring.positioning_score(sym, cot)
        factors["positioning"] = {"score": sc, "reading": rd, "source": "CFTC"}
        sc, rd = scoring.intermarket_score(sym, techs)
        factors["intermarket"] = {"score": sc, "reading": rd, "source": "code"}
        weights = cfg.weights_dict
        for k, v in factors.items():
            v["weight"] = weights.get(k, 0)
        comb = scoring.combine(factors, weights, cfg.score_threshold, cfg.min_agree)

        sig = Signal(cycle_id=cycle_id, symbol=sym, candle_time=candle_times[sym], bias=comb["bias"], score=comb["score"],
                     agree=comb["agree"], available=comb["available"], factors=factors, mode=mode,
                     context={"closes": closes[sym]})
        if comb["bias"] == "NONE":
            why = []
            if abs(comb["score"]) < cfg.score_threshold:
                why.append(f"score {comb['score']:+.1f} is inside the ±{cfg.score_threshold:g} threshold")
            if comb["agree"] < cfg.min_agree:
                why.append(f"{comb['agree']} factors agree, {cfg.min_agree} needed")
            sig.status, sig.reason = "no_trade", "No trade: " + " and ".join(why)
            levels = scoring.build_levels("BUY" if comb["score"] >= 0 else "SELL", tech, tick, info["digits"], cfg.stop_atr_mult, cfg.rr_target)
            sig.levels = levels
            with session() as s:
                s.add(sig)
                s.commit()
                sid = sig.id
            log(f"{sym}: {sig.reason}", "info", sym, sid)
            results[sym] = sig.status
            continue

        levels = scoring.build_levels(comb["bias"], tech, tick, info["digits"], cfg.stop_atr_mult, cfg.rr_target)
        sig.levels = levels
        spread_price = tick["ask"] - tick["bid"]
        blk = collectors.blackout(sym)
        data_ok = not stale.get(sym, False)
        gate_args = dict(cfg=cfg, symbol=sym, bias=comb["bias"], levels=levels, account=account, sym_info=info,
                         spread_price=spread_price, open_positions=positions, day_start_equity=start_eq,
                         loss_streak=int(kv_get("loss_streak", 0) or 0), halted=False, blackout=blk, data_ok=data_ok)
        pre = evaluate(confidence=None, **gate_args)
        sig.gate = pre
        if not pre["ok"]:
            sig.status, sig.reason = "gate_blocked", "Blocked by the risk gate: " + pre["reason"]
            _save(sig)
            log(f"{sym}: {comb['bias']} score {comb['score']:+.0f} blocked. {pre['reason']}", "warn", sym, sig.id)
            results[sym] = sig.status
            continue

        # Claude has the final say
        ctx = _claude_context(sym, comb, factors, levels, tech, closes[sym], news24, cb_items, macro, cot, positions, account, start_eq, info)
        sig.context = {**sig.context, "claude_input": {k: v for k, v in ctx.items() if k != "recent_closes"}}
        dec = decide(ctx)
        sig.claude = dec
        if dec["decision"] != "YES":
            sig.status = "claude_no"
            sig.reason = "Claude said no" + (f": {dec['against']}" if dec.get("against") else f" ({dec['error']})" if dec.get("error") else "")
            _save(sig)
            log(f"{sym}: Claude declined {comb['bias']}. {dec.get('against') or dec.get('error', '')}", "info", sym, sig.id)
            results[sym] = sig.status
            continue
        fin = evaluate(confidence=dec["confidence"], **gate_args)
        sig.gate = fin
        if not fin["ok"]:
            sig.status, sig.reason = "gate_blocked", "Blocked by the risk gate: " + fin["reason"]
            _save(sig)
            log(f"{sym}: Claude said yes at {dec['confidence']:.2f} but the gate blocked it. {fin['reason']}", "warn", sym, sig.id)
            results[sym] = sig.status
            continue

        sig.lots = fin["lots"]
        _save(sig)
        results[sym] = _dispatch(sig.id, mode)
        positions = open_positions_view()
    return {"ok": True, "cycle_id": cycle_id, "results": results}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _save(sig: Signal) -> None:
    with session() as s:
        s.add(sig) if sig.id is None else s.merge(sig)
        s.commit()


def _claude_context(sym, comb, factors, levels, tech, closes, news24, cb_items, macro, cot, positions, account, start_eq, info) -> dict:
    heads = [{"title": n.title, "effect": (n.tags or {}).get(sym, 0)} for n in sorted(news24, key=lambda x: x.published, reverse=True)
             if n.tags is not None and (n.tags or {}).get(sym, 0) != 0][:12]
    return {
        "symbol": sym, "timeframe": "H1", "proposed_side": comb["bias"],
        "composite_score": comb["score"], "factors_agreeing": f"{comb['agree']} of {comb['available']}",
        "factors": {k: {"score": None if v["score"] is None else round(v["score"]), "weight": v["weight"], "reading": v["reading"]} for k, v in factors.items()},
        "levels": levels,
        "technicals": {"close": round(tech["close"], info["digits"]), "atr": round(tech["atr"], info["digits"]),
                       "ema20": round(tech["ema20"], info["digits"]), "ema50": round(tech["ema50"], info["digits"]),
                       "ema200": round(tech["ema200"], info["digits"]), "rsi": round(tech["rsi"]),
                       "swing_highs": [round(x, info["digits"]) for x in tech["swing_highs"]],
                       "swing_lows": [round(x, info["digits"]) for x in tech["swing_lows"]]},
        "recent_closes": closes[-24:],
        "headlines_moving_this_symbol": heads,
        "central_bank_items_48h": cb_items,
        "macro": macro, "positioning": (cot or {}).get(sym),
        "upcoming_events_36h": collectors.upcoming_events(),
        "open_positions": [{"symbol": p["symbol"], "side": p["side"], "lots": p["lots"]} for p in positions],
        "account": {"equity": round(account["equity"], 2), "day_start_equity": round(start_eq, 2)},
    }


# ---------------------------------------------------------------- execution
def _dispatch(signal_id: int, mode: str) -> str:
    cfg = get_settings()
    with session() as s:
        sig = s.get(Signal, signal_id)
        side, sym = sig.bias, sig.symbol
        text = f"{sym} {side} {sig.lots:g} lots, entry {sig.levels['entry']}, stop {sig.levels['sl']}, target {sig.levels['tp']}"
        if mode == "research":
            sig.status, sig.reason = "alerted", "Research mode: signal recorded, no order"
            s.commit()
            log(f"Signal (research): {text}", "trade", sym, signal_id)
            return sig.status
        if mode == "alerts":
            sig.status, sig.reason = "alerted", "Alerts mode: place it yourself"
            s.commit()
            log(f"Signal (alert): {text}", "trade", sym, signal_id)
            notify(f"Signal: {text}")
            return sig.status
        if mode == "live_approval":
            sig.status = "pending_approval"
            sig.expires_at = utcnow() + timedelta(minutes=cfg.approval_ttl_min)
            sig.reason = "Waiting for your approval"
            s.commit()
            log(f"Approval needed: {text}", "trade", sym, signal_id)
            notify(f"Approval needed: {text}", {"signal_id": signal_id})
            return sig.status
    return execute_signal(signal_id)


def execute_signal(signal_id: int) -> str:
    """Final checks and the market order. Refuses a real account unless live trading was enabled on purpose."""
    cfg = get_settings()
    broker = get_broker()
    with session() as s:
        sig = s.get(Signal, signal_id)
        if sig is None or sig.status in ("executed", "rejected", "expired"):
            return "ignored"
        sym, side, levels, mode = sig.symbol, sig.bias, dict(sig.levels), get_mode()
        try:
            broker.connect()
            account = broker.account()
            bsym = cfg.broker_symbol(sym)
            info = broker.symbol_info(bsym)
            tick = broker.tick(bsym)
        except BrokerError as e:
            return _fail(s, sig, f"broker unavailable: {e}")

        real = not account_is_demo(account)
        if real and not (cfg.allow_live and mode in LIVE_MODES):
            return _fail(s, sig, "refused: the connected account is a REAL account and live trading is not enabled")

        # re-check against the price right now
        price = tick["ask"] if side == "BUY" else tick["bid"]
        moved = abs(price - levels["entry"])
        if moved > 0.25 * levels["atr"]:
            return _fail(s, sig, f"price moved {moved:g} since the signal (more than 0.25 ATR)")
        live_levels = {**levels, "entry": price}
        fin = evaluate(cfg=cfg, symbol=sym, bias=side, levels=live_levels, confidence=(sig.claude or {}).get("confidence", 0.0),
                       account=account, sym_info=info, spread_price=tick["ask"] - tick["bid"], open_positions=open_positions_view(),
                       day_start_equity=day_start_equity(account), loss_streak=int(kv_get("loss_streak", 0) or 0), halted=halted()["on"],
                       blackout=collectors.blackout(sym), data_ok=True)
        sig.gate = fin
        if not fin["ok"]:
            return _fail(s, sig, "gate re-check failed: " + fin["reason"], status="gate_blocked")
        try:
            res = broker.send_market(bsym, side, fin["lots"], levels["sl"], levels["tp"], cfg.magic, cfg.deviation)
        except BrokerError as e:
            return _fail(s, sig, str(e))
        ticket = int(res["order"])
        fill = float(res.get("price") or price)
        sig.status, sig.ticket, sig.lots = "executed", ticket, fin["lots"]
        sig.reason = f"Order filled at {fill:g}"
        s.add(Position(ticket=ticket, signal_id=sig.id, symbol=sym, side=side, lots=fin["lots"], entry=fill, sl=levels["sl"],
                       tp=levels["tp"], risk_usd=fin["risk_usd"], last_price=fill))
        s.commit()
        msg = f"{sym} {side} {fin['lots']:g} lots filled at {fill:g}, stop {levels['sl']}, target {levels['tp']} (#{ticket})"
        log("Order filled: " + msg, "trade", sym, sig.id)
        notify("Order filled: " + msg)
        return "executed"


def _fail(s, sig: Signal, reason: str, status: str = "error") -> str:
    sig.status, sig.reason = status, reason
    s.commit()
    log(f"{sig.symbol}: {reason}", "error" if status == "error" else "warn", sig.symbol, sig.id)
    return status


def approve(signal_id: int) -> str:
    with session() as s:
        sig = s.get(Signal, signal_id)
        if sig is None or sig.status != "pending_approval":
            raise ValueError("this signal is not waiting for approval")
        if sig.expires_at and _aware(sig.expires_at) < utcnow():
            sig.status, sig.reason = "expired", "Approval window passed"
            s.commit()
            raise ValueError("the approval window has passed")
    return execute_signal(signal_id)


def reject(signal_id: int) -> None:
    with session() as s:
        sig = s.get(Signal, signal_id)
        if sig is None or sig.status != "pending_approval":
            raise ValueError("this signal is not waiting for approval")
        sig.status, sig.reason = "rejected", "Rejected by you"
        s.commit()
        log(f"{sig.symbol}: signal rejected by you", "info", sig.symbol, sig.id)


def close_position(ticket: int) -> None:
    cfg = get_settings()
    broker = get_broker()
    broker.connect()
    broker.close_position(ticket, cfg.magic, cfg.deviation)
    log(f"Close sent for #{ticket}", "trade")
    reconcile()


# ---------------------------------------------------------------- reconcile and kill switch
def reconcile() -> None:
    """Sync open positions with the broker, book closed trades, and trip the kill switch when limits are hit."""
    cfg = get_settings()
    broker = get_broker()
    try:
        broker.connect()
        live = {p["ticket"]: p for p in broker.positions(cfg.magic)}
        account = broker.account()
    except BrokerError:
        return
    events, streak_now = [], int(kv_get("loss_streak", 0) or 0)
    with session() as s:
        for pos in s.scalars(select(Position).where(Position.status == "open")).all():
            lp = live.get(pos.ticket)
            if lp:
                pos.floating, pos.last_price = float(lp["profit"] + lp.get("swap", 0.0)), float(lp["price_current"])
                continue
            deals = broker.deals_for_position(pos.ticket)
            out = [d for d in deals if d["entry"] in (1, 2, 3)]
            profit = sum(d["profit"] + d["commission"] + d["swap"] + d.get("fee", 0.0) for d in deals)
            pos.status, pos.closed_at, pos.profit, pos.floating = "closed", utcnow(), float(profit), 0.0
            pos.close_price = float(out[-1]["price"]) if out else pos.last_price
            streak_now = streak_now + 1 if profit < 0 else 0
            events.append((f"{pos.symbol} #{pos.ticket} closed at {pos.close_price:g}, result {profit:+.2f}", pos.symbol, pos.signal_id))
        # expire stale approvals
        for sig in s.scalars(select(Signal).where(Signal.status == "pending_approval")).all():
            if sig.expires_at and _aware(sig.expires_at) < utcnow():
                sig.status, sig.reason = "expired", "Approval window passed"
        s.commit()
    # write to the journal only after the transaction above is closed
    if events:
        kv_set("loss_streak", streak_now)
    for text, sym, sid in events:
        log(text, "trade", sym, sid)
        notify(text)
    if events and streak_now >= cfg.max_loss_streak:
        halt(f"{streak_now} losses in a row")
    start_eq = day_start_equity(account)
    if start_eq and (account["equity"] - start_eq) / start_eq * 100 <= -cfg.daily_loss_cap_pct and not halted()["on"]:
        halt(f"daily loss cap of {cfg.daily_loss_cap_pct:g}% reached")


# ---------------------------------------------------------------- state for the UI
def state() -> dict:
    cfg = get_settings()
    broker = get_broker()
    acct, bridge, acct_err = None, False, ""
    try:
        broker.connect()
        acct = broker.account()
        bridge = True
    except BrokerError as e:
        acct_err = str(e)
    positions = open_positions_view()
    start_eq = day_start_equity(acct) if acct else 0.0
    open_risk = sum(p["risk_usd"] for p in positions)
    streak = int(kv_get("loss_streak", 0) or 0)
    from .llm import usage

    now = utcnow()
    nxt = now.replace(minute=0, second=cfg.cycle_delay_seconds, microsecond=0)
    if nxt <= now:
        nxt += timedelta(hours=1)
    ages = {n: collectors.snapshot_age_min(n) for n in ("macro", "cot", "calendar", "news_run")}
    return {
        "now": iso(now), "mode": get_mode(), "modes": MODES, "allow_live": cfg.allow_live, "halted": halted(),
        "mock": cfg.mt5_mock, "bridge": {"ok": bridge, "error": acct_err},
        "account": None if not acct else {
            "login": acct["login"], "server": acct["server"], "demo": account_is_demo(acct), "currency": acct["currency"],
            "balance": acct["balance"], "equity": acct["equity"], "day_start_equity": start_eq,
            "day_pnl": acct["equity"] - start_eq, "day_pnl_pct": ((acct["equity"] - start_eq) / start_eq * 100) if start_eq else 0.0},
        "risk": {"open_risk_usd": open_risk, "open_risk_pct": (open_risk / acct["balance"] * 100) if acct and acct["balance"] else 0.0,
                 "open_risk_cap_pct": cfg.max_open * cfg.risk_per_trade_pct, "daily_loss_cap_pct": cfg.daily_loss_cap_pct,
                 "open_count": len(positions), "max_open": cfg.max_open, "loss_streak": streak, "max_loss_streak": cfg.max_loss_streak,
                 "risk_per_trade_pct": cfg.risk_per_trade_pct},
        "next_cycle": iso(nxt), "market_open": market_open(),
        "usage": {"claude": usage("claude"), "claude_cap": cfg.claude_max_calls_per_day,
                  "freellm": usage("freellm"), "freellm_cap": cfg.freellmapi_max_calls_per_day},
        "feeds": {"ages_min": ages, "fred_key": bool(cfg.fred_api_key), "claude_key": bool(cfg.anthropic_api_key),
                  "freellm_url": bool(cfg.freellmapi_base_url)},
        "events": collectors.upcoming_events(),
        "symbols": cfg.symbol_list,
        "thresholds": {"score": cfg.score_threshold, "agree": cfg.min_agree, "min_rr": cfg.min_rr},
    }
