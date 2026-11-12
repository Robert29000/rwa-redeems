"""Runtime settings: secrets from environment variables (optionally a `.env` file); directories fixed to the project."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    etherscan_api_key: str
    config_dir: Path
    abi_dir: Path
    raw_dir: Path  # cache of raw API responses (logs, oracle calls, receipts); git-ignored, safe to delete
    output_dir: Path  # datasets for analysis (CSV + manifest); tracked in git


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    key = os.environ.get("ETHERSCAN_API_KEY")
    if not key:
        raise RuntimeError("Missing env var ETHERSCAN_API_KEY (see .env.example)")
    return Settings(
        etherscan_api_key=key,
        config_dir=PROJECT_ROOT / "config",
        abi_dir=PROJECT_ROOT / "abis",
        raw_dir=PROJECT_ROOT / "data" / "raw",
        output_dir=PROJECT_ROOT / "output",
    )
