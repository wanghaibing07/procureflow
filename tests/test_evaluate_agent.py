import json
from uuid import uuid4

import httpx
import pytest

from scripts import evaluate_agent as evaluation


BUYER = "buyer-secret-0123456789"
APPROVER = "approver-secret-0123456789"
TOKENS = {"buyer": BUYER, "approver": APPROVER}
BASE = "http://127.0.0.1:8000"


class FakeApplication:
    def __init__(self):
        self.drafts = {}
        self.calls = []
        self.fail_first = False

    def response(self, request):
        self.calls.append((request.method, request.url.path))
        role = "buyer" if request.headers.get("authorization") == f"Bearer {BUYER}" else "approver"
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"model_configured": True})
        if request.url.path == "/api/me":
            return httpx.Response(200, json={"role": role})
        if request.url.path == "/api/recommendations":
            return httpx.Response(200, json=[
                {"stock": {"sku": "BRACKET", "shortage": 20},
                 "line": {"supplier_id": "S-FAST", "quantity": 20, "unit_price": "11.50"}},
                {"stock": {"sku": "GASKET", "shortage": 23}, "line": None},
            ])
        if request.url.path == "/api/drafts":
            return httpx.Response(200, json=list(self.drafts.values()))
        if request.url.path.startswith("/api/drafts/"):
            return httpx.Response(200, json=self.drafts[request.url.path.split("/")[-1]])
        if request.url.path == "/api/agent/run":
            if self.fail_first:
                self.fail_first = False
                return httpx.Response(503)
            payload = json.loads(request.content)
            draft_id = None
            if payload["allow_draft"] and role == "buyer":
                draft_id = str(uuid4())
                self.drafts[draft_id] = {
                    "draft_id": draft_id, "status": "draft", "total": "230.00",
                    "lines": [{"sku": "BRACKET", "quantity": 20, "unit_price": "11.50", "supplier_id": "S-FAST"}],
                }
            return httpx.Response(200, json={"request_id": payload["request_id"], "draft_id": draft_id,
                "answer": f"synthetic answer {BUYER}",
                "trace": {"model": "fake", "events": [{"tool": "list_stock", "outcome": "ok"},
                                                       {"tool": "list_quotes", "outcome": "ok"}]}})
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")


def client_for(app):
    return httpx.Client(transport=httpx.MockTransport(app.response), follow_redirects=False)


def test_suite_is_bounded_and_never_calls_approval_or_submission():
    app = FakeApplication()
    with client_for(app) as client:
        result = evaluation.run_suite(client, BASE, TOKENS)
    assert len(result["cases"]) == 15
    assert all(case["status"] == "needs_review" for case in result["cases"])
    assert len({case["request_id"] for case in result["cases"]}) == 15
    assert len(app.drafts) == 2
    assert all("/decision" not in path and "/submit" not in path for _, path in app.calls)
    assert BUYER not in json.dumps(result) and APPROVER not in json.dumps(result)


def test_failure_is_reported_and_later_cases_run():
    app = FakeApplication()
    app.fail_first = True
    with client_for(app) as client:
        result = evaluation.run_suite(client, BASE, TOKENS)
    assert result["cases"][0]["status"] == "failed"
    assert result["cases"][1]["status"] == "needs_review"
    assert len([path for method, path in app.calls if method == "POST" and path == "/api/agent/run"]) == 15


def test_missing_required_stock_tool_is_failed_not_a_pass():
    app = FakeApplication()
    def no_tool(request):
        response = app.response(request)
        if request.url.path == "/api/agent/run":
            data = response.json()
            data["trace"]["events"] = []
            return httpx.Response(200, json=data)
        return response
    with httpx.Client(transport=httpx.MockTransport(no_tool)) as client:
        result = evaluation.evaluate_one(client, BASE, TOKENS, evaluation.CASES[0])
    assert result["status"] == "failed"
    assert "stock tool" in result["checks"][0]


@pytest.mark.parametrize("url", ["https://example.com:443", "http://localhost:8000", "http://8.8.8.8:80",
                                "http://127.0.0.1:8000/path", "http://user@127.0.0.1:8000"])
def test_rejects_non_loopback_or_ambiguous_targets(url):
    with pytest.raises(ValueError):
        evaluation.local_base(url)


def test_requires_live_flag_before_network_or_output(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation.httpx, "Client", lambda **kwargs: pytest.fail("network was requested"))
    output = tmp_path / "result.json"
    with pytest.raises(SystemExit) as error:
        evaluation.main(["--base-url", BASE, "--output", str(output)])
    assert error.value.code == 2
    assert not output.exists()


def test_existing_output_is_not_overwritten_or_networked(monkeypatch, tmp_path):
    monkeypatch.setenv("PROCUREFLOW_BUYER_TOKEN", BUYER)
    monkeypatch.setenv("PROCUREFLOW_APPROVER_TOKEN", APPROVER)
    monkeypatch.setattr(evaluation.httpx, "Client", lambda **kwargs: pytest.fail("network was requested"))
    output = tmp_path / "result.json"
    output.write_text("keep", encoding="utf-8")
    assert evaluation.main(["--base-url", BASE, "--output", str(output), "--run-live"]) == 1
    assert output.read_text(encoding="utf-8") == "keep"
