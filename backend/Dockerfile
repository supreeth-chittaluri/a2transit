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

# One worker by default. The timetable cache is per-process and a service date
# costs ~120 MB resident, so four workers is four copies of the same tables.
# Scale with instances, not workers.
CMD ["uvicorn", "a2transit.main:app", "--host", "0.0.0.0", "--port", "8000"]
