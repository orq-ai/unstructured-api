import logging
from typing import List, Any, TypedDict
from unstructured.partition.md import partition_md

logger = logging.getLogger("message_processor")


class ChunkMetadata(TypedDict):
    chunk_index: int
    total_chunks: int
    type: str


class Chunk(TypedDict):
    content: str
    metadata: ChunkMetadata


async def process_markdown_message(markdown: str, **partition_kwargs: Any) -> List[Chunk]:
    """
    Process markdown content using unstructured's native methods.

    Args:
        markdown: The markdown content to process
        **partition_kwargs: Additional parameters for partitioning

    Returns:
        List of chunks with their metadata
    """
    try:
        # Use unstructured's partition_md to process the markdown with provided parameters
        elements = partition_md(text=markdown, **partition_kwargs)

        chunks: List[Chunk] = []
        for i, element in enumerate(elements):
            if element.text.strip():
                chunk: Chunk = {
                    "content": element.text.strip(),
                    "metadata": {
                        "chunk_index": i,
                        "total_chunks": len(elements),
                        "type": getattr(element, "type", "unknown"),  # type: ignore
                    },
                }
                chunks.append(chunk)

        logger.info(f"Generated {len(chunks)} chunks from markdown using unstructured")
        logger.info(chunks)
        return chunks

    except Exception as e:
        logger.error(f"Error processing markdown: {e}")
        raise
