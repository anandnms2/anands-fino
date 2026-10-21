import numpy as np
import pandas as pd

from app import indicators as ind
from app import scoring
from app.config import DEFAULT_WEIGHTS


def test_combine_matches_signal_lab():
    vals = dict(trend=70, pattern=45, momentum=30, macro=55, news=35, positioning=-20, intermarket=15)
    f = {k: {"score": v} for k, v in vals.items()}
    r = scoring.combine(f, DEFAULT_WEIGHTS, 35, 4)
    assert r["score"] == 44.0 and r["agree"] == 5 and r["bias"] == "BUY"


def test_missing_factors_drop_out_and_agree_gate_holds():
    f = {"trend": {"score": 90}, "pattern": {"score": 90}, "momentum": {"score": None}}
    r = scoring.combine(f, DEFAULT_WEIGHTS, 35, 4)
    assert r["bias"] == "NONE" and r["available"] == 2


def test_macro_direction():
    weak_usd = {"usd_index": {"pct5": -0.6, "latest": 1, "chg5": 0}, "real_yield": {"chg5": -0.15, "latest": 1.8}}
    s, _ = scoring.macro_score("XAUUSD", weak_usd)
    assert s > 40
    s, _ = scoring.macro_score("EURUSD", weak_usd)
    assert s > 30
    s, _ = scoring.macro_score("USDJPY", weak_usd)
    assert s < -30


def test_levels_buy_and_sell():
    tech = {"atr": 10.0, "swing_lows": [1980.0], "swing_highs": [2020.0]}
    lv = scoring.build_levels("BUY", tech, {"ask": 2000.0, "bid": 1999.8}, 2, 1.5, 2.0)
    assert lv["sl"] == 1977.5 and lv["tp"] == 2045.0  # stop under the swing low minus 0.25 ATR
    lv = scoring.build_levels("SELL", {"atr": 10.0, "swing_lows": [], "swing_highs": []}, {"ask": 2000.2, "bid": 2000.0}, 2, 1.5, 2.0)
    assert lv["sl"] == 2015.0 and lv["tp"] == 1970.0


def test_indicators():
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert ind.rsi(up).iloc[-1] > 99
    df = pd.DataFrame({"open": [10, 10.5, 9.5], "high": [11, 11, 10.6], "low": [9, 9.8, 9.4], "close": [10.5, 9.7, 10.5]})
    assert ind.candle_pattern(df) == ("bullish engulfing", 1)
    assert ind.clip(250) == 100.0


def test_extract_json_keeps_list_of_objects_and_wrapped_lists():
    from app.llm import extract_json
    from app.sentiment import _valid

    syms = ["XAUUSD", "EURUSD"]
    assert extract_json('Sure:\n```json\n[{"i":0,"XAUUSD":1},{"i":1,"XAUUSD":-1}]\n```') == [{"i": 0, "XAUUSD": 1}, {"i": 1, "XAUUSD": -1}]
    assert extract_json('{"a": 1}') == {"a": 1}
    rows = _valid(extract_json('[{"i":0,"XAUUSD":1,"EURUSD":5},{"i":1,"XAUUSD":-1}]'), 2, syms)
    assert rows[0] == {"XAUUSD": 1, "EURUSD": 0} and rows[1]["XAUUSD"] == -1
    assert 0 in _valid(extract_json('{"tags":[{"i":0,"XAUUSD":1}]}'), 1, syms)


def test_blackout_fails_closed_without_fresh_calendar():
    from app import collectors, db

    with db.session() as s:
        s.query(db.KV).delete()
        s.commit()
    blocked, why = collectors.blackout("XAUUSD")
    assert blocked and "not loaded" in why
    collectors.save_snapshot("calendar", [])
    assert collectors.blackout("XAUUSD")[0] is False


def test_agree_percentage_rule():
    from app.scoring import combine
    w = {"a": 1, "b": 1, "c": 1, "d": 1, "e": 1}
    f = lambda *s: {k: {"score": v} for k, v in zip("abcde", s)}  # noqa: E731
    # 3 of 5 agree is 60%: allowed. 2 of 5 (40%) is not.
    assert combine(f(60, 60, 60, -5, -5), w, 20, 4, 60)["bias"] == "BUY"
    assert combine(f(80, 80, -5, -5, -5), w, 20, 4, 60)["bias"] == "NONE"


def test_inverse_peer_and_inverted_cot():
    from app import scoring as sc
    techs = {"EURUSD": {"trend": {"score": 50}}}
    s, why = sc.intermarket_score("USDCHF", techs)
    assert s == -30 and "contradicts" in why   # EURUSD up is bad for USDCHF
    row = {"USDJPY": {"net": 90000, "percentile": 95, "weeks": 150, "date": "x"}}
    s, why = sc.positioning_score("USDJPY", row)
    assert s == 60   # funds crowded long yen = USDJPY crowded short, contrarian lift


def test_stop_never_thinner_than_twelve_spreads():
    from app.scoring import build_levels
    tech = {"atr": 0.00009, "swing_lows": [], "swing_highs": []}
    tick = {"bid": 1.16500, "ask": 1.16512}
    lv = build_levels("BUY", tech, tick, 5, 1.5, 2.0, 10.5)
    assert lv["stop"] >= 10.5 * 0.00012 - 1e-9
    assert 0.00012 <= 0.1 * lv["stop"] + 1e-9   # now the 10% spread rule can pass
    assert build_levels("BUY", tech, tick, 5, 1.5, 2.0)["stop"] < 0.0002  # without the floor it was a hair
