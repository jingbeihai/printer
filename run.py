#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打印集群统一入口:
  python run.py server   云端任务中心(部署到 Ubuntu 云服务器)
  python run.py desktop  Windows 统一 EXE 的桌面入口（开发运行）
"""
import os
import sys

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
    uvicorn.run(app, host="0.0.0.0", port=config.SERVER_PORT, log_level="warning")


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
    role = sys.argv[1] if len(sys.argv) > 1 else "desktop"
    {"server": run_server, "desktop": run_desktop,
     "invite": show_invite}.get(
        role, lambda: sys.exit(f"未知角色: {role}"))()
