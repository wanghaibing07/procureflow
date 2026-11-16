import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from secrets import token_hex
from typing import Literal, Protocol
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, JSON, String, select, text, update
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


class DraftPreview(Value):
    skus: tuple[str, ...]
    as_of: date
    horizon_days: int
    lines: tuple[PurchaseLine, ...]
    total: Decimal
    preview_hash: str


class PreviewCancellation(Value):
    status: Literal["cancelled", "committed"]
    draft: DraftView | None = None


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


class _DraftPreview(Base):
    """A single-use preview capability; only its hash is stored in the database."""
    __tablename__ = "purchase_draft_previews"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    buyer_id: Mapped[str] = mapped_column(String(128), index=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed_request_key: Mapped[str | None] = mapped_column(String(64))


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
        preview_hash: str | None = None,
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
        fingerprint_parts = [selected, as_of.isoformat(), horizon_days]
        if preview_hash is not None:
            fingerprint_parts.append(preview_hash)
        fingerprint = hashlib.sha256(json.dumps(
            fingerprint_parts, separators=(",", ":"), ensure_ascii=False,
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

        # A committed request always wins over current catalog state, including
        # after a lost response. Check before taking locks on mutable catalog rows.
        with self.database.transaction() as session:
            prior = existing(session)
            if prior is not None:
                return prior

        try:
            with self.database.transaction() as session:
                if preview_hash is not None and session.bind.dialect.name == "sqlite":
                    # Reserve the writer before any snapshot read, so the preview
                    # comparison and insertion cannot race another catalog writer.
                    session.execute(text("BEGIN IMMEDIATE"))
                if preview_hash is not None and session.bind.dialect.name == "mysql":
                    # Use InnoDB next-key locking even if the server/session default
                    # is READ COMMITTED. This must precede any transactional query.
                    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                    # Current locking reads, including indexed ranges for new quotes and
                    # shipments, precede any snapshot read. Locks last until commit.
                    from .catalog import InboundShipment, InventoryItem, SupplierQuote
                preview = None
                if preview_hash is not None:
                    token_hash = hashlib.sha256(preview_hash.encode("ascii")).hexdigest()
                    preview = session.scalar(
                        select(_DraftPreview).where(_DraftPreview.token_hash == token_hash).with_for_update()
                    )
                    if preview is None or preview.buyer_id != actor.actor_id:
                        raise WorkflowError(409, "The purchase preview changed. Review and confirm it again.")
                    if preview.consumed_request_key == request_key:
                        prior = existing(session)
                        if prior is not None:
                            return prior
                        raise WorkflowError(409, "The existing draft request could not be resolved.")
                    if preview.expires_at <= _now() or preview.consumed_request_key is not None:
                        raise WorkflowError(409, "The purchase preview changed. Review and confirm it again.")
                if preview_hash is not None and session.bind.dialect.name == "mysql":
                    session.execute(select(InventoryItem).where(InventoryItem.sku.in_(selected)).order_by(InventoryItem.sku).with_for_update()).all()
                    session.execute(select(InboundShipment).where(InboundShipment.sku.in_(selected)).order_by(InboundShipment.sku, InboundShipment.shipment_id).with_for_update()).all()
                    session.execute(select(SupplierQuote).where(SupplierQuote.sku.in_(selected)).order_by(SupplierQuote.sku, SupplierQuote.supplier_id).with_for_update()).all()
                prior = existing(session)
                if prior is not None:
                    return prior
                eligible = {
                    item.stock.sku: item.line
                    for item in recommend(session, as_of, horizon_days)
                    if item.line is not None
                }
                if not set(selected) <= eligible.keys() and preview is None:
                    raise WorkflowError(400, "Choose SKUs with a current orderable recommendation.")
                lines = tuple(eligible[sku] for sku in selected if sku in eligible)
                stale = preview is not None and (
                    len(lines) != len(selected)
                    or preview.snapshot_hash != self._preview_hash(actor, selected, as_of, horizon_days, lines)
                )
                if stale:
                    # Persist invalidation under the same lock. Raising inside
                    # this transaction would roll it back and allow a delayed
                    # request to succeed after the catalog reverts.
                    preview.expires_at = _now()
                else:
                    intent = _DraftIntent(request_key=request_key, fingerprint=fingerprint, draft_id=None)
                    session.add(intent)
                    session.flush()
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
                    if preview is not None:
                        preview.consumed_request_key = request_key
                    session.flush()
                    return _view(draft)
            # The invalidation has committed before the 409 is returned.
            raise WorkflowError(409, "The purchase preview changed. Review and confirm it again.")
        except IntegrityError:
            with self.database.transaction() as session:
                prior = existing(session)
                if prior is None:
                    legacy = session.scalar(select(_Draft).where(_Draft.request_key == request_key))
                    if legacy is not None:
                        raise WorkflowError(409, "This key belongs to an earlier draft without a saved request intent.") from None
                    raise
                return prior

    @staticmethod
    def _preview_hash(actor: Actor, selected: list[str], as_of: date, horizon_days: int,
                      lines: tuple[PurchaseLine, ...]) -> str:
        payload = [actor.actor_id, selected, as_of.isoformat(), horizon_days,
                   [line.model_dump(mode="json") for line in lines]]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()

    def cancel_draft_preview(self, actor: Actor, preview_hash: str) -> PreviewCancellation:
        """Atomically cancel an uncommitted preview, or return its committed draft."""
        self._require_role(actor, Role.BUYER)
        if len(preview_hash) != 64 or any(char not in "0123456789abcdef" for char in preview_hash):
            raise WorkflowError(400, "A valid purchase preview is required.")
        token_hash = hashlib.sha256(preview_hash.encode("ascii")).hexdigest()
        with self.database.transaction() as session:
            if session.bind.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            preview = session.scalar(
                select(_DraftPreview).where(_DraftPreview.token_hash == token_hash).with_for_update()
            )
            if preview is None or preview.buyer_id != actor.actor_id:
                raise WorkflowError(404, "Purchase preview not found.")
            if preview.consumed_request_key is not None:
                intent = session.get(_DraftIntent, preview.consumed_request_key)
                draft = session.get(_Draft, intent.draft_id) if intent is not None and intent.draft_id else None
                if draft is None or draft.buyer_id != actor.actor_id:
                    raise WorkflowError(409, "The existing draft request could not be resolved.")
                return PreviewCancellation(status="committed", draft=_view(draft))
            preview.expires_at = min(preview.expires_at, _now())
            return PreviewCancellation(status="cancelled")

    def preview_recommended_draft(self, actor: Actor, skus: tuple[str, ...], as_of: date,
                                  horizon_days: int) -> DraftPreview:
        from .catalog import recommend

        self._require_role(actor, Role.BUYER)
        if not 1 <= len(skus) <= 30 or len(set(skus)) != len(skus):
            raise WorkflowError(400, "Select between 1 and 30 distinct SKU codes.")
        selected = sorted(skus)
        with self.database.transaction() as session:
            eligible = {item.stock.sku: item.line for item in recommend(session, as_of, horizon_days)
                        if item.line is not None}
            if not set(selected) <= eligible.keys():
                raise WorkflowError(400, "Choose SKUs with a current orderable recommendation.")
            lines = tuple(eligible[sku] for sku in selected)
            token = token_hex(32)
            session.add(_DraftPreview(
                token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
                buyer_id=actor.actor_id,
                snapshot_hash=self._preview_hash(actor, selected, as_of, horizon_days, lines),
                expires_at=_now() + timedelta(minutes=15),
            ))
            return DraftPreview(skus=tuple(selected), as_of=as_of, horizon_days=horizon_days, lines=lines,
                                total=sum((line.amount for line in lines), Decimal("0.00")),
                                preview_hash=token)

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
