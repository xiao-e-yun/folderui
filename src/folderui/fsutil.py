"""File helpers: every write is atomic (write a temp file, then rename) so readers never see half a file."""
import os
from pathlib import Path


def write_text(path: Path, text: str, mode: int | None = None) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_text(path: Path, default=None):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
