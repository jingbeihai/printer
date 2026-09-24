import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, db, server_web
from app.webcommon import hash_password, make_session


class TaskQueueActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.object(config, "INVITE_CODE", ""), patch.object(
                config, "BOOTSTRAP_ADMIN_PASSWORD", "test-password-123"), patch.object(
                config, "UPLOAD_DIR", Path(self.temp.name)), patch.object(
                server_web, "start_retention_thread"):
            db.init(":memory:", db.SERVER_SCHEMA)
            self.client = TestClient(server_web.create_app())
        self.addCleanup(self.client.close)
        self.addCleanup(lambda: db._conn.close())
        admin = db.one("SELECT id FROM users WHERE role='superadmin'")
        self.client.cookies.set(server_web.SESSION_COOKIE, make_session({"uid": admin["id"]}))
        self.worker_id = db.execute(
            "INSERT INTO users(username,pw_hash,role,created_at) VALUES(?,?,?,?)",
            ("worker", hash_password("password-123"), "user", time.time()))

    def task(self, title, owner="worker", status="QUEUED", claimed=""):
        return db.execute(
            "INSERT INTO tasks(title,copies,pages,sheets,source_user,status,"
            "claimed_client_id,pdf_path,pdf_name,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (title, 1, 1, 1, owner, status, claimed,
             f"cos://bucket/{title}.pdf", f"{title}.pdf", time.time()))

    def order(self):
        return [row["id"] for row in self.client.get("/api/v1/bootstrap").json()["tasks"]]

    def test_move_changes_cloud_queue_and_skips_claimed_tasks(self):
        first = self.task("first")
        claimed = self.task("claimed", claimed="client-a")
        last = self.task("last")
        self.assertEqual(self.order(), [last, first, claimed])
        moved = self.client.post(f"/api/v1/tasks/{first}/move", json={"direction": "up"})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(self.order(), [first, last, claimed])
        self.assertEqual(self.client.post(
            f"/api/v1/tasks/{first}/move", json={"direction": "up"}).status_code, 409)
        self.assertEqual(self.client.post(
            f"/api/v1/tasks/{claimed}/move", json={"direction": "down"}).status_code, 409)
        self.assertEqual(self.client.post(
            f"/api/v1/tasks/{last}/move", json={"direction": "sideways"}).status_code, 400)
        self.client.cookies.set(server_web.SESSION_COOKIE, make_session({"uid": self.worker_id}))
        self.assertEqual(self.client.post(
            f"/api/v1/tasks/{last}/move", json={"direction": "up"}).status_code, 200)
        self.assertEqual(self.order(), [last, first, claimed])

    def test_cancel_checks_owner_and_started_state_without_deleting_object(self):
        own = self.task("own")
        other = self.task("other", owner="someone-else")
        started = self.task("started", status="PRINTING")
        claimed = self.task("claimed", claimed="client-a")
        self.client.cookies.set(server_web.SESSION_COOKIE, make_session({"uid": self.worker_id}))
        self.assertEqual(self.client.post(f"/api/v1/tasks/{other}/cancel").status_code, 403)
        self.assertEqual(self.client.post(f"/api/v1/tasks/{started}/cancel").status_code, 409)
        self.assertEqual(self.client.post(f"/api/v1/tasks/{claimed}/cancel").status_code, 409)
        self.assertEqual(self.client.post(f"/api/v1/tasks/{own}/cancel").status_code, 200)
        row = db.one("SELECT status,pdf_path FROM tasks WHERE id=?", (own,))
        self.assertEqual(row["status"], "CANCELED")
        self.assertEqual(row["pdf_path"], "cos://bucket/own.pdf")


if __name__ == "__main__":
    unittest.main()
