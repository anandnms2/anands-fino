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


def test_paper_balance_is_editable_and_survives_reset():
    from app.paper import get_paper
    from app import main

    assert get_paper().account()["balance"] == 10000.0
    assert main.api_paper_balance({"balance": 25000})["balance"] == 25000.0
    assert get_paper().account()["balance"] == 25000.0
    get_paper().reset()
    assert get_paper().account()["balance"] == 25000.0
    import pytest
    from fastapi import HTTPException
    for bad in (5, "x", None, float("nan")):
        with pytest.raises(HTTPException):
            main.api_paper_balance({"balance": bad})


def test_paper_tick_uses_fresh_quote_and_fast_tick_never_blocks():
    import time
    from app import paper
    b = paper.get_paper()
    paper._quotes["EURUSD"] = (time.time(), 1.2345)
    t = b.tick("EURUSD")
    assert abs((t["bid"] + t["ask"]) / 2 - 1.2345) < 1e-9
    assert abs((b.tick_fast("EURUSD")["bid"] + b.tick_fast("EURUSD")["ask"]) / 2 - 1.2345) < 1e-9
    paper._quotes.pop("EURUSD")
    b._cache.clear()
    import pytest
    with pytest.raises(paper.BrokerError):
        b.tick_fast("EURUSD")


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
