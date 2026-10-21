import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import broker as brk
from app import collectors, engine, scoring, venues
from app.config import get_settings
from app.db import Position, Signal, kv_set, session


def force_buy(monkeypatch, bias="BUY"):
    monkeypatch.setattr(scoring, "combine", lambda *a, **k: {"score": 55.0, "agree": 5, "available": 7, "bias": bias})


def yes(monkeypatch, conf=0.8):
    monkeypatch.setattr(engine, "decide", lambda ctx, model=None: {"decision": "YES", "confidence": conf, "thesis": "t", "against": "a", "invalidation": "i", "main_risk": "r"})


def test_cycle_executes_on_demo(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    out = engine.run_cycle(force=True)
    assert out["ok"], out
    with session() as s:
        sigs = s.scalars(select(Signal)).all()
        assert len(sigs) == len(get_settings().symbol_list) == 13
        done = [x for x in sigs if x.status == "executed"]
        assert done and all(x.ticket for x in done)
        assert s.scalars(select(Position).where(Position.status == "open")).first() is not None
    assert len(brk.get_broker("demo").orders) >= 1
    # never more than the max open trades, and one per correlation group and side
    assert len(brk.get_broker("demo").orders) <= 3


def test_claude_no_blocks_trade(monkeypatch):
    force_buy(monkeypatch)
    monkeypatch.setattr(engine, "decide", lambda ctx, model=None: {"decision": "NO", "confidence": 0.2, "thesis": "", "against": "event risk", "invalidation": "", "main_risk": ""})
    engine.run_cycle(force=True)
    assert brk.get_broker("demo").orders == []
    with session() as s:
        assert {x.status for x in s.scalars(select(Signal))} <= {"claude_no", "gate_blocked", "no_trade"}


def test_claude_failure_fails_closed(monkeypatch):
    force_buy(monkeypatch)
    from app import llm
    monkeypatch.setattr(llm, "claude_chat", lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("boom")))
    engine.run_cycle(force=True)
    assert brk.get_broker("demo").orders == []


def test_real_account_refused(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    b = brk.get_broker("demo")
    real = b.account()
    monkeypatch.setattr(b, "account", lambda: {**real, "trade_mode": 2})
    engine.run_cycle(force=True)
    assert b.orders == []
    from app.db import Activity
    with session() as s:
        assert any("REAL account" in (x.text or "") for x in s.scalars(select(Activity)))


def test_halt_stops_cycle(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    engine.halt("test")
    assert engine.run_cycle(force=True)["note"] == "halted"
    assert brk.get_broker("demo").orders == []


def test_no_venue_on_places_nothing(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    venues.set_venue("demo", on=False)
    engine.run_cycle(force=True)
    assert brk.get_broker("demo").orders == []
    with session() as s:
        assert any(x.status == "alerted" for x in s.scalars(select(Signal)))


def test_live_is_locked_without_allow_live():
    for kw in ({"on": True}, {"auto": True}):
        with pytest.raises(ValueError, match="ALLOW_LIVE"):
            venues.set_venue("live", **kw)
    assert venues.venue_cfg("live")["on"] is False


def test_allow_live_switch_from_dashboard():
    from app.main import app
    c = TestClient(app); a = ("admin", "secret"); H = {"X-Requested-With": "fino"}
    assert c.post("/api/venue/live", auth=a, headers=H, json={"on": True}).status_code == 400
    assert c.post("/api/allow-live", auth=a, headers=H, json={"on": True}).json()["allow_live"] is True
    assert c.post("/api/venue/live", auth=a, headers=H, json={"on": True, "auto": True}).json()["on"] is True
    assert c.get("/api/state", auth=a).json()["allow_live"] is True
    c.post("/api/allow-live", auth=a, headers=H, json={"on": False})
    v = venues.venue_cfg("live")
    assert v["on"] is False and v["auto"] is False


def test_manual_approval_flow(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    venues.set_venue("demo", auto=False)
    engine.run_cycle(force=True)
    assert brk.get_broker("demo").orders == []  # nothing until approved
    with session() as s:
        pend = s.scalars(select(Signal).where(Signal.status == "pending_approval")).first()
        assert pend and pend.venue == "demo"
        pid = pend.id
    assert engine.approve(pid) == "executed"
    assert len(brk.get_broker("demo").orders) == 1
    # switching auto-approve back on executes without a click
    venues.set_venue("demo", auto=True)
    engine.run_cycle(force=True)
    assert len(brk.get_broker("demo").orders) >= 2


def test_demo_and_live_run_side_by_side(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    monkeypatch.setenv("ALLOW_LIVE", "true")
    from app.config import get_settings
    get_settings.cache_clear()
    live = brk.get_broker("live")
    real = live.account()
    monkeypatch.setattr(live, "account", lambda: {**real, "trade_mode": 2})
    venues.set_venue("live", on=True, auto=False)  # live waits for a click, demo is automatic
    assert engine.run_cycle(force=True)["ok"]
    assert len(brk.get_broker("demo").orders) >= 1 and live.orders == []
    with session() as s:
        pend = s.scalars(select(Signal).where(Signal.status == "pending_approval", Signal.venue == "live")).first()
        assert pend is not None
        pid = pend.id
    assert engine.approve(pid) == "executed"
    with session() as s:
        assert {p.venue for p in s.scalars(select(Position))} == {"demo", "live"}
    assert len(engine.open_positions_view("live")) == 1


def test_live_venue_refuses_demo_account(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    monkeypatch.setenv("ALLOW_LIVE", "true")
    from app.config import get_settings
    get_settings.cache_clear()
    venues.set_venue("live", on=True, auto=True)
    venues.set_venue("demo", on=False)
    engine.run_cycle(force=True)  # the mock account is a demo account
    assert brk.get_broker("live").orders == []


def test_one_venue_pausing_does_not_stop_the_others(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    from app import paper  # noqa: F401
    venues.set_venue("demo", on=True)
    engine.halt("test pause", "demo")
    assert venues.venue_halt("demo")["on"] and not engine.halted()["on"]
    engine.run_cycle(force=True)
    assert brk.get_broker("demo").orders == []
    engine.resume()
    assert not venues.venue_halt("demo")["on"]


def test_old_database_gets_venue_columns():
    from sqlalchemy import inspect, text
    from app import db
    eng = db.get_engine()
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE positions"))
        conn.execute(text("CREATE TABLE positions (ticket INTEGER PRIMARY KEY, signal_id INTEGER, symbol VARCHAR(16), side VARCHAR(8), lots FLOAT, entry FLOAT, sl FLOAT, tp FLOAT, risk_usd FLOAT, opened_at DATETIME, closed_at DATETIME, close_price FLOAT, profit FLOAT, floating FLOAT, last_price FLOAT, status VARCHAR(8))"))
        conn.execute(text("INSERT INTO positions (ticket, symbol, side, lots, entry, sl, tp, risk_usd, floating, last_price, status) VALUES (5, 'EURUSD', 'BUY', 0.1, 1, 0.9, 1.1, 5, 0, 1, 'open')"))
    db.init_db()
    assert "venue" in {c["name"] for c in inspect(eng).get_columns("positions")}
    with session() as s:
        assert s.get(Position, (5, 'demo')).venue == "demo"


def test_reconcile_books_closed_trade_and_streak(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    engine.run_cycle(force=True)
    b = brk.get_broker("demo")
    ticket = next(iter(b._positions))
    b.close_position(ticket, 0, 0)  # mock books -12.50
    engine.reconcile()
    with session() as s:
        p = s.get(Position, (ticket, 'demo'))
        assert p.status == "closed" and p.profit == -12.5


def test_api_auth_and_state():
    from app.main import app
    c = TestClient(app)
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/state").status_code == 401
    auth = ("admin", "secret")
    r = c.get("/api/state", auth=auth)
    assert r.status_code == 200 and r.json()["venues"]["demo"]["account"]["demo"] is True
    assert c.post("/api/halt", auth=auth, json={}).status_code == 403  # needs the header
    r = c.post("/api/halt", auth=auth, json={"reason": "x"}, headers={"X-Requested-With": "fino"})
    assert r.json()["on"] is True
    assert c.get("/", auth=auth).status_code == 200
    assert c.get("/api/diagnostics", auth=auth).json()["bridge"]["ok"] is True
    H = {"X-Requested-With": "fino"}
    assert c.post("/api/venue/demo", auth=auth, headers=H, json={"auto": False}).json()["auto"] is False
    assert c.post("/api/venue/live", auth=auth, headers=H, json={"on": True}).status_code == 400
    assert c.post("/api/venue/nope", auth=auth, headers=H, json={"on": True}).status_code == 400


def test_report_per_venue(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    engine.run_cycle(force=True)
    b = brk.get_broker("demo")
    for tk in list(b._positions):
        b.close_position(tk, 0, 0)
    engine.reconcile()
    from app.main import app
    r = TestClient(app).get("/api/report", auth=("admin", "secret")).json()
    d = r["venues"]["demo"]
    assert d["trades"] >= 1 and d["net"] < 0 and d["losses"] == d["trades"] and d["win_rate"] == 0
    assert r["venues"]["live"]["trades"] == 0 and d["by_symbol"] and d["recent"]


def test_symbols_editable_from_dashboard(monkeypatch):
    from app.main import app
    monkeypatch.setenv("SYMBOLS", "EURUSD,GBPUSD")  # a stale Coolify value
    get_settings.cache_clear()
    c = TestClient(app); a = ("admin", "secret"); H = {"X-Requested-With": "fino"}
    assert get_settings().symbol_list == ["EURUSD", "GBPUSD"]
    r = c.post("/api/symbols", auth=a, headers=H, json={"symbols": "xauusd, eurusd usdjpy"})
    assert r.json()["symbols"] == ["XAUUSD", "EURUSD", "USDJPY"]
    assert c.get("/api/state", auth=a).json()["symbols_custom"] is True
    assert c.post("/api/symbols", auth=a, headers=H, json={"symbols": "bad!"}).status_code == 400
    assert c.post("/api/symbols", auth=a, headers=H, json={"reset": True}).json()["symbols"] == ["EURUSD", "GBPUSD"]


def test_same_gate_block_is_not_journaled_every_minute(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    monkeypatch.setenv("SPREAD_STOP_LIMIT", "0.0000001")   # every trade fails the spread check
    from app.config import get_settings
    get_settings.cache_clear()
    engine.run_cycle(force=True)
    with session() as s:
        n1 = len(s.scalars(select(Signal).where(Signal.status == "gate_blocked")).all())
    assert n1 >= 1
    engine.run_cycle(force=True)
    with session() as s:
        assert len(s.scalars(select(Signal).where(Signal.status == "gate_blocked")).all()) == n1


def test_strategy_mode_trades_only_proven_setups_and_respects_loss_cooldown(monkeypatch):
    import pandas as pd
    from app import strategies
    from app.db import kv_set

    kv_set("strategy_mode", True)
    force_buy(monkeypatch); yes(monkeypatch)
    # nothing proven yet: no symbol may trade, whatever the score says
    engine.run_cycle(force=True)
    assert brk.get_broker("demo").orders == []
    with session() as s:
        sigs = s.scalars(select(Signal)).all()
        assert sigs and all(x.status == "no_trade" and "proven" in x.reason for x in sigs)
    # one symbol proven, and its strategy fires on the last candle
    kv_set("strategies", {"EURUSD": {"tf": 3, "strategy": "pullback", "session": "all", "stop_k": 1.5, "rr": 1.5, "train": {}, "test": {}}})
    monkeypatch.setattr(strategies, "signal", lambda df, name, session="all": pd.Series(1, index=df.index))
    engine.run_cycle(force=True)
    opened = [o for o in brk.get_broker("demo").orders]
    assert len(opened) == 1
    with session() as s:
        row = s.scalars(select(Signal).where(Signal.status == "executed")).one()
        assert row.symbol == "EURUSD" and row.levels["strategy"] == "pullback" and abs(row.levels["stop"] / row.levels["atr"] - 1.5) < 0.05
        pos = s.scalars(select(Position).where(Position.symbol == "EURUSD")).one()
        pos.status, pos.profit, pos.closed_at = "closed", -5.0, engine.utcnow()   # it lost
        s.commit()
    n = len(brk.get_broker("demo").orders)
    engine.run_cycle(force=True)
    assert len(brk.get_broker("demo").orders) == n      # the same symbol and direction is sat out after the loss
    kv_set("strategy_mode", False)


def test_strategy_trades_keep_their_tested_exits():
    from app.db import Position, Signal, session, utcnow

    with session() as s:
        a = Signal(symbol="EURUSD", bias="BUY", status="executed", venue="demo", levels={"strategy": "pullback", "entry": 1, "sl": 0.9, "tp": 1.2})
        b = Signal(symbol="EURUSD", bias="BUY", status="executed", venue="demo", levels={"entry": 1, "sl": 0.9, "tp": 1.2})
        s.add_all([a, b]); s.commit()
        ia, ib = a.id, b.id
    assert engine.is_strategy_trade(ia) and not engine.is_strategy_trade(ib) and not engine.is_strategy_trade(None)
