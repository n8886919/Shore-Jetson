from __future__ import annotations

import argparse
import json
import os
import sys

from shore_tracker.config import ConfigError, load_config
from shore_tracker.doctor import run_doctor
from shore_tracker.runtime import ShoreTrackerRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shore-tracker")
    parser.add_argument(
        "--config",
        default="/etc/shore-tracker/config.toml",
        help="TOML configuration path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="run the local dashboard and controller")
    subparsers.add_parser(
        "doctor", help="print secret-free static prerequisite checks"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "doctor":
        result = run_doctor(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    # Keep locally recorded MKV files private even outside systemd.
    os.umask(0o077)
    runtime = ShoreTrackerRuntime(config)
    runtime.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
