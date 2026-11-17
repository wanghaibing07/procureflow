from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import os
from secrets import token_hex
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from procureflow.api import create_app
from procureflow.agent import AgentRequest as SavedAgentRequest
from procureflow.catalog import DEMO_DATE, InboundShipment, InventoryItem, Supplier, SupplierQuote
from procureflow.erp import HTTPERPClient
from procureflow.mock_erp import create_app as create_mock_erp
from procureflow.settings import Settings
from procureflow.workflow import Actor, Role, Workflow, WorkflowError, _DraftPreview, _now


@pytest.fixture(params=["sqlite", "mysql"] if os.getenv("PROCUREFLOW_TEST_DB_URL") else ["sqlite"])
def deployment(request, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'procureflow.sqlite3'}"
    if request.param == "mysql":
        database_url = os.environ["PROCUREFLOW_TEST_DB_URL"]
        parsed_url = make_url(database_url)
        if parsed_url.get_backend_name() != "mysql" or parsed_url.database != "procureflow_test":
            pytest.fail("PROCUREFLOW_TEST_DB_URL must target a dedicated MySQL database named procureflow_test.")

    settings = Settings(database_url, token_hex(24), token_hex(24), "http://testserver", token_hex(24))
    erp_url = f"sqlite:///{tmp_path / 'erp.sqlite3'}"
    with TestClient(create_mock_erp(token=settings.erp_token, database_url=erp_url)) as erp_server:
        erp = HTTPERPClient(settings.erp_url, settings.erp_token, client=erp_server)
        with TestClient(create_app(settings, erp=erp)) as application:
            yield application, settings, erp_server


def auth(token, **headers):
    return {"Authorization": f"Bearer {token}", **headers}


def preview_bracket(client, settings):
    response = client.post("/api/drafts/preview", headers=auth(settings.buyer_token), json={"skus": ["BRACKET"]})
    assert response.status_code == 200, response.text
    return response.json()["preview_hash"]


def create_bracket_draft(client, settings, request_key, preview_hash=None):
    preview_hash = preview_hash or preview_bracket(client, settings)
    return client.post(
        "/api/drafts",
        headers=auth(settings.buyer_token, **{"Idempotency-Key": request_key}),
        json={"skus": ["BRACKET"], "preview_hash": preview_hash},
    )


def test_stock_to_erp_receipt_with_two_roles(deployment):
    client, settings, erp_server = deployment
    buyer = auth(settings.buyer_token)
    approver = auth(settings.approver_token)
    assert client.get("/api/me", headers=buyer).json()["role"] == "buyer"
    assert client.get("/api/me", headers=approver).json()["role"] == "approver"
    results = {row["stock"]["sku"]: row for row in client.get("/api/recommendations", headers=buyer).json()}
    assert results["BRACKET"]["line"]["quantity"] == 20
    assert results["BRACKET"]["line"]["supplier_id"] == "S-FAST"
    assert results["GASKET"]["line"] is None
    assert "LABEL" not in results

    request_key = str(uuid4())
    preview_hash = preview_bracket(client, settings)
    first = create_bracket_draft(client, settings, request_key, preview_hash)
    assert first.status_code == 200, first.text
    draft = first.json()
    assert draft["total"] == "230.00"
    assert create_bracket_draft(client, settings, request_key, preview_hash).json()["draft_id"] == draft["draft_id"]
    path = f"/api/drafts/{draft['draft_id']}"
    assert client.post(f"{path}/submit", headers=approver).status_code == 409
    assert client.post(f"{path}/decision", headers=buyer, json={"approve": True}).status_code == 403
    approval = client.post(f"{path}/decision", headers=approver, json={"approve": True})
    assert approval.json()["status"] == "approved"
    assert client.post(f"{path}/submit", headers=buyer).status_code == 403
    submitted = client.post(f"{path}/submit", headers=approver)
    assert submitted.status_code == 200, submitted.text
    outcome = submitted.json()
    assert outcome["status"] == "submitted"
    assert outcome["erp_reference"]
    assert [event["event"] for event in outcome["events"]] == ["created", "approved", "submitted"]
    assert client.post(f"{path}/submit", headers=approver).json()["erp_reference"] == outcome["erp_reference"]
    direct_replay = erp_server.post(
        "/purchase-requests",
        headers=auth(settings.erp_token, **{"Idempotency-Key": draft["draft_id"]}),
        json={"lines": draft["lines"]},
    )
    assert direct_replay.status_code == 200
    assert direct_replay.json()["erp_reference"] == outcome["erp_reference"]


def test_rejected_draft_cannot_be_submitted(deployment):
    client, settings, _ = deployment
    response = create_bracket_draft(client, settings, str(uuid4()))
    path = f"/api/drafts/{response.json()['draft_id']}"
    approver = auth(settings.approver_token)
    assert client.post(f"{path}/decision", headers=approver, json={"approve": False}).json()["status"] == "rejected"
    assert client.post(f"{path}/submit", headers=approver).status_code == 409


def test_concurrent_retries_create_only_one_draft(deployment):
    client, settings, _ = deployment
    key = str(uuid4())
    preview_hash = preview_bracket(client, settings)
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: create_bracket_draft(client, settings, key, preview_hash), range(4)))
    assert [response.status_code for response in responses] == [200] * 4
    assert len({response.json()["draft_id"] for response in responses}) == 1
    draft_id = responses[0].json()["draft_id"]
    drafts = client.get("/api/drafts", headers=auth(settings.buyer_token)).json()
    assert sum(draft["draft_id"] == draft_id for draft in drafts) == 1


def test_auth_bad_inputs_and_model_disabled(deployment):
    client, settings, _ = deployment
    buyer = auth(settings.buyer_token)
    assert client.get("/api/stock").status_code == 401
    assert client.get("/api/stock", headers=auth(token_hex(24))).status_code == 401
    assert client.get("/api/stock?as_of=9999-12-31", headers=buyer).status_code == 422
    assert client.get("/api/stock?horizon_days=0", headers=buyer).status_code == 422
    request_headers = auth(settings.buyer_token, **{"Idempotency-Key": str(uuid4())})
    assert client.post("/api/drafts/preview", headers=buyer, json={"skus": ["GASKET"]}).status_code == 400
    assert client.post("/api/drafts", headers=request_headers, json={"skus": ["BRACKET"]}).status_code == 409
    assert client.post("/api/drafts", headers=request_headers, json={"skus": ["BRACKET"], "total": "0.01"}).status_code == 422
    assert client.get("/healthz").json() == {"alive": True, "model_configured": False}
    response = client.post("/api/agent/run", headers=buyer, json={"request_id": str(uuid4()), "prompt": "查缺货"})
    assert response.status_code == 503


def test_legacy_draft_can_be_recovered_without_any_model_configuration(deployment):
    client, settings, _ = deployment
    actor = Actor("demo-buyer", Role.BUYER)
    request_id, prompt = uuid4(), "legacy request"
    original = client.app.state.workflow.create_recommended_draft(
        actor, f"agent:{request_id}", ("BRACKET",), DEMO_DATE, 7,
    )
    key = hashlib.sha256(json.dumps([actor.actor_id, str(request_id)]).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(
        [str(request_id), actor.role.value, prompt, DEMO_DATE.isoformat(), 7, True],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    with client.app.state.db.transaction() as session:
        session.add(SavedAgentRequest(request_key=key, fingerprint=fingerprint, state="running", owner=str(uuid4())))
    payload = {"request_id": str(request_id), "prompt": prompt, "allow_draft": True}
    response = client.post("/api/agent/run", headers=auth(settings.buyer_token), json=payload)
    assert response.status_code == 200
    assert response.json()["draft_id"] == original.draft_id
    assert response.json()["trace"]["model_calls"] == 0
    payload["prompt"] = "different request"
    assert client.post("/api/agent/run", headers=auth(settings.buyer_token), json=payload).status_code == 409


def test_draft_retry_uses_original_intent_after_stock_and_quote_change(deployment):
    client, settings, _ = deployment
    key = str(uuid4())
    preview_hash = preview_bracket(client, settings)
    first = create_bracket_draft(client, settings, key, preview_hash)
    assert first.status_code == 200
    with client.app.state.db.transaction() as session:
        item = session.get(InventoryItem, "BRACKET")
        quote = session.get(SupplierQuote, ("BRACKET", "S-FAST"))
        original_stock, original_price = item.on_hand, quote.unit_price
        item.on_hand, quote.unit_price = 1000, 99
    try:
        retry = create_bracket_draft(client, settings, key, preview_hash)
        assert retry.status_code == 200
        assert retry.json() == first.json()
        changed = client.post(
            "/api/drafts",
            headers=auth(settings.buyer_token, **{"Idempotency-Key": key}),
            json={"skus": ["BRACKET"], "horizon_days": 8, "preview_hash": preview_hash},
        )
        assert changed.status_code == 409
        changed_sku = client.post(
            "/api/drafts",
            headers=auth(settings.buyer_token, **{"Idempotency-Key": key}),
            json={"skus": ["GASKET"], "preview_hash": preview_hash},
        )
        assert changed_sku.status_code == 409
    finally:
        with client.app.state.db.transaction() as session:
            session.get(InventoryItem, "BRACKET").on_hand = original_stock
            session.get(SupplierQuote, ("BRACKET", "S-FAST")).unit_price = original_price


def test_preview_binds_actor_sku_window_and_current_lines(deployment):
    client, settings, _ = deployment
    buyer, approver = auth(settings.buyer_token), auth(settings.approver_token)
    request = {"skus": ["BRACKET"]}
    preview = client.post("/api/drafts/preview", headers=buyer, json=request)
    assert preview.status_code == 200
    body = preview.json()
    assert body["lines"][0]["sku"] == "BRACKET"
    assert body["lines"][0]["quantity"] == 20 and body["total"] == "230.00"
    assert client.post("/api/drafts/preview", headers=approver, json=request).status_code == 403
    assert client.post("/api/drafts", headers=auth(settings.approver_token, **{"Idempotency-Key": str(uuid4())}),
                       json={**request, "preview_hash": body["preview_hash"]}).status_code == 403
    headers = auth(settings.buyer_token, **{"Idempotency-Key": str(uuid4())})
    assert client.post("/api/drafts", headers=headers, json={**request, "horizon_days": 8,
                       "preview_hash": body["preview_hash"]}).status_code == 409
    workflow = client.app.state.workflow
    preview_other = workflow.preview_recommended_draft(Actor("other-buyer", Role.BUYER),
                                                        ("BRACKET",), DEMO_DATE, 7)
    with pytest.raises(WorkflowError) as error:
        workflow.create_recommended_draft(Actor("different-buyer", Role.BUYER), str(uuid4()),
                                           ("BRACKET",), DEMO_DATE, 7, preview_other.preview_hash)
    assert error.value.status_code == 409


def test_preview_is_single_use_even_with_different_keys_and_after_connection_reset(deployment):
    client, settings, _ = deployment
    token, key = preview_bracket(client, settings), str(uuid4())
    first = create_bracket_draft(client, settings, key, token)
    assert first.status_code == 200
    # Dispose the connection pool; a separate test covers application process replacement.
    client.app.state.db.engine.dispose()
    assert create_bracket_draft(client, settings, key, token).json()["draft_id"] == first.json()["draft_id"]
    assert create_bracket_draft(client, settings, str(uuid4()), token).status_code == 409
    new_token = preview_bracket(client, settings)
    assert new_token != token
    second = create_bracket_draft(client, settings, str(uuid4()), new_token)
    assert second.status_code == 200 and second.json()["draft_id"] != first.json()["draft_id"]


def test_one_preview_concurrent_different_keys_only_creates_one_draft(deployment):
    client, settings, _ = deployment
    token = preview_bracket(client, settings)
    keys = [str(uuid4()), str(uuid4())]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda key: create_bracket_draft(client, settings, key, token), keys))
    assert sorted(result.status_code for result in results) == [200, 409]
    success = next(result for result in results if result.status_code == 200)
    successful_key = keys[results.index(success)]
    assert create_bracket_draft(client, settings, successful_key, token).json()["draft_id"] == success.json()["draft_id"]


def test_expired_or_unknown_preview_fails_closed_but_committed_retry_survives_expiry(deployment):
    client, settings, _ = deployment
    expired_token = preview_bracket(client, settings)
    token_hash = hashlib.sha256(expired_token.encode("ascii")).hexdigest()
    with client.app.state.db.transaction() as session:
        session.get(_DraftPreview, token_hash).expires_at = _now() - timedelta(seconds=1)
    assert create_bracket_draft(client, settings, str(uuid4()), expired_token).status_code == 409
    assert create_bracket_draft(client, settings, str(uuid4()), "0" * 64).status_code == 409
    token, key = preview_bracket(client, settings), str(uuid4())
    first = create_bracket_draft(client, settings, key, token)
    assert first.status_code == 200
    with client.app.state.db.transaction() as session:
        session.get(_DraftPreview, hashlib.sha256(token.encode("ascii")).hexdigest()).expires_at = _now() - timedelta(seconds=1)
    assert create_bracket_draft(client, settings, key, token).json()["draft_id"] == first.json()["draft_id"]


def test_stale_preview_is_rejected_before_writing_and_new_preview_works(deployment):
    client, settings, _ = deployment
    old_hash = preview_bracket(client, settings)
    key = str(uuid4())
    with client.app.state.db.transaction() as session:
        quote = session.get(SupplierQuote, ("BRACKET", "S-FAST"))
        original = quote.unit_price
        quote.unit_price = Decimal("12.00")
    try:
        assert create_bracket_draft(client, settings, key, old_hash).status_code == 409
        new_hash = preview_bracket(client, settings)
        fresh = create_bracket_draft(client, settings, key, new_hash)
        assert fresh.status_code == 200 and fresh.json()["total"] == "240.00"
    finally:
        with client.app.state.db.transaction() as session:
            session.get(SupplierQuote, ("BRACKET", "S-FAST")).unit_price = original


@pytest.mark.parametrize("changed_field", ["price", "inventory"])
def test_stale_rejection_permanently_invalidates_even_if_catalog_reverts(deployment, changed_field):
    client, settings, _ = deployment
    old_token, old_key = preview_bracket(client, settings), str(uuid4())
    with client.app.state.db.transaction() as session:
        quote = session.get(SupplierQuote, ("BRACKET", "S-FAST"))
        item = session.get(InventoryItem, "BRACKET")
        old_price, old_stock = quote.unit_price, item.on_hand
        if changed_field == "price":
            quote.unit_price = Decimal("12.00")
        else:
            item.on_hand = 10000
    try:
        assert create_bracket_draft(client, settings, old_key, old_token).status_code == 409
        with client.app.state.db.transaction() as session:
            session.get(SupplierQuote, ("BRACKET", "S-FAST")).unit_price = old_price
            session.get(InventoryItem, "BRACKET").on_hand = old_stock
        new_token, new_key = preview_bracket(client, settings), str(uuid4())
        fresh = create_bracket_draft(client, settings, new_key, new_token)
        assert fresh.status_code == 200
        client.app.state.db.engine.dispose()
        delayed = create_bracket_draft(client, settings, old_key, old_token)
        assert delayed.status_code == 409
        assert create_bracket_draft(client, settings, new_key, new_token).json()["draft_id"] == fresh.json()["draft_id"]
    finally:
        with client.app.state.db.transaction() as session:
            session.get(SupplierQuote, ("BRACKET", "S-FAST")).unit_price = old_price
            session.get(InventoryItem, "BRACKET").on_hand = old_stock


def cancel_preview(client, token, preview_hash):
    return client.post("/api/drafts/preview/cancel", headers=auth(token), json={"preview_hash": preview_hash})


def test_cancel_before_delayed_confirmation_revokes_preview_and_requires_new_review(deployment):
    client, settings, _ = deployment
    token, key = preview_bracket(client, settings), str(uuid4())
    assert cancel_preview(client, settings.approver_token, token).status_code == 403
    assert cancel_preview(client, settings.buyer_token, "0" * 64).status_code == 404
    with pytest.raises(WorkflowError) as wrong_buyer:
        client.app.state.workflow.cancel_draft_preview(Actor("another-buyer", Role.BUYER), token)
    assert wrong_buyer.value.status_code == 404
    result = cancel_preview(client, settings.buyer_token, token)
    assert result.status_code == 200 and result.json() == {"status": "cancelled", "draft": None}
    assert cancel_preview(client, settings.buyer_token, token).json()["status"] == "cancelled"
    assert create_bracket_draft(client, settings, key, token).status_code == 409
    fresh = create_bracket_draft(client, settings, str(uuid4()), preview_bracket(client, settings))
    assert fresh.status_code == 200


def test_cancel_after_commit_returns_authoritative_draft_and_preserves_replay(deployment):
    client, settings, _ = deployment
    token, key = preview_bracket(client, settings), str(uuid4())
    result = create_bracket_draft(client, settings, key, token)
    assert result.status_code == 200
    cancelled = cancel_preview(client, settings.buyer_token, token)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "committed"
    assert cancelled.json()["draft"] == result.json()
    assert create_bracket_draft(client, settings, key, token).json() == result.json()
    assert create_bracket_draft(client, settings, str(uuid4()), token).status_code == 409


def test_cancel_racing_confirmation_returns_committed_draft(deployment, monkeypatch):
    client, settings, _ = deployment
    token, key = preview_bracket(client, settings), str(uuid4())
    entered, release, cancelling = Event(), Event(), Event()
    original = Workflow._preview_hash

    def pause_after_locks(*args):
        value = original(*args)
        entered.set()
        assert release.wait(10)
        return value

    monkeypatch.setattr(Workflow, "_preview_hash", staticmethod(pause_after_locks))

    def cancel():
        cancelling.set()
        return cancel_preview(client, settings.buyer_token, token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        confirmation = executor.submit(create_bracket_draft, client, settings, key, token)
        assert entered.wait(10)
        cancellation = executor.submit(cancel)
        assert cancelling.wait(10)
        try:
            with pytest.raises(FutureTimeout):
                cancellation.result(timeout=0.3)
        finally:
            release.set()
        created, resolved = confirmation.result(timeout=10), cancellation.result(timeout=10)
    assert created.status_code == 200
    assert resolved.status_code == 200 and resolved.json()["status"] == "committed"
    assert resolved.json()["draft"] == created.json()


def test_confirmation_racing_cancel_fails_closed(deployment):
    client, settings, _ = deployment
    token, key = preview_bracket(client, settings), str(uuid4())
    target = hashlib.sha256(token.encode("ascii")).hexdigest()
    entered, release, confirming = Event(), Event(), Event()

    def hold_update(_mapper, _connection, preview):
        if preview.token_hash == target:
            entered.set()
            assert release.wait(10)

    event.listen(_DraftPreview, "before_update", hold_update)
    try:
        def confirm():
            confirming.set()
            return create_bracket_draft(client, settings, key, token)

        with ThreadPoolExecutor(max_workers=2) as executor:
            cancellation = executor.submit(cancel_preview, client, settings.buyer_token, token)
            assert entered.wait(10)
            confirmation = executor.submit(confirm)
            assert confirming.wait(10)
            try:
                with pytest.raises(FutureTimeout):
                    confirmation.result(timeout=0.3)
            finally:
                release.set()
            assert cancellation.result(timeout=10).json()["status"] == "cancelled"
            assert confirmation.result(timeout=10).status_code == 409
    finally:
        release.set()
        event.remove(_DraftPreview, "before_update", hold_update)


def test_failed_invalidation_transaction_does_not_report_a_false_409(deployment):
    client, settings, _ = deployment
    token, key = preview_bracket(client, settings), str(uuid4())
    engine = client.app.state.db.engine
    with client.app.state.db.transaction() as session:
        quote = session.get(SupplierQuote, ("BRACKET", "S-FAST"))
        original = quote.unit_price
        quote.unit_price = Decimal("12.00")

    def fail_invalidation(_conn, _cursor, statement, _params, _context, _many):
        if statement.lower().startswith("update purchase_draft_previews"):
            raise OperationalError("injected update failure", {}, Exception("test"))

    event.listen(engine, "before_cursor_execute", fail_invalidation)
    try:
        with pytest.raises(OperationalError):
            create_bracket_draft(client, settings, key, token)
    finally:
        event.remove(engine, "before_cursor_execute", fail_invalidation)
        with client.app.state.db.transaction() as session:
            session.get(SupplierQuote, ("BRACKET", "S-FAST")).unit_price = original
    # No 409 was returned: rollback leaves the preview usable. A client whose
    # result is unknown must cancel it before preparing a different request.
    assert create_bracket_draft(client, settings, key, token).status_code == 200


def test_cancel_database_failure_never_reports_cancelled(deployment):
    client, settings, _ = deployment
    token, key = preview_bracket(client, settings), str(uuid4())
    engine = client.app.state.db.engine

    def fail_cancel(_conn, _cursor, statement, _params, _context, _many):
        if statement.lower().startswith("update purchase_draft_previews"):
            raise OperationalError("injected cancel failure", {}, Exception("test"))

    event.listen(engine, "before_cursor_execute", fail_cancel)
    try:
        with pytest.raises(OperationalError):
            cancel_preview(client, settings.buyer_token, token)
    finally:
        event.remove(engine, "before_cursor_execute", fail_cancel)
    # Failed cancellation rolled back; the client must retain the pending
    # request until it can obtain a positive cancellation/commit result.
    assert create_bracket_draft(client, settings, key, token).status_code == 200


def test_no_longer_orderable_preview_is_stale_and_does_not_create_draft(deployment):
    client, settings, _ = deployment
    old_hash = preview_bracket(client, settings)
    before = {row["draft_id"] for row in client.get("/api/drafts", headers=auth(settings.buyer_token)).json()}
    with client.app.state.db.transaction() as session:
        item = session.get(InventoryItem, "BRACKET")
        original = item.on_hand
        item.on_hand = 10000
    try:
        result = create_bracket_draft(client, settings, str(uuid4()), old_hash)
        assert result.status_code == 409
        after = {row["draft_id"] for row in client.get("/api/drafts", headers=auth(settings.buyer_token)).json()}
        assert after == before
    finally:
        with client.app.state.db.transaction() as session:
            session.get(InventoryItem, "BRACKET").on_hand = original


@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ"])
@pytest.mark.parametrize("change", ["inventory", "inbound_insert", "quote_insert"])
def test_preview_confirmation_serializes_competing_catalog_changes(deployment, monkeypatch, isolation, change):
    client, _settings, _ = deployment
    database = client.app.state.db
    mysql = database.engine.dialect.name == "mysql"
    if not mysql and isolation == "READ COMMITTED":
        pytest.skip("SQLite does not expose MySQL isolation settings")
    suffix = uuid4().hex.upper()
    sku, supplier_a, supplier_b = f"T{suffix}", f"A{suffix}", f"B{suffix}"
    buyer = Actor(f"buyer-{suffix}", Role.BUYER)
    with database.transaction() as session:
        session.add_all([
            InventoryItem(sku=sku, name="isolated item", on_hand=0, reserved=0, daily_demand=1, safety_stock=0),
            Supplier(supplier_id=supplier_a, name="original"), Supplier(supplier_id=supplier_b, name="alternative"),
        ])
        session.flush()
        session.add(SupplierQuote(sku=sku, supplier_id=supplier_a, unit_price=Decimal("10.00"), lead_days=1, min_order=1))
    workflow = Workflow(database, client.app.state.workflow.erp)
    preview = workflow.preview_recommended_draft(buyer, (sku,), DEMO_DATE, 7)
    assert preview.lines[0].quantity == 7 and preview.total == Decimal("70.00")

    if mysql:
        def apply_session_isolation(connection, *_):
            cursor = connection.cursor()
            try:
                cursor.execute(f"SET SESSION TRANSACTION ISOLATION LEVEL {isolation}")
            finally:
                cursor.close()
        event.listen(database.engine, "checkout", apply_session_isolation)

    entered, release, writer_started = Event(), Event(), Event()
    original_hash = Workflow._preview_hash

    def hold_before_commit(*args):
        value = original_hash(*args)
        entered.set()
        assert release.wait(10)
        return value

    monkeypatch.setattr(Workflow, "_preview_hash", staticmethod(hold_before_commit))

    def confirm():
        return workflow.create_recommended_draft(buyer, str(uuid4()), (sku,), DEMO_DATE, 7, preview.preview_hash)

    def update_catalog():
        with database.transaction() as session:
            writer_started.set()
            if change == "inventory":
                session.get(InventoryItem, sku).on_hand = 2
            elif change == "inbound_insert":
                session.add(InboundShipment(shipment_id=f"I{suffix}", sku=sku,
                                            expected_on=DEMO_DATE + timedelta(days=1), quantity=2))
            else:
                session.add(SupplierQuote(sku=sku, supplier_id=supplier_b, unit_price=Decimal("1.00"),
                                          lead_days=1, min_order=1))
            session.flush()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            confirmation = pool.submit(confirm)
            assert entered.wait(10)
            writer = pool.submit(update_catalog)
            assert writer_started.wait(10)
            try:
                with pytest.raises(FutureTimeout):
                    writer.result(timeout=0.3)
            finally:
                release.set()
            draft = confirmation.result(timeout=10)
            writer.result(timeout=10)
        assert draft.lines == preview.lines and draft.total == preview.total
    finally:
        release.set()
        if mysql:
            event.remove(database.engine, "checkout", apply_session_isolation)
