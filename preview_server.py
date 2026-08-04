"""Minimal preview server for UI development — no companion needed."""
import math
import time
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

app = FastAPI()
BASE = Path(__file__).parent / "src" / "lora_explorer" / "web"
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        BASE / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )

MOCK_PLAYER = {
    "key": "abcdef1234567890abcdef1234567890",
    "rank_level": 3,
    "provisions": 42,
    "field_notes": 15,
    "survey_marks": 7,
    "base_camp_level": 2,
    "home_validated": True,
    "scan_count": 28,
    "last_survey_sender": "node_abc123",
}

MOCK_CONFIG = {
    "connection_type": "wifi",
    "companion_host": "10.0.0.50",
    "companion_port": 5000,
    "home_lat": 33.4484,
    "home_lon": -112.0740,
    "utc_offset": -7,
}

from lora_explorer.radio.meshcore_adapter import derive_mesh_health, _fmt_uptime
from lora_explorer.game.titles import TITLE_MEANINGS
from lora_explorer.web.multiplayer_routes import _distance_info as _preview_distance

MOCK_COMPANION = {
    "connected": True,
    "connection": "TCP 10.0.0.50:5000",
    "node_name": "Spyglass-01",
    "public_key": "a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00",
    "battery_mv": 3850,
    "noise_floor": -102,
    "last_rssi": -86,
    "last_snr": 9.5,
    "tx_air_secs": 320,
    "rx_air_secs": 1850,
    "uptime_secs": 93600,   # 1d 2h
    "errors": 3,
    "queue_len": 2,
    "recv": 1420,
    "recv_errors": 120,
}
MOCK_COMPANION["uptime_display"] = _fmt_uptime(MOCK_COMPANION["uptime_secs"])
MOCK_COMPANION["health_metrics"] = derive_mesh_health(MOCK_COMPANION)

MOCK_RELICS = [
    {"id": 1, "type": "buried_cache"},
    {"id": 2, "type": "vigor_tonic"},
    {"id": 3, "type": "vigor_tonic"},
    {"id": 4, "type": "wardstone"},
]

MOCK_POSTS = [
    {"id": 1, "name": "Hilltop Watch"},
    {"id": 2, "name": "River Bend"},
]

COMMON = {
    "flash_msg": None,
    "flash_type": "info",
    "currency": {"provisions": 42, "field_notes": 15, "survey_marks": 7},
}


def _hex_boundary(lat, lng, radius=0.008):
    """Generate 6 vertices of a hexagon centered at (lat, lng)."""
    return [
        [lat + radius * math.cos(math.radians(60 * i + 30)),
         lng + radius * math.sin(math.radians(60 * i + 30))]
        for i in range(6)
    ]


HOME_LAT, HOME_LNG = 33.4484, -112.0740
NOW = int(time.time())

MOCK_HEXES = [
    {
        "hex_id": "hex_01", "name": "Crimson Mesa Outlook", "lat": 33.460, "lng": -112.080,
        "boundary": _hex_boundary(33.460, -112.080), "survey_count": 4,
        "discovered_at": NOW - 8 * 86400, "on_cooldown": False, "type": "discovered",
        "post_name": None, "post_level": None,
        "survey_lat": 33.461, "survey_lng": -112.079, "survey_id": 101,
        "surveyed_at": NOW - 3600, "distance_miles": 1.1,
    },
    {
        "hex_id": "hex_02", "name": "Dusty Creek Hollow", "lat": 33.452, "lng": -112.060,
        "boundary": _hex_boundary(33.452, -112.060), "survey_count": 6,
        "discovered_at": NOW - 7 * 86400, "on_cooldown": True, "type": "post",
        "post_name": "River Bend", "post_level": 1,
        "survey_lat": 33.453, "survey_lng": -112.059, "survey_id": 102,
        "surveyed_at": NOW - 7200, "distance_miles": 1.2,
    },
    {
        "hex_id": "hex_03", "name": "Ironwood Basin", "lat": 33.442, "lng": -112.095,
        "boundary": _hex_boundary(33.442, -112.095), "survey_count": 2,
        "discovered_at": NOW - 5 * 86400, "on_cooldown": False, "type": "discovered",
        "post_name": None, "post_level": None,
        "survey_lat": 33.443, "survey_lng": -112.094, "survey_id": 103,
        "surveyed_at": NOW - 86400, "distance_miles": 1.5,
    },
    {
        "hex_id": "hex_04", "name": "Cactus Ridge", "lat": 33.468, "lng": -112.068,
        "boundary": _hex_boundary(33.468, -112.068), "survey_count": 3,
        "discovered_at": NOW - 6 * 86400, "on_cooldown": True, "type": "discovered",
        "post_name": None, "post_level": None,
        "survey_lat": 33.469, "survey_lng": -112.067, "survey_id": 104,
        "surveyed_at": NOW - 2 * 86400, "distance_miles": 1.8,
    },
    {
        "hex_id": "hex_05", "name": "Sunset Bluff", "lat": 33.435, "lng": -112.050,
        "boundary": _hex_boundary(33.435, -112.050), "survey_count": 8,
        "discovered_at": NOW - 10 * 86400, "on_cooldown": False, "type": "post",
        "post_name": "Hilltop Watch", "post_level": 2, "upkeep_due": True,
        "survey_lat": 33.436, "survey_lng": -112.049, "survey_id": 105,
        "surveyed_at": NOW - 1800, "distance_miles": 2.0,
    },
    {
        "hex_id": "hex_06", "name": "Copper Canyon", "lat": 33.458, "lng": -112.042,
        "boundary": _hex_boundary(33.458, -112.042), "survey_count": 1,
        "discovered_at": NOW - 3 * 86400, "on_cooldown": True, "type": "discovered",
        "post_name": None, "post_level": None,
        "survey_lat": 33.459, "survey_lng": -112.041, "survey_id": 106,
        "surveyed_at": NOW - 3 * 86400, "distance_miles": 2.4,
    },
    {
        "hex_id": "hex_07", "name": "Rattlesnake Flats", "lat": 33.425, "lng": -112.088,
        "boundary": _hex_boundary(33.425, -112.088), "survey_count": 2,
        "discovered_at": NOW - 4 * 86400, "on_cooldown": False, "type": "discovered",
        "post_name": None, "post_level": None,
        "survey_lat": 33.426, "survey_lng": -112.087, "survey_id": 107,
        "surveyed_at": NOW - 4 * 86400, "distance_miles": 1.9,
    },
    {
        "hex_id": "hex_08", "name": "Saguaro Heights", "lat": 33.482, "lng": -112.055,
        "boundary": _hex_boundary(33.482, -112.055), "survey_count": 1,
        "discovered_at": NOW - 2 * 86400, "on_cooldown": False, "type": "discovered",
        "post_name": None, "post_level": None,
        "survey_lat": 33.490, "survey_lng": -112.045, "survey_id": 108,
        "surveyed_at": NOW - 2 * 86400, "distance_miles": 3.4,
    },
    {
        "hex_id": "hex_09", "name": "Palo Verde Wash", "lat": 33.440, "lng": -112.115,
        "boundary": _hex_boundary(33.440, -112.115), "survey_count": 3,
        "discovered_at": NOW - 9 * 86400, "on_cooldown": True, "type": "discovered",
        "post_name": None, "post_level": None,
        "survey_lat": 33.441, "survey_lng": -112.114, "survey_id": 109,
        "surveyed_at": NOW - 5 * 86400, "distance_miles": 2.8,
    },
]


MOCK_REPEATERS = [
    {"public_key": "abc123", "name": "Hilltop-R1", "lat": 33.455, "lon": -112.080, "path_len": 1, "updated_at": NOW},
    {"public_key": "def456", "name": "Tower-R2", "lat": 33.430, "lon": -112.100, "path_len": 2, "updated_at": NOW},
]


@app.get("/api/hexes")
async def api_hexes():
    return JSONResponse({"hexes": MOCK_HEXES, "repeaters": MOCK_REPEATERS})


_sse_subscribers = []
_sse_next_id = [100]


async def _sse_push(event: dict):
    """Fan a live event out to all connected SSE streams (preview only)."""
    _sse_next_id[0] += 1
    event.setdefault("id", _sse_next_id[0])
    for q in list(_sse_subscribers):
        q.put_nowait(event)


@app.get("/api/events")
async def api_events():
    from starlette.responses import StreamingResponse
    import asyncio
    import json

    now = int(time.time())
    mock_events = [
        {"id": 1, "type": "survey", "ts": now - 300, "data": {"hex_name": "Verdant Hollow", "xp": 42, "provisions": 15, "field_notes": 3, "discovery": True, "relic": None, "distance": 2.3, "promotions": []}},
        {"id": 2, "type": "rank_up", "ts": now - 298, "data": {"level": 5, "name": "Scout"}},
        {"id": 3, "type": "survey", "ts": now - 120, "data": {"hex_name": "Iron Ridge", "xp": 28, "provisions": 12, "field_notes": 3, "discovery": False, "relic": None, "distance": 1.1, "promotions": []}},
        {"id": 4, "type": "charter", "ts": now - 60, "data": {"post_name": "Northwatch", "territory": "Iron Ridge"}},
    ]

    queue = asyncio.Queue()
    _sse_subscribers.append(queue)

    async def event_stream():
        try:
            for event in mock_events:
                yield f"data: {json.dumps(event)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            if queue in _sse_subscribers:
                _sse_subscribers.remove(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {
        **COMMON,
        "nav_active": "dashboard",
        # Preview the one-time First Contact popup with ?first_contact=1
        "show_first_contact": request.query_params.get("first_contact") == "1",
        "player": MOCK_PLAYER,
        "rank_name": "Trailblazer",
        "next_rank": {"level": 4},
        "xp_in_rank": 120,
        "xp_needed": 300,
        "xp_progress": 40,
        "momentum_tier": 2,
        "relics": [
            {"id": 1, "type": "vigor_tonic"},
            {"id": 2, "type": "wardstone"},
            {"id": 3, "type": "buried_cache"},
        ],
        "dispatch": {"message": "The Society commends your tireless efforts. New territories await beyond the ridge."},
        "dispatch_msg": "The Society commends your tireless efforts. New territories await beyond the ridge. 2 Field Training objectives remain before full Society clearance.",
        "relic_counts": {"buried_cache": 1, "vigor_tonic": 1, "wardstone": 0},
        "discovered_relics": {"buried_cache", "vigor_tonic", "wardstone"},
        "buried_cache_amount": 40,
        "relic_salvage_value": {"vigor_tonic": 50, "wardstone": 75},
        "merchant_unlocked": True,
        "merchant_reset_ts": int(__import__('time').time()) + 3 * 86400 + 4 * 3600,
        "merchant_relic_prices": {"vigor_tonic": 100, "wardstone": 150},
        "merchant_relic_purchased": {"vigor_tonic"},
        "merchant_min_camp": 5,
        "posts": MOCK_POSTS,
        "ft_cards": [
            {"name": "Staking Claim", "earned": True, "hint": "Set your base camp location"},
            {"name": "First Contact", "earned": True, "hint": "Complete your first survey"},
            {"name": "Long Range", "earned": True, "hint": "Survey from 1+ mile away"},
            {"name": "Cartographer", "earned": False, "hint": "Discover 5 unique territories"},
            {"name": "Relic Hunter", "earned": False, "hint": "Find your first relic"},
            {"name": "Sworn In", "earned": True, "hint": "Reach Scout (level 5)"},
        ],
        "ft_count": 4,
        "ft_total": 6,
        "ft_complete": False,
        "strongbox_claimed": False,
        "contracts_unlocked": True,
        "contracts": [
            {"id": 1, "label": "Survey Sweep", "desc": "Survey 8 territories this week",
             "objective": "survey_sweep", "target": 8, "cost": 60, "progress": 5,
             "purchased": 1, "completed": 0, "reward_type": "survey_marks",
             "reward_amount": 10, "reward_label": "10 🪙"},
            {"id": 2, "label": "Long Shot", "desc": "Survey from 5+ miles away",
             "objective": "long_shot", "target": 5, "cost": 180, "progress": 0,
             "purchased": 0, "completed": 0, "reward_type": "relic",
             "reward_amount": 1, "reward_label": "Random Relic"},
            {"id": 3, "label": "Grand Traverse", "desc": "Survey 2+ sq mi of territory this week",
             "objective": "grand_traverse", "target": 2, "cost": 200, "progress": 2,
             "purchased": 1, "completed": 1, "reward_type": "attack_epic",
             "reward_amount": 1, "reward_label": "⚔️ Blasting Powder IV",
             "reward_granted": 0, "pending_supply_drop": True},
        ],
        "contract_reset_ts": int(time.time()) + 2 * 86400,
        "active_title": "Boundless",
        "commission_steps": [
            {"name": "Field Training", "status": "done",
             "requirement": "Complete 6 training objectives (6/6)",
             "unlocks": "Society Strongbox · 📦100 + Wardstone",
             "tip": "Guided objectives that walk you through the core survey loop. "
                    "Finish them all to crack open the Society Strongbox — a one-time "
                    "starter cache of provisions and marks."},
            {"name": "Scout Commission", "status": "done",
             "requirement": "Reach Scout (rank 5)", "unlocks": "Expedition Contracts",
             "tip": "Earn XP from surveys to make Scout. Reaching it opens Expedition "
                    "Contracts — rotating objectives that pay bonus provisions and marks."},
            {"name": "Frontier Merchant", "status": "locked",
             "requirement": "Upgrade to Lodge (camp 5)",
             "unlocks": "Frontier Merchant · weekly relic shop",
             "tip": "Spend provisions and field notes to grow Base Camp. At Lodge the "
                    "Frontier Merchant opens — a weekly shop for trading marks for relics."},
            {"name": "Charter License", "status": "current",
             "requirement": "Reach rank 8 + Field Camp (camp 3)",
             "unlocks": "Charter Survey Posts · +3 🪙 first-charter bonus",
             "tip": "The big one. Reach the required rank and Base Camp level to charter "
                    "Survey Posts — permanent outposts you plant at real-world sites 3+ mi "
                    "from home that boost nearby surveys. Also opens optional PvP."},
        ],
        "commission_done": 2,
        "commission_total": 4,
    })


@app.get("/radio")
async def radio(request: Request):
    return templates.TemplateResponse(request, "spyglass.html", {
        **COMMON,
        "nav_active": "radio",
        "player": MOCK_PLAYER,
        "companion_connected": True,
        "selected_node": "node_abc123",
        "nodes": [
            {"key": "node_abc123", "name": "Relay-Alpha"},
            {"key": "node_def456", "name": "Relay-Bravo"},
        ],
        "dispatch": {"message": "Society scouts report unusual ley line activity near the southern ridges."},
        "shortcuts": {"enabled": True, "survey": "LoRa Survey", "charter": "LoRa Charter", "reinforce": "LoRa Reinforce"},
        "home_lat": HOME_LAT,
        "home_lon": HOME_LNG,
        "hex_count": len(MOCK_HEXES),
        "post_count": 2,
        # Forced on in preview so the "Charter Available" Locate readout is demoable
        # (MOCK_PLAYER is only rank 3 / camp 2 and wouldn't qualify in the real app).
        "charter_ready": True,
        "charter_min_miles": 3.0,
        "furthest_survey": {"lat": 33.490, "lon": -112.045, "distance_miles": 3.4},
        "telemetry_timeout": PREVIEW_TELEMETRY_TIMEOUT,
    })


# Short window so the draining signal bar is watchable in preview (real app: 45s).
PREVIEW_TELEMETRY_TIMEOUT = 8


@app.get("/api/travel-mode")
async def preview_get_travel_mode():
    # Read-only routing diagnostics (routing is automatic now — no settable mode).
    return {"has_route": False, "last_contact_ts": None, "last_contact_age": None}


@app.post("/api/survey")
@app.post("/api/charter")
@app.post("/api/reinforce")
async def preview_command(request: Request):
    """Simulate a live mesh round-trip: push cmd/triangulate/retry/fix events
    onto the SSE stream so the Radio signal bar can be exercised end to end."""
    import asyncio

    cmd = request.url.path.rsplit("/", 1)[-1]
    auto = False
    try:
        body = await request.json()
        auto = bool(body.get("auto"))
    except Exception:
        pass

    async def script():
        t = PREVIEW_TELEMETRY_TIMEOUT
        async def emit(type_, data=None):
            await _sse_push({"type": type_, "ts": time.time(), "data": data or {}})
        await emit("cmd_received", {"command": cmd})
        await asyncio.sleep(0.4)
        await emit("gps_request", {"command": cmd})
        await asyncio.sleep(0.4)
        await emit("gps_triangulating", {"command": cmd, "attempt": 1, "mode": "last_path",
                                         "deadline_ts": time.time() + t})
        await asyncio.sleep(t + 1)  # let attempt 1 drain to "listening"
        await emit("gps_triangulating", {"command": cmd, "attempt": 2, "mode": "flood",
                                         "fallback": True, "deadline_ts": time.time() + t})
        await asyncio.sleep(3)  # answer partway through the retry
        await emit("gps_fix", {"command": cmd, "lat": 33.49, "lon": -112.05})
        await asyncio.sleep(0.6)
        if cmd == "survey":
            await emit("survey", {
                "hex_name": "Verdant Hollow", "xp": 105, "provisions": 33,
                "discovery": True, "first_today": True, "first_survey_bonus": 1})
            if auto:
                await emit("autosurvey_logged", {})
        else:
            await emit(cmd, {"hex_name": "Verdant Hollow", "xp": 42, "provisions": 15,
                             "post_name": "Northwatch", "territory": "Verdant Hollow", "level": 2})

    asyncio.create_task(script())
    return JSONResponse({"ok": True})


@app.get("/outposts")
async def outposts(request: Request):
    mock_posts = [
        {
            "id": 1, "name": "Hilltop Watch", "level": 2, "hex_name": "Sunset Bluff",
            "hex_id": "hex_05", "distance": 3.2, "prov_per_day": 4, "prov_per_day_full": 4, "renown_per_day": 27, "renown_per_day_full": 27, "renown_base": 6, "renown_age_bonus": 21,
            "ruin_status": "stable", "income_factor": 1.0, "full_days_left": 6.0,
            "days_until_ruined": 13.0, "upkeep_total_days": 17,
            "ruin_frozen": False, "warded": False, "upgrade_cost": 30,
        },
        {
            "id": 3, "name": "Lone Mesa", "level": 3, "hex_name": "Windbitten Rise",
            "hex_id": "hex_07", "distance": 5.1, "prov_per_day": 3, "prov_per_day_full": 6, "renown_per_day": 7, "renown_per_day_full": 13, "renown_base": 9, "renown_age_bonus": 4,
            "ruin_status": "fading", "income_factor": 0.5, "full_days_left": 0.0,
            "days_until_ruined": 3.5, "upkeep_total_days": 17,
            "ruin_frozen": False, "warded": False, "upgrade_cost": 80,
        },
        {
            "id": 2, "name": "River Bend", "level": 1, "hex_name": "Dusty Creek Hollow",
            "hex_id": "hex_02", "distance": 1.8, "prov_per_day": 0, "prov_per_day_full": 2, "renown_per_day": 0, "renown_per_day_full": 33, "renown_base": 3, "renown_age_bonus": 30,
            "ruin_status": "warded", "income_factor": 0.0,
            "ruin_frozen": True, "warded": True, "warded_days_left": 11.4, "frozen_days_left": 11.4,
            "upgrade_cost": 15,
        },
    ]
    return templates.TemplateResponse(request, "outposts.html", {
        **COMMON,
        "nav_active": "outposts",
        "pvp_enabled": True,
        "player": MOCK_PLAYER,
        "camp_name": "Outpost",
        "camp_info": {"mult": 1.1},
        "next_camp_name": "Lodge",
        "next_camp_info": {"prov": 50, "mult": 1.2, "perk": "charter_license"},
        "next_camp_level": 3,
        "post_limit": 3,
        "total_prov_per_day": 5,
        "renown_per_level": 3,
        "renown_age_rate": 0.5,
        "camp_perk_descriptions": {
            "charter_license": "Unlocks Charter License",
            "merchant": "Unlocks Frontier Merchant",
            "relay_boost": "Relay Signal Boost",
            "extra_contract": "Extra Contract Slot",
            "wardstone_craft": "Wardstone Crafting",
            "relic_mastery": "Relic Mastery",
        },
        "posts": mock_posts,
        "charter_license": True,
        "charter_prov_cost": 10,
        "charter_mark_cost": 3,
        "wardstone_relic": {"id": 99},
        "wardstone_count": 2,
        "raid_in_flight": False,
        "merchant_unlocked": True,
        "merchant_min_camp": 5,
        "mp_registered": True,
        "supply_pending": 6,
        "supply_next_at": int(time.time()) + 1900,
        "supply_interval_min": 60,
        "supply_runs": [
            {"ran_at": int(time.time()) - 300, "survey_count": 9, "drop_count": 3,
             "drops": ["attack_common", "attack_common", "probe"],
             "summary": "2× Blasting Powder I, 1× Scout"},
            {"ran_at": int(time.time()) - 3900, "survey_count": 4, "drop_count": 0,
             "drops": [], "summary": "no items"},
            {"ran_at": int(time.time()) - 7500, "survey_count": 12, "drop_count": 1,
             "drops": ["defense_uncommon"], "summary": "1× Defense (Uncommon)"},
        ],
    })


@app.get("/api/telemetry-stats")
async def preview_telemetry_stats():
    return {
        "entries": [],
        "stats": {
            "total": 42, "successes": 33, "failures": 9,
            "avg_s": 18.4, "median_s": 16.0, "min_s": 6.2, "p95_s": 34.1, "max_s": 38.7,
            "histogram": {
                "labels": ["1-10s", "10-15s", "15-20s", "20-25s", "25-30s", "30-35s"],
                "values": [5, 8, 6, 9, 3, 2],
            },
            "by_route": {
                "last_path": {"attempts": 24, "successes": 20, "rate": 83},
                "flood": {"attempts": 18, "successes": 13, "rate": 72},
            },
            "routing_model": {
                "learned_d_mi": 0.42,
                "samples": 18,
                "min_samples": 12,
                "using_default": False,
                "default_d_mi": 0.5,
            },
        },
    }


@app.post("/api/companion/test-message")
async def preview_test_message(request: Request):
    import asyncio
    body = await request.json()
    await asyncio.sleep(1.5)  # simulate the mesh round-trip
    name = "Relay-Alpha" if body.get("target") == "node_abc123" else "Ridge-Runner"
    return {"ok": True, "message": f"Delivered to {name} — ACK received."}


@app.get("/settings")
async def settings(request: Request):
    return templates.TemplateResponse(request, "settings.html", {
        **COMMON,
        "nav_active": "settings",
        "config": MOCK_CONFIG,
        "player": MOCK_PLAYER,
        "companion": MOCK_COMPANION,
        "spyglasses": [
            {"key": "node_abc123", "name": "Relay-Alpha"},
            {"key": "node_def456", "name": "Ridge-Runner"},
        ],
        "default_spyglass": "node_abc123",
        "shortcuts": {"enabled": True, "survey": "LoRa Survey", "charter": "LoRa Charter", "reinforce": ""},
        "backups": [
            {"filename": "explorer-20260629-120000.db", "size_kb": 48, "time_ago": "6h ago"},
            {"filename": "explorer-20260628-120000.db", "size_kb": 44, "time_ago": "1d ago"},
        ],
        "player_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
        "pvp_enabled": False,
        "mp_registered": True,
        "pvp_ready": False,
        "pvp_ready_reason": "Charter at least one Survey Post before enabling PvP",
        "webhook_url": "https://discord.com/api/webhooks/example",
        "app_version": "0.2.0",
        "update_check_enabled": False,
        "update_check": {
            "ok": True, "update_available": True, "latest_version": "v0.3.0",
            "url": "https://github.com/hornofabraxas/lora-the-explorer/releases/tag/v0.3.0",
            "checked_at": 0, "current_version": "0.2.0",
        },
    })


@app.get("/stats")
async def stats(request: Request):
    from lora_explorer.web.routes import build_postcard_card
    postcard_cards = [
        build_postcard_card("Strider", 2),
        build_postcard_card("Trailblazer", 3),
        build_postcard_card("Relentless", 1),
        build_postcard_card("Steadfast", 0),
        build_postcard_card("Boundless", 5),
    ]
    earned_count = sum(pc["earned_stars"] for pc in postcard_cards)
    return templates.TemplateResponse(request, "stats.html", {
        **COMMON,
        "nav_active": "achievements",
        "player": MOCK_PLAYER,
        "hex_count": 56,
        "survey_count": 82,
        "area_sq_mi": 17.1,
        "max_distance": 4.7,
        "streak": 5,
        "post_count": 2,
        "postcards_earned": earned_count,
        "postcards_total": 25,
        "postcard_cards": postcard_cards,
        "active_title": "Boundless",
        "recent_surveys": [
            {"hex_id": "hex_01", "hex_name": "Crimson Mesa Outlook", "is_discovery": True, "distance_miles": 1.1, "snr": 8.5, "xp_earned": 105, "provisions_earned": 33, "marks_earned": 2, "time_ago": "2h ago",
             "breakdown": {
                 "xp": {"base": 20, "distance_mult": 1.17, "camp_mult": 1.10, "post_mult": 1.00, "momentum_mult": 1.05, "subtotal": 27, "event_bonus": 0, "discovery_bonus": 75, "total": 105},
                 "provisions": {"base": 16, "distance_bonus": 1, "post_mult": 1.00, "subtotal": 17, "event_bonus": 0, "discovery_bonus": 15, "total": 33},
                 "marks": {"discovery": 1, "event_bonus": 0, "first_survey": 1, "total": 2}}},
            {"hex_id": "hex_03", "hex_name": "Ironwood Basin", "is_discovery": False, "distance_miles": 1.5, "snr": 12.1, "xp_earned": 25, "provisions_earned": 17, "marks_earned": 0, "time_ago": "1d ago",
             "breakdown": {
                 "xp": {"base": 20, "distance_mult": 1.22, "camp_mult": 1.10, "post_mult": 1.00, "momentum_mult": 1.00, "subtotal": 26, "event_bonus": -1, "discovery_bonus": 0, "total": 25},
                 "provisions": {"base": 16, "distance_bonus": 1, "post_mult": 1.00, "subtotal": 17, "event_bonus": 0, "discovery_bonus": 0, "total": 17},
                 "marks": {"discovery": 0, "event_bonus": 0, "first_survey": 0, "total": 0}}},
            {"hex_id": "hex_08", "hex_name": "Saguaro Heights", "is_discovery": True, "distance_miles": 3.4, "snr": 6.2, "xp_earned": 97, "provisions_earned": 35, "marks_earned": 1, "time_ago": "2d ago",
             "breakdown": {
                 "xp": {"base": 20, "distance_mult": 1.51, "camp_mult": 1.10, "post_mult": 1.00, "momentum_mult": 1.00, "subtotal": 33, "event_bonus": 0, "discovery_bonus": 75, "total": 97},
                 "provisions": {"base": 16, "distance_bonus": 4, "post_mult": 1.00, "subtotal": 20, "event_bonus": 0, "discovery_bonus": 15, "total": 35},
                 "marks": {"discovery": 1, "event_bonus": 0, "first_survey": 0, "total": 1}}},
        ],
    })


@app.get("/multiplayer")
async def multiplayer_page(request: Request):
    import time
    now = int(time.time())
    # Preview the join-gate states: ?registered=0 shows the register screen;
    # add &charter=1 to preview the "charter a post" step, else the earlier
    # "earn your Charter License" step. Defaults to the registered warfront.
    qp = request.query_params
    registered = qp.get("registered") != "0"
    has_charter = qp.get("charter") == "1"
    return templates.TemplateResponse(request, "multiplayer.html", {
        **COMMON,
        "nav_active": "multiplayer",
        "enabled": True,
        "registered": registered,
        "pvp_ready": False,
        "pvp_ready_reason": (
            "Charter at least one Survey Post before enabling PvP"
            if has_charter else
            "Earn your Charter License first (reach the Charter checkpoint)"
        ),
        "charter_license": has_charter,
        "player_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
        "pvp_enabled": True,
        "attack_window_active": True,
        "items": [
            {"id": "probe_1", "item_type": "probe"},
            {"id": "probe_2", "item_type": "probe"},
            {"id": "attack_common_1", "item_type": "attack_common"},
            {"id": "attack_uncommon_1", "item_type": "attack_uncommon"},
            {"id": "defense_common_1", "item_type": "defense_common"},
            {"id": "defense_rare_1", "item_type": "defense_rare"},
        ],
        "item_counts": {"probe": 2, "attack_common": 1, "attack_uncommon": 1, "defense_common": 1, "defense_rare": 1},
        "merchant_unlocked": True,
        "merchant_reset_ts": int(time.time()) + 3 * 86400 + 4 * 3600,
        "war_chest": [
            {"type": "probe", "label": "Scout", "name_short": "Scout", "tier": "", "emoji": "🔭", "rarity": "common", "kind": "probe",
             "count": 2, "buy": {"type": "probe", "price": 60, "currency": "provisions", "symbol": "📦", "purchased": False, "affordable": True},
             "salvage": {"value": 30, "symbol": "📦"}},
            {"type": "attack_common", "label": "Blasting Powder I", "name_short": "Powder", "tier": "I", "emoji": "⚔️", "rarity": "common", "kind": "attack",
             "count": 3, "buy": {"type": "attack_common", "price": 8, "currency": "marks", "symbol": "🪙", "purchased": True, "affordable": True},
             "salvage": {"value": 4, "symbol": "🪙"}},
            {"type": "attack_uncommon", "label": "Blasting Powder II", "name_short": "Powder", "tier": "II", "emoji": "⚔️", "rarity": "uncommon", "kind": "attack",
             "count": 1, "buy": {"type": "attack_uncommon", "price": 15, "currency": "marks", "symbol": "🪙", "purchased": False, "affordable": False},
             "salvage": {"value": 7, "symbol": "🪙"}},
            {"type": "defense_common", "label": "Bulwark I", "name_short": "Bulwark", "tier": "I", "emoji": "🛡️", "rarity": "common", "kind": "defense",
             "count": 1, "buy": {"type": "defense_common", "price": 8, "currency": "marks", "symbol": "🪙", "purchased": False, "affordable": True},
             "salvage": {"value": 4, "symbol": "🪙"}},
            {"type": "defense_rare", "label": "Bulwark III", "name_short": "Bulwark", "tier": "III", "emoji": "🛡️", "rarity": "rare", "kind": "defense",
             "count": 1, "buy": None, "salvage": {"value": 15, "symbol": "🪙"}},
        ],
        "leaderboard": [
            {"player_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", "display_name": "Wanderer", "post_count": 3, "total_renown": 1250, "renown_per_day": 18, "active_title": "Boundless"},
            {"player_id": "x9y8z7w6v5u4t3s2r1q0p9o8n7m6l5k4", "display_name": "Pathfinder", "post_count": 2, "total_renown": 870, "active_title": "Reaver", "post_tokens": ["8a2a1072b5dffff", "8a2a1072b5effff"], "posts": [{"post_token": "8a2a1072b5dffff", "name": "Ravensperch Watch"}, {"post_token": "8a2a1072b5effff", "name": "Saltmarsh Redoubt"}]},
            {"player_id": "f1e2d3c4b5a6f7e8d9c0b1a2f3e4d5c6", "display_name": "Trailblazer", "post_count": 1, "total_renown": 340, "active_title": None},
            {"player_id": "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7", "display_name": "Scout Rhea", "post_count": 4, "total_renown": 1610, "active_title": "Warlord"},
            {"player_id": "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8", "display_name": "Compass Kade", "post_count": 2, "total_renown": 620, "active_title": None},
            # Unscouted but with posts on file — exercises the '?' rows in the
            # shared Warfront table. Ridge Runner below keeps the legacy
            # post_tokens-only shape (no `posts`) that older payloads sent.
            {"player_id": "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9", "display_name": "Marsh Vell", "post_count": 3, "total_renown": 990, "active_title": "Steadfast", "post_tokens": ["8a2a1072b5a1ffff", "8a2a1072b5a2ffff", "8a2a1072b5a3ffff"], "posts": [{"post_token": "8a2a1072b5a1ffff", "name": "Reedwater Post"}, {"post_token": "8a2a1072b5a2ffff", "name": ""}, {"post_token": "8a2a1072b5a3ffff", "name": "Fenlight Station"}]},
            {"player_id": "e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0", "display_name": "Dune Warden", "post_count": 5, "total_renown": 2040, "active_title": "Vanguard"},
            {"player_id": "f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1", "display_name": "Ridge Runner", "post_count": 1, "total_renown": 210, "active_title": None, "post_tokens": ["8a2a1072b5b1ffff"]},
        ],
        "available_titles": ["Boundless", "Trailblazer", "Reaver", "Pathfinder"],
        "active_title": "Boundless",
        "title_meanings": TITLE_MEANINGS,
        "hex_name": lambda h: f"Territory {h[-4:].upper()}",
        "cached_scouts": {
            "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7": [
                {"post_token": "8a2a1072b591ffff", "name": "Ironhold Bastion", "level": 4, "age_days": 22, "hp": 90, "max_hp": 120, "defense_reduction": 0.25},
                {"post_token": "8a2a1072b593ffff", "name": "", "level": 2, "age_days": 6, "hp": 40, "max_hp": 50, "defense_reduction": 0.0},
            ],
            "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8": [
                {"post_token": "8a2a1072b5a1ffff", "name": "Kade's Rest", "level": 2, "age_days": 9, "hp": 45, "max_hp": 50, "defense_reduction": 0.0},
            ],
            "e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0": [
                {"post_token": "8a2a1072b5c1ffff", "name": "Dunewatch", "level": 5, "age_days": 40, "hp": 150, "max_hp": 160, "defense_reduction": 0.30},
            ],
        },
        "scout_ages": {
            "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7": "3h ago",
            "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8": "just now",
            "e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0": "1d ago",
        },
        # Fuzzed distances (nearest 50mi) revealed by scouting; everyone else is "?".
        "distance_info": {
            "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7": _preview_distance(True, 150),
            "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8": _preview_distance(True, 50),
            "e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0": _preview_distance(True, 800),
        },
        "defense_posts": {
            "8a2a1072b59ffff": {"defense_item": "defense_common", "defense_value": 10, "hp": 85, "max_hp": 100,
                                "besieged_until": now + 3600,
                                "incoming_raids": [{"eta_seconds": 720, "threat": "raze"},
                                                   {"eta_seconds": 2400, "threat": "heavy"}]},
            "8a2a1072b5bffff": {"defense_item": None, "defense_value": 0, "hp": 50, "max_hp": 50,
                                "incoming_raids": [{"eta_seconds": 5400, "threat": "hold"}]},
        },
        "now_ts": now,
        # One rival post on cooldown from a recent raid — exercises the
        # Warfront countdown badge in place of the Attack button.
        "raid_cooldowns": {"8a2a1072b5c1ffff": now + 5 * 3600 + 1200},
        "can_attack": True,
        "active_raid": {
            "raid_id": "raid-preview-1",
            "attacker_name": "Wanderer",
            "target_player_name": "Pathfinder",
            "target_post_token": "8a2a1072b5dffff",
            "target_post_name": "Ravensperch Watch",
            "item_types": ["attack_uncommon", "attack_common"],
            "raw_power": 50,
            "dispatched_at": now - 600,
            "arrives_at": now + 4200,  # ~70 min out
            "status": "in_flight",
            # Snapshot of what the picker projected at dispatch (local-only).
            "projection": {"scouted": True, "projected": "raze", "effective_damage": 42,
                           "target_hp": 40},
        },
        "last_sync_at": now - 120,
    })


@app.get("/api/multiplayer/items")
async def multiplayer_items_api():
    return JSONResponse({
        "items": [
            {"id": "probe_1", "item_type": "probe", "used": 0},
            {"id": "probe_2", "item_type": "probe", "used": 0},
            {"id": "attack_common_1", "item_type": "attack_common", "used": 0},
            {"id": "attack_common_2", "item_type": "attack_common", "used": 0},
            {"id": "attack_common_3", "item_type": "attack_common", "used": 0},
            {"id": "attack_uncommon_1", "item_type": "attack_uncommon", "used": 0},
            {"id": "attack_rare_1", "item_type": "attack_rare", "used": 0},
            {"id": "defense_common_1", "item_type": "defense_common", "used": 0},
            {"id": "defense_rare_1", "item_type": "defense_rare", "used": 0},
        ],
    })


@app.get("/api/multiplayer/defense")
async def multiplayer_defense_api():
    # Keyed to the outposts mock hex_ids so the post cards render defense.
    return JSONResponse({
        "ok": True,
        "posts": [
            {"post_token": "hex_05", "defense_item": "defense_common", "defense_pct": 0.08,
             "hp": 72, "max_hp": 100, "boost_hp": 40, "effective_hp": 122, "effective_max_hp": 109,
             "active_boosts": 1,
             "incoming_raids": [{"eta_seconds": 480, "threat": "heavy"}]},
            {"post_token": "hex_07", "defense_item": None, "defense_pct": 0.0,
             "hp": 175, "max_hp": 175, "boost_hp": 0, "effective_hp": 175, "effective_max_hp": 175,
             "active_boosts": 0},
            {"post_token": "hex_02", "defense_item": None, "defense_pct": 0.0,
             "hp": 50, "max_hp": 50, "boost_hp": 0, "effective_hp": 50, "effective_max_hp": 50,
             "active_boosts": 0},
        ],
    })


@app.post("/api/multiplayer/install-item")
async def multiplayer_install_item():
    return JSONResponse({"ok": True, "defense_value": 10, "hp": 100, "max_hp": 100})


@app.post("/api/multiplayer/defend/boost")
async def multiplayer_defend_boost():
    return JSONResponse({"ok": True, "boost_hp_added": 20, "total_boost_hp": 20})


@app.post("/api/multiplayer/restore-hp")
async def multiplayer_restore_hp():
    return JSONResponse({"ok": True, "new_hp": 100, "max_hp": 100})


@app.get("/help")
async def help_page(request: Request):
    return templates.TemplateResponse(request, "help.html", {
        **COMMON, "nav_active": "help",
        "diagnostics": {
            "version": "0.9.3", "install": "docker",
            "os": "Linux 6.6.0 (x86_64)", "python": "3.12.4",
            "connection_type": "wifi", "companion_connected": True,
            "multiplayer_registered": False, "pvp_enabled": False,
        },
        "issue_url": "https://github.com/hornofabraxas/lora-the-explorer/issues/new",
        "discord_url": "https://discord.gg/EHXemsA2SS",
    })


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=1493)
