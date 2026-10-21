"""Settings, read from environment variables (Coolify env) or a local .env file."""
import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_WEIGHTS = {
    "trend": 25,
    "pattern": 15,
    "momentum": 10,
    "macro": 20,
    "news": 15,
    "positioning": 5,
    "intermarket": 10,
}

DEFAULT_FEEDS = [
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.ecb.europa.eu/rss/press.html",
    "https://www.bankofengland.co.uk/rss/news",
]

# Symbols that move together. The gate allows one open trade per group and direction.
# A symbol can sit in several groups (USDJPY is both USD-long and JPY-short).
CORRELATION_GROUPS = [
    ["XAUUSD", "XAGUSD"],
    ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"],          # all short the dollar when bought
    ["USDJPY", "USDCHF", "USDCAD"],                    # all long the dollar when bought
    ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY"],          # all short the yen when bought
]

MODES = ["research", "alerts", "demo_auto", "live_approval", "live_auto"]
LIVE_MODES = {"live_approval", "live_auto"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # storage
    database_url: str = "sqlite:///./fino.db"

    # which broker connection to use: "ctrader" (cTrader Open API) or "mt5" (MT5 under Wine, see docker-compose.mt5.yaml)
    broker: str = "ctrader"
    ctrader_client_id: str = ""
    ctrader_client_secret: str = ""
    ctrader_env: str = "demo"          # demo or live; must match the account used
    ctrader_account_id: str = ""       # optional: trader login or ctid when the cTrader ID has several accounts (applies to CTRADER_ENV)
    ctrader_demo_account_id: str = ""  # optional, per venue
    ctrader_live_account_id: str = ""
    ctrader_access_token: str = ""     # optional: normally obtained through /ctrader/connect and kept in the database
    ctrader_refresh_token: str = ""
    ctrader_url: str = ""              # override of the WebSocket endpoint (tests)

    # paper trading: real prices from a free feed, simulated account. The dashboard switch overrides paper_mode.
    paper_mode: bool = False
    paper_feed: str = "yahoo"          # yahoo (no key) or twelvedata
    twelvedata_api_key: str = ""
    paper_balance: float = 10000.0
    paper_spreads: str = ""            # JSON, price units, e.g. {"XAUUSD": 0.35}
    paper_price_ttl_s: int = 60

    # MT5 bridge (MT5 under Wine, reached over RPyC on the stack's private network)
    mt5_host: str = "mt5"
    mt5_port: int = 8001
    mt5_mock: bool = False
    broker_utc_offset_hours: str = ""  # leave empty to auto-detect from candles
    magic: int = 26102026
    deviation: int = 20

    # what to trade
    symbols: str = "XAUUSD,XAGUSD,EURUSD,GBPUSD,USDJPY,USDCHF,USDCAD,AUDUSD,NZDUSD,EURJPY,GBPJPY,EURGBP,AUDJPY"
    symbol_map: str = ""  # JSON, e.g. {"XAUUSD": "XAUUSD.m"} when the broker adds suffixes

    # autonomy. One of: research, alerts, demo_auto, live_approval, live_auto
    mode: str = "demo_auto"
    allow_live: bool = False  # live orders are refused unless this is true
    approval_ttl_min: int = 10

    # models
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-5-5"
    claude_fallback_model: str = "claude-haiku-5-5"
    claude_max_calls_per_day: int = 40
    freellmapi_base_url: str = ""
    freellmapi_key: str = ""
    freellmapi_model: str = "auto"
    freellmapi_max_calls_per_day: int = 500

    # data
    fred_api_key: str = ""
    news_feeds: str = ""  # comma separated, overrides the defaults
    calendar_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    # signal rules
    weights: str = ""  # JSON override of DEFAULT_WEIGHTS
    score_threshold: float = 35.0
    min_agree: int = 4                 # used only when min_agree_pct is 0
    min_agree_pct: float = 60.0        # share of available factors that must agree with the direction
    stop_atr_mult: float = 1.5
    rr_target: float = 2.0

    # risk gate
    risk_per_trade_pct: float = 0.5
    daily_loss_cap_pct: float = 2.0
    max_open: int = 3
    min_rr: float = 1.5
    spread_stop_limit: float = 0.25
    min_confidence: float = 0.6
    max_loss_streak: int = 5
    blackout_min: int = 15

    # timing
    cycle_delay_seconds: int = 15
    timeframe_min: int = 3             # candle size the whole analysis runs on (1, 2, 3, 4, 5, 10, 15, 30 or 60); one cycle per candle
    min_atr_to_spread: float = 2.0     # skip when one average candle moves less than this many spreads (nothing to earn after costs)
    max_stop_atr: float = 3.5          # a stop wider than this many ATRs puts the target out of reach on this chart: skip
    stop_floor_spreads: float = 0.0    # 0 = derived from SPREAD_STOP_LIMIT (stop at least 1.05 / that many spreads); renamed from MIN_STOP_SPREADS, whose old value Coolify froze at 12
    max_tp_atr: float = 3.0            # the target is never further than this many ATRs when the risk:reward allows (a 7 ATR target is never reached)
    watch_on: bool = True              # reversal watch: re-score every open trade on all factors and act when the view flips (the dashboard switch overrides)
    watch_every_s: int = 30
    watch_exit_score: float = 30.0     # the verdict against the trade this strong, confirmed WATCH_CONFIRM checks in a row with 3+ factors against, closes it
    watch_confirm: int = 2
    trail_on: bool = True              # trailing stop: lock profit as a trade moves in our favour (the dashboard switch overrides)
    trail_start_r: float = 1.0         # at this many R of profit (R = the first stop distance) the stop moves to break-even plus TRAIL_LOCK_R
    trail_lock_r: float = 0.2
    trail_step_r: float = 1.5          # from here the stop follows the price at TRAIL_GAP_R behind it and the target moves TRAIL_TP_R ahead of it
    trail_gap_r: float = 0.75
    trail_tp_r: float = 1.0
    profile: str = "fast"              # fast = trade often (looser filters), normal = the stricter values below; the dashboard switch overrides this
    auto_tf: bool = True               # per symbol: climb to a bigger candle when the chosen one moves less than AUTO_TF_ATR_SPREADS spreads
    auto_tf_atr_spreads: float = 3.0
    auto_tf_max: int = 15              # the biggest candle the auto chart will climb to
    claude_cooldown_min: int = 15      # after Claude says NO to a symbol and direction, do not ask again for this long
    run_cycle_on_start: bool = False
    scheduler_enabled: bool = True

    # access and alerts
    admin_user: str = "admin"
    admin_password: str = ""
    notify_webhook_url: str = ""

    @property
    def symbol_list(self) -> list[str]:
        """The dashboard's list wins over the SYMBOLS environment variable (Coolify freezes old values)."""
        try:
            from .db import kv_get

            over = kv_get("symbols")
            if over:
                return list(over)
        except Exception:  # noqa: BLE001  (database not ready yet)
            pass
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]

    @property
    def symbol_map_dict(self) -> dict:
        if not self.symbol_map.strip():
            return {}
        try:
            return json.loads(self.symbol_map)
        except json.JSONDecodeError:
            return {}

    @property
    def weights_dict(self) -> dict:
        w = dict(DEFAULT_WEIGHTS)
        if self.weights.strip():
            try:
                w.update({k: float(v) for k, v in json.loads(self.weights).items() if k in w})
            except (json.JSONDecodeError, ValueError, AttributeError):
                pass
        return w

    @property
    def feed_list(self) -> list[str]:
        if self.news_feeds.strip():
            return [f.strip() for f in self.news_feeds.split(",") if f.strip()]
        return DEFAULT_FEEDS

    def broker_symbol(self, symbol: str) -> str:
        return self.symbol_map_dict.get(symbol, symbol)


# "fast" trades often: looser entry filters and a short Claude cooldown. The loss limits, the news blackout, the stop
# and spread cost checks, Claude's YES/NO and the Live lock are the same in both profiles.
PROFILES = {
    "fast": {"score_threshold": 20.0, "min_agree_pct": 50.0, "min_atr_to_spread": 1.5, "max_stop_atr": 2.5, "auto_tf_atr_spreads": 2.5,
             "claude_cooldown_min": 5, "min_confidence": 0.55, "max_open": 5, "min_rr": 1.1,
             "stop_atr_mult": 1.0, "rr_target": 1.5, "max_tp_atr": 1.5},
    "normal": {},
}


@lru_cache
def _base() -> Settings:
    return Settings()


def profile_name() -> str:
    base = _base()
    try:
        from .db import kv_get

        p = kv_get("profile")
        if p in PROFILES:
            return p
    except Exception:  # noqa: BLE001  (database not ready yet)
        pass
    return base.profile if base.profile in PROFILES else "fast"


def get_settings() -> Settings:
    base = _base()
    over = PROFILES.get(profile_name(), {})
    return base.model_copy(update=over) if over else base


get_settings.cache_clear = _base.cache_clear  # type: ignore[attr-defined]


def tf_label(minutes: int | None = None) -> str:
    m = minutes or get_settings().timeframe_min
    return f"H{m // 60}" if m % 60 == 0 else f"M{m}"
