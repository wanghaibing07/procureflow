import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from secrets import compare_digest
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from procureflow.contracts import PurchaseLine, Value


class PurchaseRequest(Value):
    lines: tuple[PurchaseLine, ...] = Field(min_length=1, max_length=100)


class Receipt(Value):
    erp_reference: str


class MockERPStore:
    def __init__(self, database_url: str) -> None:
        invalid_url = "Mock ERP requires a dedicated file-backed SQLite URL."
        try:
            url = make_url(database_url)
        except (ArgumentError, ValueError):
            raise ValueError(invalid_url) from None
        if (
            url.drivername != "sqlite" or url.database in (None, "", ":memory:")
            or url.username or url.password or url.host or url.port or url.query
        ):
            raise ValueError(invalid_url)
        self.path = Path(url.database).resolve()

    def setup(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS erp_receipts ("
                "idempotency_key TEXT PRIMARY KEY, payload TEXT NOT NULL, reference TEXT NOT NULL UNIQUE)"
            )

    def record(self, idempotency_key: str, purchase: PurchaseRequest) -> tuple[Receipt, bool]:
        payload = json.dumps(
            [{**line.model_dump(mode="json"), "unit_price": format(line.unit_price, ".2f")} for line in purchase.lines],
            sort_keys=True,
            separators=(",", ":"),
        )
        with closing(sqlite3.connect(self.path, timeout=2.0)) as connection, connection:
            inserted = connection.execute(
                "INSERT INTO erp_receipts (idempotency_key, payload, reference) VALUES (?, ?, ?) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (idempotency_key, payload, f"MOCK-{uuid4().hex.upper()}"),
            ).rowcount == 1
            original = connection.execute(
                "SELECT payload, reference FROM erp_receipts WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if original is None:
                raise sqlite3.DatabaseError("No receipt after insert.")
            original_payload, reference = original
            if payload != original_payload:
                raise HTTPException(409, "The idempotency key is already bound to a different purchase.")
            return Receipt(erp_reference=reference), inserted


def create_app(*, token: str | None = None, database_url: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configured_token = token if token is not None else os.getenv("PROCUREFLOW_ERP_TOKEN", "")
        if not re.fullmatch(r"[A-Za-z0-9_.~+/\-]+=*", configured_token):
            raise RuntimeError("Set PROCUREFLOW_ERP_TOKEN to a valid bearer token before starting Mock ERP.")
        application.state.token = configured_token.encode("utf-8")
        configured_db = database_url if database_url is not None else os.getenv(
            "PROCUREFLOW_ERP_DATABASE_URL", "sqlite:///mock-erp.db"
        )
        store = MockERPStore(configured_db)
        try:
            store.setup()
        except sqlite3.Error:
            raise RuntimeError("Could not initialize Mock ERP storage.") from None
        application.state.store = store
        yield

    application = FastAPI(title="ProcureFlow Mock ERP", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def require_token(
        request: Request, supplied: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
    ) -> None:
        if supplied is None or not compare_digest(supplied.credentials.encode("utf-8"), request.app.state.token):
            raise HTTPException(401, "ERP authentication required.", headers={"WWW-Authenticate": "Bearer"})

    @application.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "Invalid purchase request."})

    @application.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/purchase-requests", status_code=201, dependencies=[Depends(require_token)])
    def submit_purchase(
        purchase: PurchaseRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str, Header(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")],
    ) -> Receipt:
        try:
            receipt, inserted = request.app.state.store.record(idempotency_key, purchase)
        except sqlite3.Error:
            raise HTTPException(503, "ERP storage is temporarily unavailable.") from None
        if not inserted:
            response.status_code = 200
        return receipt

    return application


app = create_app()
