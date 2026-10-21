"""The trading cycle (one per candle), order execution, reconciliation and the kill switch."""
import threading
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from . import collectors, scoring
from .broker import ACCOUNT_TRADE_MODE_DEMO, BrokerError, get_broker
from . import venues as V
from .config import Settings, get_settings, tf_label
from .db import NewsItem, Position, Signal, iso, kv_get, kv_set, log, session, utcnow
from .decision import decide
from .features import compute_technicals
from .gate import evaluate

_lock = threading.Lock()
MIN_CANDLES = 260


# ---------------------------------------------------------------- small state helpers
def halted() -> dict:
    return kv_get("halted") or {"on": False, "reason": "", "since": None}


def halt(reason: str, venue: str | None = None) -> None:
    """Stop everything (kill switch) or, with a venue, pause only that venue."""
    if venue:
        V.set_halt(venue, reason)
        log(f"{V.LABEL[venue].upper()} VENUE PAUSED: {reason}", "error")
        notify(f"Anands Fino paused the {venue} venue: {reason}")
        return
    kv_set("halted", {"on": True, "reason": reason, "since": iso(utcnow())})
    log(f"TRADING HALTED: {reason}", "error")
    notify(f"Anands Fino halted: {reason}")


def resume() -> None:
    kv_set("halted", {"on": False, "reason": "", "since": None})
    for v in V.VENUES:
        V.set_halt(v, None)
        V.set_loss_streak(v, 0)
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


def open_positions_view(venue: str | None = None) -> list[dict]:
    with session() as s:
        q = select(Position).where(Position.status == "open")
        if venue:
            q = q.where(Position.venue == venue)
        return [{"ticket": p.ticket, "symbol": p.symbol, "side": p.side, "lots": p.lots, "entry": p.entry, "sl": p.sl,
                 "tp": p.tp, "risk_usd": p.risk_usd, "floating": p.floating, "last_price": p.last_price,
                 "opened_at": iso(p.opened_at), "signal_id": p.signal_id, "venue": p.venue} for p in s.scalars(q).all()]


def profit_rate(broker, info: dict, account: dict) -> float | None:
    """Value of one unit of the symbol's profit currency in the account currency (None when it cannot be priced)."""
    pc, ac = info.get("currency_profit"), account.get("currency")
    if not pc or not ac or pc == ac:
        return 1.0
    cfg = get_settings()
    for pair, inv in ((pc + ac, False), (ac + pc, True)):
        try:
            t = broker.tick(cfg.broker_symbol(pair))
        except BrokerError:
            continue
        mid = (t["bid"] + t["ask"]) / 2
        if mid > 0:
            return 1.0 / mid if inv else mid
    return None


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


def _venue_ready(v: str):
    """Connect a venue and check its account type. Returns (broker, account) or None, logging why not."""
    try:
        b = get_broker(v)
        b.connect()
        acct = b.account()
    except BrokerError as e:
        log(f"{V.LABEL[v]} venue skipped: {e}", "error")
        return None
    if v == "live" and account_is_demo(acct):
        log("Live venue skipped: the connected account is a DEMO account", "error")
        return None
    if v != "live" and not account_is_demo(acct):
        log(f"{V.LABEL[v]} venue skipped: the connected account is a REAL account", "error")
        return None
    return b, acct


def _run_cycle(force: bool) -> dict:
    cfg = get_settings()
    cycle_id = utcnow().strftime("%Y%m%d%H") + "-" + uuid.uuid4().hex[:4]
    if halted()["on"]:
        log(f"Cycle skipped: halted ({halted()['reason']})", "warn")
        return {"ok": False, "note": "halted"}
    if not force and not market_open():
        log("Cycle skipped: forex market is closed", "info")
        return {"ok": False, "note": "market closed"}

    # venues that may trade this cycle, each with its own broker, account and book
    vctx = {}
    for v in V.enabled():
        if V.venue_halt(v)["on"]:
            log(f"{V.LABEL[v]} venue is paused: {V.venue_halt(v)['reason']}", "warn")
            continue
        r = _venue_ready(v)
        if r:
            b, acct = r
            vctx[v] = {"broker": b, "account": acct, "start_eq": V.day_start_equity(v, acct),
                       "positions": open_positions_view(v), "auto": V.venue_cfg(v)["auto"]}

    # one set of candles for the analysis: a trading venue's broker if there is one, else demo or the paper feed
    order = [v for v in ("demo", "live", "paper") if v in vctx] or ["demo", "paper"]
    broker, data_v = None, None
    for v in order:
        try:
            b = vctx[v]["broker"] if v in vctx else get_broker(v)
            b.connect()
            sync_offset(b, cfg)
            b.account()
            broker, data_v = b, v
            break
        except BrokerError as e:
            log(f"Price source {V.LABEL[v]} unavailable: {e}", "warn")
    if broker is None:
        log("Cycle aborted: no price source is reachable", "error")
        return {"ok": False, "note": "no price source"}

    macro = (collectors.snapshot("macro") or {}).get("data")
    cot = (collectors.snapshot("cot") or {}).get("data")
    if utcnow().minute < cfg.timeframe_min:
        prune()

    # 1. technicals for every symbol
    techs, infos, ticks, closes, candle_times, stale = {}, {}, {}, {}, {}, {}
    for sym in cfg.symbol_list:
        bsym = cfg.broker_symbol(sym)
        try:
            info = broker.symbol_info(bsym)
            df = broker.rates(bsym, 500 if cfg.timeframe_min >= 30 else 1500)
            tick = broker.tick(bsym)
        except BrokerError as e:
            log(f"{sym}: {e}", "error", sym)
            continue
        closed = df.iloc[:-1].reset_index(drop=True)
        if len(closed) < MIN_CANDLES:
            log(f"{sym}: only {len(closed)} candles, need {MIN_CANDLES}", "warn", sym)
            continue
        last_open = closed["time"].iloc[-1]
        age_min = (utcnow() - last_open.to_pydatetime()).total_seconds() / 60
        stale[sym] = (not force) and age_min > 4 * cfg.timeframe_min + 2
        techs[sym] = compute_technicals(closed, info["digits"], cfg.timeframe_min)
        infos[sym], ticks[sym] = info, tick
        closes[sym] = [round(float(x), info["digits"]) for x in closed["close"].tail(48)]
        candle_times[sym] = iso(last_open.to_pydatetime())

    since = utcnow() - timedelta(hours=24)
    with session() as s:
        news24 = list(s.scalars(select(NewsItem).where(NewsItem.published >= since)))
        cb_items = [{"source": n.source, "title": n.title, "age_h": round((utcnow() - _aware(n.published)).total_seconds() / 3600, 1)}
                    for n in s.scalars(select(NewsItem).where(NewsItem.central_bank == 1, NewsItem.published >= utcnow() - timedelta(hours=48)).order_by(NewsItem.published.desc()).limit(6))]

    results = {}
    prep = {}
    for sym, tech in techs.items():
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
        prep[sym] = (factors, scoring.combine(factors, weights, cfg.score_threshold, cfg.min_agree, cfg.min_agree_pct or None))

    # strongest setups first, so the open-trade slots go to the best of the 13 rather than the first in the list
    for sym in sorted(prep, key=lambda s: -abs(prep[s][1]["score"])):
        tech, info, tick = techs[sym], infos[sym], ticks[sym]
        factors, comb = prep[sym]

        def mk(venue: str = "") -> Signal:
            return Signal(cycle_id=cycle_id, symbol=sym, timeframe=tf_label(), candle_time=candle_times[sym], bias=comb["bias"], score=comb["score"],
                          agree=comb["agree"], available=comb["available"], factors=factors, venue=venue,
                          mode=(f"{venue}:{'auto' if vctx[venue]['auto'] else 'manual'}" if venue in vctx else ""),
                          context={"closes": closes[sym]})

        sig = mk()
        if comb["bias"] == "NONE":
            why = []
            if abs(comb["score"]) < cfg.score_threshold:
                why.append(f"score {comb['score']:+.1f} is inside the ±{cfg.score_threshold:g} threshold")
            pct = comb["agree"] / comb["available"] * 100 if comb["available"] else 0
            if cfg.min_agree_pct and pct < cfg.min_agree_pct - 1e-9 or comb["available"] < 3:
                why.append(f"{comb['agree']} of {comb['available']} factors agree ({pct:.0f}%), {cfg.min_agree_pct:g}% needed")
            elif not cfg.min_agree_pct and comb["agree"] < cfg.min_agree:
                why.append(f"{comb['agree']} factors agree, {cfg.min_agree} needed")
            sig.status, sig.reason = "no_trade", "No trade: " + " and ".join(why)
            sig.levels = scoring.build_levels("BUY" if comb["score"] >= 0 else "SELL", tech, tick, info["digits"], cfg.stop_atr_mult, cfg.rr_target)
            sid = _save(sig)
            results[sym] = sig.status
            continue

        levels = scoring.build_levels(comb["bias"], tech, tick, info["digits"], cfg.stop_atr_mult, cfg.rr_target)
        sig.levels = levels
        if not vctx:
            sig.status, sig.reason = "alerted", "No trading venue is switched on: signal recorded, no order"
            sid = _save(sig)
            text = f"{sym} {comb['bias']} entry {levels['entry']}, stop {levels['sl']}, target {levels['tp']}"
            log(f"Signal (no venue on): {text}", "trade", sym, sid)
            notify(f"Signal: {text}")
            results[sym] = sig.status
            continue

        blk = collectors.blackout(sym)
        data_ok = not stale.get(sym, False)
        gate_args, passed, outcome, vlevels = {}, [], [], {}
        for v, c in vctx.items():
            try:
                vb = c["broker"]
                vinfo, vtick = vb.symbol_info(cfg.broker_symbol(sym)), vb.tick(cfg.broker_symbol(sym))
            except BrokerError as e:
                log(f"{sym} on {V.LABEL[v]}: {e}", "error", sym)
                continue
            # venues quote slightly different prices (Yahoo vs cTrader, say): shift the levels onto this venue's own price
            side_px = lambda tk: tk["ask"] if comb["bias"] == "BUY" else tk["bid"]  # noqa: E731
            delta = side_px(vtick) - side_px(tick)
            if abs(delta) > 0.5 * levels["atr"]:
                vs = mk(v)
                vs.levels = levels
                vs.status, vs.reason = "gate_blocked", f"Blocked: {V.LABEL[v]} quotes {delta:+g} away from the analysis price (more than 0.5 ATR)"
                sid = _save(vs)
                log(f"{sym} on {V.LABEL[v]}: {vs.reason}", "warn", sym, sid)
                outcome.append(f"{v}:gate_blocked")
                continue
            d = vinfo["digits"]
            vlev = {**levels, "entry": round(levels["entry"] + delta, d), "sl": round(levels["sl"] + delta, d), "tp": round(levels["tp"] + delta, d)}
            vlevels[v] = vlev
            gate_args[v] = dict(cfg=cfg, symbol=sym, bias=comb["bias"], levels=vlev, account=c["account"], sym_info=vinfo,
                                spread_price=vtick["ask"] - vtick["bid"], open_positions=c["positions"], day_start_equity=c["start_eq"],
                                loss_streak=V.loss_streak(v), halted=False, blackout=blk, data_ok=data_ok,
                                profit_rate=profit_rate(vb, vinfo, c["account"]))
            pre = evaluate(confidence=None, **gate_args[v])
            if pre["ok"]:
                passed.append(v)
            else:
                vs = mk(v)
                vs.levels, vs.gate = vlevels[v], pre
                vs.status, vs.reason = "gate_blocked", "Blocked by the risk gate: " + pre["reason"]
                sid = _save(vs)
                log(f"{sym} on {V.LABEL[v]}: {comb['bias']} score {comb['score']:+.0f} blocked. {pre['reason']}", "warn", sym, sid)
                outcome.append(f"{v}:gate_blocked")
        if not passed:
            results[sym] = ", ".join(outcome) or "no venue reachable"
            continue

        # do not pester Claude: after a NO on this symbol and direction, wait out the cooldown
        with session() as s:
            recent_no = s.scalars(select(Signal).where(Signal.symbol == sym, Signal.bias == comb["bias"], Signal.status == "claude_no",
                                                       Signal.ts >= utcnow() - timedelta(minutes=cfg.claude_cooldown_min))).first()
        if recent_no is not None:
            results[sym] = "claude_cooldown"
            continue

        # Claude has the final say, once per symbol, and every venue shares the answer
        v0 = passed[0]
        ctx = _claude_context(sym, comb, factors, levels, tech, closes[sym], news24, cb_items, macro, cot, vctx[v0]["positions"],
                              vctx[v0]["account"], vctx[v0]["start_eq"], info)
        claude_input = {k: v for k, v in ctx.items() if k != "recent_closes"}
        dec = decide(ctx)
        if dec["decision"] != "YES":
            sig.claude, sig.context = dec, {**sig.context, "claude_input": claude_input}
            sig.status = "claude_no"
            sig.reason = "Claude said no" + (f": {dec['against']}" if dec.get("against") else f" ({dec['error']})" if dec.get("error") else "")
            sid = _save(sig)
            log(f"{sym}: Claude declined {comb['bias']}. {dec.get('against') or dec.get('error', '')}", "info", sym, sid)
            results[sym] = sig.status
            continue
        for v in passed:
            vs = mk(v)
            vs.levels, vs.claude = vlevels[v], dec
            vs.context = {**vs.context, "claude_input": claude_input}
            fin = evaluate(confidence=dec["confidence"], **gate_args[v])
            vs.gate = fin
            if not fin["ok"]:
                vs.status, vs.reason = "gate_blocked", "Blocked by the risk gate: " + fin["reason"]
                sid = _save(vs)
                log(f"{sym} on {V.LABEL[v]}: Claude said yes at {dec['confidence']:.2f} but the gate blocked it. {fin['reason']}", "warn", sym, sid)
                outcome.append(f"{v}:gate_blocked")
                continue
            vs.lots = fin["lots"]
            sid = _save(vs)
            outcome.append(f"{v}:{_dispatch(sid)}")
            vctx[v]["positions"] = open_positions_view(v)
        results[sym] = ", ".join(outcome)
    return {"ok": True, "cycle_id": cycle_id, "results": results}


def prune() -> None:
    """Keep the database small now that a cycle runs every few minutes: drop old no-trade rows and old journal lines."""
    from sqlalchemy import delete

    from .db import Activity

    with session() as s:
        s.execute(delete(Signal).where(Signal.status == "no_trade", Signal.ts < utcnow() - timedelta(days=3)))
        s.execute(delete(Activity).where(Activity.ts < utcnow() - timedelta(days=14)))
        s.commit()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _save(sig: Signal) -> int:
    with session() as s:
        s.add(sig) if sig.id is None else s.merge(sig)
        s.commit()
        return sig.id


def _claude_context(sym, comb, factors, levels, tech, closes, news24, cb_items, macro, cot, positions, account, start_eq, info) -> dict:
    heads = [{"title": n.title, "effect": (n.tags or {}).get(sym, 0)} for n in sorted(news24, key=lambda x: x.published, reverse=True)
             if n.tags is not None and (n.tags or {}).get(sym, 0) != 0][:12]
    return {
        "symbol": sym, "timeframe": tf_label(), "proposed_side": comb["bias"],
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
def _dispatch(signal_id: int) -> str:
    """Send a cleared signal to its venue: straight to the broker (auto-approve) or into the approval queue."""
    cfg = get_settings()
    with session() as s:
        sig = s.get(Signal, signal_id)
        venue, side, sym = sig.venue, sig.bias, sig.symbol
        text = f"[{V.LABEL[venue]}] {sym} {side} {sig.lots:g} lots, entry {sig.levels['entry']}, stop {sig.levels['sl']}, target {sig.levels['tp']}"
        if not V.venue_cfg(venue)["auto"]:
            sig.status = "pending_approval"
            sig.expires_at = utcnow() + timedelta(minutes=cfg.approval_ttl_min)
            sig.reason = "Waiting for your approval"
            s.commit()
            log(f"Approval needed: {text}", "trade", sym, signal_id)
            notify(f"Approval needed: {text}", {"signal_id": signal_id})
            return sig.status
    return execute_signal(signal_id)


def execute_signal(signal_id: int) -> str:
    """Final checks and the market order on the signal's venue. Demo and Paper refuse a real account; Live refuses a demo one."""
    cfg = get_settings()
    with session() as s:
        sig = s.get(Signal, signal_id)
        if sig is None or sig.status in ("executed", "rejected", "expired"):
            return "ignored"
        venue = sig.venue or "demo"
        sym, side, levels = sig.symbol, sig.bias, dict(sig.levels)
        broker = get_broker(venue)
        try:
            broker.connect()
            account = broker.account()
            bsym = cfg.broker_symbol(sym)
            info = broker.symbol_info(bsym)
            tick = broker.tick(bsym)
        except BrokerError as e:
            return _fail(s, sig, f"broker unavailable: {e}")

        real = not account_is_demo(account)
        if venue == "live" and not V.allow_live():
            return _fail(s, sig, "refused: live trading is not enabled (ALLOW_LIVE)")
        if venue == "live" and not real:
            return _fail(s, sig, "refused: the Live venue is connected to a DEMO account")
        if venue != "live" and real:
            return _fail(s, sig, f"refused: the connected account is a REAL account but this is the {V.LABEL[venue]} venue")

        # re-check against the price right now
        price = tick["ask"] if side == "BUY" else tick["bid"]
        moved = abs(price - levels["entry"])
        if moved > 0.25 * levels["atr"]:
            return _fail(s, sig, f"price moved {moved:g} since the signal (more than 0.25 ATR)")
        live_levels = {**levels, "entry": price}
        fin = evaluate(cfg=cfg, symbol=sym, bias=side, levels=live_levels, confidence=(sig.claude or {}).get("confidence", 0.0),
                       account=account, sym_info=info, spread_price=tick["ask"] - tick["bid"], open_positions=open_positions_view(venue),
                       day_start_equity=V.day_start_equity(venue, account), loss_streak=V.loss_streak(venue),
                       halted=halted()["on"] or V.venue_halt(venue)["on"], blackout=collectors.blackout(sym), data_ok=True,
                       profit_rate=profit_rate(broker, info, account))
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
        sig.reason = f"Order filled at {fill:g} ({V.LABEL[venue]})"
        s.add(Position(ticket=ticket, signal_id=sig.id, symbol=sym, side=side, lots=fin["lots"], entry=fill, sl=levels["sl"],
                       tp=levels["tp"], risk_usd=fin["risk_usd"], last_price=fill, venue=venue))
        s.commit()
        msg = f"[{V.LABEL[venue]}] {sym} {side} {fin['lots']:g} lots filled at {fill:g}, stop {levels['sl']}, target {levels['tp']} (#{ticket})"
        log("Order filled: " + msg, "trade", sym, sig.id)
        notify("Order filled: " + msg)
        return "executed"


def _fail(s, sig: Signal, reason: str, status: str = "error") -> str:
    sig.status, sig.reason = status, reason
    s.commit()
    log(f"{sig.symbol} ({sig.venue or '-'}): {reason}", "error" if status == "error" else "warn", sig.symbol, sig.id)
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


def close_position(ticket: int, venue: str = "demo") -> None:
    cfg = get_settings()
    broker = get_broker(venue)
    broker.connect()
    broker.close_position(ticket, cfg.magic, cfg.deviation)
    log(f"Close sent for #{ticket} ({V.LABEL[venue]})", "trade")
    reconcile()


# ---------------------------------------------------------------- reconcile and kill switch
def reconcile() -> None:
    """Sync open positions with each venue's broker, book closed trades, and pause a venue when its limits are hit."""
    cfg = get_settings()
    expire_stale()
    for v in V.VENUES:
        if not (V.venue_cfg(v)["on"] or open_positions_view(v)):
            continue
        if v == "live" and not V.allow_live() and not open_positions_view(v):
            continue
        _reconcile_venue(v, cfg)


def expire_stale() -> None:
    with session() as s:
        for sig in s.scalars(select(Signal).where(Signal.status == "pending_approval")).all():
            if sig.expires_at and _aware(sig.expires_at) < utcnow():
                sig.status, sig.reason = "expired", "Approval window passed"
        s.commit()


def _reconcile_venue(v: str, cfg: Settings) -> None:
    broker = get_broker(v)
    try:
        broker.connect()
        live = {p["ticket"]: p for p in broker.positions(cfg.magic)}
        account = broker.account()
    except BrokerError:
        return
    events, streak_now = [], V.loss_streak(v)
    with session() as s:
        for pos in s.scalars(select(Position).where(Position.status == "open", Position.venue == v)).all():
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
            events.append((f"[{V.LABEL[v]}] {pos.symbol} #{pos.ticket} closed at {pos.close_price:g}, result {profit:+.2f}", pos.symbol, pos.signal_id))
        s.commit()
    # write to the journal only after the transaction above is closed
    if events:
        V.set_loss_streak(v, streak_now)
    for text, sym, sid in events:
        log(text, "trade", sym, sid)
        notify(text)
    if events and streak_now >= cfg.max_loss_streak and not V.venue_halt(v)["on"]:
        halt(f"{streak_now} losses in a row", v)
    start_eq = V.day_start_equity(v, account)
    if start_eq and (account["equity"] - start_eq) / start_eq * 100 <= -cfg.daily_loss_cap_pct and not V.venue_halt(v)["on"]:
        halt(f"daily loss cap of {cfg.daily_loss_cap_pct:g}% reached", v)


# ---------------------------------------------------------------- state for the UI
def venue_state(v: str) -> dict:
    cfg = get_settings()
    vc = V.venue_cfg(v)
    positions = open_positions_view(v)
    out = {"name": v, "label": V.LABEL[v], "on": vc["on"], "auto": vc["auto"], "locked": V.locked(v),
           "halt": V.venue_halt(v), "account": None, "error": "", "connected": None,
           "loss_streak": V.loss_streak(v), "open_count": len(positions),
           "open_risk_usd": sum(p["risk_usd"] for p in positions), "positions": positions,
           "floating": sum(p["floating"] for p in positions)}
    if V.locked(v) and not positions:
        return out
    if not vc["on"] and not positions:
        return out
    try:
        b = get_broker(v)
        b.connect()
        acct = b.account()
        out["connected"] = True
        start_eq = V.day_start_equity(v, acct)
        out["account"] = {"login": acct["login"], "server": acct["server"], "demo": account_is_demo(acct), "currency": acct["currency"],
                          "balance": acct["balance"], "equity": acct["equity"], "day_start_equity": start_eq,
                          "day_pnl": acct["equity"] - start_eq,
                          "day_pnl_pct": ((acct["equity"] - start_eq) / start_eq * 100) if start_eq else 0.0}
        out["open_risk_pct"] = (out["open_risk_usd"] / acct["balance"] * 100) if acct["balance"] else 0.0
    except BrokerError as e:
        out["connected"], out["error"] = False, str(e)
    return out


def state() -> dict:
    cfg = get_settings()
    from .llm import usage

    now = utcnow()
    tf = max(1, min(60, cfg.timeframe_min))
    nxt = now.replace(minute=(now.minute // tf) * tf, second=cfg.cycle_delay_seconds, microsecond=0)
    if nxt <= now:
        nxt += timedelta(minutes=tf)
    ages = {n: collectors.snapshot_age_min(n) for n in ("macro", "cot", "calendar", "news_run")}
    return {
        "now": iso(now), "allow_live": V.allow_live(), "halted": halted(),
        "mock": cfg.mt5_mock, "broker": cfg.broker, "paper_feed": cfg.paper_feed, "timeframe": tf_label(),
        "venues": {v: venue_state(v) for v in V.VENUES},
        "risk": {"daily_loss_cap_pct": cfg.daily_loss_cap_pct, "max_open": cfg.max_open, "max_loss_streak": cfg.max_loss_streak,
                 "risk_per_trade_pct": cfg.risk_per_trade_pct, "open_risk_cap_pct": cfg.max_open * cfg.risk_per_trade_pct},
        "next_cycle": iso(nxt), "market_open": market_open(),
        "usage": {"claude": usage("claude"), "claude_cap": cfg.claude_max_calls_per_day,
                  "freellm": usage("freellm"), "freellm_cap": cfg.freellmapi_max_calls_per_day},
        "feeds": {"ages_min": ages, "fred_key": bool(cfg.fred_api_key), "claude_key": bool(cfg.anthropic_api_key),
                  "freellm_url": bool(cfg.freellmapi_base_url)},
        "events": collectors.upcoming_events(),
        "symbols": cfg.symbol_list,
        "thresholds": {"score": cfg.score_threshold, "agree": cfg.min_agree, "agree_pct": cfg.min_agree_pct, "min_rr": cfg.min_rr},
    }
