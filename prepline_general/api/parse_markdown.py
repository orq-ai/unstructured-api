from fastapi import APIRouter, HTTPException
import logging
from typing import List

import tiktoken
from prepline_general.api.models.knowledge import Chunk, ChunkMetadata, ChunkingOptions, ParseMarkdownRequest, ParseMarkdownResponse
from prepline_general.api.services.message_processor import process_markdown_message
from prepline_general.api.utils import (
    count_words,
    count_sentences,
    count_paragraphs,
    count_characters,
)

router = APIRouter()
logger = logging.getLogger("unstructured_api")
tokenizer = tiktoken.get_encoding("o200k_base")



@router.post(
    "",
    tags=["parse-markdown"],
    summary="Parse markdown content into chunks",
    description="Process markdown content and return chunks based on provided options",
    response_model=ParseMarkdownResponse
)
async def parse_markdown(request: ParseMarkdownRequest):
    try:
        # Map chunking options to partition parameters
        chunking_options = request.chunking_options or ChunkingOptions(chunking_strategy="basic")
        partition_params = {
            "max_characters": chunking_options.chunk_max_characters,
            "overlap": chunking_options.chunk_overlap,
            "chunking_strategy": chunking_options.chunking_strategy,
            "delete_emails": chunking_options.delete_emails,
            "delete_credit_cards": chunking_options.delete_credit_cards,
            "delete_phone_numbers": chunking_options.delete_phone_numbers,
            "clean_bullet_points": chunking_options.clean_bullet_points,
            "clean_numbered_list": chunking_options.clean_numbered_list,
            "clean_dashes": chunking_options.clean_dashes,
            "clean_whitespaces": chunking_options.clean_whitespaces
        }
        
        # Process the markdown and get chunks
        chunks = await process_markdown_message(request.markdown, **partition_params)
        
        # Convert chunks to response format
        processed_chunks: List[Chunk] = []
        for i, chunk in enumerate(chunks):
            chunk_metadata = ChunkMetadata(
                words_count=count_words(chunk["content"]),
                sentences_count=count_sentences(chunk["content"]),
                paragraphs_count=count_paragraphs(chunk["content"]),
                tokens_count=len(tokenizer.encode(chunk["content"])),
                characters_count=count_characters(chunk["content"]),
                chunk_index=i,
                total_chunks=len(chunks)
            )
            
            processed_chunk = Chunk(
                text=chunk["content"],
                type=chunk["metadata"]["type"],
                metadata=chunk_metadata
            )
            processed_chunks.append(processed_chunk)
        
        return ParseMarkdownResponse(chunks=processed_chunks)
        
    except Exception as e:
        logger.error(f"Error parsing markdown: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error processing markdown: {str(e)}"
        )

