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


def _single_instance():
    import msvcrt
    APP_DIR.mkdir(parents=True, exist_ok=True)
    stream = (APP_DIR / "client.lock").open("a+b")
    stream.seek(0)
    stream.write(b"0")
    stream.flush()
    try:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        stream.close()
        raise SystemExit("客户端已经运行，请先关闭已打开的窗口") from exc
    return stream


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
    instance_guard = _single_instance()
    health_file = os.environ.get("PRINT_UPDATE_HEALTH_FILE")
    if health_file:
        from . import db
        from .update_client import VERSION
        config.DATA_DIR = APP_DIR / "station"
        config.UPLOAD_DIR = config.DATA_DIR / "uploads"
        config.COMPOSED_DIR = config.DATA_DIR / "composed"
        config.LOCAL_DB = config.DATA_DIR / "local.db"
        config.ensure_dirs()
        db.init(config.LOCAL_DB, db.LOCAL_SCHEMA)
        StationRuntime._ensure_schema_version()
        from .desktop_web import create_desktop_app
        create_desktop_app(_settings()['cloud_url'])
        if not (Path(__file__).parent / 'static' / 'desk' / 'index.html').is_file():
            raise RuntimeError('新版业务页面缺失')
        Path(health_file).write_text(json.dumps({"version": VERSION, "pid": os.getpid()}), encoding="utf-8")
        return
    try:
        import webview
    except ImportError as exc:
        raise SystemExit("缺少桌面组件，请执行 pip install -r requirements.txt") from exc
    settings = _first_run_dialog(_settings())
    station = StationRuntime(settings, APP_DIR, _save_settings)
    desktop_server = None
    desktop_socket = None
    from .desktop_web import create_desktop_app
    from .update_client import UpdateClient
    updater = UpdateClient(APP_DIR)
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
        create_desktop_app(settings["cloud_url"], station, updater), log_config=None,
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
