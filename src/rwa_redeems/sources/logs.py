"""Log sources: anything that returns all logs for (chain, address, topic0[, indexed topics]) in a block range."""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from rwa_redeems.abi import RawLog
from rwa_redeems.sources.ratelimit import RateLimiter

log = logging.getLogger(__name__)


# Optional filter on indexed event arguments: topic position (1..3) -> 32-byte hex topic value.
TopicFilter = dict[int, str]


class LogSource(Protocol):
    def get_logs(
        self,
        chain_id: int,
        address: str,
        topic0: str,
        from_block: int,
        to_block: int,
        topics: TopicFilter | None = None,
    ) -> list[RawLog]: ...


class EtherscanError(RuntimeError):
    pass


class EtherscanRateLimited(EtherscanError):
    pass


def _hex_int(value: str) -> int:
    value = value.strip()
    if value in ("", "0x"):
        return 0
    return int(value, 16) if value.startswith("0x") else int(value)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (EtherscanRateLimited, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class EtherscanLogSource:
    """Etherscan API V2 `logs/getLogs` with forward-cursor pagination.

    Results are sorted by (block, logIndex) and capped at 1000 per page (page*offset <= 10000), so we walk forward:
    re-query from the last returned block (dedupe on (tx, logIndex)) while pages are full, and page inside a single
    block only if one block alone holds >= 1000 matching logs.
    """

    URL = "https://api.etherscan.io/v2/api"
    PAGE_SIZE = 1000
    MAX_PAGES = 10
    SHORT_PAGE_SLACK = 10  # Etherscan occasionally returns slightly fewer rows than a full page

    def __init__(self, api_key: str, client: httpx.Client, rate_per_sec: float = 2.5):
        self._api_key = api_key
        self._client = client
        self._limiter = RateLimiter(rate_per_sec)

    @retry(
        retry=retry_if_exception(_is_transient),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(8),
        reraise=True,
    )
    def _get(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        self._limiter.wait()
        resp = self._client.get(self.URL, params={**params, "apikey": self._api_key})
        resp.raise_for_status()
        body = resp.json()
        result = body.get("result")
        if body.get("status") == "1" or isinstance(result, list):
            return result or []  # status "0" + [] == "No records found"
        msg = str(result or body.get("message"))
        if "rate limit" in msg.lower():
            raise EtherscanRateLimited(msg)
        raise EtherscanError(f"Etherscan error for {params.get('address')}: {msg}")

    def _page(
        self,
        chain_id: int,
        address: str,
        topic0: str,
        from_block: int,
        to_block: int,
        page: int,
        topics: TopicFilter | None = None,
    ) -> list[RawLog]:
        params: dict[str, Any] = {
            "chainid": chain_id,
            "module": "logs",
            "action": "getLogs",
            "address": address,
            "topic0": topic0,
            "fromBlock": from_block,
            "toBlock": to_block,
            "page": page,
            "offset": self.PAGE_SIZE,
        }
        for pos, value in (topics or {}).items():
            params[f"topic{pos}"] = value
            params[f"topic0_{pos}_opr"] = "and"
        rows = self._get(params)
        return [
            RawLog(
                address=r["address"].lower(),
                topics=tuple(t.lower() for t in r["topics"] if t),
                data=r["data"].lower(),
                block_number=_hex_int(r["blockNumber"]),
                timestamp=_hex_int(r["timeStamp"]),
                tx_hash=r["transactionHash"].lower(),
                log_index=_hex_int(r["logIndex"]),
                tx_index=_hex_int(r["transactionIndex"]),
            )
            for r in rows
        ]

    def get_logs(
        self,
        chain_id: int,
        address: str,
        topic0: str,
        from_block: int,
        to_block: int,
        topics: TopicFilter | None = None,
    ) -> list[RawLog]:
        seen: dict[tuple[str, int], RawLog] = {}
        cursor = from_block
        while cursor <= to_block:
            rows = self._page(chain_id, address, topic0, cursor, to_block, page=1, topics=topics)
            if not rows:
                break
            new = 0
            for r in rows:
                if r.key not in seen:
                    seen[r.key] = r
                    new += 1
            first, last = rows[0].block_number, rows[-1].block_number
            full = len(rows) >= self.PAGE_SIZE - self.SHORT_PAGE_SLACK
            if full and first == last:
                self._drain_block(chain_id, address, topic0, last, seen, topics)
                cursor = last + 1
            elif full and new:
                cursor = last  # last block may be partially returned
            else:
                cursor = last + 1
        log.debug("etherscan %s %s: %d logs", address, topic0[:10], len(seen))
        return sorted(seen.values(), key=lambda r: (r.block_number, r.log_index))

    def _drain_block(
        self,
        chain_id: int,
        address: str,
        topic0: str,
        block: int,
        seen: dict[tuple[str, int], RawLog],
        topics: TopicFilter | None = None,
    ) -> None:
        for page in range(2, self.MAX_PAGES + 1):
            rows = self._page(chain_id, address, topic0, block, block, page=page, topics=topics)
            for r in rows:
                seen.setdefault(r.key, r)
            if len(rows) < self.PAGE_SIZE:
                return
        raise EtherscanError(f"block {block} has more than {self.PAGE_SIZE * self.MAX_PAGES} logs for {address}")
