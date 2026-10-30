"""檔案工具：所有寫入都是原子的（先寫暫存檔再 rename），避免讀到半個檔案。"""
import json
import os
from pathlib import Path


def write_text(path: Path, text: str, mode: int | None = None) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_json(path: Path, data) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_text(path: Path, default=None):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def read_json(path: Path, default=None):
    text = read_text(path)
    return default if text is None else json.loads(text)
