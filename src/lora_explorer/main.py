import asyncio
import logging
import os
import signal

import uvicorn

from .paths import default_db_path
from .radio.meshcore_adapter import MeshCoreAdapter
from .game.database import Database
from .game.engine import GameEngine
from .game.backup import run_backup_loop
from .multiplayer.client import WorkerClient
from .multiplayer.manager import MultiplayerManager
from .update_check import run_update_check_loop
from .web.app import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

WEB_PORT = int(os.getenv("WEB_PORT", "1492"))
# 0.0.0.0 is correct for Docker/Unraid (unchanged default — nothing there sets
# HOST). A native desktop run should bind loopback-only instead, since this
# machine's database holds full location history; the Windows installer sets
# HOST=127.0.0.1 explicitly rather than this default changing underneath
# existing container deployments.
WEB_HOST = os.getenv("HOST", "0.0.0.0")


def browser_host(bind_host: str) -> str:
    """Map a *bind* address to a host a browser can actually connect to.

    ``0.0.0.0`` and ``::`` are wildcard bind addresses — they mean "listen on
    every interface", not a reachable destination. On Linux/macOS a browser
    typing ``0.0.0.0`` usually still resolves to loopback, but on Windows it
    does not connect at all, so a fresh Windows install shows "can't reach this
    page" at ``0.0.0.0:1492`` even though the server is bound and listening
    (Taskwarrior task 10). Advertise loopback instead; the actual bind is
    unchanged, so LAN/VPN access via the machine's real IP still works."""
    if bind_host in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return bind_host


def get_env_config() -> dict:
    return {
        "connection_type": os.getenv("CONNECTION_TYPE", "wifi"),
        "companion_host": os.getenv("COMPANION_HOST", ""),
        "companion_port": int(os.getenv("COMPANION_PORT", "4000")),
        "serial_port": os.getenv("SERIAL_PORT", "/dev/ttyUSB0"),
        "ble_address": os.getenv("BLE_ADDRESS", ""),
        "ble_pin": os.getenv("BLE_PIN", ""),
        "home_lat": float(os.getenv("HOME_LAT", "0")),
        "home_lon": float(os.getenv("HOME_LON", "0")),
        "db_path": os.getenv("DB_PATH") or default_db_path(),
    }


async def _load_companion_config(db: Database, env_config: dict) -> dict:
    saved = await db.get_companion_config()
    if saved:
        return {
            "connection_type": saved.get("connection_type", "wifi"),
            "companion_host": saved.get("companion_host", ""),
            "companion_port": saved.get("companion_port", 4000),
            "serial_port": saved.get("serial_port", "/dev/ttyUSB0"),
            "ble_address": saved.get("ble_address", ""),
            "ble_pin": saved.get("ble_pin", ""),
        }
    if env_config["companion_host"] or env_config["ble_address"]:
        return {
            "connection_type": env_config["connection_type"],
            "companion_host": env_config["companion_host"],
            "companion_port": env_config["companion_port"],
            "serial_port": env_config["serial_port"],
            "ble_address": env_config["ble_address"],
            "ble_pin": env_config["ble_pin"],
        }
    return {
        "connection_type": "wifi",
        "companion_host": "",
        "companion_port": 4000,
        "serial_port": "/dev/ttyUSB0",
        "ble_address": "",
        "ble_pin": "",
    }


async def run(on_ready=None) -> None:
    """``on_ready(loop, stop_event)``, if given, is called once the event loop
    and shutdown signal exist, before the server starts serving. The Windows
    tray launcher uses this to capture a reference it can use to trigger a
    clean shutdown from its own thread (loop.call_soon_threadsafe(stop_event.set))
    when the user clicks Quit — the CLI/Docker entry point passes nothing and
    behaves exactly as before."""
    env_config = get_env_config()

    if env_config["home_lat"] == 0 and env_config["home_lon"] == 0:
        log.info("HOME_LAT/HOME_LON not set — player will choose via web setup")

    db = Database(db_path=env_config["db_path"])
    await db.connect()

    companion_cfg = await _load_companion_config(db, env_config)

    adapter = MeshCoreAdapter(
        connection_type=companion_cfg["connection_type"],
        host=companion_cfg["companion_host"],
        port=companion_cfg["companion_port"],
        serial_port=companion_cfg["serial_port"],
        ble_address=companion_cfg["ble_address"],
        ble_pin=companion_cfg["ble_pin"],
    )
    adapter.set_data_dir(os.path.dirname(env_config["db_path"]))

    config = {
        **env_config,
        **companion_cfg,
    }

    engine = GameEngine(
        adapter=adapter,
        home_lat=config["home_lat"],
        home_lon=config["home_lon"],
        db=db,
    )

    worker_url = os.getenv("CUSTOM_WORKER_URL", "https://lora.nukeradio.net")
    worker_invite_code = os.getenv("WORKER_INVITE_CODE") or None
    worker_client = WorkerClient(worker_url, invite_code=worker_invite_code)
    multiplayer_manager = MultiplayerManager(worker_client, db, engine)
    log.info("Multiplayer Worker at %s", worker_url)

    app = create_app(engine, db, config, radio=adapter, multiplayer_manager=multiplayer_manager)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    if on_ready:
        on_ready(loop, stop_event)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
    except NotImplementedError:
        # Windows' default ProactorEventLoop doesn't implement
        # add_signal_handler at all — this raises before registering anything,
        # so falling back for both signals here is safe. signal.signal() is
        # supported on Windows; call_soon_threadsafe is the correct way to
        # touch the event loop from a handler that isn't scheduled by it.
        def _handle_signal(signum, frame):
            loop.call_soon_threadsafe(stop_event.set)

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, _handle_signal)

    uvi_config = uvicorn.Config(
        app, host=WEB_HOST, port=WEB_PORT, log_level="info",
        # Backstop: force-close anything still open 10s into the drain so a
        # wedged connection can't hold the container open until Docker SIGKILLs.
        timeout_graceful_shutdown=10,
    )
    server = uvicorn.Server(uvi_config)

    await engine.start()
    await multiplayer_manager.start()
    log.info("LoRa the Explorer is running. Dashboard at http://%s:%d", browser_host(WEB_HOST), WEB_PORT)

    web_task = asyncio.create_task(server.serve())
    web_task.add_done_callback(lambda _: stop_event.set())
    backup_task = asyncio.create_task(run_backup_loop(config["db_path"], engine=engine))
    update_check_task = asyncio.create_task(run_update_check_loop(db))

    try:
        await stop_event.wait()
        # Tell live SSE streams to close *before* draining the web server, so
        # uvicorn's graceful shutdown doesn't block on them (adapter/db teardown
        # still runs in `finally` via engine.stop()).
        engine.begin_shutdown()
        server.should_exit = True
        backup_task.cancel()
        update_check_task.cancel()
        await web_task
    finally:
        await multiplayer_manager.stop()
        await engine.stop()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
