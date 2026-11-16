from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StockRisk(Value):
    sku: str
    name: str
    on_hand: int = Field(ge=0)
    reserved: int = Field(ge=0)
    inbound_by_due: int = Field(ge=0)
    forecast_demand: int = Field(ge=0)
    safety_stock: int = Field(ge=0)
    shortage: int = Field(ge=0)
    due_date: date


class Quote(Value):
    sku: str
    supplier_id: str
    supplier_name: str
    unit_price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    lead_days: int = Field(ge=0)
    min_order: int = Field(ge=1)


class PurchaseLine(Value):
    sku: str = Field(min_length=1, max_length=40)
    supplier_id: str = Field(min_length=1, max_length=40)
    quantity: int = Field(gt=0, le=1_000_000)
    unit_price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    due_date: date

    @property
    def amount(self) -> Decimal:
        return self.unit_price * self.quantity


class Recommendation(Value):
    stock: StockRisk
    line: PurchaseLine | None = None
    reason: str | None = None
