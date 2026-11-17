import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from procureflow.agent import MAX_STEPS, AgentError, AgentRequest, AgentRun, AgentService, OpenAICompatibleModel
from procureflow.catalog import DEMO_DATE, InventoryItem, Supplier, SupplierQuote, seed_catalog
from procureflow.db import Database
from procureflow.workflow import Actor, DraftStatus, Role, Workflow, WorkflowError


BUYER = Actor("test-buyer", Role.BUYER)
APPROVER = Actor("test-approver", Role.APPROVER)


class NeverSubmit:
    def submit(self, draft_id, lines):
        pytest.fail("The model must not be able to submit drafts to ERP.")


@pytest.fixture
def workflow(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'agent.sqlite3').as_posix()}")
    db.create_schema()
    with db.transaction() as session:
        seed_catalog(session)
    try:
        yield Workflow(db, NeverSubmit())
    finally:
        db.close()


@pytest.fixture
def mysql_agent_workflow():
    url = os.getenv("PROCUREFLOW_TEST_DB_URL")
    if not url:
        pytest.skip("PROCUREFLOW_TEST_DB_URL is not configured")
    parsed = make_url(url)
    if parsed.get_backend_name() != "mysql" or parsed.database != "procureflow_test":
        pytest.fail("PROCUREFLOW_TEST_DB_URL must target a dedicated MySQL database named procureflow_test.")
    db = Database(url)
    try:
        db.create_schema()
        with db.transaction() as session:
            seed_catalog(session)
        yield Workflow(db, NeverSubmit())
    finally:
        db.close()


def call(name, arguments, *, call_id="call_123"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}],
    }


def answer(content="基于库存工具完成调查。"):
    return {"role": "assistant", "content": content}


class StubModel:
    def __init__(self, workflow, messages):
        self.messages = iter(messages)
        self.requests = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.reply), follow_redirects=True)
        self.model = OpenAICompatibleModel("https://example.invalid/v1", "fake-api-key", "test-model", client=self.client)
        self.service = AgentService(workflow.database, workflow, self.model)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.model.close()
        self.client.close()

    def reply(self, request):
        assert request.url == "https://example.invalid/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fake-api-key"
        payload = json.loads(request.content)
        self.requests.append(payload)
        assert payload["model"] == "test-model"
        assert payload["parallel_tool_calls"] is False
        message = next(self.messages)
        if isinstance(message, httpx.Response):
            return message
        if isinstance(message, Exception):
            raise message
        return httpx.Response(200, json={
            "choices": [{"message": message, "finish_reason": "tool_calls" if "tool_calls" in message else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        })

    def run(self, actor=BUYER, *, allow_draft=False, request_id=None, prompt="帮我看看缺货物料", as_of=DEMO_DATE, horizon_days=7):
        return self.service.run(actor, prompt, as_of, horizon_days, allow_draft, request_id or uuid4())


def tool_output(script, request_index):
    message = script.requests[request_index]["messages"][-1]
    assert message["role"] == "tool"
    return json.loads(message["content"])


def test_stock_and_quotes_flow_keeps_chat_protocol_and_persists_safe_trace(workflow):
    prompt = "sensitive demo prompt marker"
    reply = "sensitive model answer marker"
    with StubModel(workflow, [call("list_stock", "{}"), call("list_quotes", '{"sku":"BRACKET"}', call_id="quote"), answer(reply)]) as script:
        result = script.run(prompt=prompt)
        assert result.answer == reply
        assert result.draft_id is None
        assert result.trace.input_tokens == 30
        assert result.trace.output_tokens == 6
        assert result.trace.model_calls == 3
        assert [event.tool for event in result.trace.events] == ["list_stock", "list_quotes"]
        assert [event.result_count for event in result.trace.events] == [3, 2]
        assert script.requests[1]["messages"][-1]["tool_call_id"] == "call_123"
        item = next(item for item in tool_output(script, 1)["items"] if item["sku"] == "BRACKET")
        assert item["shortage"] == 20
        assert item["net_available"] == 32 and item["total_required"] == 52
        assert sorted(quote["supplier_id"] for quote in tool_output(script, 2)["items"]) == ["S-FAST", "S-VOLUME"]
        assert "create_draft" not in {entry["function"]["name"] for entry in script.requests[0]["tools"]}
    with workflow.database.transaction() as session:
        run = session.scalars(select(AgentRun)).one()
        assert run.request_id == str(result.request_id)
        assert run.status == "completed"
        assert run.trace == result.trace.model_dump(mode="json")
        trace = json.dumps(run.trace)
        assert prompt not in trace and reply not in trace and "fake-api-key" not in trace


def test_application_rules_remain_distinct_from_untrusted_model_answer(workflow):
    # The raw answer is deliberately wrong. The API must preserve provenance:
    # a client can show the system's rules separately instead of treating the
    # model's text as an authoritative workflow or delivery commitment.
    wrong = "2026-01-08 是确认到货日；先建单再预览，自己挑供应商，并重新提交已拒绝单。"
    with StubModel(workflow, [answer(wrong)]) as script:
        result = script.run(prompt="请解释采购流程")
        assert result.answer == wrong
        rules = {rule.code: rule.explanation for rule in result.authoritative_rules}
        assert "不是" in rules["required_date"] and "到货日期" in rules["required_date"]
        assert "不能手动指定" in rules["supplier_selection"]
        assert rules["draft_workflow"].index("预览") < rules["draft_workflow"].index("生成草案")
        assert "不能重新审批或提交" in rules["rejected_draft"]
        assert "合成" in rules["demo_data"] and "模拟 ERP" in rules["demo_data"]
        assert "不能查询草案" in rules["draft_status"]
        assert result.draft_id is None
        assert not workflow.list_drafts(BUYER)


def test_stock_tool_does_not_present_shortage_as_supplier_availability(workflow):
    # GASKET has a shortage but no supplier that meets the planning window.
    # The stock tool must not imply the existence of a purchase proposal.
    with StubModel(workflow, [call("list_stock", "{}"), answer()]) as script:
        result = script.run(prompt="密封垫的库存风险是多少？")
        output = tool_output(script, 1)
        gasket = next(item for item in output["items"] if item["sku"] == "GASKET")
        assert gasket["shortage"] == 23
        assert "supplier_id" not in gasket
        assert "does not establish" in output["availability_semantics"]
        assert "list_recommendations" in output["availability_semantics"]
        system_prompt = script.requests[0]["messages"][0]["content"]
        assert "a buyer cannot approve or submit" in system_prompt
        assert "Only an approver can approve a draft" in system_prompt
        assert result.draft_id is None


@pytest.mark.parametrize(
    ("sku", "present", "quote_count"),
    [("BRACKET", True, 2), ("LABEL", True, 0), ("MISSING", False, 0)],
)
def test_quote_tool_distinguishes_catalog_membership_from_quotes(workflow, sku, present, quote_count):
    with StubModel(workflow, [call("list_quotes", json.dumps({"sku": sku})), answer()]) as script:
        result = script.run()
        output = tool_output(script, 1)
        assert output["sku"] == sku
        assert output["catalog_sku_found"] is present
        assert len(output["items"]) == quote_count
        assert not output["truncated"]
        assert result.trace.events[0].result_count == quote_count
        assert result.trace.events[0].outcome == "ok"
        assert result.draft_id is None


def test_quote_comparison_uses_shortage_minimum_order_and_reference_window(workflow):
    with StubModel(workflow, [call("list_quotes", '{"sku":"BRACKET"}'), answer()]) as script:
        script.run(prompt="支架缺口采购应如何比较报价？")
        output = tool_output(script, 1)
        quotes = {quote["supplier_id"]: quote for quote in output["items"]}
        assert quotes["S-FAST"]["shortage_comparison"] == {
            "shortage": 20, "order_quantity": 20, "extra_units": 0, "total_amount": "230.00"
        }
        assert quotes["S-VOLUME"]["shortage_comparison"] == {
            "shortage": 20, "order_quantity": 40, "extra_units": 20, "total_amount": "320.00"
        }
        assert all(quote["within_planning_window"] for quote in quotes.values())
        assert "not a confirmed delivery promise" in output["comparison_semantics"]

    with StubModel(workflow, [call("list_quotes", '{"sku":"GASKET"}'), answer()]) as script:
        script.run(prompt="密封垫报价是否符合计划窗口？")
        quote = tool_output(script, 1)["items"][0]
        assert quote["within_planning_window"] is False
        assert quote["shortage_comparison"] == {
            "shortage": 23, "order_quantity": 23, "extra_units": 0, "total_amount": "73.60"
        }


def test_quote_tool_compares_requested_quantity_independently_of_shortage(workflow):
    # Simulate the tool call a model may issue for a real user question; this
    # tests the tool contract, not whether a real model chooses that call.
    with StubModel(workflow, [
        call("list_quotes", '{"sku":"BRACKET","comparison_quantity":30}'), answer()
    ]) as script:
        result = script.run(prompt="如果需要采购30件安装支架，请对比供应商的起订量和总金额。")
        spec = next(tool["function"] for tool in script.requests[0]["tools"] if tool["function"]["name"] == "list_quotes")
        schema = spec["parameters"]
        assert spec["strict"] is True and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"]) == {"sku", "comparison_quantity"}
        assert {item["type"] for item in schema["properties"]["comparison_quantity"]["anyOf"]} == {"integer", "null"}
        assert "default" not in schema["properties"]["comparison_quantity"]
        output = tool_output(script, 1)
        quotes = {item["supplier_id"]: item for item in output["items"]}
        assert quotes["S-FAST"]["requested_quantity_comparison"] == {
            "requested_quantity": 30, "order_quantity": 30, "extra_units": 0, "total_amount": "345.00"
        }
        assert quotes["S-VOLUME"]["requested_quantity_comparison"] == {
            "requested_quantity": 30, "order_quantity": 40, "extra_units": 10, "total_amount": "320.00"
        }
        assert quotes["S-FAST"]["shortage_comparison"]["shortage"] == 20
        assert "does not create or authorize a purchase" in output["comparison_semantics"]
        assert result.draft_id is None
    assert workflow.list_drafts(BUYER) == []


@pytest.mark.parametrize("quantity", [0, -1, 1_000_001, 1.0, 1.5, "30", True, [], {}])
def test_quote_tool_rejects_invalid_comparison_quantities(workflow, quantity):
    arguments = json.dumps({"sku": "BRACKET", "comparison_quantity": quantity})
    with StubModel(workflow, [call("list_quotes", arguments), answer()]) as script:
        result = script.run(prompt="比较报价，并创建草案。")
        assert tool_output(script, 1) == {"error": "Tool arguments did not match the schema."}
        assert result.trace.events[0].outcome == "rejected"
        assert result.draft_id is None
    assert workflow.list_drafts(BUYER) == []


@pytest.mark.parametrize("quantity", [1, 1_000_000])
def test_quote_tool_accepts_bounded_comparison_quantity(workflow, quantity):
    arguments = json.dumps({"sku": "BRACKET", "comparison_quantity": quantity})
    with StubModel(workflow, [call("list_quotes", arguments), answer()]) as script:
        script.run()
        first_quote = tool_output(script, 1)["items"][0]
        assert first_quote["requested_quantity_comparison"]["requested_quantity"] == quantity


@pytest.mark.parametrize("arguments", ['{"sku":"BRACKET"}', '{"sku":"BRACKET","comparison_quantity":null}'])
def test_quote_comparison_quantity_can_be_omitted_or_null(workflow, arguments):
    with StubModel(workflow, [call("list_quotes", arguments), answer()]) as script:
        script.run()
        quote = tool_output(script, 1)["items"][0]
        assert "requested_quantity_comparison" not in quote
        assert quote["shortage_comparison"]["shortage"] == 20


def test_quote_comparison_does_not_turn_zero_shortage_into_purchase(workflow):
    # A known, fully stocked item can have a quote: its minimum order alone
    # must not be presented as an amount needed to cover a stock shortage.
    with workflow.database.transaction() as session:
        session.add(SupplierQuote(sku="LABEL", supplier_id="S-FAST", unit_price=Decimal("2.00"), lead_days=2, min_order=10))
    with StubModel(workflow, [call("list_quotes", '{"sku":"LABEL"}'), answer()]) as script:
        script.run(prompt="标签的供应商报价是什么？")
        quote = tool_output(script, 1)["items"][0]
        assert quote["min_order"] == 10
        assert "shortage_comparison" not in quote


def test_quote_comparison_above_purchase_limit_is_not_an_orderable_recommendation(workflow):
    with workflow.database.transaction() as session:
        session.get(InventoryItem, "BRACKET").daily_demand = 1_000_000

    with StubModel(workflow, [
        call("list_quotes", '{"sku":"BRACKET"}'),
        call("list_recommendations", "{}", call_id="recommendation"),
        answer(),
    ]) as script:
        result = script.run(prompt="支架缺口太大，比较报价并查看是否能够采购。")
        quote_output = tool_output(script, 1)
        quote = next(item for item in quote_output["items"] if item["supplier_id"] == "S-FAST")
        comparison = quote["shortage_comparison"]
        assert comparison["shortage"] == 6_999_978
        assert comparison["order_quantity"] == 6_999_978
        assert comparison["total_amount"] == "80499747.00"
        assert "does not itself constitute a purchase recommendation" in quote_output["comparison_semantics"]
        recommendation = next(row for row in tool_output(script, 2)["items"] if row["stock"]["sku"] == "BRACKET")
        assert recommendation["reason"] == "quantity_exceeds_limit"
        assert recommendation["line"] is None
        assert result.draft_id is None

    with pytest.raises(WorkflowError, match="current orderable recommendation") as error:
        workflow.preview_recommended_draft(BUYER, ("BRACKET",), DEMO_DATE, 7)
    assert error.value.status_code == 400
    assert workflow.list_drafts(BUYER) == []


def test_mysql_quote_tool_distinguishes_catalog_membership_from_quotes():
    """Use uniquely named rows; never change the existing demo catalog."""
    url = os.getenv("PROCUREFLOW_TEST_DB_URL")
    if not url:
        pytest.skip("PROCUREFLOW_TEST_DB_URL is not configured")
    parsed = make_url(url)
    if parsed.get_backend_name() != "mysql" or parsed.database != "procureflow_test":
        pytest.fail("PROCUREFLOW_TEST_DB_URL must target a dedicated MySQL database named procureflow_test.")

    db = Database(url)
    suffix = uuid4().hex.upper()
    quoted, unquoted, unknown, supplier_id = (f"Q{suffix}", f"N{suffix}", f"U{suffix}", f"S{suffix}")
    buyer = Actor(f"mysql-quote-{suffix}", Role.BUYER)
    requests = [uuid4() for _ in range(3)]
    created = False
    try:
        db.create_schema()
        with db.transaction() as session:
            assert all(session.get(InventoryItem, sku) is None for sku in (quoted, unquoted, unknown))
            assert session.get(Supplier, supplier_id) is None
            session.add_all([
                InventoryItem(sku=quoted, name="quoted test item", on_hand=1, reserved=0, daily_demand=0, safety_stock=0),
                InventoryItem(sku=unquoted, name="unquoted test item", on_hand=1, reserved=0, daily_demand=0, safety_stock=0),
                Supplier(supplier_id=supplier_id, name="test supplier"),
            ])
            session.flush()
            session.add(SupplierQuote(sku=quoted, supplier_id=supplier_id, unit_price=Decimal("1.00"), lead_days=1, min_order=1))
        created = True
        workflow = Workflow(db, NeverSubmit())
        for sku, present, quote_count, request_id in zip(
            (quoted, unquoted, unknown), (True, True, False), (1, 0, 0), requests
        ):
            with StubModel(workflow, [call("list_quotes", json.dumps({"sku": sku})), answer()]) as script:
                result = script.run(buyer, request_id=request_id)
                output = tool_output(script, 1)
                assert output["sku"] == sku
                assert output["catalog_sku_found"] is present
                assert len(output["items"]) == quote_count
                assert not output["truncated"]
                assert result.trace.events[0].result_count == quote_count
                assert result.draft_id is None
    finally:
        try:
            if created:
                request_keys = [hashlib.sha256(json.dumps([buyer.actor_id, str(request_id)]).encode()).hexdigest()
                                for request_id in requests]
                with db.transaction() as session:
                    session.execute(delete(AgentRun).where(AgentRun.actor_id == buyer.actor_id))
                    session.execute(delete(AgentRequest).where(AgentRequest.request_key.in_(request_keys)))
                    session.execute(delete(SupplierQuote).where(SupplierQuote.sku == quoted, SupplierQuote.supplier_id == supplier_id))
                    session.execute(delete(InventoryItem).where(InventoryItem.sku.in_((quoted, unquoted))))
                    session.execute(delete(Supplier).where(Supplier.supplier_id == supplier_id))
        finally:
            db.close()


@pytest.mark.parametrize("actor", [BUYER, APPROVER])
def test_unoffered_write_is_also_blocked_by_executor(workflow, actor):
    with StubModel(workflow, [call("create_draft", '{"skus":["BRACKET"]}'), answer()]) as script:
        result = script.run(actor)
        assert {item["function"]["name"] for item in script.requests[0]["tools"]} == {"list_stock", "list_quotes", "list_recommendations"}
        assert "error" in tool_output(script, 1)
        assert result.trace.events[0].outcome == "rejected"
        assert result.draft_id is None
    assert workflow.list_drafts(APPROVER) == []


@pytest.mark.parametrize("actor", [BUYER, APPROVER])
def test_legacy_write_opt_in_fails_closed_without_calling_model(workflow, actor):
    with StubModel(workflow, []) as script:
        with pytest.raises(AgentError) as error:
            script.run(actor, allow_draft=True)
        assert error.value.status_code == 409
        assert not script.requests
    assert workflow.list_drafts(APPROVER) == []


def test_legacy_committed_draft_recovery_requires_exact_saved_binding(workflow):
    request_id = uuid4()
    original = workflow.create_recommended_draft(BUYER, f"agent:{request_id}", ("BRACKET",), DEMO_DATE, 7)
    prompt = "retrieve old draft"
    request_key = hashlib.sha256(json.dumps([BUYER.actor_id, str(request_id)]).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(
        [str(request_id), BUYER.role.value, prompt, DEMO_DATE.isoformat(), 7, True],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    with StubModel(workflow, []) as script:
        # A draft without a saved request fingerprint cannot be silently claimed.
        with pytest.raises(AgentError) as unknown:
            script.run(prompt=prompt, allow_draft=True, request_id=request_id)
        assert unknown.value.status_code == 409
        with workflow.database.transaction() as session:
            session.add(AgentRequest(request_key=request_key, fingerprint=fingerprint, state="running", owner=str(uuid4())))
        for changes in ({"prompt": "other"}, {"horizon_days": 8}):
            with pytest.raises(AgentError) as error:
                script.run(prompt=changes.get("prompt", prompt), horizon_days=changes.get("horizon_days", 7),
                           allow_draft=True, request_id=request_id)
            assert error.value.status_code == 409
        recovered = script.run(prompt=prompt, allow_draft=True, request_id=request_id)
        assert recovered.draft_id == original.draft_id and recovered.trace.model_calls == 0
        assert not script.requests
        with workflow.database.transaction() as session:
            session.get(AgentRequest, request_key).state = "legacy"
        with pytest.raises(AgentError) as legacy:
            script.run(prompt=prompt, allow_draft=True, request_id=request_id)
        assert legacy.value.status_code == 409
    assert len(workflow.list_drafts(BUYER)) == 1


def test_read_request_replay_is_bound_and_never_reinvokes_model(workflow):
    request_id = uuid4()
    with StubModel(workflow, [answer("No draft")]) as script:
        first = script.run(request_id=request_id)
        retry = script.run(request_id=request_id)
        assert first.draft_id is None and retry.draft_id is None
        assert retry.trace.model_calls == 0
        assert len(script.requests) == 1
        for changes in ({"prompt": "different request"}, {"horizon_days": 8}):
            with pytest.raises(AgentError) as error:
                script.run(request_id=request_id, **changes)
            assert error.value.status_code == 409


def test_concurrent_read_request_is_not_invoked_twice(workflow):
    request_id, entered, resume = uuid4(), Event(), Event()
    with StubModel(workflow, [answer("No draft")]) as script:
        complete = script.model.complete

        def blocked_complete(messages, tools):
            entered.set()
            assert resume.wait(10)
            return complete(messages, tools)

        script.model.complete = blocked_complete
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(script.run, request_id=request_id)
            assert entered.wait(10)
            try:
                with pytest.raises(AgentError) as error:
                    script.run(request_id=request_id)
                assert error.value.status_code == 409
            finally:
                resume.set()
            assert running.result(timeout=10).draft_id is None
        assert len(script.requests) == 1


def test_failed_read_can_retry_without_writing_a_draft(workflow):
    request_id = uuid4()
    with StubModel(workflow, [httpx.ReadTimeout("upstream"), answer("Retry")]) as script:
        with pytest.raises(AgentError, match="in time"):
            script.run(request_id=request_id)
        assert script.run(request_id=request_id).answer == "Retry"
        assert len(script.requests) == 2
    assert workflow.list_drafts(BUYER) == []
























@pytest.mark.parametrize(("name", "arguments"), [
    ("create_draft", '{"skus":["BRACKET"],"unit_price": 0.01}'),
    ("create_draft", '{"skus":["BRACKET","GASKET"]}'),
    ("list_quotes", '{"sku":123}'),
    ("list_quotes", "not JSON"),
    ("submit_draft", '{"draft_id":"some-draft"}'),
])
def test_untrusted_tool_calls_cannot_write_outside_contract(workflow, name, arguments):
    with StubModel(workflow, [call(name, arguments), answer()]) as script:
        result = script.run()
        assert result.draft_id is None
        assert "error" in tool_output(script, 1)
        assert result.trace.events[0].outcome == "rejected"
    assert workflow.list_drafts(APPROVER) == []


def test_tool_budget_rejects_last_call_before_side_effect(workflow):
    responses = [call("list_stock", "{}", call_id=f"read-{step}") for step in range(MAX_STEPS - 1)]
    responses.append(call("create_draft", '{"skus":["BRACKET"]}'))
    with StubModel(workflow, responses) as script, pytest.raises(AgentError, match="tool-call budget"):
        script.run()
    with workflow.database.transaction() as session:
        run = session.scalars(select(AgentRun)).one()
        assert run.status == "failed"
        assert run.trace["model_calls"] == MAX_STEPS
        assert len(run.trace["events"]) == MAX_STEPS - 1
    assert workflow.list_drafts(APPROVER) == []


def test_provider_can_return_multiple_tool_calls_even_when_parallel_disabled(workflow):
    first = call("list_stock", "{}", call_id="stock")["tool_calls"][0]
    second = call("list_quotes", '{"sku":"BRACKET"}', call_id="quote")["tool_calls"][0]
    with StubModel(workflow, [{"role": "assistant", "content": None, "tool_calls": [first, second]}, answer()]) as script:
        result = script.run()
        assert [event.tool for event in result.trace.events] == ["list_stock", "list_quotes"]
        messages = script.requests[1]["messages"]
        assert [message.get("tool_call_id") for message in messages[-2:]] == ["stock", "quote"]
        assert len(messages[-3]["tool_calls"]) == 2


def test_recommendation_tool_exposes_computed_order_and_unavailable_supplier(workflow):
    with StubModel(workflow, [call("list_recommendations", "{}"), answer()]) as script:
        result = script.run()
        rows = {row["stock"]["sku"]: row for row in tool_output(script, 1)["items"]}
        assert rows["BRACKET"]["line"]["quantity"] == 20
        assert rows["BRACKET"]["line"]["supplier_id"] == "S-FAST"
        assert rows["BRACKET"]["total_amount"] == "230.00"
        assert rows["BRACKET"]["stock"]["net_available"] == 32
        assert rows["GASKET"]["line"] is None
        assert rows["GASKET"]["reason"] == "supplier_cannot_meet_due"
        assert "reference lead time exceeding the planning window" in rows["GASKET"]["reason_explanation"]
        assert "does not establish actual supplier availability" in rows["GASKET"]["reason_explanation"]
        output = tool_output(script, 1)
        assert "not a confirmed or estimated" in output["date_semantics"]["due_date"]
        assert "cannot manually choose" in output["selection_policy"]
        assert result.trace.events[0].tool == "list_recommendations"


def test_multiple_tool_calls_with_duplicate_ids_are_rejected(workflow):
    first = call("list_stock", "{}", call_id="duplicate")["tool_calls"][0]
    second = call("create_draft", '{"skus":["BRACKET"]}', call_id="duplicate")["tool_calls"][0]
    with StubModel(workflow, [{"role": "assistant", "content": None, "tool_calls": [first, second]}]) as script:
        with pytest.raises(AgentError, match="unsupported completion"):
            script.run()
    assert workflow.list_drafts(BUYER) == []


def test_batch_exceeding_tool_budget_has_no_partial_effects(workflow):
    earlier = [call("list_stock", "{}", call_id=f"read-{index}") for index in range(MAX_STEPS - 2)]
    batch = [call("create_draft", '{"skus":["BRACKET"]}', call_id="draft")["tool_calls"][0],
             call("list_stock", "{}", call_id="over-budget")["tool_calls"][0]]
    with StubModel(workflow, earlier + [{"role": "assistant", "content": None, "tool_calls": batch}]) as script:
        with pytest.raises(AgentError, match="tool-call budget"):
            script.run()
    assert workflow.list_drafts(BUYER) == []


@pytest.mark.parametrize(("response", "code"), [
    (httpx.ReadTimeout("secret upstream timeout"), 504),
    (httpx.Response(401, text="secret upstream rejection"), 502),
    (httpx.Response(200, json={"choices": [{"message": []}]}), 502),
    (httpx.Response(302, headers={"location": "https://wrong-host.invalid/"}), 502),
])
def test_provider_errors_are_safe_and_never_follow_redirects(workflow, response, code):
    with StubModel(workflow, [response]) as script, pytest.raises(AgentError) as raised:
        script.run()
    assert raised.value.status_code == code
    assert "secret" not in str(raised.value)
    assert len(script.requests) == 1
    with workflow.database.transaction() as session:
        run = session.scalars(select(AgentRun)).one()
        assert run.status == "failed"
        assert run.trace["input_tokens"] is None




def test_truncated_completion_cannot_create_draft(workflow):
    response = httpx.Response(200, json={
        "choices": [{"message": call("create_draft", '{"skus":["BRACKET"]}'), "finish_reason": "length"}],
    })
    with StubModel(workflow, [response]) as script, pytest.raises(AgentError, match="unsupported completion"):
        script.run()
    assert workflow.list_drafts(BUYER) == []


def test_invalid_planning_window_returns_promptly(workflow):
    with StubModel(workflow, []) as script, pytest.raises(AgentError) as raised:
        script.run(horizon_days=31)
    assert raised.value.status_code == 400
    assert script.requests == []
