"""Feed definitions and downloading.

Downloads are cached under `data/` with a sidecar recording the upstream ETag
and Last-Modified, so a weekly refresh revalidates with a conditional GET and
transfers nothing when the agency has not republished. TheRide's licence
requires refreshing within three business days of a new file; being cheap to run
is what makes running it often realistic.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from a2transit.config import Settings, get_settings
from a2transit.db.models import AgencySource

logger = logging.getLogger(__name__)

USER_AGENT = "a2transit-ingest/0.1 (+https://github.com/a2transit)"
DOWNLOAD_TIMEOUT = 120.0


@dataclass(frozen=True)
class FeedSpec:
    agency_source: AgencySource
    label: str
    gtfs_url: str


def feed_specs(settings: Settings | None = None) -> tuple[FeedSpec, ...]:
    settings = settings or get_settings()
    return (
        FeedSpec(AgencySource.THERIDE, "TheRide (AAATA)", settings.theride_gtfs_url),
        FeedSpec(AgencySource.MBUS, "U-M MBus", settings.mbus_gtfs_url),
    )


def feed_spec_for(agency: AgencySource, settings: Settings | None = None) -> FeedSpec:
    for spec in feed_specs(settings):
        if spec.agency_source is agency:
            return spec
    raise KeyError(agency)


@dataclass(frozen=True)
class DownloadedFeed:
    """A GTFS ZIP on local disk, with the provenance the audit trail needs."""

    spec: FeedSpec
    path: Path
    sha256: str
    content_length: int
    last_modified: str | None
    fetched_at: dt.datetime
    #: True when the upstream answered 304, or a local file was supplied.
    from_cache: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_paths(cache_dir: Path, spec: FeedSpec) -> tuple[Path, Path]:
    return (
        cache_dir / f"{spec.agency_source.value}.zip",
        cache_dir / f"{spec.agency_source.value}.meta.json",
    )


def download_feed(
    spec: FeedSpec,
    cache_dir: Path,
    *,
    force: bool = False,
    client: httpx.Client | None = None,
) -> DownloadedFeed:
    """Fetch the feed, revalidating against the cache unless `force`."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path, meta_path = _cache_paths(cache_dir, spec)

    cached_meta: dict = {}
    if zip_path.exists() and meta_path.exists() and not force:
        try:
            cached_meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            logger.warning("cache metadata for %s is unreadable; refetching", spec.label)

    headers = {"User-Agent": USER_AGENT}
    if cached_meta.get("etag"):
        headers["If-None-Match"] = cached_meta["etag"]
    if cached_meta.get("last_modified"):
        headers["If-Modified-Since"] = cached_meta["last_modified"]

    owns_client = client is None
    client = client or httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
    try:
        response = client.get(spec.gtfs_url, headers=headers)
    finally:
        if owns_client:
            client.close()

    fetched_at = dt.datetime.now(tz=dt.UTC)

    if response.status_code == httpx.codes.NOT_MODIFIED:
        logger.info("%s: unchanged upstream (304), using cached %s", spec.label, zip_path.name)
        return DownloadedFeed(
            spec=spec,
            path=zip_path,
            sha256=cached_meta.get("sha256") or _sha256(zip_path),
            content_length=zip_path.stat().st_size,
            last_modified=cached_meta.get("last_modified"),
            fetched_at=fetched_at,
            from_cache=True,
        )

    response.raise_for_status()

    # Write to a temporary path and move into place, so an interrupted download
    # cannot leave a truncated ZIP that the next run happily revalidates.
    temp_path = zip_path.with_suffix(".zip.part")
    temp_path.write_bytes(response.content)
    temp_path.replace(zip_path)

    digest = _sha256(zip_path)
    last_modified = response.headers.get("last-modified")
    meta_path.write_text(
        json.dumps(
            {
                "url": spec.gtfs_url,
                "etag": response.headers.get("etag"),
                "last_modified": last_modified,
                "sha256": digest,
                "content_length": zip_path.stat().st_size,
                "fetched_at": fetched_at.isoformat(),
            },
            indent=2,
        )
    )

    logger.info(
        "%s: downloaded %.2f MB (modified %s)",
        spec.label,
        zip_path.stat().st_size / 1_048_576,
        last_modified or "unknown",
    )
    return DownloadedFeed(
        spec=spec,
        path=zip_path,
        sha256=digest,
        content_length=zip_path.stat().st_size,
        last_modified=last_modified,
        fetched_at=fetched_at,
        from_cache=False,
    )


def local_feed(spec: FeedSpec, path: Path) -> DownloadedFeed:
    """Wrap an existing ZIP on disk — for `--from-file` and for tests."""
    return DownloadedFeed(
        spec=spec,
        path=path,
        sha256=_sha256(path),
        content_length=path.stat().st_size,
        last_modified=None,
        fetched_at=dt.datetime.now(tz=dt.UTC),
        from_cache=True,
    )
