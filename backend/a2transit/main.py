"""FastAPI application entrypoint.

    uvicorn a2transit.main:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from a2transit import __version__
from a2transit.api import health, places, plan, stops
from a2transit.config import get_settings

# Displayed at /docs, and the attribution TheRide's data licence requires wherever
# their data is surfaced. See docs/feeds.md.
ATTRIBUTION = (
    "Transit scheduling, geographic, and real-time data provided by "
    "permission of AAATA/TheRide. Campus transit data from University of "
    "Michigan Transit Services."
)


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="a2transit",
        version=__version__,
        summary="Door-to-door journey planning across Ann Arbor's two transit agencies.",
        description=(
            "Routes across TheRide (AAATA) and U-M MBus as a single network, "
            "including transfers between them.\n\n" + ATTRIBUTION
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(plan.router)
    app.include_router(stops.router)
    app.include_router(places.router)
    return app


app = create_app()
