"""Exercise the deployment verifier against the real API and mock ERP."""

import io
from secrets import token_hex
from urllib.error import HTTPError
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from procureflow.api import create_app
from procureflow.erp import HTTPERPClient
from procureflow.mock_erp import create_app as create_mock_erp
from procureflow.settings import Settings
from scripts import verify_compose


class ClientOpener:
    def __init__(self, client):
        self.client = client

    def open(self, request, timeout):
        parsed = urlsplit(request.full_url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        result = self.client.request(request.get_method(), path, headers=dict(request.header_items()), content=request.data)
        body = io.BytesIO(result.content)
        if result.status_code >= 400:
            raise HTTPError(request.full_url, result.status_code, "error", {}, body)
        body.status = result.status_code
        return body


def test_verifier_uses_real_api_preview_and_mock_erp(tmp_path, monkeypatch):
    settings = Settings(
        f"sqlite:///{tmp_path / 'app.db'}", token_hex(24), token_hex(24),
        "http://testserver", token_hex(24),
    )
    with TestClient(create_mock_erp(token=settings.erp_token, database_url=f"sqlite:///{tmp_path / 'erp.db'}")) as erp_server:
        erp = HTTPERPClient(settings.erp_url, settings.erp_token, client=erp_server)
        with TestClient(create_app(settings, erp=erp)) as app:
            monkeypatch.setattr(verify_compose, "build_opener", lambda *_args: ClientOpener(app))
            verify_compose.verify("http://127.0.0.1:8000", settings.buyer_token, settings.approver_token)
