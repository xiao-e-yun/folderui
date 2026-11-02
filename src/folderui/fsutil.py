"""File helpers: every write is atomic (write a temp file, then rename) so readers never see half a file."""
import os
from pathlib import Path


def write_text(path: Path, text: str, mode: int | None = None) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")  # LF everywhere: a CRLF script breaks a strict sh
    if mode is not None:
        os.chmod(tmp, mode)
    _writable(path)
    os.replace(tmp, path)


def remove(path: Path) -> None:
    _writable(path)
    path.unlink(missing_ok=True)


def _writable(path: Path) -> None:
    """On Windows a read-only file (mode 444) can't be replaced or deleted, so make it writable first."""
    if os.name == "nt" and path.exists():
        os.chmod(path, 0o644)


def read_text(path: Path, default=None):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
