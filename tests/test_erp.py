from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from decimal import Decimal
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from procureflow.contracts import PurchaseLine
from procureflow.erp import HTTPERPClient
from procureflow.mock_erp import create_app
from procureflow.workflow import ERPUnavailable


TOKEN = "test-erp-token"
KEY = "draft-001"
LINE = PurchaseLine(sku="LAB-100", supplier_id="SUP-001", quantity=12, unit_price=Decimal("2.50"), due_date=date(2026, 1, 8))
AUTH = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": KEY}


def test_mock_fails_to_start_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("PROCUREFLOW_ERP_TOKEN", raising=False)
    db = tmp_path / "erp.db"
    with pytest.raises(RuntimeError, match="Set PROCUREFLOW_ERP_TOKEN"), TestClient(create_app(database_url=f"sqlite:///{db}")):
        pass
    assert not db.exists()


@pytest.mark.parametrize("supplied", [None, "wrong-token"])
def test_submit_requires_correct_bearer(tmp_path, supplied):
    db = tmp_path / "erp.db"
    with TestClient(create_app(token=TOKEN, database_url=f"sqlite:///{db}")) as client:
        headers = {"Idempotency-Key": KEY}
        if supplied:
            headers["Authorization"] = f"Bearer {supplied}"
        response = client.post("/purchase-requests", json={"lines": [LINE.model_dump(mode="json")]}, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "ERP authentication required."}
        assert response.headers["WWW-Authenticate"] == "Bearer"

    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT count(*) FROM erp_receipts").fetchone() == (0,)


def test_replay_survives_restart_and_rejects_changed_purchase(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'erp.db'}"
    payload = {"lines": [LINE.model_dump(mode="json")]}
    with TestClient(create_app(token=TOKEN, database_url=database_url)) as client:
        first = client.post("/purchase-requests", json=payload, headers=AUTH)
        assert first.status_code == 201

    with TestClient(create_app(token=TOKEN, database_url=database_url)) as client:
        adapter = HTTPERPClient(str(client.base_url), TOKEN, client=client)
        equivalent_line = LINE.model_copy(update={"unit_price": Decimal("2.5")})
        assert adapter.submit(KEY, (equivalent_line,)) == first.json()["erp_reference"]
        assert client.post("/purchase-requests", json=payload, headers=AUTH).status_code == 200
        payload["lines"][0]["quantity"] += 1
        bad = client.post("/purchase-requests", json=payload, headers=AUTH)
        assert bad.status_code == 409
        assert bad.json() == {"detail": "The idempotency key is already bound to a different purchase."}
        assert adapter.submit(KEY, (LINE,)) == first.json()["erp_reference"]


def test_concurrent_replayed_purchase_creates_one_receipt(tmp_path):
    db = tmp_path / "erp.db"
    payload = {"lines": [LINE.model_dump(mode="json")]}
    with TestClient(create_app(token=TOKEN, database_url=f"sqlite:///{db}")) as client:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(client.post, "/purchase-requests", json=payload, headers=AUTH) for _attempt in range(6)]
            responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == [200] * 5 + [201]
    assert len({response.json()["erp_reference"] for response in responses}) == 1


def test_validation_does_not_echo_supplied_data(tmp_path):
    app = create_app(token=TOKEN, database_url=f"sqlite:///{tmp_path / 'erp.db'}")
    with TestClient(app) as client:
        response = client.post("/purchase-requests", json={"lines": [TOKEN]}, headers=AUTH)
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid purchase request."}


def test_storage_failure_gets_generic_error_then_same_request_can_retry(tmp_path):
    db = tmp_path / "erp.db"
    database_url = f"sqlite:///{db}"
    payload = {"lines": [LINE.model_dump(mode="json")]}
    db.mkdir()
    with pytest.raises(RuntimeError, match="Could not initialize Mock ERP storage"), TestClient(
        create_app(token=TOKEN, database_url=database_url)
    ):
        pass
    db.rmdir()

    with TestClient(create_app(token=TOKEN, database_url=database_url)) as client:
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("DROP TABLE erp_receipts")
        response = client.post("/purchase-requests", json=payload, headers=AUTH)
        assert response.status_code == 503
        assert response.json() == {"detail": "ERP storage is temporarily unavailable."}

    with TestClient(create_app(token=TOKEN, database_url=database_url)) as client:
        recovered = client.post("/purchase-requests", json=payload, headers=AUTH)
        assert recovered.status_code == 201
        assert client.post("/purchase-requests", json=payload, headers=AUTH).json() == recovered.json()


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_error_is_safe_and_keeps_draft_key_for_retry(error_type):
    requests = []

    def transport(request):
        requests.append(request)
        assert request.extensions["timeout"]["connect"] == 2.0
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if len(requests) == 1:
            raise error_type(f"Internal data: {TOKEN}", request=request)
        return httpx.Response(200, json={"erp_reference": "REC-01"})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        adapter = HTTPERPClient("https://erp.example.test", TOKEN, client=client)
        with pytest.raises(ERPUnavailable, match=r"^No receipt from ERP\.$"):
            adapter.submit(KEY, (LINE,))
        assert adapter.submit(KEY, (LINE,)) == "REC-01"
    assert {request.headers["Idempotency-Key"] for request in requests} == {KEY}
    assert requests[0].content == requests[1].content


def test_no_redirects_even_if_the_injected_client_follows_by_default():
    addresses = []

    def transport(request):
        addresses.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://other.example.test/secret"})

    with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        adapter = HTTPERPClient("https://erp.example.test", TOKEN, client=client)
        with pytest.raises(ERPUnavailable):
            adapter.submit(KEY, (LINE,))
    assert addresses == ["https://erp.example.test/purchase-requests"]


@pytest.mark.parametrize("raw", ["not json", "[]", '{"erp_reference":""}', '{"erp_reference":"row\\nnew"}'])
def test_invalid_success_receipt_is_unavailable(raw):
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=raw))) as client:
        adapter = HTTPERPClient("http://erp.example.test", TOKEN, client=client)
        with pytest.raises(ERPUnavailable):
            adapter.submit(KEY, (LINE,))
