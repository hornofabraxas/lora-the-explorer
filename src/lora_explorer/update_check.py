import asyncio
import json
import logging
import time

import httpx

from . import __version__

log = logging.getLogger(__name__)

GITHUB_RELEASES_URL = "https://api.github.com/repos/hornofabraxas/lora-the-explorer/releases/latest"
# How often the background loop is allowed to actually hit the network, once
# enabled. Not how often it wakes up — see run_update_check_loop.
CHECK_INTERVAL = 86400
# How often the background loop wakes to check whether it's time (or whether
# the setting has been toggled on since the last wake). Cheap — a local
# settings read, no network — so this can be short without cost.
POLL_INTERVAL = 300
TIMEOUT = 5.0

SETTING_ENABLED = "update_check_enabled"
SETTING_CACHE = "update_check_cache"


def _parse_version(v: str) -> tuple[int, ...]:
    """'v0.3.0' -> (0, 3, 0). Non-numeric segments/suffixes are dropped."""
    v = v.lstrip("vV").split("+")[0].split("-")[0]
    parts = []
    for p in v.split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(remote: str, local: str) -> bool:
    return _parse_version(remote) > _parse_version(local)


async def is_enabled(db) -> bool:
    return (await db.get_setting(SETTING_ENABLED)) == "1"


async def set_enabled(db, enabled: bool) -> None:
    await db.set_setting(SETTING_ENABLED, "1" if enabled else "0")


async def get_cached(db) -> dict | None:
    raw = await db.get_setting(SETTING_CACHE)
    return json.loads(raw) if raw else None


async def check_now(db) -> dict:
    """One-shot check against GitHub's public Releases API — a plain,
    unauthenticated GET with no player-identifying data (no player id, no
    location, nothing beyond a standard User-Agent). Always triggered locally,
    either by this explicit call or by the opt-in loop below; never by
    anything a player didn't choose. See PRIVACY.md before changing what this
    sends. Caches the result so the dashboard has something to show between
    checks, and so a failed check doesn't leave the UI blank."""
    result: dict = {
        "checked_at": int(time.time()),
        "ok": False,
        "current_version": __version__,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                GITHUB_RELEASES_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "lora-the-explorer-update-check",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        latest = (data.get("tag_name") or "").strip()
        result.update({
            "ok": True,
            "latest_version": latest,
            "url": data.get("html_url", ""),
            "update_available": bool(latest) and is_newer(latest, __version__),
        })
    except Exception as exc:
        # The app runs at INFO level by default, so this must not be `debug`
        # — a `debug`-level failure here is invisible in `docker logs`, which
        # is exactly where a container owner would look to see why "Check
        # now" failed (DNS, TLS, GitHub rate limiting, etc.).
        log.warning("Update check failed: %s", exc)
        result["error"] = str(exc)
    await db.set_setting(SETTING_CACHE, json.dumps(result))
    return result


async def run_update_check_loop(db) -> None:
    """Opt-in only: makes zero network requests unless SETTING_ENABLED is "1".
    Re-reads the setting every POLL_INTERVAL so toggling it in Settings takes
    effect within minutes, not on the next restart, without polling GitHub
    more than once a day."""
    while True:
        try:
            if await is_enabled(db):
                cached = await get_cached(db)
                stale = (
                    not cached
                    or int(time.time()) - cached.get("checked_at", 0) >= CHECK_INTERVAL
                )
                if stale:
                    log.info("Checking for updates...")
                    await check_now(db)
        except Exception:
            log.exception("Update check loop iteration failed")
        await asyncio.sleep(POLL_INTERVAL)
