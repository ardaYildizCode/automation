"""Command line entry point: python -m reelforge <command>."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Config, ConfigError
from .pipeline import (
    PipelineError,
    run_check,
    run_graduate,
    run_measure,
    run_promote,
    run_publish,
    run_status,
)
from .state import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reelforge",
        description="Dropbox -> 10 reel variants -> Instagram trial reels -> winner -> ad",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="verify credentials, folders and tooling")
    sub.add_parser("publish", help="render the next Dropbox drop and publish trial reels")

    measure = sub.add_parser("measure", help="pull insights and rank a due batch")
    measure.add_argument(
        "--force", action="store_true", help="measure now, ignoring MEASURE_AFTER_HOURS"
    )

    graduate = sub.add_parser(
        "graduate", help="record that you graduated the winner in the Instagram app"
    )
    graduate.add_argument("batch_id")

    promote = sub.add_parser("promote", help="build a paused ad from the graduated winner")
    promote.add_argument("batch_id")
    promote.add_argument("--budget", type=int, help="daily budget in TRY")

    sub.add_parser("status", help="show recent batches")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = Config.load()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "check":
            return run_check(config)

        store = Store()
        if args.command == "publish":
            return run_publish(config, store)
        if args.command == "measure":
            return run_measure(config, store, force=args.force)
        if args.command == "graduate":
            return run_graduate(config, store, args.batch_id)
        if args.command == "promote":
            return run_promote(config, store, args.batch_id, budget=args.budget)
        if args.command == "status":
            return run_status(config, store)
    except PipelineError as exc:
        print(f"Pipeline error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
