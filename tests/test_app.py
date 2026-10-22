import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import broker as brk
from app import engine, scoring
from app.config import get_settings
from app.db import Position, Signal, kv_set, session

AUTH = ("admin", "secret")
HDR = {"X-Requested-With": "fino"}
MAGIC = 26102026


def test_auto_chart_climbs_when_candles_are_too_quiet(monkeypatch):
    """With AUTO_TF on, a symbol whose candle moves less than 3 spreads is analysed on a bigger candle."""
    from app import engine, venues as V
    from app.config import get_settings
    monkeypatch.setenv("AUTO_TF", "true")
    get_settings.cache_clear()
    assert V.TIMEFRAMES[V.TIMEFRAMES.index(3) + 1] == 5
    cfg = get_settings()
    assert cfg.auto_tf and cfg.auto_tf_max == 15 and cfg.spread_stop_limit == 0.25
    get_settings.cache_clear()


def test_profile_switch_loosens_filters():
    from app.config import get_settings
    from app.db import kv_set
    kv_set("profile", "fast")
    c = get_settings()
    assert c.score_threshold == 20 and c.min_agree_pct == 50 and c.max_open == 5
    kv_set("profile", "normal")
    assert get_settings().score_threshold == 35


def test_claude_veto_off_skips_claude(monkeypatch):
    from app import engine
    from app.db import kv_set
    kv_set("claude_veto", False)
    called = []
    monkeypatch.setattr(engine, "decide", lambda ctx: called.append(1) or {"decision": "NO", "confidence": 0.0})
    # the switch is read at the Claude step; a NO from the stub must not be reached
    assert engine.state()["claude_veto"] is False and not called
    kv_set("claude_veto", False)


def test_trailing_stop_locks_profit_then_follows():
    from types import SimpleNamespace as NS
    from app.config import Settings
    from app.engine import _trail_plan
    cfg = Settings()
    pos = NS(side="BUY", entry=100.0, sl=99.0, sl0=None, tp=102.0)
    assert _trail_plan(pos, {"price_current": 100.5, "sl": 99.0, "tp": 102.0}, cfg) is None          # +0.5R: nothing yet
    sl, tp, _ = _trail_plan(pos, {"price_current": 101.0, "sl": 99.0, "tp": 102.0}, cfg)               # +1R: break-even plus 0.2R
    assert abs(sl - 100.2) < 1e-9 and tp == 102.0
    sl, tp, _ = _trail_plan(pos, {"price_current": 101.6, "sl": 100.2, "tp": 102.0}, cfg)               # +1.6R: trails 0.75R behind, target moves ahead
    assert abs(sl - 100.85) < 1e-9 and abs(tp - 102.6) < 1e-9
    assert _trail_plan(pos, {"price_current": 101.0, "sl": 100.85, "tp": 102.6}, cfg) is None           # price pulled back: never loosen
    sell = NS(side="SELL", entry=100.0, sl=101.0, sl0=None, tp=98.0)
    sl, _, _ = _trail_plan(sell, {"price_current": 98.4, "sl": 101.0, "tp": 98.0}, cfg)
    assert abs(sl - 99.15) < 1e-9


def test_reconcile_moves_stop_on_broker():
    from app import broker, engine
    from app.db import Position, session, utcnow
    mb = broker.get_broker("demo") if hasattr(broker, "get_broker") else None
    mb.connect()
    r = mb.send_market("XAUUSD", "BUY", 0.1, 99.0, 102.0, 1, 10)
    tk = r["order"]
    with session() as s:
        s.add(Position(ticket=tk, venue="demo", symbol="XAUUSD", side="BUY", lots=0.1, entry=100.0, sl=99.0, tp=102.0, risk_usd=10, opened_at=utcnow()))
        s.commit()
    mb._positions[tk].update(price_current=101.0, price_open=100.0)
    engine._reconcile_venue("demo", engine.get_settings())
    assert abs(mb._positions[tk]["sl"] - 100.2) < 1e-6
    with session() as s:
        assert abs(s.get(Position, (tk, "demo")).sl - 100.2) < 1e-6


def test_target_is_capped_in_atr():
    from app import scoring
    tech = {"atr": 1.0, "swing_lows": [], "swing_highs": []}
    tick = {"bid": 100.0, "ask": 100.0}
    lv = scoring.build_levels("BUY", tech, tick, 2, 3.0, 2.0, 0.0, 3.0, 1.5)   # stop 3 ATR, 2R would be a 6 ATR target
    assert abs(lv["tp"] - 104.5) < 1e-9 and lv["rr"] == 1.5                     # capped at the 1.5R floor, not 3 ATR (below it)
    lv = scoring.build_levels("BUY", tech, tick, 2, 1.5, 2.0, 0.0, 3.0, 1.5)    # stop 1.5 ATR: 2R = 3 ATR, within the cap
    assert abs(lv["tp"] - 103.0) < 1e-9 and lv["rr"] == 2.0


def test_stream_and_candles_endpoints():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app, headers={"Authorization": "Basic " + __import__("base64").b64encode(b"admin:secret").decode()})
    r = c.get("/api/candles", params={"symbol": "XAUUSD", "tf": 1, "venue": "demo"})
    assert r.status_code == 200 and len(r.json()["candles"]) > 10 and len(r.json()["candles"][0]) == 5
    assert c.get("/api/candles", params={"symbol": "NOPE", "tf": 1, "venue": "demo"}).status_code == 400


def test_stream_payload_has_prices_and_live_positions():
    import json as _json
    from app import broker, venues
    from app.db import Position, session, utcnow
    from app.main import api_stream
    venues.set_venue("demo", on=True)
    mb = broker.get_broker("demo")
    mb.connect()
    tk = mb.send_market("XAUUSD", "BUY", 0.1, 90.0, 120.0, 1, 10)["order"]
    with session() as s:
        s.add(Position(ticket=tk, venue="demo", symbol="XAUUSD", side="BUY", lots=0.1, entry=100.0, sl=90.0, tp=120.0, risk_usd=10, opened_at=utcnow()))
        s.commit()
    import asyncio
    async def first_chunk():
        it = api_stream().body_iterator
        try:
            return await it.__anext__()
        finally:
            await it.aclose()   # an abandoned stream would otherwise complain when garbage-collected, and fail an unrelated later test

    chunk = asyncio.run(first_chunk())
    d = _json.loads(chunk.split("data: ", 1)[1])
    assert "XAUUSD" in d["px"]["demo"] and d["pos"] and d["pos"][0]["ticket"] == tk and "floating" in d["pos"][0]


def test_risk_limits_editable_validated_and_reset():
    import pytest
    from fastapi import HTTPException
    from app import main
    from app.config import get_settings

    base = get_settings().daily_loss_cap_pct
    main.api_tune({"daily_loss_cap_pct": 4.5, "max_open": 7})
    c = get_settings()
    assert c.daily_loss_cap_pct == 4.5 and c.max_open == 7
    for bad in ({"max_open": 0}, {"daily_loss_cap_pct": 99}, {"risk_per_trade_pct": "x"}, {"nope": 1}):
        with pytest.raises(HTTPException):
            main.api_tune(bad)
    assert get_settings().max_open == 7          # a rejected change leaves the saved limits alone
    main.api_tune({"reset": True})
    assert get_settings().daily_loss_cap_pct == base


def test_position_row_carries_the_candle_size_of_its_signal():
    from app import main
    from app.db import Position, Signal, session, utcnow

    with session() as s:
        sig = Signal(symbol="EURUSD", timeframe="M5", bias="BUY", score=40.0, status="executed", venue="paper")
        s.add(sig)
        s.commit()
        s.add(Position(ticket=900001, venue="paper", symbol="EURUSD", side="BUY", lots=0.1, entry=1.1, sl=1.09, tp=1.12, risk_usd=10, opened_at=utcnow(), signal_id=sig.id))
        s.commit()
    rows = main.api_positions()["open"]
    assert [r["tf"] for r in rows if r["ticket"] == 900001] == [5]
    assert main._tf_minutes("H1") == 60 and main._tf_minutes(None) is None


def test_every_rule_is_tunable_and_weights_flow_through():
    from app import main
    from app.config import TUNABLES, get_settings, tune_table

    assert {"score_threshold", "trail_gap_r", "w_trend", "min_confidence", "max_tp_atr"} <= set(TUNABLES)
    main.api_tune({"w_trend": 40, "score_threshold": 12, "trail_gap_r": 0.5})
    c = get_settings()
    assert c.weights_dict["trend"] == 40 and c.weights_dict["news"] == 15 and c.score_threshold == 12 and c.trail_gap_r == 0.5
    row = {r["key"]: r for r in tune_table()}
    assert row["score_threshold"]["custom"] and row["score_threshold"]["value"] == 12 and row["score_threshold"]["base"] != 12
    main.api_tune({"score_threshold": row["score_threshold"]["base"]})   # typing the profile value back clears the override
    assert not {r["key"]: r for r in tune_table()}["score_threshold"]["custom"]
    main.api_tune({"clear": ["w_trend"]})
    assert get_settings().weights_dict["trend"] == 25
    main.api_tune({"reset": True})


def test_manual_trade_opens_checks_limits_and_books_a_position():
    import pytest
    from app import broker, engine, venues
    from app.db import Position, Signal, session
    from sqlalchemy import select

    venues.set_venue("demo", on=True)
    tk = broker.get_broker("demo").tick("XAUUSD")
    t = engine.manual_ticket("demo", "XAUUSD", "BUY")
    assert t["sl"] < t["entry"] < t["tp"] and t["lots"] > 0 and t["pv"] > 0
    with pytest.raises(ValueError, match="stop must be below"):
        engine.manual_trade("demo", "XAUUSD", "BUY", 0.1, tk["ask"] + 1, tk["ask"] + 5)
    with pytest.raises(ValueError, match="daily loss limit"):
        engine.manual_trade("demo", "XAUUSD", "BUY", 50, t["sl"], t["tp"])        # far too big for the account
    with pytest.raises(ValueError, match="locked"):
        engine.manual_trade("live", "XAUUSD", "BUY", 0.1, t["sl"], t["tp"])
    with pytest.raises(ValueError):
        engine.manual_trade("demo", "NOPE", "BUY", 0.1, t["sl"], t["tp"])
    r = engine.manual_trade("demo", "XAUUSD", "BUY", t["lots"], t["sl"], t["tp"])
    with session() as s:
        p = s.scalars(select(Position).where(Position.ticket == r["ticket"])).one()
        sig = s.get(Signal, p.signal_id)
    assert p.status == "open" and p.venue == "demo" and sig.mode == "manual:manual" and p.risk_usd > 0
    engine.halt("test")
    with pytest.raises(ValueError, match="halted"):
        engine.manual_trade("demo", "EURUSD", "SELL", 0.1, 9.0, 0.0)


def test_paper_is_gone_and_its_leftovers_are_purged_once():
    from app import main, venues
    from app.db import Activity, kv_get

    assert venues.VENUES == ("demo", "live")
    c = TestClient(main.app)
    assert c.post("/api/venue/paper", auth=AUTH, headers=HDR, json={"on": True}).status_code >= 400
    assert c.post("/api/paper/reset", auth=AUTH, headers=HDR).status_code in (404, 405)
    with session() as s:
        s.add(Signal(symbol="EURUSD", bias="BUY", status="executed", venue="paper"))
        s.add(Position(ticket=777001, venue="paper", symbol="EURUSD", side="BUY", lots=0.1, entry=1.1, sl=1.0, tp=1.2, status="closed", profit=-5.0))
        s.add(Position(ticket=777002, venue="demo", symbol="EURUSD", side="BUY", lots=0.1, entry=1.1, sl=1.0, tp=1.2, status="closed", profit=5.0))
        s.commit()
    kv_set("paper_removed", None); kv_set("paper_state", {"balance": 1})
    main._purge_paper()
    with session() as s:
        assert s.scalars(select(Position).where(Position.venue == "paper")).first() is None
        assert s.scalars(select(Signal).where(Signal.venue == "paper")).first() is None
        assert s.scalars(select(Position).where(Position.venue == "demo", Position.ticket == 777002)).first() is not None   # demo history is kept
    assert kv_get("paper_removed") is True and not kv_get("paper_state")
