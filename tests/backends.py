"""Databases for tests: a SQLite file by default, or Postgres in a throwaway container.

Postgres test classes start one container per test run through testcontainers, and give
each test an empty database. They're skipped when Docker isn't available, or when
KANBOT_TEST_POSTGRES=0.
"""

import atexit
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

import sqlalchemy as sa

POSTGRES_IMAGE = "postgres:17-alpine"
_container = None
_container_error = None
_lock = threading.Lock()


def postgres_admin_url():
    """URL of the shared test container's default database, starting it on first use."""
    global _container, _container_error
    with _lock:
        if _container is None and _container_error is None:
            if os.environ.get("KANBOT_TEST_POSTGRES") == "0":
                _container_error = "KANBOT_TEST_POSTGRES=0"
            else:
                try:
                    from testcontainers.community.postgres import PostgresContainer

                    _container = PostgresContainer(POSTGRES_IMAGE, driver="psycopg").start()
                    atexit.register(_container.stop)
                except Exception as error:  # Docker missing, not running, or no access.
                    _container_error = f"Postgres container unavailable: {error}"
        if _container_error:
            raise unittest.SkipTest(_container_error)
        return _container.get_connection_url()


class SqliteBackend:
    """Mixin: self.new_database() returns a fresh SQLite file."""

    backend = "sqlite"

    def new_database(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return str(Path(tmp.name) / "test.db")

    def sql(self, statement, params=None):
        """Run SQL directly against the test database and return any rows as tuples."""
        engine = sa.create_engine(sa.URL.create("sqlite", database=self.db))
        return _run(engine, statement, params)


class PostgresBackend:
    """Mixin: self.new_database() returns the URL of a new, empty Postgres database."""

    backend = "postgresql"

    def new_database(self):
        admin = sa.create_engine(postgres_admin_url(), isolation_level="AUTOCOMMIT")
        name = f"test_{uuid.uuid4().hex[:12]}"
        with admin.connect() as conn:
            conn.execute(sa.text(f"CREATE DATABASE {name}"))

        def drop():
            with admin.connect() as conn:
                conn.execute(sa.text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            admin.dispose()

        self.addCleanup(drop)
        return admin.url.set(database=name).render_as_string(hide_password=False)

    def sql(self, statement, params=None):
        return _run(sa.create_engine(self.db), statement, params)


def _run(engine, statement, params):
    try:
        with engine.begin() as conn:
            result = conn.execute(sa.text(statement), params or {})
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        engine.dispose()
