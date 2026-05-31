"""Entry point for code-light."""

import argparse
import sys

from .app import App
from .config import Config
from .utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="code-light",
        description="Lightweight desktop status monitoring for AI coding workflows",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--no-floating",
        action="store_true",
        help="Start with floating window hidden",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=7681,
        help="Dashboard port (default: 7681)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Polling interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--opacity",
        type=float,
        default=1.0,
        help="Floating window opacity (0.0-1.0, default: 1.0)",
    )
    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Setup logging
    log_level = 10 if args.debug else 20  # DEBUG or INFO
    setup_logger(level=log_level)

    # Create config
    config = Config(
        dashboard_port=args.port,
        poll_interval_seconds=args.poll_interval,
        floating_window_opacity=args.opacity,
    )

    # Create and run app
    app = App(config)

    if args.no_floating:
        app.start()
        # Keep running
        import signal
        import time

        def signal_handler(sig, frame):
            app.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            app.stop()
    else:
        app.run()


if __name__ == "__main__":
    main()
