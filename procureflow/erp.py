import re

import httpx

from procureflow.contracts import PurchaseLine
from procureflow.workflow import ERPUnavailable


class HTTPERPClient:
    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None) -> None:
        invalid_url = "ERP requires an absolute HTTP(S) base URL without credentials, query or fragment."
        try:
            url = httpx.URL(base_url)
            if url.scheme not in ("http", "https") or not url.host or url.userinfo or url.query or url.fragment:
                raise ValueError(invalid_url)
        except httpx.InvalidURL:
            raise ValueError(invalid_url) from None
        if not re.fullmatch(r"[A-Za-z0-9_.~+/\-]+=*", token):
            raise ValueError("ERP requires a valid bearer token.")
        self._endpoint = url.copy_with(path=url.path.rstrip("/") + "/purchase-requests")
        self._authorization = f"Bearer {token}"
        self._client = client if client is not None else httpx.Client(trust_env=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def submit(self, draft_id: str, lines: tuple[PurchaseLine, ...]) -> str:
        try:
            response = self._client.post(
                self._endpoint,
                headers={"Authorization": self._authorization, "Idempotency-Key": draft_id},
                json={"lines": [line.model_dump(mode="json") for line in lines]},
                follow_redirects=False,
                timeout=httpx.Timeout(5.0, connect=2.0),
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise ERPUnavailable("No receipt from ERP.") from None

        reference = payload.get("erp_reference") if isinstance(payload, dict) else None
        if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", reference):
            raise ERPUnavailable("ERP returned an invalid receipt.")
        return reference
