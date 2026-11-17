from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Numeric, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .contracts import PurchaseLine, Quote, Recommendation, StockRisk
from .db import Base


DEMO_DATE = date(2026, 1, 1)
MAX_PURCHASE_QUANTITY = 1_000_000


class InventoryItem(Base):
    __tablename__ = "catalog_inventory"
    __table_args__ = (
        CheckConstraint("on_hand >= 0 AND reserved >= 0 AND daily_demand >= 0 AND safety_stock >= 0"),
    )

    sku: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    on_hand: Mapped[int]
    reserved: Mapped[int]
    daily_demand: Mapped[int]
    safety_stock: Mapped[int]


class InboundShipment(Base):
    __tablename__ = "catalog_inbound"
    __table_args__ = (CheckConstraint("quantity > 0"),)

    shipment_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sku: Mapped[str] = mapped_column(ForeignKey("catalog_inventory.sku"), index=True)
    expected_on: Mapped[date] = mapped_column(Date)
    quantity: Mapped[int]


class Supplier(Base):
    __tablename__ = "catalog_supplier"

    supplier_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))


class SupplierQuote(Base):
    __tablename__ = "catalog_quotes"
    __table_args__ = (CheckConstraint("unit_price > 0 AND lead_days >= 0 AND min_order BETWEEN 1 AND 1000000"),)

    sku: Mapped[str] = mapped_column(ForeignKey("catalog_inventory.sku"), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("catalog_supplier.supplier_id"), primary_key=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    lead_days: Mapped[int]
    min_order: Mapped[int]


def seed_catalog(session: Session, as_of: date = DEMO_DATE) -> None:
    items = [
        InventoryItem(sku="BRACKET", name="安装支架", on_hand=25, reserved=8, daily_demand=6, safety_stock=10),
        InventoryItem(sku="GASKET", name="密封垫", on_hand=5, reserved=2, daily_demand=3, safety_stock=5),
        InventoryItem(sku="LABEL", name="包装标签", on_hand=120, reserved=20, daily_demand=4, safety_stock=20),
    ]
    suppliers = [
        Supplier(supplier_id="S-FAST", name="准达配件"),
        Supplier(supplier_id="S-VOLUME", name="量惠工业"),
        Supplier(supplier_id="S-SLOW", name="远程物料"),
    ]
    for item in items:
        if session.get(InventoryItem, item.sku) is None:
            session.add(item)
    for supplier in suppliers:
        if session.get(Supplier, supplier.supplier_id) is None:
            session.add(supplier)
    session.flush()

    shipments = [
        InboundShipment(shipment_id="DEMO-IN-1", sku="BRACKET", expected_on=as_of + timedelta(days=3), quantity=15),
        InboundShipment(shipment_id="DEMO-IN-2", sku="BRACKET", expected_on=as_of + timedelta(days=10), quantity=40),
    ]
    quotes = [
        SupplierQuote(sku="BRACKET", supplier_id="S-FAST", unit_price=Decimal("11.50"), lead_days=4, min_order=10),
        SupplierQuote(sku="BRACKET", supplier_id="S-VOLUME", unit_price=Decimal("8.00"), lead_days=6, min_order=40),
        SupplierQuote(sku="GASKET", supplier_id="S-SLOW", unit_price=Decimal("3.20"), lead_days=12, min_order=15),
    ]
    for shipment in shipments:
        if session.get(InboundShipment, shipment.shipment_id) is None:
            session.add(shipment)
    for quote in quotes:
        if session.get(SupplierQuote, (quote.sku, quote.supplier_id)) is None:
            session.add(quote)
    session.flush()


def _due_date(as_of: date, horizon_days: int) -> date:
    if type(horizon_days) is not int or not 1 <= horizon_days <= 30:
        raise ValueError("horizon_days must be an integer between 1 and 30")

    try:
        return as_of + timedelta(days=horizon_days)
    except OverflowError as error:
        raise ValueError("as_of and horizon_days must produce a representable due date") from error


def _on_time_shipments(as_of: date, due_date: date):
    return (
        select(InboundShipment.sku, func.sum(InboundShipment.quantity).label("quantity"))
        .where(InboundShipment.expected_on.between(as_of, due_date))
        .group_by(InboundShipment.sku)
        .subquery()
    )


def _raw_shortage(item, inbound_by_due, horizon_days: int):
    """Share the shortage expression between Python values and SQL columns."""
    return item.reserved + item.daily_demand * horizon_days + item.safety_stock - item.on_hand - inbound_by_due


def _stock_risk(item, inbound_by_due: int, horizon_days: int, due_date: date) -> StockRisk:
    forecast_demand = item.daily_demand * horizon_days
    inbound_quantity = int(inbound_by_due)
    shortage = max(0, _raw_shortage(item, inbound_quantity, horizon_days))
    return StockRisk(
        sku=item.sku, name=item.name, on_hand=item.on_hand, reserved=item.reserved,
        inbound_by_due=inbound_quantity, forecast_demand=forecast_demand,
        safety_stock=item.safety_stock, shortage=shortage, due_date=due_date,
    )


def list_stock(session: Session, as_of: date, horizon_days: int) -> list[StockRisk]:
    due_date = _due_date(as_of, horizon_days)
    on_time_shipments = _on_time_shipments(as_of, due_date)
    statement = (
        select(InventoryItem, func.coalesce(on_time_shipments.c.quantity, 0))
        .outerjoin(on_time_shipments, on_time_shipments.c.sku == InventoryItem.sku)
        .order_by(InventoryItem.sku)
    )
    result = []
    for item, inbound_by_due in session.execute(statement):
        result.append(_stock_risk(item, inbound_by_due, horizon_days, due_date))
    return result


def _read_quotes(session: Session, skus: list[str]) -> list[Quote]:
    statement = (
        select(SupplierQuote, Supplier.name)
        .join(Supplier, Supplier.supplier_id == SupplierQuote.supplier_id)
        .where(SupplierQuote.sku.in_(skus))
        .order_by(SupplierQuote.sku, SupplierQuote.supplier_id)
    )
    return [
        Quote(
            sku=quote.sku,
            supplier_id=quote.supplier_id,
            supplier_name=supplier_name,
            unit_price=quote.unit_price,
            lead_days=quote.lead_days,
            min_order=quote.min_order,
        )
        for quote, supplier_name in session.execute(statement)
    ]


def list_quotes(session: Session, sku: str) -> list[Quote]:
    return _read_quotes(session, [sku])


def _stock_quote_statement(inbound):
    # Scalar columns avoid stale objects already held by the Session identity map.
    return (
        select(
            InventoryItem.sku, InventoryItem.name, InventoryItem.on_hand, InventoryItem.reserved,
            InventoryItem.daily_demand, InventoryItem.safety_stock,
            func.coalesce(inbound.c.quantity, 0).label("inbound_by_due"),
            SupplierQuote.supplier_id.label("quote_supplier_id"), Supplier.name.label("supplier_name"),
            SupplierQuote.unit_price, SupplierQuote.lead_days, SupplierQuote.min_order,
        )
        .select_from(InventoryItem)
        .outerjoin(inbound, inbound.c.sku == InventoryItem.sku)
        .outerjoin(SupplierQuote, SupplierQuote.sku == InventoryItem.sku)
        .outerjoin(Supplier, Supplier.supplier_id == SupplierQuote.supplier_id)
    )


def _quote_from_row(row) -> Quote | None:
    if row.quote_supplier_id is None or row.supplier_name is None:
        return None
    return Quote(sku=row.sku, supplier_id=row.quote_supplier_id, supplier_name=row.supplier_name,
                 unit_price=row.unit_price, lead_days=row.lead_days, min_order=row.min_order)


def stock_and_quotes(session: Session, sku: str, as_of: date, horizon_days: int) -> tuple[StockRisk | None, list[Quote]]:
    """Read a SKU, its in-window inbound and its quotes from one statement snapshot."""
    due_date = _due_date(as_of, horizon_days)
    inbound = _on_time_shipments(as_of, due_date)
    statement = (_stock_quote_statement(inbound)
                 .where(InventoryItem.sku == sku)
                 .order_by(SupplierQuote.supplier_id))
    rows = session.execute(statement).all()
    if not rows:
        return None, []
    stock = _stock_risk(rows[0], rows[0].inbound_by_due, horizon_days, due_date)
    quotes = [quote for row in rows if (quote := _quote_from_row(row)) is not None]
    return stock, quotes


def recommend(session: Session, as_of: date, horizon_days: int) -> list[Recommendation]:
    # One statement keeps the stock, inbound and quotes in the same snapshot,
    # including on SQLite and MySQL READ COMMITTED. Filter out stocked items
    # before joining their quotes, retaining the original deterministic order.
    due_date = _due_date(as_of, horizon_days)
    inbound = _on_time_shipments(as_of, due_date)
    quantity = func.coalesce(inbound.c.quantity, 0)
    statement = (_stock_quote_statement(inbound)
                 .where(_raw_shortage(InventoryItem, quantity, horizon_days) > 0)
                 .order_by(InventoryItem.sku, SupplierQuote.supplier_id))
    risks_by_sku: dict[str, StockRisk] = {}
    quotes_by_sku: dict[str, list[Quote]] = {}
    for row in session.execute(statement):
        if row.sku not in risks_by_sku:
            risks_by_sku[row.sku] = _stock_risk(row, row.inbound_by_due, horizon_days, due_date)
        quote = _quote_from_row(row)
        if quote is not None:
            quotes_by_sku.setdefault(row.sku, []).append(quote)

    recommendations = []
    for stock in risks_by_sku.values():
        quotes = quotes_by_sku.get(stock.sku, [])
        eligible = [quote for quote in quotes if quote.lead_days <= horizon_days]
        if not eligible:
            reason = "supplier_cannot_meet_due" if quotes else "no_supplier_quotes"
            recommendations.append(Recommendation(stock=stock, reason=reason))
            continue
        if stock.shortage > MAX_PURCHASE_QUANTITY:
            recommendations.append(Recommendation(stock=stock, reason="quantity_exceeds_limit"))
            continue

        selected = min(
            eligible,
            key=lambda quote: (
                quote.unit_price * max(stock.shortage, quote.min_order),
                quote.lead_days,
                quote.supplier_id,
            ),
        )
        line = PurchaseLine(
            sku=stock.sku,
            supplier_id=selected.supplier_id,
            quantity=max(stock.shortage, selected.min_order),
            unit_price=selected.unit_price,
            due_date=stock.due_date,
        )
        recommendations.append(Recommendation(stock=stock, line=line))
    return recommendations
