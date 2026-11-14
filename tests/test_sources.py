import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from tenacity import wait_none

from rwa_redeems.abi import RawLog
from rwa_redeems.sources.cache import CachedLogSource
from rwa_redeems.sources.logs import EtherscanError, EtherscanLogSource
from rwa_redeems.sources.rpc import RpcClient


def _log(block: int, idx: int) -> dict:
    return {
        "address": "0x" + "a" * 40,
        "topics": ["0x" + "1" * 64],
        "data": "0x",
        "blockNumber": hex(block),
        "timeStamp": hex(1_700_000_000 + block),
        "transactionHash": "0x" + f"{block:032x}{idx:032x}",
        "logIndex": hex(idx) if idx else "0x",  # Etherscan sometimes returns "0x" for index 0
        "transactionIndex": "0x0",
    }


class FakeEtherscan:
    """Mimics getLogs: sorted results, `offset` rows per page, page*offset <= 10000."""

    def __init__(self, logs: list[dict], fail_first: int = 0):
        self.logs = logs
        self.requests: list[dict] = []
        self.fail_first = fail_first

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        self.requests.append(q)
        if self.fail_first:
            self.fail_first -= 1
            return httpx.Response(200, json={"status": "0", "message": "NOTOK", "result": "Max calls per sec rate limit reached (3/sec)"})
        lo, hi, page, offset = int(q["fromBlock"]), int(q["toBlock"]), int(q["page"]), int(q["offset"])
        rows = [r for r in self.logs if lo <= int(r["blockNumber"], 16) <= hi]
        rows = rows[(page - 1) * offset : page * offset]
        if not rows:
            return httpx.Response(200, json={"status": "0", "message": "No records found", "result": []})
        return httpx.Response(200, json={"status": "1", "message": "OK", "result": rows})


class SmallPages(EtherscanLogSource):
    PAGE_SIZE = 5
    SHORT_PAGE_SLACK = 0


def _source(server, cls=SmallPages):
    return cls("key", httpx.Client(transport=httpx.MockTransport(server)), rate_per_sec=1000)


def test_pagination_returns_every_log_once():
    logs = [_log(b, i) for b in range(100, 120) for i in range(3)]  # 60 logs, 3 per block
    logs += [_log(150, i) for i in range(12)]  # one block with more logs than a page
    logs += [_log(b, 0) for b in range(160, 163)]
    server = FakeEtherscan(logs)
    out = _source(server).get_logs(1, "0x" + "a" * 40, "0x" + "1" * 64, 0, 1000)
    assert len(out) == len(logs) == len({r.key for r in out})
    assert [r.block_number for r in out] == sorted(r.block_number for r in out)
    assert out[0].log_index == 0


def test_no_records_and_errors():
    assert _source(FakeEtherscan([])).get_logs(1, "0x" + "a" * 40, "0x" + "1" * 64, 0, 10) == []

    def bad(request):
        return httpx.Response(200, json={"status": "0", "message": "NOTOK", "result": "Invalid API Key (#err2)"})

    with pytest.raises(EtherscanError, match="Invalid API Key"):
        _source(bad).get_logs(1, "0x" + "a" * 40, "0x" + "1" * 64, 0, 10)


def test_rate_limit_is_retried(monkeypatch):
    monkeypatch.setattr(EtherscanLogSource._get.retry, "wait", wait_none())
    server = FakeEtherscan([_log(5, 0)], fail_first=1)
    assert len(_source(server).get_logs(1, "0x" + "a" * 40, "0x" + "1" * 64, 0, 10)) == 1


class CountingSource:
    def __init__(self, logs):
        self.logs, self.calls = logs, []

    def get_logs(self, chain_id, address, topic0, from_block, to_block, topics=None):
        self.calls.append((from_block, to_block))
        return [lg for lg in self.logs if from_block <= lg.block_number <= to_block]


def _raw(block):
    return RawLog("0x" + "a" * 40, ("0x" + "1" * 64,), "0x", block, block, f"0x{block:064x}", 0, 0)


def test_cache_is_incremental(tmp_path):
    inner = CountingSource([_raw(b) for b in (10, 50, 150, 250)])
    cached = CachedLogSource(inner, tmp_path)
    args = (1, "0x" + "a" * 40, "0x" + "1" * 64)
    assert len(cached.get_logs(*args, 0, 100)) == 2
    assert len(cached.get_logs(*args, 0, 200)) == 3
    assert len(cached.get_logs(*args, 0, 200)) == 3
    assert inner.calls == [(0, 100), (101, 200)]
    # requesting earlier history than cached triggers a full refetch
    assert len(CachedLogSource(inner, tmp_path).get_logs(*args, 0, 300)) == 4
    assert inner.calls[-1] == (201, 300)


def test_rpc_revert_and_empty_return_none():
    def handler(request):
        body = json.loads(request.content)
        block = body["params"][1]
        if block == hex(1):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": 3, "message": "execution reverted"}})
        if block == hex(2):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0x"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0x" + "00" * 31 + "07"})

    rpc = RpcClient("http://rpc", httpx.Client(transport=httpx.MockTransport(handler)))
    assert rpc.eth_call("0x" + "a" * 40, "0x", 1) is None
    assert rpc.eth_call("0x" + "a" * 40, "0x", 2) is None
    assert rpc.eth_call("0x" + "a" * 40, "0x", 3)[-1] == 7


def test_topic_filter_is_sent_and_cached_separately(tmp_path):
    server = FakeEtherscan([_log(5, 0)])
    to = "0x" + "0" * 24 + "b" * 40
    assert len(_source(server).get_logs(1, "0x" + "a" * 40, "0x" + "1" * 64, 0, 10, {2: to})) == 1
    assert server.requests[0]["topic2"] == to and server.requests[0]["topic0_2_opr"] == "and"

    inner = CountingSource([_raw(10)])
    cached = CachedLogSource(inner, tmp_path)
    args = (1, "0x" + "a" * 40, "0x" + "1" * 64)
    cached.get_logs(*args, 0, 100)
    cached.get_logs(*args, 0, 100, {2: to})
    assert len(inner.calls) == 2  # the filtered query does not reuse the unfiltered cache file


def test_abi_topic_filter(abis):
    spec = abis.event("superstate/SuperstateToken", "OffchainRedeem")
    addr = "0x" + "c" * 40
    assert spec.topic_filter({"src": addr}) == {2: "0x" + "0" * 24 + "c" * 40}
    with pytest.raises(KeyError):
        spec.topic_filter({"amount": 1})
