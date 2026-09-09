# -*- coding: utf-8 -*-
"""网页公共件: 密码哈希、会话cookie签名、Jinja 环境。"""
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config

_TEMPLATE_DIR = Path(__file__).parent / "templates"

jinja = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def render(template: str, **ctx) -> HTMLResponse:
    """统一返回 HTMLResponse: 只 return 字符串时 FastAPI 会把 str 序列化成 JSON,
    浏览器就会显示原始代码(乱码)。"""
    return HTMLResponse(jinja.get_template(template).render(**ctx))


# ---------------- 密码 ----------------

def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or os.urandom(8).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 60000)
    return f"{salt}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


# ---------------- 会话cookie ----------------
# 格式: payload_json_b64(不加密,仅签名) . hmac_hex ;内含过期时间

def _sign(data: bytes) -> str:
    return hmac.new(config.SECRET.encode(), data, hashlib.sha256).hexdigest()


def make_session(payload: dict, hours: int = 24 * 7) -> str:
    body = dict(payload)
    body["exp"] = time.time() + hours * 3600
    raw = json.dumps(body, ensure_ascii=False).encode()
    import base64
    b64 = base64.urlsafe_b64encode(raw).decode()
    return f"{b64}.{_sign(b64.encode())}"


def read_session(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    import base64
    b64, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(b64.encode())):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(b64.encode()))
    except Exception:  # noqa: BLE001
        return None
    if body.get("exp", 0) < time.time():
        return None
    return body
