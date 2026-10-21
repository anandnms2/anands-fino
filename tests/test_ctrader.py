import pytest

from app.broker import BrokerError
from tests.fake_ctrader import FakeCTrader

MAGIC = 26102026


@pytest.fixture
def fake(monkeypatch):
    f = FakeCTrader()
    monkeypatch.setenv("CTRADER_URL", f.url)
    monkeypatch.setenv("CTRADER_CLIENT_ID", "cid")
    monkeypatch.setenv("CTRADER_CLIENT_SECRET", "secret")
    monkeypatch.setenv("CTRADER_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("CTRADER_ENV", "demo")
    from app.config import get_settings

    get_settings.cache_clear()
    yield f
    f.stop()
    get_settings.cache_clear()


@pytest.fixture
def broker(fake):
    from app.ctrader import CTraderBroker

    b = CTraderBroker(timeout=5)
    yield b
    b._drop()


def test_account_picks_demo_and_reads_money(broker, fake):
    a = broker.account()
    assert a["login"] == 9001 and a["trade_mode"] == 0 and a["currency"] == "USD"
    assert a["balance"] == 20000.0 and a["equity"] == 20000.0 and a["leverage"] == 100.0
    # application auth, then account auth for the demo account (555), not the live one (999)
    types = [m["payloadType"] for m in fake.received]
    assert types[:3] == [2100, 2149, 2102]
    assert next(m for m in fake.received if m["payloadType"] == 2102)["payload"]["ctidTraderAccountId"] == 555


def test_live_account_refused_when_env_is_demo_only_account_missing(fake, monkeypatch):
    from app.config import get_settings
    from app.ctrader import CTraderBroker

    monkeypatch.setenv("CTRADER_ACCOUNT_ID", "1111")  # the live account while CTRADER_ENV=demo
    get_settings.cache_clear()
    b = CTraderBroker(timeout=5)
    with pytest.raises(BrokerError, match="is live but CTRADER_ENV is demo"):
        b.account()
    b._drop()


def test_symbol_info_scales_lots(broker):
    i = broker.symbol_info("EURUSD")
    assert i["digits"] == 5 and i["trade_contract_size"] == 100000.0
    assert i["volume_min"] == 0.01 and i["volume_step"] == 0.01 and i["currency_profit"] == "USD"
    g = broker.symbol_info("XAUUSD")
    assert g["trade_contract_size"] == 100.0 and g["volume_min"] == 0.01
    with pytest.raises(BrokerError, match="not found"):
        broker.symbol_info("NOPE")


def test_tick_and_rates(broker):  # fake now returns one bar per requested hour
    t = broker.tick("EURUSD")
    assert t["bid"] == pytest.approx(1.165) and t["ask"] == pytest.approx(1.16512)
    df = broker.rates("EURUSD", 20)
    assert len(df) == 20 and list(df.columns)[:5] == ["time", "open", "high", "low", "close"]
    assert (df["high"] >= df["low"]).all() and df["time"].is_monotonic_increasing and str(df["time"].dt.tz) == "UTC"
    assert df["open"].iloc[-1] > 1.16


def test_market_order_sets_protection_and_returns_position_id(broker, fake):
    res = broker.send_market("EURUSD", "BUY", 0.05, 1.1600, 1.1700, MAGIC, 20)
    assert res["order"] == 7001 and res["price"] == pytest.approx(1.16512)
    order = next(m for m in fake.received if m["payloadType"] == 2106)["payload"]
    assert order["volume"] == 500000 and order["tradeSide"] == 1 and order["label"] == str(MAGIC) and order["orderType"] == 1
    assert "stopLoss" not in order  # absolute stops cannot ride on a market order
    amend = next(m for m in fake.received if m["payloadType"] == 2110)["payload"]
    assert amend["positionId"] == 7001 and amend["stopLoss"] == 1.16 and amend["takeProfit"] == 1.17
    pos = broker.positions(MAGIC)
    assert len(pos) == 1 and pos[0]["ticket"] == 7001 and pos[0]["volume"] == pytest.approx(0.05)
    assert pos[0]["sl"] == 1.16 and pos[0]["tp"] == 1.17 and pos[0]["profit"] == 12.5 and pos[0]["type"] == 0
    assert pos[0]["symbol"] == "EURUSD" and pos[0]["price_current"] == pytest.approx(1.165)
    assert broker.positions(12345) == []  # other labels are not ours


def test_close_and_deals(broker, fake):
    broker.send_market("EURUSD", "SELL", 0.02, 1.17, 1.16, MAGIC, 20)
    r = broker.close_position(7001, MAGIC, 20)
    assert r["price"] == pytest.approx(1.166)
    assert broker.positions(MAGIC) == []
    deals = broker.deals_for_position(7001)
    out = [d for d in deals if d["entry"] == 1]
    assert len(deals) == 2 and len(out) == 1 and out[0]["profit"] == 25.0 and out[0]["swap"] == -0.1
    assert sum(d["profit"] + d["commission"] + d["swap"] for d in deals) == pytest.approx(25.0 - 3.5 - 3.5 - 0.1)


def test_rejected_order_raises(fake, monkeypatch):
    fake.reject_order = True
    from app.ctrader import CTraderBroker

    b = CTraderBroker(timeout=5)
    with pytest.raises(BrokerError, match="NOT_ENOUGH_MONEY"):
        b.send_market("EURUSD", "BUY", 0.05, 1.16, 1.17, MAGIC, 20)
    b._drop()


def test_failed_stop_closes_the_position_again(fake):
    fake.fail_amend = True
    from app.ctrader import CTraderBroker

    b = CTraderBroker(timeout=5)
    with pytest.raises(BrokerError, match="position was closed again"):
        b.send_market("EURUSD", "BUY", 0.05, 1.16, 1.17, MAGIC, 20)
    assert fake.closed_calls == 1 and fake.positions[7001]["positionStatus"] == 2
    b._drop()


def test_volume_below_minimum_is_refused_before_sending(broker, fake):
    with pytest.raises(BrokerError, match="below the minimum"):
        broker.send_market("EURUSD", "BUY", 0.001, 1.16, 1.17, MAGIC, 20)
    assert not any(m["payloadType"] == 2106 for m in fake.received)


def test_missing_credentials_message(monkeypatch):
    from app.config import get_settings
    from app.ctrader import CTraderBroker

    monkeypatch.setenv("CTRADER_CLIENT_ID", "")
    get_settings.cache_clear()
    with pytest.raises(BrokerError, match="CTRADER_CLIENT_ID"):
        CTraderBroker().connect()
    get_settings.cache_clear()


def test_engine_cycle_reconcile_and_close_on_ctrader(fake, monkeypatch):
    """The real engine, scoring and gate running against the cTrader client (fake server), end to end."""
    from sqlalchemy import select

    from app import broker as brk
    from app import engine, scoring
    from app.config import get_settings
    from app.ctrader import CTraderBroker
    from app.db import Position, Signal, session

    monkeypatch.setenv("SYMBOLS", "EURUSD,XAUUSD")
    get_settings.cache_clear()
    b = CTraderBroker(timeout=5)
    brk.set_broker(b)
    monkeypatch.setattr(scoring, "combine", lambda *a, **k: {"score": 55.0, "agree": 5, "available": 7, "bias": "BUY"})
    monkeypatch.setattr(engine, "decide", lambda ctx, model=None: {"decision": "YES", "confidence": 0.8, "thesis": "t", "against": "a", "invalidation": "i", "main_risk": "r"})
    out = engine.run_cycle(force=True)
    assert out["ok"], out
    with session() as s:
        sigs = s.scalars(select(Signal)).all()
        assert [x.status for x in sigs].count("executed") >= 1, [(x.symbol, x.status, x.reason) for x in sigs]
        pos = s.scalars(select(Position).where(Position.status == "open")).all()
        assert pos and all(p.ticket > 7000 for p in pos)
        assert all(0 < p.lots <= 50 for p in pos)
    assert all(m["payloadType"] != 2106 or m["payload"]["label"] == str(get_settings().magic) for m in fake.received)
    st = engine.state()
    assert st["bridge"]["ok"] and st["account"]["demo"] and st["risk"]["open_count"] >= 1
    engine.reconcile()
    with session() as s:
        assert all(p.floating == 12.5 for p in s.scalars(select(Position).where(Position.status == "open")))
    first = pos[0].ticket
    engine.close_position(first)
    engine.reconcile()
    with session() as s:
        closed = s.scalars(select(Position).where(Position.ticket == first)).one()
        assert closed.status == "closed" and closed.profit == pytest.approx(25.0 - 3.5 - 3.5 - 0.1)
    b._drop()
    brk.set_broker(brk.MockBroker())
