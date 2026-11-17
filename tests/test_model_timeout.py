from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

import procureflow.api as api_module
from procureflow.agent import OpenAICompatibleModel
from procureflow.settings import Settings


@pytest.mark.parametrize("value", ["", "0", "-1", "181", "1.5", "nan", "invalid"])
def test_model_timeout_rejects_invalid_environment_values(monkeypatch, value):
    monkeypatch.setenv("PROCUREFLOW_MODEL_URL", "https://model.invalid/v1")
    monkeypatch.setenv("PROCUREFLOW_MODEL_API_KEY", "model-token-1234567890")
    monkeypatch.setenv("PROCUREFLOW_MODEL_NAME", "test-model")
    monkeypatch.setenv("PROCUREFLOW_MODEL_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="integer between 1 and 180"):
        Settings.from_env()


def test_bad_model_timeout_does_not_block_manual_business_when_model_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("PROCUREFLOW_DATABASE_URL", f"sqlite:///{(tmp_path / 'manual.sqlite3').as_posix()}")
    buyer = "buyer-token-1234567890"
    monkeypatch.setenv("PROCUREFLOW_BUYER_TOKEN", buyer)
    monkeypatch.setenv("PROCUREFLOW_APPROVER_TOKEN", "approver-token-1234567890")
    monkeypatch.setenv("PROCUREFLOW_ERP_TOKEN", "erp-token-1234567890")
    for key in ("PROCUREFLOW_MODEL_URL", "PROCUREFLOW_MODEL_API_KEY", "PROCUREFLOW_MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PROCUREFLOW_MODEL_TIMEOUT_SECONDS", "not-a-number")
    headers = {"Authorization": f"Bearer {buyer}"}
    with TestClient(api_module.create_app(erp=object())) as client:
        assert client.get("/healthz").json()["model_configured"] is False
        stock = client.get("/api/stock", headers=headers)
        assert stock.status_code == 200 and len(stock.json()) == 3
        preview = client.post("/api/drafts/preview", headers=headers, json={"skus": ["BRACKET"]})
        assert preview.status_code == 200
        draft = client.post("/api/drafts", headers={**headers, "Idempotency-Key": str(uuid4())},
                            json={"skus": ["BRACKET"], "preview_hash": preview.json()["preview_hash"]})
        assert draft.status_code == 200 and draft.json()["total"] == "230.00"


@pytest.mark.parametrize(("environment_value", "expected"), [(None, 20), ("1", 1), ("90", 90), ("180", 180)])
def test_environment_timeout_reaches_model_http_request(monkeypatch, tmp_path, environment_value, expected):
    if environment_value is None:
        monkeypatch.delenv("PROCUREFLOW_MODEL_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("PROCUREFLOW_MODEL_TIMEOUT_SECONDS", environment_value)
    monkeypatch.setenv("PROCUREFLOW_DATABASE_URL", f"sqlite:///{(tmp_path / 'inventory.sqlite3').as_posix()}")
    buyer = "buyer-token-1234567890"
    monkeypatch.setenv("PROCUREFLOW_BUYER_TOKEN", buyer)
    monkeypatch.setenv("PROCUREFLOW_APPROVER_TOKEN", "approver-token-1234567890")
    monkeypatch.setenv("PROCUREFLOW_ERP_TOKEN", "erp-token-1234567890")
    monkeypatch.setenv("PROCUREFLOW_MODEL_URL", "https://model.invalid/v1")
    monkeypatch.setenv("PROCUREFLOW_MODEL_API_KEY", "model-token-1234567890")
    monkeypatch.setenv("PROCUREFLOW_MODEL_NAME", "test-model")

    observed = []

    def reply(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"},
                                                    "finish_reason": "stop"}]})

    transport_client = httpx.Client(transport=httpx.MockTransport(reply))
    original_model = OpenAICompatibleModel

    def model_factory(url, key, model, *, timeout_seconds):
        return original_model(url, key, model, client=transport_client, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(api_module, "OpenAICompatibleModel", model_factory)
    try:
        with TestClient(api_module.create_app(erp=object())) as client:
            response = client.post("/api/agent/run", headers={"Authorization": f"Bearer {buyer}"},
                                   json={"request_id": str(uuid4()), "prompt": "检查库存"})
            assert response.status_code == 200
    finally:
        transport_client.close()
    assert observed == [{"connect": 5.0, "read": float(expected),
                         "write": float(expected), "pool": float(expected)}]


@pytest.mark.parametrize("value", [True, 0, 181, 2.5])
def test_direct_model_rejects_invalid_timeout(value):
    with pytest.raises(ValueError, match="integer between 1 and 180"):
        OpenAICompatibleModel("https://model.invalid/v1", "model-token", "model", timeout_seconds=value)
