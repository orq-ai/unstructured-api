from fastapi import FastAPI, Request, status, HTTPException
from fastapi.datastructures import FormData
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import logging
import os
import sentry_sdk

from .general import router as general_router
from .openapi import set_custom_openapi
from fastapi.middleware.cors import CORSMiddleware
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from prometheus_fastapi_instrumentator import Instrumentator

from .request_context import request_context
from .pdf_extractor import router as pdf_extractor_router
from .parse_markdown import router as parse_markdown_router
from .services.nats_service import start_nats, stop_nats

logger = logging.getLogger("unstructured_api")


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


sentry_sdk.init(
    environment=os.environ.get("ENVIRONMENT", "localhost"),
    dsn=os.environ.get("SENTRY_DSN", ""),
    # Set traces_sample_rate to 1.0 to capture 100%
    # of transactions for tracing.
    traces_sample_rate=1.0,
    # Set profiles_sample_rate to 1.0 to profile 100%
    # of sampled transactions.
    # We recommend adjusting this value in production.
    profiles_sample_rate=1.0,
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
    """Handle startup and shutdown events"""
    try:
        await start_nats()
        yield
    finally:
        try:
            await stop_nats()
            logger.info("NATS service stopped successfully")
        except Exception as e:
            logger.error(f"Error stopping NATS service: {e}")


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

# Note(austin) - This logger just dumps exceptions
# We'd rather handle those below, so disable this in deployments
uvicorn_logger = logging.getLogger("uvicorn.error")

if os.environ.get("ENVIRONMENT") in ["staging", "production"]:
    uvicorn_logger.disabled = True


# Catch all HTTPException for uniform logging and response
@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, e: HTTPException):
    logger.error(e.detail)
    return JSONResponse(status_code=e.status_code, content={"detail": e.detail})


# Catch any other errors and return as 500
@app.exception_handler(Exception)
async def error_handler(request: Request, e: Exception):
    logger.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
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


async def patched_get_form(
    self,
    *,
    max_files: int | float = 1000,
    max_fields: int | float = 1000,
) -> FormData:
    """
    Call the original get_form, and iterate the results
    If a key has brackets at the end, remove them before returning the final FormData
    Note the extra params here are unused, but needed to match the signature
    """
    form_params = await get_form(self)

    fixed_params = []
    for key, value in form_params.multi_items():
        # Transform key[] into key
        if key and key.endswith("[]"):
            key = key[:-2]

        fixed_params.append((key, value))

    return FormData(fixed_params)


# Replace the private method with our wrapper
Request._get_form = patched_get_form  # type: ignore[assignment]


# Filter out /healthcheck noise
class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().find("/healthcheck") == -1


# Filter out /metrics noise
class MetricsCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().find("/metrics") == -1


logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())
logging.getLogger("uvicorn.access").addFilter(MetricsCheckFilter())


@app.middleware("http")
async def log_request_context(request: Request, call_next):
    """One structured access line per request enriched with Cloudflare context.

    Behind Cloudflare -> gateway -> mesh, the real caller is only visible via the
    CF-* headers; logging CF-Connecting-IP / CF-Ray / CF-IPCountry here gives the
    true client IP and a correlation id on every endpoint (the auth layer adds the
    same context to its rejection logs). Health/metrics are skipped to cut noise.
    """
    response = await call_next(request)
    path = request.url.path
    if path not in ("/healthcheck", "/metrics"):
        ctx = request_context(request)
        logger.info(
            "request method=%s path=%s status=%s ip=%s country=%s cf_ray=%s",
            request.method,
            path,
            response.status_code,
            ctx["ip"],
            ctx["country"],
            ctx["ray"],
        )
    return response


@app.get("/healthcheck", status_code=status.HTTP_200_OK, include_in_schema=False)
def healthcheck(request: Request):
    return {"healthcheck": "HEALTHCHECK STATUS: EVERYTHING OK!"}


# -- Prometheus: auto-instrument every endpoint (request count, latency, and
# exact status code) and expose /metrics. Exact status codes (not grouped) so
# security dashboards can alert on 401/400/500 rates per endpoint. Domain- and
# security-specific counters live in metrics.py and are incremented inside the
# individual handlers, the auth layer, and the NATS consumer.
Instrumentator(
    should_group_status_codes=False,
    excluded_handlers=["/metrics", "/healthcheck"],
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


logger.info("Started Unstructured API")
