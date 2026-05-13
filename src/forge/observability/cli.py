"""CLI entry point for the Forge Observability plugin."""

import argparse
import asyncio
import contextlib
import logging
import sys


def main() -> int:
    """Entry point for: forge-observability <cmd>"""
    parser = argparse.ArgumentParser(
        prog="forge-observability",
        description="Forge Observability — dlt pipeline worker",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    worker_parser = subparsers.add_parser(
        "worker",
        help="Run Forge observability dlt pipelines",
    )
    worker_parser.add_argument(
        "--once",
        action="store_true",
        help="Run each pipeline once and exit (useful for backfill)",
    )
    worker_parser.add_argument(
        "--skip-dbt",
        action="store_true",
        help="Run source pipelines only — skip dbt silver/gold rebuilds",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if args.command != "worker":
        parser.print_help()
        return 0

    from forge.observability.config import get_settings
    from forge.observability.worker import run_pipelines

    s = get_settings()
    skip_dbt = args.skip_dbt or s.forge_observability_worker_skip_dbt
    # A signal can arrive in the narrow window after run_pipelines removes its
    # signal handlers but before asyncio.run() fully returns. The work has
    # already finished at this point; suppress the stray interrupt.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_pipelines(once=args.once, skip_dbt=skip_dbt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
