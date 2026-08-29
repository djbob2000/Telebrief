#!/usr/bin/env python3
"""
Telebrief - Automated Telegram Digest Generator

Main entry point for the application.
Starts the scheduler and bot command handler.
"""

import argparse
import asyncio
import logging
import signal
import sys
from contextlib import suppress
from typing import Any

from src.bootstrap import ApplicationInfrastructure, build_infrastructure
from src.bot_commands import BotCommandHandler
from src.config_loader import Config, load_config
from src.mcp_server import build_server
from src.runtime import clear_runtime, install_runtime
from src.scheduler import DigestScheduler
from src.utils import setup_logging


class TelebriefApp:
    """Main application controller."""

    def __init__(self):
        """Initialize the application."""
        self.config: Config | None = None
        self.logger: logging.Logger | None = None
        self.scheduler: DigestScheduler | None = None
        self.bot_handler: BotCommandHandler | None = None
        self.mcp: Any | None = None
        self.mcp_task: asyncio.Task | None = None
        self.infrastructure: ApplicationInfrastructure | None = None
        self.shutdown_event = asyncio.Event()

    async def initialize(self):
        """Load configuration and set up components."""
        try:
            # Load configuration
            print("Loading configuration...")
            self.config = load_config()

            # Set up logging
            self.logger = setup_logging(self.config.log_level)
            self.logger.info("=" * 70)
            self.logger.info("🚀 TELEBRIEF STARTING")
            self.logger.info("=" * 70)

            # Display configuration
            self.logger.info(f"Configured channels: {len(self.config.channels)}")
            for ch in self.config.channels:
                self.logger.info(f"  • {ch.name} ({ch.id})")

            schedule = (
                f"daily {self.config.settings.schedule_time} "
                f"({self.config.settings.lookback_hours}h)"
            )
            self.logger.info(f"Schedule: {schedule} {self.config.settings.timezone}")
            self.logger.info(f"Target user: {self.config.settings.target_user_id}")
            self.logger.info(
                f"AI provider: {self.config.settings.ai_provider}, model: {self.config.settings.ai_model}"
            )

            # Bootstrap domain infrastructure only when the PostgreSQL store is
            # enabled; legacy mode (database.enabled=false) stays DB-free and
            # must not import src.jobs.app or require DATABASE_URL.
            if self.config.database.enabled:
                self.logger.info("Initializing domain database infrastructure...")
                self.infrastructure = await build_infrastructure(self.config.database)
                install_runtime(self.infrastructure)

            # Initialize scheduler
            self.logger.info("Initializing scheduler...")
            self.scheduler = DigestScheduler(self.config, self.logger)

            # Initialize bot command handler
            self.logger.info("Initializing bot command handler...")
            self.bot_handler = BotCommandHandler(self.config, self.logger, self.scheduler)
            self.bot_handler.setup_application()

            # Initialize MCP server (optional)
            if self.config.mcp.enabled:
                self.logger.info("Initializing MCP server...")
                self.mcp = build_server(self.config, self.logger)

            self.logger.info("✅ Initialization complete")
            return True

        except FileNotFoundError as e:
            print(f"❌ Configuration error: {e}")
            print("\nPlease ensure:")
            print("1. config.yaml exists and is properly configured")
            print("2. .env file exists with required API credentials")
            print("\nSee .env.example and SPECIFICATION.md for details.")
            return False

        except ValueError as e:
            print(f"❌ Configuration error: {e}")
            return False

        except Exception as e:
            print(f"❌ Initialization failed: {e}")
            import traceback

            traceback.print_exc()
            return False

    async def run(self):
        """Run the application."""
        if (
            self.config is None
            or self.logger is None
            or self.scheduler is None
            or self.bot_handler is None
        ):
            raise RuntimeError("Cannot run TelebriefApp before initialize() completes successfully")

        # Start scheduler
        self.logger.info("Starting scheduler...")
        self.scheduler.start()

        # Start bot
        self.logger.info("Starting bot command handler...")
        await self.bot_handler.run()

        # Start MCP server in the same event loop, so it shares the Telegram session
        if self.mcp:
            mcp_cfg = self.config.mcp
            self.logger.info(
                f"Starting MCP server on http://{mcp_cfg.host}:{mcp_cfg.port}{mcp_cfg.path}"
            )
            self.mcp_task = asyncio.create_task(
                self.mcp.run_streamable_http_async(
                    host=mcp_cfg.host,
                    port=mcp_cfg.port,
                    streamable_http_path=mcp_cfg.path,
                )
            )

        self.logger.info("=" * 70)
        self.logger.info("✅ TELEBRIEF IS RUNNING")
        self.logger.info("=" * 70)
        self.logger.info("Scheduler: Active")
        self.logger.info(f"Next digest: {self.scheduler.get_next_run_time()}")
        self.logger.info("Bot commands: Active")
        if self.mcp_task:
            mcp_cfg = self.config.mcp
            self.logger.info(f"MCP server: http://{mcp_cfg.host}:{mcp_cfg.port}{mcp_cfg.path}")
        self.logger.info("")
        self.logger.info("Available commands in Telegram:")
        self.logger.info("  /digest - Generate digest instantly")
        self.logger.info("  /cleanup - Delete old digests")
        self.logger.info("  /status - Show status")
        self.logger.info("  /help - Show help")
        self.logger.info("")
        self.logger.info("Press Ctrl+C to stop")
        self.logger.info("=" * 70)

        # Wait for shutdown signal
        await self.shutdown_event.wait()

    async def shutdown(self):
        """Graceful shutdown."""
        if self.logger is not None:
            self.logger.info("=" * 70)
            self.logger.info("🛑 SHUTTING DOWN TELEBRIEF")
            self.logger.info("=" * 70)

        # Stop scheduler
        if self.scheduler:
            if self.logger:
                self.logger.info("Stopping scheduler...")
            self.scheduler.stop()

        # Stop bot
        if self.bot_handler:
            if self.logger:
                self.logger.info("Stopping bot...")
            await self.bot_handler.stop()

        # Stop MCP server
        # ponytail: cancelling the task makes uvicorn log a CancelledError traceback on
        # the way out — cosmetic, right after the line below. Build uvicorn.Server here
        # and flip should_exit instead if that log noise ever matters.
        if self.mcp_task:
            if self.logger:
                self.logger.info("Stopping MCP server...")
            self.mcp_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.mcp_task

        # Tear down domain infrastructure: clear the process runtime first,
        # then close Procrastinate and the pool. A failing component must not
        # prevent the rest of the shutdown (close() already guarantees both
        # resources get their teardown attempt).
        if self.infrastructure is not None:
            logger = self.logger
            if logger is not None:
                logger.info("Stopping domain infrastructure...")
                clear_runtime(self.infrastructure)
                try:
                    await self.infrastructure.close()
                except Exception as e:
                    logger.error(f"Infrastructure shutdown failed: {e}", exc_info=True)
                self.infrastructure = None

        if self.logger is not None:
            self.logger.info("✅ Shutdown complete")
            self.logger.info("=" * 70)

        # Signal that shutdown is complete
        self.shutdown_event.set()


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Telebrief - Telegram Digest and Editorial Article Generator"
    )
    parser.add_argument(
        "--article",
        action="store_true",
        help="Generate and publish editorial article immediately and exit",
    )
    parser.add_argument(
        "--digest",
        action="store_true",
        help="Generate and send digest immediately and exit",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Lookback hours for on-demand generation",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate without publishing to Telegra.ph or sending to Telegram",
    )
    args = parser.parse_args()

    app = TelebriefApp()

    # Initialize
    if not await app.initialize() or app.config is None or app.logger is None:
        sys.exit(1)

    config = app.config
    logger = app.logger

    if args.article:
        from src.core import generate_and_publish_article

        hours = args.hours or config.settings.article.lookback_hours
        if args.dry_run:
            logger.info(f"Building on-demand article preview ({hours}h, dry_run=True)...")
        else:
            logger.info(
                f"Requesting on-demand article publication ({hours}h)... "
                "worker performs generation and delivery"
            )
        success = await generate_and_publish_article(
            config, logger, hours=hours, dry_run=args.dry_run
        )
        sys.exit(0 if success else 1)

    if args.digest:
        hours = args.hours or config.settings.lookback_hours
        if args.dry_run:
            logger.info(f"Building on-demand digest preview ({hours}h, dry_run=True)...")
            from src.core import DIGEST_PUBLICATION_TYPE
            from src.publication.facade import build_publication_preview

            preview = await build_publication_preview(
                publication_type=DIGEST_PUBLICATION_TYPE,
                lookback_hours=hours,
                config=config,
            )
            print("\n" + "=" * 70)
            print(f"📰 ТЕСТОВОЕ ПРЕВЬЮ ДАЙДЖЕСТА (DRY-RUN, run_id={preview.run_id})")
            print("=" * 70)
            if preview.title:
                print(f"TITLE: {preview.title}\n")
            if preview.lead:
                print(f"LEAD: {preview.lead}\n")
            print(preview.body)
            print("=" * 70 + "\n")
            sys.exit(0)
        else:
            from src.core import generate_and_send_digest

            logger.info(
                f"Requesting on-demand digest publication ({hours}h)... "
                "worker performs generation and delivery"
            )
            success = await generate_and_send_digest(config, logger, hours=hours)
            sys.exit(0 if success else 1)

    # Set up signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(app.shutdown()))

    try:
        # Run application
        await app.run()

    except KeyboardInterrupt:
        pass

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

    finally:
        # Ensure clean shutdown
        if not app.shutdown_event.is_set():
            await app.shutdown()


if __name__ == "__main__":
    print(
        """
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   ████████╗███████╗██╗     ███████╗██████╗ ██████╗     ║
║   ╚══██╔══╝██╔════╝██║     ██╔════╝██╔══██╗██╔══██╗    ║
║      ██║   █████╗  ██║     █████╗  ██████╔╝██████╔╝    ║
║      ██║   ██╔══╝  ██║     ██╔══╝  ██╔══██╗██╔══██╗    ║
║      ██║   ███████╗███████╗███████╗██████╔╝██║  ██║    ║
║      ╚═╝   ╚══════╝╚══════╝╚══════╝╚═════╝ ╚═╝  ╚═╝    ║
║                                                          ║
║         Automated Telegram Digest Generator             ║
║                   Powered by AI                          ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
    """
    )

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nGoodbye! 👋")
