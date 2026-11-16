"""Verify the manual purchasing flow of an isolated ProcureFlow demo.

This script writes one synthetic BRACKET draft and submits it to the demo's
mock ERP. It must only be used with an isolated, synthetic deployment.
"""

import argparse
from http.client import HTTPException
import ipaddress
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4


class VerificationError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def loopback_base(value):
    try:
        parsed = urlsplit(value)
        host = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        raise VerificationError("Use an explicit loopback IP address and port.") from None
    if (parsed.scheme != "http" or not host.is_loopback or port is None or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise VerificationError("Use an HTTP loopback IP address and port without path or credentials.")
    return value.rstrip("/")


def demo_tokens(path):
    wanted = {"PROCUREFLOW_BUYER_TOKEN", "PROCUREFLOW_APPROVER_TOKEN"}
    found = {}
    try:
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in wanted:
                if key in found or not re.fullmatch(r"[0-9a-fA-F]{32,}", value):
                    raise VerificationError("The demo environment has invalid or duplicate actor tokens.")
                found[key] = value
    except (OSError, UnicodeError):
        raise VerificationError("Could not read the demo environment file.") from None
    if set(found) != wanted or found["PROCUREFLOW_BUYER_TOKEN"] == found["PROCUREFLOW_APPROVER_TOKEN"]:
        raise VerificationError("The demo environment must contain two distinct actor tokens.")
    return found["PROCUREFLOW_BUYER_TOKEN"], found["PROCUREFLOW_APPROVER_TOKEN"]


def expect(condition, label):
    if not condition:
        raise VerificationError(label)
    print("PASS:", label, flush=True)


def verify(base, buyer, approver):
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(method, path, token=None, body=None, key=None):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if key:
            headers["Idempotency-Key"] = key
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        req = Request(base + path, data=payload, headers=headers, method=method)
        try:
            with opener.open(req, timeout=15) as response:
                status = response.status
                raw = response.read(2_000_001)
        except HTTPError as error:
            status = error.code
            raw = error.read(2_000_001)
            error.close()
        except (OSError, URLError, HTTPException):
            raise VerificationError("A local demo request failed; check the deployment health.") from None
        if len(raw) > 2_000_000:
            raise VerificationError("The demo returned an unexpectedly large response.")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise VerificationError("The demo returned invalid JSON.") from None
        return status, data

    status, health = request("GET", "/healthz")
    expect(status == 200 and health.get("ready") is True, "application is ready")
    for token, role, actor in ((buyer, "buyer", "demo-buyer"), (approver, "approver", "demo-approver")):
        status, identity = request("GET", "/api/me", token)
        expect(status == 200 and identity == {"actor_id": actor, "role": role}, role + " identity")

    status, stock = request("GET", "/api/stock?as_of=2026-01-01&horizon_days=7", buyer)
    stock_by_sku = {item["sku"]: item for item in stock}
    expect(status == 200 and set(stock_by_sku) == {"BRACKET", "GASKET", "LABEL"}
           and [stock_by_sku[x]["shortage"] for x in ("BRACKET", "GASKET", "LABEL")] == [20, 23, 0],
           "synthetic inventory and shortages")
    status, quotes = request("GET", "/api/quotes/BRACKET", buyer)
    quote_by_supplier = {item["supplier_id"]: item for item in quotes}
    expect(status == 200 and set(quote_by_supplier) == {"S-FAST", "S-VOLUME"}
           and str(quote_by_supplier["S-FAST"]["unit_price"]) == "11.50"
           and quote_by_supplier["S-FAST"]["lead_days"] == 4
           and quote_by_supplier["S-FAST"]["min_order"] == 10
           and str(quote_by_supplier["S-VOLUME"]["unit_price"]) == "8.00"
           and quote_by_supplier["S-VOLUME"]["lead_days"] == 6
           and quote_by_supplier["S-VOLUME"]["min_order"] == 40,
           "synthetic supplier quotes")
    status, recommendations = request("GET", "/api/recommendations?as_of=2026-01-01&horizon_days=7", buyer)
    by_sku = {item["stock"]["sku"]: item for item in recommendations}
    line = by_sku["BRACKET"]["line"]
    expect(status == 200 and line["quantity"] == 20 and line["supplier_id"] == "S-FAST"
           and str(line["unit_price"]) == "11.50" and by_sku["GASKET"]["line"] is None,
           "synthetic replenishment recommendation")

    status, previous = request("GET", "/api/drafts", buyer)
    expect(status == 200 and isinstance(previous, list), "existing draft list readable")
    previous_ids = {item["draft_id"] for item in previous}
    key = "compose-" + uuid4().hex
    body = {"skus": ["BRACKET"], "as_of": "2026-01-01", "horizon_days": 7}
    status, draft = request("POST", "/api/drafts", buyer, body, key)
    try:
        draft_id = str(UUID(draft["draft_id"]))
    except (KeyError, TypeError, ValueError):
        raise VerificationError("The demo did not return a valid draft identifier.") from None
    expect(status == 200 and draft["status"] == "draft" and str(draft["total"]) == "230.00"
           and len(draft["lines"]) == 1 and draft["lines"][0]["sku"] == "BRACKET"
           and draft["lines"][0]["quantity"] == 20 and draft["lines"][0]["supplier_id"] == "S-FAST",
           "draft created with deterministic total")
    status, replay = request("POST", "/api/drafts", buyer, body, key)
    expect(status == 200 and replay["draft_id"] == draft_id, "same draft returned on retry")
    status, current = request("GET", "/api/drafts", buyer)
    expect(status == 200 and {item["draft_id"] for item in current} - previous_ids == {draft_id},
           "only one new draft persisted")
    path = "/api/drafts/" + draft_id
    status, _ = request("POST", path + "/submit", approver)
    expect(status == 409, "unapproved submission rejected")
    status, _ = request("POST", path + "/decision", buyer, {"approve": True})
    expect(status == 403, "buyer cannot approve")
    status, approved = request("POST", path + "/decision", approver, {"approve": True})
    expect(status == 200 and approved["status"] == "approved", "approver can approve")
    status, _ = request("POST", path + "/submit", buyer)
    expect(status == 403, "buyer cannot submit")
    status, submitted = request("POST", path + "/submit", approver)
    receipt = submitted.get("erp_reference")
    expect(status == 200 and submitted["status"] == "submitted" and isinstance(receipt, str) and bool(receipt)
           and [item["event"] for item in submitted["events"]] == ["created", "approved", "submitted"],
           "mock ERP receipt and audit trail")
    status, replay = request("POST", path + "/submit", approver)
    expect(status == 200 and replay.get("erp_reference") == receipt
           and [item["event"] for item in replay["events"]] == ["created", "approved", "submitted"],
           "ERP submission retry returns the original receipt")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="HTTP URL with an explicit loopback IP and port")
    parser.add_argument("--env-file", required=True, type=Path, help="Local demo .env containing the two actor tokens")
    parser.add_argument("--synthetic-demo", action="store_true", required=True,
                        help="Confirm the endpoint is an isolated synthetic demo; one draft will be submitted")
    args = parser.parse_args()
    try:
        base = loopback_base(args.base_url)
        buyer, approver = demo_tokens(args.env_file)
        verify(base, buyer, approver)
    except VerificationError as error:
        print("Verification failed:", error, file=sys.stderr)
        return 1
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        print("Verification failed; inspect the isolated demo and rerun with a fresh request.", file=sys.stderr)
        return 1
    print("PASS: isolated synthetic manual purchasing flow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
