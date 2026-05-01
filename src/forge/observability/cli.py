"""CLI entry point for the Forge Observability plugin."""

import argparse
import asyncio
import logging
import sys


def _add_observability_subcommands(parser: argparse.ArgumentParser) -> None:
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
        help="Run source pipelines only — skip dbt silver/gold rebuilds (useful for iterating on dbt models)",
    )
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the forge observability API server",
    )
    serve_parser.add_argument("--port", type=int)
    serve_parser.add_argument("--host", type=str)
    serve_parser.add_argument("--reload", action="store_true")


def _dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "command", None)

    if command == "worker":
        from forge.observability.pipelines.worker import _run_pipelines

        from forge.observability.config import get_settings

        s = get_settings()
        skip_dbt = args.skip_dbt or s.forge_observability_worker_skip_dbt
        asyncio.run(_run_pipelines(once=args.once, skip_dbt=skip_dbt))
        return 0

    if command == "serve":
        import uvicorn

        from forge.observability.config import get_settings

        s = get_settings()
        port = args.port if args.port is not None else s.forge_observability_api_port
        log_level = "debug" if args.verbose else s.forge_observability_api_log_level.lower()
        uvicorn.run(
            "forge.observability.api.app:app",
            host=args.host,
            port=port,
            reload=args.reload,
            log_level=log_level,
        )
        return 0

    return None  # caller should print help


def main() -> int:
    """Entry point for: forge observability <cmd>"""
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge SDLC Orchestrator",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    plugins = parser.add_subparsers(dest="plugin")

    obs_parser = plugins.add_parser("observability", help="Forge Observability plugin")
    _add_observability_subcommands(obs_parser)

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if getattr(args, "plugin", None) != "observability":
        parser.print_help()
        return 0

    result = _dispatch(args)
    if result is None:
        obs_parser.print_help()
        return 0
    return result


if __name__ == "__main__":
    sys.exit(main())
