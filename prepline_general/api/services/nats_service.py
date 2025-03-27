import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional, List

import nats
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from ..models.knowledge import UnstructuredProcessMarkdownCommand, UnstructuredProcessChunksCommand
from ..services.message_processor import Chunk, process_markdown_message
from ..utils import compute_dispatch_nats_subject

logger = logging.getLogger(__name__)

nats_client: Optional[NATS] = None
subscription_tasks: Dict[str, asyncio.Task[None]] = {}


async def start_nats() -> None:
    global nats_client
    try:
        logger.info("Starting NATS service...")
        await connect_nats()
        logger.info("NATS connected, subscribing...")
        await subscribe_nats()
        logger.info("NATS service started successfully")
    except Exception as e:
        logger.error(f"Error during NATS startup: {e}", exc_info=True)
        raise


async def stop_nats() -> None:
    global nats_client, subscription_tasks
    for task in subscription_tasks.values():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await disconnect_nats()
    logger.info("NATS service stopped successfully")


async def connect_nats() -> None:
    global nats_client
    orq_nats_server = os.getenv("ORQ_NATS_SERVER", "nats://localhost:4222")
    try:
        nats_client = await nats.connect(servers=orq_nats_server)  # type: ignore
        logger.info("Connected to NATS")
    except Exception as e:
        logger.error(f"Could not connect to NATS: {e}")
        raise


async def disconnect_nats() -> None:
    global nats_client
    if nats_client:
        await nats_client.drain()
        await nats_client.close()
        logger.info("Disconnected from NATS")


async def subscribe_nats(
    subject: str = "command.knowledge.>",
) -> None:
    global nats_client, subscription_tasks
    try:
        if not nats_client:
            raise RuntimeError("NATS client not connected")

        subscription: Subscription = await nats_client.subscribe(  # type: ignore
            subject=subject,
            cb=message_handler
        )
        
        task_key = subject
        if task_key in subscription_tasks:
            subscription_tasks[task_key].cancel()
            try:
                await subscription_tasks[task_key]
            except asyncio.CancelledError:
                pass

        subscription_tasks[task_key] = asyncio.create_task(
            process_subscription(subscription)
        )
        logger.info(f"Subscribed to {subject}")
    except Exception as e:
        logger.error(f"Error subscribing to NATS: {e}")
        raise


async def process_subscription(subscription: Subscription) -> None:
    try:
        while True:
            await asyncio.sleep(1)  # Keep the subscription alive
    except asyncio.CancelledError:
        await subscription.unsubscribe()
        logger.info("Subscription cancelled")


async def message_handler(msg: Msg) -> None:
    try:
        data = json.loads(msg.data.decode('utf-8', errors='replace'))
        logger.info(f"Message type: {data.get('type')}")
        
        # Filter for specific message type
        if data.get("type") != "command.knowledge.process_unstructured_markdown":
            logger.info(f"Skipping message with type: {data.get('type')}")
            return
            
        # Validate and parse the command
        command = UnstructuredProcessMarkdownCommand(**data)
        logger.info(f"Command validated successfully: {command}")
        
        # Process the markdown and get chunks
        markdown = command.data.get("markdown", "")
        chunking_options = command.data.get("chunking_options", {})
        
        # Map chunking options to partition parameters
        partition_params = {
            "max_characters": chunking_options.get("chunk_max_characters"),
            "overlap": chunking_options.get("chunk_overlap"),
            "chunking_strategy": chunking_options.get("chunking_strategy"),
            "delete_emails": chunking_options.get("delete_emails", False),
            "delete_credit_cards": chunking_options.get("delete_credit_cards", False),
            "delete_phone_numbers": chunking_options.get("delete_phone_numbers", False),
            "clean_bullet_points": chunking_options.get("clean_bullet_points", False),
            "clean_numbered_list": chunking_options.get("clean_numbered_list", False),
            "clean_dashes": chunking_options.get("clean_dashes", False),
            "clean_whitespaces": chunking_options.get("clean_whitespaces", False)
        }
        
        chunks = await process_markdown_message(markdown, **partition_params)
        logger.info(f"Generated {len(chunks)} chunks")
        
        # Create chunks array with all chunks
        await create_chunks_response(chunks, command)
            
        logger.info("Message processed and chunks published successfully")
    except Exception as e:
        logger.error(f"Error handling message: {e}", exc_info=True)
        raise


async def create_chunks_response(chunks: List[Chunk], command: Any) -> Dict[str, Any]:
    """
    Create a response message with processed chunks.
    
    Args:
        chunks: List of processed chunks
        command: Original command object containing metadata
        
    Returns:
        Dictionary containing the response message
    """
    try:
        # Create chunks array with all chunks
        processed_chunks: List[Dict[str, Any]] = []
        for i, chunk in enumerate(chunks):
            processed_chunk = {
                "type": chunk["metadata"]["type"],
                "element_id": f"chunk_{i}",
                "metadata": {
                    "words_count": len(chunk["content"].split()),
                    "sentences_count": len(chunk["content"].split('.')),
                    "paragraphs_count": len(chunk["content"].split('\n\n')),
                    "tokens_count": len(chunk["content"].split()),
                    "characters_count": len(chunk["content"]),
                    "chunks_count": chunk["metadata"]["total_chunks"],
                    # "chunk_index": chunk["metadata"]["chunk_index"],
                },
                "text": chunk["content"]
            }
            processed_chunks.append(processed_chunk)
        
        # Create single message with all chunks
        chunk_message: Dict[str, Any] = {
            "messageType": "command",
            "entityType": "knowledge",
            "type": "command.knowledge.processed_unstructured_chunks",
            "version": 1,
            "subject": "knowledge.processed_unstructured_chunks",
            "timestamp": command.timestamp.isoformat(),
            "entityId": command.entityId,
            "workspaceId": command.workspaceId,
            "correlationId": command.correlationId,
            "causationId": command.id,
            "data": {
                "knowledgeId": command.data.get("knowledgeId"),
                "datasourceId": command.data.get("datasourceId"),
                "chunks": processed_chunks,
            }
        }
        
        chunk_command = UnstructuredProcessChunksCommand(**chunk_message)
        await publish_nats_message(chunk_command.model_dump(mode='json'))
        logger.info(f"Published {len(processed_chunks)} chunks in a single message")
        
        return chunk_message
    except Exception as e:
        logger.error(f"Error creating chunks response: {e}", exc_info=True)
        raise


async def publish_nats_message(data: Dict[str, Any]) -> None:
    global nats_client
    if not nats_client:
        raise RuntimeError("NATS client not connected")
        
    subject = compute_dispatch_nats_subject(data)
    payload = json.dumps(data).encode()
    await nats_client.publish(subject, payload)
    logger.info(f"Published message to {subject}")


async def reconnect_nats() -> None:
    global nats_client, subscription_tasks
    max_reconnect_attempts = 10
    reconnect_delay = 5

    for attempt in range(max_reconnect_attempts):
        try:
            await disconnect_nats()
            await connect_nats()
            # Resubscribe to all previous subscriptions
            for subject in list(subscription_tasks.keys()):
                await subscribe_nats(subject)
            logger.info("Successfully reconnected to NATS")
            return
        except Exception as e:
            logger.error(f"Reconnection attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(reconnect_delay * (attempt + 1))

    logger.error("Failed to reconnect to NATS after multiple attempts")
