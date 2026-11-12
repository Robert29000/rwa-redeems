import math

import eth_abi
import httpx
import pandas as pd

from rwa_redeems.config import HttpNavSourceConfig, RpcCallSourceConfig
from rwa_redeems.pricing import CompositePriceSource, PriceSource, PricingContext
from rwa_redeems.pricing.http_nav import HttpNavPriceSource
from rwa_redeems.pricing.rpc_call import RpcCallPriceSource


class Stub(PriceSource):
    def __init__(self, label, prices: dict[int, float]):
        super().__init__(label)
        self.prices = prices

    @classmethod
    def from_config(cls, cfg, ctx):
        raise NotImplementedError

    def lookup(self, blocks, timestamps):
        return pd.DataFrame(
            {"price": [self.prices.get(int(b), math.nan) for b in blocks], "price_timestamp": math.nan},
            index=blocks.index,
        )


def test_composite_fallback_and_backfill():
    blocks = pd.Series([1, 2, 3, 4], index=[10, 11, 12, 13])
    ts = blocks * 100
    comp = CompositePriceSource([Stub("oracle", {3: 3.0, 4: 4.0}), Stub("api", {2: 2.0, 3: 99.0})])
    out = comp.lookup(blocks, ts)
    assert list(out["price_usd"]) == [2.0, 2.0, 3.0, 4.0]
    assert list(out["price_source"]) == ["backfill:api", "api", "oracle", "oracle"]
    assert list(out["price_is_backfilled"]) == [True, False, False, False]


class FakeRpc:
    def __init__(self, answers: dict[int, int | None]):
        self.answers, self.calls = answers, []

    def eth_call(self, to, data, block):
        self.calls.append(block)
        a = self.answers.get(block)
        if a is None:
            return None
        return eth_abi.encode(["uint80", "int256", "uint256", "uint256", "uint80"], [1, a, 0, 1_700_000_000, 1])

    def map(self, fn, items):
        return [fn(x) for x in items]


def test_rpc_call_source_caches_and_respects_from_block(abis, tmp_path):
    rpc = FakeRpc({100: 10_500_000, 101: None})
    cfg = RpcCallSourceConfig(type="rpc_call", name="feed", address="0x" + "b" * 40, decimals=6, from_block=100)
    ctx = PricingContext("ethereum", rpc, abis, httpx.Client(), tmp_path)  # type: ignore[arg-type]
    src = RpcCallPriceSource.from_config(cfg, ctx)
    blocks = pd.Series([99, 100, 100, 101])
    out = src.lookup(blocks, blocks)
    assert math.isnan(out["price"][0]) and out["price"][1] == 10.5 and math.isnan(out["price"][3])
    assert out["price_timestamp"][1] == 1_700_000_000
    assert sorted(rpc.calls) == [100, 101]  # deduped, nothing below from_block
    # second instance reads the on-disk cache: no new calls
    RpcCallPriceSource.from_config(cfg, ctx).lookup(blocks, blocks)
    assert sorted(rpc.calls) == [100, 101]


def test_http_nav_as_of_event_date():
    body = [
        {"net_asset_value_date": "01/03/2024", "net_asset_value": "10.00"},
        {"net_asset_value_date": "01/05/2024", "net_asset_value": "10.10"},
        {"net_asset_value_date": "01/04/2024", "net_asset_value": "10.05"},
    ]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    cfg = HttpNavSourceConfig(
        type="http_nav", url="http://nav", date_field="net_asset_value_date",
        date_format="%m/%d/%Y", price_field="net_asset_value",
    )
    src = HttpNavPriceSource(cfg, client)
    ts = pd.Series(
        [pd.Timestamp("2024-01-02 12:00", tz="UTC"), pd.Timestamp("2024-01-04 23:59", tz="UTC"),
         pd.Timestamp("2024-01-09", tz="UTC")]
    ).map(lambda t: int(t.timestamp()))
    out = src.lookup(pd.Series([1, 2, 3]), ts)
    assert math.isnan(out["price"][0])
    assert list(out["price"][1:]) == [10.05, 10.10]
