#####################################################################
# THIS FILE IS AUTOMATICALLY GENERATED BY UNSTRUCTURED API TOOLS.
# DO NOT MODIFY DIRECTLY
#####################################################################

import os
from typing import List, Union
from fastapi import status, FastAPI, File, Form, Request, UploadFile, APIRouter
from slowapi.errors import RateLimitExceeded
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from fastapi.responses import PlainTextResponse
import json
from fastapi.responses import StreamingResponse
from starlette.types import Send
from base64 import b64encode
from typing import Optional, Mapping, Iterator, Tuple
import secrets
from unstructured.partition.auto import partition
from unstructured.staging.base import convert_to_isd
import tempfile


limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
router = APIRouter()

RATE_LIMIT = os.environ.get("PIPELINE_API_RATE_LIMIT", "1/second")


def is_expected_response_type(media_type, response_type):
    if media_type == "application/json" and response_type not in [dict, list]:
        return True
    elif media_type == "text/csv" and response_type != str:
        return True
    else:
        return False


# pipeline-api
def pipeline_api(file, filename="", response_type="application/json"):
    # NOTE(robinson) - This is a hacky solution due to
    # limitations in the SpooledTemporaryFile wrapper.
    # Specifically, it does't have a `seekable` attribute,
    # which is required for .pptx and .docx. See below
    # the link below
    # ref: https://stackoverflow.com/questions/47160211
    # /why-doesnt-tempfile-spooledtemporaryfile-implement-readable-writable-seekable
    with tempfile.TemporaryDirectory() as tmpdir:
        _filename = os.path.join(tmpdir, filename.split("/")[-1])
        with open(_filename, "wb") as f:
            f.write(file.read())
        elements = partition(filename=_filename)

    # Due to the above, elements have an ugly temp filename in their metadata
    # For now, replace this with the basename
    result = convert_to_isd(elements)
    for element in result:
        element["metadata"]["filename"] = os.path.basename(filename)

    return result


class MultipartMixedResponse(StreamingResponse):
    CRLF = b"\r\n"

    def __init__(self, *args, content_type: str = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.content_type = content_type

    def init_headers(self, headers: Optional[Mapping[str, str]] = None) -> None:
        super().init_headers(headers)
        self.boundary_value = secrets.token_hex(16)
        content_type = f'multipart/mixed; boundary="{self.boundary_value}"'
        self.raw_headers.append((b"content-type", content_type.encode("latin-1")))

    @property
    def boundary(self):
        return b"--" + self.boundary_value.encode()

    def _build_part_headers(self, headers: dict) -> bytes:
        header_bytes = b""
        for header, value in headers.items():
            header_bytes += f"{header}: {value}".encode() + self.CRLF
        return header_bytes

    def build_part(self, chunk: bytes) -> bytes:
        part = self.boundary + self.CRLF
        part_headers = {
            "Content-Length": len(chunk),
            "Content-Transfer-Encoding": "base64",
        }
        if self.content_type is not None:
            part_headers["Content-Type"] = self.content_type
        part += self._build_part_headers(part_headers)
        part += self.CRLF + chunk + self.CRLF
        return part

    async def stream_response(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        async for chunk in self.body_iterator:
            if not isinstance(chunk, bytes):
                chunk = chunk.encode(self.charset)
                chunk = b64encode(chunk)
            await send(
                {
                    "type": "http.response.body",
                    "body": self.build_part(chunk),
                    "more_body": True,
                }
            )

        await send({"type": "http.response.body", "body": b"", "more_body": False})


@router.post("/general/v0.0.4/general")
@limiter.limit(RATE_LIMIT)
async def pipeline_1(
    request: Request,
    files: Union[List[UploadFile], None] = File(default=None),
    output_format: Union[str, None] = Form(default=None),
):
    content_type = request.headers.get("Accept")

    default_response_type = output_format or "application/json"
    if not content_type or content_type == "*/*" or content_type == "multipart/mixed":
        media_type = default_response_type
    else:
        media_type = content_type

    if isinstance(files, list) and len(files):
        if len(files) > 1:
            if content_type and content_type not in [
                "*/*",
                "multipart/mixed",
                "application/json",
            ]:
                return PlainTextResponse(
                    content=(
                        f"Conflict in media type {content_type}"
                        ' with response type "multipart/mixed".\n'
                    ),
                    status_code=status.HTTP_406_NOT_ACCEPTABLE,
                )

            def response_generator(is_multipart):
                for file in files:
                    _file = file.file

                    response = pipeline_api(
                        _file,
                        response_type=media_type,
                        filename=file.filename,
                    )
                    if is_multipart:
                        if type(response) not in [str, bytes]:
                            response = json.dumps(response)
                    yield response

            if content_type == "multipart/mixed":
                return MultipartMixedResponse(
                    response_generator(is_multipart=True), content_type=media_type
                )
            else:
                return response_generator(is_multipart=False)
        else:
            file = files[0]
            _file = file.file

            response = pipeline_api(
                _file,
                response_type=media_type,
                filename=file.filename,
            )

            if is_expected_response_type(media_type, type(response)):
                return PlainTextResponse(
                    content=(
                        f"Conflict in media type {media_type}"
                        f" with response type {type(response)}.\n"
                    ),
                    status_code=status.HTTP_406_NOT_ACCEPTABLE,
                )
            valid_response_types = ["application/json", "text/csv", "*/*"]
            if media_type in valid_response_types:
                return response
            else:
                return PlainTextResponse(
                    content=f"Unsupported media type {media_type}.\n",
                    status_code=status.HTTP_406_NOT_ACCEPTABLE,
                )

    else:
        return PlainTextResponse(
            content='Request parameter "files" is required.\n',
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@app.get("/healthcheck", status_code=status.HTTP_200_OK)
async def healthcheck(request: Request):
    return {"healthcheck": "HEALTHCHECK STATUS: EVERYTHING OK!"}


app.include_router(router)