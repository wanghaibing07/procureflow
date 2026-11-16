from concurrent.futures import ThreadPoolExecutor
import os
from secrets import token_hex
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from procureflow.api import create_app
from procureflow.catalog import InventoryItem, SupplierQuote
from procureflow.erp import HTTPERPClient
from procureflow.mock_erp import create_app as create_mock_erp
from procureflow.settings import Settings


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


def create_bracket_draft(client, settings, request_key):
    return client.post(
        "/api/drafts",
        headers=auth(settings.buyer_token, **{"Idempotency-Key": request_key}),
        json={"skus": ["BRACKET"]},
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
    first = create_bracket_draft(client, settings, request_key)
    assert first.status_code == 200, first.text
    draft = first.json()
    assert draft["total"] == "230.00"
    assert create_bracket_draft(client, settings, request_key).json()["draft_id"] == draft["draft_id"]
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
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: create_bracket_draft(client, settings, key), range(4)))
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
    assert client.post("/api/drafts", headers=request_headers, json={"skus": ["GASKET"]}).status_code == 400
    assert client.post("/api/drafts", headers=request_headers, json={"skus": ["BRACKET"], "total": "0.01"}).status_code == 422
    assert client.get("/healthz").json() == {"ready": True, "model_configured": False}
    response = client.post("/api/agent/run", headers=buyer, json={"request_id": str(uuid4()), "prompt": "查缺货"})
    assert response.status_code == 503


def test_draft_retry_uses_original_intent_after_stock_and_quote_change(deployment):
    client, settings, _ = deployment
    key = str(uuid4())
    first = create_bracket_draft(client, settings, key)
    assert first.status_code == 200
    with client.app.state.db.transaction() as session:
        item = session.get(InventoryItem, "BRACKET")
        quote = session.get(SupplierQuote, ("BRACKET", "S-FAST"))
        original_stock, original_price = item.on_hand, quote.unit_price
        item.on_hand, quote.unit_price = 1000, 99
    try:
        retry = create_bracket_draft(client, settings, key)
        assert retry.status_code == 200
        assert retry.json() == first.json()
        changed = client.post(
            "/api/drafts",
            headers=auth(settings.buyer_token, **{"Idempotency-Key": key}),
            json={"skus": ["BRACKET"], "horizon_days": 8},
        )
        assert changed.status_code == 409
        changed_sku = client.post(
            "/api/drafts",
            headers=auth(settings.buyer_token, **{"Idempotency-Key": key}),
            json={"skus": ["GASKET"]},
        )
        assert changed_sku.status_code == 409
    finally:
        with client.app.state.db.transaction() as session:
            session.get(InventoryItem, "BRACKET").on_hand = original_stock
            session.get(SupplierQuote, ("BRACKET", "S-FAST")).unit_price = original_price
