"""Prometheus metrics for the unstructured API.

Every HTTP endpoint gets request-rate / latency / status metrics automatically
via the prometheus-fastapi-instrumentator wired up in ``app.py`` (exposed at
``/metrics``). This module adds the domain- and security-specific signals that
auto-instrumentation cannot infer:

* ``auth`` outcomes (the auth layer was previously silent on failures),
* files processed per endpoint with detected filetype and outcome,
* uploaded file size and processing latency,
* requests blocked before/within processing (unsupported filetype, invalid
  object reference, parser sandbox blocks) — these are the probing / exploit
  signals surfaced by the RST LFD/SSRF hardening,
* object-storage downloads (the ``/extract`` path),
* the NATS knowledge consumer (messages handled + connection state).

Label cardinality is kept bounded on purpose: ``reason``/``outcome``/``filetype``
are drawn from small fixed sets, and we never label by ``workspace_id`` or
filename (unbounded / sensitive).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_NS = "unstructured"

# -- security: JWT auth outcomes (auth.py raised but logged/metered nothing) --
# result: "success" | "failure"
# reason: ok | missing_header | invalid_token | expired | untrusted_issuer
#         | missing_workspace | not_configured
AUTH_TOTAL = Counter(
    f"{_NS}_auth_total",
    "JWT auth attempts by result and failure reason.",
    ["result", "reason"],
)

# -- files processed per endpoint --
# endpoint: general | extract | parse_markdown
# outcome: ok | error | rejected
FILES_TOTAL = Counter(
    f"{_NS}_files_total",
    "Files/documents processed by endpoint, detected filetype, and outcome.",
    ["endpoint", "filetype", "outcome"],
)

FILE_BYTES = Histogram(
    f"{_NS}_file_bytes",
    "Uploaded payload size in bytes by endpoint.",
    ["endpoint"],
    buckets=(1e3, 1e4, 1e5, 5e5, 1e6, 5e6, 1e7, 5e7, 1e8),
)

PROCESS_SECONDS = Histogram(
    f"{_NS}_process_seconds",
    "End-to-end processing duration by endpoint.",
    ["endpoint"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)

# -- security: blocked / rejected requests (probing + exploit-attempt signal) --
# reason: unsupported_filetype | invalid_object_ref | conflict_media_type
#         | sandbox_blocked
BLOCKED_TOTAL = Counter(
    f"{_NS}_blocked_total",
    "Requests blocked before or during processing (security + validation).",
    ["endpoint", "reason"],
)

# -- object storage (the /extract download path) --
# outcome: ok | not_found | error
STORAGE_OPS_TOTAL = Counter(
    f"{_NS}_storage_ops_total",
    "Object-storage download attempts by outcome.",
    ["outcome"],
)

# -- NATS knowledge consumer (non-HTTP path) --
# outcome: processed | skipped | error
NATS_MESSAGES_TOTAL = Counter(
    f"{_NS}_nats_messages_total",
    "NATS messages handled by outcome.",
    ["outcome"],
)

NATS_CONNECTED = Gauge(
    f"{_NS}_nats_connected",
    "1 when the NATS client is connected, 0 otherwise.",
)
