import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..game.titles import POSTCARD_TITLE_MEANINGS, TITLE_MEANINGS
from ..game.engine import (
    MULTIPLAYER_SHOP_CATALOG,
    MULTIPLAYER_ITEM_SALVAGE,
    multiplayer_item_name,
    weekly_merchant_item_types,
    _week_start_utc,
)
from .routes import MERCHANT_MIN_CAMP_LEVEL

log = logging.getLogger(__name__)

router = APIRouter()


# Proximity buckets (miles) derived from a scout-revealed distance. These drive
# the Warfront filter chips and the "Nearest first" sort. Distance is unknown
# until a rival is scouted, so an unscouted (or unlocatable) rival is "unknown".
_BAND_NEARBY_MAX_MI = 100
_BAND_REGIONAL_MAX_MI = 500
_BAND_ORDER = {"nearby": 0, "regional": 1, "distant": 2, "unknown": 3}


def _distance_info(scouted: bool, distance_mi: int | None) -> dict:
    """Warfront distance cell for one rival: a display string plus a proximity
    band for filtering/sorting. '?' until scouted; a fuzzed '~N mi' once a probe
    reveals it (or 'unknown' if the rival can't be placed)."""
    if not scouted:
        return {"display": "?", "band": "unknown", "band_ord": _BAND_ORDER["unknown"],
                "scouted": False}
    if distance_mi is None:
        return {"display": "distance unknown", "band": "unknown",
                "band_ord": _BAND_ORDER["unknown"], "scouted": True}
    if distance_mi <= 0:
        display = "< 50 mi"
    else:
        display = f"~{distance_mi} mi"
    if distance_mi <= _BAND_NEARBY_MAX_MI:
        band = "nearby"
    elif distance_mi <= _BAND_REGIONAL_MAX_MI:
        band = "regional"
    else:
        band = "distant"
    return {"display": display, "band": band, "band_ord": _BAND_ORDER[band],
            "scouted": True, "miles": distance_mi}


def _humanize_ago(seconds: int) -> str:
    """Compact 'how long ago' for scout snapshots: 'just now', '9m ago',
    '3h ago', '2d ago'."""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


async def _available_title_labels(db, manager) -> list[str]:
    """Every title label the player has earned and may display: completed
    (5-star) postcard titles plus any earned multiplayer titles. Postcards first,
    then multiplayer, so the picker groups naturally."""
    labels: list[str] = []
    player = await db.get_first_player()
    if player:
        all_postcards = await db.get_all_postcards(player["key"])
        for cls in POSTCARD_TITLE_MEANINGS:
            earned = max(
                (p["stars"] for p in all_postcards if p["class"] == cls),
                default=0,
            )
            if earned >= 5:
                labels.append(cls)
    if manager and manager.registered:
        labels.extend(await manager.get_earned_mp_title_labels())
    return labels


def _flash_redirect(path: str, msg: str, category: str = "info") -> RedirectResponse:
    import urllib.parse
    qs = urllib.parse.urlencode({"flash_msg": msg, "flash_type": category})
    return RedirectResponse(f"{path}?{qs}", status_code=303)


async def _template(request, name, ctx):
    ctx.setdefault("flash_msg", request.query_params.get("flash_msg"))
    ctx.setdefault("flash_type", request.query_params.get("flash_type", "info"))
    radio = request.app.state.radio
    if radio:
        ctx.setdefault("companion_configured", radio.configured)
        ctx.setdefault("companion_connected", radio._mc is not None)
    else:
        ctx.setdefault("companion_configured", False)
        ctx.setdefault("companion_connected", False)
    if "currency" not in ctx:
        player = ctx.get("player")
        if not player:
            db = request.app.state.db
            p = await db.get_first_player()
            if p:
                ctx["currency"] = {
                    "provisions": p["provisions"],
                    "survey_marks": p.get("survey_marks", 0),
                }
    return request.app.state.templates.TemplateResponse(request, name, ctx)


@router.get("/multiplayer")
async def multiplayer_page(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager:
        return await _template(request, "multiplayer.html", {
            "nav_active": "multiplayer",
            "enabled": False,
            "registered": False,
            "player_id": None,
            "leaderboard": [],
        })

    leaderboard = []
    if manager.registered:
        result = await manager._client.get_leaderboard()
        if "players" in result:
            leaderboard = result["players"]

    items = await manager.get_items()

    item_counts = {}
    for item in items:
        t = item["item_type"]
        item_counts[t] = item_counts.get(t, 0) + 1

    # Munitions side of the Frontier Merchant now lives here in the War Chest, so
    # PvP supplies are bought where they're stored. Weekly stock, shared purchase
    # ledger with the relic shelf on Briefing.
    db_ = request.app.state.db
    _pl = await db_.get_first_player()
    merchant_unlocked = bool(_pl and _pl["base_camp_level"] >= MERCHANT_MIN_CAMP_LEVEL)
    merchant_reset_ts = _week_start_utc() + 7 * 86400
    merchant_items = []
    if merchant_unlocked and manager.registered:
        offered = weekly_merchant_item_types(_pl["key"])
        purchased = set(await db_.get_merchant_purchases(_pl["key"], _week_start_utc()))
        for item_type, spec in MULTIPLAYER_SHOP_CATALOG.items():
            if item_type not in offered:
                continue
            currency = spec["currency"]
            emoji = "🔭" if item_type == "probe" else (
                "⚔️" if item_type.startswith("attack") else "🛡️")
            balance = _pl["provisions"] if currency == "provisions" else _pl["survey_marks"]
            merchant_items.append({
                "type": item_type,
                "label": "Scout" if item_type == "probe" else item_type.replace("_", " ").title(),
                "emoji": emoji,
                "price": spec["price"],
                "currency": currency,
                "symbol": "📦" if currency == "provisions" else "🪙",
                "purchased": f"mp_{item_type}" in purchased,
                "affordable": balance >= spec["price"],
            })

    # Merge owned counts with this week's buyable stock into a single War Chest
    # view so each munition has one row: [Buy] [icon Name ×N] [Sell]. Catalog
    # order first, then any owned types not currently stocked.
    buy_by_type = {m["type"]: m for m in merchant_items}
    _order = list(MULTIPLAYER_SHOP_CATALOG.keys())
    for t in item_counts:
        if t not in _order:
            _order.append(t)
    war_chest = []
    for t in _order:
        if t not in item_counts and t not in buy_by_type:
            continue
        emoji = "🔭" if t == "probe" else (
            "⚔️" if t.startswith("attack") else "🛡️" if t.startswith("defense") else "📦")
        sv = MULTIPLAYER_ITEM_SALVAGE.get(t)
        name = multiplayer_item_name(t)
        war_chest.append({
            "type": t,
            "label": name["full"],       # full "Blasting Powder IV" for roomy contexts (salvage dialog)
            "name_short": name["short"],  # shelf: short root + separate tier chip so the numeral survives truncation
            "tier": name["tier"],
            "emoji": emoji,
            "rarity": t.split("_")[-1] if "_" in t else "common",
            "kind": "attack" if t.startswith("attack") else "defense" if t.startswith("defense") else t,
            "count": item_counts.get(t, 0),
            "buy": buy_by_type.get(t),
            "salvage": ({"value": sv["value"],
                         "symbol": "📦" if sv["currency"] == "provisions" else "🪙"} if sv else None),
        })

    cached_scouts = await manager.get_cached_scouts()
    scout_times = await manager.get_scout_times()
    scout_distances = await manager.get_scout_distances()
    _now = int(time.time())
    scout_ages = {pid: _humanize_ago(max(0, _now - ts)) for pid, ts in scout_times.items()}
    # Per-rival distance cell: '?' until scouted, then a fuzzed '~N mi'. Keyed by
    # player_id so the Warfront rows can render + filter/sort by proximity.
    distance_info = {
        p.get("player_id"): _distance_info(
            p.get("player_id") in cached_scouts,
            scout_distances.get(p.get("player_id")),
        )
        for p in (leaderboard or [])
    }
    defense_data = await manager.get_defense() if manager.registered else {}
    defense_posts = {p["post_token"]: p for p in defense_data.get("posts", [])} if defense_data.get("ok") else {}
    inbound_count = sum(len(p.get("incoming_raids", [])) for p in defense_posts.values())

    active_raid = await manager.get_active_raid() if manager.registered else None

    # Per-target raid cooldowns (post_token -> epoch seconds it clears), so the
    # Warfront can show a countdown instead of the player only finding out a
    # target is on cooldown after Dispatch rejects it.
    raid_cooldowns = {}
    if manager.registered and manager.pvp_enabled:
        cd_result = await manager._client.get_raid_cooldowns()
        if cd_result.get("ok"):
            raid_cooldowns = cd_result.get("expires_at", {})

    # Re-check title eligibility on render (rank from cached leaderboard, plus
    # raid/scout counters) so a title earned since the last poll shows right away.
    if manager.registered:
        await manager.check_multiplayer_titles(leaderboard or None)

    # You need at least one active (non-warded) outpost to launch a raid.
    can_attack = False
    own_post_names = {}
    db = request.app.state.db
    player = await db.get_first_player()
    available_titles = await _available_title_labels(db, manager)
    active_title = player.get("active_title") if player else None
    if player:
        now = int(time.time())
        posts = await db.get_all_posts(player["key"])
        can_attack = any(
            not (p.get("ruin_frozen_until") and now < p["ruin_frozen_until"])
            for p in posts
        )
        # Worker refs (mp_token) → local post names, so the Warfront can label
        # our own threatened posts with their real names instead of a token.
        own_post_names = {
            p["mp_token"]: p["name"] for p in posts
        }

    return await _template(request, "multiplayer.html", {
        "nav_active": "multiplayer",
        "enabled": True,
        "registered": manager.registered,
        "player_id": manager.player_id,
        "pvp_enabled": manager.pvp_enabled,
        "leaderboard": leaderboard,
        "items": items,
        "merchant_unlocked": merchant_unlocked,
        "merchant_reset_ts": merchant_reset_ts,
        "war_chest": war_chest,
        "cached_scouts": cached_scouts,
        "scout_ages": scout_ages,
        "scout_distances": scout_distances,
        "distance_info": distance_info,
        "defense_posts": defense_posts,
        "inbound_count": inbound_count,
        "now_ts": int(time.time()),
        "active_raid": active_raid,
        "raid_cooldowns": raid_cooldowns,
        "can_attack": can_attack,
        "own_post_names": own_post_names,
        "last_sync_at": manager._last_push_at,
        "available_titles": available_titles,
        "active_title": active_title,
        "title_meanings": TITLE_MEANINGS,
    })


@router.post("/api/multiplayer/title")
async def multiplayer_set_title(request: Request):
    """Set (or clear) the player's displayed title from the Multiplayer picker.
    Accepts any postcard or multiplayer title the player has actually earned;
    pushes the change to the Worker so it lands on every leaderboard row."""
    manager = request.app.state.multiplayer_manager
    db = request.app.state.db
    player = await db.get_first_player()
    if not player:
        return _flash_redirect("/multiplayer", "No player found", "error")

    form = await request.form()
    title = str(form.get("title", "")).strip()

    if not title:
        await db.set_active_title(player["key"], None)
    elif title in await _available_title_labels(db, manager):
        await db.set_active_title(player["key"], title)
    else:
        return _flash_redirect("/multiplayer", "Title not earned yet", "error")

    # Propagate immediately rather than waiting for the next survey bundle.
    if manager and manager.registered:
        try:
            await manager.force_sync()
        except Exception:
            log.debug("Title force-sync failed", exc_info=True)
    return _flash_redirect("/multiplayer", "Title updated", "success")


@router.post("/api/multiplayer/register")
async def multiplayer_register(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager:
        return _flash_redirect("/multiplayer", "Multiplayer not configured", "error")

    if manager.registered:
        return _flash_redirect("/multiplayer", "Already registered", "error")

    form = await request.form()
    display_name = str(form.get("display_name", "")).strip()
    if not display_name or len(display_name) > 32:
        return _flash_redirect("/multiplayer", "Name must be 1-32 characters", "error")

    # The hosted war ledger is 18+ (see TERMS.md). The checkbox is `required` in
    # the form, but enforce it here too so the age confirmation can't be skipped
    # by posting the form directly.
    if not form.get("age_confirm"):
        return _flash_redirect(
            "/multiplayer",
            "You must confirm you are 18 or over to join the war ledger",
            "error",
        )

    invite_code = str(form.get("invite_code", "")).strip() or None
    result = await manager.register(display_name, invite_code=invite_code)
    if result.get("ok"):
        await manager.force_sync()
        return _flash_redirect("/multiplayer", f"Registered as {display_name}!", "success")
    return _flash_redirect("/multiplayer", f"Registration failed: {result.get('error', 'unknown')}", "error")


@router.post("/api/multiplayer/force-sync")
async def multiplayer_force_sync(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return _flash_redirect("/multiplayer", "Not registered", "error")

    result = await manager.force_sync()
    if result is None:
        return _flash_redirect("/settings", "No new surveys to sync", "info")
    if result.get("ok"):
        return _flash_redirect("/settings", "Sync complete", "success")
    return _flash_redirect("/settings", f"Sync failed: {result.get('error', 'unknown')}", "error")


@router.get("/api/multiplayer/status")
async def multiplayer_status(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager:
        return JSONResponse({"enabled": False, "registered": False})
    return JSONResponse({
        "enabled": True,
        "registered": manager.registered,
        "player_id": manager.player_id,
    })


@router.get("/api/multiplayer/leaderboard")
async def multiplayer_leaderboard_api(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"players": []})
    result = await manager._client.get_leaderboard()
    return JSONResponse(result if "players" in result else {"players": []})


@router.get("/api/multiplayer/items")
async def multiplayer_items_api(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"items": []})
    items = await manager.get_items()
    return JSONResponse({"items": items})


@router.post("/api/multiplayer/scout")
async def multiplayer_scout(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"ok": False, "error": "Not registered"}, status_code=400)

    body = await request.json()
    target_player_id = body.get("target_player_id")
    probe_item_id = body.get("probe_item_id")

    if not target_player_id or not probe_item_id:
        return JSONResponse({"ok": False, "error": "Missing fields"}, status_code=400)

    result = await manager.scout_target(target_player_id, probe_item_id)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@router.post("/api/multiplayer/install-item")
async def multiplayer_install_item(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"ok": False, "error": "Not registered"}, status_code=400)

    body = await request.json()
    post_token = body.get("post_token")
    item_id = body.get("item_id")

    if not post_token or not item_id:
        return JSONResponse({"ok": False, "error": "Missing fields"}, status_code=400)

    result = await manager.install_item(post_token, item_id)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@router.post("/api/multiplayer/restore-hp")
async def multiplayer_restore_hp(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"ok": False, "error": "Not registered"}, status_code=400)

    body = await request.json()
    post_token = body.get("post_token")
    provisions_spent = body.get("provisions_spent")

    if not post_token or not isinstance(provisions_spent, int) or provisions_spent <= 0:
        return JSONResponse({"ok": False, "error": "Invalid fields"}, status_code=400)

    result = await manager.restore_hp(post_token, provisions_spent)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@router.get("/api/multiplayer/defense")
async def multiplayer_defense_api(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"ok": False, "error": "Not registered"}, status_code=400)
    result = await manager.get_defense()
    return JSONResponse(result)


@router.get("/api/multiplayer/raid/mine")
async def multiplayer_raid_mine(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"ok": False, "error": "Not registered"}, status_code=400)
    raid = await manager.get_active_raid()
    return JSONResponse({"ok": True, "raid": raid, "now_ts": int(time.time())})


@router.post("/api/multiplayer/raid/preview")
async def multiplayer_raid_preview(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"ok": False, "error": "Not registered"}, status_code=400)
    body = await request.json()
    result = await manager.preview_raid(
        body.get("target_player_id"), body.get("target_post_token"),
        body.get("item_ids", []),
    )
    return JSONResponse(result)


@router.post("/api/multiplayer/raid/dispatch")
async def multiplayer_raid_dispatch(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"ok": False, "error": "Not registered"}, status_code=400)
    body = await request.json()
    target_player_id = body.get("target_player_id")
    target_post_token = body.get("target_post_token")
    item_ids = body.get("item_ids", [])
    if not target_player_id or not target_post_token or not item_ids:
        return JSONResponse({"ok": False, "error": "Missing fields"}, status_code=400)
    result = await manager.dispatch_raid(target_player_id, target_post_token, item_ids)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/api/multiplayer/defend/boost")
async def multiplayer_defend_boost(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"ok": False, "error": "Not registered"}, status_code=400)
    body = await request.json()
    post_token = body.get("post_token")
    item_ids = body.get("item_ids", [])
    if not post_token or not item_ids:
        return JSONResponse({"ok": False, "error": "Missing fields"}, status_code=400)
    result = await manager.deploy_boost(post_token, item_ids)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/api/multiplayer/pvp/enable")
async def multiplayer_pvp_enable(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"ok": False, "error": "Not registered"}, status_code=400)

    result = await manager.enable_pvp()
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@router.post("/api/multiplayer/webhook")
async def multiplayer_webhook(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"ok": False, "error": "Not registered"}, status_code=400)

    body = await request.json()
    url = body.get("url", "").strip()
    await manager.set_webhook_url(url)
    return JSONResponse({"ok": True, "webhook_url": url})


@router.post("/api/multiplayer/mesh-notify")
async def multiplayer_mesh_notify(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return JSONResponse({"ok": False, "error": "Not registered"}, status_code=400)

    body = await request.json()
    enabled = bool(body.get("enabled", True))
    await manager.set_mesh_notify(enabled)
    return JSONResponse({"ok": True, "enabled": enabled})


@router.get("/api/multiplayer/pvp/status")
async def multiplayer_pvp_status(request: Request):
    manager = request.app.state.multiplayer_manager
    if not manager:
        return JSONResponse({"pvp_enabled": False})
    return JSONResponse({"pvp_enabled": manager.pvp_enabled})
