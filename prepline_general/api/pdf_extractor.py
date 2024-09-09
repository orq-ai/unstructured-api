from fastapi import APIRouter, HTTPException, Depends, Body
import tempfile
import os
from .storage.storage_client import StorageClient
from .config.database_config import get_database, FileDocument
import logging
from pymongo.collection import Collection
from pydantic import BaseModel
import pypdfium2 # type: ignore
from typing import List, BinaryIO

router = APIRouter()
logger = logging.getLogger("unstructured_api")

class FileIdRequest(BaseModel):
    fileId: str

def get_storage_client():
    return StorageClient(
        os.environ.get('STORAGE_END_POINT', ''),
        os.environ.get('STORAGE_ACCESS_KEY', ''),
        os.environ.get('STORAGE_SECRET_KEY', ''),
        os.environ.get('ORQ_S3_BUCKET_NAME', ''),
        secure=True
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

    return "\n\n".join(text_list)

@router.post("/extract-pdf-content")
async def get_pdf_content(
    request: FileIdRequest = Body(...),
    storage_client: StorageClient = Depends(get_storage_client),
    db: Collection[FileDocument] = Depends(get_database)
):
    try:
        file_doc = db.find_one({"_id": request.fileId})
        if not file_doc:
            raise HTTPException(status_code=404, detail="File not found in database")

        object_name = file_doc.get("object_name")
        if not object_name:
            raise HTTPException(status_code=400, detail="Object name not found in file document")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            success = storage_client.download_file(object_name, temp_file.name)
            if not success:
                raise HTTPException(status_code=404, detail="File not found or error downloading from storage")

            with open(temp_file.name, 'rb') as file:
                content = extract_pdf_content(file)

        os.unlink(temp_file.name)  # Delete the temporary file

        return {"content": content}
    except Exception as e:
        logger.error(f"Error processing PDF for fileId {request.fileId}: {str(e)}")
        raise HTTPException(status_code=500, detail="Error processing PDF")