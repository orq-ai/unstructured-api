from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from ulid import ULID


class EntityType(str, Enum):
    KNOWLEDGE = "knowledge"


class OrquestaProduct(str, Enum):
    UNSTRUCTURED = "unstructured"


class KnowledgeCommandType(str, Enum):
    UNSTRUCTURED_PROCESS_MARKDOWN = "command.knowledge.process_unstructured_markdown"
    UNSTRUCTURED_PROCESS_CHUNKS = "command.knowledge.processed_unstructured_chunks"


class KnowledgeCommand(BaseModel):
    messageType: str = "command"
    entityType: EntityType = EntityType.KNOWLEDGE
    id: str = Field(default_factory=lambda: str(ULID()))
    version: int = 1
    type: KnowledgeCommandType
    subject: str = "knowledge.process_unstructured_markdown"
    # naive UTC (not aware) to keep the serialized timestamp wire-compatible
    # with what utcnow() produced; utcnow itself is deprecated
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    entityId: str
    product: Optional[OrquestaProduct] = None
    causationId: Optional[str] = None
    correlationId: Optional[str] = Field(default_factory=lambda: str(ULID()))
    source: Optional[str] = None
    workspaceId: str
    workspaceProjectId: Optional[str] = None
    userId: Optional[str] = None
    spanId: str = Field(default_factory=lambda: str(ULID()))
    parentId: Optional[str] = None
    sessionId: Optional[str] = None
    data: Dict[str, Any]


class ChunkingOptions(BaseModel):
    chunk_max_characters: Optional[int] = None
    chunk_overlap: Optional[int] = None
    chunking_strategy: str
    delete_emails: bool = False
    delete_credit_cards: bool = False
    delete_phone_numbers: bool = False
    clean_bullet_points: bool = False
    clean_numbered_list: bool = False
    clean_dashes: bool = False
    clean_whitespaces: bool = False


class ParseMarkdownRequest(BaseModel):
    markdown: str
    chunking_options: Optional[ChunkingOptions] = None


class ChunkMetadata(BaseModel):
    words_count: int
    sentences_count: int
    paragraphs_count: int
    tokens_count: int
    characters_count: int
    chunk_index: int
    total_chunks: int


class Chunk(BaseModel):
    text: str
    type: str
    metadata: ChunkMetadata


class ParseMarkdownResponse(BaseModel):
    chunks: List[Chunk]


class UnstructuredProcessMarkdownCommand(KnowledgeCommand):
    type: KnowledgeCommandType = KnowledgeCommandType.UNSTRUCTURED_PROCESS_MARKDOWN
    data: Dict[str, Any] = Field(
        ...,
        json_schema_extra={
            "example": {
                "markdown": "string",
                "knowledgeId": "string",
                "datasourceId": "string",
                "chunking_options": {
                    "chunk_max_characters": 500,
                    "chunk_overlap": 50,
                    "chunking_strategy": "basic",
                    "delete_emails": False,
                    "delete_credit_cards": False,
                    "delete_phone_numbers": False,
                    "clean_bullet_points": False,
                    "clean_numbered_list": False,
                    "clean_dashes": False,
                    "clean_whitespaces": False,
                },
            }
        },
    )


class UnstructuredProcessChunksCommand(KnowledgeCommand):
    type: KnowledgeCommandType = KnowledgeCommandType.UNSTRUCTURED_PROCESS_CHUNKS
    data: Dict[str, Any] = Field(
        ...,
        json_schema_extra={
            "example": {
                "chunks": [
                    {
                        "type": "Text",
                        "element_id": "chunk_0",
                        "metadata": {
                            "chunk_index": 0,
                            "total_chunks": 1,
                            "type": "text",
                            "coordinates": None,
                            "page_number": None,
                            "words_count": 10,
                            "sentences_count": 1,
                            "paragraphs_count": 1,
                            "tokens_count": 10,
                            "characters_count": 50,
                        },
                        "text": "string",
                    }
                ],
                "originalCommandId": "string",
            }
        },
    )
