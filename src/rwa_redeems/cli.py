"""Command line entry point: `uv run rwa-redeems collect [--tokens USTB,mWIN] [--refresh]`."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from rwa_redeems.config import load_config
from rwa_redeems.pipeline import Pipeline
from rwa_redeems.settings import load_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rwa-redeems", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="fetch redemptions, price them and write CSVs")
    collect.add_argument("--tokens", help="comma-separated token symbols (default: all configured)")
    collect.add_argument("--refresh", action="store_true", help="delete cached raw data and refetch everything")
    collect.add_argument("--to-block", type=int, help="last block to collect (default: head - confirmations)")
    collect.add_argument("--output-dir", type=Path, help="default: <project>/output")
    collect.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("tokens", help="list configured tokens")

    args = parser.parse_args(argv)
    settings = load_settings()
    config = load_config(settings.config_dir)

    if args.command == "tokens":
        for t in config.tokens:
            roles = ", ".join(f"{r}={len(c)}" for r, c in t.contracts.items())
            print(f"{t.chain:10} {t.symbol:8} {t.protocol:12} {t.address}  from {t.start_block}  [{roles}]")
        return

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "web3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    pipeline = Pipeline(settings, config, refresh=args.refresh, to_block=args.to_block)
    symbols = [s.strip() for s in args.tokens.split(",")] if args.tokens else None
    result = pipeline.run(symbols)
    paths = Pipeline.write(result, args.output_dir or settings.output_dir)
    for name, path in paths.items():
        print(f"{name:12} {path}")


if __name__ == "__main__":
    main()
