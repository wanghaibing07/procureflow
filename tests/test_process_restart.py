"""Persistence across real application processes, using an isolated SQLite database."""

import json
import os
import sqlite3
import subprocess
import sys
from secrets import token_hex
from uuid import uuid4


# Each invocation starts a fresh Python process and FastAPI application. TestClient
# invokes the HTTP API in-process; this intentionally does not test TCP delivery.
_APPLICATION = r'''
import json
import os
import sys
from fastapi.testclient import TestClient
from procureflow.api import create_app
from procureflow.settings import Settings

data = json.load(sys.stdin)
settings = Settings(data["database_url"], data["buyer"], data["approver"],
                    "http://testserver", data["erp"])
headers = {"Authorization": "Bearer " + data["buyer"]}
with TestClient(create_app(settings)) as client:
    if data["phase"] == "create":
        preview = client.post("/api/drafts/preview", headers=headers, json={"skus": ["BRACKET"]})
        assert preview.status_code == 200, preview.text
        token = preview.json()["preview_hash"]
        response = client.post("/api/drafts", headers={**headers, "Idempotency-Key": data["key"]},
                               json={"skus": ["BRACKET"], "preview_hash": token})
        assert response.status_code == 200, response.text
        # Simulate a caller losing the successful response: do not forward its body.
        print(json.dumps({"pid": os.getpid(), "preview_hash": token}))
    else:
        response = client.post("/api/drafts", headers={**headers, "Idempotency-Key": data["key"]},
                               json={"skus": ["BRACKET"], "preview_hash": data["preview_hash"]})
        assert response.status_code == 200, response.text
        drafts = client.get("/api/drafts", headers=headers)
        assert drafts.status_code == 200, drafts.text
        print(json.dumps({"pid": os.getpid(), "retry": response.json(), "drafts": drafts.json()}))
'''


def test_committed_draft_retry_after_application_process_restart(tmp_path):
    database = tmp_path / "restart.sqlite3"
    common = {
        "database_url": f"sqlite:///{database.as_posix()}",
        "buyer": token_hex(24),
        "approver": token_hex(24),
        "erp": token_hex(24),
        "key": str(uuid4()),
    }

    def run_process(**arguments):
        completed = subprocess.run(
            [sys.executable, "-c", _APPLICATION],
            input=json.dumps({**common, **arguments}),
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
        )
        return json.loads(completed.stdout)

    created = run_process(phase="create")
    # Read independently, after the first process exits, to establish what committed.
    with sqlite3.connect(database) as connection:
        committed = connection.execute("SELECT draft_id FROM purchase_drafts").fetchall()
    assert len(committed) == 1

    recovered = run_process(phase="retry", preview_hash=created["preview_hash"])
    assert created["pid"] != os.getpid() and recovered["pid"] != os.getpid()
    assert recovered["retry"]["draft_id"] == committed[0][0]
    assert recovered["retry"]["total"] == "230.00"
    assert [draft["draft_id"] for draft in recovered["drafts"]] == [committed[0][0]]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM purchase_drafts").fetchone()[0] == 1
