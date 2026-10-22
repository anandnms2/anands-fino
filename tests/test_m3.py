import numpy as np
import pandas as pd

from app import broker as brk
from app import engine, scoring
from app.config import get_settings
from app.db import Signal, session
from app.features import compute_technicals
from sqlalchemy import select


def test_technicals_on_m3_use_h1_confirmation():
    n = 1500
    close = 100 + np.cumsum(np.full(n, 0.01)) + np.sin(np.arange(n) / 7) * 0.2
    t = pd.date_range("2026-10-19", periods=n, freq="3min", tz="UTC")
    df = pd.DataFrame({"time": t, "open": close - 0.01, "high": close + 0.05, "low": close - 0.05, "close": close, "tick_volume": 1})
    tech = compute_technicals(df, 2, 3)
    assert "H1 trend up" in tech["trend"]["reading"]


def test_cycle_on_m3_labels_signals_and_claude_cooldown(monkeypatch):
    monkeypatch.setenv("TIMEFRAME_MIN", "3")
    get_settings.cache_clear()
    monkeypatch.setattr(scoring, "combine", lambda *a, **k: {"score": 55.0, "agree": 5, "available": 7, "bias": "BUY"})
    calls = []
    monkeypatch.setattr(engine, "decide", lambda ctx, model=None: calls.append(1) or {"decision": "NO", "confidence": 0.2, "thesis": "", "against": "x", "invalidation": "", "main_risk": ""})
    df = brk.get_broker("demo").rates("EURUSD", 10)
    assert (df["time"].diff().dropna() == pd.Timedelta(minutes=3)).all()
    engine.run_cycle(force=True)
    first = len(calls)
    assert first >= 1
    engine.run_cycle(force=True)
    assert len(calls) == first  # cooldown: no second question for the same symbol and direction
    with session() as s:
        assert {x.timeframe for x in s.scalars(select(Signal))} == {"M3"}
    assert engine.state()["venues"]["demo"]["tf_label"] == "M3"


def test_ctrader_period_map():
    from app.ctrader import PERIODS
    assert PERIODS[3] == 3 and PERIODS[60] == 9 and PERIODS[5] == 5
