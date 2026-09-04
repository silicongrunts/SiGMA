"""Shared test factories for upload fakes."""

from typing import Optional


class ChunkedUpload:
    """Fake UploadFile whose read() yields bounded chunks like Starlette does."""

    def __init__(
        self,
        content: bytes,
        filename: str = "data.bin",
        content_type: Optional[str] = None,
    ):
        self.filename = filename
        self.content_type = content_type
        self.read_calls = 0
        self._content = content
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if size is None or size < 0:
            data = self._content[self._offset:]
            self._offset = len(self._content)
            return data
        data = self._content[self._offset:self._offset + size]
        self._offset += len(data)
        return data
