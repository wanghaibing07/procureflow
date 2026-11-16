import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, JSON, String, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .contracts import PurchaseLine, Value
from .db import Base, Database


class Role(StrEnum):
    BUYER = "buyer"
    APPROVER = "approver"


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    role: Role


class DraftStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUBMITTED = "submitted"


class ERPClient(Protocol):
    def submit(self, draft_id: str, lines: tuple[PurchaseLine, ...]) -> str: ...


class ERPUnavailable(Exception):
    pass


class WorkflowError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuditEvent(Value):
    event: str
    actor_id: str
    happened_at: datetime


class DraftView(Value):
    draft_id: str
    status: DraftStatus
    buyer_id: str
    lines: tuple[PurchaseLine, ...]
    total: Decimal
    erp_reference: str | None
    events: tuple[AuditEvent, ...]


class _Draft(Base):
    __tablename__ = "purchase_drafts"

    draft_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    buyer_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(12))
    lines: Mapped[list[dict]] = mapped_column(JSON)
    total: Mapped[str] = mapped_column(String(24))
    erp_reference: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    events: Mapped[list["_DraftEvent"]] = relationship(lazy="selectin", order_by="_DraftEvent.event_id")


class _DraftEvent(Base):
    __tablename__ = "purchase_draft_events"

    event_id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey("purchase_drafts.draft_id"), index=True)
    event: Mapped[str] = mapped_column(String(12))
    actor_id: Mapped[str] = mapped_column(String(128))
    happened_at: Mapped[datetime] = mapped_column(DateTime)


class _DraftIntent(Base):
    __tablename__ = "purchase_draft_intents"

    request_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    draft_id: Mapped[str | None] = mapped_column(ForeignKey("purchase_drafts.draft_id"), unique=True)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def _view(draft: _Draft) -> DraftView:
    return DraftView(
        draft_id=draft.draft_id,
        status=DraftStatus(draft.status),
        buyer_id=draft.buyer_id,
        lines=tuple(PurchaseLine.model_validate(line) for line in draft.lines),
        total=Decimal(draft.total),
        erp_reference=draft.erp_reference,
        events=tuple(
            AuditEvent(event=event.event, actor_id=event.actor_id, happened_at=event.happened_at.replace(tzinfo=UTC))
            for event in draft.events
        ),
    )


class Workflow:
    def __init__(self, database: Database, erp: ERPClient) -> None:
        self.database = database
        self.erp = erp

    @staticmethod
    def _require_role(actor: Actor, required: Role | None = None) -> None:
        if actor.role not in (Role.BUYER, Role.APPROVER) or (required is not None and actor.role != required):
            raise WorkflowError(403, "This action is not available to your role.")
        if not actor.actor_id.strip() or len(actor.actor_id) > 128:
            raise WorkflowError(403, "A valid actor identity is required.")

    def create_draft(self, actor: Actor, key: str, lines: tuple[PurchaseLine, ...]) -> DraftView:
        self._require_role(actor, Role.BUYER)
        if not key.strip() or len(key) > 128:
            raise WorkflowError(400, "An idempotency key of 1 to 128 characters is required.")
        if not 1 <= len(lines) <= 100:
            raise WorkflowError(400, "A draft must contain 1 to 100 purchase lines.")

        request_key = hashlib.sha256(json.dumps([actor.actor_id, key]).encode()).hexdigest()
        line_payload = [
            {**line.model_dump(mode="json"), "unit_price": format(line.unit_price, ".2f")} for line in lines
        ]
        total = sum((line.amount for line in lines), Decimal("0.00"))
        draft_id = str(uuid4())
        created_at = _now()

        try:
            with self.database.transaction() as session:
                draft = _Draft(
                    draft_id=draft_id,
                    request_key=request_key,
                    buyer_id=actor.actor_id,
                    status=DraftStatus.DRAFT,
                    lines=line_payload,
                    total=format(total, ".2f"),
                    created_at=created_at,
                    events=[_DraftEvent(event="created", actor_id=actor.actor_id, happened_at=created_at)],
                )
                session.add(draft)
                session.flush()
                return _view(draft)
        except IntegrityError:
            with self.database.transaction() as session:
                original = session.scalar(select(_Draft).where(_Draft.request_key == request_key))
                if original is None:
                    raise
                if original.buyer_id != actor.actor_id or original.lines != line_payload:
                    raise WorkflowError(409, "This idempotency key has different purchase lines.") from None
                return _view(original)

    def create_recommended_draft(
        self, actor: Actor, key: str, skus: tuple[str, ...], as_of: date, horizon_days: int,
    ) -> DraftView:
        from .catalog import recommend

        self._require_role(actor, Role.BUYER)
        if not key.strip() or len(key) > 128:
            raise WorkflowError(400, "An idempotency key of 1 to 128 characters is required.")
        if not 1 <= len(skus) <= 30 or any(not isinstance(sku, str) or not sku or len(sku) > 40 for sku in skus):
            raise WorkflowError(400, "Supply one or more valid SKU codes.")
        if not isinstance(as_of, date) or type(horizon_days) is not int or not 1 <= horizon_days <= 30:
            raise WorkflowError(400, "A valid planning window is required.")
        selected = sorted(set(skus))
        request_key = hashlib.sha256(json.dumps([actor.actor_id, key]).encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps(
            [selected, as_of.isoformat(), horizon_days], separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()

        def existing(session):
            intent = session.get(_DraftIntent, request_key)
            if intent is None:
                return None
            if intent.fingerprint != fingerprint:
                raise WorkflowError(409, "This idempotency key has a different request.")
            draft = session.get(_Draft, intent.draft_id)
            if draft is None or draft.buyer_id != actor.actor_id:
                raise WorkflowError(409, "The existing draft request could not be resolved.")
            return _view(draft)

        try:
            with self.database.transaction() as session:
                prior = existing(session)
                if prior is not None:
                    return prior
                intent = _DraftIntent(request_key=request_key, fingerprint=fingerprint, draft_id=None)
                session.add(intent)
                session.flush()
                eligible = {
                    item.stock.sku: item.line
                    for item in recommend(session, as_of, horizon_days)
                    if item.line is not None
                }
                if not set(selected) <= eligible.keys():
                    raise WorkflowError(400, "Choose SKUs with a current orderable recommendation.")
                lines = tuple(eligible[sku] for sku in selected)
                created_at = _now()
                draft = _Draft(
                    draft_id=str(uuid4()), request_key=request_key, buyer_id=actor.actor_id,
                    status=DraftStatus.DRAFT,
                    lines=[{**line.model_dump(mode="json"), "unit_price": format(line.unit_price, ".2f")} for line in lines],
                    total=format(sum((line.amount for line in lines), Decimal("0.00")), ".2f"),
                    created_at=created_at,
                    events=[_DraftEvent(event="created", actor_id=actor.actor_id, happened_at=created_at)],
                )
                session.add(draft)
                session.flush()
                intent.draft_id = draft.draft_id
                session.flush()
                return _view(draft)
        except IntegrityError:
            with self.database.transaction() as session:
                prior = existing(session)
                if prior is None:
                    legacy = session.scalar(select(_Draft).where(_Draft.request_key == request_key))
                    if legacy is not None:
                        raise WorkflowError(409, "This key belongs to an earlier draft without a saved request intent.") from None
                    raise
                return prior

    def get_draft(self, actor: Actor, draft_id: str) -> DraftView:
        self._require_role(actor)
        with self.database.transaction() as session:
            draft = session.get(_Draft, draft_id)
            if draft is None or draft.draft_id != draft_id or (actor.role == Role.BUYER and draft.buyer_id != actor.actor_id):
                raise WorkflowError(404, "Draft not found.")
            return _view(draft)

    def find_draft_by_request(self, actor: Actor, key: str) -> DraftView | None:
        """Resolve an already committed draft for the same buyer and request key."""
        self._require_role(actor, Role.BUYER)
        if not key.strip() or len(key) > 128:
            raise WorkflowError(400, "An idempotency key of 1 to 128 characters is required.")
        request_key = hashlib.sha256(json.dumps([actor.actor_id, key]).encode()).hexdigest()
        with self.database.transaction() as session:
            draft = session.scalar(select(_Draft).where(_Draft.request_key == request_key))
            if draft is None:
                return None
            if draft.buyer_id != actor.actor_id:
                raise WorkflowError(409, "The existing draft request could not be resolved.")
            return _view(draft)

    def list_drafts(self, actor: Actor) -> list[DraftView]:
        self._require_role(actor)
        query = select(_Draft).order_by(_Draft.created_at.desc(), _Draft.draft_id)
        if actor.role == Role.BUYER:
            query = query.where(_Draft.buyer_id == actor.actor_id)
        with self.database.transaction() as session:
            return [
                _view(draft)
                for draft in session.scalars(query)
                if actor.role == Role.APPROVER or draft.buyer_id == actor.actor_id
            ]

    def decide(self, actor: Actor, draft_id: str, approve: bool) -> DraftView:
        self._require_role(actor, Role.APPROVER)
        draft = self.get_draft(actor, draft_id)
        if draft.buyer_id == actor.actor_id:
            raise WorkflowError(403, "You cannot approve or reject your own draft.")
        if draft.status != DraftStatus.DRAFT:
            raise WorkflowError(409, "Only a new draft can be approved or rejected.")
        status = DraftStatus.APPROVED if approve else DraftStatus.REJECTED
        if not self._transition(actor, draft_id, DraftStatus.DRAFT, status):
            raise WorkflowError(409, "Another decision already changed this draft.")
        return self.get_draft(actor, draft_id)

    def submit(self, actor: Actor, draft_id: str) -> DraftView:
        self._require_role(actor, Role.APPROVER)
        draft = self.get_draft(actor, draft_id)
        if draft.status == DraftStatus.SUBMITTED:
            return draft
        if draft.status != DraftStatus.APPROVED:
            raise WorkflowError(409, "Only an approved draft can be submitted.")

        try:
            reference = self.erp.submit(draft_id, draft.lines)
            if not isinstance(reference, str) or not reference.strip() or len(reference) > 128:
                raise ERPUnavailable
        except (ERPUnavailable, TimeoutError):
            latest = self.get_draft(actor, draft_id)
            if latest.status == DraftStatus.SUBMITTED:
                return latest
            raise WorkflowError(503, "ERP submission did not return a receipt. Retry the same draft.") from None

        self._transition(actor, draft_id, DraftStatus.APPROVED, DraftStatus.SUBMITTED, reference)
        return self.get_draft(actor, draft_id)

    def _transition(
        self,
        actor: Actor,
        draft_id: str,
        before: DraftStatus,
        after: DraftStatus,
        erp_reference: str | None = None,
    ) -> bool:
        with self.database.transaction() as session:
            result = session.execute(
                update(_Draft)
                .where(_Draft.draft_id == draft_id, _Draft.status == before)
                .values(status=after, erp_reference=erp_reference)
            )
            if result.rowcount != 1:
                return False
            session.add(_DraftEvent(draft_id=draft_id, event=after, actor_id=actor.actor_id, happened_at=_now()))
            return True
