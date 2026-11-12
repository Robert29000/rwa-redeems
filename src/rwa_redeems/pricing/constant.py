from __future__ import annotations

import math

import pandas as pd

from rwa_redeems.config import ConstantSourceConfig
from rwa_redeems.pricing.base import PriceSource, PricingContext, register


@register("constant")
class ConstantPriceSource(PriceSource):
    def __init__(self, label: str, value: float):
        super().__init__(label)
        self.value = value

    @classmethod
    def from_config(cls, cfg: ConstantSourceConfig, ctx: PricingContext) -> ConstantPriceSource:
        return cls(cfg.label, cfg.value)

    def lookup(self, blocks: pd.Series, timestamps: pd.Series) -> pd.DataFrame:
        return pd.DataFrame({"price": self.value, "price_timestamp": math.nan}, index=blocks.index)
