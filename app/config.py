# -*- coding: utf-8 -*-
"""Shared server and formal Windows desktop settings."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PRINT_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"       # 云端: 按日期归档原始PDF; 本地: 拉取到的PDF
COMPOSED_DIR = DATA_DIR / "composed"    # 本地: 合成后的打印PDF
CLOUD_DB = DATA_DIR / "cloud.db"
LOCAL_DB = DATA_DIR / "local.db"
# 生产云端设置为 postgresql://user:password@127.0.0.1:5432/print_cluster。
# 未设置时保留 SQLite，仅用于本机演示。
CLOUD_DB_URL = os.environ.get("PRINT_CLOUD_DB_URL", "")

# ---------- 云端 server ----------
SERVER_PORT = int(os.environ.get("PRINT_PORT", "8011"))
SECRET = os.environ.get("PRINT_SECRET", "change-me-print-cluster")
INVITE_CODE = os.environ.get("PRINT_INVITE_CODE", "")
# 仅在数据库还没有任何用户时创建首个管理员；后续修改环境变量不会重置密码。
BOOTSTRAP_ADMIN_USERNAME = os.environ.get("PRINT_BOOTSTRAP_ADMIN_USERNAME", "admin")
BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("PRINT_BOOTSTRAP_ADMIN_PASSWORD", "")
FILE_RETENTION_DAYS = int(os.environ.get("PRINT_RETENTION_DAYS", "30"))  # 上传PDF保留天数

# ---------- 云端文件存储：服务器本地磁盘 / 腾讯云 COS ----------
# COS 密钥只配置在云服务器环境变量中，绝不能放进 EXE 或 GitHub。
STORAGE_BACKEND = os.environ.get("PRINT_STORAGE_BACKEND", "local").strip().lower()
COS_REGION = os.environ.get("COS_REGION", "ap-shanghai").strip()
COS_BUCKET = os.environ.get("COS_BUCKET", "").strip()
COS_SECRET_ID = os.environ.get("COS_SECRET_ID", "").strip()
COS_SECRET_KEY = os.environ.get("COS_SECRET_KEY", "").strip()
COS_PRESIGN_SECONDS = int(os.environ.get("COS_PRESIGN_SECONDS", "600"))

# ---------- Windows 印务台 ----------
CLOUD_URL = os.environ.get("PRINT_CLOUD_URL", "https://printer.7988798.xyz")
SYNC_INTERVAL = int(os.environ.get("PRINT_SYNC_INTERVAL", "30"))   # 与云端同步周期(秒)
PRINT_WINDOW = os.environ.get("PRINT_WINDOW", "")                  # 如 "13:00-18:00",空=全天

# ---------- 调度参数 ----------
POLL_INTERVAL = float(os.environ.get("PRINT_POLL_INTERVAL", "2"))  # 打印机轮询周期(秒)
DUPLEX = True
# ---------- Windows 真机后端 ----------
SUMATRA_PATH = os.environ.get("SUMATRA_PATH", str(BASE_DIR / "tools" / "SumatraPDF.exe"))


def ensure_dirs():
    for d in (DATA_DIR, UPLOAD_DIR, COMPOSED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def in_print_window(now_hm: str) -> bool:
    """PRINT_WINDOW='13:00-18:00' 时判断当前是否处于打印时间窗;空串恒为 True。"""
    if not PRINT_WINDOW:
        return True
    try:
        start, end = PRINT_WINDOW.split("-")
        return start.strip() <= now_hm <= end.strip()
    except ValueError:
        return True
