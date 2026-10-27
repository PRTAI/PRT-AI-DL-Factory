"""Versioned public pricing and usage-based RMB estimates (never provider invoices)."""

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .common import Base, utcnow


class RateTier(Base):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    max_input: int | None = Field(default=None, gt=0)
    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float | None = Field(default=None, ge=0)
    explicit_cache_read: float | None = Field(default=None, ge=0)
    cache_write: float | None = Field(default=None, ge=0)
    cache_write_long: float | None = Field(default=None, ge=0)


class ModelPrice(Base):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    vendor: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    official_model: str = ""
    aliases: list[str] = Field(default_factory=list, max_length=100)
    currency: Literal["CNY", "USD"] = "CNY"
    discount: float = Field(default=1, ge=0, le=1)
    tiers: list[RateTier] = Field(min_length=1, max_length=20)
    region: str = ""
    source: str = Field(min_length=1, max_length=2000)
    verified_on: str = "2026-10-22"
    valid_until: str = ""
    note: str = ""
    # cn-workday: Beijing Mon-Fri 9-12/14-18 outside statutory holidays is peak; other times use off_peak_factor.
    peak_schedule: Literal["", "cn-workday"] = ""
    off_peak_factor: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def ordered_tiers(self):
        limits = [tier.max_input for tier in self.tiers]
        if any(limit is None for limit in limits[:-1]):
            raise ValueError("无限输入区间只能放在最后")
        finite = [limit for limit in limits if limit is not None]
        if finite != sorted(set(finite)):
            raise ValueError("输入长度区间必须递增且不重复")
        if bool(self.peak_schedule) != (self.off_peak_factor is not None):
            raise ValueError("分时计价需要同时设置时段规则和空闲时段系数")
        return self


class PricingSettings(Base):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    usd_cny: float = Field(default=6.7195, gt=0, le=1000)
    exchange_date: str = "2026-10-22"
    exchange_source: str = "https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do"
    # None loads the bundled catalog; [] intentionally disables all prices.
    prices: list[ModelPrice] | None = Field(default=None, max_length=2000)
    updated_at: datetime = Field(default_factory=utcnow)
    updated_by: str = ""

    @model_validator(mode="after")
    def unique_prices(self):
        seen = set()
        for price in self.prices or []:
            for name in [price.model, *price.aliases]:
                key = (price.vendor, name.lower())
                if key in seen:
                    raise ValueError(f"重复的价格或别名：{price.vendor}/{name}")
                seen.add(key)
        return self


class PriceSnapshot(Base):
    model: str = ""
    vendor: str = ""
    region: str = ""
    cache_mode: Literal["implicit", "explicit", "unknown"] = "unknown"
    price: ModelPrice | None = None
    free: bool = False


class BillingSnapshot(Base):
    usd_cny: float
    exchange_date: str
    exchange_source: str
    captured_at: datetime = Field(default_factory=utcnow)
    historical: bool = False
    agent: PriceSnapshot
    judge: PriceSnapshot


class UsageCharge(Base):
    id: str
    job_id: str
    phase: Literal["agent", "judge"]
    model: str
    vendor: str
    amount_cny: float | None = None
    tokens: dict[str, int] = Field(default_factory=dict)
    requests: int = 0
    warnings: list[str] = Field(default_factory=list)
    snapshot: BillingSnapshot


class BillingSummary(Base):
    input_signature: str = ""
    status: Literal["estimated", "partial", "pending", "not_applicable"] = "pending"
    amount_cny: float | None = None
    agent_cny: float | None = None
    judge_cny: float | None = None
    requests: int = 0
    warnings: list[str] = Field(default_factory=list)
    by_model: list[dict] = Field(default_factory=list)
    snapshot: BillingSnapshot | None = None
    updated_at: datetime | None = None


class BillingArchive(Base):
    id: str  # original job ID; deleting a task must not erase incurred spend
    owner_id: str
    owner_email: str
    project_id: str
    project_name: str
    billing: BillingSummary
    # When the original job finished; archived records otherwise lose their job date.
    job_finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
