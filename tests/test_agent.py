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
from procureflow.workflow import Actor, DraftStatus, Role, Workflow


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


class ScriptedModel:
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
    with ScriptedModel(workflow, [call("list_stock", "{}"), call("list_quotes", '{"sku":"BRACKET"}', call_id="quote"), answer(reply)]) as script:
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


@pytest.mark.parametrize(
    ("sku", "present", "quote_count"),
    [("BRACKET", True, 2), ("LABEL", True, 0), ("MISSING", False, 0)],
)
def test_quote_tool_distinguishes_catalog_membership_from_quotes(workflow, sku, present, quote_count):
    with ScriptedModel(workflow, [call("list_quotes", json.dumps({"sku": sku})), answer()]) as script:
        result = script.run()
        output = tool_output(script, 1)
        assert output["sku"] == sku
        assert output["catalog_sku_found"] is present
        assert len(output["items"]) == quote_count
        assert not output["truncated"]
        assert result.trace.events[0].result_count == quote_count
        assert result.trace.events[0].outcome == "ok"
        assert result.draft_id is None


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
            with ScriptedModel(workflow, [call("list_quotes", json.dumps({"sku": sku})), answer()]) as script:
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


@pytest.mark.parametrize(("actor", "allow_draft"), [(BUYER, False), (APPROVER, True)])
def test_unoffered_write_is_also_blocked_by_executor(workflow, actor, allow_draft):
    with ScriptedModel(workflow, [call("create_draft", '{"skus":["BRACKET"]}'), answer()]) as script:
        result = script.run(actor, allow_draft=allow_draft)
        assert {item["function"]["name"] for item in script.requests[0]["tools"]} == {"list_stock", "list_quotes", "list_recommendations"}
        assert "error" in tool_output(script, 1)
        assert result.trace.events[0].outcome == "rejected"
        assert result.draft_id is None
    assert workflow.list_drafts(APPROVER) == []


def test_draft_values_are_recomputed_and_retry_does_not_duplicate(workflow):
    request_id = uuid4()
    tool_call = call("create_draft", '{"skus":["BRACKET","BRACKET"]}')
    responses = [tool_call, answer(), tool_call, answer()]
    with ScriptedModel(workflow, responses) as script:
        first = script.run(allow_draft=True, request_id=request_id)
        second = script.run(allow_draft=True, request_id=request_id)
        assert first.draft_id == second.draft_id == tool_output(script, 1)["draft_id"]
        assert first.trace.events[0].result_count == 1
        assert len(workflow.list_drafts(BUYER)) == 1
        draft = workflow.get_draft(BUYER, first.draft_id)
        assert draft.status == DraftStatus.DRAFT
        assert draft.lines[0].quantity == 20
        assert draft.lines[0].supplier_id == "S-FAST"
        assert str(draft.total) == "230.00"


def test_agent_replay_resolves_original_draft_after_stock_and_quote_change(workflow):
    request_id = uuid4()
    first_call = call("create_draft", '{"skus":["BRACKET"]}')
    with ScriptedModel(workflow, [first_call, answer(), first_call, answer()]) as script:
        original = script.run(allow_draft=True, request_id=request_id)
        with workflow.database.transaction() as session:
            session.get(InventoryItem, "BRACKET").on_hand = 1000
            session.get(SupplierQuote, ("BRACKET", "S-FAST")).unit_price = 99
        retry = script.run(allow_draft=True, request_id=request_id)
        assert retry.draft_id == original.draft_id
        assert len(script.requests) == 2
        assert retry.trace.model_calls == 0 and "找回" in retry.answer
        assert workflow.get_draft(BUYER, retry.draft_id).total == 230


def test_agent_replay_binds_original_request_and_skips_model(workflow):
    request_id = uuid4()
    with ScriptedModel(workflow, [
        call("create_draft", '{"skus":["BRACKET"]}'), answer(),
        call("create_draft", '{"skus":["GASKET"]}'), answer(),
        call("create_draft", '{"skus":["BRACKET"]}'), answer(),
    ]) as script:
        original = script.run(allow_draft=True, request_id=request_id)
        replay = script.run(allow_draft=True, request_id=request_id)
        assert original.draft_id is not None
        assert replay.draft_id == original.draft_id and len(script.requests) == 2
        for changes in ({"horizon_days": 8}, {"prompt": "different request"}, {"allow_draft": False}):
            options = {"allow_draft": True, **changes}
            with pytest.raises(AgentError) as error:
                script.run(request_id=request_id, **options)
            assert error.value.status_code == 409
    assert len(workflow.list_drafts(BUYER)) == 1


def test_response_lost_retry_does_not_depend_on_model_repeating_write(workflow):
    request_id = uuid4()
    with ScriptedModel(workflow, [call("create_draft", '{"skus":["BRACKET"]}'), answer(),
                                  answer("No draft this time")]) as script:
        first = script.run(allow_draft=True, request_id=request_id)
        retry = script.run(allow_draft=True, request_id=request_id)
        assert retry.draft_id == first.draft_id
        assert retry.draft_id in retry.answer
        assert retry.trace.model_calls == 0 and not retry.trace.events
        assert len(script.requests) == 2  # A second stochastic model run cannot hide the draft.
    assert len(workflow.list_drafts(BUYER)) == 1


def test_same_request_concurrent_run_fails_closed_until_original_finishes(workflow):
    request_id = uuid4()
    entered, resume = Event(), Event()
    with ScriptedModel(workflow, [call("create_draft", '{"skus":["BRACKET"]}'), answer()]) as script:
        original_complete = script.model.complete

        def blocked_complete(messages, tools):
            if not entered.is_set():
                entered.set()
                assert resume.wait(10)
            return original_complete(messages, tools)

        script.model.complete = blocked_complete
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(script.run, allow_draft=True, request_id=request_id)
            assert entered.wait(10)
            try:
                with pytest.raises(AgentError) as error:
                    script.run(allow_draft=True, request_id=request_id)
                assert error.value.status_code == 409
            finally:
                resume.set()
            first = running.result(timeout=10)
        retry = script.run(allow_draft=True, request_id=request_id)
        assert retry.draft_id == first.draft_id
        assert len(workflow.list_drafts(BUYER)) == 1


def test_concurrent_retry_recovers_committed_draft_while_model_is_pending(workflow):
    request_id = uuid4()
    entered, resume = Event(), Event()
    with ScriptedModel(workflow, [call("create_draft", '{"skus":["BRACKET"]}'), answer()]) as script:
        original_complete = script.model.complete
        count = 0

        def blocked_complete(messages, tools):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()  # The create_draft transaction already committed.
                assert resume.wait(10)
            return original_complete(messages, tools)

        script.model.complete = blocked_complete
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(script.run, allow_draft=True, request_id=request_id)
            assert entered.wait(10)
            try:
                retry = script.run(allow_draft=True, request_id=request_id)
                assert retry.draft_id and retry.trace.model_calls == 0
            finally:
                resume.set()
            first = running.result(timeout=10)
        assert first.draft_id == retry.draft_id
        assert len(workflow.list_drafts(BUYER)) == 1


def test_same_uuid_is_isolated_by_buyer(workflow):
    other = Actor("second-buyer", Role.BUYER)
    request_id = uuid4()
    with ScriptedModel(workflow, [call("create_draft", '{"skus":["BRACKET"]}'), answer(),
                                  call("create_draft", '{"skus":["BRACKET"]}'), answer()]) as script:
        first = script.run(allow_draft=True, request_id=request_id)
        second = script.run(other, allow_draft=True, request_id=request_id)
        assert first.draft_id != second.draft_id
        assert script.run(allow_draft=True, request_id=request_id).draft_id == first.draft_id
        assert script.run(other, allow_draft=True, request_id=request_id).draft_id == second.draft_id


def test_mysql_agent_replay_and_concurrent_claim(mysql_agent_workflow):
    """Exercise request binding and both concurrent states on an isolated MySQL database."""
    workflow = mysql_agent_workflow
    buyer = Actor(f"mysql-agent-{uuid4()}", Role.BUYER)
    lost_request, concurrent_request = uuid4(), uuid4()
    draft_call = call("create_draft", '{"skus":["BRACKET"]}')
    with ScriptedModel(workflow, [draft_call, httpx.ReadTimeout("simulated lost response"),
                                  draft_call, answer()]) as script:
        with pytest.raises(AgentError, match="in time"):
            script.run(buyer, allow_draft=True, request_id=lost_request)
        retry = script.run(buyer, allow_draft=True, request_id=lost_request)
        assert retry.draft_id is not None and retry.trace.model_calls == 0
        assert len(script.requests) == 2
        with pytest.raises(AgentError) as changed:
            script.run(buyer, allow_draft=True, request_id=lost_request, prompt="different request")
        assert changed.value.status_code == 409

        before_tool, allow_tool, after_commit, allow_answer = Event(), Event(), Event(), Event()
        original_complete = script.model.complete
        count = 0

        def gated_complete(messages, tools):
            nonlocal count
            count += 1
            if count == 1:
                before_tool.set()
                assert allow_tool.wait(20)
            elif count == 2:
                after_commit.set()
                assert allow_answer.wait(20)
            return original_complete(messages, tools)

        script.model.complete = gated_complete
        with ThreadPoolExecutor(max_workers=2) as pool:
            original = pool.submit(script.run, buyer, allow_draft=True, request_id=concurrent_request)
            try:
                assert before_tool.wait(20)
                with pytest.raises(AgentError) as running:
                    script.run(buyer, allow_draft=True, request_id=concurrent_request)
                assert running.value.status_code == 409
                allow_tool.set()
                assert after_commit.wait(20)
                replay = script.run(buyer, allow_draft=True, request_id=concurrent_request)
                assert replay.draft_id is not None and replay.trace.model_calls == 0
            finally:
                allow_tool.set()
                allow_answer.set()
            assert original.result(timeout=20).draft_id == replay.draft_id
        drafts = workflow.list_drafts(buyer)
        assert {draft.draft_id for draft in drafts} == {retry.draft_id, replay.draft_id}
        assert len(drafts) == 2


def test_completed_read_only_response_replays_without_new_model_side_effect(workflow):
    request_id = uuid4()
    with ScriptedModel(workflow, [answer("No draft"), call("create_draft", '{"skus":["BRACKET"]}'), answer()]) as script:
        first = script.run(allow_draft=True, request_id=request_id)
        second = script.run(allow_draft=True, request_id=request_id)
        assert first.draft_id is None and second.draft_id is None
        assert second.trace.model_calls == 0 and "未生成" in second.answer
        assert len(script.requests) == 1
    assert workflow.list_drafts(BUYER) == []


def test_incomplete_run_with_committed_draft_is_recoverable(workflow):
    request_id = uuid4()
    with ScriptedModel(workflow, [call("create_draft", '{"skus":["BRACKET"]}'), answer()]) as script:
        first = script.run(allow_draft=True, request_id=request_id)
        # Simulate an interrupted process after its draft transaction committed.
        with workflow.database.transaction() as session:
            request = session.scalars(select(AgentRequest)).one()
            request.state, request.owner = "running", str(uuid4())
        second = script.run(allow_draft=True, request_id=request_id)
        assert second.draft_id == first.draft_id and second.trace.model_calls == 0


def test_old_draft_without_bound_agent_request_fails_closed(workflow):
    request_id = uuid4()
    workflow.create_recommended_draft(BUYER, f"agent:{request_id}", ("BRACKET",), DEMO_DATE, 7)
    with ScriptedModel(workflow, []) as script:
        for _ in range(2):
            with pytest.raises(AgentError, match="no saved agent request details") as error:
                script.run(allow_draft=True, request_id=request_id)
            assert error.value.status_code == 409
        assert script.requests == []


@pytest.mark.parametrize(("name", "arguments"), [
    ("create_draft", '{"skus":["BRACKET"],"unit_price": 0.01}'),
    ("create_draft", '{"skus":["BRACKET","GASKET"]}'),
    ("list_quotes", '{"sku":123}'),
    ("list_quotes", "not JSON"),
    ("submit_draft", '{"draft_id":"some-draft"}'),
])
def test_untrusted_tool_calls_cannot_write_outside_contract(workflow, name, arguments):
    with ScriptedModel(workflow, [call(name, arguments), answer()]) as script:
        result = script.run(allow_draft=True)
        assert result.draft_id is None
        assert "error" in tool_output(script, 1)
        assert result.trace.events[0].outcome == "rejected"
    assert workflow.list_drafts(APPROVER) == []


def test_tool_budget_rejects_last_call_before_side_effect(workflow):
    responses = [call("list_stock", "{}", call_id=f"read-{step}") for step in range(MAX_STEPS - 1)]
    responses.append(call("create_draft", '{"skus":["BRACKET"]}'))
    with ScriptedModel(workflow, responses) as script, pytest.raises(AgentError, match="tool-call budget"):
        script.run(allow_draft=True)
    with workflow.database.transaction() as session:
        run = session.scalars(select(AgentRun)).one()
        assert run.status == "failed"
        assert run.trace["model_calls"] == MAX_STEPS
        assert len(run.trace["events"]) == MAX_STEPS - 1
    assert workflow.list_drafts(APPROVER) == []


def test_provider_can_return_multiple_tool_calls_even_when_parallel_disabled(workflow):
    first = call("list_stock", "{}", call_id="stock")["tool_calls"][0]
    second = call("list_quotes", '{"sku":"BRACKET"}', call_id="quote")["tool_calls"][0]
    with ScriptedModel(workflow, [{"role": "assistant", "content": None, "tool_calls": [first, second]}, answer()]) as script:
        result = script.run()
        assert [event.tool for event in result.trace.events] == ["list_stock", "list_quotes"]
        messages = script.requests[1]["messages"]
        assert [message.get("tool_call_id") for message in messages[-2:]] == ["stock", "quote"]
        assert len(messages[-3]["tool_calls"]) == 2


def test_recommendation_tool_exposes_computed_order_and_unavailable_supplier(workflow):
    with ScriptedModel(workflow, [call("list_recommendations", "{}"), answer()]) as script:
        result = script.run()
        rows = {row["stock"]["sku"]: row for row in tool_output(script, 1)["items"]}
        assert rows["BRACKET"]["line"]["quantity"] == 20
        assert rows["BRACKET"]["line"]["supplier_id"] == "S-FAST"
        assert rows["BRACKET"]["total_amount"] == "230.00"
        assert rows["BRACKET"]["stock"]["net_available"] == 32
        assert rows["GASKET"]["line"] is None
        assert rows["GASKET"]["reason"] == "supplier_cannot_meet_due"
        assert result.trace.events[0].tool == "list_recommendations"


def test_multiple_tool_calls_with_duplicate_ids_are_rejected(workflow):
    first = call("list_stock", "{}", call_id="duplicate")["tool_calls"][0]
    second = call("create_draft", '{"skus":["BRACKET"]}', call_id="duplicate")["tool_calls"][0]
    with ScriptedModel(workflow, [{"role": "assistant", "content": None, "tool_calls": [first, second]}]) as script:
        with pytest.raises(AgentError, match="unsupported completion"):
            script.run(allow_draft=True)
    assert workflow.list_drafts(BUYER) == []


def test_batch_exceeding_tool_budget_has_no_partial_effects(workflow):
    earlier = [call("list_stock", "{}", call_id=f"read-{index}") for index in range(MAX_STEPS - 2)]
    batch = [call("create_draft", '{"skus":["BRACKET"]}', call_id="draft")["tool_calls"][0],
             call("list_stock", "{}", call_id="over-budget")["tool_calls"][0]]
    with ScriptedModel(workflow, earlier + [{"role": "assistant", "content": None, "tool_calls": batch}]) as script:
        with pytest.raises(AgentError, match="tool-call budget"):
            script.run(allow_draft=True)
    assert workflow.list_drafts(BUYER) == []


@pytest.mark.parametrize(("response", "code"), [
    (httpx.ReadTimeout("secret upstream timeout"), 504),
    (httpx.Response(401, text="secret upstream rejection"), 502),
    (httpx.Response(200, json={"choices": [{"message": []}]}), 502),
    (httpx.Response(302, headers={"location": "https://wrong-host.invalid/"}), 502),
])
def test_provider_errors_are_safe_and_never_follow_redirects(workflow, response, code):
    with ScriptedModel(workflow, [response]) as script, pytest.raises(AgentError) as raised:
        script.run()
    assert raised.value.status_code == code
    assert "secret" not in str(raised.value)
    assert len(script.requests) == 1
    with workflow.database.transaction() as session:
        run = session.scalars(select(AgentRun)).one()
        assert run.status == "failed"
        assert run.trace["input_tokens"] is None


def test_retry_after_final_response_timeout_keeps_existing_draft(workflow):
    request_id = uuid4()
    tool_call = call("create_draft", '{"skus":["BRACKET"]}')
    with ScriptedModel(workflow, [tool_call, httpx.ReadTimeout("upstream"), tool_call, answer()]) as script:
        with pytest.raises(AgentError, match="in time"):
            script.run(allow_draft=True, request_id=request_id)
        result = script.run(allow_draft=True, request_id=request_id)
    with workflow.database.transaction() as session:
        attempts = list(session.scalars(select(AgentRun).order_by(AgentRun.attempt_id)))
        assert [attempt.status for attempt in attempts] == ["failed", "replayed"]
        assert [attempt.draft_id for attempt in attempts] == [result.draft_id, result.draft_id]
    assert len(workflow.list_drafts(BUYER)) == 1


def test_truncated_completion_cannot_create_draft(workflow):
    response = httpx.Response(200, json={
        "choices": [{"message": call("create_draft", '{"skus":["BRACKET"]}'), "finish_reason": "length"}],
    })
    with ScriptedModel(workflow, [response]) as script, pytest.raises(AgentError, match="unsupported completion"):
        script.run(allow_draft=True)
    assert workflow.list_drafts(BUYER) == []


def test_invalid_planning_window_returns_promptly(workflow):
    with ScriptedModel(workflow, []) as script, pytest.raises(AgentError) as raised:
        script.run(horizon_days=31)
    assert raised.value.status_code == 400
    assert script.requests == []
