"""Alembic migration environment.

The database URL is resolved from application settings (DATABASE_URL) unless a
caller explicitly overrides `sqlalchemy.url` on the config object, which is what
the test-suite does to point migrations at a temporary database.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, event, pool

from alembic import context

# Make the `app` package importable regardless of how alembic was invoked.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings  # noqa: E402
from app.database import Base  # noqa: E402
from app import models  # noqa: E402,F401  (imported so Base.metadata is populated)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> str:
    override = config.get_main_option("sqlalchemy.url", default=None)
    if override:
        return override
    # Instantiated fresh (not the cached module-level singleton) so that a
    # DATABASE_URL set for this process is always honoured.
    return Settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _make_sqlite_ddl_transactional(connectable) -> None:
    """Give SQLite migrations a real transaction, including for DDL.

    pysqlite does not emit BEGIN before DDL, so without this a migration that
    failed halfway through a table rebuild would leave the half it had done. The
    standard SQLAlchemy recipe: put the driver in autocommit mode and emit BEGIN
    ourselves. Registered on this engine only — the application's engine is
    untouched.
    """

    @event.listens_for(connectable, "connect")
    def _autocommit_driver(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        dbapi_connection.isolation_level = None

    @event.listens_for(connectable, "begin")
    def _emit_begin(conn) -> None:  # noqa: ANN001
        conn.exec_driver_sql("BEGIN")


def _set_sqlite_foreign_keys(connection, enabled: bool) -> None:
    """Set the pragma directly on the driver connection, outside any transaction.

    `PRAGMA foreign_keys` is a no-op inside a transaction, so it must be issued
    before BEGIN (and after COMMIT/ROLLBACK) — hence the raw DBAPI connection.
    """
    raw = connection.connection.dbapi_connection
    raw.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {}) or {}
    section["sqlalchemy.url"] = get_database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    is_sqlite = connectable.dialect.name == "sqlite"
    if is_sqlite:
        _make_sqlite_ddl_transactional(connectable)

    with connectable.connect() as connection:
        # Table rebuilds (batch mode) DROP the original table; with foreign keys
        # enforced that DROP cascades into child tables. They are disabled for this
        # migration connection only, and restored in `finally` whether or not the
        # migration succeeded. Migrations that rebuild tables check the pragma
        # themselves and run `PRAGMA foreign_key_check` before committing.
        if is_sqlite:
            _set_sqlite_foreign_keys(connection, False)
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                # SQLite cannot ALTER most things in place; batch mode keeps future
                # migrations workable.
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            if is_sqlite:
                if connection.in_transaction():
                    connection.rollback()
                _set_sqlite_foreign_keys(connection, True)

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
