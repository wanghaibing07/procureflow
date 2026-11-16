from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date
from decimal import Decimal
from threading import Barrier, Lock

import pytest

from procureflow.catalog import DEMO_DATE, seed_catalog
from procureflow.contracts import PurchaseLine
from procureflow.db import Database
from procureflow.workflow import Actor, DraftStatus, ERPUnavailable, Role, Workflow, WorkflowError


BUYER = Actor("buyer-one", Role.BUYER)
OTHER_BUYER = Actor("buyer-two", Role.BUYER)
APPROVER = Actor("approver-one", Role.APPROVER)
SECOND_APPROVER = Actor("approver-two", Role.APPROVER)
LINE = PurchaseLine(
    sku="sku-01", supplier_id="vendor-01", quantity=5, unit_price=Decimal("3.07"), due_date=date(2026, 1, 8)
)


class FakeERP:
    def __init__(self) -> None:
        self.receipts: dict[str, str] = {}
        self.requests: list[tuple[str, tuple[PurchaseLine, ...]]] = []
        self.lose_response = False
        self.barrier: Barrier | None = None
        self.lock = Lock()

    def submit(self, draft_id: str, lines: tuple[PurchaseLine, ...]) -> str:
        if self.barrier:
            self.barrier.wait(timeout=10)
        with self.lock:
            self.requests.append((draft_id, lines))
            receipt = self.receipts.setdefault(draft_id, f"ERP-{len(self.receipts) + 1}")
            if self.lose_response:
                self.lose_response = False
                raise ERPUnavailable("sensitive upstream diagnostic")
            return receipt


@pytest.fixture
def context(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'workflow.db').as_posix()}")
    database.create_schema()
    erp = FakeERP()
    yield Workflow(database, erp), erp
    database.close()


def test_approved_draft_submits_once_and_persists_audit(context):
    workflow, erp = context
    draft = workflow.create_draft(BUYER, "request-1", (LINE,))
    assert draft.status == DraftStatus.DRAFT
    assert draft.total == Decimal("15.35")
    assert workflow.decide(APPROVER, draft.draft_id, True).status == DraftStatus.APPROVED

    submitted = workflow.submit(APPROVER, draft.draft_id)
    assert submitted.status == DraftStatus.SUBMITTED
    assert submitted.erp_reference == "ERP-1"
    assert workflow.submit(SECOND_APPROVER, draft.draft_id) == submitted
    assert erp.requests == [(draft.draft_id, (LINE,))]

    reloaded = Workflow(workflow.database, FakeERP()).get_draft(BUYER, draft.draft_id)
    assert reloaded == submitted
    assert [event.event for event in reloaded.events] == ["created", "approved", "submitted"]
    assert [event.actor_id for event in reloaded.events] == [BUYER.actor_id, APPROVER.actor_id, APPROVER.actor_id]
    assert all(event.happened_at.tzinfo is UTC for event in reloaded.events)


def test_rejected_draft_cannot_be_reapproved_or_sent(context):
    workflow, erp = context
    draft = workflow.create_draft(BUYER, "reject", (LINE,))
    with pytest.raises(WorkflowError) as early:
        workflow.submit(APPROVER, draft.draft_id)
    assert early.value.status_code == 409

    assert workflow.decide(APPROVER, draft.draft_id, False).status == DraftStatus.REJECTED
    with pytest.raises(WorkflowError) as approval:
        workflow.decide(SECOND_APPROVER, draft.draft_id, True)
    assert approval.value.status_code == 409
    with pytest.raises(WorkflowError) as submission:
        workflow.submit(APPROVER, draft.draft_id)
    assert submission.value.status_code == 409
    assert erp.requests == []


def test_buyer_cannot_access_another_buyers_draft_or_approve(context):
    workflow, _erp = context
    own = workflow.create_draft(BUYER, "same-key", (LINE,))
    other = workflow.create_draft(OTHER_BUYER, "same-key", (LINE,))
    assert workflow.list_drafts(BUYER) == [own]
    assert set(draft.draft_id for draft in workflow.list_drafts(APPROVER)) == {own.draft_id, other.draft_id}

    with pytest.raises(WorkflowError) as missing:
        workflow.get_draft(OTHER_BUYER, own.draft_id)
    assert missing.value.status_code == 404
    with pytest.raises(WorkflowError) as approve:
        workflow.decide(BUYER, own.draft_id, True)
    assert approve.value.status_code == 403
    with pytest.raises(WorkflowError) as self_approve:
        workflow.decide(Actor(BUYER.actor_id, Role.APPROVER), own.draft_id, True)
    assert self_approve.value.status_code == 403
    with pytest.raises(WorkflowError) as create:
        workflow.create_draft(APPROVER, "approver-create", (LINE,))
    assert create.value.status_code == 403
    with pytest.raises(WorkflowError) as submit:
        workflow.submit(BUYER, own.draft_id)
    assert submit.value.status_code == 403


def test_idempotent_draft_retry_compares_canonical_prices_and_line_contents(context):
    workflow, _erp = context
    first = workflow.create_draft(BUYER, "replay", (LINE,))
    assert workflow.create_draft(BUYER, "replay", (LINE.model_copy(update={"unit_price": Decimal("3.070")}),)) == first

    changed = LINE.model_copy(update={"quantity": 10})
    with pytest.raises(WorkflowError) as conflict:
        workflow.create_draft(BUYER, "replay", (changed,))
    assert conflict.value.status_code == 409
    assert workflow.list_drafts(BUYER) == [first]


def test_concurrent_same_recommendation_intent_creates_one_draft(context):
    workflow, _ = context
    with workflow.database.transaction() as session:
        seed_catalog(session)
    start = Barrier(2)

    def create():
        start.wait(timeout=10)
        return workflow.create_recommended_draft(BUYER, "manual:concurrent", ("BRACKET",), DEMO_DATE, 7)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result(timeout=15) for future in [pool.submit(create), pool.submit(create)]]
    assert first == second
    assert len(workflow.list_drafts(BUYER)) == 1


def test_recommendation_intent_rolls_back_for_ineligible_sku(context):
    workflow, _ = context
    with workflow.database.transaction() as session:
        seed_catalog(session)
    with pytest.raises(WorkflowError) as invalid:
        workflow.create_recommended_draft(BUYER, "manual:retry", ("GASKET",), DEMO_DATE, 7)
    assert invalid.value.status_code == 400
    draft = workflow.create_recommended_draft(BUYER, "manual:retry", ("BRACKET",), DEMO_DATE, 7)
    assert draft.lines[0].sku == "BRACKET"


def test_rejects_empty_drafts_and_blank_idempotency_keys(context):
    workflow, _erp = context
    for key, lines in [("request-1", ()), ("  ", (LINE,))]:
        with pytest.raises(WorkflowError) as invalid:
            workflow.create_draft(BUYER, key, lines)
        assert invalid.value.status_code == 400
    assert workflow.list_drafts(BUYER) == []


def test_large_amount_round_trips_without_binary_float_conversion(context):
    workflow, _erp = context
    large_line = PurchaseLine(
        sku="sku-02",
        supplier_id="vendor-02",
        quantity=1_000_000,
        unit_price=Decimal("9999999999.99"),
        due_date=date(2026, 1, 8),
    )
    draft = workflow.create_draft(BUYER, "large-total", (large_line, LINE))
    reloaded = workflow.get_draft(BUYER, draft.draft_id)
    assert reloaded.total == Decimal("9999999999990015.35")


def test_timeout_after_erp_acceptance_reuses_the_same_erp_receipt(context):
    workflow, erp = context
    draft = workflow.create_draft(BUYER, "retry", (LINE,))
    workflow.decide(APPROVER, draft.draft_id, True)
    erp.lose_response = True

    with pytest.raises(WorkflowError) as unavailable:
        workflow.submit(APPROVER, draft.draft_id)
    assert unavailable.value.status_code == 503
    assert "sensitive upstream diagnostic" not in str(unavailable.value)
    assert workflow.get_draft(BUYER, draft.draft_id).status == DraftStatus.APPROVED

    submitted = Workflow(workflow.database, erp).submit(SECOND_APPROVER, draft.draft_id)
    assert len(erp.receipts) == 1
    assert erp.requests[0] == erp.requests[1] == (draft.draft_id, (LINE,))
    assert submitted.erp_reference == erp.receipts[draft.draft_id]
    assert [event.event for event in submitted.events] == ["created", "approved", "submitted"]


def test_competing_approvals_record_one_decision(context):
    workflow, _erp = context
    draft = workflow.create_draft(BUYER, "competing-decisions", (LINE,))
    start = Barrier(2)

    def decide(actor, approve):
        start.wait(timeout=10)
        try:
            return workflow.decide(actor, draft.draft_id, approve)
        except WorkflowError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(decide, APPROVER, True), pool.submit(decide, SECOND_APPROVER, False)]
        results = [future.result(timeout=15) for future in futures]
    final = workflow.get_draft(BUYER, draft.draft_id)
    assert results.count(409) == 1
    assert final in results
    assert [event.event for event in final.events] == ["created", final.status]


def test_concurrent_submit_creates_only_one_erp_receipt_and_transition(context):
    workflow, erp = context
    draft = workflow.create_draft(BUYER, "competing-submissions", (LINE,))
    workflow.decide(APPROVER, draft.draft_id, True)
    erp.barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(workflow.submit, actor, draft.draft_id) for actor in (APPROVER, SECOND_APPROVER)]
        first, second = [future.result(timeout=15) for future in futures]

    assert first == second == workflow.get_draft(BUYER, draft.draft_id)
    assert erp.requests == [(draft.draft_id, (LINE,))] * 2
    assert erp.receipts == {draft.draft_id: first.erp_reference}
    assert [event.event for event in first.events] == ["created", "approved", "submitted"]
