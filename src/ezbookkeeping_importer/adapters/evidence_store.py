import hashlib
import os
import tempfile
from pathlib import Path


class EvidenceStore:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def put(self, raw: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(raw).hexdigest()
        path = self.directory / f"{digest}.eml"
        fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise ValueError("immutable evidence content mismatch")
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(temporary)
        return digest, str(path)
