"""Minimal JSON-RPC client (archive eth_call at a block, receipts) with retries and bounded concurrency."""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)
T = TypeVar("T")
R = TypeVar("R")

_TRANSIENT_HINTS = ("rate limit", "too many", "timeout", "timed out", "temporarily", "try again", "overloaded")


class RpcError(RuntimeError):
    def __init__(self, code: int | None, message: str, data: Any = None):
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data

    @property
    def is_revert(self) -> bool:
        return self.code == 3 or "revert" in self.message.lower()


class RpcTransientError(RuntimeError):
    pass


class RpcClient:
    def __init__(self, url: str, client: httpx.Client, max_workers: int = 16):
        self._url = url
        self._client = client
        self._ids = itertools.count(1)
        self.max_workers = max_workers

    @retry(
        retry=retry_if_exception_type((RpcTransientError, httpx.TransportError)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=20),
        stop=stop_after_attempt(8),
        reraise=True,
    )
    def request(self, method: str, params: list[Any]) -> Any:
        resp = self._client.post(
            self._url, json={"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RpcTransientError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise RpcTransientError(f"HTTP {resp.status_code}: non-JSON body {resp.text[:200]!r}") from exc
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            message = str(err.get("message", ""))
            if any(h in message.lower() for h in _TRANSIENT_HINTS):
                raise RpcTransientError(message)
            raise RpcError(err.get("code"), message, err.get("data"))
        resp.raise_for_status()
        return body["result"]

    def block_number(self) -> int:
        return int(self.request("eth_blockNumber", []), 16)

    def eth_call(self, to: str, data: str, block: int) -> bytes | None:
        """Returns raw return data, or None if the call reverted or the address had no code at that block."""
        try:
            result = self.request("eth_call", [{"to": to, "data": data}, hex(block)])
        except RpcError as exc:
            if exc.is_revert:
                return None
            raise
        if not result or result == "0x":
            return None
        return bytes.fromhex(result[2:])

    def get_receipt(self, tx_hash: str) -> dict[str, Any]:
        return self.request("eth_getTransactionReceipt", [tx_hash])

    def map(self, fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
        items = list(items)
        if len(items) <= 1:
            return [fn(x) for x in items]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(fn, items))
