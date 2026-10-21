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
CORRELATION_GROUPS = [["XAUUSD", "XAGUSD"], ["EURUSD", "GBPUSD"]]

MODES = ["research", "alerts", "demo_auto", "live_approval", "live_auto"]
LIVE_MODES = {"live_approval", "live_auto"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # storage
    database_url: str = "sqlite:///./fino.db"

    # broker bridge (MT5 under Wine, reached over RPyC on the stack's private network)
    mt5_host: str = "mt5"
    mt5_port: int = 8001
    mt5_mock: bool = False
    broker_utc_offset_hours: str = ""  # leave empty to auto-detect from candles
    magic: int = 26102026
    deviation: int = 20

    # what to trade
    symbols: str = "XAUUSD,XAGUSD,EURUSD,GBPUSD"
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
    min_agree: int = 4
    stop_atr_mult: float = 1.5
    rr_target: float = 2.0

    # risk gate
    risk_per_trade_pct: float = 0.5
    daily_loss_cap_pct: float = 2.0
    max_open: int = 3
    min_rr: float = 1.5
    max_spread_to_stop: float = 0.10
    min_confidence: float = 0.6
    max_loss_streak: int = 5
    blackout_min: int = 15

    # timing
    cycle_delay_seconds: int = 15
    run_cycle_on_start: bool = False
    scheduler_enabled: bool = True

    # access and alerts
    admin_user: str = "admin"
    admin_password: str = ""
    notify_webhook_url: str = ""

    @property
    def symbol_list(self) -> list[str]:
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
