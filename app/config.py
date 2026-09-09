# -*- coding: utf-8 -*-
"""
全局配置。同一套代码两种角色:
  python run.py server   → 云端任务中心(提交/队列/仪表盘/存档/代理接口)
  python run.py agent    → 学校本地打印任务管理程序(调度/状态墙/机队)
  python run.py demo     → 单机演示(server+agent 同机,模拟器后端)

所有项可用环境变量覆盖。
"""
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

# ---------- 打印后端: sim / cups / windows ----------
BACKEND = os.environ.get("PRINT_BACKEND", "sim")

# ---------- 云端 server ----------
SERVER_PORT = int(os.environ.get("PRINT_PORT", "8000"))
SECRET = os.environ.get("PRINT_SECRET", "change-me-print-cluster")
INVITE_CODE = os.environ.get("PRINT_INVITE_CODE", "SXSZ")
FILE_RETENTION_DAYS = int(os.environ.get("PRINT_RETENTION_DAYS", "30"))  # 上传PDF保留天数

# ---------- 云端文件存储：local（演示）/ r2（生产） ----------
STORAGE_BACKEND = os.environ.get("PRINT_STORAGE_BACKEND", "local").lower()
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")

# ---------- 本地 agent ----------
CLOUD_URL = os.environ.get("PRINT_CLOUD_URL", "https://printer.7988798.xyz")
AGENT_TOKEN = os.environ.get("PRINT_AGENT_TOKEN", "dev-token")     # 与云端管理页一致
AGENT_PORT = int(os.environ.get("PRINT_AGENT_PORT", "8001"))
SYNC_INTERVAL = int(os.environ.get("PRINT_SYNC_INTERVAL", "30"))   # 与云端同步周期(秒)
LOCAL_PASSWORD = os.environ.get("PRINT_LOCAL_PASSWORD", "")        # 本地网页口令,空=免登录
PRINT_WINDOW = os.environ.get("PRINT_WINDOW", "")                  # 如 "13:00-18:00",空=全天

# ---------- 调度参数 ----------
POLL_INTERVAL = float(os.environ.get("PRINT_POLL_INTERVAL", "2"))  # 打印机轮询周期(秒)
TRAY_SHEET_LIMIT = int(os.environ.get("PRINT_TRAY_LIMIT", "80"))   # 单组张数安全上限(托盘物理100)
DUPLEX = True
MAX_GROUP_ATTEMPTS = 5          # 单组最大重试次数,超过暂停并报警
COMPLETION_SETTLE_SEC = 4       # 回idle后需持续的秒数,防止假完成
ROLLER_WARN_PAGES = 30000       # 搓纸轮更换提醒阈值(累计张数)

# ---------- 维护 ----------
MAINTENANCE_INTERVAL_DAYS = 7   # 闲置期自动维护页周期

# ---------- 模拟器 ----------
SIM_PRINTERS = int(os.environ.get("SIM_PRINTERS", "5"))
SIM_SHEETS_PER_SEC = float(os.environ.get("SIM_SPEED", "4"))  # 真机双面约0.075张/秒,模拟加速
SIM_TRAY_CAPACITY = 250

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
