import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import nats
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from ..models.knowledge import (
    UnstructuredProcessChunksCommand,
    UnstructuredProcessMarkdownCommand,
)
from ..services.message_processor import Chunk, process_markdown_message
from ..utils import compute_dispatch_nats_subject

logger = logging.getLogger(__name__)

_HANDLED_TYPE = "command.knowledge.process_unstructured_markdown"

# Queue group so multiple replicas of this service SHARE the work instead of
# each processing (and re-publishing) every message. Without this, running
# two replicas doubles every chunk event downstream.
_QUEUE_GROUP = os.getenv("ORQ_NATS_QUEUE_GROUP", "unstructured-markdown-workers")

# Bound how many documents are chunked at once; everything else waits.
_MAX_CONCURRENT = int(os.getenv("ORQ_NATS_MAX_CONCURRENT", "4"))

# Reject absurd inputs before they reach the chunker.
_MAX_MARKDOWN_BYTES = int(os.getenv("ORQ_MAX_MARKDOWN_BYTES", str(10 * 1024 * 1024)))
_MAX_CHUNK_CHARACTERS = 100_000
_ALLOWED_CHUNKING_STRATEGIES = {"basic", "by_title"}

nats_client: Optional[NATS] = None
_subscriptions: List[Subscription] = []
_inflight: Set["asyncio.Task[None]"] = set()
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

async def start_nats() -> None:
    logger.info("Starting NATS service...")
    await connect_nats()
    await subscribe_nats()
    logger.info("NATS service started successfully")


async def stop_nats() -> None:
    global nats_client
    # Stop accepting new messages first, then let in-flight work finish.
    for sub in _subscriptions:
        try:
            await sub.unsubscribe()
        except Exception:
            logger.warning("Failed to unsubscribe cleanly", exc_info=True)
    _subscriptions.clear()

    if _inflight:
        logger.info("Waiting for %d in-flight message(s)...", len(_inflight))
        _, pending = await asyncio.wait(_inflight, timeout=30)
        for task in pending:
            task.cancel()

    if nats_client:
        try:
            await nats_client.drain()
        except Exception:
            logger.warning("Error draining NATS connection", exc_info=True)
        nats_client = None
    logger.info("NATS service stopped successfully")


async def connect_nats() -> None:
    """Connect with nats.py's built-in reconnection.

    The client transparently reconnects (and replays subscriptions) on
    connection loss, which replaces the previous hand-rolled
    `reconnect_nats` loop. Credentials/TLS come from the environment; use a
    tls:// or nats://user:pass@host URL, or the explicit variables below.
    """
    global nats_client
    servers = os.getenv("ORQ_NATS_SERVER", "nats://localhost:4222").split(",")

    async def _disconnected_cb() -> None:
        logger.warning("NATS disconnected; client will attempt to reconnect")

    async def _reconnected_cb() -> None:
        conn = nats_client.connected_url.netloc if nats_client and nats_client.connected_url else "?"
        logger.info("NATS reconnected to %s", conn)

    async def _error_cb(e: Exception) -> None:
        logger.error("NATS error: %s", e)

    async def _closed_cb() -> None:
        logger.warning("NATS connection closed")

    try:
        nats_client = await nats.connect(
            servers=servers,
            name="unstructured-service",
            user=os.getenv("ORQ_NATS_USER") or None,
            password=os.getenv("ORQ_NATS_PASSWORD") or None,
            token=os.getenv("ORQ_NATS_TOKEN") or None,
            user_credentials=os.getenv("ORQ_NATS_CREDS_FILE") or None,
            max_reconnect_attempts=-1,  # keep trying forever
            reconnect_time_wait=2,
            connect_timeout=10,
        )
        # Callbacks passed post-hoc to keep the connect call readable.
        nats_client._disconnected_cb = _disconnected_cb  # type: ignore[attr-defined]
        nats_client._reconnected_cb = _reconnected_cb  # type: ignore[attr-defined]
        nats_client._error_cb = _error_cb  # type: ignore[attr-defined]
        nats_client._closed_cb = _closed_cb  # type: ignore[attr-defined]
        logger.info("Connected to NATS (%s)", ",".join(servers))
    except Exception as e:
        logger.error("Could not connect to NATS: %s", e)
        raise


async def disconnect_nats() -> None:
    global nats_client
    if nats_client:
        await nats_client.drain()  # drain() unsubscribes, flushes, and closes
        nats_client = None
        logger.info("Disconnected from NATS")


async def subscribe_nats(subject: str = "command.knowledge.>") -> None:
    if not nats_client:
        raise RuntimeError("NATS client not connected")

    subscription = await nats_client.subscribe(
        subject=subject,
        queue=_QUEUE_GROUP,
        cb=message_handler,
    )
    _subscriptions.append(subscription)
    logger.info("Subscribed to %s (queue group %s)", subject, _QUEUE_GROUP)


# --------------------------------------------------------------------------
# Message handling
# --------------------------------------------------------------------------

async def message_handler(msg: Msg) -> None:
    """Dispatch each message to its own bounded task.

    nats.py invokes callbacks sequentially per subscription; chunking a large
    document inline here would block every other message. Spawning tasks
    behind a semaphore gives bounded parallelism instead.
    """
    task = asyncio.create_task(_process_message(msg))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


async def _process_message(msg: Msg) -> None:
    async with _semaphore:
        correlation_id: Optional[str] = None
        try:
            data = json.loads(msg.data)
            msg_type = data.get("type")
            if msg_type != _HANDLED_TYPE:
                logger.debug("Skipping message with type: %r", msg_type)
                return

            command = UnstructuredProcessMarkdownCommand(**data)
            correlation_id = getattr(command, "correlationId", None)
            # Log identifiers, never the payload: the markdown routinely
            # contains exactly the PII the redaction options exist for.
            logger.info(
                "Processing %s (entityId=%s correlationId=%s, %d bytes)",
                _HANDLED_TYPE,
                command.entityId,
                correlation_id,
                len(msg.data),
            )

            markdown = command.data.get("markdown", "")
            if len(markdown.encode("utf-8", errors="ignore")) > _MAX_MARKDOWN_BYTES:
                raise ValueError(
                    f"markdown exceeds {_MAX_MARKDOWN_BYTES} bytes; refusing to process"
                )

            partition_params = _sanitize_chunking_options(
                command.data.get("chunking_options", {})
            )

            chunks = await process_markdown_message(markdown, **partition_params)
            logger.info("Generated %d chunks", len(chunks))

            await create_chunks_response(chunks, command)
            logger.info("Message processed and chunks published successfully")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Never let a bad message go silent: the upstream workflow is
            # waiting on a response keyed by correlationId. Raising here
            # (as before) only lands in nats.py's error callback and the
            # producer hangs forever.
            logger.error(
                "Error handling message (correlationId=%s): %s",
                correlation_id,
                e,
                exc_info=True,
            )
            await _publish_failure(msg, e)


def _sanitize_chunking_options(raw: Any) -> Dict[str, Any]:
    """Whitelist, type-check, and clamp caller-supplied chunking options.

    These values arrive over the message bus and are splatted into
    `process_markdown_message(**params)`; treat them as untrusted.
    """
    if not isinstance(raw, dict):
        raw = {}

    def _bounded_int(value: Any, lo: int, hi: int) -> Optional[int]:
        if value is None or isinstance(value, bool) or not isinstance(value, int):
            return None
        return max(lo, min(hi, value))

    strategy = raw.get("chunking_strategy")
    if strategy not in _ALLOWED_CHUNKING_STRATEGIES:
        strategy = None

    max_characters = _bounded_int(raw.get("chunk_max_characters"), 1, _MAX_CHUNK_CHARACTERS)
    overlap = _bounded_int(raw.get("chunk_overlap"), 0, _MAX_CHUNK_CHARACTERS)
    if max_characters is not None and overlap is not None:
        overlap = min(overlap, max_characters - 1) if max_characters > 1 else 0

    return {
        "max_characters": max_characters,
        "overlap": overlap,
        "chunking_strategy": strategy,
        "delete_emails": bool(raw.get("delete_emails", False)),
        "delete_credit_cards": bool(raw.get("delete_credit_cards", False)),
        "delete_phone_numbers": bool(raw.get("delete_phone_numbers", False)),
        "clean_bullet_points": bool(raw.get("clean_bullet_points", False)),
        "clean_numbered_list": bool(raw.get("clean_numbered_list", False)),
        "clean_dashes": bool(raw.get("clean_dashes", False)),
        "clean_whitespaces": bool(raw.get("clean_whitespaces", False)),
    }


async def create_chunks_response(
    chunks: List[Chunk], command: UnstructuredProcessMarkdownCommand
) -> Dict[str, Any]:
    """Build and publish the processed-chunks event for a command."""
    processed_chunks: List[Dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        content = chunk.get("content", "")
        metadata = chunk.get("metadata", {}) or {}
        processed_chunks.append(
            {
                "type": metadata.get("type", "unknown"),
                "element_id": f"chunk_{i}",
                "metadata": {
                    "words_count": len(content.split()),
                    "sentences_count": len(content.split(".")),
                    "paragraphs_count": len(content.split("\n\n")),
                    "tokens_count": len(content.split()),
                    "characters_count": len(content),
                    "chunks_count": metadata.get("total_chunks", len(chunks)),
                },
                "text": content,
            }
        )

    chunk_message: Dict[str, Any] = {
        "messageType": "command",
        "entityType": "knowledge",
        "type": "command.knowledge.processed_unstructured_chunks",
        "version": 1,
        "subject": "knowledge.processed_unstructured_chunks",
        # This event's own creation time; the causal link to the original
        # command is carried by correlationId/causationId, not the timestamp.
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entityId": command.entityId,
        "workspaceId": command.workspaceId,
        "correlationId": command.correlationId,
        "causationId": command.id,
        "data": {
            "knowledgeId": command.data.get("knowledgeId"),
            "datasourceId": command.data.get("datasourceId"),
            "chunks": processed_chunks,
        },
    }

    chunk_command = UnstructuredProcessChunksCommand(**chunk_message)
    await publish_nats_message(chunk_command.model_dump(mode="json"))
    logger.info("Published %d chunks in a single message", len(processed_chunks))
    return chunk_message


async def _publish_failure(msg: Msg, error: Exception) -> None:
    """Best-effort failure event so the producer can stop waiting.

    NOTE: the `type`/schema here must match whatever the orchestrator listens
    for; adjust to the platform's failure-event contract if one already
    exists.
    """
    try:
        raw = json.loads(msg.data)
    except Exception:
        raw = {}
    if not isinstance(raw, dict) or not raw.get("correlationId"):
        return  # nothing to correlate the failure to

    failure = {
        "messageType": "event",
        "entityType": "knowledge",
        "type": "event.knowledge.process_unstructured_markdown_failed",
        "version": 1,
        "subject": "knowledge.process_unstructured_markdown_failed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entityId": raw.get("entityId"),
        "workspaceId": raw.get("workspaceId"),
        "correlationId": raw.get("correlationId"),
        "causationId": raw.get("id"),
        "data": {
            "knowledgeId": (raw.get("data") or {}).get("knowledgeId"),
            "datasourceId": (raw.get("data") or {}).get("datasourceId"),
            # Exception class only — messages can embed payload fragments.
            "error": type(error).__name__,
        },
    }
    try:
        await publish_nats_message(failure)
    except Exception:
        logger.error("Failed to publish failure event", exc_info=True)


async def publish_nats_message(data: Dict[str, Any]) -> None:
    if not nats_client:
        raise RuntimeError("NATS client not connected")

    subject = compute_dispatch_nats_subject(data)
    payload = json.dumps(data).encode()
    await nats_client.publish(subject, payload)
    logger.info("Published message to %s", subject)
