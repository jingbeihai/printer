#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打印集群统一入口:
  python run.py server   云端任务中心(部署到 Ubuntu 云服务器, :8000)
  python run.py agent    学校本地打印任务管理程序(:8001)
  python run.py demo     单机演示(同机起 server+agent, 模拟器后端)
  python run.py desktop  Windows 统一 EXE 的桌面入口（开发运行）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import config  # noqa: E402


def run_server():
    import uvicorn
    from app import db
    from app.server_web import create_app
    config.ensure_dirs()
    db.init(config.CLOUD_DB_URL or config.CLOUD_DB, db.SERVER_SCHEMA)
    app = create_app()
    print(f"[server] 云端任务中心 → http://0.0.0.0:{config.SERVER_PORT}")
    print(f"[server] 注册邀请码: {db.get_conf('invite_code')}  (管理页可改)")
    print(f"[server] 代理Token: {db.get_conf('agent_token')}  (学校PC PRINT_AGENT_TOKEN 填这个)")
    uvicorn.run(app, host="0.0.0.0", port=config.SERVER_PORT, log_level="warning")


def run_agent():
    import uvicorn
    from app import db
    from app.agent_web import create_app
    from app.backends import get_backend
    from app.cloudclient import CloudSync
    from app.executor import Executor
    from app.models import PrinterState
    config.ensure_dirs()
    db.init(config.LOCAL_DB, db.AGENT_SCHEMA)

    # 首次启动: 模拟器模式下播种虚拟打印机
    if config.BACKEND == "sim" and db.one("SELECT COUNT(*) c FROM printers")["c"] == 0:
        for i in range(1, config.SIM_PRINTERS + 1):
            db.execute("INSERT INTO printers(name,state) VALUES(?,?)",
                       (f"{i:02d}号机", PrinterState.IDLE.value))
        print(f"[agent] 已播种 {config.SIM_PRINTERS} 台虚拟打印机")

    backend = get_backend()
    syncer = CloudSync() if config.CLOUD_URL else None
    executor = Executor(backend=backend, syncer=syncer)
    executor.start()
    app = create_app(executor, syncer)
    print(f"[agent] 本地打印管理 → http://0.0.0.0:{config.AGENT_PORT}  (后端:{config.BACKEND})")
    try:
        uvicorn.run(app, host="0.0.0.0", port=config.AGENT_PORT, log_level="warning")
    finally:
        executor.stop()


def run_demo():
    import subprocess
    print("[demo] 启动云端 :8000 与本地代理 :8001 (模拟器后端)")
    print("[demo] 云端任务中心:  http://127.0.0.1:8000  (先注册,首个账号是管理员)")
    print("[demo] 本地状态墙:    http://127.0.0.1:8001")
    env = os.environ.copy()
    env.setdefault("PRINT_BACKEND", "sim")
    procs = [
        subprocess.Popen([sys.executable, __file__, "server"], env=env),
        subprocess.Popen([sys.executable, __file__, "agent"], env=env),
    ]
    try:
        while True:
            time.sleep(1)
            for p in procs:
                if p.poll() is not None:
                    raise SystemExit(p.returncode)
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()


def show_invite():
    """在服务器本机查看当前注册邀请码，避免误把代理 Token 当邀请码。"""
    from app import db
    config.ensure_dirs()
    db.init(config.CLOUD_DB_URL or config.CLOUD_DB, db.SERVER_SCHEMA)
    from app.server_web import ensure_defaults
    ensure_defaults()
    users = db.one("SELECT COUNT(*) c FROM users")["c"]
    print(f"[server] 数据库: {config.CLOUD_DB}")
    print(f"[server] 当前注册邀请码: {db.get_conf('invite_code')}")
    print(f"[server] 已注册账号: {users} 个")


def run_desktop():
    from app.desktop import main
    main()


if __name__ == "__main__":
    # 打包后的 Windows EXE 双击即打开统一桌面客户端；源码直接运行仍默认演示。
    role = sys.argv[1] if len(sys.argv) > 1 else ("desktop" if getattr(sys, "frozen", False) else "demo")
    {"server": run_server, "agent": run_agent, "demo": run_demo, "desktop": run_desktop,
     "invite": show_invite}.get(
        role, lambda: sys.exit(f"未知角色: {role}"))()
