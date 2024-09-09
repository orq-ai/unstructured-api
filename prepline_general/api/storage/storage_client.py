from minio import Minio
from minio.error import S3Error

class StorageClient:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket_name: str, secure: bool = True):
        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure
        )
        self.bucket_name=bucket_name

    def download_file(self, object_name: str, file_path: str):
        try:
            self.client.fget_object(self.bucket_name, object_name, file_path)
            return True
        except S3Error as e:
            print(f"Error downloading file: {e}")
            return False