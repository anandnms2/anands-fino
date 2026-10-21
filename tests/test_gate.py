from app.config import Settings
from app.gate import evaluate

INFO = {"trade_contract_size": 100.0, "volume_min": 0.01, "volume_max": 50.0, "volume_step": 0.01, "currency_profit": "USD"}
ACCT = {"balance": 20000.0, "equity": 20000.0, "currency": "USD"}
LEVELS = {"entry": 4100.0, "sl": 4052.0, "tp": 4196.0}


def run(**over):
    args = dict(cfg=Settings(), symbol="XAUUSD", bias="BUY", levels=LEVELS, confidence=0.7, account=ACCT, sym_info=INFO,
                spread_price=0.18, open_positions=[], day_start_equity=20000.0, loss_streak=0, halted=False,
                blackout=(False, ""), data_ok=True)
    args.update(over)
    return evaluate(**args)


def test_sizing_matches_signal_lab_example():
    r = run()
    assert r["ok"], r["reason"]
    assert r["lots"] == 0.02
    assert abs(r["risk_usd"] - 96.0) < 1e-6


def test_lots_round_down_never_up():
    r = run(levels={"entry": 4100.0, "sl": 4040.0, "tp": 4220.0})  # stop 60 -> 100/6000 = 0.0166
    assert r["lots"] == 0.01


def test_blocks():
    assert not run(halted=True)["ok"]
    assert not run(blackout=(True, "USD CPI in 5 min"))["ok"]
    assert not run(confidence=0.4)["ok"]
    assert not run(levels={"entry": 4100.0, "sl": 4052.0, "tp": 4150.0})["ok"]  # reward:risk about 1.04
    assert not run(spread_price=9.0)["ok"]
    assert not run(data_ok=False)["ok"]
    assert not run(loss_streak=5)["ok"]
    assert not run(account={**ACCT, "equity": 19500.0})["ok"]  # -2.5% on the day
    assert not run(account={**ACCT, "currency": "EUR"})["ok"]
    assert not run(levels={"entry": 4100.0, "sl": 4110.0, "tp": 4200.0})["ok"]  # stop on the wrong side


def test_correlation_and_limits():
    opened = [{"symbol": "XAGUSD", "side": "BUY", "risk_usd": 100.0}]
    assert not run(open_positions=opened)["ok"]  # silver long open, gold long blocked
    assert run(open_positions=[{"symbol": "XAGUSD", "side": "SELL", "risk_usd": 100.0}])["ok"]
    full = [{"symbol": s, "side": "SELL", "risk_usd": 100.0} for s in ("EURUSD", "GBPUSD", "USDJPY")]
    assert not run(open_positions=full)["ok"]


def test_too_small_budget():
    r = run(account={**ACCT, "balance": 1000.0})
    assert not r["ok"] and "minimum" in r["reason"].lower()


def test_non_usd_profit_currency_is_converted_into_account_currency():
    jpy = {**INFO, "trade_contract_size": 100000.0, "currency_profit": "JPY"}
    lv = {"entry": 155.0, "sl": 154.5, "tp": 155.9}  # 50 pips = 0.5 yen
    r = run(symbol="USDJPY", sym_info=jpy, levels=lv, profit_rate=1 / 155.0, spread_price=0.012)
    assert r["ok"], r["reason"]
    # risk 100 USD / (0.5 yen * 100000 units / 155) = 0.31 lots, rounded down
    assert r["lots"] in (0.30, 0.31) and abs(r["risk_usd"] - r["lots"] * 0.5 * 100000 / 155) < 1e-6 and r["risk_usd"] <= 100.0
    assert not run(symbol="USDJPY", sym_info=jpy, levels=lv, profit_rate=None, spread_price=0.012)["ok"]  # no conversion rate: refuse


def test_symbol_in_two_correlation_groups():
    open_ = [{"symbol": "EURJPY", "side": "BUY", "risk_usd": 50.0}, ]
    assert not run(symbol="USDJPY", open_positions=open_)["ok"]          # both short the yen
    assert not run(symbol="USDCAD", open_positions=[{"symbol": "USDJPY", "side": "BUY", "risk_usd": 50.0}])["ok"]  # both long the dollar
    assert any(c["ok"] for c in run(symbol="USDCAD", open_positions=[{"symbol": "USDJPY", "side": "SELL", "risk_usd": 50.0}])["checks"]
               if c["name"].startswith("No correlated"))


def test_stop_too_wide_for_the_chart_is_blocked():
    lv = {"entry": 4100.0, "sl": 4090.0, "tp": 4120.0, "atr": 2.0}   # stop 10 = 5 ATR
    r = run(levels=lv, spread_price=0.18)
    assert not r["ok"] and "ATR wide" in r["reason"]
    assert run(levels={**lv, "atr": 4.0}, spread_price=0.18)["ok"]     # 2.5 ATR is fine
