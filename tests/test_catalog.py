from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from procureflow.catalog import (
    DEMO_DATE,
    InboundShipment,
    InventoryItem,
    Supplier,
    SupplierQuote,
    list_quotes,
    list_stock,
    recommend,
    seed_catalog,
)
from procureflow.db import Database


@pytest.fixture
def database():
    database = Database("sqlite://")
    database.create_schema()
    try:
        yield database
    finally:
        database.close()


def test_seeded_recommendations_show_purchase_and_missed_due_date(database):
    with database.transaction() as session:
        seed_catalog(session)
        accepted, late = recommend(session, DEMO_DATE, 7)
        assert accepted.stock.sku == "BRACKET"
        assert accepted.stock.shortage == 20
        assert accepted.line is not None
        assert accepted.line.supplier_id == "S-FAST"
        assert accepted.line.quantity == 20
        assert accepted.line.amount == Decimal("230.00")
        assert late.stock.sku == "GASKET"
        assert late.stock.shortage == 23
        assert late.line is None
        assert late.reason == "supplier_cannot_meet_due"
        assert next(stock for stock in list_stock(session, DEMO_DATE, 7) if stock.sku == "LABEL").shortage == 0


def test_pick_lowest_purchase_total_even_when_moq_exceeds_shortage(database):
    with database.transaction() as session:
        seed_catalog(session)
        volume_quote = session.get(SupplierQuote, ("BRACKET", "S-VOLUME"))
        volume_quote.unit_price = Decimal("5.00")
        line = recommend(session, DEMO_DATE, 7)[0].line
        assert line is not None
        assert line.quantity == 40
        assert line.amount == Decimal("200.00")


def test_quotes_include_supplier_name_and_are_sorted(database):
    with database.transaction() as session:
        seed_catalog(session)
        quotes = list_quotes(session, "BRACKET")
        assert [(quote.supplier_id, quote.supplier_name, quote.unit_price) for quote in quotes] == [
            ("S-FAST", "准达配件", Decimal("11.50")),
            ("S-VOLUME", "量惠工业", Decimal("8.00")),
        ]
        assert list_quotes(session, "MISSING") == []


def test_date_boundaries_for_inbound_and_supplier_eta(database):
    with database.transaction() as session:
        seed_catalog(session)
        for days, quantity in [(-1, 99), (0, 1), (7, 2), (8, 99)]:
            session.add(
                InboundShipment(
                    shipment_id=f"boundary-{days}",
                    sku="BRACKET",
                    expected_on=DEMO_DATE + timedelta(days=days),
                    quantity=quantity,
                )
            )
        fast_quote = session.get(SupplierQuote, ("BRACKET", "S-FAST"))
        fast_quote.lead_days = 7
        volume_quote = session.get(SupplierQuote, ("BRACKET", "S-VOLUME"))
        volume_quote.lead_days = 8
        accepted = recommend(session, DEMO_DATE, 7)[0]
        assert accepted.stock.inbound_by_due == 18
        assert accepted.stock.forecast_demand == 42
        assert accepted.stock.shortage == 17
        assert accepted.line is not None
        assert accepted.line.supplier_id == "S-FAST"
        assert accepted.line.due_date == DEMO_DATE + timedelta(days=7)


def test_tied_totals_prefer_lead_time_then_supplier_id(database):
    with database.transaction() as session:
        seed_catalog(session)
        fast_quote = session.get(SupplierQuote, ("BRACKET", "S-FAST"))
        fast_quote.unit_price = Decimal("16.00")
        fast_quote.lead_days = 6
        session.add(Supplier(supplier_id="S-AHEAD", name="协作供应商"))
        session.flush()
        earlier_quote = SupplierQuote(
            sku="BRACKET", supplier_id="S-AHEAD", unit_price=Decimal("16.00"), lead_days=7, min_order=1
        )
        session.add(earlier_quote)
        first_line = recommend(session, DEMO_DATE, 7)[0].line
        assert first_line is not None
        assert first_line.supplier_id == "S-FAST"
        earlier_quote.lead_days = 6
        second_line = recommend(session, DEMO_DATE, 7)[0].line
        assert second_line is not None
        assert second_line.supplier_id == "S-AHEAD"


def test_reports_missing_quotes_for_shortage(database):
    with database.transaction() as session:
        session.add(
            InventoryItem(sku="NO-QUOTE", name="没有报价的物料", on_hand=0, reserved=2, daily_demand=0, safety_stock=0)
        )
        only = recommend(session, DEMO_DATE, 1)[0]
        assert only.line is None
        assert only.reason == "no_supplier_quotes"
        assert only.stock.shortage == 2


def test_reports_shortages_that_exceed_purchase_line_capacity(database):
    with database.transaction() as session:
        seed_catalog(session)
        bracket = session.get(InventoryItem, "BRACKET")
        bracket.daily_demand = 1_000_000
        recommendation = recommend(session, DEMO_DATE, 7)[0]
        assert recommendation.stock.shortage > 1_000_000
        assert recommendation.line is None
        assert recommendation.reason == "quantity_exceeds_limit"


def test_reads_do_not_write_and_seed_leaves_transaction_control_to_caller(database):
    with database.sessions() as session:
        seed_catalog(session)
        seed_catalog(session, DEMO_DATE + timedelta(days=1))
        assert session.scalar(select(func.count()).select_from(InventoryItem)) == 3
        assert session.scalar(select(func.count()).select_from(InboundShipment)) == 2
        assert session.get(InboundShipment, "DEMO-IN-1").expected_on == DEMO_DATE + timedelta(days=3)
        assert session.scalar(select(func.count()).select_from(SupplierQuote)) == 3
        list_stock(session, DEMO_DATE, 7)
        list_quotes(session, "BRACKET")
        recommend(session, DEMO_DATE, 7)
        assert not session.new and not session.dirty and not session.deleted
        session.rollback()

    with database.transaction() as session:
        assert list_stock(session, DEMO_DATE, 7) == []
        assert recommend(session, DEMO_DATE, 7) == []


@pytest.mark.parametrize("days", [1, 30])
def test_accepts_horizon_boundaries(database, days):
    with database.transaction() as session:
        assert list_stock(session, DEMO_DATE, days) == []


@pytest.mark.parametrize("days", [0, 31, 7.0, True])
def test_rejects_invalid_horizon(database, days):
    with database.transaction() as session:
        with pytest.raises(ValueError, match="horizon_days"):
            list_stock(session, DEMO_DATE, days)


def test_rejects_unrepresentable_due_date(database):
    with database.transaction() as session:
        with pytest.raises(ValueError, match="due date"):
            list_stock(session, date.max, 1)
