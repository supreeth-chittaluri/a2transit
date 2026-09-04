# Two stages so the runtime image carries no compiler, no build cache and no
# test suite. ~375 MB finished, most of which is psycopg's bundled libpq, the
# protobuf runtime and GEOS via geoalchemy2 — comfortably inside what Fly and
# Render allow on a free plan.
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml ./
COPY a2transit ./a2transit

# Installed into a venv rather than the system site-packages so the whole
# dependency tree copies to the runtime stage as one directory.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Not root. Nothing here needs to write to the image at runtime.
RUN useradd --create-home --uid 10001 a2transit
COPY --from=build /opt/venv /opt/venv

# No source in the runtime image: the package is installed in the venv, and a
# second copy on the working directory would shadow it — so a stale file left
# behind by a bad build would be the one that ran.
WORKDIR /srv
USER a2transit

EXPOSE 8000

# One worker by default. The timetable cache is per-process, so four workers is
# four copies of the same tables — scale with instances, not workers, and see
# TIMETABLE_CACHE_SIZE for sizing a small one.
#
# $PORT because Render (and Heroku, and Cloud Run) assign the port and expect
# the process to bind it. Hardcoding 8000 works — the platform detects the open
# port eventually — but costs a network-reconfigure and restart on every deploy.
#
# `sh -c` because exec-form CMD does no variable expansion, and `exec` inside it
# because without it `sh` stays PID 1 and swallows SIGTERM: the container would
# be SIGKILLed after the grace period instead of shutting down cleanly, and the
# inline poller's shutdown — which cancels its task and waits for the current
# cycle to finish rather than dying mid-write to Redis — would never run.
CMD ["sh", "-c", "exec uvicorn a2transit.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
