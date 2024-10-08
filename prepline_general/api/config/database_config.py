from typing import TypedDict
from pymongo import MongoClient
from pymongo.collection import Collection
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
import os
from typing import Any

# Define the document structure using TypedDict
class FilesPurposes:
    Retrieval = 'retrieval'

class FileDocument(TypedDict):
    object_name: str
    purpose: FilesPurposes
    bytes: int
    file_name: str
    file_id: str

# Synchronous function
def get_database() -> Collection[FileDocument]:
    mongo_url = os.environ.get("MONGO_DATABASE_URL")
    if not mongo_url:
        raise ValueError("The 'MONGO_DATABASE_URL' environment variable is not set.")
    
    try:
        client: MongoClient[Any] = MongoClient(mongo_url)
        database = client['storage']  # Access the 'storage' database
        return database.get_collection('files')  # Return the 'files' collection
    except Exception as e:
        raise ConnectionError(f"Error connecting to MongoDB: {e}")

# Asynchronous function
async def get_async_database() -> AsyncIOMotorCollection[FileDocument]:
    mongo_uri = os.environ.get("MONGODB_URI")
    db_name = os.environ.get("MONGODB_DB_NAME")

    if not mongo_uri or not db_name:
        raise ValueError("The 'MONGODB_URI' or 'MONGODB_DB_NAME' environment variable is not set.")

    try:
        client: AsyncIOMotorClient[Any] = AsyncIOMotorClient(mongo_uri)
        database = client[db_name]  # Access the database asynchronously
        return database.get_collection('files')  # Return the 'files' collection asynchronously
    except Exception as e:
        raise ConnectionError(f"Error connecting to MongoDB asynchronously: {e}")