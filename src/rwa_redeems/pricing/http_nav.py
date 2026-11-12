"""Daily NAV series from an HTTP JSON API, joined as-of on the event's UTC date (latest NAV date <= event date)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from rwa_redeems.config import HttpNavSourceConfig
from rwa_redeems.pricing.base import PriceSource, PricingContext, register

log = logging.getLogger(__name__)


@register("http_nav")
class HttpNavPriceSource(PriceSource):
    def __init__(self, cfg: HttpNavSourceConfig, http: httpx.Client):
        super().__init__(cfg.label)
        self.cfg = cfg
        self._http = http
        self._series: pd.DataFrame | None = None

    @classmethod
    def from_config(cls, cfg: HttpNavSourceConfig, ctx: PricingContext) -> HttpNavPriceSource:
        return cls(cfg, ctx.http)

    @retry(wait=wait_exponential(min=1, max=20), stop=stop_after_attempt(5), reraise=True)
    def _fetch(self) -> list[dict]:
        resp = self._http.get(self.cfg.url)
        resp.raise_for_status()
        body = resp.json()
        return body[self.cfg.records_path] if self.cfg.records_path else body

    def series(self) -> pd.DataFrame:
        """Sorted frame: nav_date (UTC midnight, datetime64), price (float)."""
        if self._series is None:
            records = self._fetch()
            rows = []
            for r in records:
                value = r.get(self.cfg.price_field)
                if value in (None, ""):
                    continue
                d = datetime.strptime(r[self.cfg.date_field], self.cfg.date_format).replace(tzinfo=timezone.utc)
                rows.append({"nav_date": d, "price": float(value)})
            df = pd.DataFrame(rows, columns=["nav_date", "price"])
            df["nav_date"] = pd.to_datetime(df["nav_date"], utc=True)
            df = df.drop_duplicates("nav_date", keep="last").sort_values("nav_date", ignore_index=True)
            log.info("  %s: %d NAV points (%s .. %s)", self.label, len(df),
                     df.nav_date.min().date() if len(df) else None, df.nav_date.max().date() if len(df) else None)
            self._series = df
        return self._series

    def lookup(self, blocks: pd.Series, timestamps: pd.Series) -> pd.DataFrame:
        series = self.series()
        left = pd.DataFrame(
            {"event_date": pd.to_datetime(timestamps.astype("int64"), unit="s", utc=True).dt.normalize()},
            index=blocks.index,
        )
        left["_order"] = range(len(left))
        merged = pd.merge_asof(
            left.sort_values("event_date"), series, left_on="event_date", right_on="nav_date", direction="backward"
        ).sort_values("_order")
        ts = merged["nav_date"].map(lambda d: d.timestamp() if pd.notna(d) else float("nan"))
        return pd.DataFrame({"price": merged["price"].to_numpy(), "price_timestamp": ts.to_numpy()}, index=blocks.index)

    def observations(self) -> pd.DataFrame:
        if self._series is None:
            return super().observations()
        s = self._series
        return pd.DataFrame(
            {
                "source": self.label,
                "block_number": pd.NA,
                "price": s["price"],
                "price_timestamp": s["nav_date"].map(lambda d: d.timestamp()),
            }
        )
