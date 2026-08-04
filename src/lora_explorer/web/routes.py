import asyncio
import json
import logging
import math
import os
import platform
import time
from datetime import datetime, timezone, timedelta

import h3
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, FileResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from .. import __version__
from .. import update_check as update_check_module
from ..game.backup import list_backups, create_backup, restore_backup
from .auth import (
    hash_password, verify_password, create_session_cookie,
    _get_secret, COOKIE_NAME, SESSION_MAX_AGE,
    is_rate_limited, record_failed_attempt, clear_failed_attempts,
)
from . import oidc as oidc_module

log = logging.getLogger(__name__)

OIDC_FLOW_COOKIE = "oidc_flow"
OIDC_FLOW_MAX_AGE = 600  # 10 minutes
from ..game.engine import (
    RANK_THRESHOLDS, BASE_CAMP_TABLE, POST_UPGRADE_COST,
    MAX_POST_LEVEL, CHARTER_PROVISION_COST,
    CHARTER_MARK_COST, CHARTER_MIN_DISTANCE_MILES,
    RUIN_RAMP_DAYS, RENOWN_PER_DAY_PER_LEVEL, RENOWN_AGE_BONUS_PER_DAY,
    upkeep_grace_days, ruin_income_factor,
    CHARTER_MIN_LEVEL, CHARTER_MIN_CAMP,
    BURIED_CACHE_AMOUNT, WARD_MIN_DAYS, clamp_ward_days,
    FIELD_TRAINING_CLASS, FIELD_TRAINING_POSTCARDS,
    STRONGBOX_PROVISIONS, CONTRACT_OBJECTIVES, CONTRACT_ITEM_REWARD_TYPES,
    CAMP_PERK_DESCRIPTIONS,
    max_posts_for_camp, rank_name, camp_name,
    get_daily_dispatch, generate_analysts_report,
    _week_start_utc, _contract_period_start_utc,
    CONTRACT_PERIOD_DAYS, CONTRACTS_MIN_LEVEL,
    multiplayer_item_name,
)
from ..game.hex_names import hex_name
from ..paths import install_method
from ..multiplayer.manager import BUNDLE_INTERVAL
from ..radio.meshcore_adapter import TELEMETRY_TIMEOUT_FLOOD

router = APIRouter()

_SW_PATH = os.path.join(os.path.dirname(__file__), "static", "sw.js")


@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    # Served from root so the worker's scope covers the whole app. Never cached
    # by the browser itself, so SW updates are picked up promptly.
    return FileResponse(
        _SW_PATH,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


def _item_label(item_type: str) -> str:
    """Human-readable name for a multiplayer drop type, e.g.
    'attack_rare' -> 'Blasting Powder III', 'probe' -> 'Scout'."""
    return multiplayer_item_name(item_type)["full"]


def _supply_run_summary(drops: list[str]) -> str:
    """'2× Blasting Powder I, 1× Scout' — the item breakdown for one run."""
    if not drops:
        return "no items"
    counts: dict[str, int] = {}
    for t in drops:
        counts[t] = counts.get(t, 0) + 1
    return ", ".join(f"{n}× {_item_label(t)}" for t, n in counts.items())


STARRED_POSTCARD_CLASSES = ["Strider", "Trailblazer", "Relentless", "Steadfast", "Boundless"]

MERCHANT_RELIC_PRICES = {
    "vigor_tonic": 100,
    "wardstone": 150,
}
# Frontier Merchant unlocks at camp 4 (Outpost). Moved down from camp 5 in the
# 2026-07-22 rework so the game's best weekly ritual reaches the casual/time-poor
# track sooner; the "merchant" perk lives on BASE_CAMP_TABLE[4] to match.
MERCHANT_MIN_CAMP_LEVEL = 4

# Salvaging an unwanted relic reclaims a fraction of its merchant value in
# provisions. Buried Caches are intentionally excluded — they already pay out
# provisions when opened, so there's nothing to reclaim (you'd just open it).
RELIC_SALVAGE_RATE = 0.5
RELIC_SALVAGE_VALUE = {
    relic_type: int(price * RELIC_SALVAGE_RATE)
    for relic_type, price in MERCHANT_RELIC_PRICES.items()
}

POSTCARD_DESCRIPTIONS = {
    "Strider": {
        1: "Survey at 5+ miles",
        2: "Survey at 10+ miles",
        3: "Survey at 15+ miles",
        4: "Survey at 25+ miles",
        5: "Survey at 35+ miles",
    },
    "Trailblazer": {
        1: "Discover 5 territories",
        2: "Discover 10 territories",
        3: "Discover 25 territories",
        4: "Discover 50 territories",
        5: "Discover 100 territories",
    },
    "Relentless": {
        1: "10-day survey streak",
        2: "25-day survey streak",
        3: "50-day survey streak",
        4: "100-day survey streak",
        5: "200-day survey streak",
    },
    "Steadfast": {
        1: "Hold a post for 7 days",
        2: "Hold a post for 14 days",
        3: "Hold a post for 30 days",
        4: "Hold a post for 60 days",
        5: "Hold a post for 90 days",
    },
    "Boundless": {
        1: "Survey 5 sq mi total",
        2: "Survey 15 sq mi total",
        3: "Survey 50 sq mi total",
        4: "Survey 100 sq mi total",
        5: "Survey 200 sq mi total",
    },
}


def build_postcard_card(pc_class: str, earned_stars: int) -> dict:
    """One postcard's view model. Achievement best-practice: the card face shows
    the best ACHIEVED metric (the highest earned tier's description); the full
    tier ladder and the next target live in the ⓘ tooltip. Shared by the live
    stats route and the preview server so both render identically."""
    descs = POSTCARD_DESCRIPTIONS.get(pc_class, {})
    earned_desc = descs.get(earned_stars, "") if earned_stars >= 1 else ""
    next_desc = descs.get(earned_stars + 1, "") if earned_stars < 5 else ""
    # Plain-text ladder for the site-wide [data-tip] tooltip: ✓ done / → next /
    # • locked, one rung per line.
    tip_lines = []
    for s in range(1, 6):
        if s <= earned_stars:
            mark = "✓"
        elif s == earned_stars + 1:
            mark = "→"
        else:
            mark = "•"
        tip_lines.append(f"{mark} {'★' * s} {descs.get(s, '')}")
    return {
        "class": pc_class,
        "earned_stars": earned_stars,
        "earned_desc": earned_desc,
        "next_desc": next_desc,
        "tooltip": "\n".join(tip_lines),
        "complete": earned_stars >= 5,
    }


def _next_rank_info(current_rank: int) -> dict | None:
    if current_rank >= 50:
        return None
    lvl = current_rank + 1
    return {"level": lvl, **RANK_THRESHOLDS[lvl]}


def _flash_redirect(path: str, msg: str, category: str = "info") -> RedirectResponse:
    import urllib.parse
    qs = urllib.parse.urlencode({"flash_msg": msg, "flash_type": category})
    response = RedirectResponse(f"{path}?{qs}", status_code=303)
    return response


def _ruin_state(last_tended_at: int, grace: float) -> dict:
    """Income-decay state of a post for the Outposts UI.

    Returns the current income factor (0-1), a coarse state, and the day counts
    the template needs — no raw countdown, the bar/chip carry the meaning.
    """
    age_days = max(0.0, (time.time() - last_tended_at) / 86400)
    factor = ruin_income_factor(age_days, grace)
    ramp_end = grace + RUIN_RAMP_DAYS
    if factor >= 1.0:
        state = "stable"
    elif factor <= 0.0:
        state = "ruined"
    else:
        state = "fading"
    return {
        "factor": factor,
        "state": state,
        "age_days": age_days,
        "full_days_left": max(0.0, grace - age_days),
        "days_until_ruined": max(0.0, ramp_end - age_days),
    }


def _format_time_ago(ts: int) -> str:
    elapsed = int(time.time()) - ts
    if elapsed < 60:
        return "just now"
    if elapsed < 3600:
        m = elapsed // 60
        return f"{m}m ago"
    if elapsed < 86400:
        h = elapsed // 3600
        return f"{h}h ago"
    d = elapsed // 86400
    return f"{d}d ago"


async def _get_player_key(db) -> str | None:
    player = await db.get_first_player()
    return player["key"] if player else None


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
    manager = request.app.state.multiplayer_manager
    ctx.setdefault("update_required", manager.update_required if manager else False)
    ctx.setdefault("min_client_version", manager.min_client_version if manager else None)
    ctx.setdefault("app_version", __version__)
    if "currency" not in ctx:
        player = ctx.get("player")
        if not player:
            db = request.app.state.db
            player = await db.get_first_player()
        if player:
            ctx["currency"] = {
                "provisions": int(player.get("provisions", 0)),
                "survey_marks": int(player.get("survey_marks", 0)),
            }
    return request.app.state.templates.TemplateResponse(request, name, ctx)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    db = request.app.state.db
    has_password = await db.get_setting("password_hash") is not None
    has_oidc = await db.get_oidc_config() is not None
    if not has_password and not has_oidc:
        return RedirectResponse("/setup", status_code=302)
    return request.app.state.templates.TemplateResponse(
        request, "login.html", {
            "error": request.query_params.get("error"),
            "has_password": has_password,
            "has_oidc": has_oidc,
        }
    )


@router.post("/login")
async def login_submit(request: Request):
    db = request.app.state.db
    form = await request.form()
    password = form.get("password", "")
    password_hash = await db.get_setting("password_hash")
    has_oidc = await db.get_oidc_config() is not None

    client_ip = request.client.host if request.client else "unknown"
    if is_rate_limited(client_ip):
        return request.app.state.templates.TemplateResponse(
            request, "login.html",
            {"error": "Too many failed attempts. Please wait 5 minutes.",
             "has_password": True, "has_oidc": has_oidc},
            status_code=429,
        )

    if not password_hash or not verify_password(password, password_hash):
        record_failed_attempt(client_ip)
        return request.app.state.templates.TemplateResponse(
            request, "login.html",
            {"error": "Incorrect password.",
             "has_password": True, "has_oidc": has_oidc},
            status_code=401,
        )

    clear_failed_attempts(client_ip)
    secret = _get_secret(request.app.state.config["db_path"])
    cookie = create_session_cookie(secret)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME, cookie,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    db = request.app.state.db
    has_password = await db.get_setting("password_hash") is not None
    has_oidc = await db.get_oidc_config() is not None
    has_any_auth = has_password or has_oidc
    if has_any_auth:
        radio = request.app.state.radio
        if radio and radio.configured and radio._mc is not None:
            return RedirectResponse("/", status_code=302)
    return await _template(request, "setup.html", {
        "nav_active": "setup",
        "needs_auth": not has_any_auth,
    })


@router.post("/setup/password")
async def setup_password(request: Request):
    db = request.app.state.db
    has_password = await db.get_setting("password_hash") is not None
    has_oidc = await db.get_oidc_config() is not None
    if has_password or has_oidc:
        return RedirectResponse("/setup", status_code=302)
    form = await request.form()
    password = form.get("password", "").strip()
    confirm = form.get("confirm", "").strip()
    if len(password) < 8:
        return await _template(request, "setup.html", {
            "nav_active": "setup", "needs_auth": True,
            "flash_msg": "Password must be at least 8 characters.", "flash_type": "error",
        })
    if password != confirm:
        return await _template(request, "setup.html", {
            "nav_active": "setup", "needs_auth": True,
            "flash_msg": "Passwords do not match.", "flash_type": "error",
        })
    await db.set_setting("password_hash", hash_password(password))
    secret = _get_secret(request.app.state.config["db_path"])
    cookie = create_session_cookie(secret)
    response = RedirectResponse("/setup", status_code=303)
    response.set_cookie(
        COOKIE_NAME, cookie,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/setup/oidc")
async def setup_oidc(request: Request):
    db = request.app.state.db
    has_password = await db.get_setting("password_hash") is not None
    has_oidc = await db.get_oidc_config() is not None
    if has_password or has_oidc:
        return RedirectResponse("/setup", status_code=302)
    form = await request.form()
    config = {
        "issuer_url": form.get("issuer_url", "").strip().rstrip("/"),
        "client_id": form.get("client_id", "").strip(),
        "client_secret": form.get("client_secret", "").strip(),
    }
    if not config["issuer_url"] or not config["client_id"]:
        return await _template(request, "setup.html", {
            "nav_active": "setup", "needs_auth": True,
            "flash_msg": "Issuer URL and Client ID are required.", "flash_type": "error",
        })
    meta = await oidc_module.validate_oidc_config(config)
    if not meta:
        return await _template(request, "setup.html", {
            "nav_active": "setup", "needs_auth": True,
            "flash_msg": "Could not reach OIDC provider. Check the Issuer URL.", "flash_type": "error",
        })
    await db.save_oidc_config(config)
    return RedirectResponse("/auth/oidc/start", status_code=303)


@router.get("/auth/oidc/start")
async def oidc_start(request: Request):
    db = request.app.state.db
    oidc_config = await db.get_oidc_config()
    if not oidc_config:
        return RedirectResponse("/login", status_code=302)
    result = await oidc_module.create_oidc_client(oidc_config)
    if not result:
        return RedirectResponse("/login?error=OIDC+provider+unreachable", status_code=302)
    client, meta = result
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    redirect_uri = f"{scheme}://{host}/auth/oidc/callback"
    url, state, code_verifier = oidc_module.get_authorization_url(client, meta, redirect_uri)
    secret = _get_secret(request.app.state.config["db_path"])
    s = URLSafeTimedSerializer(secret)
    flow_data = s.dumps({"state": state, "code_verifier": code_verifier, "redirect_uri": redirect_uri})
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        OIDC_FLOW_COOKIE, flow_data,
        max_age=OIDC_FLOW_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/auth/oidc/",
    )
    return response


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request):
    error = request.query_params.get("error")
    if error:
        desc = request.query_params.get("error_description", error)
        return RedirectResponse(f"/login?error={desc}", status_code=302)

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return RedirectResponse("/login?error=Missing+authorization+code", status_code=302)

    secret = _get_secret(request.app.state.config["db_path"])
    s = URLSafeTimedSerializer(secret)
    flow_cookie = request.cookies.get(OIDC_FLOW_COOKIE)
    if not flow_cookie:
        return RedirectResponse("/login?error=Session+expired,+please+try+again", status_code=302)
    try:
        flow_data = s.loads(flow_cookie, max_age=OIDC_FLOW_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return RedirectResponse("/login?error=Session+expired,+please+try+again", status_code=302)

    if flow_data["state"] != state:
        return RedirectResponse("/login?error=Invalid+state+parameter", status_code=302)

    db = request.app.state.db
    oidc_config = await db.get_oidc_config()
    if not oidc_config:
        return RedirectResponse("/login?error=OIDC+not+configured", status_code=302)

    result = await oidc_module.create_oidc_client(oidc_config)
    if not result:
        return RedirectResponse("/login?error=OIDC+provider+unreachable", status_code=302)
    _client, meta = result

    try:
        sub = await oidc_module.handle_callback(
            oidc_config, meta,
            redirect_uri=flow_data["redirect_uri"],
            code=code,
            code_verifier=flow_data["code_verifier"],
        )
    except Exception:
        log.exception("OIDC callback failed")
        return RedirectResponse("/login?error=Authentication+failed", status_code=302)

    if not sub:
        return RedirectResponse("/login?error=Authentication+failed", status_code=302)

    stored_sub = await db.get_setting("oidc_sub")
    if stored_sub and stored_sub != sub:
        return RedirectResponse(
            "/login?error=This+identity+does+not+match+the+registered+player",
            status_code=302,
        )
    if not stored_sub:
        await db.set_setting("oidc_sub", sub)

    cookie = create_session_cookie(secret)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME, cookie,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(OIDC_FLOW_COOKIE, path="/auth/oidc/")
    return response


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    db = request.app.state.db
    config = request.app.state.config
    radio = request.app.state.radio
    player = await db.get_first_player()

    if not player and radio and not radio.configured:
        return RedirectResponse("/setup", status_code=302)

    if not player:
        needs_setup = config["home_lat"] == 0 and config["home_lon"] == 0
        return await _template(request, "dashboard.html", {
            "player": None, "needs_setup": needs_setup, "nav_active": "dashboard",
        })

    key = player["key"]

    next_rank = _next_rank_info(player["rank_level"])
    xp_progress = 0
    xp_in_rank = 0
    xp_needed = 0
    if next_rank:
        prev_xp = RANK_THRESHOLDS[player["rank_level"]]["xp"]
        xp_in_rank = int(player["xp"] - prev_xp)
        xp_needed = next_rank["xp"] - prev_xp
        xp_progress = min(100, int(xp_in_rank / max(1, xp_needed) * 100))

    posts = await db.get_all_posts(key)

    relics = await db.get_unused_relics(key)
    relic_counts = {"buried_cache": 0, "vigor_tonic": 0, "wardstone": 0}
    for r in relics:
        if r["type"] in relic_counts:
            relic_counts[r["type"]] += 1
    discovered_relics = await db.get_discovered_relic_types(key)

    # Relic side of the Frontier Merchant now lives here on Briefing, folded into
    # the relic inventory so buying/holding/using a relic all happen in one place.
    merchant_unlocked = player["base_camp_level"] >= MERCHANT_MIN_CAMP_LEVEL
    merchant_reset_ts = _week_start_utc() + 7 * 86400
    merchant_relic_prices = MERCHANT_RELIC_PRICES
    merchant_relic_purchased = set()
    if merchant_unlocked:
        merchant_relic_purchased = set(
            await db.get_merchant_purchases(key, _week_start_utc())
        )

    momentum_tier = player.get("momentum_tier", 0)

    ft_postcards = await db.get_postcards_by_class(key, FIELD_TRAINING_CLASS)
    # Intersect with the current postcard list so a legacy "Sworn In" postcard
    # (dropped — that rank-5 gate now lives only in Society Commission) can't
    # inflate the count for players who earned it before the change.
    ft_earned = {c["description"] for c in ft_postcards} & set(FIELD_TRAINING_POSTCARDS)
    ft_total = len(FIELD_TRAINING_POSTCARDS)
    ft_count = len(ft_earned)
    ft_complete = ft_count >= ft_total
    strongbox_claimed = bool(await db.get_setting("strongbox_claimed"))

    ft_descriptions = {
        "Staking Claim": "Set your base camp location",
        "First Contact": "Complete your first survey",
        "Long Range": "Survey from 1+ mile away",
        "Cartographer": "Discover 5 unique territories",
        "Relic Hunter": "Find your first relic",
    }
    ft_cards = []
    for ft_name in FIELD_TRAINING_POSTCARDS:
        ft_cards.append({
            "name": ft_name,
            "earned": ft_name in ft_earned,
            "hint": ft_descriptions.get(ft_name, ""),
        })

    manager = request.app.state.multiplayer_manager
    pvp_enabled = manager.pvp_enabled if manager else False

    contracts = []
    contracts_unlocked = player["rank_level"] >= CONTRACTS_MIN_LEVEL
    if contracts_unlocked:
        engine = request.app.state.engine
        contracts = await engine.ensure_weekly_contracts(key, pvp_enabled=pvp_enabled)
        for c in contracts:
            obj_info = CONTRACT_OBJECTIVES.get(c["objective"], {})
            c["label"] = obj_info.get("label", c["objective"])
            c["desc"] = obj_info.get("desc", "").format(target=c["target"])
            if c["reward_type"] in CONTRACT_ITEM_REWARD_TYPES:
                c["reward_label"] = f"⚔️ {multiplayer_item_name(c['reward_type'])['full']}"
                # The munition is minted on the Worker with the next survey
                # bundle ("supply drop"), not the instant the contract clears —
                # flag a completed-but-unminted one so the card says so.
                c["pending_supply_drop"] = bool(c["completed"]) and not c["reward_granted"]
            elif c["reward_type"] == "relic":
                c["reward_label"] = "Random Relic"
            elif c["reward_type"] == "provisions":
                c["reward_label"] = f"{c['reward_amount']} 📦"
            else:
                c["reward_label"] = f"{c['reward_amount']} 🪙"

    dispatch = get_daily_dispatch()
    raw_posts = await db.get_all_posts(key)

    # Gather multiplayer briefing state for the dispatch: inbound raids (from the
    # poll loop's cached defense — no Worker round-trip), the player's own raiding
    # party in flight, and the latest supply drop they may have missed.
    mp_brief = None
    if manager and manager.registered and pvp_enabled:
        now_ts = int(time.time())
        mp_brief = {"incoming": [], "outgoing": None, "supply": None}
        # Worker post ref (mp_token, hex_id fallback) → readable outpost name.
        name_by_ref = {
            p["mp_token"]: hex_name(p["hex_id"])
            for p in raw_posts
        }
        cached_def = await manager.get_cached_defense()
        # Ignore a stale snapshot (e.g. app was offline): raids travel ≥1h and the
        # idle cadence is 20m, so anything older than ~40m can't be trusted.
        if cached_def and now_ts - cached_def.get("_cached_at", 0) <= 2400:
            for post in cached_def.get("posts", []):
                ref = post.get("post_token")
                for raid in post.get("incoming_raids", []):
                    mp_brief["incoming"].append({
                        "post": name_by_ref.get(ref, (ref or "")[:8]),
                        "eta_min": max(1, round(raid.get("eta_seconds", 0) / 60)),
                        "threat": raid.get("threat", "hold"),
                    })
        active = await manager.get_active_raid()
        if active and active.get("status") == "in_flight":
            mp_brief["outgoing"] = {
                "target": active.get("target_player_name") or "the target",
                "eta_min": max(0, round((active.get("arrives_at", now_ts) - now_ts) / 60)),
            }
        for run in await db.get_recent_supply_runs(5):
            if run.get("drop_count", 0) > 0 and now_ts - run.get("ran_at", 0) <= 86400:
                mp_brief["supply"] = _supply_run_summary(run["drops"])
                break

    analysts_report = await generate_analysts_report(
        db, player, raw_posts, contracts, ft_complete, strongbox_claimed, mp=mp_brief,
    )
    dispatch_msg = dispatch["message"]
    if analysts_report:
        dispatch_msg = dispatch_msg + " " + analysts_report

    # Society Commission — one card tying together the checkpoint chain so the
    # player always sees the next unlock, what it needs, and what it grants.
    rl = player["rank_level"]
    cl = player["base_camp_level"]
    charter_license = rl >= CHARTER_MIN_LEVEL and cl >= CHARTER_MIN_CAMP

    commission_steps = [
        {
            "name": "Complete Field Training",
            "done": ft_complete,
            "requirement": f"Complete {ft_total} training objectives ({ft_count}/{ft_total})",
            "unlocks": "Society Strongbox",
            "tip": "Guided objectives that walk you through the core survey loop. "
                   "Finish them all to crack open the Society Strongbox — a one-time "
                   "starter cache of provisions and marks.",
        },
        {
            "name": "Scout Rank",
            "done": rl >= CONTRACTS_MIN_LEVEL,
            "requirement": f"Reach {rank_name(CONTRACTS_MIN_LEVEL)} (rank {CONTRACTS_MIN_LEVEL})",
            "unlocks": "Expedition Contracts",
            "tip": "Earn XP from surveys to make Scout. Reaching it opens Expedition "
                   "Contracts — rotating objectives on the Briefing page that pay bonus "
                   "provisions and marks.",
        },
        {
            "name": "Unlock Frontier Merchant",
            "done": cl >= MERCHANT_MIN_CAMP_LEVEL,
            "requirement": f"Upgrade to {camp_name(MERCHANT_MIN_CAMP_LEVEL)} (camp {MERCHANT_MIN_CAMP_LEVEL})",
            "unlocks": "Frontier Merchant · weekly relic shop",
            "tip": "Spend provisions and field notes to grow Base Camp. At "
                   f"{camp_name(MERCHANT_MIN_CAMP_LEVEL)} the Frontier Merchant opens — "
                   "a weekly shop where you trade marks for relics.",
        },
        {
            "name": "Obtain Charter License",
            "done": charter_license,
            "requirement": f"Reach rank {CHARTER_MIN_LEVEL} + {camp_name(CHARTER_MIN_CAMP)} (camp {CHARTER_MIN_CAMP})",
            "unlocks": "Charter Survey Posts · +3 🪙 first-charter bonus · "
                       "PvP Combat unlocks (enable in Settings) after your first post",
            "tip": "The big one. Reach the required rank and Base Camp level to charter "
                   "Survey Posts — permanent outposts you plant at real-world sites 3+ mi "
                   "from home that boost nearby surveys. Also opens optional PvP.",
        },
    ]
    _current_seen = False
    for step in commission_steps:
        if step["done"]:
            step["status"] = "done"
        elif not _current_seen:
            step["status"] = "current"
            _current_seen = True
        else:
            step["status"] = "locked"
    commission_done = sum(1 for s in commission_steps if s["done"])
    commission_total = len(commission_steps)

    # One-time First Contact celebration: the survey engine sets this flag when
    # it awards the First Contact postcard. Show the popup on the next dashboard
    # load, then clear it so it only ever fires once.
    show_first_contact = await db.get_setting("pending_first_contact_popup") == "1"
    if show_first_contact:
        await db.set_setting("pending_first_contact_popup", "0")

    return await _template(request, "dashboard.html", {
        "player": player,
        "show_first_contact": show_first_contact,
        "dispatch": dispatch,
        "dispatch_msg": dispatch_msg,
        "rank_name": rank_name(player["rank_level"]),
        "next_rank": next_rank,
        "xp_progress": xp_progress,
        "xp_in_rank": xp_in_rank,
        "xp_needed": xp_needed,
        "relics": relics,
        "relic_counts": relic_counts,
        "discovered_relics": discovered_relics,
        "merchant_unlocked": merchant_unlocked,
        "merchant_reset_ts": merchant_reset_ts,
        "merchant_relic_prices": merchant_relic_prices,
        "merchant_relic_purchased": merchant_relic_purchased,
        "merchant_min_camp": MERCHANT_MIN_CAMP_LEVEL,
        "posts": posts,
        "buried_cache_amount": BURIED_CACHE_AMOUNT,
        "relic_salvage_value": RELIC_SALVAGE_VALUE,
        "momentum_tier": momentum_tier,
        "ft_cards": ft_cards,
        "ft_count": ft_count,
        "ft_total": ft_total,
        "ft_complete": ft_complete,
        "strongbox_claimed": strongbox_claimed,
        "contracts": contracts,
        "contracts_unlocked": contracts_unlocked,
        "contract_reset_ts": _contract_period_start_utc() + CONTRACT_PERIOD_DAYS * 86400,
        "active_title": player.get("active_title"),
        "commission_steps": commission_steps,
        "commission_done": commission_done,
        "commission_total": commission_total,
        "nav_active": "dashboard",
    })


@router.post("/setup/home")
async def setup_home(request: Request):
    form = await request.form()
    try:
        lat = float(form["lat"])
        lon = float(form["lon"])
    except (KeyError, ValueError):
        return _flash_redirect("/", "Invalid coordinates", "error")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return _flash_redirect("/", "Coordinates out of range", "error")
    engine = request.app.state.engine
    await engine.set_home(lat, lon)
    return _flash_redirect("/", "Base camp established! Welcome to the World's End Society.", "success")


# --- Radio ---

@router.get("/radio", response_class=HTMLResponse)
async def radio_page(request: Request):
    db = request.app.state.db
    player = await db.get_first_player()
    if not player:
        return RedirectResponse("/", status_code=302)

    key = player["key"]
    hex_count = await db.count_discovered_hexes(key)
    post_count = await db.count_player_posts(key)
    furthest_survey = await db.get_furthest_survey(key)
    radio = request.app.state.radio
    companion = await radio.get_companion_status()
    selected_node = player.get("last_survey_sender") or ""
    nodes = await db.get_known_nodes()

    # Locate readout ("Charter Available") only fires when the player is actually
    # eligible to charter: high enough rank/camp and a free post slot. Distance /
    # discovery are checked client-side against the current hex.
    charter_ready = (
        player["rank_level"] >= CHARTER_MIN_LEVEL
        and player["base_camp_level"] >= CHARTER_MIN_CAMP
        and post_count < max_posts_for_camp(player["base_camp_level"])
    )

    return await _template(request, "spyglass.html", {
        "player": player,
        "home_lat": player["home_lat"],
        "home_lon": player["home_lon"],
        "hex_count": hex_count,
        "post_count": post_count,
        "charter_ready": charter_ready,
        "charter_min_miles": CHARTER_MIN_DISTANCE_MILES,
        "furthest_survey": furthest_survey,
        "companion_connected": companion.get("connected", False),
        "selected_node": selected_node,
        "nodes": nodes,
        "telemetry_timeout": TELEMETRY_TIMEOUT_FLOOD,
        "nav_active": "radio",
    })


@router.get("/spyglass")
async def spyglass_redirect():
    return RedirectResponse("/radio", status_code=301)


# --- SSE Live Feed ---

def _sse_frame(event: dict) -> str:
    # Emit the SSE `id:` line so the browser tracks Last-Event-ID as a backstop,
    # and the client can dedup reconnect replays exactly.
    prefix = f"id: {event['id']}\n" if event.get("id") else ""
    return f"{prefix}data: {json.dumps(event)}\n\n"


@router.get("/api/events")
async def sse_events(request: Request):
    engine = request.app.state.engine
    last_id = int(request.query_params.get("last_id", 0))
    # Each connection gets its own queue; the engine broadcasts to all of them, so
    # a reconnecting client (or a second tab) never steals events from another.
    queue = engine.subscribe()

    async def event_stream():
        try:
            if last_id:
                events = await engine.get_events_since(last_id)
            else:
                events = await engine.get_recent_events_from_db(20)
            for event in events:
                yield _sse_frame(event)
            ping_counter = 0
            while not engine._shutting_down:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    event = None
                if event is None:
                    # ~15s heartbeat so a client behind a silently-dead link can
                    # notice the gap and reconnect (see the client stall watchdog).
                    ping_counter += 1
                    if ping_counter >= 3:
                        yield "event: ping\ndata: {}\n\n"
                        ping_counter = 0
                elif event.get("type") == "shutdown":
                    break
                else:
                    ping_counter = 0
                    yield _sse_frame(event)
        except asyncio.CancelledError:
            return
        finally:
            engine.unsubscribe(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --- Outposts ---

@router.get("/outposts", response_class=HTMLResponse)
async def outposts_page(request: Request):
    db = request.app.state.db
    player = await db.get_first_player()
    if not player:
        return await _template(request, "outposts.html", {
            "player": None, "posts": [], "nav_active": "outposts",
        })

    key = player["key"]
    current = player["base_camp_level"]
    camp_info = BASE_CAMP_TABLE[current]
    next_camp_level = current + 1
    next_camp_info = BASE_CAMP_TABLE.get(next_camp_level)
    post_limit = max_posts_for_camp(current)

    posts = await db.get_all_posts(key)
    engine = request.app.state.engine
    p_home_lat, p_home_lon = player["home_lat"], player["home_lon"]
    grace = upkeep_grace_days(player)
    total_prov_per_day = 0
    now = int(time.time())
    for p in posts:
        if p["level"] < MAX_POST_LEVEL:
            p["upgrade_cost"] = POST_UPGRADE_COST[p["level"] + 1]
        else:
            p["upgrade_cost"] = None
        dist = engine._distance_from_hex(p["hex_id"], p_home_lat, p_home_lon)
        p["distance"] = dist
        warded_until = p.get("ruin_frozen_until")
        p["warded"] = bool(warded_until and now < warded_until)
        p["ruin_frozen"] = p["warded"]  # ward end doubles as the ruin-freeze end
        if p["warded"]:
            p["warded_days_left"] = (warded_until - now) / 86400
            p["frozen_days_left"] = p["warded_days_left"]

        # Ruin is income-decay: full rate for `grace` days since upkeep, then a
        # ramp to zero. Warded posts are dormant (frozen, no income).
        ruin = _ruin_state(p["last_tended_at"], grace)
        full_rate = p["level"] * (1 + math.floor(dist / 5))
        p["prov_per_day_full"] = full_rate
        if p["warded"]:
            p["ruin_status"] = "warded"
            p["income_factor"] = 0.0
            p["prov_per_day"] = 0
        else:
            p["ruin_status"] = ruin["state"]
            p["income_factor"] = ruin["factor"]
            p["full_days_left"] = ruin["full_days_left"]
            p["days_until_ruined"] = ruin["days_until_ruined"]
            # Full timeline (grace + ruin ramp) so the bar can show a fixed-scale
            # runway: green grace segment + red ruin segment against the same max.
            p["upkeep_total_days"] = grace + RUIN_RAMP_DAYS
            p["prov_per_day"] = int(full_rate * ruin["factor"])
        # Renown/day = a per-level base plus a longevity bonus for every day the
        # post has survived (mirrors the Worker). Split out base vs. age so the
        # card can show the formula. Like provisions, renown now fades with ruin:
        # the full rate is scaled by the same income factor, so a post in ruin (or
        # dormant under a ward) yields 0/day — its leaderboard standing stops
        # climbing until it's tended again. Already-earned renown is never lost.
        renown_age_days = max(0.0, (now - (p.get("created_at") or now)) / 86400)
        p["renown_base"] = p["level"] * RENOWN_PER_DAY_PER_LEVEL
        p["renown_age_bonus"] = int(round(renown_age_days * RENOWN_AGE_BONUS_PER_DAY))
        p["renown_per_day_full"] = p["renown_base"] + p["renown_age_bonus"]
        p["renown_per_day"] = int(round(p["renown_per_day_full"] * p["income_factor"]))
        p["hex_name"] = hex_name(p["hex_id"])
        total_prov_per_day += p["prov_per_day"]

    relics = await db.get_unused_relics(key)
    wardstone_relic = next((r for r in relics if r["type"] == "wardstone"), None)
    wardstone_count = sum(1 for r in relics if r["type"] == "wardstone")

    # Warding is blocked while a raiding party is in flight — only worth the
    # Worker round-trip if the player actually has a wardstone to spend.
    raid_in_flight = False
    _mgr = request.app.state.multiplayer_manager
    if wardstone_relic and _mgr and _mgr.registered:
        active = await _mgr.get_active_raid()
        raid_in_flight = bool(active and active.get("status") == "in_flight")

    # The Frontier Merchant shop moved off this page (relics → Briefing,
    # munitions → Multiplayer). Base Camp only needs the milestone *statuses*
    # now, so the player can see what's unlocked and where to use it.
    merchant_unlocked = current >= MERCHANT_MIN_CAMP_LEVEL
    mp_manager = request.app.state.multiplayer_manager
    mp_registered = bool(mp_manager and mp_manager.registered)
    pvp_enabled = bool(getattr(mp_manager, "_pvp_enabled", False))

    # Supply Drops — the hourly bundle of logged surveys goes out to the Worker,
    # which rolls PvP item drops. Surface the pending count, next-run time, and a
    # short history so the cadence is legible off the live feed.
    supply_last_at = mp_manager._last_push_at if mp_manager else None
    # Count down to the recurring hourly *check*, not the last successful push
    # — a queue of 0 surveys never advances _last_push_at, which is why "Next
    # Drop" used to read "Due" forever. Fall back to the old estimate only if
    # the loop hasn't recorded a tick yet.
    supply_next_at = getattr(mp_manager, "_next_push_at", None) if mp_manager else None
    if supply_next_at is None and supply_last_at:
        supply_next_at = supply_last_at + BUNDLE_INTERVAL
    supply_pending = (
        await db.count_surveys_since(key, supply_last_at or 0) if mp_registered else 0
    )
    supply_runs = await db.get_recent_supply_runs(3) if mp_registered else []
    for run in supply_runs:
        run["summary"] = _supply_run_summary(run["drops"])

    charter_license = current >= CHARTER_MIN_CAMP and player["rank_level"] >= CHARTER_MIN_LEVEL

    return await _template(request, "outposts.html", {
        "player": player,
        "camp_name": camp_name(current),
        "camp_info": camp_info,
        "next_camp_info": next_camp_info,
        "next_camp_level": next_camp_level,
        "next_camp_name": camp_name(next_camp_level) if next_camp_info else None,
        "post_limit": post_limit,
        "charter_license": charter_license,
        "total_prov_per_day": total_prov_per_day,
        "renown_per_level": RENOWN_PER_DAY_PER_LEVEL,
        "renown_age_rate": RENOWN_AGE_BONUS_PER_DAY,
        "posts": posts,
        "charter_prov_cost": CHARTER_PROVISION_COST,
        "charter_mark_cost": CHARTER_MARK_COST,
        "wardstone_relic": wardstone_relic,
        "wardstone_count": wardstone_count,
        "raid_in_flight": raid_in_flight,
        "merchant_unlocked": merchant_unlocked,
        "merchant_min_camp": MERCHANT_MIN_CAMP_LEVEL,
        "mp_registered": mp_registered,
        "camp_perk_descriptions": CAMP_PERK_DESCRIPTIONS,
        "nav_active": "outposts",
        "pvp_enabled": pvp_enabled,
        "supply_pending": supply_pending,
        "supply_next_at": supply_next_at,
        "supply_runs": supply_runs,
        "supply_interval_min": BUNDLE_INTERVAL // 60,
    })


# --- Survey Posts (redirect to Outposts) ---

@router.get("/posts", response_class=HTMLResponse)
async def posts_page(request: Request):
    return RedirectResponse("/outposts", status_code=301)


@router.post("/posts/{post_id}/upgrade")
async def upgrade_post(request: Request, post_id: int):
    engine = request.app.state.engine
    key = await _get_player_key(request.app.state.db)
    if not key:
        return _flash_redirect("/outposts", "No player found", "error")
    result = await engine.upgrade_post(key, post_id)
    if result["success"]:
        return _flash_redirect("/outposts", f"Post upgraded to level {result['new_level']}!", "success")
    return _flash_redirect("/outposts", result["reason"], "error")


@router.post("/posts/{post_id}/repair")
async def repair_post(request: Request, post_id: int):
    """Repair a PvP post's battle-damaged Health with provisions.

    Ruin is no longer paid off here — that's physical /lora upkeep only. This
    action heals raid damage on the federated Worker (5 HP per provision).
    """
    engine = request.app.state.engine
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return _flash_redirect("/outposts", "No player found", "error")
    post = await db.get_post_by_id(post_id)
    if not post or post["player_key"] != key:
        return _flash_redirect("/outposts", "Post not found", "error")

    manager = getattr(request.app.state, "multiplayer_manager", None)
    if not (manager and getattr(manager, "registered", False)):
        return _flash_redirect("/outposts", "Repair is only for PvP-enabled outposts", "info")

    healed = await _repair_post_hp(engine, manager, key, post["mp_token"])
    if not healed:
        return _flash_redirect("/outposts", "Nothing to repair — Health already full", "info")
    return _flash_redirect(
        "/outposts",
        f"Repaired! +{healed['hp_restored']} HP for {healed['cost']} provisions.",
        "success",
    )


async def _repair_post_hp(engine, manager, player_key: str, post_token: str) -> dict | None:
    """Heal a PvP post's defense HP to full, charging provisions locally.

    HP lives on the federated Worker, so we fetch the current deficit, spend as
    many provisions as we can afford (5 HP each), then deduct locally only if the
    Worker confirms the heal. Returns {"cost", "hp_restored"} or None if there was
    nothing to repair.
    """
    try:
        defense = await manager.get_defense()
    except Exception:
        return None
    if not defense or not defense.get("ok"):
        return None
    target = next(
        (p for p in defense.get("posts", []) if p.get("post_token") == post_token), None
    )
    if not target:
        return None
    hp, max_hp = target.get("hp"), target.get("max_hp")
    if hp is None or max_hp is None or hp >= max_hp:
        return None

    deficit = max_hp - hp
    needed = math.ceil(deficit / 5)  # Worker heals 5 HP per provision.
    player = await engine._db.get_player(player_key)
    spend = min(needed, player["provisions"])
    if spend <= 0:
        return None

    result = await manager.restore_hp(post_token, spend)
    if not result or not result.get("ok"):
        return None
    await engine._db.deduct_provisions(player_key, spend)
    return {"cost": spend, "hp_restored": spend * 5}


@router.post("/posts/collect-provisions")
async def collect_provisions(request: Request):
    engine = request.app.state.engine
    key = await _get_player_key(request.app.state.db)
    if not key:
        return _flash_redirect("/outposts", "No player found", "error")
    result = await engine.collect_passive_provisions(key)
    if result["total"] > 0:
        return _flash_redirect("/outposts", f"Collected {result['total']} provisions from outposts!", "success")
    return _flash_redirect("/outposts", "No provisions to collect yet", "info")


# --- Base Camp (upgrade action, rendered on dashboard) ---

@router.post("/basecamp/upgrade")
async def upgrade_basecamp(request: Request):
    engine = request.app.state.engine
    key = await _get_player_key(request.app.state.db)
    if not key:
        return _flash_redirect("/", "No player found", "error")
    result = await engine.upgrade_base_camp(key)
    if result["success"]:
        return _flash_redirect(
            "/outposts",
            f"Base camp upgraded to {result['name']}! (Lv {result['new_level']}, {result['mult']}x multiplier)",
            "success",
        )
    return _flash_redirect("/outposts", result["reason"], "error")


# --- Relics (use actions, rendered on dashboard) ---

@router.post("/relics/{relic_id}/use-cache")
async def use_buried_cache(request: Request, relic_id: int):
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return _flash_redirect("/", "No player found", "error")
    if not await db.use_buried_cache(relic_id, key, BURIED_CACHE_AMOUNT):
        return _flash_redirect("/", "Relic not found or already used", "error")
    await db.log_activity(key, "relic", "Opened Buried Cache", f"+{BURIED_CACHE_AMOUNT} provisions")
    return _flash_redirect("/", f"Buried Cache opened! +{BURIED_CACHE_AMOUNT} provisions", "success")


@router.post("/relics/{relic_id}/ward/{post_id}")
async def ward_post(request: Request, relic_id: int, post_id: int):
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return _flash_redirect("/outposts", "No player found", "error")
    form = await request.form()
    try:
        days = clamp_ward_days(int(form.get("days", WARD_MIN_DAYS)))
    except (TypeError, ValueError):
        days = WARD_MIN_DAYS
    # Can't batten down while you're still on the offensive — resolve your
    # raiding party first (anti-turtling: no attack-then-hide).
    manager = request.app.state.multiplayer_manager
    if manager and manager.registered:
        active = await manager.get_active_raid()
        if active and active.get("status") == "in_flight":
            return _flash_redirect(
                "/outposts",
                "Can't ward an outpost while your raiding party is in flight — resolve your raid first.",
                "error",
            )
    if not await db.ward_post(relic_id, post_id, key, days * 86400):
        return _flash_redirect("/outposts", "Wardstone or Survey Post not found", "error")
    await db.log_activity(key, "relic", "Warded outpost", f"Dormant for {days} day{'s' if days != 1 else ''}")
    # Push the new dormant state so the Worker stops accepting raids on it.
    if manager and manager.registered:
        await manager.force_sync()
    return _flash_redirect("/outposts", f"Outpost warded — dormant for {days} day{'s' if days != 1 else ''}.", "success")


@router.post("/relics/{relic_id}/use-tonic")
async def use_vigor_tonic(request: Request, relic_id: int):
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return _flash_redirect("/", "No player found", "error")
    if not await db.use_vigor_tonic(relic_id, key):
        return _flash_redirect("/", "Relic not found or already used", "error")
    await db.log_activity(key, "relic", "Used Vigor Tonic", "All survey cooldowns cleared")
    return _flash_redirect("/", "Vigor Tonic consumed! All survey cooldowns cleared.", "success")


@router.post("/relics/{relic_id}/salvage")
async def salvage_relic(request: Request, relic_id: int):
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return _flash_redirect("/", "No player found", "error")
    amount = await db.salvage_relic(relic_id, key, RELIC_SALVAGE_VALUE)
    if amount is None:
        return _flash_redirect("/", "Relic not found, already used, or can't be salvaged", "error")
    await db.log_activity(key, "relic", "Salvaged relic", f"+{amount} provisions")
    return _flash_redirect("/", f"Relic salvaged! +{amount} provisions", "success")


@router.post("/strongbox/claim")
async def claim_strongbox(request: Request):
    engine = request.app.state.engine
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return _flash_redirect("/stats", "No player found", "error")
    result = await engine.claim_strongbox(key)
    if not result["success"]:
        return _flash_redirect("/stats", result["reason"], "error")
    return _flash_redirect(
        "/stats",
        f"Society Strongbox opened! +{result['provisions']} provisions and a {result['relic']}!",
        "success",
    )


@router.post("/contracts/{contract_id}/buy")
async def buy_contract(request: Request, contract_id: int):
    engine = request.app.state.engine
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return _flash_redirect("/", "No player found", "error")
    result = await engine.purchase_contract(key, contract_id)
    if not result["ok"]:
        return _flash_redirect("/", result.get("error", "Cannot purchase"), "error")
    return _flash_redirect("/", "Contract accepted! Get to work, explorer.", "success")


@router.post("/merchant/buy-relic")
async def merchant_buy_relic(request: Request):
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return _flash_redirect("/", "No player found", "error")
    player = await db.get_first_player()
    if not player or player["base_camp_level"] < MERCHANT_MIN_CAMP_LEVEL:
        return _flash_redirect("/", "Merchant not unlocked yet", "error")
    form = await request.form()
    relic_type = form.get("relic_type", "")
    if relic_type not in MERCHANT_RELIC_PRICES:
        return _flash_redirect("/", "Invalid relic", "error")
    ws = _week_start_utc()
    purchases = await db.get_merchant_purchases(key, ws)
    if relic_type in purchases:
        return _flash_redirect("/", "Already purchased this relic this week", "error")
    discovered = await db.get_discovered_relic_types(key)
    if relic_type not in discovered:
        return _flash_redirect("/", "You haven't discovered this relic yet", "error")
    price = MERCHANT_RELIC_PRICES[relic_type]
    if player["provisions"] < price:
        return _flash_redirect("/", "Not enough provisions", "error")
    await db._execute(
        "UPDATE players SET provisions = provisions - ? WHERE key = ?",
        (price, key),
    )
    now = int(time.time())
    await db._execute(
        "INSERT INTO relics (player_key, type, found_at) VALUES (?, ?, ?)",
        (key, relic_type, now),
    )
    await db.add_merchant_purchase(key, relic_type, ws)
    label = relic_type.replace("_", " ").title()
    await db.log_activity(key, "merchant", f"Purchased {label} for {price} provisions")
    return _flash_redirect("/", f"Purchased {label} for {price} 📦", "success")


@router.post("/merchant/buy-item")
async def merchant_buy_item(request: Request):
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return _flash_redirect("/multiplayer", "No player found", "error")
    player = await db.get_first_player()
    if not player or player["base_camp_level"] < MERCHANT_MIN_CAMP_LEVEL:
        return _flash_redirect("/multiplayer", "Merchant not unlocked yet", "error")
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return _flash_redirect("/multiplayer", "Multiplayer not enabled", "error")
    form = await request.form()
    item_type = form.get("item_type", "")
    result = await manager.buy_multiplayer_item(item_type)
    if not result.get("ok"):
        return _flash_redirect("/multiplayer", result.get("error", "Purchase failed"), "error")
    label = item_type.replace("_", " ").title()
    symbol = "📦" if result["currency"] == "provisions" else "🪙"
    await db.log_activity(key, "merchant", f"Purchased {label} for {result['price']} {result['currency']}")
    return _flash_redirect("/multiplayer", f"Purchased {label} for {result['price']} {symbol}", "success")


@router.post("/merchant/salvage-item")
async def merchant_salvage_item(request: Request):
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return _flash_redirect("/multiplayer", "No player found", "error")
    manager = request.app.state.multiplayer_manager
    if not manager or not manager.registered:
        return _flash_redirect("/multiplayer", "Multiplayer not enabled", "error")
    form = await request.form()
    item_type = form.get("item_type", "")
    try:
        count = int(form.get("count", "1"))
    except (TypeError, ValueError):
        count = 1
    result = await manager.salvage_multiplayer_item(item_type, count)
    if not result.get("ok"):
        return _flash_redirect("/multiplayer", result.get("error", "Salvage failed"), "error")
    label = item_type.replace("_", " ").title()
    symbol = "📦" if result["currency"] == "provisions" else "🪙"
    await db.log_activity(
        key, "merchant",
        f"Salvaged {result['count']}× {label} for {result['value']} {result['currency']}")
    return _flash_redirect(
        "/multiplayer",
        f"Salvaged {result['count']}× {label} for +{result['value']} {symbol}", "success")


# --- Instrument Panel API (server-initiated commands) ---

@router.post("/api/survey")
async def api_survey(request: Request):
    engine = request.app.state.engine
    key = await _get_player_key(request.app.state.db)
    if not key:
        return JSONResponse({"ok": False, "error": "No player found"}, status_code=404)
    # Hands-free auto-surveys mark themselves so a too-soon one is a silent no-op
    # rather than a "hold" message. Tolerate an empty/no body for manual taps.
    auto = False
    try:
        body = await request.json()
        auto = bool(body.get("auto"))
    except Exception:
        pass
    result = await engine.web_survey(key, auto=auto)
    return JSONResponse(result)


@router.post("/api/charter")
async def api_charter(request: Request):
    engine = request.app.state.engine
    key = await _get_player_key(request.app.state.db)
    if not key:
        return JSONResponse({"ok": False, "error": "No player found"}, status_code=404)
    result = await engine.web_charter(key)
    return JSONResponse(result)


@router.post("/api/upkeep")
async def api_upkeep(request: Request):
    engine = request.app.state.engine
    key = await _get_player_key(request.app.state.db)
    if not key:
        return JSONResponse({"ok": False, "error": "No player found"}, status_code=404)
    result = await engine.web_upkeep(key)
    return JSONResponse(result)


@router.post("/api/select-node")
async def api_select_node(request: Request):
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return JSONResponse({"ok": False, "error": "No player found"}, status_code=404)
    body = await request.json()
    node_key = body.get("node_key", "")
    await db._execute(
        "UPDATE players SET last_survey_sender = ?, last_survey_lat = NULL, last_survey_lon = NULL, last_survey_at = NULL WHERE key = ?",
        (node_key or None, key),
    )
    return JSONResponse({"ok": True})


@router.post("/api/posts/{post_id}/rename")
async def api_rename_post(request: Request, post_id: int):
    engine = request.app.state.engine
    key = await _get_player_key(request.app.state.db)
    if not key:
        return JSONResponse({"ok": False, "error": "No player found"}, status_code=404)
    body = await request.json()
    name = body.get("name", "").strip()
    result = await engine.rename_post(key, post_id, name)
    return JSONResponse(result)


# --- Stats ---

@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    db = request.app.state.db
    player = await db.get_first_player()
    if not player:
        return await _template(request, "stats.html", {
            "player": None, "nav_active": "achievements",
        })

    key = player["key"]
    hex_count = await db.count_discovered_hexes(key)
    survey_count = await db.get_survey_count(key)
    post_count = await db.count_player_posts(key)
    streak = await db.get_survey_streak(key)
    max_distance = await db.get_max_distance(key)
    recent_surveys = await db.get_recent_surveys(key, limit=6)
    area_sq_mi = hex_count * 0.306

    home_hex = h3.latlng_to_cell(player["home_lat"], player["home_lon"], 8)
    for s in recent_surveys:
        s["time_ago"] = _format_time_ago(s["surveyed_at"])
        s["hex_name"] = "Base Camp" if s["hex_id"] == home_hex else hex_name(s["hex_id"])
        s["breakdown"] = None
        if s.get("reward_breakdown"):
            try:
                s["breakdown"] = json.loads(s["reward_breakdown"])
            except (ValueError, TypeError):
                s["breakdown"] = None

    all_postcards = await db.get_all_postcards(key)

    postcard_cards = []
    postcards_earned = 0
    for pc_class in STARRED_POSTCARD_CLASSES:
        earned_stars = max(
            (p["stars"] for p in all_postcards if p["class"] == pc_class),
            default=0,
        )
        postcards_earned += earned_stars
        postcard_cards.append(build_postcard_card(pc_class, earned_stars))
    postcards_total = len(STARRED_POSTCARD_CLASSES) * 5

    return await _template(request, "stats.html", {
        "player": player,
        "hex_count": hex_count,
        "survey_count": survey_count,
        "post_count": post_count,
        "streak": streak,
        "max_distance": max_distance,
        "area_sq_mi": area_sq_mi,
        "recent_surveys": recent_surveys,
        "postcard_cards": postcard_cards,
        "postcards_earned": postcards_earned,
        "postcards_total": postcards_total,
        "active_title": player.get("active_title"),
        "nav_active": "achievements",
    })


@router.post("/title/set")
async def set_title(request: Request):
    form = await request.form()
    title = form.get("title", "").strip()
    db = request.app.state.db
    player = await db.get_first_player()
    if not player:
        return RedirectResponse("/stats", status_code=302)
    key = player["key"]
    if title and title in STARRED_POSTCARD_CLASSES:
        all_postcards = await db.get_all_postcards(key)
        earned = max(
            (p["stars"] for p in all_postcards if p["class"] == title),
            default=0,
        )
        if earned >= 5:
            await db.set_active_title(key, title)
    elif not title:
        await db.set_active_title(key, None)
    # Propagate the change to the multiplayer leaderboard immediately.
    manager = getattr(request.app.state, "multiplayer_manager", None)
    if manager and manager.registered:
        try:
            await manager.force_sync()
        except Exception:
            pass
    return RedirectResponse("/stats?flash_msg=Title+updated", status_code=302)


# --- Settings ---

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    config = request.app.state.config
    db = request.app.state.db
    player = await db.get_first_player()
    backups = list_backups(config["db_path"])
    for b in backups:
        b["time_ago"] = _format_time_ago(b["created_at"])

    radio = request.app.state.radio
    companion = await radio.get_companion_status()

    # Spyglasses the base camp can reach, for the test-message target picker.
    # Same source as the Radio page's spyglass selector.
    spyglasses = await db.get_known_nodes()
    default_spyglass = (player.get("last_survey_sender") if player else "") or ""
    if default_spyglass and not any(s["key"] == default_spyglass for s in spyglasses):
        default_spyglass = ""
    if not default_spyglass and spyglasses:
        default_spyglass = spyglasses[0]["key"]

    oidc_config = await db.get_oidc_config()
    has_password = await db.get_setting("password_hash") is not None

    update_check_enabled = await update_check_module.is_enabled(db)
    update_check_cache = await update_check_module.get_cached(db)

    manager = request.app.state.multiplayer_manager
    pvp_enabled = manager.pvp_enabled if manager else False
    mp_registered = manager.registered if manager else False
    webhook_url = ""
    mesh_notify = True
    pvp_ready = True
    pvp_ready_reason = ""
    if manager and manager.registered:
        settings = await manager._load_settings()
        webhook_url = settings.get("webhook_url", "")
        mesh_notify = settings.get("mesh_notify", "true") != "false"
        if not pvp_enabled:
            readiness = await manager.pvp_readiness()
            pvp_ready = readiness["ready"]
            pvp_ready_reason = readiness["reason"]

    return await _template(request, "settings.html", {
        "player": player,
        "config": config,
        "backups": backups,
        "companion": companion,
        "spyglasses": spyglasses,
        "default_spyglass": default_spyglass,
        "oidc_config": oidc_config,
        "has_password": has_password,
        "has_oidc": oidc_config is not None,
        "nav_active": "settings",
        "player_id": manager.player_id if manager else None,
        "pvp_enabled": pvp_enabled,
        "mp_registered": mp_registered,
        "pvp_ready": pvp_ready,
        "pvp_ready_reason": pvp_ready_reason,
        "webhook_url": webhook_url,
        "mesh_notify": mesh_notify,
        "last_sync_at": manager._last_push_at if manager else None,
        "app_version": __version__,
        "update_check_enabled": update_check_enabled,
        "update_check": update_check_cache,
    })


@router.get("/api/version")
async def api_version():
    """Local version info only — not a phone-home check. Nothing here makes a
    network request; it just reports what this install already knows about
    itself. See PRIVACY.md before wiring this into any remote update check."""
    return JSONResponse({"version": __version__})


@router.post("/api/update-check/toggle")
async def api_update_check_toggle(request: Request):
    """Opt-in switch for the periodic GitHub Releases check (update_check.py).
    Off by default — this is the only place that turns automatic checking on,
    matching the opt-in mesh/webhook toggles above."""
    db = request.app.state.db
    try:
        body = await request.json()
    except Exception:
        body = {}
    enabled = bool(body.get("enabled"))
    await update_check_module.set_enabled(db, enabled)
    return JSONResponse({"ok": True, "enabled": enabled})


@router.post("/api/update-check/now")
async def api_update_check_now(request: Request):
    """Manual, explicit check — always allowed regardless of the opt-in
    toggle above, since a user clicking a 'Check now' button is itself the
    consent for that one request."""
    db = request.app.state.db
    result = await update_check_module.check_now(db)
    return JSONResponse(result)


@router.post("/api/companion/test-message")
async def api_test_message(request: Request):
    """Send a test message to a chosen spyglass and report the result as JSON.

    Sending blocks until the mesh ACKs or times out (up to ~40s), so the client
    drives this with a live status indicator rather than a page reload."""
    radio = request.app.state.radio
    db = request.app.state.db
    try:
        body = await request.json()
    except Exception:
        body = {}
    target = (body.get("target") or "").strip()

    nodes = await db.get_known_nodes()
    if not target:
        player = await db.get_first_player()
        if not player or not player.get("key"):
            return JSONResponse({"ok": False, "message": "No spyglass registered yet — send a survey to link one first."})
        target = player["key"]
    name = next((n["name"] for n in nodes if n["key"] == target), None) or "spyglass"

    ok = await radio.send_message(target, "COMPANION TEST\nGame service connection OK.")
    if ok:
        return JSONResponse({"ok": True, "message": f"Delivered to {name} — ACK received."})
    return JSONResponse({"ok": False, "message": f"No ACK from {name} — check it's powered on and in range."})


@router.post("/settings/reboot-companion")
async def reboot_companion(request: Request):
    radio = request.app.state.radio
    ok = await radio.reboot_companion()
    if ok:
        return _flash_redirect("/settings", "Reboot command sent — companion will restart", "success")
    return _flash_redirect("/settings", "Reboot failed — check connection", "error")



@router.post("/settings/change-password")
async def change_password(request: Request):
    db = request.app.state.db
    form = await request.form()
    current = form.get("current_password", "")
    new_password = form.get("new_password", "").strip()
    confirm = form.get("confirm_password", "").strip()

    password_hash = await db.get_setting("password_hash")
    if not password_hash or not verify_password(current, password_hash):
        return _flash_redirect("/settings", "Current password is incorrect.", "error")
    if len(new_password) < 8:
        return _flash_redirect("/settings", "New password must be at least 8 characters.", "error")
    if new_password != confirm:
        return _flash_redirect("/settings", "New passwords do not match.", "error")

    await db.set_setting("password_hash", hash_password(new_password))
    secret = _get_secret(request.app.state.config["db_path"])
    cookie = create_session_cookie(secret)
    response = _flash_redirect("/settings", "Password changed successfully.", "success")
    response.set_cookie(
        COOKIE_NAME, cookie,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/settings/oidc")
async def settings_save_oidc(request: Request):
    db = request.app.state.db
    form = await request.form()
    config = {
        "issuer_url": form.get("issuer_url", "").strip().rstrip("/"),
        "client_id": form.get("client_id", "").strip(),
        "client_secret": form.get("client_secret", "").strip(),
    }
    if not config["issuer_url"] or not config["client_id"]:
        return _flash_redirect("/settings", "Issuer URL and Client ID are required.", "error")
    meta = await oidc_module.validate_oidc_config(config)
    if not meta:
        return _flash_redirect("/settings", "Could not reach OIDC provider. Check the Issuer URL.", "error")
    await db.save_oidc_config(config)
    await db.delete_setting("oidc_sub")
    return _flash_redirect("/settings", "OIDC configured. Log in with SSO to complete linking.", "success")


@router.post("/settings/oidc/remove")
async def settings_remove_oidc(request: Request):
    db = request.app.state.db
    has_password = await db.get_setting("password_hash") is not None
    if not has_password:
        return _flash_redirect("/settings", "Cannot remove OIDC — it is your only authentication method. Set a password first.", "error")
    await db.save_oidc_config(None)
    return _flash_redirect("/settings", "OIDC configuration removed.", "success")


@router.post("/settings/backup")
async def create_backup_route(request: Request):
    config = request.app.state.config
    result = create_backup(config["db_path"])
    if result:
        return _flash_redirect("/settings", "Backup created successfully", "success")
    return _flash_redirect("/settings", "Backup failed — check logs", "error")


@router.post("/settings/restore/{filename}")
async def restore_backup_route(request: Request, filename: str):
    if not filename:
        return _flash_redirect("/settings", "No backup selected", "error")

    # A backup is a snapshot of *local* single-player state only. The Worker holds
    # the authoritative PvP ledger (items, renown, raids) and does not roll back,
    # so restoring while PvP is on would desync the two — and worse, let a player
    # roll back the provisions/marks they spent minting a Worker-held item while
    # keeping the item. Block restore whenever PvP is enabled.
    manager = getattr(request.app.state, "multiplayer_manager", None)
    if manager and manager.pvp_enabled:
        return _flash_redirect(
            "/settings",
            "Restore is disabled while PvP is enabled — a local backup can't roll "
            "back your multiplayer inventory or renown, and restoring would desync "
            "you from the war ledger. Contact support on Discord "
            "(https://discord.gg/EHXemsA2SS) if you need help recovering.",
            "error",
        )

    config = request.app.state.config
    db = request.app.state.db
    await db.close()

    success = restore_backup(config["db_path"], filename)
    await db.connect()

    # The restored DB carries an older survey push cursor; re-anchor it to now so
    # the next sync doesn't replay already-counted surveys to the Worker.
    if success and manager and manager.registered:
        try:
            await manager.reanchor_push_cursor()
        except Exception:
            log.exception("Failed to re-anchor push cursor after restore")

    if success:
        return _flash_redirect("/settings", f"Restored from {filename}. Verify your data.", "success")
    return _flash_redirect("/settings", "Restore failed — check logs", "error")


# --- Companion Configuration ---

# Read-only diagnostics: reports whether the selected node has a learned mesh
# route and how old it is. Routing itself is now automatic (distance-based) —
# there is no player-settable travel mode any more, so there is no POST here.
@router.get("/api/travel-mode")
async def get_travel_mode(request: Request):
    radio = request.app.state.radio
    db = request.app.state.db
    player = await db.get_first_player()
    node_key = player.get("last_survey_sender", "") if player else ""
    last_ts = radio.get_last_contact_ts(node_key) if node_key else None
    age_str = None
    if last_ts is not None:
        age = int(time.time()) - last_ts
        if age < 60:
            age_str = f"{age}s"
        elif age < 3600:
            age_str = f"{age // 60}m"
        elif age < 86400:
            age_str = f"{age // 3600}h"
        else:
            age_str = f"{age // 86400}d"
    return JSONResponse({
        "last_contact_ts": last_ts,
        "last_contact_age": age_str,
        "has_route": last_ts is not None,
    })


@router.get("/api/telemetry-stats")
async def api_telemetry_stats(request: Request):
    radio = request.app.state.radio
    log_path = getattr(radio, '_telemetry_log_path', '')
    if not log_path:
        return JSONResponse({"entries": [], "stats": {}})
    import os as _os
    if not _os.path.exists(log_path):
        return JSONResponse({"entries": [], "stats": {}})
    try:
        with open(log_path, "r") as f:
            lines = f.readlines()
        entries = []
        for line in lines:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

        # "Responded" = the node sent a telemetry reply back, so the mesh route
        # delivered — that's what these stats measure. A reply with GPS disabled
        # on the node (reason="no_gps") still means the route worked, so it
        # counts as a hit even though the in-game survey couldn't use it. (A
        # cooldown/"already surveyed" rejection happens later in the engine and
        # never reaches this log — the telemetry round-trip already succeeded.)
        # Local companion/link errors ("error") say nothing about the route, so
        # they're excluded from every rate and timing figure below.
        def _responded(e):
            return bool(e.get("success")) or e.get("reason") == "no_gps"

        scored = [e for e in entries if e.get("reason") != "error"]
        successes = [e["elapsed_s"] for e in scored if _responded(e)]
        failures = [e["elapsed_s"] for e in scored if not _responded(e)]
        stats = {
            "total": len(scored),
            "successes": len(successes),
            "failures": len(failures),
        }
        if successes:
            successes.sort()
            stats["avg_s"] = round(sum(successes) / len(successes), 1)
            stats["min_s"] = successes[0]
            stats["max_s"] = successes[-1]
            mid = len(successes) // 2
            stats["median_s"] = successes[mid] if len(successes) % 2 else round((successes[mid - 1] + successes[mid]) / 2, 1)
            buckets = [0] * 6
            for v in successes:
                if v < 10: buckets[0] += 1
                elif v < 15: buckets[1] += 1
                elif v < 20: buckets[2] += 1
                elif v < 25: buckets[3] += 1
                elif v < 30: buckets[4] += 1
                elif v < 35: buckets[5] += 1
            stats["histogram"] = {
                "labels": ["1-10s", "10-15s", "15-20s", "20-25s", "25-30s", "30-35s"],
                "values": buckets,
            }
            p95_idx = int(len(successes) * 0.95)
            stats["p95_s"] = successes[min(p95_idx, len(successes) - 1)]

        # Per-route hit rate (last_path vs flood). Automatic routing no longer
        # differentiates walking/driving — the last-path rate here is what the
        # smart router is judged against. Counts any reply (incl. GPS-off) as a
        # hit, matching _responded above.
        def _rate(items):
            att = len(items)
            ok = sum(1 for e in items if _responded(e))
            return {"attempts": att, "successes": ok,
                    "rate": round(ok / att * 100) if att else None}

        routed = [e for e in scored if e.get("route")]
        by_route = {}
        for route in ("last_path", "flood"):
            items = [e for e in routed if e.get("route") == route]
            if not items:
                continue
            by_route[route] = _rate(items)
        if by_route:
            stats["by_route"] = by_route

        # Smart-routing model state (learned path-reach D + how far along the
        # learner is), so the diagnostics can show whether last-path is worth
        # attempting yet. Optional — mock/basic adapters may not expose it.
        model_fn = getattr(radio, "get_routing_model_stats", None)
        if callable(model_fn):
            try:
                stats["routing_model"] = model_fn()
            except Exception:
                pass
        return JSONResponse({"entries": entries[-50:], "stats": stats})
    except Exception:
        return JSONResponse({"entries": [], "stats": {}})


@router.get("/api/companion/config")
async def get_companion_config(request: Request):
    radio = request.app.state.radio
    config = radio.get_connection_config()
    status = await radio.get_companion_status()
    return JSONResponse({**config, "connected": status.get("connected", False)})


@router.post("/api/companion/config")
async def save_companion_config(request: Request):
    data = await request.json()
    connection_type = data.get("connection_type", "wifi")
    companion_host = data.get("companion_host", "").strip()
    companion_port = int(data.get("companion_port", 4000))
    serial_port = data.get("serial_port", "/dev/ttyUSB0").strip()
    ble_address = data.get("ble_address", "").strip()
    ble_pin = data.get("ble_pin", "").strip()

    if connection_type not in ("wifi", "usb", "ble"):
        return JSONResponse({"ok": False, "error": "Invalid connection type"}, status_code=400)
    if connection_type == "wifi" and not companion_host:
        return JSONResponse({"ok": False, "error": "Host is required for WiFi"}, status_code=400)
    if connection_type == "ble" and not ble_address:
        return JSONResponse({"ok": False, "error": "BLE address is required"}, status_code=400)

    cfg = {
        "connection_type": connection_type,
        "companion_host": companion_host,
        "companion_port": companion_port,
        "serial_port": serial_port,
        "ble_address": ble_address,
        "ble_pin": ble_pin,
    }

    db = request.app.state.db
    await db.save_companion_config(cfg)

    radio = request.app.state.radio
    try:
        await radio.reconfigure(
            connection_type=connection_type,
            host=companion_host,
            port=companion_port,
            serial_port=serial_port,
            ble_address=ble_address,
            ble_pin=ble_pin,
        )
        request.app.state.config.update(cfg)
        return JSONResponse({"ok": True, "message": "Connected successfully"})
    except Exception as e:
        request.app.state.config.update(cfg)
        return JSONResponse({"ok": False, "error": f"Saved but connection failed: {e}"}, status_code=200)


@router.post("/api/companion/test")
async def test_companion_connection(request: Request):
    data = await request.json()
    from ..radio.meshcore_adapter import MeshCoreAdapter
    test_adapter = MeshCoreAdapter(
        connection_type=data.get("connection_type", "wifi"),
        host=data.get("companion_host", ""),
        port=int(data.get("companion_port", 4000)),
        serial_port=data.get("serial_port", "/dev/ttyUSB0"),
        ble_address=data.get("ble_address", ""),
        ble_pin=data.get("ble_pin", ""),
    )
    try:
        await test_adapter.connect()
        status = await test_adapter.get_companion_status()
        await test_adapter.disconnect()
        return JSONResponse({"ok": True, "status": status})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/api/companion/scan-ble")
async def scan_ble(request: Request):
    from ..radio.meshcore_adapter import MeshCoreAdapter
    devices = await MeshCoreAdapter.scan_ble(timeout=5.0)
    return JSONResponse({"devices": devices})


# --- Help ---

@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    db = request.app.state.db
    config = request.app.state.config
    radio = request.app.state.radio
    mp_manager = getattr(request.app.state, "multiplayer_manager", None)
    player = await db.get_first_player()
    key = player["key"] if player else None
    discovered_relics = await db.get_discovered_relic_types(key) if key else set()

    # Bug-report diagnostics: a deliberately minimal, non-sensitive snapshot the
    # player can copy into a GitHub issue or Discord. Everything here is either
    # environmental (version/OS) or a coarse boolean/type — NEVER coordinates,
    # pubkeys, node/display names, hosts, or logs. The app itself sends nothing;
    # the player is the transport. Keep this list conservative.
    companion = await radio.get_companion_status() if radio else {}
    diagnostics = {
        "version": __version__,
        "install": install_method(),
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": platform.python_version(),
        "connection_type": config.get("connection_type", "unknown"),
        "companion_connected": bool(companion.get("connected")),
        "multiplayer_registered": bool(mp_manager and mp_manager.registered),
        "pvp_enabled": bool(mp_manager and getattr(mp_manager, "pvp_enabled", False)),
    }

    return await _template(request, "help.html", {
        "player": player,
        "charter_prov_cost": CHARTER_PROVISION_COST,
        "charter_mark_cost": CHARTER_MARK_COST,
        "charter_min_dist": CHARTER_MIN_DISTANCE_MILES,
        "buried_cache_amount": BURIED_CACHE_AMOUNT,
        "discovered_relics": discovered_relics,
        "diagnostics": diagnostics,
        "issue_url": "https://github.com/hornofabraxas/lora-the-explorer/issues/new",
        "discord_url": "https://discord.gg/EHXemsA2SS",
        "nav_active": "help",
    })


# --- Hex Map API (embedded on dashboard) ---

@router.get("/api/hexes")
async def api_hexes(request: Request):
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return JSONResponse({"hexes": [], "repeaters": []})

    hexes = await db.get_all_hexes(key)
    player = await db.get_first_player()
    home_hex = h3.latlng_to_cell(player["home_lat"], player["home_lon"], 8) if player else None

    # Which post hexes actually need tending, so the Radio can gild the Upkeep
    # button only when standing in a post that's fading or already in ruin
    # (a freshly-upkept, warded, or full-runway post stays neutral).
    now = int(time.time())
    grace = upkeep_grace_days(player) if player else 0
    upkeep_due: dict[str, bool] = {}
    for p in await db.get_all_posts(key):
        warded_until = p.get("ruin_frozen_until")
        if warded_until and now < warded_until:
            continue  # dormant — protected from ruin, no upkeep needed
        ruin = _ruin_state(p["last_tended_at"], grace)
        upkeep_due[p["hex_id"]] = ruin["state"] in ("fading", "ruined")

    features = []
    for hx in hexes:
        lat, lng = h3.cell_to_latlng(hx["hex_id"])
        boundary = h3.cell_to_boundary(hx["hex_id"])
        hex_type = "post" if hx["post_name"] else "discovered"
        feature = {
            "hex_id": hx["hex_id"],
            "name": "Base Camp" if hx["hex_id"] == home_hex else hex_name(hx["hex_id"]),
            "lat": lat,
            "lng": lng,
            "boundary": [[b[0], b[1]] for b in boundary],
            "type": hex_type,
            "on_cooldown": bool(hx.get("on_cooldown")),
            "discovered_at": hx["discovered_at"],
            "survey_count": hx["survey_count"],
            "post_name": hx["post_name"],
            "post_level": hx["post_level"],
            "upkeep_due": upkeep_due.get(hx["hex_id"], False),
        }
        if hx.get("last_survey_lat") is not None:
            feature["survey_lat"] = hx["last_survey_lat"]
            feature["survey_lng"] = hx["last_survey_lon"]
            feature["survey_id"] = hx.get("survey_id")
            feature["distance_miles"] = hx.get("distance_miles")
            feature["surveyed_at"] = hx.get("surveyed_at")
        features.append(feature)

    repeaters = await db.get_mesh_repeaters()
    return JSONResponse({"hexes": features, "repeaters": repeaters})


@router.delete("/api/surveys/{survey_id}")
async def delete_survey(survey_id: int, request: Request):
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return JSONResponse({"error": "No player"}, status_code=404)

    survey = await db.get_survey(survey_id)
    if not survey or survey["player_key"] != key:
        return JSONResponse({"error": "Survey not found"}, status_code=404)

    xp = survey["xp_earned"]
    prov = survey["provisions_earned"]
    marks = 1 if survey["is_discovery"] else 0
    hex_id = survey["hex_id"]
    surveyed_at = survey["surveyed_at"]

    await db.delete_survey(survey_id)

    remaining = await db.get_hex_survey_count(key, hex_id)
    if remaining == 0 and survey["is_discovery"]:
        await db.delete_hex_discovery(key, hex_id)

    await db.rollback_survey_rewards(key, xp, prov, marks)

    revoked = await db.revoke_postcards_near(key, surveyed_at)

    player = await db.get_player(key)
    if player:
        new_xp = player["xp"]
        correct_rank = 0
        for lvl in sorted(RANK_THRESHOLDS):
            if new_xp >= RANK_THRESHOLDS[lvl]["xp"]:
                correct_rank = lvl
            else:
                break
        if correct_rank < player["rank_level"]:
            await db.set_rank(key, correct_rank)

    return JSONResponse({"ok": True, "rolled_back": {"xp": xp, "provisions": prov, "marks": marks, "postcards_revoked": revoked}})


@router.get("/api/posts")
async def api_posts(request: Request):
    db = request.app.state.db
    key = await _get_player_key(db)
    if not key:
        return JSONResponse([])

    posts = await db.get_all_posts(key)
    result = []
    for p in posts:
        lat, lng = h3.cell_to_latlng(p["hex_id"])
        result.append({
            "id": p["id"],
            "name": p["name"],
            "level": p["level"],
            "hex_id": p["hex_id"],
            "lat": lat,
            "lng": lng,
        })
    return JSONResponse(result)
