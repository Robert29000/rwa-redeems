"""Generic event fetching + decoding shared by all protocol adapters."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import pandas as pd

from rwa_redeems.abi import AbiRegistry
from rwa_redeems.sources.logs import LogSource

log = logging.getLogger(__name__)

META_COLUMNS = ["event", "emitter", "block_number", "block_timestamp", "tx_hash", "log_index"]


class EventFetcher:
    def __init__(self, log_source: LogSource, abis: AbiRegistry):
        self._source = log_source
        self.abis = abis

    def fetch(
        self,
        chain_id: int,
        address: str,
        abi_name: str,
        event_names: Iterable[str],
        from_block: int,
        to_block: int,
    ) -> pd.DataFrame:
        """Decoded logs of the given events emitted by `address`, one row per log.

        Columns: META_COLUMNS + the union of decoded argument names (addresses lowercase, ints unbounded Python ints).
        """
        rows: list[dict] = []
        arg_columns: list[str] = []
        for name in event_names:
            spec = self.abis.event(abi_name, name)
            clash = set(spec.arg_names) & set(META_COLUMNS)
            if clash:
                raise ValueError(f"event {name} argument names clash with meta columns: {clash}")
            arg_columns += [a for a in spec.arg_names if a not in arg_columns]
            logs = self._source.get_logs(chain_id, address, spec.topic0, from_block, to_block)
            log.info("  %s.%s: %d logs", abi_name, name, len(logs))
            for raw in logs:
                rows.append(
                    {
                        "event": name,
                        "emitter": raw.address,
                        "block_number": raw.block_number,
                        "block_timestamp": raw.timestamp,
                        "tx_hash": raw.tx_hash,
                        "log_index": raw.log_index,
                        **spec.decode(raw),
                    }
                )
        df = pd.DataFrame(rows, columns=META_COLUMNS + arg_columns)
        # keep raw uint256 values as exact Python ints
        for col in arg_columns:
            df[col] = df[col].astype(object)
        return df.sort_values(["block_number", "log_index"], ignore_index=True)
