"""Typed model of `config/chains.yaml` and `config/tokens.yaml`.

Everything address- or token-specific lives in YAML; code only knows *roles* (e.g. a Midas token has `vaults`,
a Superstate token has `token` and `redemption` contracts).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _normalize_address(value: str) -> str:
    if not _ADDRESS_RE.match(value):
        raise ValueError(f"not an EVM address: {value!r}")
    return value.lower()


Address = Annotated[str, AfterValidator(_normalize_address)]


def expand_env(value: str) -> str:
    """Replace `${VAR}` with the environment value; fail loudly if the variable is not set."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in os.environ:
            raise RuntimeError(f"Config references ${{{name}}} but the env var is not set (see .env.example)")
        return os.environ[name]

    return _ENV_RE.sub(repl, value)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- price sources


class RpcCallSourceConfig(_Model):
    """Call a view function (Chainlink-style `latestRoundData` by default) on-chain at the event block."""

    type: Literal["rpc_call"]
    name: str | None = None
    address: Address
    abi: str = "common/ChainlinkAggregator"
    fn: str = "latestRoundData"
    output: str | None = None  # output field holding the price; default "answer", else the first output
    timestamp_output: str | None = None  # output field holding the update time; default "updatedAt" if present
    timestamp_fn: str | None = None  # second view on the same contract returning the update time (priceLastUpdated)
    decimals: int
    from_block: int | None = None  # skip calls before this block (feed not deployed yet)

    @property
    def label(self) -> str:
        return self.name or f"{self.fn}@{self.address}"


class HttpNavSourceConfig(_Model):
    """Daily NAV series from an HTTP JSON API; joined as-of on the event's UTC date."""

    type: Literal["http_nav"]
    name: str | None = None
    url: str
    records_path: str | None = None  # key holding the list of records if the body is not a list
    date_field: str
    date_format: str
    price_field: str

    @property
    def label(self) -> str:
        return self.name or f"http_nav@{self.url}"


class ConstantSourceConfig(_Model):
    type: Literal["constant"]
    name: str | None = None
    value: float

    @property
    def label(self) -> str:
        return self.name or f"constant:{self.value}"


PriceSourceConfig = Annotated[
    RpcCallSourceConfig | HttpNavSourceConfig | ConstantSourceConfig, Field(discriminator="type")
]


# ---------------------------------------------------------------- chains / tokens


class AssetConfig(_Model):
    """A known ERC-20 on a chain (used to label/price redemption payouts such as USDC)."""

    symbol: str
    decimals: int
    usd_price: float | None = None  # fixed USD price (stablecoins); None = unknown


class ChainConfig(_Model):
    chain_id: int
    rpc_url: str  # may contain ${ENV_VAR}
    confirmations: int = 12  # only collect up to head - confirmations (reorg safety + stable cache)
    assets: dict[Address, AssetConfig] = Field(default_factory=dict)

    def resolved_rpc_url(self) -> str:
        return expand_env(self.rpc_url)


class ContractConfig(_Model):
    name: str | None = None
    address: Address | None = None  # None → the token's own address
    abi: str
    from_block: int | None = None
    price_sources: list[PriceSourceConfig] | None = None  # overrides the token's price sources for rows of this contract
    params: dict[str, Any] = Field(default_factory=dict)  # protocol-specific extras


class TokenConfig(_Model):
    symbol: str
    protocol: str
    chain: str
    address: Address
    decimals: int
    start_block: int  # first block to collect (deploy block, or later to skip legacy versions)
    live_from_block: int | None = None  # rows before it are flagged is_pre_launch (kept)
    contracts: dict[str, list[ContractConfig]] = Field(default_factory=dict)
    price_sources: list[PriceSourceConfig]

    @field_validator("contracts", mode="before")
    @classmethod
    def _single_to_list(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {role: (c if isinstance(c, list) else [c]) for role, c in v.items()}
        return v

    def contracts_for(self, role: str) -> list[ContractConfig]:
        """Contracts configured under `role`, with the address defaulted to the token address."""
        return [
            c if c.address else c.model_copy(update={"address": self.address})
            for c in self.contracts.get(role, [])
        ]

    def require_contract(self, role: str) -> ContractConfig:
        contracts = self.contracts_for(role)
        if len(contracts) != 1:
            raise ValueError(f"{self.symbol}: expected exactly one contract under '{role}', got {len(contracts)}")
        return contracts[0]

    def from_block(self, contract: ContractConfig) -> int:
        return max(self.start_block, contract.from_block or 0)

    def price_sources_for(self, contract_address: str) -> list[PriceSourceConfig]:
        for contracts in self.contracts.values():
            for c in contracts:
                if (c.address or self.address) == contract_address and c.price_sources:
                    return c.price_sources
        return self.price_sources


class AppConfig(_Model):
    chains: dict[str, ChainConfig]
    tokens: list[TokenConfig]

    @model_validator(mode="after")
    def _check(self) -> AppConfig:
        seen: set[tuple[str, str]] = set()
        for t in self.tokens:
            if t.chain not in self.chains:
                raise ValueError(f"token {t.symbol}: unknown chain {t.chain!r}")
            key = (t.chain, t.symbol)
            if key in seen:
                raise ValueError(f"duplicate token {t.symbol} on {t.chain}")
            seen.add(key)
        return self

    def select(self, symbols: list[str] | None) -> list[TokenConfig]:
        if not symbols:
            return list(self.tokens)
        wanted = {s.lower() for s in symbols}
        picked = [t for t in self.tokens if t.symbol.lower() in wanted]
        missing = wanted - {t.symbol.lower() for t in picked}
        if missing:
            raise ValueError(f"unknown token(s): {sorted(missing)}")
        return picked


def load_config(config_dir: Path) -> AppConfig:
    chains = yaml.safe_load((config_dir / "chains.yaml").read_text())
    tokens = yaml.safe_load((config_dir / "tokens.yaml").read_text())
    return AppConfig(chains=chains["chains"], tokens=tokens["tokens"])
