import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "MT5_MOCK": "true",
    "ADMIN_PASSWORD": "secret",
    "SCHEDULER_ENABLED": "false",
    "ANTHROPIC_API_KEY": "test-key",
    "MODE": "demo_auto",
    "TIMEFRAME_MIN": "60",   # the older tests model hourly candles; the M3 behaviour has its own tests
    "PROFILE": "normal",     # the older tests use the stricter values
    "AUTO_TF": "false",      # the older tests pin one chart; auto-chart has its own test
})


@pytest.fixture(autouse=True)
def fresh_db():
    from app import broker, db
    from app.config import get_settings

    get_settings.cache_clear()
    db.reset_engine()
    db.get_engine()
    db.Base.metadata.drop_all(db.get_engine())
    db.init_db()
    broker._brokers.clear()
    broker.set_broker(broker.MockBroker())
    from app import collectors

    from app import engine

    engine._strat_sig.clear()
    db.kv_set("strategy_mode", False)   # the older tests exercise the score-only entries; strategy mode has its own tests
    db.kv_set("proven_only", False)   # the older tests exercise unproven trading; the proven-only default has its own test
    db.kv_set("claude_veto", True)   # the older tests exercise Claude's veto; its switch has its own test
    collectors.save_snapshot("calendar", [])  # a loaded, empty calendar: no events, so no blackout
    yield
