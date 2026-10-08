# -*- coding: utf-8 -*-
"""Windows 统一客户端入口。

普通用户通过正式云端界面和 Windows 本地打印站工作。
配置写入用户目录，不会污染安装目录，也不会把令牌写进源码。
"""
import json
import os
import threading
import socket
import time
from pathlib import Path

import uvicorn

from . import config
from .station_runtime import StationRuntime


APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "CanonPrintDesk"
SETTINGS_PATH = APP_DIR / "client-settings.json"


def _settings() -> dict:
    defaults = {
        "cloud_url": config.CLOUD_URL,
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
    """首次启动直接进入登录页；打印站在登录后的页面中配置。"""
    if not SETTINGS_PATH.exists():
        _save_settings(settings)
    return settings


def main() -> None:
    try:
        import webview
    except ImportError as exc:
        raise SystemExit("缺少桌面组件，请执行 pip install -r requirements.txt") from exc
    settings = _first_run_dialog(_settings())
    station = StationRuntime(settings, APP_DIR, _save_settings)
    desktop_server = None
    desktop_socket = None
    from .desktop_web import create_desktop_app
    station.start()
    desktop_socket = socket.socket()
    # 固定本机来源，确保“记住登录信息”的浏览器存储跨启动有效。
    try:
        desktop_socket.bind(("127.0.0.1", int(settings.get("desktop_port", 18765))))
    except OSError as exc:
        desktop_socket.close()
        station.stop()
        raise SystemExit("客户端端口已被占用，请关闭已打开的客户端后再启动。") from exc
    desktop_server = uvicorn.Server(uvicorn.Config(
        create_desktop_app(settings["cloud_url"], station), log_config=None,
        log_level="warning"))
    threading.Thread(target=desktop_server.run, kwargs={"sockets": [desktop_socket]}, daemon=True).start()
    deadline = time.monotonic() + 15
    while not desktop_server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("客户端服务启动失败")
        time.sleep(.05)
    url = f"http://127.0.0.1:{desktop_socket.getsockname()[1]}/login"
    webview.create_window("绍兴数知 · 印务台", url, width=1000, height=650,
                          min_size=(640, 400), maximized=True)
    try:
        webview.start(gui="edgechromium", private_mode=False,
                      storage_path=str(APP_DIR / "browser"))
    finally:
        if desktop_server:
            desktop_server.should_exit = True
        station.stop()


if __name__ == "__main__":
    main()
