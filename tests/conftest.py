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

    collectors.save_snapshot("calendar", [])  # a loaded, empty calendar: no events, so no blackout
    yield
