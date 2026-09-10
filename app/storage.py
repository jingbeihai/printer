# -*- coding: utf-8 -*-
"""服务器本地磁盘或腾讯云 COS 文件存储。"""
from io import BytesIO
from pathlib import Path

from . import config


def object_key(task_id: int, filename: str) -> str:
    safe = Path(filename).name.replace("/", "_").replace("\\", "_")
    import time
    return f"uploads/{time.strftime('%Y-%m-%d')}/{task_id}_{safe}"


def _cos_client():
    if not all((config.COS_BUCKET, config.COS_REGION,
                config.COS_SECRET_ID, config.COS_SECRET_KEY)):
        raise RuntimeError("腾讯云 COS 配置不完整")
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as exc:
        raise RuntimeError("缺少腾讯云 COS SDK，请安装 requirements.txt") from exc
    cos_config = CosConfig(Region=config.COS_REGION,
                           SecretId=config.COS_SECRET_ID,
                           SecretKey=config.COS_SECRET_KEY,
                           Scheme="https")
    return CosS3Client(cos_config)


def _cos_key(location: str) -> str:
    if not location.startswith("cos://"):
        raise ValueError("不是 COS 文件地址")
    return location.removeprefix("cos://")


def prepare_upload(task_id: int, filename: str) -> tuple[str, str]:
    """返回数据库存储位置和浏览器直传用的短期 PUT URL。"""
    if config.STORAGE_BACKEND != "cos":
        raise RuntimeError("当前未启用腾讯云 COS 直传")
    key = object_key(task_id, filename)
    url = _cos_client().get_presigned_url(
        Method="PUT", Bucket=config.COS_BUCKET, Key=key,
        Expired=config.COS_PRESIGN_SECONDS)
    return f"cos://{key}", url


def exists(location: str) -> bool:
    if location.startswith("cos://"):
        try:
            _cos_client().head_object(Bucket=config.COS_BUCKET, Key=_cos_key(location))
            return True
        except Exception:  # COS SDK 对不存在和无权限使用不同异常类型
            return False
    return Path(location).is_file()


def download_url(location: str) -> str | None:
    if not location.startswith("cos://"):
        return None
    return _cos_client().get_presigned_url(
        Method="GET", Bucket=config.COS_BUCKET, Key=_cos_key(location),
        Expired=config.COS_PRESIGN_SECONDS)


def save(task_id: int, filename: str, content: bytes) -> str:
    key = object_key(task_id, filename)
    if config.STORAGE_BACKEND == "cos":
        _cos_client().put_object(Bucket=config.COS_BUCKET, Key=key, Body=content,
                                 ContentType="application/pdf")
        return f"cos://{key}"
    path = config.UPLOAD_DIR / key.removeprefix("uploads/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)


def read(location: str) -> bytes:
    if location.startswith("cos://"):
        response = _cos_client().get_object(Bucket=config.COS_BUCKET,
                                            Key=_cos_key(location))
        return response["Body"].get_raw_stream().read()
    return Path(location).read_bytes()


def remove(location: str) -> None:
    if location.startswith("cos://"):
        _cos_client().delete_object(Bucket=config.COS_BUCKET, Key=_cos_key(location))
        return
    Path(location).unlink(missing_ok=True)


def page_count(content: bytes) -> int:
    from pypdf import PdfReader
    return len(PdfReader(BytesIO(content)).pages)
