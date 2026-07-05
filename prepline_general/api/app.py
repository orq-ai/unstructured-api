import logging
import os
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.datastructures import FormData
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from .general import router as general_router
from .openapi import set_custom_openapi
from .parse_markdown import router as parse_markdown_router
from .pdf_extractor import router as pdf_extractor_router
from .services.nats_service import start_nats, stop_nats

logger = logging.getLogger("unstructured_api")

_ENVIRONMENT = os.environ.get("ENVIRONMENT", "localhost")
_NATS_ENABLED = os.environ.get("ORQ_NATS_ENABLED", "true").lower() == "true"


def _get_cors_allowed_origins() -> list[str]:
    configured_origins = os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "https://my.orq.ai,https://my.staging.orq.ai",
    )
    allowed_origins = [
        origin.strip()
        for origin in configured_origins.split(",")
        if origin.strip() and origin.strip() != "*"
    ]

    return allowed_origins or ["https://my.orq.ai"]


_sentry_dsn = os.environ.get("SENTRY_DSN", "")
if _sentry_dsn:
    sentry_sdk.init(
        environment=_ENVIRONMENT,
        dsn=_sentry_dsn,
        # Sampling hardcoded at 1.0 is fine for staging but expensive in
        # production and captures every request; make it configurable.
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        profiles_sample_rate=float(os.environ.get("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
        # This service exists to process documents that contain exactly the
        # PII the delete_* options redact. Never ship request bodies (the
        # documents themselves) or user PII to Sentry.
        max_request_body_size="never",
        send_default_pii=False,
        integrations=[
            StarletteIntegration(
                transaction_style="endpoint",
                failed_request_status_codes=[403, range(500, 599)],
            ),
            FastApiIntegration(
                transaction_style="endpoint",
                failed_request_status_codes=[403, range(500, 599)],
            ),
        ],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events."""
    if _NATS_ENABLED:
        # Let a NATS failure fail the boot loudly: half-started replicas that
        # serve HTTP but silently never consume commands are worse than a
        # crash-looping pod. Set ORQ_NATS_ENABLED=false for HTTP-only runs.
        await start_nats()
    try:
        yield
    finally:
        if _NATS_ENABLED:
            try:
                await stop_nats()
            except Exception:
                logger.error("Error stopping NATS service", exc_info=True)


app = FastAPI(
    title="Unstructured Pipeline API",
    summary="Partition documents with the Unstructured library",
    version="0.0.82",
    docs_url="/general/docs",
    openapi_url="/general/openapi.json",
    servers=[
        {
            "url": "https://api.unstructured.io",
            "description": "Hosted API",
            "x-speakeasy-server-id": "prod",
        },
        {
            "url": "http://localhost:8000",
            "description": "Development server",
            "x-speakeasy-server-id": "local",
        },
    ],
    openapi_tags=[{"name": "general"}, {"name": "pdf_extractor"}],
    lifespan=lifespan,
)

# NOTE: uvicorn.error is deliberately NOT disabled anymore. It used to be
# switched off in staging/production to suppress duplicate exception dumps,
# but that logger also carries "Application startup failed" and bind errors —
# with NATS in the lifespan, a boot failure became completely silent. The
# app-level exception handlers below already prevent duplicate tracebacks,
# because a handled exception never propagates back to uvicorn.


# Catch all HTTPException for uniform logging and response
@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, e: HTTPException):
    # 4xx are the caller's problem; only 5xx are ours. Logging every 401/422
    # at ERROR buried real errors and let any client spam the error stream.
    log = logger.error if e.status_code >= 500 else logger.info
    log("HTTP %s on %s %s: %s", e.status_code, request.method, request.url.path, e.detail)
    # Preserve headers: dropping them stripped WWW-Authenticate from 401s
    # (RFC 6750) and would eat Retry-After on 429/503s.
    return JSONResponse(
        status_code=e.status_code,
        content={"detail": e.detail},
        headers=e.headers,
    )


# Catch any other errors and return as 500
@app.exception_handler(Exception)
async def error_handler(request: Request, e: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_allowed_origins(),
    allow_credentials=False,
    # The API is form-POST + preflight; there is no reason to advertise
    # PUT/DELETE/PATCH support to browsers.
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)
app.include_router(general_router)
app.include_router(pdf_extractor_router, prefix="/extract", tags=["extract"])
app.include_router(parse_markdown_router, prefix="/parse-markdown", tags=["parse-markdown"])

set_custom_openapi(app)

# Note(austin) - When FastAPI parses our FormData params,
# it builds lists out of duplicate keys, like so:
# FormData([('key', 'value1'), ('key', 'value2')])
#
# The Speakeasy clients send a more explicit form:
# FormData([('key[]', 'value1'), ('key[]', 'value2')])
#
# FastAPI doesn't understand these, so we need to transform them.
# Can't do this in middleware before the data stream is read, nor in the endpoint
# after the fields are parsed. Thus, we have to patch it into Request.form() on startup.
get_form = Request._get_form


async def patched_get_form(self, *args, **kwargs) -> FormData:
    """Call the original get_form and strip trailing "[]" from keys.

    Accepts and FORWARDS whatever arguments the caller passes. The previous
    version declared max_files/max_fields "to match the signature" but then
    dropped them, so any parsing limits FastAPI passed were silently ignored
    — and the hardcoded signature would break whenever starlette adds a
    parameter (it has since grown max_part_size).
    """
    form_params = await get_form(self, *args, **kwargs)

    fixed_params = []
    for key, value in form_params.multi_items():
        # Transform key[] into key
        if key and key.endswith("[]"):
            key = key[:-2]

        fixed_params.append((key, value))

    return FormData(fixed_params)


# Replace the private method with our wrapper
Request._get_form = patched_get_form  # type: ignore[assignment]


# Filter out /healthcheck and /metrics noise
class _PathNoiseFilter(logging.Filter):
    _NOISY = ("/healthcheck", "/metrics")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(path in message for path in self._NOISY)


logging.getLogger("uvicorn.access").addFilter(_PathNoiseFilter())


@app.get("/healthcheck", status_code=status.HTTP_200_OK, include_in_schema=False)
def healthcheck():
    return {"healthcheck": "HEALTHCHECK STATUS: EVERYTHING OK!"}


logger.info("Started Unstructured API")
