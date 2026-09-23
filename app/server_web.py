# -*- coding: utf-8 -*-
"""
云端任务中心网页 + 代理接口。
页面: 注册/登录/提交/队列/仪表盘(台账)/管理
接口: POST /api/agent/sync  GET /api/agent/file/{id}
"""
import csv
import io
import json
import secrets
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, Request, Response, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)
from fastapi.staticfiles import StaticFiles

from . import config, db, importer, storage
from .compose import sheets_per_copy
from .models import ROLE_ADMIN, ROLE_SUPERADMIN, ROLE_USER, TASK_STATUS_CN, TaskStatus
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
    # 显式环境变量始终覆盖旧值，便于管理员轮换打印站凭据；
    # 未提供时首次自动生成，绝不再使用公开的开发默认值。
    if config.AGENT_TOKEN:
        db.set_conf("agent_token", config.AGENT_TOKEN)
    elif not db.get_conf("agent_token"):
        db.set_conf("agent_token", secrets.token_urlsafe(32))
    # 首次初始化创建超级管理员；升级旧库时保留原密码并提升最早的管理员。
    if db.one("SELECT COUNT(*) c FROM users")["c"] == 0:
        if len(config.BOOTSTRAP_ADMIN_PASSWORD) < 8:
            raise RuntimeError("首次部署请设置 PRINT_BOOTSTRAP_ADMIN_PASSWORD（至少 8 位）")
        db.execute("INSERT INTO users(username,pw_hash,role,created_at) VALUES(?,?,?,?)",
                   (config.BOOTSTRAP_ADMIN_USERNAME,
                    hash_password(config.BOOTSTRAP_ADMIN_PASSWORD), ROLE_SUPERADMIN, time.time()))
    elif not db.one("SELECT id FROM users WHERE role=?", (ROLE_SUPERADMIN,)):
        oldest = db.one("SELECT id FROM users WHERE role=? ORDER BY id LIMIT 1", (ROLE_ADMIN,))
        if not oldest:
            oldest = db.one("SELECT id FROM users ORDER BY id LIMIT 1")
        if oldest:
            db.execute("UPDATE users SET role=?,active=1 WHERE id=?",
                       (ROLE_SUPERADMIN, oldest["id"]))
    db.execute("UPDATE users SET role=? WHERE role IN ('admin','teacher')", (ROLE_USER,))


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
        return [dict(row) for row in
                db.q("SELECT * FROM school_presets ORDER BY school, grade")]

    # React 桌面界面使用这一组同源接口。旧的 Jinja 页面仍可访问，便于已打开的
    # 浏览器会话平滑过渡；新 EXE 不再以那些页面作为产品界面。
    def api_user(req: Request):
        user = require_user(req)
        if not user:
            return None, JSONResponse({"ok": False, "error": "登录已失效"}, status_code=401)
        return user, None

    def api_task(row):
        task = dict(row)
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
            "SELECT * FROM tasks WHERE status<>'UPLOADING' ORDER BY priority DESC, id DESC")]
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
        if user["role"] != ROLE_SUPERADMIN:
            return JSONResponse({"ok": False, "error": "仅超级管理员可维护学校"}, status_code=403)
        body = await req.json()
        school, grade = str(body.get("school", "")).strip(), str(body.get("grade", "")).strip()
        try:
            classes, copies = int(body.get("classes")), int(body.get("copies"))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "班级数和份数必须为整数"}, status_code=400)
        if not school or not grade or classes < 1 or copies < 1:
            return JSONResponse({"ok": False, "error": "请填写学校、年级、班级数和每班份数"}, status_code=400)
        db.execute("INSERT INTO school_presets(school,grade,classes,copies) VALUES(?,?,?,?) "
                   "ON CONFLICT(school,grade) DO UPDATE SET classes=excluded.classes,copies=excluded.copies",
                   (school, grade, classes, copies))
        return api_snapshot(req)

    @app.put("/api/v1/schools/{sid}")
    async def api_school_update(req: Request, sid: int):
        user, error = api_user(req)
        if error:
            return error
        if user["role"] != ROLE_SUPERADMIN:
            return JSONResponse({"ok": False, "error": "仅超级管理员可维护学校"}, status_code=403)
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
        db.execute("UPDATE school_presets SET school=?,grade=?,classes=?,copies=? WHERE id=?",
                   (school, grade, classes, copies, sid))
        return api_snapshot(req)

    @app.delete("/api/v1/schools/{sid}")
    def api_school_delete(req: Request, sid: int):
        user, error = api_user(req)
        if error:
            return error
        if user["role"] != ROLE_SUPERADMIN:
            return JSONResponse({"ok": False, "error": "仅超级管理员可维护学校"}, status_code=403)
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
        return [dict(row) for row in db.q(
            "SELECT id,username,role,active,display_name,created_at FROM users ORDER BY id")]

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
        role = str(body.get("role", ROLE_USER))
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
        role = str(body.get("role", target["role"]))
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
        if task["status"] in (TaskStatus.DONE.value, TaskStatus.CANCELED.value):
            return JSONResponse({"ok": False, "error": "该任务不能取消"}, status_code=400)
        db.execute("UPDATE tasks SET status='CANCELED', done_at=? WHERE id=?", (time.time(), tid))
        db.log_event("TASK_CANCELED", f"{user['username']} 取消任务", task_id=tid, table="task_events")
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
        task = db.one("SELECT id,status,claimed_client_id FROM tasks WHERE id=?", (tid,))
        if not task:
            return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
        if task["status"] != "QUEUED":
            return JSONResponse({"ok": False, "error": "任务已经开始或结束"}, status_code=409)
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
        if filename_copies:
            copies_per_class = filename_copies
        total_copies = classes * copies_per_class
        tid = db.execute(
            "INSERT INTO tasks(title,school,grade,classes,copies,copies_per_class,pages,sheets,note,"
            "source_user,status,pdf_path,pdf_name,created_at) VALUES(?,?,?,?,?,?,?,?,'',?,'UPLOADING','',?,?)",
            (title, school, grade, classes, total_copies, copies_per_class, pages,
             sheets_per_copy(pages) * total_copies, user["username"], filename, time.time()))
        try:
            location, upload_url = storage.prepare_upload(tid, filename)
            db.execute("UPDATE tasks SET pdf_path=? WHERE id=?", (location, tid))
        except Exception as exc:  # 配置或签名错误应立即反馈，且不留下可见任务
            db.execute("UPDATE tasks SET status='CANCELED', done_at=? WHERE id=?", (time.time(), tid))
            return JSONResponse({"ok": False, "error": f"COS 上传地址生成失败：{exc}"},
                                status_code=503)
        return {"ok": True, "task_id": tid, "upload_url": upload_url,
                "expires_in": config.COS_PRESIGN_SECONDS}

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
        if filename_copies:
            copies_per_class = filename_copies
        total_copies = classes * copies_per_class
        tid = db.execute(
            "INSERT INTO tasks(title,school,grade,classes,copies,copies_per_class,pages,sheets,note,"
            "source_user,status,pdf_path,pdf_name,created_at) VALUES(?,?,?,?,?,?,?,?,'',?,'QUEUED','',?,?)",
            (title, school, grade, classes, total_copies, copies_per_class, pages,
             sheets_per_copy(pages) * total_copies,
             user["username"], upload.filename, time.time()))
        path = save_upload_by_date(tid, upload, content)
        db.execute("UPDATE tasks SET pdf_path=? WHERE id=?", (path, tid))
        db.log_event("TASK_SUBMITTED", f"《{title}》已上传，原稿{pages}页，{classes}班×{copies_per_class}份",
                     task_id=tid, table="task_events")
        return JSONResponse({"ok": True, "task_id": tid})

    @app.get("/submit", response_class=HTMLResponse)
    def submit_page(req: Request):
        u = require_user(req)
        if not u:
            return RedirectResponse("/login", 303)
        profile = json.loads(u["profile_json"] or "{}")
        return render("submit.html", user=u, p=profile, presets=school_presets(), saved=False)

    @app.post("/submit")
    async def submit(req: Request, school: str = Form(""), grade: str = Form(""),
                     classes: int = Form(0), copies: int = Form(1), note: str = Form("")):
        u = require_user(req)
        if not u:
            return RedirectResponse("/login", 303)
        presets = school_presets()
        if presets and not any(x["school"] == school.strip() and
                               x["grade"] == grade.strip() for x in presets):
            profile = {"school": school.strip(), "grade": grade.strip(),
                       "classes": classes, "copies": copies, "note": note.strip()}
            return render("submit.html", user=u, p=profile, presets=presets, saved=False,
                          error="请选择后台管理中已有的学校和年级")
        form = await req.form()
        # 注意: req.form() 返回 starlette 的 UploadFile,不能拿 fastapi.UploadFile 做 isinstance
        files = [v for v in form.getlist("files") if getattr(v, "filename", None)]
        if not files:
            profile = json.loads(u["profile_json"] or "{}")
            return render("submit.html", user=u, p=profile, presets=presets, saved=False,
                          error="请至少选择一个 PDF 文件")
        created = []
        for up in files:
            content = await up.read()
            title, file_copies = importer.parse_filename(up.filename)
            use_copies = file_copies or copies
            # 先入库占位拿 id,再存文件,再读真实页数补录
            tid = db.execute(
                "INSERT INTO tasks(title,school,grade,classes,copies,pages,sheets,note,"
                "source_user,status,pdf_path,pdf_name,created_at)"
                " VALUES(?,?,?,?,?,0,0,?,?,'QUEUED','',?,?)",
                (title, school.strip(), grade.strip(), classes, use_copies,
                 note.strip(), u["username"], up.filename, time.time()))
            path = save_upload_by_date(tid, up, content)
            from .compose import sheets_per_copy
            pages = storage.page_count(content)
            db.execute("UPDATE tasks SET pdf_path=?, pages=?, sheets=? WHERE id=?",
                       (path, pages, sheets_per_copy(pages) * use_copies, tid))
            db.log_event("TASK_SUBMITTED",
                         f"《{title}》 {pages}页×{use_copies}份 由 {u['username']} 提交",
                         task_id=tid, table="task_events")
            created.append(f"《{title}》{use_copies}份")
        # 表单记忆
        profile = {"school": school.strip(), "grade": grade.strip(),
                   "classes": classes, "copies": copies, "note": note.strip()}
        db.execute("UPDATE users SET profile_json=? WHERE id=?",
                   (json.dumps(profile, ensure_ascii=False), u["id"]))
        return render("submit.html", user=u, p=profile, presets=presets, saved=True,
                      message="已提交: " + "、".join(created))

    # ---------- 队列 ----------
    @app.get("/queue", response_class=HTMLResponse)
    def queue_page(req: Request):
        u = require_user(req)
        if not u:
            return RedirectResponse("/login", 303)
        tasks = db.q("SELECT * FROM tasks WHERE status NOT IN ('DONE','CANCELED')"
                     " ORDER BY priority DESC, id")
        history = db.q("SELECT * FROM tasks WHERE status IN ('DONE','CANCELED')"
                       " ORDER BY id DESC LIMIT 30")
        return render("queue.html", user=u, tasks=tasks, history=history,
                      cn=TASK_STATUS_CN, admin=u["role"] in (ROLE_ADMIN, ROLE_SUPERADMIN))

    @app.post("/admin/task/{tid}/top")
    def task_top(req: Request, tid: int):
        u = require_user(req)
        if not u or u["role"] not in (ROLE_ADMIN, ROLE_SUPERADMIN):
            return RedirectResponse("/login", 303)
        maxp = db.one("SELECT COALESCE(MAX(priority),0) m FROM tasks")["m"]
        db.execute("UPDATE tasks SET priority=? WHERE id=?", (maxp + 1, tid))
        db.log_event("TASK_TOPPED", "管理员插队置顶", task_id=tid, table="task_events")
        return RedirectResponse("/queue", 303)

    @app.post("/admin/task/{tid}/cancel")
    def task_cancel(req: Request, tid: int):
        u = require_user(req)
        if not u or u["role"] not in (ROLE_ADMIN, ROLE_SUPERADMIN):
            return RedirectResponse("/login", 303)
        db.execute("UPDATE tasks SET status='CANCELED', done_at=? WHERE id=?",
                   (time.time(), tid))
        db.log_event("TASK_CANCELED", "管理员取消", task_id=tid, table="task_events")
        return RedirectResponse("/queue", 303)

    # ---------- 仪表盘/台账 ----------
    def day_range(value: str):
        start = time.mktime(time.strptime(value, "%Y-%m-%d"))
        return start, start + 86400

    def month_range(value: str):
        year, month = (int(x) for x in value.split("-"))
        start = time.mktime(time.strptime(f"{year:04d}-{month:02d}", "%Y-%m"))
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        end = time.mktime(time.strptime(f"{next_year:04d}-{next_month:02d}", "%Y-%m"))
        return start, end

    def print_stats(start: float, end: float):
        return db.one("SELECT COUNT(*) c, COALESCE(SUM(copies),0) cp,"
                      " COALESCE(SUM(pages*copies),0) pg, COALESCE(SUM(sheets),0) s"
                      " FROM tasks WHERE status='DONE' AND done_at>=? AND done_at<?",
                      (start, end))

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(req: Request, period: str = "day", date: str = "", month: str = ""):
        u = require_user(req)
        if not u:
            return RedirectResponse("/login", 303)
        today = time.strftime("%Y-%m-%d")
        current_month = today[:7]
        today_stats = print_stats(*day_range(today))
        month_stats = print_stats(*month_range(current_month))

        selected_period = "month" if period == "month" else "day"
        try:
            if selected_period == "month":
                selected_value = month or current_month
                selected_range = month_range(selected_value)
                selected_label = f"{selected_value} 月度打印记录"
            else:
                selected_value = date or today
                selected_range = day_range(selected_value)
                selected_label = f"{selected_value} 打印记录"
        except (ValueError, TypeError):
            selected_period, selected_value = "day", today
            selected_range = day_range(today)
            selected_label = f"{today} 打印记录"

        manifest = db.q("SELECT * FROM tasks WHERE status='DONE'"
                        " AND done_at>=? AND done_at<? ORDER BY done_at", selected_range)
        selected_stats = print_stats(*selected_range)
        return render("dashboard.html", user=u, today=today, current_month=current_month,
                      today_stats=today_stats, month_stats=month_stats,
                      selected_period=selected_period, selected_value=selected_value,
                      selected_label=selected_label, selected_stats=selected_stats,
                      manifest=manifest, ts=time)

    @app.get("/dashboard.csv")
    def dashboard_csv(req: Request, period: str = "day", date: str = "", month: str = ""):
        u = require_user(req)
        if not u:
            return RedirectResponse("/login", 303)
        try:
            value = month if period == "month" else date
            start, end = month_range(value) if period == "month" else day_range(value)
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "invalid period"}, status_code=400)
        rows = db.q("SELECT * FROM tasks WHERE status='DONE' AND done_at>=? AND done_at<?"
                    " ORDER BY done_at", (start, end))
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["任务", "学校", "年级", "班级数", "份数", "原稿页数",
                         "打印总页数", "估算用纸", "提交人", "提交时间", "完成时间"])
        for r in rows:
            writer.writerow([
                r["title"], r["school"], r["grade"], r["classes"], r["copies"], r["pages"],
                r["pages"] * r["copies"], r["sheets"], r["source_user"],
                time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"])),
                time.strftime("%Y-%m-%d %H:%M", time.localtime(r["done_at"]))])
        return PlainTextResponse("﻿" + output.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition":
                                          f"attachment; filename=manifest_{value}.csv"})

    # ---------- 管理 ----------
    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(req: Request):
        u = require_user(req)
        if not u or u["role"] != ROLE_SUPERADMIN:
            return RedirectResponse("/login", 303)
        users = db.q("SELECT * FROM users ORDER BY id")
        schools = school_presets()
        return render("admin.html", user=u, users=users, schools=schools,
                      invite=db.get_conf("invite_code"), ts=time)

    @app.post("/admin/users/{uid}/delete")
    def delete_user(req: Request, uid: int):
        u = require_user(req)
        if not u or u["role"] != ROLE_SUPERADMIN:
            return RedirectResponse("/login", 303)
        target = db.one("SELECT role FROM users WHERE id=?", (uid,))
        if uid != u["id"] and target and target["role"] != ROLE_SUPERADMIN:
            db.execute("DELETE FROM users WHERE id=?", (uid,))
        return RedirectResponse("/admin", 303)

    @app.post("/admin/users/{uid}/promote")
    def promote_user(req: Request, uid: int):
        u = require_user(req)
        if not u or u["role"] != ROLE_SUPERADMIN:
            return RedirectResponse("/login", 303)
        db.execute("UPDATE users SET role=? WHERE id=?", (ROLE_SUPERADMIN, uid))
        return RedirectResponse("/admin", 303)

    @app.post("/admin/schools/add")
    def add_school(req: Request, school: str = Form(...), grade: str = Form(...),
                   classes: int = Form(0), copies: int = Form(1)):
        u = require_user(req)
        if not u or u["role"] != ROLE_SUPERADMIN:
            return RedirectResponse("/login", 303)
        school, grade = school.strip(), grade.strip()
        if school and grade and classes >= 0 and copies >= 1:
            db.execute("INSERT INTO school_presets(school,grade,classes,copies)"
                       " VALUES(?,?,?,?) ON CONFLICT(school,grade) DO UPDATE SET"
                       " classes=excluded.classes,copies=excluded.copies",
                       (school, grade, classes, copies))
        return RedirectResponse("/admin", 303)

    @app.post("/admin/schools/{sid}/delete")
    def delete_school(req: Request, sid: int):
        u = require_user(req)
        if not u or u["role"] != ROLE_SUPERADMIN:
            return RedirectResponse("/login", 303)
        db.execute("DELETE FROM school_presets WHERE id=?", (sid,))
        return RedirectResponse("/admin", 303)

    @app.post("/admin/settings")
    def settings(req: Request, invite: str = Form("")):
        u = require_user(req)
        if not u or u["role"] != ROLE_SUPERADMIN:
            return RedirectResponse("/login", 303)
        if invite.strip():
            db.set_conf("invite_code", invite.strip())
        return RedirectResponse("/admin", 303)

    # ---------- 代理接口 ----------
    def check_agent(req: Request):
        token = req.headers.get("X-Agent-Token", "")
        expected = db.get_conf("agent_token")
        return bool(token and expected and secrets.compare_digest(token, expected))

    @app.post("/api/agent/sync")
    async def agent_sync(req: Request):
        legacy_agent = check_agent(req)
        user = current_user(req) if not legacy_agent else None
        if not legacy_agent and not user:
            return JSONResponse({"ok": False, "error": "token invalid"}, status_code=401)
        body = await req.json()
        client_id = str(body.get("client_id", "")) if not legacy_agent else ""
        if not legacy_agent:
            try:
                client_id = str(uuid.UUID(client_id))
            except ValueError:
                return JSONResponse({"ok": False, "error": "客户端标识无效"}, status_code=400)
        states = body.get("task_states", {})
        now = time.time()
        for cid, st in states.items():
            t = db.one("SELECT * FROM tasks WHERE id=?", (int(cid),))
            if not t or t["status"] in ("DONE", "CANCELED"):
                continue
            if not legacy_agent and t["claimed_client_id"] != client_id:
                continue
            if st in (TaskStatus.PRINTING.value, TaskStatus.AWAITING_PICKUP.value,
                      TaskStatus.DONE.value):
                fields = {"status": st}
                if st == TaskStatus.PRINTING.value and not t["started_at"]:
                    fields["started_at"] = now
                if st == TaskStatus.DONE.value:
                    fields["done_at"] = now
                sets = ",".join(f"{k}=?" for k in fields)
                db.execute(f"UPDATE tasks SET {sets} WHERE id=?",
                           (*fields.values(), int(cid)))
                if st != t["status"]:
                    db.log_event("AGENT_REPORT", f"状态 → {st}", task_id=int(cid),
                                 table="task_events")
        # 后台页数核对完成前不派发，避免打印站拿到 pages=0 的占位任务。
        queue_filter = " AND claimed_client_id=''" if legacy_agent else ""
        queued = db.q("SELECT * FROM tasks WHERE status='QUEUED' AND pages>0" +
                      queue_filter + " ORDER BY priority DESC, id")
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
        legacy_agent = check_agent(req)
        user = current_user(req) if not legacy_agent else None
        if not legacy_agent and not user:
            return JSONResponse({"ok": False, "error": "token invalid"}, status_code=401)
        t = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not t or not t["pdf_path"]:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        if not legacy_agent and t["status"] != "QUEUED":
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
                        headers={"Content-Disposition": f'attachment; filename="{t["pdf_name"]}"'})

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
