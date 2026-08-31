"""CLI entry point for Instrument Universe Builder.

Usage:
    python -m astock_api.universe build --source tdx_live --data-dir /app/data/universe
    python -m astock_api.universe build --source snapshot --raw-path /path/to/raw.json
"""

import argparse
import json
import logging
import sys

from astock_api.universe_builder import InstrumentUniverseBuilder


def main():
    parser = argparse.ArgumentParser(
        description="Instrument Universe Builder — R6-1"
    )
    subparsers = parser.add_subparsers(dest="command")

    # build command
    build_parser = subparsers.add_parser("build", help="Build universe artifact")
    build_parser.add_argument(
        "--source", choices=["tdx_live", "snapshot"], default="tdx_live",
        help="Source: tdx_live (fetch from mootdx) or snapshot (from saved file)"
    )
    build_parser.add_argument(
        "--raw-path", default=None,
        help="Path to raw snapshot JSON (required if source=snapshot)"
    )
    build_parser.add_argument(
        "--data-dir", default="/app/data/universe",
        help="Output directory for artifacts"
    )

    args = parser.parse_args()

    if args.command == "build":
        logging.basicConfig(level=logging.INFO)
        builder = InstrumentUniverseBuilder(data_dir=args.data_dir)

        try:
            result = builder.build(source=args.source, raw_path=args.raw_path)
            print(json.dumps(result, indent=2))
        except Exception as e:
            logging.error(f"Build failed: {e}")
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
