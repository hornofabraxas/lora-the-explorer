"""Windows tray launcher for LoRa the Explorer.

Only used by the packaged Windows build (see pyproject.toml's `windows` extra
and packaging/windows/). Docker, Linux, and `python -m lora_explorer` never
import this module and never need pystray/Pillow installed.

Runs the same async server as the CLI entry point (main.run()) — this file
adds a system-tray icon and a "no console window" logging setup around it,
nothing about the game server itself changes.
"""
import asyncio
import logging
import logging.handlers
import os
import sys
import threading
import webbrowser
from pathlib import Path

# Must happen before `lora_explorer.main` is imported below: main.py reads HOST
# via os.getenv at MODULE level (WEB_HOST = os.getenv("HOST", "0.0.0.0")), so
# setting this in main() would be too late — the import below has already
# locked in whatever HOST was set at this point. Desktop default is loopback
# only, since this machine's DB holds full location history; a user who wants
# LAN access can still set HOST themselves before launching.
os.environ.setdefault("HOST", "127.0.0.1")


def _configure_logging() -> Path:
    """Must run before `lora_explorer.main` is imported: that module's own
    logging.basicConfig() call is a no-op once a handler already exists, so
    setting one up first is what redirects output to a file instead of a
    console window a --windowed build doesn't have. Without this, a startup
    crash would be completely invisible to the user."""
    from .paths import default_db_path
    log_dir = Path(default_db_path()).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "lora-explorer.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    ))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    return log_path


_LOG_PATH = _configure_logging()

# PyInstaller windowed (console=False) builds have no console, so Windows
# leaves sys.stdout/sys.stderr as None — anything downstream that assumes a
# real stream exists (uvicorn's default logging setup, an uncaught
# traceback's default excepthook, etc.) then throws the moment it tries to
# write, and since stderr is None that failure is completely invisible: the
# asyncio main thread dies silently while pystray's tray-icon thread (a
# separate, non-daemon thread) keeps the process alive forever with no
# window, no port bound, and nothing in the log. Redirecting to devnull here
# — PyInstaller's own documented fix for this — guarantees a real stream
# exists everywhere below.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from . import __version__  # noqa: E402  (must follow _configure_logging above)
from . import main as lora_main  # noqa: E402  (same import-order reason)
from .web.app import STATIC_DIR  # noqa: E402  (same import-order reason)

log = logging.getLogger(__name__)


class TrayApp:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._icon = None

    def _dashboard_url(self) -> str:
        return f"http://{lora_main.WEB_HOST}:{lora_main.WEB_PORT}"

    def _open_dashboard(self, icon=None, item=None) -> None:
        webbrowser.open(self._dashboard_url())

    def _open_log_folder(self, icon=None, item=None) -> None:
        os.startfile(_LOG_PATH.parent)  # noqa: S606 — Windows-only build

    def _quit(self, icon=None, item=None) -> None:
        log.info("Quit requested from tray")
        if self._loop and self._stop_event:
            # We're on pystray's thread here, not the asyncio loop's — this is
            # the one safe way to touch an asyncio.Event from another thread.
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._icon:
            self._icon.stop()

    def _on_ready(self, loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
        """Passed to main.run() as on_ready — captures the references Quit
        needs, and opens the dashboard shortly after (uvicorn's own bind
        happens within this same startup, well under the delay below, and a
        manually-launched tray app's user is already waiting a beat)."""
        self._loop = loop
        self._stop_event = stop_event
        threading.Timer(1.5, self._open_dashboard).start()

    def _build_icon(self):
        import pystray
        from PIL import Image
        image = Image.open(STATIC_DIR / "icon-512.png")
        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", self._open_dashboard, default=True),
            pystray.MenuItem("Open Log Folder", self._open_log_folder),
            pystray.MenuItem("Quit", self._quit),
        )
        return pystray.Icon("lora-explorer", image, "LoRa the Explorer", menu)

    def run(self) -> None:
        self._icon = self._build_icon()
        # run_detached() spawns pystray's own message-loop thread and returns
        # immediately — asyncio.run() below stays the true main-thread
        # blocker, which the signal-handling fallback in main.py assumes.
        self._icon.run_detached()
        try:
            asyncio.run(lora_main.run(on_ready=self._on_ready))
        except Exception:
            # Belt-and-suspenders: the stdout/stderr guard above should mean
            # exceptions no longer vanish, but a windowed build has no console
            # to show them either way — the log file is the only place a user
            # (or Open Log Folder from the tray menu) can ever see this.
            log.exception("Fatal error during startup/run")
        finally:
            self._icon.stop()


def main() -> None:
    log.info("Starting LoRa the Explorer (tray), version %s", __version__)
    TrayApp().run()


if __name__ == "__main__":
    main()
