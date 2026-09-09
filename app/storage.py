# -*- coding: utf-8 -*-
"""云端文件存储。默认 local 供演示；生产环境使用 Cloudflare R2 私有桶。"""
from io import BytesIO
from pathlib import Path

from . import config


def object_key(task_id: int, filename: str) -> str:
    safe = Path(filename).name.replace("/", "_").replace("\\", "_")
    import time
    return f"uploads/{time.strftime('%Y-%m-%d')}/{task_id}_{safe}"


def _r2_client():
    if not all((config.R2_ENDPOINT, config.R2_BUCKET, config.R2_ACCESS_KEY_ID,
                config.R2_SECRET_ACCESS_KEY)):
        raise RuntimeError("R2 配置不完整：请设置 endpoint、bucket 与访问密钥")
    import boto3
    return boto3.client("s3", endpoint_url=config.R2_ENDPOINT,
                        aws_access_key_id=config.R2_ACCESS_KEY_ID,
                        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
                        region_name="auto")


def save(task_id: int, filename: str, content: bytes) -> str:
    key = object_key(task_id, filename)
    if config.STORAGE_BACKEND == "r2":
        _r2_client().put_object(Bucket=config.R2_BUCKET, Key=key, Body=content,
                                ContentType="application/pdf")
        return f"r2://{key}"
    path = config.UPLOAD_DIR / key.removeprefix("uploads/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)


def read(location: str) -> bytes:
    if location.startswith("r2://"):
        key = location.removeprefix("r2://")
        return _r2_client().get_object(Bucket=config.R2_BUCKET, Key=key)["Body"].read()
    return Path(location).read_bytes()


def remove(location: str) -> None:
    if location.startswith("r2://"):
        _r2_client().delete_object(Bucket=config.R2_BUCKET, Key=location.removeprefix("r2://"))
    else:
        Path(location).unlink(missing_ok=True)


def page_count(content: bytes) -> int:
    from pypdf import PdfReader
    return len(PdfReader(BytesIO(content)).pages)
