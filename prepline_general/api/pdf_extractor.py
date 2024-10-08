import mimetypes
from fastapi import APIRouter, HTTPException, Depends, Body
import tempfile
import os
import sentry_sdk
from .storage.storage_client import StorageClient
from .config.database_config import get_database, FileDocument
import logging
from pymongo.collection import Collection
from pydantic import BaseModel
import pypdfium2  # type: ignore
from typing import List, BinaryIO
from unstructured.partition.auto import partition

router = APIRouter()
logger = logging.getLogger("unstructured_api")


class FileIdRequest(BaseModel):
    file_id: str


def get_storage_client():
    return StorageClient(
        os.environ.get("STORAGE_END_POINT", ""),
        os.environ.get("STORAGE_ACCESS_KEY", ""),
        os.environ.get("STORAGE_SECRET_KEY", ""),
        os.environ.get("ORQ_S3_BUCKET_NAME", ""),
        secure=True,
    )


def extract_pdf_content(file: BinaryIO) -> str:
    file.seek(0)  # Ensure we're at the start of the file
    pdf_bytes = file.read()

    pdf_reader = pypdfium2.PdfDocument(pdf_bytes, autoclose=True)

    text_list: List[str] = []

    try:
        for page in pdf_reader:
            text_page = page.get_textpage()
            content = text_page.get_text_range()
            text_list.append(content)
            text_page.close()
            page.close()
    finally:
        pdf_reader.close()

    return "".join(text_list)


def extract_file_content(file: BinaryIO, content_type: str) -> str:
    elements = partition(file=file, content_type=content_type, max_characters=1000)

    content = "".join([element.text for element in elements])

    return content


@router.post("")
async def get_pdf_content(
    request: FileIdRequest = Body(...),
    storage_client: StorageClient = Depends(get_storage_client),
    db: Collection[FileDocument] = Depends(get_database),
):
    try:
        file_doc = db.find_one({"_id": request.file_id})

        if not file_doc:
            raise HTTPException(status_code=404, detail="File not found in database")

        object_name = file_doc.get("object_name")
        file_name = file_doc.get("file_name", "")

        if not object_name:
            raise HTTPException(status_code=400, detail="Object name not found in file document")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            success = storage_client.download_file(object_name, temp_file.name)

            if not success:
                raise HTTPException(
                    status_code=404, detail="File not found or error downloading from storage"
                )

            with open(temp_file.name, "rb") as file:

                file_content_type = str(mimetypes.guess_type(file_name)[0])

                # If the type of the file is PDF, extract the content
                if file_content_type == "application/pdf":
                    content = extract_pdf_content(file)
                else:
                    content = extract_file_content(file, file_content_type)

        os.unlink(temp_file.name)  # Delete the temporary file

        return {"content": content, "file_id": request.file_id, "file_name": file_name, "object_name": object_name}
    except Exception as e:
        sentry_sdk.capture_message("Error processing PDF")
        sentry_sdk.capture_exception(e)
        logger.error(f"Error processing PDF for fileId {request.file_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Error processing PDF")
