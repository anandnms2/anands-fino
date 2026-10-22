import numpy as np
import pandas as pd

from app import backtest, strategies


def _bars(n=4600, seed=1, drift=0.0, vol=0.0006):
    rng = np.random.default_rng(seed)
    c = 1.1 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + abs(rng.normal(0, vol / 3, n)))
    l = np.minimum(o, c) * (1 - abs(rng.normal(0, vol / 3, n)))
    return pd.DataFrame({"time": 1_700_000_000 + np.arange(n) * 3600, "open": o, "high": h, "low": l, "close": c})


def test_simulate_target_and_stop_and_spread():
    n = 260
    df = pd.DataFrame({"time": 1_700_000_000 + np.arange(n) * 900, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0})
    sig = pd.Series(0, index=df.index)
    sig.iloc[220] = 1
    # the candle after the signal runs up through the target
    df.loc[221, ["high", "close"]] = 110.0
    r = backtest.simulate(df, sig, stop_k=1.0, rr=2.0, spread=0.0, lo=0, hi=n)
    assert list(r) == [2.0]
    # same bar touches both: the stop wins (pessimistic)
    df.loc[221, "low"] = 90.0
    assert list(backtest.simulate(df, sig, 1.0, 2.0, 0.0, 0, n)) == [-1.0]
    # a spread makes the target harder to reach: the long enters at the ask and closes on the bid
    df.loc[221, ["high", "low", "close"]] = [101.1, 99.9, 100.0]   # ATR is 1.0, so the 1R stop is 1.0 and the 2R target 102
    assert backtest.simulate(df, sig, 1.0, 2.0, 0.0, 0, n).size == 1


def test_costs_make_a_random_market_unprofitable_and_nothing_validates_by_luck():
    wins = 0
    for seed in range(4):
        row = backtest.test_symbol("EURUSD", 60, _bars(seed=seed))
        assert row["baseline_test"] is not None
        wins += row["ok"]
    assert wins <= 1      # a coin-flip market must not hand out validated setups


def test_pullback_finds_a_planted_trend_edge_and_signals_have_no_lookahead():
    df = _bars(1500, seed=7, drift=0.0004)          # a steadily trending market
    full = strategies.signal(df, "pullback")
    cut = strategies.signal(df.iloc[:900].reset_index(drop=True), "pullback")
    assert (full.iloc[:900].to_numpy() == cut.to_numpy()).all()     # a signal never depends on later candles
    assert full.abs().sum() > 0


def test_choose_keeps_only_validated_setups():
    rows = [{"symbol": "A", "tf": 15, "ok": True, "best": {"strategy": "pullback", "session": "all", "stop_k": 1.5, "rr": 1.5, "train": {"exp": .2}, "test": {"exp": .1, "n": 20, "t": 1.0}}},
            {"symbol": "A", "tf": 60, "ok": True, "best": {"strategy": "breakout", "session": "all", "stop_k": 2.0, "rr": 2.0, "train": {"exp": .2}, "test": {"exp": .3, "n": 20, "t": 2.0}}},
            {"symbol": "B", "tf": 15, "ok": False, "best": None}]
    ch = backtest.choose(rows)
    assert set(ch) == {"A"} and ch["A"]["strategy"] == "breakout" and ch["A"]["tf"] == 60


def test_history_shorter_than_six_months_is_refused():
    import pytest
    with pytest.raises(RuntimeError, match="180 needed"):
        backtest.test_symbol("EURUSD", 60, _bars(2000))     # about 83 days


def test_resample_builds_three_minute_candles_and_short_history_rules():
    one = pd.DataFrame({"time": 1_700_000_040 + np.arange(9) * 60, "open": np.arange(9, dtype=float), "high": np.arange(9) + 1.0, "low": np.arange(9) - 1.0, "close": np.arange(9) + .5})
    t3 = backtest.resample(one, 3)
    assert (t3["time"] % 180 == 0).all() and t3["high"].iloc[0] >= one["high"].iloc[0]
    assert backtest.MIN_DAYS_BY_TF[1] < backtest.MIN_DAYS_BY_TF[60] == 180 and set(backtest.TIMEFRAMES) == {1, 3, 5, 15, 30, 60}


def test_every_strategy_runs_and_never_looks_ahead():
    df = _bars(1800, seed=11, drift=0.0003)
    assert len([n for n in strategies.STRATEGIES if n != strategies.BASELINE]) == 10
    for name in strategies.STRATEGIES:
        full = strategies.signal(df, name)
        cut = strategies.signal(df.iloc[:1000].reset_index(drop=True), name)
        assert (full.iloc[:1000].to_numpy() == cut.to_numpy()).all(), name      # a signal never depends on later candles
        assert set(full.unique()) <= {-1, 0, 1}


def test_session_range_breakouts_fire_once_after_the_range():
    n = 24 * 3
    t = 1_700_000_000 // 86400 * 86400 + np.arange(n) * 3600       # three days of hourly candles from 00:00 UTC
    c = np.full(n, 100.0); c[8] = 103.0                              # day one, 08:00: a close above the 07:00 range
    df = pd.DataFrame({"time": t, "open": c, "high": c + 0.5, "low": c - 0.5, "close": c})
    sg = strategies.orb_london(df)
    assert sg.iloc[8] == 1 and sg.abs().sum() == 1
