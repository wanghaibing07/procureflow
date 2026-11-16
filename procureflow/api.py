from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, StringConstraints

from .agent import AgentError, AgentResult, AgentService, OpenAICompatibleModel
from .catalog import DEMO_DATE, list_quotes, list_stock, recommend, seed_catalog
from .contracts import Quote, Recommendation, StockRisk, Value
from .db import Database
from .erp import HTTPERPClient
from .settings import Settings
from .workflow import Actor, DraftView, ERPClient, Role, Workflow, WorkflowError

SKU = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9_-]{0,39}$")]
Horizon = Annotated[int, Field(ge=1, le=30)]
HorizonQuery = Annotated[int, Query(ge=1, le=30)]
AsOf = Annotated[date, Field(ge=date(1000, 1, 1), le=date(9999, 12, 1))]
AsOfQuery = Annotated[date, Query(ge=date(1000, 1, 1), le=date(9999, 12, 1))]


class DraftRequest(Value):
    skus: list[SKU] = Field(min_length=1, max_length=30)
    as_of: AsOf = DEMO_DATE
    horizon_days: Horizon = 7


class DecisionRequest(Value):
    approve: bool


class AgentRequest(Value):
    request_id: UUID
    prompt: str = Field(min_length=1, max_length=2_000)
    as_of: AsOf = DEMO_DATE
    horizon_days: Horizon = 7
    allow_draft: bool = False


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    erp: ERPClient | None = None,
    model: OpenAICompatibleModel | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        config = settings or Settings.from_env()
        config.validate()
        db = database or Database(config.database_url)
        erp_client = erp or HTTPERPClient(config.erp_url, config.erp_token)
        model_client = model
        if model_client is None and config.model_url:
            model_client = OpenAICompatibleModel(config.model_url, config.model_api_key, config.model_name)
        try:
            db.create_schema()
            if config.load_demo:
                with db.transaction() as session:
                    seed_catalog(session)
            application.state.settings = config
            application.state.db = db
            application.state.workflow = Workflow(db, erp_client)
            application.state.agent = (
                AgentService(db, application.state.workflow, model_client) if model_client else None
            )
            yield
        finally:
            for client in (erp_client if erp is None else None, model_client if model is None else None):
                close_client = getattr(client, "close", None)
                if callable(close_client):
                    close_client()
            if database is None:
                db.close()

    application = FastAPI(title="ProcureFlow", lifespan=lifespan)

    def actor_from_token(request: Request, authorization: Annotated[str | None, Header()] = None) -> Actor:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() == "bearer" and supplied:
            token = supplied.encode("utf-8")
            config = request.app.state.settings
            if compare_digest(token, config.buyer_token.encode("utf-8")):
                return Actor("demo-buyer", Role.BUYER)
            if compare_digest(token, config.approver_token.encode("utf-8")):
                return Actor("demo-approver", Role.APPROVER)
        raise HTTPException(401, "Valid actor credentials required", headers={"WWW-Authenticate": "Bearer"})

    ActorIdentity = Annotated[Actor, Depends(actor_from_token)]

    async def business_error(_request: Request, error: WorkflowError | AgentError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=error.status_code)

    application.add_exception_handler(WorkflowError, business_error)
    application.add_exception_handler(AgentError, business_error)

    @application.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(Path(__file__).parent / "web" / "index.html")

    @application.get("/healthz")
    def health(request: Request) -> dict[str, bool]:
        return {"ready": True, "model_configured": request.app.state.agent is not None}

    @application.get("/api/me")
    def identify(actor: ActorIdentity) -> dict[str, str]:
        return {"actor_id": actor.actor_id, "role": actor.role.value}

    @application.get("/api/stock")
    def stock(request: Request, actor: ActorIdentity, as_of: AsOfQuery = DEMO_DATE, horizon_days: HorizonQuery = 7) -> list[StockRisk]:
        with request.app.state.db.transaction() as session:
            return list_stock(session, as_of, horizon_days)

    @application.get("/api/quotes/{sku}")
    def quotes(request: Request, sku: SKU, actor: ActorIdentity) -> list[Quote]:
        with request.app.state.db.transaction() as session:
            return list_quotes(session, sku)

    @application.get("/api/recommendations")
    def recommendations(
        request: Request,
        actor: ActorIdentity,
        as_of: AsOfQuery = DEMO_DATE,
        horizon_days: HorizonQuery = 7,
    ) -> list[Recommendation]:
        with request.app.state.db.transaction() as session:
            return recommend(session, as_of, horizon_days)

    @application.post("/api/drafts", response_model=DraftView)
    def create_draft(
        request: Request,
        data: DraftRequest,
        actor: ActorIdentity,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")],
    ) -> DraftView:
        if actor.role != Role.BUYER:
            raise HTTPException(403, "Only a buyer can create a draft")
        if len(set(data.skus)) != len(data.skus):
            raise HTTPException(400, "Duplicate SKUs are not allowed")
        return request.app.state.workflow.create_recommended_draft(
            actor, f"manual:{idempotency_key}", tuple(data.skus), data.as_of, data.horizon_days
        )

    @application.get("/api/drafts")
    def drafts(request: Request, actor: ActorIdentity) -> list[DraftView]:
        return request.app.state.workflow.list_drafts(actor)

    @application.get("/api/drafts/{draft_id}")
    def draft(request: Request, draft_id: UUID, actor: ActorIdentity) -> DraftView:
        return request.app.state.workflow.get_draft(actor, str(draft_id))

    @application.post("/api/drafts/{draft_id}/decision")
    def decide(request: Request, draft_id: UUID, data: DecisionRequest, actor: ActorIdentity) -> DraftView:
        return request.app.state.workflow.decide(actor, str(draft_id), data.approve)

    @application.post("/api/drafts/{draft_id}/submit")
    def submit(request: Request, draft_id: UUID, actor: ActorIdentity) -> DraftView:
        return request.app.state.workflow.submit(actor, str(draft_id))

    @application.post("/api/agent/run")
    def agent_run(request: Request, data: AgentRequest, actor: ActorIdentity) -> AgentResult:
        service = request.app.state.agent
        if service is None:
            raise HTTPException(503, "Model is not configured; manual workflow remains available")
        return service.run(actor, data.prompt, data.as_of, data.horizon_days, data.allow_draft, data.request_id)

    return application


app = create_app()
