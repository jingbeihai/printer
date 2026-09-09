# -*- coding: utf-8 -*-
"""Windows 统一客户端入口。

普通用户只看到云端任务中心；打印站打开同一 EXE 后可切换到本机的状态墙。
配置写入用户目录，不会污染安装目录，也不会把令牌写进源码。
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import uvicorn

from . import config, db
from .agent_web import create_app as create_agent_app
from .backends import get_backend
from .cloudclient import CloudSync
from .executor import Executor
from .models import PrinterState


APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "CanonPrintDesk"
SETTINGS_PATH = APP_DIR / "client-settings.json"


def _settings() -> dict:
    defaults = {
        "cloud_url": config.CLOUD_URL,
        "station_mode": False,
        "agent_token": config.AGENT_TOKEN,
        "backend": config.BACKEND,
    }
    try:
        defaults.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return defaults


def _save_settings(value: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _first_run_dialog(settings: dict) -> dict:
    """首次启动只收集必要连接信息；后续可编辑 client-settings.json。"""
    if SETTINGS_PATH.exists():
        return settings
    import tkinter as tk
    from tkinter import messagebox, simpledialog
    root = tk.Tk()
    root.withdraw()
    url = simpledialog.askstring("打印管理系统", "云端服务地址：", initialvalue=settings["cloud_url"], parent=root)
    if not url:
        root.destroy()
        raise SystemExit("未配置云端服务地址")
    settings["cloud_url"] = url.rstrip("/")
    settings["station_mode"] = messagebox.askyesno(
        "打印站设置", "这台电脑是否连接佳能 C5560 并作为打印站？", parent=root)
    if settings["station_mode"]:
        token = simpledialog.askstring("打印站凭据", "输入管理员登记的打印站令牌：", show="*", parent=root)
        if token:
            settings["agent_token"] = token
    root.destroy()
    _save_settings(settings)
    return settings


def _start_station(settings: dict) -> tuple[Executor, str]:
    # 此处覆盖进程内配置，云端连接信息始终来自用户的本机设置。
    config.CLOUD_URL = settings["cloud_url"]
    config.AGENT_TOKEN = settings["agent_token"]
    config.BACKEND = settings.get("backend", "windows")
    config.ensure_dirs()
    db.init(config.LOCAL_DB, db.AGENT_SCHEMA)
    if config.BACKEND == "sim" and db.one("SELECT COUNT(*) c FROM printers")["c"] == 0:
        for index in range(1, 6):
            db.execute("INSERT INTO printers(name,state) VALUES(?,?)",
                       (f"{index:02d}号机", PrinterState.IDLE.value))
    syncer = CloudSync(config.CLOUD_URL, config.AGENT_TOKEN)
    executor = Executor(backend=get_backend(config.BACKEND), syncer=syncer)
    executor.start()
    app = create_agent_app(executor, syncer)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=config.AGENT_PORT,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    # 给本地服务一个短暂启动窗口；失败时 WebView 会显示明确错误。
    time.sleep(0.25)
    # 打印站默认打开与新原型一致的“打印队列”，状态墙仍作为辅助页面。
    return executor, f"http://127.0.0.1:{config.AGENT_PORT}/tasks"


def main() -> None:
    try:
        import webview
    except ImportError as exc:
        raise SystemExit("缺少桌面组件，请执行 pip install -r requirements.txt") from exc
    settings = _first_run_dialog(_settings())
    executor = None
    if settings.get("station_mode"):
        executor, url = _start_station(settings)
    else:
        url = settings["cloud_url"].rstrip("/") + "/login"
    window = webview.create_window("佳能打印管理", url, width=1440, height=920,
                                   min_size=(1100, 700))
    try:
        webview.start(gui="edgechromium")
    finally:
        if executor:
            executor.stop()


if __name__ == "__main__":
    main()
