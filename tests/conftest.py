from __future__ import annotations

import json
from pathlib import Path

import pytest

from rwa_redeems.abi import AbiRegistry, RawLog
from rwa_redeems.config import load_config
from rwa_redeems.events import EventFetcher
from rwa_redeems.pricing import PriceSourceFactory
from rwa_redeems.protocols import AdapterContext

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class FixtureLogSource:
    """LogSource over a fixed list of logs (real logs captured from mainnet)."""

    def __init__(self, logs: list[RawLog]):
        self.logs = logs
        self.calls: list[tuple] = []

    def get_logs(self, chain_id, address, topic0, from_block, to_block, topics=None):
        self.calls.append((chain_id, address, topic0, from_block, to_block))
        return [
            lg
            for lg in self.logs
            if lg.address == address
            and lg.topics[0] == topic0
            and from_block <= lg.block_number <= to_block
            and all(len(lg.topics) > pos and lg.topics[pos] == value for pos, value in (topics or {}).items())
        ]


class FakeRpc:
    def __init__(self, receipts: dict[str, list[dict]] | None = None, calls: dict[tuple[str, str], str] | None = None):
        self.receipts = receipts or {}
        self.calls = calls or {}  # (to, calldata) -> hex return data; anything else reverts
        self.receipt_calls: list[str] = []

    def get_receipt(self, tx_hash):
        self.receipt_calls.append(tx_hash)
        return {"logs": self.receipts[tx_hash]}

    def eth_call(self, to, data, block):
        result = self.calls.get((to, data))
        return bytes.fromhex(result[2:]) if result else None

    def map(self, fn, items):
        return [fn(x) for x in items]


@pytest.fixture(scope="session")
def config():
    return load_config(ROOT / "config")


@pytest.fixture(scope="session")
def abis():
    return AbiRegistry(ROOT / "abis")


@pytest.fixture(scope="session")
def fixture_logs() -> list[RawLog]:
    return [RawLog.from_json(json.loads(line)) for line in (FIXTURES / "logs.jsonl").read_text().splitlines()]


@pytest.fixture(scope="session")
def fixture_receipts() -> dict:
    return json.loads((FIXTURES / "receipts.json").read_text())


@pytest.fixture
def make_ctx(config, abis, tmp_path, fixture_receipts):
    def make(logs: list[RawLog], to_block: int = 27_000_000, calls: dict | None = None) -> AdapterContext:
        return AdapterContext(
            chain_key="ethereum",
            chain=config.chains["ethereum"],
            to_block=to_block,
            fetcher=EventFetcher(FixtureLogSource(logs), abis),
            rpc=FakeRpc(fixture_receipts, calls),  # type: ignore[arg-type]
            prices=PriceSourceFactory({}),
            cache_dir=tmp_path,
        )

    return make


def token(config, symbol):
    return next(t for t in config.tokens if t.symbol == symbol)
