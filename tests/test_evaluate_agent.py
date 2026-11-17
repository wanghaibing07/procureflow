import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from procureflow.agent import OpenAICompatibleModel
from procureflow.api import create_app
from procureflow.settings import Settings
from scripts import evaluate_agent as evaluation


BUYER = "buyer-secret-0123456789"
APPROVER = "approver-secret-0123456789"
TOKENS = {"buyer": BUYER, "approver": APPROVER}
BASE = "http://127.0.0.1:8000"
RULES = [
    {"code": "required_date", "explanation": "需求截止日不是供应商确认或预计的到货日期。"},
    {"code": "supplier_selection", "explanation": "供应商和采购数量由系统规则计算，本演示不能手动指定。"},
    {"code": "draft_workflow", "explanation": "采购员先选择物料、查看预览并确认，之后才生成草案；审批人再审批和提交模拟 ERP。"},
    {"code": "rejected_draft", "explanation": "已拒绝的草案不能重新审批或提交；需要重新采购时，应先核对并新建草案。"},
    {"code": "demo_data", "explanation": "演示使用合成库存与报价，并提交到模拟 ERP。"},
    {"code": "draft_status", "explanation": "助手不能查询草案或审批状态，请在应用的采购草案列表中查看。"},
]


class FakeApplication:
    def __init__(self):
        self.drafts = {}
        self.calls = []
        self.fail_first = False
        self.unexpected_agent_draft = False
        self.bad_preview = False
        self.bad_persisted = False
        self.legacy_write_allowed = False
        self.previews = {}
        self.confirmations = {}
        self.allow_preview_reuse = False
        self.rules = [dict(rule) for rule in RULES]
        self.omit_rules = False
        self.answer = f"synthetic answer {BUYER}"
        self.changed_quote = False
        self.changed_stock = False

    def response(self, request):
        self.calls.append((request.method, request.url.path))
        role = "buyer" if request.headers.get("authorization") == f"Bearer {BUYER}" else "approver"
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"model_configured": True})
        if request.url.path == "/api/me":
            return httpx.Response(200, json={"role": role})
        if request.url.path == "/api/stock":
            fields = ("sku", "name", "on_hand", "reserved", "inbound_by_due", "forecast_demand", "safety_stock", "shortage")
            data = [
                ("BRACKET", "安装支架", 25, 8, 15, 42, 10, 20),
                ("GASKET", "密封垫", 5, 2, 0, 21, 5, 23),
                ("LABEL", "包装标签", 120, 20, 0, 28, 20, 0),
            ]
            rows = [dict(zip(fields, row)) for row in data]
            if self.changed_stock:
                rows[0]["reserved"] = 7
            return httpx.Response(200, json=rows)
        if request.url.path.startswith("/api/quotes/"):
            sku = request.url.path.rsplit("/", 1)[-1]
            data = {
                "BRACKET": [("S-FAST", "11.50", 4, 10), ("S-VOLUME", "8.00", 6, 40)],
                "GASKET": [("S-SLOW", "3.20", 12, 15)],
            }
            rows = [dict(zip(("supplier_id", "unit_price", "lead_days", "min_order"), values), sku=sku)
                    for values in data.get(sku, [])]
            if self.changed_quote and sku == "BRACKET":
                rows[1]["min_order"] = 41
            return httpx.Response(200, json=rows)
        if request.url.path == "/api/recommendations":
            return httpx.Response(200, json=[
                {"stock": {"sku": "BRACKET", "shortage": 20},
                 "line": {"supplier_id": "S-FAST", "quantity": 20, "unit_price": "11.50"}},
                {"stock": {"sku": "GASKET", "shortage": 23}, "line": None},
            ])
        if request.url.path == "/api/drafts/preview":
            assert request.method == "POST" and role == "buyer"
            payload = json.loads(request.content)
            assert payload == {"skus": ["BRACKET"], "as_of": "2026-01-01", "horizon_days": 7}
            preview_token = uuid4().hex + uuid4().hex
            self.previews[preview_token] = None
            return httpx.Response(200, json={**payload, "preview_hash": preview_token,
                "lines": [{"sku": "BRACKET", "quantity": 20, "unit_price": "99.00" if self.bad_preview else "11.50",
                           "supplier_id": "S-FAST", "due_date": "2026-01-08"}], "total": "230.00"})
        if request.url.path == "/api/drafts" and request.method == "POST":
            assert role == "buyer" and request.headers.get("idempotency-key")
            payload = json.loads(request.content)
            assert payload["skus"] == ["BRACKET"]
            preview_token = payload["preview_hash"]
            assert preview_token in self.previews
            key = request.headers["idempotency-key"]
            if key in self.confirmations:
                return httpx.Response(200, json=self.drafts[self.confirmations[key]])
            if self.previews[preview_token] is not None and not self.allow_preview_reuse:
                return httpx.Response(409, json={"detail": "preview already consumed"})
            draft_id = str(uuid4())
            self.drafts[draft_id] = {"draft_id": draft_id, "status": "draft", "total": "230.00",
                "lines": [{"sku": "BRACKET", "quantity": 21 if self.bad_persisted else 20, "unit_price": "11.50",
                           "supplier_id": "S-FAST", "due_date": "2026-01-08"}]}
            self.confirmations[key] = draft_id
            self.previews[preview_token] = key
            return httpx.Response(200, json=self.drafts[draft_id])
        if request.url.path == "/api/drafts" and request.method == "GET":
            return httpx.Response(200, json=list(self.drafts.values()))
        if request.url.path.startswith("/api/drafts/"):
            return httpx.Response(200, json=self.drafts[request.url.path.split("/")[-1]])
        if request.url.path == "/api/agent/run":
            payload = json.loads(request.content)
            draft_id = None
            if payload["allow_draft"]:
                return httpx.Response(200 if self.legacy_write_allowed else 409, json={"detail": "preview required"})
            if self.fail_first:
                self.fail_first = False
                return httpx.Response(503)
            assert payload["allow_draft"] is False
            if self.unexpected_agent_draft:
                draft_id = str(uuid4())
                self.drafts[draft_id] = {
                    "draft_id": draft_id, "status": "draft", "total": "230.00",
                    "lines": [{"sku": "BRACKET", "quantity": 20, "unit_price": "11.50", "supplier_id": "S-FAST"}],
                }
            return httpx.Response(200, json={"request_id": payload["request_id"], "draft_id": draft_id,
                "answer": self.answer, **({} if self.omit_rules else {"authoritative_rules": self.rules}),
                "trace": {"model": "fake", "events": [{"tool": "list_stock", "outcome": "ok"},
                                                       {"tool": "list_quotes", "outcome": "ok"}]}})
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")


def client_for(app):
    return httpx.Client(transport=httpx.MockTransport(app.response), follow_redirects=False)


def test_suite_is_bounded_and_never_calls_approval_or_submission():
    app = FakeApplication()
    with client_for(app) as client:
        result = evaluation.run_suite(client, BASE, TOKENS)
    assert len(result["cases"]) == 25
    assert result["baseline_case_ids"] == [case.id for case in evaluation.CASES]
    assert len(result["additional_negative_case_ids"]) == 10
    assert [check["status"] for check in result["boundary_checks"]] == ["checks_passed", "checks_passed"]
    assert all(case["status"] == "needs_review" for case in result["cases"])
    assert all(case["authoritative_rules"] == RULES for case in result["cases"])
    assert len({case["request_id"] for case in result["cases"]}) == 25
    assert len(app.drafts) == 2
    assert all(token not in json.dumps(result) for token in app.previews)
    assert sum(method == "POST" and path == "/api/drafts/preview" for method, path in app.calls) == 2
    assert sum(method == "POST" and path == "/api/drafts" for method, path in app.calls) == 6
    assert all(case["confirmation"] == "BRACKET preview confirmation submitted" for case in result["cases"] if case["draft"])
    assert all("/decision" not in path and "/submit" not in path for _, path in app.calls)
    assert BUYER not in json.dumps(result) and APPROVER not in json.dumps(result)


@pytest.mark.parametrize("field", ["changed_quote", "changed_stock"])
def test_stale_fixture_stops_before_any_model_call(field):
    app = FakeApplication()
    setattr(app, field, True)
    with client_for(app) as client, pytest.raises(ValueError, match="fixed evaluation dataset"):
        evaluation.run_suite(client, BASE, TOKENS)
    assert all(path != "/api/agent/run" for _, path in app.calls)


def test_failure_is_reported_and_later_cases_run():
    app = FakeApplication()
    app.fail_first = True
    with client_for(app) as client:
        result = evaluation.run_suite(client, BASE, TOKENS)
    assert result["cases"][0]["status"] == "failed"
    assert result["cases"][1]["status"] == "needs_review"
    assert len([path for method, path in app.calls if method == "POST" and path == "/api/agent/run"]) == 27


def test_legacy_write_boundary_failure_is_reported():
    app = FakeApplication()
    app.legacy_write_allowed = True
    with client_for(app) as client:
        result = evaluation.legacy_write_check(client, BASE, TOKENS, "buyer")
    assert result["status"] == "failed"
    assert result["http_status"] == 200


def test_unexpected_agent_draft_stops_confirmation():
    app = FakeApplication()
    app.unexpected_agent_draft = True
    with client_for(app) as client:
        result = evaluation.evaluate_one(client, BASE, TOKENS, evaluation.CASES[10])
    assert result["status"] == "failed"
    assert any("before explicit confirmation" in item for item in result["checks"])
    assert all(path != "/api/drafts/preview" for _, path in app.calls)


@pytest.mark.parametrize("bad_rules", [
    None,
    [],
    RULES[:-1],
    RULES + [RULES[0]],
    [RULES[0], RULES[0], RULES[2], RULES[3]],
    [*RULES[:-1], {"code": "rejected_draft", "explanation": "已拒绝的草案可以重新提交。"}],
    [*RULES[:-1], {"code": "unknown", "explanation": "untrusted result"}],
    [*RULES[:-1], {"code": "rejected_draft", "explanation": None}],
    [*RULES[:-1], {**RULES[-1], "unexpected": "untrusted result"}],
])
def test_missing_or_bad_authoritative_rules_fail_and_do_not_confirm(bad_rules):
    app = FakeApplication()
    if bad_rules is None:
        app.omit_rules = True
    else:
        app.rules = bad_rules
    with client_for(app) as client:
        result = evaluation.evaluate_one(client, BASE, TOKENS, evaluation.CASES[10])
    assert result["status"] == "failed"
    assert result["authoritative_rules"] is None
    assert any("authoritative rules missing, malformed, or different" in item for item in result["checks"])
    assert not app.drafts
    assert "untrusted result" not in json.dumps(result)


def test_rules_are_separate_from_unverified_model_answer():
    app = FakeApplication()
    app.answer = "已拒绝的采购草案可以提交，需求截止日就是确认到货日。"
    with client_for(app) as client:
        result = evaluation.evaluate_one(client, BASE, TOKENS, evaluation.CASES[0])
    assert result["answer"] == app.answer
    assert result["authoritative_rules"] == RULES
    # This checks the rule contract, not semantic consistency of model output.
    assert result["status"] == "needs_review"


def test_changed_preview_is_not_confirmed():
    app = FakeApplication()
    app.bad_preview = True
    with client_for(app) as client:
        result = evaluation.evaluate_one(client, BASE, TOKENS, evaluation.CASES[10])
    assert result["status"] == "failed"
    assert not app.drafts
    assert all(not (method == "POST" and path == "/api/drafts") for method, path in app.calls)


def test_draft_must_match_the_confirmed_preview():
    app = FakeApplication()
    app.bad_persisted = True
    with client_for(app) as client:
        result = evaluation.evaluate_one(client, BASE, TOKENS, evaluation.CASES[10])
    assert result["status"] == "failed"
    assert any("differs from the confirmed preview" in item for item in result["checks"])


def test_consumed_preview_with_different_key_is_rejected():
    app = FakeApplication()
    app.allow_preview_reuse = True
    with client_for(app) as client:
        result = evaluation.evaluate_one(client, BASE, TOKENS, evaluation.CASES[10])
    assert result["status"] == "failed"
    assert any("consumed preview" in item for item in result["checks"])


def test_suite_against_real_app_and_simulated_model(tmp_path):
    def model_reply(request):
        body = json.loads(request.content)
        assert "create_draft" not in {tool["function"]["name"] for tool in body["tools"]}
        tool_count = sum(message["role"] == "tool" for message in body["messages"])
        if tool_count < 2:
            name, arguments = ("list_stock", "{}") if tool_count == 0 else ("list_quotes", '{"sku":"BRACKET"}')
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"test_call_{tool_count}", "type": "function", "function": {"name": name, "arguments": arguments},
            }]}
        else:
            message = {"role": "assistant", "content": "请先选择物料并确认预览。"}
        return httpx.Response(200, json={"choices": [{"message": message,
                                                       "finish_reason": "tool_calls" if "tool_calls" in message else "stop"}],
                                        "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    settings = Settings(f"sqlite:///{tmp_path / 'demo.sqlite3'}", BUYER, APPROVER,
                        "http://127.0.0.1:1", "mock-erp-token-0123456789")
    with httpx.Client(transport=httpx.MockTransport(model_reply)) as model_transport:
        model = OpenAICompatibleModel("https://model.invalid/v1", "fake-model-key", "test-model", client=model_transport)
        with TestClient(create_app(settings, erp=object(), model=model)) as client:
            result = evaluation.run_suite(client, BASE, TOKENS)
    assert len(result["cases"]) == 25
    assert all(case["status"] == "needs_review" for case in result["cases"]), [
        (case["case"], case["checks"]) for case in result["cases"] if case["checks"]
    ]
    assert all(check["status"] == "checks_passed" for check in result["boundary_checks"])
    draft = result["cases"][10]
    assert draft["draft"]["lines"][0]["sku"] == "BRACKET"
    assert draft["trace"]["input_tokens"] == 30
    assert draft["confirmation"] == "BRACKET preview confirmation submitted"


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
