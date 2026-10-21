"""The risk gate. Pure code. No model can change these rules or the numbers they produce."""
import math

from .config import CORRELATION_GROUPS, Settings


def _group(symbol: str) -> set[str]:
    out = {symbol}
    for g in CORRELATION_GROUPS:
        if symbol in g:
            out |= set(g)
    return out


def evaluate(*, cfg: Settings, symbol: str, bias: str, levels: dict, confidence: float | None, account: dict,
             sym_info: dict, spread_price: float, open_positions: list[dict], day_start_equity: float,
             loss_streak: int, halted: bool, blackout: tuple[bool, str], data_ok: bool, profit_rate: float | None = None) -> dict:
    """Returns {ok, reason, lots, risk_usd, checks}. `confidence` is None for the pre-check before Claude is asked."""
    checks = []

    def add(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    entry, sl, tp = levels["entry"], levels["sl"], levels["tp"]
    buy = bias == "BUY"
    stop = abs(entry - sl)
    reward = abs(tp - entry)
    rr = reward / stop if stop > 0 else 0.0

    add("Trading not halted", not halted, "halted by kill switch" if halted else "")
    add("Data is fresh", data_ok, "" if data_ok else "candles or feeds are stale")
    sl_ok = stop > 0 and ((sl < entry) if buy else (sl > entry))
    add("Stop loss present and on the right side", sl_ok, f"stop distance {stop:g}")
    same_ccy = sym_info.get("currency_profit") == account.get("currency")
    rate = 1.0 if same_ccy else profit_rate
    add("Profit currency known in account currency", rate is not None and rate > 0,
        f"{sym_info.get('currency_profit')} to {account.get('currency')}" + ("" if same_ccy or not rate else f" at {rate:g}") + ("" if rate else ": no conversion rate"))
    if confidence is None:
        add("Claude confidence", True, "checked after Claude decides")
    else:
        add(f"Confidence {confidence:.2f}, minimum {cfg.min_confidence:.2f}", confidence >= cfg.min_confidence)
    add(f"Reward to risk {rr:.2f}, minimum {cfg.min_rr:.2f}", rr >= cfg.min_rr)
    add("No high-impact event within the blackout window", not blackout[0], blackout[1])
    add(f"Open trades {len(open_positions)} of {cfg.max_open}", len(open_positions) < cfg.max_open)
    add("No open position on this symbol", all(p["symbol"] != symbol for p in open_positions))
    grp = _group(symbol)
    side = "BUY" if buy else "SELL"
    clash = [p["symbol"] for p in open_positions if p["symbol"] in grp and p["side"] == side]
    add("No correlated trade open in the same direction", not clash, ", ".join(clash))
    loss_pct = (account["equity"] - day_start_equity) / day_start_equity * 100 if day_start_equity else 0.0
    add(f"Daily result {loss_pct:+.2f}%, loss cap {cfg.daily_loss_cap_pct:.2f}%", loss_pct > -cfg.daily_loss_cap_pct)
    add(f"Losses in a row {loss_streak} of {cfg.max_loss_streak}", loss_streak < cfg.max_loss_streak)
    spread_ok = stop > 0 and spread_price <= cfg.max_spread_to_stop * stop
    add(f"Spread {spread_price:g}, limit {cfg.max_spread_to_stop * stop:g}", spread_ok)

    # position size from the risk budget, rounded DOWN to the broker's lot step
    lots, risk_usd = 0.0, 0.0
    balance = account["balance"]
    per_lot_loss = stop * sym_info["trade_contract_size"] * (rate if rate else 0.0)
    if per_lot_loss > 0:
        raw = balance * cfg.risk_per_trade_pct / 100 / per_lot_loss
        step = sym_info["volume_step"] or 0.01
        lots = math.floor(raw / step + 1e-9) * step
        lots = min(lots, sym_info["volume_max"])
        lots = round(lots, 8)
        risk_usd = lots * per_lot_loss
    add(f"Lot size {lots:g}, minimum {sym_info['volume_min']:g}", lots >= sym_info["volume_min"] and lots > 0,
        "risk budget buys less than the minimum lot" if lots < sym_info["volume_min"] else "")
    open_risk = sum(p.get("risk_usd", 0.0) for p in open_positions)
    cap_pct = cfg.max_open * cfg.risk_per_trade_pct
    total_pct = (open_risk + risk_usd) / balance * 100 if balance else 100.0
    add(f"Total open risk {total_pct:.2f}%, limit {cap_pct:.2f}%", total_pct <= cap_pct + 1e-9)

    failed = [c for c in checks if not c["ok"]]
    return {
        "ok": not failed,
        "reason": "; ".join(f"{c['name']}" + (f" ({c['detail']})" if c["detail"] else "") for c in failed),
        "lots": lots if not failed else 0.0,
        "risk_usd": risk_usd,
        "checks": checks,
    }
