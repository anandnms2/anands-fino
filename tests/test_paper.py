import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import broker as brk
from app import engine, paper, scoring
from app.config import get_settings
from app.db import Position, Signal, kv_set, session

AUTH = ("admin", "secret")
HDR = {"X-Requested-With": "fino"}
MAGIC = 26102026


class Feed:
    """Controllable candle source: each symbol has a base price; extra candles can be appended."""

    def __init__(self):
        self.extra: dict[str, list[tuple]] = {}
        self.base = {"EURUSD": 1.1650, "XAUUSD": 4100.0, "XAGUSD": 50.0, "GBPUSD": 1.34, "USDJPY": 155.0, "EURJPY": 180.0}

    def __call__(self, symbol, tf=60):
        now_h = int(time.time() // 3600) * 3600
        b = self.base.get(symbol, 1.2)
        rows = []
        for k in range(300):
            px = b * (1 + 0.0003 * ((k * 7) % 11 - 5) / 5 + 0.00002 * k)
            rows.append([now_h - (299 - k) * 3600, px, px * 1.0008, px * 0.9992, px * 1.0001, 10])
        for i, (hi, lo, cl) in enumerate(self.extra.get(symbol, [])):
            rows.append([now_h + (i + 1) * 3600, rows[-1][4], hi, lo, cl, 10])
        return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "tick_volume"])


@pytest.fixture
def feed(monkeypatch):
    f = Feed()
    monkeypatch.setattr(paper, "fetch_candles", f)
    monkeypatch.setenv("PAPER_PRICE_TTL_S", "0")
    monkeypatch.setenv("PAPER_BALANCE", "10000")
    get_settings.cache_clear()
    paper._paper = None
    yield f
    get_settings.cache_clear()
    paper._paper = None


@pytest.fixture
def pb(feed):
    b = paper.get_paper()
    b.reset()
    return b


def test_rates_tick_and_symbol_info(pb):
    df = pb.rates("EURUSD", 200)
    assert len(df) == 200 and list(df.columns) == brk.RATE_COLS and str(df["time"].dt.tz) == "UTC"
    t = pb.tick("EURUSD")
    assert t["ask"] > t["bid"] and (t["ask"] - t["bid"]) == pytest.approx(0.00007)
    g = pb.symbol_info("XAUUSD")
    assert g["trade_contract_size"] == 100.0 and g["digits"] == 2 and g["currency_profit"] == "USD"
    assert pb.account()["trade_mode"] == 0 and pb.account()["balance"] == 10000.0


def test_take_profit_books_profit(pb, feed):
    price = pb.tick("XAUUSD")["ask"]
    res = pb.send_market("XAUUSD", "BUY", 0.10, price - 30, price + 60, MAGIC, 20)
    tk = res["order"]
    assert pb.positions(MAGIC)[0]["ticket"] == tk
    feed.extra["XAUUSD"] = [(price + 70, price + 5, price + 50)]  # touches the target, not the stop
    assert pb.positions(MAGIC) == []
    deals = pb.deals_for_position(tk)
    out = [d for d in deals if d["entry"] == 1]
    assert len(out) == 1 and out[0]["price"] == pytest.approx(price + 60) and out[0]["profit"] == pytest.approx(60 * 0.10 * 100)
    assert pb.account()["balance"] == pytest.approx(10000 + 600.0)


def test_stop_wins_when_both_touched_and_sell_side(pb, feed):
    price = pb.tick("EURUSD")["bid"]
    res = pb.send_market("EURUSD", "SELL", 0.50, price + 0.0050, price - 0.0100, MAGIC, 20)
    feed.extra["EURUSD"] = [(price + 0.02, price - 0.02, price)]  # one candle spans stop and target
    assert pb.positions(MAGIC) == []
    out = [d for d in pb.deals_for_position(res["order"]) if d["entry"] == 1][0]
    assert out["price"] == pytest.approx(price + 0.0050) and out["profit"] < 0
    assert out["profit"] == pytest.approx(-0.0050 * 0.50 * 100000, rel=1e-6)


def test_manual_close_and_floating_pnl(pb):
    price = pb.tick("EURUSD")["ask"]
    res = pb.send_market("EURUSD", "BUY", 0.10, price - 0.01, price + 0.02, MAGIC, 20)
    pos = pb.positions(MAGIC)[0]
    assert pos["profit"] == pytest.approx((pb.tick("EURUSD")["bid"] - price) * 0.10 * 100000)  # just the spread paid
    r = pb.close_position(res["order"], MAGIC, 20)
    assert r["retcode"] == brk.RETCODE_DONE and pb.positions(MAGIC) == []
    with pytest.raises(brk.BrokerError):
        pb.close_position(res["order"], MAGIC, 20)


def test_state_survives_new_instance(pb):
    price = pb.tick("EURUSD")["ask"]
    pb.send_market("EURUSD", "BUY", 0.10, price - 0.01, price + 0.02, MAGIC, 20)
    again = paper.PaperBroker()
    assert len(again.positions(MAGIC)) == 1


def test_paper_and_demo_trade_in_parallel(feed, monkeypatch):
    from app import venues
    from app.main import app

    c = TestClient(app)
    monkeypatch.setattr(scoring, "combine", lambda *a, **k: {"score": 55.0, "agree": 5, "available": 7, "bias": "BUY"})
    monkeypatch.setattr(engine, "decide", lambda ctx, model=None: {"decision": "YES", "confidence": 0.8, "thesis": "t", "against": "a", "invalidation": "i", "main_risk": "r"})
    monkeypatch.setenv("SYMBOLS", "EURUSD,XAUUSD")
    get_settings.cache_clear()
    paper.get_paper().reset()
    for sym in feed.base:  # the paper feed quotes close to the demo broker, as real feeds do
        feed.base[sym] = brk.get_broker("demo").tick(sym)["bid"] / 1.006  # the synthetic feed drifts up ~0.6%
    assert c.post("/api/venue/paper", auth=AUTH, headers=HDR, json={"on": True}).json()["on"] is True
    assert isinstance(brk.get_broker("paper"), paper.PaperBroker) and isinstance(brk.get_broker("demo"), brk.MockBroker)
    assert engine.run_cycle(force=True)["ok"]
    with session() as s:
        venues_hit = {p.venue for p in s.scalars(select(Position).where(Position.status == "open"))}
        assert venues_hit == {"paper", "demo"}
        assert {x.venue for x in s.scalars(select(Signal).where(Signal.status == "executed"))} == {"paper", "demo"}
    st = c.get("/api/state", auth=AUTH).json()
    assert st["venues"]["paper"]["account"]["demo"] is True and st["venues"]["paper"]["open_count"] >= 1
    assert st["venues"]["live"]["locked"] is True
    latest = c.get("/api/signals/latest", auth=AUTH).json()
    assert any({v["venue"] for v in row["venues"]} == {"paper", "demo"} for row in latest)
    assert c.get("/api/diagnostics?venue=paper", auth=AUTH).json()["bridge"]["ok"] is True
    # the paper account cannot be reset with positions open, then can once they are closed
    assert c.post("/api/paper/reset", auth=AUTH, headers=HDR).status_code == 400
    engine.reconcile()
    for p in engine.open_positions_view("paper"):
        engine.close_position(p["ticket"], "paper")
    engine.reconcile()
    assert c.post("/api/paper/reset", auth=AUTH, headers=HDR).status_code == 200
    assert c.get("/", auth=AUTH).status_code == 200


def test_paper_env_default_and_switch_override(feed, monkeypatch):
    from app import venues

    monkeypatch.setenv("PAPER_MODE", "true")
    get_settings.cache_clear()
    assert venues.venue_cfg("paper")["on"] is True
    venues.set_venue("paper", on=False)
    assert venues.venue_cfg("paper")["on"] is False


def test_yen_pair_profit_is_converted_to_usd(pb):
    t = pb.tick("USDJPY")
    info = pb.symbol_info("USDJPY")
    assert info["currency_profit"] == "JPY" and info["digits"] == 3
    r = pb.send_market("USDJPY", "BUY", 0.10, t["ask"] - 0.5, t["ask"] + 5.0, MAGIC, 20)
    p = pb._state()["positions"][str(r["order"])]
    one_yen = pb._pnl({**p, "price_open": 155.0}, 156.0)   # +1.00 yen on 0.10 lot = 10,000 yen
    assert one_yen == pytest.approx(10000 / ((pb.tick("USDJPY")["bid"] + pb.tick("USDJPY")["ask"]) / 2), rel=1e-6)


def test_each_venue_runs_on_its_own_timeframe(feed, monkeypatch):
    from datetime import datetime, timezone
    from app import venues
    from app.main import app

    c = TestClient(app)
    assert venues.due(3, datetime(2026, 10, 21, 7, 9, tzinfo=timezone.utc)) and not venues.due(3, datetime(2026, 10, 21, 7, 10, tzinfo=timezone.utc))
    assert venues.due(60, datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)) and not venues.due(60, datetime(2026, 10, 21, 8, 30, tzinfo=timezone.utc))
    assert c.post("/api/venue/paper", auth=AUTH, headers=HDR, json={"tf": 7}).status_code == 400
    assert c.post("/api/venue/paper", auth=AUTH, headers=HDR, json={"on": True, "tf": 5}).json()["tf"] == 5
    assert c.post("/api/venue/demo", auth=AUTH, headers=HDR, json={"tf": 3}).json()["tf"] == 3
    monkeypatch.setattr(scoring, "combine", lambda *a, **k: {"score": 55.0, "agree": 5, "available": 7, "bias": "BUY"})
    monkeypatch.setattr(engine, "decide", lambda ctx, model=None: {"decision": "NO", "confidence": 0.2, "thesis": "", "against": "x", "invalidation": "", "main_risk": ""})
    paper.get_paper().reset()
    out = engine.run_cycle(force=True)
    assert out["ok"] and any(k.startswith("M5:") for k in out["results"]) and any(k.startswith("M3:") for k in out["results"])
    with session() as s:
        assert {x.timeframe for x in s.scalars(select(Signal))} == {"M3", "M5"}
    # not forced: a minute that closes neither a 3 nor a 5 minute candle does nothing
    class FakeNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 21, 7, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(engine, "utcnow", lambda: datetime(2026, 10, 21, 7, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(engine, "market_open", lambda *a, **k: True)
    assert engine.run_cycle()["note"] == "no timeframe is due"


def test_paper_spec_overrides_spread_contract_and_pnl(pb):
    from app.main import app
    c = TestClient(app)
    d = paper.default_spec("EURUSD")
    assert d["contract"] == 100000.0 and d["digits"] == 5
    r = c.post("/api/paper/spec/EURUSD", auth=AUTH, headers=HDR, json={"spread": "0.00030", "contract": "1000", "volume_min": "0.1"})
    assert r.status_code == 200 and r.json()["spread"] == 0.0003
    t = pb.tick("EURUSD")
    assert (t["ask"] - t["bid"]) == pytest.approx(0.0003)
    info = pb.symbol_info("EURUSD")
    assert info["trade_contract_size"] == 1000.0 and info["volume_min"] == 0.1
    rows = c.get("/api/specs?venue=paper", auth=AUTH).json()
    eur = next(x for x in rows["rows"] if x["symbol"] == "EURUSD")
    assert rows["editable"] and eur["custom"] and eur["cost_per_lot"] == pytest.approx(0.0003 * 1000, abs=0.01)
    assert c.post("/api/paper/spec/EURUSD", auth=AUTH, headers=HDR, json={"spread": "-1"}).status_code == 400
    assert c.post("/api/paper/spec/NOPE", auth=AUTH, headers=HDR, json={"spread": "1"}).status_code == 400
    c.post("/api/paper/spec/EURUSD", auth=AUTH, headers=HDR, json={"reset": True})
    assert paper.paper_spec("EURUSD")["contract"] == 100000.0
    # demo reads the broker's own specs and is read-only
    demo = c.get("/api/specs?venue=demo", auth=AUTH).json()
    assert demo["editable"] is False and demo["rows"][0]["ok"] and demo["rows"][0]["contract"] > 0
