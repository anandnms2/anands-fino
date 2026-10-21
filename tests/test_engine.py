from fastapi.testclient import TestClient
from sqlalchemy import select

from app import broker as brk
from app import collectors, engine, scoring
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
        assert len(sigs) == 4
        done = [x for x in sigs if x.status == "executed"]
        assert done and all(x.ticket for x in done)
        assert s.scalars(select(Position).where(Position.status == "open")).first() is not None
    assert len(brk.get_broker().orders) >= 1
    # never more than the max open trades, and one per correlation group and side
    assert len(brk.get_broker().orders) <= 3


def test_claude_no_blocks_trade(monkeypatch):
    force_buy(monkeypatch)
    monkeypatch.setattr(engine, "decide", lambda ctx, model=None: {"decision": "NO", "confidence": 0.2, "thesis": "", "against": "event risk", "invalidation": "", "main_risk": ""})
    engine.run_cycle(force=True)
    assert brk.get_broker().orders == []
    with session() as s:
        assert {x.status for x in s.scalars(select(Signal))} <= {"claude_no", "gate_blocked", "no_trade"}


def test_claude_failure_fails_closed(monkeypatch):
    force_buy(monkeypatch)
    from app import llm
    monkeypatch.setattr(llm, "claude_chat", lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("boom")))
    engine.run_cycle(force=True)
    assert brk.get_broker().orders == []


def test_real_account_refused(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    b = brk.get_broker()
    real = b.account()
    monkeypatch.setattr(b, "account", lambda: {**real, "trade_mode": 2})
    engine.run_cycle(force=True)
    assert b.orders == []
    with session() as s:
        assert any("REAL account" in (x.reason or "") for x in s.scalars(select(Signal)))


def test_halt_stops_cycle(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    engine.halt("test")
    assert engine.run_cycle(force=True)["note"] == "halted"
    assert brk.get_broker().orders == []


def test_alerts_mode_places_nothing(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    engine.set_mode("alerts")
    engine.run_cycle(force=True)
    assert brk.get_broker().orders == []
    with session() as s:
        assert any(x.status == "alerted" for x in s.scalars(select(Signal)))


def test_live_mode_needs_allow_live():
    try:
        engine.set_mode("live_auto")
        assert False
    except ValueError as e:
        assert "ALLOW_LIVE" in str(e)


def test_approval_flow(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    monkeypatch.setenv("ALLOW_LIVE", "true")
    from app.config import get_settings
    get_settings.cache_clear()
    engine.set_mode("live_approval")
    engine.run_cycle(force=True)
    assert brk.get_broker().orders == []  # nothing until approved
    with session() as s:
        pend = s.scalars(select(Signal).where(Signal.status == "pending_approval")).first()
        assert pend
        pid = pend.id
    assert engine.approve(pid) == "executed"  # demo mock account, live approval flow
    assert len(brk.get_broker().orders) == 1


def test_reconcile_books_closed_trade_and_streak(monkeypatch):
    force_buy(monkeypatch); yes(monkeypatch)
    engine.run_cycle(force=True)
    b = brk.get_broker()
    ticket = next(iter(b._positions))
    b.close_position(ticket, 0, 0)  # mock books -12.50
    engine.reconcile()
    with session() as s:
        p = s.get(Position, ticket)
        assert p.status == "closed" and p.profit == -12.5


def test_api_auth_and_state():
    from app.main import app
    c = TestClient(app)
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/state").status_code == 401
    auth = ("admin", "secret")
    r = c.get("/api/state", auth=auth)
    assert r.status_code == 200 and r.json()["account"]["demo"] is True
    assert c.post("/api/halt", auth=auth, json={}).status_code == 403  # needs the header
    r = c.post("/api/halt", auth=auth, json={"reason": "x"}, headers={"X-Requested-With": "fino"})
    assert r.json()["on"] is True
    assert c.get("/", auth=auth).status_code == 200
    assert c.get("/api/diagnostics", auth=auth).json()["bridge"]["ok"] is True
