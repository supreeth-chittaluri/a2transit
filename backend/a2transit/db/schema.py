"""Schema creation and teardown.

No Alembic yet, on purpose: the feeds are reloaded wholesale on every refresh,
so there is no accumulated data a migration would need to preserve. When the
schema stops being disposable — M8, when something is deployed and serving —
this is where a migration tool goes.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, text

from a2transit.db.models import Base

logger = logging.getLogger(__name__)

# postgis: geography/geometry types and ST_DWithin (M4 footpaths)
# pg_trgm:  trigram index behind /stops/search (M5)
#
# docker/initdb/01-extensions.sql already installs these when the container's
# data volume is first created. Repeating it here means a database created some
# other way — a hosted Neon or Supabase instance, a test fixture — works too.
REQUIRED_EXTENSIONS = ("postgis", "pg_trgm")


def ensure_extensions(engine: Engine) -> None:
    with engine.begin() as connection:
        for extension in REQUIRED_EXTENSIONS:
            connection.execute(text(f"CREATE EXTENSION IF NOT EXISTS {extension}"))


def create_all(engine: Engine) -> None:
    """Create extensions, then every table and index. Idempotent."""
    ensure_extensions(engine)
    Base.metadata.create_all(engine)
    logger.info("schema ready (%d tables)", len(Base.metadata.tables))


def drop_all(engine: Engine) -> None:
    """Drop every table and the agency_source enum type.

    checkfirst on the enum because SQLAlchemy does not always drop a named type
    that no surviving table references, and leaving it behind makes the next
    create_all fail with "type already exists".
    """
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TYPE IF EXISTS agency_source"))
