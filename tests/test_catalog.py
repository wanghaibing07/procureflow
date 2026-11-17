from datetime import date, timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

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
    stock_and_quotes,
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


def test_stock_and_quotes_shares_stock_rules_and_distinguishes_absent_items(database):
    with database.transaction() as session:
        seed_catalog(session)
        stock, quotes = stock_and_quotes(session, "BRACKET", DEMO_DATE, 7)
        assert stock == next(item for item in list_stock(session, DEMO_DATE, 7) if item.sku == "BRACKET")
        assert quotes == list_quotes(session, "BRACKET")
        stock, quotes = stock_and_quotes(session, "LABEL", DEMO_DATE, 7)
        assert stock is not None and stock.shortage == 0 and quotes == []
        assert stock_and_quotes(session, "MISSING", DEMO_DATE, 7) == (None, [])


def test_stock_and_quotes_ignore_previously_loaded_orm_values(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'identity-map.sqlite3').as_posix()}")
    try:
        database.create_schema()
        with database.transaction() as session:
            seed_catalog(session)
        with database.sessions() as reader:
            stale_item = reader.get(InventoryItem, "BRACKET")
            stale_quote = reader.get(SupplierQuote, ("BRACKET", "S-FAST"))
            assert stale_item.on_hand == 25 and stale_quote.unit_price == Decimal("11.50")
            with database.transaction() as writer:
                writer.get(InventoryItem, "BRACKET").on_hand = 5
                writer.get(SupplierQuote, ("BRACKET", "S-FAST")).unit_price = Decimal("20.00")
            stock, quotes = stock_and_quotes(reader, "BRACKET", DEMO_DATE, 7)
            assert stale_item.on_hand == 25 and stale_quote.unit_price == Decimal("11.50")
            assert stock is not None and stock.on_hand == 5 and stock.shortage == 40
            assert quotes[0].unit_price == Decimal("20.00")
    finally:
        database.close()


def test_recommend_ignores_stale_orm_identity_map(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'recommend-identity-map.sqlite3').as_posix()}")
    try:
        database.create_schema()
        with database.transaction() as session:
            seed_catalog(session)
        with database.sessions() as reader:
            stale_item = reader.get(InventoryItem, "BRACKET")
            stale_quote = reader.get(SupplierQuote, ("BRACKET", "S-FAST"))
            with database.transaction() as writer:
                writer.get(InventoryItem, "BRACKET").on_hand = 5
                writer.get(SupplierQuote, ("BRACKET", "S-FAST")).unit_price = Decimal("20.00")
            result = next(item for item in recommend(reader, DEMO_DATE, 7) if item.stock.sku == "BRACKET")
            assert stale_item.on_hand == 25 and stale_quote.unit_price == Decimal("11.50")
            assert result.stock.on_hand == 5 and result.stock.shortage == 40
            assert result.line is not None and result.line.supplier_id == "S-VOLUME"
            assert result.line.quantity == 40 and result.line.amount == Decimal("320.00")
    finally:
        database.close()


@pytest.mark.parametrize("backend", ["sqlite", "mysql-read-committed"])
def test_stock_and_quotes_use_one_snapshot_during_concurrent_commit(tmp_path, backend):
    if backend == "mysql-read-committed":
        url = os.getenv("PROCUREFLOW_TEST_DB_URL")
        if not url:
            pytest.skip("PROCUREFLOW_TEST_DB_URL is not configured")
        parsed = make_url(url)
        if parsed.get_backend_name() != "mysql" or parsed.database != "procureflow_test":
            pytest.fail("PROCUREFLOW_TEST_DB_URL must target a dedicated MySQL database named procureflow_test.")
    else:
        url = f"sqlite:///{(tmp_path / 'quote-snapshot.sqlite3').as_posix()}"
    database = Database(url)
    suffix = uuid4().hex.upper()
    sku, supplier_id, shipment_id = f"I{suffix}", f"S{suffix}", f"T{suffix}"
    writer_go, writer_done = Event(), Event()
    created = False
    try:
        if backend == "sqlite":
            with database.engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        database.create_schema()
        with database.transaction() as session:
            session.add_all([
                InventoryItem(sku=sku, name="isolated", on_hand=5, reserved=0, daily_demand=2, safety_stock=0),
                Supplier(supplier_id=supplier_id, name="supplier"),
            ])
            session.flush()
            session.add_all([
                InboundShipment(shipment_id=shipment_id, sku=sku, expected_on=DEMO_DATE, quantity=2),
                SupplierQuote(sku=sku, supplier_id=supplier_id, unit_price=Decimal("10.00"), lead_days=1, min_order=1),
            ])
        created = True
        observed_queries = []

        def pause_after_select(connection, cursor, statement, parameters, context, executemany):
            if connection.info.get("test_quote_snapshot") and "catalog_quotes" in statement.lower():
                observed_queries.append(statement)
                writer_go.set()
                assert writer_done.wait(10), "Concurrent update did not commit while the result was being read"

        def writer():
            assert writer_go.wait(10), "Reader did not execute the snapshot query"
            try:
                with database.transaction() as session:
                    session.execute(update(InventoryItem).where(InventoryItem.sku == sku).values(on_hand=1))
                    session.execute(update(InboundShipment).where(InboundShipment.shipment_id == shipment_id).values(quantity=4))
                    session.execute(update(SupplierQuote).where(SupplierQuote.sku == sku).values(unit_price=Decimal("20.00")))
            finally:
                writer_done.set()

        event.listen(database.engine, "after_cursor_execute", pause_after_select)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(writer)
                with database.engine.connect() as connection:
                    if backend == "mysql-read-committed":
                        connection = connection.execution_options(isolation_level="READ COMMITTED")
                    connection.info["test_quote_snapshot"] = True
                    try:
                        with Session(bind=connection) as session:
                            stock, quotes = stock_and_quotes(session, sku, DEMO_DATE, 7)
                    finally:
                        connection.info.pop("test_quote_snapshot", None)
                pending.result(timeout=10)
        finally:
            writer_go.set()
            event.remove(database.engine, "after_cursor_execute", pause_after_select)
        assert len(observed_queries) == 1
        assert stock is not None and (stock.on_hand, stock.inbound_by_due, stock.shortage) == (5, 2, 7)
        assert quotes[0].unit_price == Decimal("10.00")
        with database.transaction() as session:
            latest, latest_quotes = stock_and_quotes(session, sku, DEMO_DATE, 7)
            assert latest is not None and (latest.on_hand, latest.inbound_by_due, latest.shortage) == (1, 4, 9)
            assert latest_quotes[0].unit_price == Decimal("20.00")
    finally:
        if created and backend == "mysql-read-committed":
            with database.transaction() as session:
                session.execute(delete(SupplierQuote).where(SupplierQuote.sku == sku))
                session.execute(delete(InboundShipment).where(InboundShipment.sku == sku))
                session.execute(delete(InventoryItem).where(InventoryItem.sku == sku))
                session.execute(delete(Supplier).where(Supplier.supplier_id == supplier_id))
        database.close()


@pytest.mark.parametrize("backend", ["sqlite", "mysql-read-committed"])
def test_recommend_uses_one_snapshot_during_concurrent_commit(tmp_path, backend):
    if backend == "mysql-read-committed":
        url = os.getenv("PROCUREFLOW_TEST_DB_URL")
        if not url:
            pytest.skip("PROCUREFLOW_TEST_DB_URL is not configured")
        parsed = make_url(url)
        if parsed.get_backend_name() != "mysql" or parsed.database != "procureflow_test":
            pytest.fail("PROCUREFLOW_TEST_DB_URL must target a dedicated MySQL database named procureflow_test.")
    else:
        url = f"sqlite:///{(tmp_path / 'recommend-snapshot.sqlite3').as_posix()}"
    database = Database(url)
    suffix = uuid4().hex.upper()
    sku, supplier_id, shipment_id = f"I{suffix}", f"S{suffix}", f"T{suffix}"
    writer_go, writer_done = Event(), Event()
    created = False
    try:
        if backend == "sqlite":
            with database.engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        database.create_schema()
        with database.transaction() as session:
            session.add_all([
                InventoryItem(sku=sku, name="isolated", on_hand=5, reserved=0, daily_demand=2, safety_stock=0),
                Supplier(supplier_id=supplier_id, name="supplier"),
            ])
            session.flush()
            session.add_all([
                InboundShipment(shipment_id=shipment_id, sku=sku, expected_on=DEMO_DATE, quantity=2),
                SupplierQuote(sku=sku, supplier_id=supplier_id, unit_price=Decimal("10.00"), lead_days=1, min_order=1),
            ])
        created = True
        queries = []

        def pause_after_select(connection, cursor, statement, parameters, context, executemany):
            if connection.info.get("test_recommend_snapshot") and statement.lstrip().lower().startswith("select"):
                queries.append(statement)
                if len(queries) == 1:
                    writer_go.set()
                    assert writer_done.wait(10), "Concurrent update did not commit during recommendation"

        def writer():
            assert writer_go.wait(10), "Recommendation did not perform its first read"
            try:
                with database.transaction() as session:
                    session.execute(update(InventoryItem).where(InventoryItem.sku == sku).values(on_hand=1))
                    session.execute(update(InboundShipment).where(InboundShipment.shipment_id == shipment_id).values(quantity=4))
                    session.execute(update(SupplierQuote).where(SupplierQuote.sku == sku).values(unit_price=Decimal("20.00")))
            finally:
                writer_done.set()

        event.listen(database.engine, "after_cursor_execute", pause_after_select)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(writer)
                with database.engine.connect() as connection:
                    if backend == "mysql-read-committed":
                        connection = connection.execution_options(isolation_level="READ COMMITTED")
                    connection.info["test_recommend_snapshot"] = True
                    try:
                        with Session(bind=connection) as session:
                            result = next(item for item in recommend(session, DEMO_DATE, 7) if item.stock.sku == sku)
                    finally:
                        connection.info.pop("test_recommend_snapshot", None)
                pending.result(timeout=10)
        finally:
            writer_go.set()
            event.remove(database.engine, "after_cursor_execute", pause_after_select)
        assert result.line is not None
        assert (result.stock.on_hand, result.stock.inbound_by_due, result.stock.shortage, result.line.unit_price) == (
            5, 2, 7, Decimal("10.00")
        ), "Recommendation combined data from different database states"
        assert len(queries) == 1
        with database.transaction() as session:
            latest = next(item for item in recommend(session, DEMO_DATE, 7) if item.stock.sku == sku)
            assert latest.line is not None
            assert (latest.stock.on_hand, latest.stock.inbound_by_due, latest.stock.shortage, latest.line.unit_price) == (
                1, 4, 9, Decimal("20.00")
            )
    finally:
        if created and backend == "mysql-read-committed":
            with database.transaction() as session:
                session.execute(delete(SupplierQuote).where(SupplierQuote.sku == sku))
                session.execute(delete(InboundShipment).where(InboundShipment.sku == sku))
                session.execute(delete(InventoryItem).where(InventoryItem.sku == sku))
                session.execute(delete(Supplier).where(Supplier.supplier_id == supplier_id))
        database.close()


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
