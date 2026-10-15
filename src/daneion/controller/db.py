"""SQLite (WAL) schema; only the controller touches it (ADR-0002)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

SCHEMA_VERSION = 2


class Base(DeclarativeBase):
    pass


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


class Worker(Base):
    __tablename__ = "workers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    is_local: Mapped[bool] = mapped_column(default=False)
    state: Mapped[str] = mapped_column(String)
    os: Mapped[str] = mapped_column(String, default="")
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    slots: Mapped[int] = mapped_column(Integer, default=1)
    # Measured per-slot speed by handler, learned from finished jobs (FitCheck "observed").
    perf: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    paired_at: Mapped[float] = mapped_column(Float)
    last_seen_at: Mapped[float] = mapped_column(Float)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    handler: Mapped[str] = mapped_column(String)
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    mode: Mapped[str] = mapped_column(String)  # requested: local | distributed | auto
    placement: Mapped[str | None] = mapped_column(String, nullable=True)  # decided
    fitcheck: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    result_path: Mapped[str | None] = mapped_column(String, nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    fence: Mapped[int] = mapped_column(Integer, default=0)
    current_attempt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    eligible_at: Mapped[float] = mapped_column(Float, default=0.0)
    est_units: Mapped[float | None] = mapped_column(Float, nullable=True)  # FitCheck estimate
    output_path: Mapped[str | None] = mapped_column(String, nullable=True)
    output_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    job_id: Mapped[str] = mapped_column(String, index=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"), index=True)
    fence: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String, index=True)
    leased_at: Mapped[float] = mapped_column(Float)
    lease_expires_at: Mapped[float] = mapped_column(Float)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Benchmark(Base):
    """Proof-of-Speed run: the same job repeated in local and distributed mode."""

    __tablename__ = "benchmarks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    handler: Mapped[str] = mapped_column(String)
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    runs: Mapped[int] = mapped_column(Integer)
    warmup: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String)  # RUNNING | DONE | FAILED | CANCELLED
    created_at: Mapped[float] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EventRow(Base):
    """Append-only timeline written in the same transaction as the state change (outbox)."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, index=True)
    kind: Mapped[str] = mapped_column(String)
    job_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Database:
    """Engine plus a process-wide write lock: SQLite has one writer anyway, and holding the lock
    for the whole read-modify-write keeps every state transition serialisable."""

    def __init__(self, path: Path | str) -> None:
        url = "sqlite://" if str(path) == ":memory:" else f"sqlite:///{Path(path).as_posix()}"
        kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
        if str(path) == ":memory:":
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **kwargs)
        event.listen(self.engine, "connect", _sqlite_pragmas)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.lock = threading.RLock()
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()
        with self.tx() as s:
            row = s.get(Meta, "schema_version")
            if row is None:
                s.add(Meta(key="schema_version", value=str(SCHEMA_VERSION)))
            elif int(row.value) > SCHEMA_VERSION:
                raise RuntimeError(f"database schema {row.value} is newer than this build "
                                   f"({SCHEMA_VERSION}); upgrade Daneion")
            else:
                row.value = str(SCHEMA_VERSION)

    def _add_missing_columns(self) -> None:
        """Additive migration: new nullable columns appear in old databases. (Renames and
        drops would need a real migration tool; the schema only grows during the MVP.)"""
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                have = {r[1] for r in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
                for col in table.columns:
                    if col.name not in have and col.nullable:
                        ddl = col.type.compile(dialect=self.engine.dialect)
                        conn.exec_driver_sql(
                            f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}')

    @contextmanager
    def tx(self) -> Iterator[Session]:
        with self.lock, self._sessions() as session, session.begin():
            yield session

    @contextmanager
    def read(self) -> Iterator[Session]:
        with self._sessions() as session:
            yield session


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()
