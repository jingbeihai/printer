# -*- coding: utf-8 -*-
"""
本地代理网页(学校局域网): 状态墙 / 任务(闸口) / 机队 / 日志。
LOCAL_PASSWORD 为空时免登录(内网场景)。
"""
import time

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import config, db, importer
from .models import STATE_COLOR, TASK_STATUS_CN
from .models import PrinterState as PS
from .webcommon import jinja, make_session, read_session, render

SESSION_COOKIE = "pc_local"


def create_app(executor, syncer) -> FastAPI:
    app = FastAPI(title="打印集群·本地代理", docs_url=None, redoc_url=None)
    # 打印站与云端页面共用同一顶栏；本地页可跳回云端的上传、学校与统计模块。
    jinja.globals["cloud_url"] = (config.CLOUD_URL or "").rstrip("/")

    def authed(req: Request) -> bool:
        if not config.LOCAL_PASSWORD:
            return True
        return read_session(req.cookies.get(SESSION_COOKIE)) is not None

    def guard(req: Request):
        return None if authed(req) else RedirectResponse("/login", 303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        if not config.LOCAL_PASSWORD:
            return RedirectResponse("/wall", 303)
        return render("local_login.html", error="")

    @app.post("/login")
    def login(password: str = Form(...)):
        if not config.LOCAL_PASSWORD:
            return RedirectResponse("/wall", 303)
        if password == config.LOCAL_PASSWORD:
            resp = RedirectResponse("/wall", 303)
            resp.set_cookie(SESSION_COOKIE, make_session({"role": "staff"}, hours=24 * 30),
                            httponly=True)
            return resp
        return render("local_login.html", error="口令错误")

    @app.get("/")
    def index():
        return RedirectResponse("/tasks", 303)

    # ---------------- 状态墙 ----------------
    @app.get("/wall", response_class=HTMLResponse)
    def wall(req: Request):
        g = guard(req)
        if g:
            return g
        printers = [dict(p) for p in db.q("SELECT * FROM printers WHERE enabled=1 ORDER BY name")]
        for p in printers:
            grp = db.one("SELECT g.*, t.title ttitle FROM groups g"
                         " JOIN local_tasks t ON t.id=g.task_id"
                         " WHERE g.id=?", (p["current_group"],)) if p["current_group"] else None
            p["group"] = dict(grp) if grp else None
            if p["group"] and p["group"]["start_counter"]:
                done = max(0, p["page_counter"] - p["group"]["start_counter"])
                p["progress"] = min(100, int(done * 100 / max(1, p["group"]["sheets"])))
            else:
                p["progress"] = 0
            if hasattr(executor.backend, "tray_of"):
                p["tray_sheets"] = executor.backend.tray_of(p["id"])
        task = importer.current_active_task()
        stats = _queue_stats()
        return render("wall.html", printers=printers, task=task, colors=STATE_COLOR,
                      sync=syncer.status_line() if syncer else "纯本地模式", stats=stats)

    # ---------------- 任务页(闸口) ----------------
    @app.get("/tasks", response_class=HTMLResponse)
    def tasks(req: Request):
        g = guard(req)
        if g:
            return g
        active = importer.current_active_task()
        groups = []
        if active:
            groups = db.q("SELECT g.*, p.name pname FROM groups g"
                          " LEFT JOIN printers p ON p.id=g.printer_id"
                          " WHERE g.task_id=? ORDER BY seq", (active["id"],))
        queued = db.q("SELECT * FROM local_tasks WHERE status='QUEUED'"
                      " ORDER BY priority DESC, cloud_id")
        recent = db.q("SELECT * FROM local_tasks WHERE status IN ('DONE','CANCELED')"
                      " ORDER BY id DESC LIMIT 20")
        return render("ltasks.html", active=active, groups=groups, queued=queued,
                      recent=recent, cn=TASK_STATUS_CN, ts=time)

    @app.post("/tasks/{tid}/pickup")
    def pickup(req: Request, tid: int):
        g = guard(req)
        if g:
            return g
        importer.confirm_pickup(tid)
        return RedirectResponse("/tasks", 303)

    # ---------------- 打印机按钮 ----------------
    @app.post("/printer/{pid}/act")
    def printer_act(req: Request, pid: int, act: str = Form(...)):
        g = guard(req)
        if g:
            return g
        {
            "fault": executor.act_fault,
            "reset": executor.act_reset,
            "refill": executor.act_refill,
            "reassign": executor.act_reassign,
        }.get(act, lambda _pid: None)(pid)
        return RedirectResponse("/wall", 303)

    # ---------------- 机队管理 ----------------
    @app.get("/fleet", response_class=HTMLResponse)
    def fleet(req: Request):
        g = guard(req)
        if g:
            return g
        printers = db.q("SELECT * FROM printers ORDER BY name")
        return render("fleet.html", printers=printers, colors=STATE_COLOR,
                      roller_warn=config.ROLLER_WARN_PAGES, sim=config.BACKEND == "sim",
                      states=[s.value for s in PS],
                      clock=lambda value: time.strftime("%H:%M", time.localtime(value)))

    @app.post("/fleet/{pid}/rest/finish")
    def fleet_finish_rest(req: Request, pid: int, reason: str = Form("")):
        g = guard(req)
        if g:
            return g
        p = db.one("SELECT * FROM printers WHERE id=?", (pid,))
        if p and p["state"] == PS.RESTING.value and reason.strip():
            now = time.time()
            db.execute("UPDATE printers SET state=?, rest_until=0, continuous_print_seconds=0 WHERE id=?",
                       (PS.IDLE.value, pid))
            db.execute("UPDATE printer_rest_sessions SET ended_at=?, override_user='现场管理员', "
                       "override_reason=? WHERE printer_id=? AND ended_at IS NULL",
                       (now, reason.strip(), pid))
            db.log_event("PRINTER_REST_OVERRIDDEN", f"管理员提前结束休息：{reason.strip()}", printer_id=pid)
        return RedirectResponse("/fleet", 303)

    @app.post("/fleet/add")
    def fleet_add(req: Request, name: str = Form(...), ip: str = Form(""),
                  queue_name: str = Form("")):
        g = guard(req)
        if g:
            return g
        name = name.strip()
        if name and not db.one("SELECT id FROM printers WHERE name=?", (name,)):
            db.execute("INSERT INTO printers(name,ip,queue_name,state) VALUES(?,?,?,?)",
                       (name, ip.strip(), queue_name.strip(), PS.IDLE.value))
            db.log_event("PRINTER_ADDED", f"新机器上线:{name}")
        return RedirectResponse("/fleet", 303)

    @app.post("/fleet/{pid}/del")
    def fleet_del(req: Request, pid: int):
        g = guard(req)
        if g:
            return g
        db.execute("UPDATE printers SET enabled=0 WHERE id=?", (pid,))
        db.log_event("PRINTER_REMOVED", "机器下架", printer_id=pid)
        return RedirectResponse("/fleet", 303)

    @app.post("/fleet/{pid}/inject")
    def fleet_inject(req: Request, pid: int, fault: str = Form("")):
        g = guard(req)
        if g:
            return g
        executor.act_sim_inject(pid, fault or None)
        return RedirectResponse("/fleet", 303)

    @app.post("/fleet/maintenance")
    def fleet_maint(req: Request):
        g = guard(req)
        if g:
            return g
        executor.send_maintenance_pages()
        return RedirectResponse("/fleet", 303)

    # ---------------- 日志 ----------------
    @app.get("/events", response_class=HTMLResponse)
    def events(req: Request):
        g = guard(req)
        if g:
            return g
        rows = db.q("SELECT e.*, p.name pname FROM events e"
                    " LEFT JOIN printers p ON p.id=e.printer_id"
                    " ORDER BY e.id DESC LIMIT 200")
        return render("events.html", events=rows, ts=time)

    # ---------------- 状态墙轮询 API ----------------
    @app.get("/api/state")
    def api_state(req: Request):
        if not authed(req):
            return {"ok": False}
        printers = [dict(p) for p in db.q("SELECT id,name,state,page_counter,pages_total,"
                                          "tray_sheets,current_group FROM printers"
                                          " WHERE enabled=1 ORDER BY name")]
        for p in printers:
            grp = db.one("SELECT label,sheets,start_counter FROM groups WHERE id=?",
                         (p["current_group"],)) if p["current_group"] else None
            p["group"] = dict(grp) if grp else None
            p["progress"] = 0
            if grp and grp["start_counter"]:
                done = max(0, p["page_counter"] - grp["start_counter"])
                p["progress"] = min(100, int(done * 100 / max(1, grp["sheets"])))
            if hasattr(executor.backend, "tray_of"):
                p["tray_sheets"] = executor.backend.tray_of(p["id"])
        task = importer.current_active_task()
        return {"ok": True, "printers": printers, "colors": STATE_COLOR,
                "task": dict(task) if task else None, "stats": _queue_stats()}

    return app


def _queue_stats():
    return {
        "queued_tasks": db.one("SELECT COUNT(*) c FROM local_tasks WHERE status='QUEUED'")["c"],
        "queued_groups": db.one("SELECT COUNT(*) c FROM groups WHERE status='QUEUED'")["c"],
        "done_groups": db.one("SELECT COUNT(*) c FROM groups WHERE status='DONE'")["c"],
    }
