import codecs
import re
from typing import Annotated, List, Literal, Optional

from fastapi import Form, HTTPException
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from prepline_general.api.utils import SmartValueParser

# Bounds for values that flow into the chunker; unbounded values either crash
# partitioning (500s) or let a single request allocate absurd amounts of work.
MAX_CHUNK_CHARACTERS = 100_000
MAX_LIST_ITEMS = 50

# tesseract-style language codes ("eng", "nld", "chi_sim") and simple tokens
# for table types / element types / model names. These strings are forwarded
# into other subsystems, so constrain them to boring shapes.
_SIMPLE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_+-]{1,32}$")
_MIME_TYPE_RE = re.compile(r"^[\w.+-]+/[\w.+-]+$")


def _validate_token_list(values: Optional[List[str]], field: str) -> Optional[List[str]]:
    if values is None:
        return None
    # Form serialization turns unset list fields into empty strings
    # (e.g. `languages=` from the parallel-mode sub-request); treat empties
    # as absent rather than rejecting the request.
    values = [v for v in values if not (isinstance(v, str) and not v.strip())]
    if not values:
        return None
    if len(values) > MAX_LIST_ITEMS:
        raise ValueError(f"{field} accepts at most {MAX_LIST_ITEMS} items")
    for v in values:
        if not isinstance(v, str) or not _SIMPLE_TOKEN_RE.match(v):
            raise ValueError(f"{field} contains an invalid value: {v!r}")
    return values


class GeneralFormParams(BaseModel):
    """General partition API form parameters for the prepline API.
    To add a new parameter, add it here and in the as_form classmethod.
    Use Annotated to add a description and example for the parameter.

    All constraints live on this model (not just in the form signature), so
    the same validation applies wherever a GeneralFormParams is constructed.
    """

    model_config = ConfigDict(extra="forbid")

    xml_keep_tags: bool = False
    languages: Optional[List[str]] = None
    ocr_languages: Optional[List[str]] = None
    skip_infer_table_types: Optional[List[str]] = None
    gz_uncompressed_content_type: Optional[str] = None
    output_format: Literal["application/json", "text/csv"] = "application/json"
    coordinates: bool = False
    encoding: str = "utf-8"
    content_type: Optional[str] = None
    hi_res_model_name: Optional[str] = None
    include_page_breaks: bool = False
    pdf_infer_table_structure: bool = True
    strategy: Literal["fast", "hi_res", "auto", "ocr_only"] = "auto"
    extract_image_block_types: Optional[List[str]] = None
    unique_element_ids: bool = False
    # -- chunking options --
    chunking_strategy: Optional[Literal["by_title", "basic"]] = None
    combine_under_n_chars: Optional[int] = Field(None, ge=0, le=MAX_CHUNK_CHARACTERS)
    max_characters: int = Field(500, ge=1, le=MAX_CHUNK_CHARACTERS)
    multipage_sections: bool = True
    new_after_n_chars: Optional[int] = Field(None, ge=0, le=MAX_CHUNK_CHARACTERS)
    overlap: int = Field(0, ge=0, le=MAX_CHUNK_CHARACTERS)
    overlap_all: bool = False
    starting_page_number: Optional[int] = Field(None, ge=1, le=1_000_000)
    delete_emails: bool = False
    delete_credit_cards: bool = False
    delete_phone_numbers: bool = False
    clean_bullet_points: bool = False
    clean_numbered_list: bool = False
    clean_dashes: bool = False
    clean_whitespaces: bool = False
    include_slide_notes: bool = True

    # -- field validators -----------------------------------------------------

    @field_validator(
        "languages", "ocr_languages", "skip_infer_table_types", "extract_image_block_types"
    )
    @classmethod
    def _check_token_lists(cls, v: Optional[List[str]], info) -> Optional[List[str]]:
        return _validate_token_list(v, info.field_name)

    @field_validator("hi_res_model_name")
    @classmethod
    def _check_model_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _SIMPLE_TOKEN_RE.match(v):
            raise ValueError("hi_res_model_name contains invalid characters")
        return v

    @field_validator("content_type", "gz_uncompressed_content_type")
    @classmethod
    def _check_mime(cls, v: Optional[str], info) -> Optional[str]:
        if v is None:
            return None
        v = v.split(";")[0].strip().lower()
        if not _MIME_TYPE_RE.match(v):
            raise ValueError(f"{info.field_name} must be a MIME type like type/subtype")
        return v

    @field_validator("encoding")
    @classmethod
    def _check_encoding(cls, v: str) -> str:
        """The encoding is caller-controlled and later passed to .decode();
        an unknown codec name would surface as a 500 deep inside partitioning.
        Reject non-text codecs (e.g. base64, zlib) as well: those are
        bytes-to-bytes transformers, not text encodings."""
        try:
            info = codecs.lookup(v)
        except LookupError:
            raise ValueError(f"Unknown encoding: {v!r}") from None
        if not getattr(info, "_is_text_encoding", True):
            raise ValueError(f"{v!r} is not a text encoding")
        return info.name

    # -- cross-field validation -------------------------------------------------

    @model_validator(mode="after")
    def _check_chunking_coherence(self) -> "GeneralFormParams":
        if self.overlap >= self.max_characters:
            raise ValueError(
                "overlap must be smaller than max_characters "
                f"({self.overlap} >= {self.max_characters})"
            )
        if self.new_after_n_chars is not None and self.new_after_n_chars > self.max_characters:
            raise ValueError("new_after_n_chars (soft max) cannot exceed max_characters (hard max)")
        if (
            self.combine_under_n_chars is not None
            and self.combine_under_n_chars > self.max_characters
        ):
            raise ValueError("combine_under_n_chars cannot exceed max_characters")
        return self

    @classmethod
    def as_form(
        cls,
        xml_keep_tags: Annotated[
            bool,
            Form(
                title="Xml Keep Tags",
                description="If True, will retain the XML tags in the output. Otherwise it will simply extract the text from within the tags. Only applies to partition_xml.",
            ),
            BeforeValidator(SmartValueParser[bool]().value_or_first_element),
        ] = False,
        languages: Annotated[
            Optional[List[str]],
            Form(
                title="Languages",
                description="The languages present in the document, for use in partitioning and/or OCR",
                examples=["[eng]"],
            ),
            BeforeValidator(SmartValueParser[List[str]]().value_or_first_element),
        ] = None,
        ocr_languages: Annotated[
            Optional[List[str]],
            Form(
                title="OCR Languages",
                description="The languages to use for OCR",
                examples=["[eng]"],
            ),
            BeforeValidator(SmartValueParser[List[str]]().value_or_first_element),
        ] = None,
        skip_infer_table_types: Annotated[
            Optional[List[str]],
            Form(
                title="Skip Infer Table Types",
                description=(
                    "The document types that you want to skip table extraction with. Default: []"
                ),
                examples=["['pdf', 'jpg', 'png']"],
            ),
            BeforeValidator(SmartValueParser[List[str]]().value_or_first_element),
        ] = None,
        gz_uncompressed_content_type: Annotated[
            Optional[str],
            Form(
                title="Uncompressed Content Type",
                description="If file is gzipped, use this content type after unzipping",
                examples=["application/pdf"],
            ),
        ] = None,
        output_format: Annotated[
            Literal["application/json", "text/csv"],
            Form(
                title="Output Format",
                description="The format of the response. Supported formats are application/json and text/csv. Default: application/json.",
                examples=["application/json"],
            ),
        ] = "application/json",
        coordinates: Annotated[
            bool,
            Form(
                title="Coordinates",
                description="If true, return coordinates for each element. Default: false",
            ),
            BeforeValidator(SmartValueParser[bool]().value_or_first_element),
        ] = False,
        content_type: Annotated[
            Optional[str],
            Form(
                title="Content type",
                description="A hint about the content type to use (such as text/markdown), when there are problems processing a specific file. This value is a MIME type in the format type/subtype.",
                examples=["text/markdown"],
            ),
            BeforeValidator(SmartValueParser[str]().value_or_first_element),
        ] = None,
        encoding: Annotated[
            str,
            Form(
                title="Encoding",
                description="The encoding method used to decode the text input. Default: utf-8",
                examples=["utf-8"],
            ),
            BeforeValidator(SmartValueParser[str]().value_or_first_element),
        ] = "utf-8",
        hi_res_model_name: Annotated[
            Optional[str],
            Form(
                title="Hi Res Model Name",
                description="The name of the inference model used when strategy is hi_res",
                examples=["yolox"],
            ),
            BeforeValidator(SmartValueParser[str]().value_or_first_element),
        ] = None,
        include_page_breaks: Annotated[
            bool,
            Form(
                title="Include Page Breaks",
                description="If True, the output will include page breaks if the filetype supports it. Default: false",
            ),
            BeforeValidator(SmartValueParser[bool]().value_or_first_element),
        ] = False,
        pdf_infer_table_structure: Annotated[
            bool,
            Form(
                title="Pdf Infer Table Structure",
                description=(
                    "Deprecated! Use skip_infer_table_types to opt out of table extraction for any "
                    "file type. If False and strategy=hi_res, no Table Elements will be extracted "
                    "from pdf files regardless of skip_infer_table_types contents."
                ),
            ),
            BeforeValidator(SmartValueParser[bool]().value_or_first_element),
        ] = True,
        strategy: Annotated[
            Literal["fast", "hi_res", "auto", "ocr_only"],
            Form(
                title="Strategy",
                description="The strategy to use for partitioning PDF/image. Options are fast, hi_res, auto, ocr_only. Default: auto",
                examples=["auto", "hi_res"],
            ),
            BeforeValidator(SmartValueParser[str]().literal_value_stripped_or_first_element),
        ] = "auto",
        extract_image_block_types: Annotated[
            Optional[List[str]],
            Form(
                title="Image block types to extract",
                description="The types of elements to extract, for use in extracting image blocks as base64 encoded data stored in metadata fields",
                examples=["""["image", "table"]"""],
            ),
            BeforeValidator(SmartValueParser[List[str]]().value_or_first_element),
        ] = None,
        unique_element_ids: Annotated[
            bool,
            Form(
                title="unique_element_ids",
                description="""When `True`, assign UUIDs to element IDs, which guarantees their uniqueness 
(useful when using them as primary keys in database). Otherwise a SHA-256 of element text is used. Default: False""",
                examples=[True],
            ),
        ] = False,
        # -- chunking options --
        chunking_strategy: Annotated[
            Optional[Literal["by_title", "basic"]],
            Form(
                title="Chunking Strategy",
                description="Use one of the supported strategies to chunk the returned elements. Currently supports: by_title, basic",
                examples=["by_title"],
            ),
        ] = None,
        combine_under_n_chars: Annotated[
            Optional[int],
            Form(
                title="Combine Under N Chars",
                description="If chunking strategy is set, combine elements until a section reaches a length of n chars. Default: 500",
                examples=[500],
            ),
        ] = None,
        max_characters: Annotated[
            int,
            Form(
                title="Max Characters",
                description="If chunking strategy is set, cut off new sections after reaching a length of n chars (hard max). Default: 500",
                examples=[1500],
            ),
        ] = 500,
        multipage_sections: Annotated[
            bool,
            Form(
                title="Multipage Sections",
                description="If chunking strategy is set, determines if sections can span multiple sections. Default: true",
            ),
        ] = True,
        new_after_n_chars: Annotated[
            Optional[int],
            Form(
                title="New after n chars",
                description="If chunking strategy is set, cut off new sections after reaching a length of n chars (soft max). Default: 1500",
                examples=[1500],
            ),
        ] = None,
        overlap: Annotated[
            int,
            Form(
                title="Overlap",
                description="""Specifies the length of a string ("tail") to be drawn from each chunk and prefixed to the
next chunk as a context-preserving mechanism. By default, this only applies to split-chunks
where an oversized element is divided into multiple chunks by text-splitting. Default: 0""",
                examples=[20],
            ),
        ] = 0,
        overlap_all: Annotated[
            bool,
            Form(
                title="Overlap all",
                description="""When `True`, apply overlap between "normal" chunks formed from whole
elements and not subject to text-splitting. Use this with caution as it entails a certain
level of "pollution" of otherwise clean semantic chunk boundaries. Default: False""",
                examples=[True],
            ),
        ] = False,
        starting_page_number: Annotated[
            Optional[int],
            Form(
                title="PDF Starting Page Number",
                description=(
                    "When PDF is split into pages before sending it into the API, providing "
                    "this information will allow the page number to be assigned correctly."
                ),
                examples=[3],
            ),
        ] = None,
        delete_emails: Annotated[
            bool,
            Form(
                title="Delete Emails",
                description="If True, will delete emails from the output. Default: False",
            ),
        ] = False,
        delete_credit_cards: Annotated[
            bool,
            Form(
                title="Delete Credit Cards",
                description="If True, will delete credit card numbers from the output. Default: False",
            ),
        ] = False,
        delete_phone_numbers: Annotated[
            bool,
            Form(
                title="Delete Phone Numbers",
                description="If True, will delete phone numbers from the output. Default: False",
            ),
        ] = False,
        clean_bullet_points: Annotated[
            bool,
            Form(
                title="Clean Bullet Points",
                description="If True, will clean and standardize bullet points in the output. Default: False",
            ),
        ] = False,
        clean_numbered_list: Annotated[
            bool,
            Form(
                title="Clean Numbered List",
                description="If True, will clean and standardize numbered lists in the output. Default: False",
            ),
        ] = False,
        clean_dashes: Annotated[
            bool,
            Form(
                title="Clean Dashes",
                description="If True, will clean and standardize dashes in the output. Default: False",
            ),
        ] = False,
        clean_whitespaces: Annotated[
            bool,
            Form(
                title="Clean Whitespaces",
                description="If True, will clean and normalize whitespace in the output. Default: False",
            ),
        ] = False,
        include_slide_notes: Annotated[
            bool,
            Form(
                title="include_slide_notes",
                description=(
                    "When `True`, slide notes from .ppt and .pptx files"
                    " will be included in the response. Default: `True`"
                ),
                examples=[False],
            ),
        ] = True,
    ) -> "GeneralFormParams":
        try:
            return cls(
                xml_keep_tags=xml_keep_tags,
                languages=languages if languages else None,
                ocr_languages=ocr_languages if ocr_languages else None,
                skip_infer_table_types=(skip_infer_table_types if skip_infer_table_types else None),
                gz_uncompressed_content_type=gz_uncompressed_content_type,
                output_format=output_format,
                coordinates=coordinates,
                content_type=content_type,
                encoding=encoding,
                hi_res_model_name=hi_res_model_name,
                include_page_breaks=include_page_breaks,
                pdf_infer_table_structure=pdf_infer_table_structure,
                strategy=strategy,
                extract_image_block_types=(
                    extract_image_block_types if extract_image_block_types else None
                ),
                chunking_strategy=chunking_strategy,
                combine_under_n_chars=combine_under_n_chars,
                max_characters=max_characters,
                multipage_sections=multipage_sections,
                new_after_n_chars=new_after_n_chars,
                overlap=overlap,
                overlap_all=overlap_all,
                unique_element_ids=unique_element_ids,
                starting_page_number=starting_page_number,
                delete_emails=delete_emails,
                delete_credit_cards=delete_credit_cards,
                delete_phone_numbers=delete_phone_numbers,
                clean_bullet_points=clean_bullet_points,
                clean_numbered_list=clean_numbered_list,
                clean_dashes=clean_dashes,
                clean_whitespaces=clean_whitespaces,
                include_slide_notes=include_slide_notes,
            )
        except ValidationError as e:
            # A ValidationError raised inside a dependency is NOT translated
            # by FastAPI — it surfaces as a 500. Convert it to the 422 the
            # caller should get. Rebuild the detail by hand: e.errors() can
            # embed raw exception objects in "ctx", which JSONResponse
            # cannot serialize.
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "loc": list(err.get("loc", ())),
                        "msg": err.get("msg"),
                        "type": err.get("type"),
                    }
                    for err in e.errors()
                ],
            ) from None


class PartitionResponseMetadata(BaseModel):
    words_count: int
    characters_count: int
    sentences_count: int
    paragraphs_count: int
    tokens_count: int


class PartitionResponse(BaseModel):
    """Response model for the partition API"""

    documents: list[dict]
    metadata: PartitionResponseMetadata

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "documents": [
                    {
                        "type": "CompositeElement",
                        "element_id": "9d2cfb585937e07b1300da6f69c788d5",
                        "text": "HET\n\nMCB BOEK\n\n1\n\nHET\n\nMCB BOEK\n\n2 HET MCB BOEK",
                        "metadata": {
                            "filename": "1a48fa8a-d39b-4727-ae34-3d8c64b141e2.pdf",
                            "languages": ["nld"],
                            "filetype": "application/pdf",
                            "words_count": 66,
                            "sentence_count": 3,
                            "paragraph_count": 6,
                            "token_count": 104,
                        },
                    }
                ],
                "metadata": {
                    "words_count": 66,
                    "characters_count": 421,
                    "sentences_count": 3,
                    "paragraphs_count": 6,
                    "tokens_count": 104,
                },
            }
        }
    )
