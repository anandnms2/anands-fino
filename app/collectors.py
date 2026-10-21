"""Data collectors: FRED macro series, CFTC positioning, news RSS, economic calendar.
Each one stores a snapshot in the KV table with its fetch time, so a stale feed is visible and never silent."""
import hashlib
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone

import httpx

from .config import get_settings
from .db import NewsItem, iso, kv_get, kv_set, log, session, utcnow

UA = {"User-Agent": "Mozilla/5.0 (compatible; AnandsFino/1.0)"}


def save_snapshot(name: str, data) -> None:
    kv_set(f"snap:{name}", {"fetched_at": iso(utcnow()), "data": data})


def snapshot(name: str) -> dict | None:
    return kv_get(f"snap:{name}")


def snapshot_age_min(name: str) -> float | None:
    snap = snapshot(name)
    if not snap:
        return None
    t = datetime.strptime(snap["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (utcnow() - t).total_seconds() / 60


# ------------------------------------------------------------------ FRED
FRED_SERIES = {"real_yield": "DFII10", "us10y": "DGS10", "usd_index": "DTWEXBGS"}


def fetch_macro() -> dict:
    key = get_settings().fred_api_key
    if not key:
        raise RuntimeError("FRED_API_KEY is not set")
    out = {}
    with httpx.Client(timeout=30, headers=UA) as c:
        for name, sid in FRED_SERIES.items():
            r = c.get("https://api.stlouisfed.org/fred/series/observations",
                      params={"series_id": sid, "api_key": key, "file_type": "json", "sort_order": "desc", "limit": 40})
            r.raise_for_status()
            obs = [(o["date"], float(o["value"])) for o in r.json()["observations"] if o["value"] not in (".", "")]
            if len(obs) < 6:
                raise RuntimeError(f"FRED series {sid} returned too few observations")
            latest = obs[0][1]
            out[name] = {
                "series": sid, "date": obs[0][0], "latest": latest,
                "chg5": latest - obs[5][1],
                "pct5": (latest / obs[5][1] - 1) * 100 if obs[5][1] else 0.0,
            }
    save_snapshot("macro", out)
    return out


# ------------------------------------------------------------------ CFTC Commitments of Traders
COT_MARKETS = {"XAUUSD": "GOLD -", "XAGUSD": "SILVER -", "EURUSD": "EURO FX -", "GBPUSD": "BRITISH POUND"}


def fetch_cot() -> dict:
    import pandas as pd

    year = utcnow().year
    frames = []
    with httpx.Client(timeout=90, headers=UA, follow_redirects=True) as c:
        for y in (year - 2, year - 1, year):
            r = c.get(f"https://www.cftc.gov/files/dea/history/deacot{y}.zip")
            if r.status_code != 200:
                continue
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                name = z.namelist()[0]
                frames.append(pd.read_csv(z.open(name), low_memory=False))
    if not frames:
        raise RuntimeError("could not download any CFTC report")
    df = pd.concat(frames, ignore_index=True)
    cols = {re.sub(r"\s+", " ", str(c)).strip().lower(): c for c in df.columns}

    def col(*needles):
        for k, v in cols.items():
            if all(n in k for n in needles):
                return v
        raise RuntimeError(f"CFTC column not found: {needles}")

    c_name = col("market and exchange")
    c_date = col("as of date", "yyyy-mm-dd")
    c_long = col("noncommercial positions-long (all)")
    c_short = col("noncommercial positions-short (all)")
    out = {}
    for sym, prefix in COT_MARKETS.items():
        sub = df[df[c_name].astype(str).str.upper().str.startswith(prefix)].copy()
        if sub.empty:
            continue
        sub["net"] = sub[c_long].astype(float) - sub[c_short].astype(float)
        sub = sub.sort_values(c_date)
        net = sub["net"].values
        latest = float(net[-1])
        pct = float((net <= latest).mean() * 100)  # percentile of the latest reading in the window
        out[sym] = {"date": str(sub[c_date].iloc[-1]), "net": latest, "percentile": pct, "weeks": int(len(net))}
    if not out:
        raise RuntimeError("no CFTC markets matched")
    save_snapshot("cot", out)
    return out


# ------------------------------------------------------------------ News RSS
CENTRAL_BANK_HOSTS = ("federalreserve.gov", "ecb.europa.eu", "bankofengland.co.uk")


def fetch_news() -> int:
    import feedparser

    new = 0
    cutoff = utcnow() - timedelta(hours=48)
    with httpx.Client(timeout=25, headers=UA, follow_redirects=True) as c, session() as s:
        for url in get_settings().feed_list:
            try:
                r = c.get(url)
                r.raise_for_status()
                feed = feedparser.parse(r.content)
            except Exception as e:  # noqa: BLE001
                log(f"News feed failed: {url} ({type(e).__name__})", "warn")
                continue
            source = (feed.feed.get("title") or url)[:80]
            is_cb = int(any(h in url for h in CENTRAL_BANK_HOSTS))
            for e in feed.entries[:40]:
                title = (e.get("title") or "").strip()
                link = e.get("link") or ""
                if not title:
                    continue
                t = e.get("published_parsed") or e.get("updated_parsed")
                pub = datetime(*t[:6], tzinfo=timezone.utc) if t else utcnow()
                if pub < cutoff:
                    continue
                uid = hashlib.sha1((link or title).encode()).hexdigest()[:40]
                if s.query(NewsItem).filter_by(uid=uid).first():
                    continue
                s.add(NewsItem(uid=uid, published=pub, source=source, title=title[:400], link=link, central_bank=is_cb))
                new += 1
        s.commit()
    save_snapshot("news_run", {"new": new})
    return new


# ------------------------------------------------------------------ Economic calendar
def fetch_calendar(min_age_min: float = 30.0) -> list[dict]:
    """The free calendar host rate-limits hard (429), so reuse the stored copy unless it is older than min_age_min."""
    age = snapshot_age_min("calendar")
    if age is not None and age < min_age_min:
        return snapshot("calendar")["data"]
    r = httpx.get(get_settings().calendar_url, timeout=30, headers=UA, follow_redirects=True)
    if r.status_code == 429 and snapshot("calendar"):
        return snapshot("calendar")["data"]  # keep the stored copy; blackout() blocks trading if it gets too old
    r.raise_for_status()
    events = []
    for e in r.json():
        impact = str(e.get("impact", ""))
        if impact not in ("High", "Medium"):
            continue
        try:
            when = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        events.append({"title": e.get("title", ""), "currency": e.get("country", ""), "impact": impact, "time": iso(when)})
    events.sort(key=lambda x: x["time"])
    save_snapshot("calendar", events)
    return events


def symbol_currencies(symbol: str) -> set[str]:
    s = symbol.upper()
    if s.startswith(("XAU", "XAG", "XPT", "XPD")):
        return {"USD"}
    if len(s) >= 6:
        return {s[:3], s[3:6]}
    return {"USD"}


def _parse(t: str) -> datetime:
    return datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def blackout(symbol: str, now: datetime | None = None) -> tuple[bool, str]:
    """True when a high-impact event for the symbol's currencies is within the blackout window."""
    now = now or utcnow()
    snap = snapshot("calendar")
    if not snap:
        return True, "calendar not loaded, cannot rule out a news event"
    age = snapshot_age_min("calendar")
    if age is not None and age > 26 * 60:
        return True, f"calendar data is {age / 60:.0f}h old, cannot rule out a news event"
    win = timedelta(minutes=get_settings().blackout_min)
    cur = symbol_currencies(symbol)
    for e in snap["data"]:
        if e["impact"] != "High" or e["currency"] not in cur:
            continue
        t = _parse(e["time"])
        if -win <= (t - now) <= win:
            mins = int((t - now).total_seconds() / 60)
            when = f"in {mins} min" if mins >= 0 else f"{-mins} min ago"
            return True, f"{e['currency']} {e['title']} {when}"
    return False, ""


def upcoming_events(now: datetime | None = None, hours: int = 36, limit: int = 6) -> list[dict]:
    now = now or utcnow()
    snap = snapshot("calendar")
    if not snap:
        return []
    out = []
    for e in snap["data"]:
        t = _parse(e["time"])
        if now - timedelta(minutes=get_settings().blackout_min) <= t <= now + timedelta(hours=hours):
            out.append({**e, "minutes": int((t - now).total_seconds() / 60)})
    return out[:limit]
