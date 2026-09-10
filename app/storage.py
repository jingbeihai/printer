# -*- coding: utf-8 -*-
"""服务器本地文件存储。所有 PDF 保存在 PRINT_DATA_DIR/uploads 下。"""
from io import BytesIO
from pathlib import Path

from . import config


def object_key(task_id: int, filename: str) -> str:
    safe = Path(filename).name.replace("/", "_").replace("\\", "_")
    import time
    return f"uploads/{time.strftime('%Y-%m-%d')}/{task_id}_{safe}"


def save(task_id: int, filename: str, content: bytes) -> str:
    key = object_key(task_id, filename)
    path = config.UPLOAD_DIR / key.removeprefix("uploads/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)


def read(location: str) -> bytes:
    return Path(location).read_bytes()


def remove(location: str) -> None:
    Path(location).unlink(missing_ok=True)


def page_count(content: bytes) -> int:
    from pypdf import PdfReader
    return len(PdfReader(BytesIO(content)).pages)
