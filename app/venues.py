"""Trading venues. Paper (simulated account, real prices), Demo (cTrader demo funds) and Live (cTrader real money).

Each venue has its own switch (on), its own approval setting (auto: execute straight away, or wait for a click), its own
account, positions, daily-loss base, loss streak and pause. Claude gives one YES/NO per symbol that all venues share."""
from datetime import datetime, timezone

from .config import get_settings
from .db import iso, kv_get, kv_set, log, utcnow

VENUES = ("paper", "demo", "live")
TIMEFRAMES = (1, 3, 5, 15, 30, 60)  # minutes; the chart and candle size a venue trades on
LABEL = {"paper": "Paper", "demo": "Demo", "live": "Live"}


def allow_live() -> bool:
    """Live trading unlock: the dashboard switch wins; ALLOW_LIVE in the environment is the starting value."""
    v = kv_get("allow_live")
    return bool(get_settings().allow_live) if v is None else bool(v)


def set_allow_live(on: bool) -> bool:
    kv_set("allow_live", bool(on))
    if not on:
        all_ = dict(kv_get("venues") or {})
        all_["live"] = {**(all_.get("live") or {}), "on": False, "auto": False}
        kv_set("venues", all_)
    log(f"Live trading {'UNLOCKED' if on else 'locked (Live venue switched off)'}", "warn")
    return allow_live()


def _tf0() -> int:
    tf = get_settings().timeframe_min
    return tf if tf in TIMEFRAMES else 3


def venue_tf(v: str) -> int:
    tf = venue_cfg(v).get("tf")
    return tf if tf in TIMEFRAMES else _tf0()


def due(tf: int, now: datetime) -> bool:
    """True when a candle of `tf` minutes has just closed (checked once a minute)."""
    return now.minute == 0 if tf >= 60 else now.minute % tf == 0


def _defaults(v: str) -> dict:
    cfg = get_settings()
    if v == "paper":
        return {"on": bool(cfg.paper_mode), "auto": True, "tf": _tf0()}
    if v == "demo":
        return {"on": cfg.mode == "demo_auto", "auto": True, "tf": _tf0()}
    return {"on": cfg.mode in ("live_approval", "live_auto") and allow_live(), "auto": cfg.mode == "live_auto", "tf": _tf0()}


def venue_cfg(v: str) -> dict:
    if v not in VENUES:
        raise ValueError(f"venue must be one of {list(VENUES)}")
    out = {**_defaults(v), **((kv_get("venues") or {}).get(v) or {})}
    out.setdefault("halt", None)
    if v == "live" and not allow_live():
        out["on"] = False
    return out


def locked(v: str) -> bool:
    return v == "live" and not allow_live()


def set_venue(v: str, on: bool | None = None, auto: bool | None = None, tf: int | None = None) -> dict:
    if v not in VENUES:
        raise ValueError(f"venue must be one of {list(VENUES)}")
    if v == "live" and (on or auto) and not allow_live():
        raise ValueError("Live trading is locked. Set ALLOW_LIVE=true in the environment first.")
    all_ = dict(kv_get("venues") or {})
    cur = {**_defaults(v), **(all_.get(v) or {})}
    if on is not None:
        cur["on"] = bool(on)
    if auto is not None:
        cur["auto"] = bool(auto)
    if tf is not None:
        if int(tf) not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {list(TIMEFRAMES)} minutes")
        cur["tf"] = int(tf)
    all_[v] = cur
    kv_set("venues", all_)
    log(f"{LABEL[v]} venue: {'ON' if cur['on'] else 'OFF'}, {'auto-approve' if cur['auto'] else 'manual approval'}, {cur.get('tf', _tf0())} minute candles", "warn")
    return venue_cfg(v)


def active() -> list[str]:
    """Venues that may open new trades right now."""
    out = []
    for v in VENUES:
        c = venue_cfg(v)
        if c["on"] and not (c.get("halt") or {}).get("on"):
            out.append(v)
    return out


def enabled() -> list[str]:
    return [v for v in VENUES if venue_cfg(v)["on"]]


def venue_halt(v: str) -> dict:
    return venue_cfg(v).get("halt") or {"on": False, "reason": "", "since": None}


def set_halt(v: str, reason: str | None) -> None:
    all_ = dict(kv_get("venues") or {})
    cur = dict(all_.get(v) or {})
    cur["halt"] = {"on": True, "reason": reason, "since": iso(utcnow())} if reason else None
    all_[v] = cur
    kv_set("venues", all_)


def loss_streak(v: str) -> int:
    return int(kv_get(f"loss_streak:{v}", 0) or 0)


def set_loss_streak(v: str, n: int) -> None:
    kv_set(f"loss_streak:{v}", int(n))


def day_start_equity(v: str, account: dict) -> float:
    today = utcnow().strftime("%Y-%m-%d")
    key = f"day_start:{v}"
    row = kv_get(key)
    if not row or row.get("day") != today:
        row = {"day": today, "equity": account["equity"]}
        kv_set(key, row)
    return row["equity"]
