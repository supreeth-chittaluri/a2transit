"""Application settings, loaded from the environment (see .env.example)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5174"

    database_url: str = "postgresql+psycopg://a2transit:a2transit@localhost:5432/a2transit"
    redis_url: str = "redis://localhost:6379/0"

    # Feed URLs verified 2026-09-03; see docs/feeds.md for provenance.
    theride_gtfs_url: str = "https://www.theride.org/sites/default/files/google/google_transit.zip"
    theride_gtfsrt_vehicles_url: str = "https://rt.theride.org/gtfsrt/vehicles"
    theride_gtfsrt_trips_url: str = "https://rt.theride.org/gtfsrt/trips"
    theride_gtfsrt_alerts_url: str = "https://rt.theride.org/gtfsrt/alerts"

    mbus_gtfs_url: str = "https://webapps.fo.umich.edu/transit_uploads/google_transit.zip"
    mbus_gtfsrt_vehicles_url: str = "https://mbus.ltp.umich.edu/gtfsrt/vehicles"
    mbus_gtfsrt_trips_url: str = "https://mbus.ltp.umich.edu/gtfsrt/trips"
    mbus_gtfsrt_alerts_url: str = "https://mbus.ltp.umich.edu/gtfsrt/alerts"

    realtime_poll_seconds: int = 20

    #: Poll the agency feeds from inside the API process instead of a separate
    #: worker. Off by default: a deployment that runs `python -m a2transit.realtime`
    #: must not also poll per web process, and a host running more than one
    #: uvicorn worker must never enable it — that is one poller per worker
    #: hitting somebody else's unauthenticated endpoint N times over.
    #:
    #: Exists because Render's free plan has no worker tier. See
    #: a2transit.realtime.inline.
    realtime_inline_poll: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached so settings are read from the environment exactly once per process."""
    return Settings()
