# -*- coding: utf-8 -*-
"""
云端任务中心网页 + 代理接口。
页面: 注册/登录/提交/队列/仪表盘(台账)/管理
接口: POST /api/agent/sync  GET /api/agent/file/{id}
"""
import json
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, Response, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import config, db, importer, storage
from .compose import sheets_per_copy
from .class_plan import expand_special_copies, validate_class_copies
from .models import ROLE_SUPERADMIN, ROLE_USER, TASK_STATUS_CN, TaskStatus
from .webcommon import hash_password, make_session, read_session, render, verify_password

SESSION_COOKIE = "pc_session"


# ---------------- 初始化与公共 ----------------

def ensure_defaults():
    if config.SECRET == "change-me-print-cluster":
        secret = db.get_conf("session_secret")
        if not secret:
            secret = secrets.token_urlsafe(48)
            db.set_conf("session_secret", secret)
        config.SECRET = secret
    saved_invite = db.get_conf("invite_code")
    if not saved_invite:
        db.set_conf("invite_code", config.INVITE_CODE if config.INVITE_CODE and
                    config.INVITE_CODE != "101010" else secrets.token_urlsafe(12))
    elif saved_invite == "101010":
        db.set_conf("invite_code", secrets.token_urlsafe(12))
    # 只在空数据库首次创建超级管理员；既有账号角色不自动转换或提升。
    if db.one("SELECT COUNT(*) c FROM users")["c"] == 0:
        if len(config.BOOTSTRAP_ADMIN_PASSWORD) < 8:
            raise RuntimeError("首次部署请设置 PRINT_BOOTSTRAP_ADMIN_PASSWORD（至少 8 位）")
        db.execute("INSERT INTO users(username,pw_hash,role,created_at) VALUES(?,?,?,?)",
                   (config.BOOTSTRAP_ADMIN_USERNAME,
                    hash_password(config.BOOTSTRAP_ADMIN_PASSWORD), ROLE_SUPERADMIN, time.time()))


def current_user(req: Request):
    s = read_session(req.cookies.get(SESSION_COOKIE))
    if not s:
        return None
    return db.one("SELECT * FROM users WHERE id=? AND active=1", (s.get("uid"),))


def require_user(req: Request):
    u = current_user(req)
    return u  # None 时由路由自行重定向


def save_upload_by_date(task_id: int, up: UploadFile, content: bytes) -> str:
    return storage.save(task_id, up.filename, content)


# ---------------- 留存清理(30天) ----------------

def cleanup_old_uploads():
    cutoff = time.time() - config.FILE_RETENTION_DAYS * 86400
    # 按数据库位置清理 COS 文件，也覆盖切回 local 后遗留的对象。
    for task in db.q("SELECT id,pdf_path FROM tasks WHERE pdf_path LIKE ? AND created_at<?",
                     ("cos://%", cutoff)):
        try:
            storage.remove(task["pdf_path"])
        except Exception as exc:
            # 保留地址，下次清理重试；单个对象失败不阻断其他文件。
            db.log_event("RETENTION_PURGE_FAILED", str(exc), task_id=task["id"],
                         table="task_events")
            continue
        db.execute("UPDATE tasks SET pdf_path='' WHERE id=? AND pdf_path=?",
                   (task["id"], task["pdf_path"]))
        db.log_event("RETENTION_PURGE", f"已按{config.FILE_RETENTION_DAYS}天留存策略删除 COS 文件",
                     task_id=task["id"], table="task_events")
    for d in sorted(config.UPLOAD_DIR.glob("*")):
        if not d.is_dir():
            continue
        try:
            day_ts = time.mktime(time.strptime(d.name, "%Y-%m-%d"))
        except ValueError:
            continue
        if day_ts < cutoff:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
            db.log_event("RETENTION_PURGE", f"已按{config.FILE_RETENTION_DAYS}天留存策略删除 {d.name}",
                         table="task_events")


def start_retention_thread():
    def loop():
        while True:
            try:
                cleanup_old_uploads()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(86400)
    threading.Thread(target=loop, daemon=True).start()


# ---------------- App ----------------

def create_app() -> FastAPI:
    app = FastAPI(title="打印集群·任务中心", docs_url=None, redoc_url=None)

    @app.get("/updates/stable.json")
    def client_update_manifest():
        path = config.DATA_DIR / "releases" / "stable.json"
        try:
            raw = path.read_bytes()
        except OSError:
            return Response(status_code=404)
        if len(raw) > 32768:
            return Response(status_code=503)
        return Response(raw, media_type="application/json",
                        headers={"Cache-Control": "no-store"})
    ensure_defaults()
    cleanup_old_uploads()
    start_retention_thread()

    # ---------- 认证 ----------
    @app.get("/register", response_class=HTMLResponse)
    def register_page(req: Request):
        return render("register.html", error="", invite="")

    @app.post("/register")
    def register(username: str = Form(...), password: str = Form(...),
                 invite: str = Form(...)):
        username = username.strip()
        if not username or len(username) > 80 or len(password) < 8:
            return render("register.html", error="用户名必填，密码至少 8 位", invite=invite)
        if invite.strip() != db.get_conf("invite_code"):
            return render("register.html", error="邀请码不正确，请向公司管理员索取", invite=invite)
        if db.one("SELECT id FROM users WHERE username=?", (username,)):
            return render("register.html", error="用户名已存在", invite=invite)
        uid = db.execute("INSERT INTO users(username,pw_hash,role,created_at) VALUES(?,?,?,?)",
                         (username, hash_password(password), ROLE_USER, time.time()))
        resp = RedirectResponse("/desk/", 303)
        resp.set_cookie(SESSION_COOKIE, make_session({"uid": uid}), httponly=True)
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        return render("login.html", error="")

    @app.post("/login")
    def login(username: str = Form(...), password: str = Form(...)):
        u = db.one("SELECT * FROM users WHERE username=?", (username.strip(),))
        if not u or not u["active"] or not verify_password(password, u["pw_hash"]):
            return render("login.html", error="用户名或密码错误")
        resp = RedirectResponse("/desk/", 303)
        resp.set_cookie(SESSION_COOKIE, make_session({"uid": u["id"]}), httponly=True)
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @app.get("/")
    def index():
        return RedirectResponse("/desk/", 303)

    # ---------- 提交 ----------
    def school_presets():
        result = []
        for row in db.q("SELECT * FROM school_presets ORDER BY school, grade"):
            preset = dict(row)
            preset["special_copies"] = json.loads(preset.pop("overrides_json") or '[]')
            result.append(preset)
        return result

    # React 桌面界面使用这一组同源接口。旧的 Jinja 页面仍可访问，便于已打开的
    # 浏览器会话平滑过渡；新 EXE 不再以那些页面作为产品界面。
    def api_user(req: Request):
        user = require_user(req)
        if not user:
            return None, JSONResponse({"ok": False, "error": "登录已失效"}, status_code=401)
        # 当前系统所有已登录用户都拥有统一的管理员操作权限；保留数据库
        # 中的历史角色字段，仅用于兼容已有数据和管理页面。
        effective = dict(user)
        effective["role"] = ROLE_SUPERADMIN
        return effective, None

    def api_task(row):
        task = dict(row)
        task["class_copies"] = json.loads(task.pop("class_copies_json") or '[]')
        task["cover_enabled"] = bool(task["cover_enabled"])
        task["claimed"] = bool(task.pop("claimed_client_id", ""))
        task["copies_per_class"] = task.get("copies_per_class") or (
            max(1, task["copies"] // task["classes"]) if task["classes"] else task["copies"])
        task["status_label"] = TASK_STATUS_CN.get(task["status"], task["status"])
        task["total_pages"] = task["pages"] * task["copies"]
        return task

    def api_snapshot(req: Request):
        user, error = api_user(req)
        if error:
            return error
        tasks = [api_task(row) for row in db.q(
            "SELECT * FROM tasks WHERE status<>'UPLOADING' ORDER BY "
            "CASE WHEN status='QUEUED' AND claimed_client_id='' THEN 0 "
            "WHEN status='QUEUED' THEN 1 "
            "WHEN status IN ('PRINTING','AWAITING_PICKUP') THEN 2 ELSE 3 END, "
            "priority DESC, id DESC")]
        now = time.time()
        day_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        month_start = time.mktime(time.strptime(time.strftime("%Y-%m"), "%Y-%m"))
        done_today = [task for task in tasks if task["status"] == TaskStatus.DONE.value and
                      task["done_at"] and task["done_at"] >= day_start]
        done_month = [task for task in tasks if task["status"] == TaskStatus.DONE.value and
                      task["done_at"] and task["done_at"] >= month_start]
        return {
            "ok": True,
            "user": {"username": user["username"], "role": user["role"],
                     "display_name": user["display_name"]},
            "client_kind": "web",
            "now": now,
            "tasks": tasks,
            "presets": school_presets(),
            # 打印机的实时信息由打印站后续上报；空列表在未连接打印站时是正常状态。
            "printers": [],
            "stats": {
                "today": {"tasks": len(done_today), "pages": sum(task["total_pages"] for task in done_today)},
                "month": {"tasks": len(done_month), "pages": sum(task["total_pages"] for task in done_month)},
            },
        }

    @app.get("/api/v1/bootstrap")
    def api_bootstrap(req: Request):
        return api_snapshot(req)

    @app.post("/api/v1/schools")
    async def api_school_create(req: Request):
        user, error = api_user(req)
        if error:
            return error
        body = await req.json()
        school, grade = str(body.get("school", "")).strip(), str(body.get("grade", "")).strip()
        try:
            classes, copies = int(body.get("classes")), int(body.get("copies"))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "班级数和份数必须为整数"}, status_code=400)
        if not school or not grade or classes < 1 or copies < 1:
            return JSONResponse({"ok": False, "error": "请填写学校、年级、班级数和每班份数"}, status_code=400)
        rules = body.get("special_copies", [])
        try:
            expand_special_copies(classes, copies, rules)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        db.execute("INSERT INTO school_presets(school,grade,classes,copies,overrides_json) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(school,grade) DO UPDATE SET classes=excluded.classes,"
                   "copies=excluded.copies,overrides_json=excluded.overrides_json",
                   (school, grade, classes, copies, json.dumps(rules, ensure_ascii=False)))
        return api_snapshot(req)

    @app.put("/api/v1/schools/{sid}")
    async def api_school_update(req: Request, sid: int):
        user, error = api_user(req)
        if error:
            return error
        body = await req.json()
        school, grade = str(body.get("school", "")).strip(), str(body.get("grade", "")).strip()
        try:
            classes, copies = int(body.get("classes")), int(body.get("copies"))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "班级数和份数必须为整数"}, status_code=400)
        if not db.one("SELECT id FROM school_presets WHERE id=?", (sid,)):
            return JSONResponse({"ok": False, "error": "学校预设不存在"}, status_code=404)
        if not school or not grade or classes < 1 or copies < 1:
            return JSONResponse({"ok": False, "error": "请填写学校、年级、班级数和每班份数"}, status_code=400)
        rules = body.get("special_copies", [])
        try:
            expand_special_copies(classes, copies, rules)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        db.execute("UPDATE school_presets SET school=?,grade=?,classes=?,copies=?,overrides_json=? WHERE id=?",
                   (school, grade, classes, copies, json.dumps(rules, ensure_ascii=False), sid))
        return api_snapshot(req)

    @app.delete("/api/v1/schools/{sid}")
    def api_school_delete(req: Request, sid: int):
        user, error = api_user(req)
        if error:
            return error
        db.execute("DELETE FROM school_presets WHERE id=?", (sid,))
        return api_snapshot(req)

    # 用户和邀请码只在云端管理；客户端即使访问代理接口也要再次经过此处鉴权。
    def api_superadmin(req: Request):
        user, error = api_user(req)
        if error:
            return None, error
        if user["role"] != ROLE_SUPERADMIN:
            return None, JSONResponse({"ok": False, "error": "仅超级管理员可操作"}, status_code=403)
        return user, None

    def public_users():
        users = [dict(row) for row in db.q(
            "SELECT id,username,role,active,display_name,created_at FROM users ORDER BY id")]
        for user in users:
            user["role"] = ROLE_SUPERADMIN
        return users

    def admin_snapshot():
        return {"ok": True, "users": public_users(), "schools": school_presets(),
                "invite_code": db.get_conf("invite_code")}

    @app.get("/api/v1/admin/overview")
    def api_admin_overview(req: Request):
        _, error = api_superadmin(req)
        return error or admin_snapshot()

    @app.post("/api/v1/admin/users")
    async def api_admin_create_user(req: Request):
        _, error = api_superadmin(req)
        if error:
            return error
        body = await req.json()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        role = ROLE_SUPERADMIN
        if not username or len(username) > 80 or len(password) < 8 or role not in (
                ROLE_USER, ROLE_SUPERADMIN):
            return JSONResponse({"ok": False, "error": "用户名不能为空，密码至少 8 位，并请选择有效角色"}, status_code=400)
        if db.one("SELECT id FROM users WHERE username=?", (username,)):
            return JSONResponse({"ok": False, "error": "用户名已存在"}, status_code=409)
        db.execute("INSERT INTO users(username,pw_hash,role,active,created_at) VALUES(?,?,?,?,?)",
                   (username, hash_password(password), role, 1, time.time()))
        return admin_snapshot()

    @app.put("/api/v1/admin/users/{uid}")
    async def api_admin_update_user(req: Request, uid: int):
        actor, error = api_superadmin(req)
        if error:
            return error
        target = db.one("SELECT * FROM users WHERE id=?", (uid,))
        if not target:
            return JSONResponse({"ok": False, "error": "用户不存在"}, status_code=404)
        body = await req.json()
        role = ROLE_SUPERADMIN
        active = body.get("active", bool(target["active"]))
        password = body.get("password", "")
        if role not in (ROLE_USER, ROLE_SUPERADMIN) or not isinstance(active, bool):
            return JSONResponse({"ok": False, "error": "角色或状态无效"}, status_code=400)
        if password and (not isinstance(password, str) or len(password) < 8):
            return JSONResponse({"ok": False, "error": "新密码至少 8 位"}, status_code=400)
        if uid == actor["id"] and (role != ROLE_SUPERADMIN or not active):
            return JSONResponse({"ok": False, "error": "不能停用或降级当前账号"}, status_code=400)
        if target["role"] == ROLE_SUPERADMIN and (role != ROLE_SUPERADMIN or not active):
            remaining = db.one("SELECT COUNT(*) c FROM users WHERE role=? AND active=1 AND id<>?",
                               (ROLE_SUPERADMIN, uid))["c"]
            if not remaining:
                return JSONResponse({"ok": False, "error": "必须保留一名启用的超级管理员"}, status_code=400)
        db.execute("UPDATE users SET role=?,active=? WHERE id=?", (role, int(active), uid))
        if password:
            db.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(password), uid))
        return admin_snapshot()

    @app.delete("/api/v1/admin/users/{uid}")
    def api_admin_delete_user(req: Request, uid: int):
        actor, error = api_superadmin(req)
        if error:
            return error
        target = db.one("SELECT id,role FROM users WHERE id=?", (uid,))
        if not target:
            return JSONResponse({"ok": False, "error": "用户不存在"}, status_code=404)
        if uid == actor["id"]:
            return JSONResponse({"ok": False, "error": "不能删除当前账号"}, status_code=400)
        if target["role"] == ROLE_SUPERADMIN and not db.one(
                "SELECT id FROM users WHERE role=? AND active=1 AND id<>?",
                (ROLE_SUPERADMIN, uid)):
            return JSONResponse({"ok": False, "error": "必须保留一名启用的超级管理员"}, status_code=400)
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        return admin_snapshot()

    @app.put("/api/v1/admin/invite")
    async def api_admin_invite(req: Request):
        _, error = api_superadmin(req)
        if error:
            return error
        body = await req.json()
        code = str(body.get("invite_code", "")).strip()
        if not 6 <= len(code) <= 80:
            return JSONResponse({"ok": False, "error": "邀请码应为 6 至 80 个字符"}, status_code=400)
        db.set_conf("invite_code", code)
        return admin_snapshot()

    @app.get("/api/v1/account")
    def api_account(req: Request):
        user, error = api_user(req)
        if error:
            return error
        return {"ok": True, "account": {"username": user["username"], "role": user["role"],
                "display_name": user["display_name"]}}

    @app.put("/api/v1/account")
    async def api_account_update(req: Request):
        user, error = api_user(req)
        if error:
            return error
        body = await req.json()
        display_name = str(body.get("display_name", user["display_name"])).strip()
        old_password = str(body.get("old_password", ""))
        new_password = str(body.get("new_password", ""))
        if len(display_name) > 80:
            return JSONResponse({"ok": False, "error": "姓名不能超过 80 字"}, status_code=400)
        if new_password:
            if len(new_password) < 8 or not verify_password(old_password, user["pw_hash"]):
                return JSONResponse({"ok": False, "error": "原密码不正确，或新密码少于 8 位"}, status_code=400)
            db.execute("UPDATE users SET pw_hash=? WHERE id=?",
                       (hash_password(new_password), user["id"]))
        db.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, user["id"]))
        return api_account(req)

    @app.post("/api/v1/tasks/{tid}/cancel")
    def api_task_cancel(req: Request, tid: int):
        user, error = api_user(req)
        if error:
            return error
        task = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not task:
            return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
        if task["source_user"] != user["username"] and user["role"] != ROLE_SUPERADMIN:
            return JSONResponse({"ok": False, "error": "只能取消自己提交的任务"}, status_code=403)
        if task["status"] != TaskStatus.QUEUED.value or task["claimed_client_id"]:
            return JSONResponse({"ok": False, "error": "任务已开始打印或结束，不能取消"}, status_code=409)
        changed = db.update_count(
            "UPDATE tasks SET status='CANCELED', done_at=? "
            "WHERE id=? AND status='QUEUED' AND claimed_client_id=''", (time.time(), tid))
        if not changed:
            return JSONResponse({"ok": False, "error": "任务已被领取，请刷新列表"}, status_code=409)
        db.log_event("TASK_CANCELED", f"{user['username']} 取消任务", task_id=tid, table="task_events")
        return api_snapshot(req)

    @app.post("/api/v1/tasks/{tid}/force-cancel")
    async def api_task_force_cancel(req: Request, tid: int):
        user, error = api_user(req)
        if error:
            return error
        task = db.one("SELECT source_user,status FROM tasks WHERE id=?", (tid,))
        if not task:
            return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
        if task["source_user"] != user["username"] and user["role"] != ROLE_SUPERADMIN:
            return JSONResponse({"ok": False, "error": "只能结束自己提交的任务"}, status_code=403)
        if task["status"] != TaskStatus.PRINTING.value:
            return JSONResponse({"ok": False, "error": "只能结束状态为打印中的异常任务"}, status_code=409)
        body = await req.json()
        if body.get("confirm_stopped") is not True:
            return JSONResponse({"ok": False, "error": "请先确认打印机和 Windows 队列已停止该任务"}, status_code=400)
        changed = db.update_count(
            "UPDATE tasks SET status='CANCELED',done_at=? WHERE id=? AND status='PRINTING'",
            (time.time(), tid))
        if not changed:
            return JSONResponse({"ok": False, "error": "任务状态已变化，请刷新列表"}, status_code=409)
        db.log_event("TASK_FORCE_CANCELED", f"{user['username']} 确认设备已停打并结束异常任务",
                     task_id=tid, table="task_events")
        return api_snapshot(req)

    @app.post("/api/v1/tasks/{tid}/move")
    async def api_task_move(req: Request, tid: int):
        user, error = api_user(req)
        if error:
            return error
        body = await req.json()
        direction = body.get("direction")
        if direction not in ("up", "down"):
            return JSONResponse({"ok": False, "error": "请选择上移或下移"}, status_code=400)
        result = db.move_queued_task(tid, direction)
        if result == "not_queued":
            return JSONResponse({"ok": False, "error": "只能调整未开始的排队任务"}, status_code=409)
        if result == "boundary":
            return JSONResponse({"ok": False, "error": "任务已在队列边界"}, status_code=409)
        db.log_event("TASK_MOVED", f"{user['username']} 将任务{'上移' if direction == 'up' else '下移'}",
                     task_id=tid, table="task_events")
        return api_snapshot(req)

    @app.post("/api/v1/tasks/{tid}/claim")
    async def api_task_claim(req: Request, tid: int):
        user, error = api_user(req)
        if error:
            return error
        body = await req.json()
        try:
            client_id = str(uuid.UUID(str(body.get("client_id", ""))))
        except ValueError:
            return JSONResponse({"ok": False, "error": "客户端标识无效"}, status_code=400)
        task = db.one("SELECT id,status,claimed_client_id,cover_enabled,class_copies_json,"
                      "copies_per_class FROM tasks WHERE id=?", (tid,))
        if not task:
            return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
        if task["status"] != "QUEUED":
            return JSONResponse({"ok": False, "error": "任务已经开始或结束"}, status_code=409)
        plan = json.loads(task["class_copies_json"] or '[]')
        needs_new_client = bool(task["cover_enabled"] or
                                (plan and any(c != task["copies_per_class"] for c in plan)))
        if needs_new_client and body.get("class_cover_v1") is not True:
            return JSONResponse({"ok": False, "error": "请升级桌面客户端后打印此任务"}, status_code=409)
        if task["claimed_client_id"] == client_id:
            return {"ok": True}
        changed = db.update_count(
            "UPDATE tasks SET claimed_client_id=?, print_requested_at=? "
            "WHERE id=? AND status='QUEUED' AND claimed_client_id=''",
            (client_id, time.time(), tid))
        if not changed:
            return JSONResponse({"ok": False, "error": "任务已由另一台电脑领取"}, status_code=409)
        db.log_event("TASK_CLAIMED", f"{user['username']} 领取打印", task_id=tid, table="task_events")
        return {"ok": True}

    @app.post("/api/v1/tasks/{tid}/release")
    async def api_task_release(req: Request, tid: int):
        user, error = api_user(req)
        if error:
            return error
        body = await req.json()
        try:
            client_id = str(uuid.UUID(str(body.get("client_id", ""))))
        except ValueError:
            return JSONResponse({"ok": False, "error": "客户端标识无效"}, status_code=400)
        changed = db.update_count(
            "UPDATE tasks SET claimed_client_id='', print_requested_at=0 "
            "WHERE id=? AND status='QUEUED' AND claimed_client_id=?",
            (tid, client_id))
        if changed:
            db.log_event("TASK_RELEASED", f"{user['username']} 释放领取", task_id=tid,
                         table="task_events")
        return {"ok": True}

    @app.post("/api/v1/uploads/presign")
    async def api_upload_presign(req: Request):
        """创建上传占位任务，并签发只允许写入该对象的短期 COS URL。"""
        user, error = api_user(req)
        if error:
            return error
        if config.STORAGE_BACKEND == "local":
            return {"ok": True, "storage_backend": "local"}
        if config.STORAGE_BACKEND != "cos":
            return JSONResponse({"ok": False, "error": "云端尚未启用腾讯云 COS 直传"},
                                status_code=503)
        body = await req.json()
        school = str(body.get("school", "")).strip()
        grade = str(body.get("grade", "")).strip()
        filename = Path(str(body.get("filename", ""))).name
        try:
            classes = int(body.get("classes", 0))
            copies_per_class = int(body.get("copies", 0))
            pages = int(body.get("pages", 0))
            file_size = int(body.get("size", 0))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "任务参数无效"}, status_code=400)
        if not school or not grade or classes < 1 or copies_per_class < 1:
            return JSONResponse({"ok": False, "error": "请填写学校、年级、班级数和每班份数"},
                                status_code=400)
        if not any(p["school"] == school and p["grade"] == grade for p in school_presets()):
            return JSONResponse({"ok": False, "error": "请先在学校管理中创建对应的学校和年级"},
                                status_code=400)
        if not filename.lower().endswith(".pdf"):
            return JSONResponse({"ok": False, "error": "只支持 PDF 文件"}, status_code=400)
        if pages < 1 or pages > 100000 or file_size < 1 or file_size > 5 * 1024 ** 3:
            return JSONResponse({"ok": False, "error": "文件大小或原稿页数无效"}, status_code=400)
        title, filename_copies = importer.parse_filename(filename)
        if filename_copies and body.get("class_copies") is None:
            copies_per_class = filename_copies
        try:
            class_copies = validate_class_copies(classes, copies_per_class, body.get("class_copies"))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        cover_enabled = body.get("cover_enabled", False)
        if not isinstance(cover_enabled, bool):
            return JSONResponse({"ok": False, "error": "封面选项无效"}, status_code=400)
        total_copies = sum(class_copies)
        tid = db.execute(
            "INSERT INTO tasks(title,school,grade,classes,copies,copies_per_class,class_copies_json,"
            "cover_enabled,pages,sheets,note,source_user,status,pdf_path,pdf_name,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'',?,'UPLOADING','',?,?)",
            (title, school, grade, classes, total_copies, copies_per_class,
             json.dumps(class_copies), int(cover_enabled), pages,
             sheets_per_copy(pages) * total_copies + (classes if cover_enabled else 0),
             user["username"], filename, time.time()))
        try:
            location, upload_url = storage.prepare_upload(tid, filename)
            db.execute("UPDATE tasks SET pdf_path=? WHERE id=?", (location, tid))
        except Exception as exc:  # 配置或签名错误应立即反馈，且不留下可见任务
            db.execute("UPDATE tasks SET status='CANCELED', done_at=? WHERE id=?", (time.time(), tid))
            return JSONResponse({"ok": False, "error": f"COS 上传地址生成失败：{exc}"},
                                status_code=503)
        return {"ok": True, "task_id": tid, "upload_url": upload_url,
                "expires_in": config.COS_PRESIGN_SECONDS}

    @app.post("/api/v1/uploads/{tid}/file")
    async def api_upload_file(req: Request, tid: int):
        """浏览器无法直传时，同源中转到原 COS 对象，完成后仍需确认入队。"""
        user, error = api_user(req)
        if error:
            return error
        task = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not task or task["source_user"] != user["username"]:
            return JSONResponse({"ok": False, "error": "上传任务不存在"}, status_code=404)
        if task["status"] != "UPLOADING" or not task["pdf_path"].startswith("cos://"):
            return JSONResponse({"ok": False, "error": "上传任务状态无效"}, status_code=409)
        async with req.form() as form:
            upload = form.get("file")
            if not getattr(upload, "filename", "").lower().endswith(".pdf"):
                return JSONResponse({"ok": False, "error": "请选择 PDF 文件"}, status_code=400)
            if not upload.size or upload.size > 200 * 1024 ** 2:
                return JSONResponse({"ok": False, "error": "中转上传支持 200 MB 以内的文件，大文件请配置 COS 直传"}, status_code=413)
            try:
                await run_in_threadpool(storage.upload_stream, task["pdf_path"], upload.file)
            except Exception:
                return JSONResponse({"ok": False, "error": "服务器中转上传失败，请检查服务器的 COS 连接和写入权限"}, status_code=502)
        return {"ok": True, "task_id": tid}

    @app.post("/api/v1/uploads/{tid}/complete")
    def api_upload_complete(req: Request, tid: int):
        """浏览器直传完成后核实对象存在，再把任务放入打印队列。"""
        user, error = api_user(req)
        if error:
            return error
        task = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not task or task["source_user"] != user["username"]:
            return JSONResponse({"ok": False, "error": "上传任务不存在"}, status_code=404)
        if task["status"] == TaskStatus.QUEUED.value:
            return {"ok": True, "task_id": tid}
        if task["status"] != "UPLOADING" or not task["pdf_path"]:
            return JSONResponse({"ok": False, "error": "上传任务状态无效"}, status_code=409)
        if not storage.exists(task["pdf_path"]):
            return JSONResponse({"ok": False, "error": "COS 尚未收到完整文件，请重试上传"},
                                status_code=409)
        db.execute("UPDATE tasks SET status='QUEUED' WHERE id=?", (tid,))
        db.log_event("TASK_SUBMITTED",
                     f"《{task['title']}》已直传 COS，原稿{task['pages']}页，"
                     f"{task['classes']}班×{task['copies_per_class']}份",
                     task_id=tid, table="task_events")
        return {"ok": True, "task_id": tid}

    @app.delete("/api/v1/uploads/{tid}")
    def api_upload_abort(req: Request, tid: int):
        """直传失败时清理占位任务以及可能存在的半成品对象。"""
        user, error = api_user(req)
        if error:
            return error
        task = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not task or task["source_user"] != user["username"]:
            return JSONResponse({"ok": False, "error": "上传任务不存在"}, status_code=404)
        if task["status"] != "UPLOADING":
            return JSONResponse({"ok": False, "error": "上传任务状态无效"}, status_code=409)
        if task["pdf_path"]:
            try:
                storage.remove(task["pdf_path"])
            except Exception:  # 未形成对象时删除失败不影响清理占位任务
                pass
        db.execute("DELETE FROM tasks WHERE id=?", (tid,))
        return {"ok": True, "task_id": tid}

    @app.post("/api/v1/tasks")
    async def api_task_create(req: Request):
        """上传文件并直接采用客户端 PDF.js 已识别的原稿页数。"""
        user, error = api_user(req)
        if error:
            return error
        form = await req.form()
        school, grade = str(form.get("school", "")).strip(), str(form.get("grade", "")).strip()
        try:
            classes, copies_per_class = int(form.get("classes", 0)), int(form.get("copies", 0))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "班级数和每班份数必须为整数"}, status_code=400)
        if not school or not grade or classes < 1 or copies_per_class < 1:
            return JSONResponse({"ok": False, "error": "请填写学校、年级、班级数和每班份数"}, status_code=400)
        if not any(p["school"] == school and p["grade"] == grade for p in school_presets()):
            return JSONResponse({"ok": False, "error": "请先在学校管理中创建对应的学校和年级"}, status_code=400)
        upload = form.get("file")
        if not upload or not getattr(upload, "filename", ""):
            return JSONResponse({"ok": False, "error": "请选择 PDF 文件"}, status_code=400)
        if not upload.filename.lower().endswith(".pdf"):
            return JSONResponse({"ok": False, "error": "只支持 PDF 文件"}, status_code=400)
        content = await upload.read()
        try:
            pages = int(form.get("pages", 0) or 0)
        except (TypeError, ValueError):
            pages = 0
        if pages < 1 or pages > 100000:
            return JSONResponse({"ok": False, "error": "原稿页数无效"}, status_code=400)
        title, filename_copies = importer.parse_filename(upload.filename)
        raw_copies = form.get("class_copies")
        if filename_copies and raw_copies is None:
            copies_per_class = filename_copies
        try:
            class_copies = validate_class_copies(
                classes, copies_per_class, json.loads(raw_copies) if raw_copies is not None else None)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        cover_raw = form.get("cover_enabled", "false")
        if cover_raw not in ("true", "false"):
            return JSONResponse({"ok": False, "error": "封面选项无效"}, status_code=400)
        cover_enabled = cover_raw == "true"
        total_copies = sum(class_copies)
        tid = db.execute(
            "INSERT INTO tasks(title,school,grade,classes,copies,copies_per_class,class_copies_json,"
            "cover_enabled,pages,sheets,note,source_user,status,pdf_path,pdf_name,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'',?,'QUEUED','',?,?)",
            (title, school, grade, classes, total_copies, copies_per_class,
             json.dumps(class_copies), int(cover_enabled), pages,
             sheets_per_copy(pages) * total_copies + (classes if cover_enabled else 0),
             user["username"], upload.filename, time.time()))
        path = save_upload_by_date(tid, upload, content)
        db.execute("UPDATE tasks SET pdf_path=? WHERE id=?", (path, tid))
        db.log_event("TASK_SUBMITTED", f"《{title}》已上传，原稿{pages}页，{classes}班×{copies_per_class}份",
                     task_id=tid, table="task_events")
        return JSONResponse({"ok": True, "task_id": tid})

    @app.get("/submit")
    @app.get("/queue")
    @app.get("/admin")
    @app.get("/dashboard")
    def retired_page():
        return RedirectResponse("/desk/", status_code=307)

    # ---------- 正式桌面客户端同步接口 ----------
    @app.post("/api/agent/sync")
    async def agent_sync(req: Request):
        user = require_user(req)
        if not user:
            return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
        body = await req.json()
        try:
            client_id = str(uuid.UUID(str(body.get("client_id", ""))))
        except ValueError:
            return JSONResponse({"ok": False, "error": "客户端标识无效"}, status_code=400)
        if req.headers.get("X-Client-ID") != client_id:
            return JSONResponse({"ok": False, "error": "客户端标识不匹配"}, status_code=400)
        states = body.get("task_states", {})
        now = time.time()
        for cid, st in states.items():
            t = db.one("SELECT * FROM tasks WHERE id=?", (int(cid),))
            if not t or t["status"] in ("DONE", "CANCELED"):
                continue
            if t["claimed_client_id"] != client_id:
                continue
            if st in (TaskStatus.PRINTING.value, TaskStatus.AWAITING_PICKUP.value,
                      TaskStatus.DONE.value, TaskStatus.CANCELED.value):
                fields = {"status": st}
                if st == TaskStatus.PRINTING.value and not t["started_at"]:
                    fields["started_at"] = now
                if st in (TaskStatus.DONE.value, TaskStatus.CANCELED.value):
                    fields["done_at"] = now
                sets = ",".join(f"{k}=?" for k in fields)
                db.execute(f"UPDATE tasks SET {sets} WHERE id=?",
                           (*fields.values(), int(cid)))
                if st != t["status"]:
                    db.log_event("AGENT_REPORT", f"状态 → {st}", task_id=int(cid),
                                 table="task_events")
        # 后台页数核对完成前不派发，避免打印站拿到 pages=0 的占位任务。
        queued = db.q("SELECT * FROM tasks WHERE status='QUEUED' AND pages>0 "
                      "ORDER BY priority DESC, id")
        canceled = [r["id"] for r in db.q("SELECT id FROM tasks WHERE status='CANCELED'")]
        priorities = {str(r["id"]): r["priority"] for r in
                      db.q("SELECT id,priority FROM tasks WHERE status NOT IN ('DONE','CANCELED')")}
        return {"ok": True,
                "new_tasks": [{"id": t["id"], "title": t["title"], "school": t["school"],
                               "grade": t["grade"], "classes": t["classes"],
                               "copies": t["copies"], "note": t["note"],
                               "source_user": t["source_user"], "priority": t["priority"],
                               "pdf_name": t["pdf_name"]} for t in queued],
                "canceled": canceled, "priorities": priorities}

    @app.get("/api/agent/file/{tid}")
    def agent_file(req: Request, tid: int):
        user = require_user(req)
        if not user:
            return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
        try:
            uuid.UUID(req.headers.get("X-Client-ID", ""))
        except ValueError:
            return JSONResponse({"ok": False, "error": "客户端标识无效"}, status_code=400)
        t = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not t or not t["pdf_path"]:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        if t["status"] != "QUEUED":
            return JSONResponse({"ok": False, "error": "只能下载尚未开始的排队任务"}, status_code=403)
        direct_url = storage.download_url(t["pdf_path"])
        if direct_url:
            # requests 默认跟随重定向；打印站由 COS 直下，不让 PDF 再穿过 Tunnel。
            return RedirectResponse(direct_url, status_code=307)
        try:
            content = storage.read(t["pdf_path"])
        except (OSError, RuntimeError):
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        return Response(content=content, media_type="application/pdf",
                        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(t["pdf_name"], safe="")})

    # 当前可交互原型的静态构建产物。它必须在所有 API 路由之后挂载，避免吞掉 /api。
    desk_dir = Path(__file__).parent / "static" / "desk"
    if desk_dir.is_dir():
        # vinext 静态导出使用站点根路径 /_next/... 引用 CSS 与 JS。
        # 页面本身位于 /desk/，因此需要单独暴露构建资源目录。
        next_dir = desk_dir / "_next"
        if next_dir.is_dir():
            app.mount("/_next", StaticFiles(directory=str(next_dir)), name="desk-next")
        app.mount("/desk", StaticFiles(directory=str(desk_dir), html=True), name="desk")
    return app
