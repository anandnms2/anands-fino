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
    broker.set_broker(broker.MockBroker())
    yield
