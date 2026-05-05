"""
Main entry point for the YouTube Downloader Monitor service.
Initializes configuration, logging, and starts the monitoring loop.
"""
from __future__ import annotations

import logging
import signal
import sys
import time

from app.config import get_settings
from app.services.monitor import MonitorService

def _setup_logging(log_level: str) -> None:
    """
    Configure the global logging system.
    Sets up a stream handler with a specific format for structured logging.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    fmt = "ts=%(asctime)sZ level=%(levelname)s logger=%(name)s msg=%(message)s"
    handler.setFormatter(logging.Formatter(fmt=fmt, datefmt="%Y-%m-%dT%H:%M:%S"))
    
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    
    # Quiet noisy libs
    logging.getLogger("urllib3").setLevel(max(level, logging.WARNING))

def main():
    """
    Bootstrap the application, set up signal handlers, and run the monitoring loop.
    """
    try:
        settings = get_settings()
        _setup_logging(settings.log_level)
    except Exception as e:
        print(f"Failed to load settings: {e}", file=sys.stderr)
        sys.exit(1)

    log = logging.getLogger("app.main")
    log.info("Starting Youtube Downloader Monitor...")
    log.info(f"Monitor Mode: {settings.poll_interval_seconds}s interval")

    monitor = MonitorService(settings)
    
    def handle_sigterm(*args):
        log.info("Received SIGTERM/SIGINT, shutting down...")
        monitor.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    # Main application loop
    while True:
        try:
            start_t = time.time()
            # Process one batch of pending requests
            monitor.run_once()
            
            # Calculate sleep time to maintain the poll interval
            elapsed = time.time() - start_t
            sleep_time = max(1.0, settings.poll_interval_seconds - elapsed)
            time.sleep(sleep_time)
        except Exception as e:
            # On unexpected errors, log and wait before retrying the loop
            log.error(f"Top-level loop error: {e}", exc_info=True)
            time.sleep(settings.retry_backoff_seconds)

if __name__ == "__main__":
    main()
