"""Database models and small helpers. Everything the desk decides is journaled here."""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(DeclarativeBase):
    pass


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    cycle_id: Mapped[str] = mapped_column(String(32), index=True, default="")
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), default="H1")
    candle_time: Mapped[str] = mapped_column(String(32), default="")
    bias: Mapped[str] = mapped_column(String(8), default="NONE")  # BUY | SELL | NONE
    score: Mapped[float] = mapped_column(Float, default=0.0)
    agree: Mapped[int] = mapped_column(Integer, default=0)
    available: Mapped[int] = mapped_column(Integer, default=0)
    factors: Mapped[dict] = mapped_column(JSON, default=dict)  # name -> {score, weight, reading, source}
    levels: Mapped[dict] = mapped_column(JSON, default=dict)  # entry, sl, tp, atr, rr
    context: Mapped[dict] = mapped_column(JSON, default=dict)  # what Claude was shown, plus closes for the chart
    claude: Mapped[dict] = mapped_column(JSON, default=dict)
    gate: Mapped[dict] = mapped_column(JSON, default=dict)
    # no_trade | gate_blocked | claude_no | alerted | pending_approval | executed | rejected | expired | error | skipped
    status: Mapped[str] = mapped_column(String(24), default="no_trade", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    lots: Mapped[float] = mapped_column(Float, default=0.0)
    ticket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="")
    venue: Mapped[str] = mapped_column(String(8), default="", index=True)  # paper | demo | live | "" (analysis only)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Position(Base):
    __tablename__ = "positions"
    ticket: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    venue: Mapped[str] = mapped_column(String(8), primary_key=True, default="demo")  # tickets of different accounts may coincide
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    lots: Mapped[float] = mapped_column(Float)
    entry: Mapped[float] = mapped_column(Float)
    sl: Mapped[float] = mapped_column(Float)
    tp: Mapped[float] = mapped_column(Float)
    sl0: Mapped[float | None] = mapped_column(Float, nullable=True)  # the first stop, kept once the trailing stop moves sl
    risk_usd: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)  # realized, after commission and swap
    floating: Mapped[float] = mapped_column(Float, default=0.0)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(8), default="open", index=True)


class Activity(Base):
    __tablename__ = "activity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")  # info | warn | error | trade
    symbol: Mapped[str] = mapped_column(String(16), default="")
    text: Mapped[str] = mapped_column(Text)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class NewsItem(Base):
    __tablename__ = "news"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(64), unique=True)
    published: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(80), default="")
    title: Mapped[str] = mapped_column(Text)
    link: Mapped[str] = mapped_column(Text, default="")
    central_bank: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # symbol -> -1, 0, 1 once tagged
    tagged_by: Mapped[str] = mapped_column(String(16), default="")


class KV(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


_engine = None
_Session = None


def get_engine():
    global _engine, _Session
    if _engine is None:
        url = get_settings().database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs = {"connect_args": {"check_same_thread": False}}
        _engine = create_engine(url, **kwargs)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine():
    """Used by tests to point at a fresh database."""
    global _engine, _Session
    _engine = None
    _Session = None


def session():
    get_engine()
    return _Session()


def init_db():
    eng = get_engine()
    Base.metadata.create_all(eng)
    # create_all does not alter existing tables: add the venue columns to databases created before venues existed
    from sqlalchemy import inspect, text

    insp = inspect(eng)
    with eng.begin() as conn:
        for table, default in (("signals", ""), ("positions", "demo")):
            if "venue" not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN venue VARCHAR(8) NOT NULL DEFAULT '{default}'"))
        if "sl0" not in {c["name"] for c in insp.get_columns("positions")}:
            conn.execute(text("ALTER TABLE positions ADD COLUMN sl0 FLOAT"))
        # tickets of different accounts can coincide, so the key of positions becomes (ticket, venue) (Postgres; SQLite is dev only)
        if eng.dialect.name == "postgresql":
            pk = insp.get_pk_constraint("positions")
            if pk.get("constrained_columns") == ["ticket"]:
                conn.execute(text(f'ALTER TABLE positions DROP CONSTRAINT "{pk["name"]}"'))
                conn.execute(text("ALTER TABLE positions ADD PRIMARY KEY (ticket, venue)"))


def kv_get(key: str, default=None):
    with session() as s:
        row = s.get(KV, key)
        return row.value if row else default


def kv_get_row(key: str):
    with session() as s:
        row = s.get(KV, key)
        return (row.value, row.updated) if row else (None, None)


def kv_set(key: str, value) -> None:
    with session() as s:
        row = s.get(KV, key)
        if row:
            row.value = value
            row.updated = utcnow()
        else:
            s.add(KV(key=key, value=value, updated=utcnow()))
        s.commit()


def bump_counter(key: str) -> int:
    """Increment and return a counter stored in the KV table."""
    n = int(kv_get(key, 0) or 0) + 1
    kv_set(key, n)
    return n


def log(text: str, level: str = "info", symbol: str = "", signal_id: int | None = None) -> None:
    with session() as s:
        s.add(Activity(text=text, level=level, symbol=symbol, signal_id=signal_id))
        s.commit()


def latest_signals_per_symbol(symbols: list[str]) -> list[Signal]:
    out = []
    with session() as s:
        for sym in symbols:
            row = s.scalars(select(Signal).where(Signal.symbol == sym).order_by(Signal.id.desc()).limit(1)).first()
            if row:
                out.append(row)
    return out
