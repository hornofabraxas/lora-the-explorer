import asyncio
import hashlib
import json
import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone

import h3

from .commands import parse_command, CommandType, ParsedCommand
from .database import Database
from .geo import distance_between
from .hex_names import hex_name
from ..radio.adapter import RadioAdapter, IncomingMessage, PositionFailure, PositionResult

log = logging.getLogger(__name__)

CHARTER_TIMEOUT = 300  # 5 minutes

DISCOVERY_XP_BONUS = 75
DISCOVERY_PROVISION_BONUS = 15

# Survey Marks are the exploration→PvP currency. As of the 2026-07-22 economy
# rework, *every* survey mints marks (SURVEY_MARK_BASE) so surveying — not
# junk-selling salvaged munitions — is the primary faucet. A discovery adds
# DISCOVERY_SURVEY_MARKS on top (base 1 + discovery 1 = 2 on a new-hex survey).
# Item salvage now pays provisions only (see MULTIPLAYER_ITEM_SALVAGE), and mark
# sinks (charter, attacks, shop) were scaled up ~3-4x to match the higher faucet.
SURVEY_MARK_BASE = 1
DISCOVERY_SURVEY_MARKS = 1

# Flat bonus granted on the first successful survey of each UTC day — a small
# streak-flavoured top-up on the per-survey base above.
FIRST_SURVEY_MARK_BONUS = 1

BASE_XP = 20
DISTANCE_XP_FACTOR = 0.15

BASE_PROVISIONS = 16
DISTANCE_PROVISION_FACTOR = 1.2

CHARTER_PROVISION_COST = 20
CHARTER_MARK_COST = 10
CHARTER_MIN_DISTANCE_MILES = 3.0
CHARTER_MIN_LEVEL = 8
CHARTER_MIN_CAMP = 3

# Checkpoint "starter kit": each of a player's first N charters grants bonus
# survey marks to seed the PvP loop. Tracked persistently in settings so razing
# and re-chartering can't farm it. Sized to one charter's mark cost so a new
# player's first posts stay effectively mark-free after the sink rebalance.
CHARTER_CHECKPOINT_MARKS = 10
CHARTER_CHECKPOINT_COUNT = 3
CONTRACTS_MIN_LEVEL = 5

# Marks spent to launch an attack item, by rarity. Scaled up (2/3/4/5 -> 5/10/15/20)
# in the 2026-07-22 rework so the per-survey mark faucet funds roughly one raid
# item per active day rather than accumulating unspent.
ATTACK_MARK_COST = {
    "attack_common": 5,
    "attack_uncommon": 10,
    "attack_rare": 15,
    "attack_epic": 20,
}

# Item combat values — mirror the Worker (src/types.ts) so the client can render
# a live raid damage preview without a round-trip. The Worker stays authoritative.
ATTACK_ITEM_POWER = {
    "attack_common": 30,
    "attack_uncommon": 70,
    "attack_rare": 150,
    "attack_epic": 300,
}
DEFENSE_ITEM_VALUE = {
    "defense_common": 10,
    "defense_uncommon": 25,
    "defense_rare": 50,
    "defense_epic": 100,
}

# Thematic display names for the multiplayer combat munitions. Attack items are
# airdropped "Blasting Powder" (breach a rival's post); defense items are
# "Bulwark" (reinforce your own). The rarity tier rides as a Roman numeral —
# that numeral is the at-a-glance strength signal (Ingress-style), so it's
# rendered in its own chip on the narrow Frontier Merchant shelf where the full
# name would truncate. Keys are untouched; this is presentation only. Mirrored
# in JS by mpItemName() in base.html — keep the two in sync.
MULTIPLAYER_ITEM_TIER = {"common": "I", "uncommon": "II", "rare": "III", "epic": "IV"}


def multiplayer_item_name(item_type: str) -> dict:
    """Display strings for a multiplayer item_type.

    Returns {"full", "short", "tier"} — ``full`` ("Blasting Powder IV") for
    roomy contexts, ``short`` ("Powder") + ``tier`` ("IV") for the width-capped
    merchant shelf so the numeral never gets ellipsized away.
    """
    if item_type == "probe":
        return {"full": "Scout", "short": "Scout", "tier": ""}
    rarity = item_type.split("_")[-1]
    tier = MULTIPLAYER_ITEM_TIER.get(rarity, "")
    is_attack = item_type.startswith("attack")
    name = "Blasting Powder" if is_attack else "Bulwark"
    short = "Powder" if is_attack else "Bulwark"
    return {"full": f"{name} {tier}".strip(), "short": short, "tier": tier}

# Multiplayer items purchasable from the Frontier Merchant. Probes cost
# provisions (PvE currency); attack/defense items cost survey marks (PvP
# currency). Rare/epic stay drop-only. Weekly-limited per type, mirroring the
# relic merchant. Attack items still cost their ATTACK_MARK_COST when used.
MULTIPLAYER_SHOP_CATALOG = {
    "probe":            {"currency": "provisions", "price": 60, "limit": 1},
    "attack_common":    {"currency": "marks",      "price": 25, "limit": 1},
    "attack_uncommon":  {"currency": "marks",      "price": 50, "limit": 1},
    "defense_common":   {"currency": "marks",      "price": 25, "limit": 1},
    "defense_uncommon": {"currency": "marks",      "price": 50, "limit": 1},
}

# Salvaging an unused munition reclaims a lean amount of PROVISIONS (never marks).
# Before the 2026-07-22 rework salvage paid marks, and because engaged players
# drown in surplus drops it became the dominant mark faucet (~6.6x the intended
# survey/contract faucets in live data) — surveying stopped feeling like what
# earns marks. Paying provisions instead keeps salvage a surplus-clearing sink
# while marks come from actually surveying. Values kept lean so it's a sink, not
# a farm; rare/epic are drop-only (never sold) and salvage a little higher.
MULTIPLAYER_ITEM_SALVAGE = {
    "probe":            {"currency": "provisions", "value": 30},
    "attack_common":    {"currency": "provisions", "value": 10},
    "attack_uncommon":  {"currency": "provisions", "value": 20},
    "attack_rare":      {"currency": "provisions", "value": 40},
    "attack_epic":      {"currency": "provisions", "value": 80},
    "defense_common":   {"currency": "provisions", "value": 10},
    "defense_uncommon": {"currency": "provisions", "value": 20},
    "defense_rare":     {"currency": "provisions", "value": 40},
    "defense_epic":     {"currency": "provisions", "value": 80},
}

MAX_VELOCITY_MPH = 150
DAILY_SURVEY_CAP = 50
# Uniform minimum spacing between successful surveys — a radio/mesh limit that
# applies to every survey (manual tap or hands-free auto). Sized at the flood
# telemetry round-trip (~35s), so a walker (hexes minutes apart) never feels it,
# but a fast mover can't out-tap the auto-surveyor. This removes any incentive to
# tap while driving without measuring speed or distinguishing driver/passenger.
SURVEY_MIN_INTERVAL_S = 35
# When the adapter reports the mesh is congested (channel busy over its backoff
# threshold), the per-player interval is multiplied by this so the game eases
# off a channel that's already loaded — its half of being a good neighbor.
CONGESTION_INTERVAL_MULT = 2

# Commands that require a GPS fix and are processed off the receive loop as a
# single backgrounded task. CHARTER_NAME completes a pending charter.
_COMMAND_DISPLAY_NAMES = {
    CommandType.SURVEY: "survey",
    CommandType.CHARTER: "charter",
    CommandType.UPKEEP: "upkeep",
    CommandType.CHARTER_NAME: "charter",
}
_BACKGROUND_COMMANDS = frozenset(_COMMAND_DISPLAY_NAMES)
DAILY_SURVEY_WARNING_THRESHOLD = 40

# XP milestones — levels between are linearly interpolated
_XP_MILESTONES = {
    1: 0, 5: 500, 10: 2500, 15: 7000, 20: 15000,
    25: 25000, 30: 40000, 35: 60000, 40: 85000, 45: 120000, 50: 160000,
}

_RANK_NAMES = [
    (50, "Grandmaster"),
    (40, "Fellow of the World's End"),
    (30, "Expedition Leader"),
    (20, "Pathfinder"),
    (15, "Wayfinder"),
    (10, "Surveyor"),
    (5,  "Scout"),
    (1,  "Novice"),
]


def _interpolate_xp(level: int) -> int:
    if level in _XP_MILESTONES:
        return _XP_MILESTONES[level]
    keys = sorted(_XP_MILESTONES)
    for i in range(len(keys) - 1):
        if keys[i] < level < keys[i + 1]:
            lo, hi = keys[i], keys[i + 1]
            frac = (level - lo) / (hi - lo)
            return int(_XP_MILESTONES[lo] + frac * (_XP_MILESTONES[hi] - _XP_MILESTONES[lo]))
    return 0


def _gps_fail_message(failure: PositionFailure | None, command: str = "Command") -> str:
    now = datetime.now().strftime("%H:%M:%S")
    if failure == PositionFailure.TIMEOUT:
        return f"NO RESPONSE [{now}]\nSpyglass did not respond.\nCheck range and try again."
    elif failure == PositionFailure.NO_GPS:
        return f"NO GPS FIX [{now}]\nSpyglass responded but has\nno GPS lock. Move outdoors."
    else:
        return f"CONNECTION ERROR [{now}]\nBase camp companion\nnot reachable."


def rank_name(level: int) -> str:
    for threshold, name in _RANK_NAMES:
        if level >= threshold:
            return name
    return "Novice"


RANK_THRESHOLDS = {}
for _lvl in range(1, 51):
    RANK_THRESHOLDS[_lvl] = {
        "name": rank_name(_lvl),
        "xp": _interpolate_xp(_lvl),
        "reward_prov": 7 + _lvl * 5,
    }

BASE_CAMP_TABLE = {
    1:  {"name": "Campsite",     "mult": 1.0, "prov": 0},
    2:  {"name": "Shelter",      "mult": 1.1, "prov": 55},
    3:  {"name": "Field Camp",   "mult": 1.2, "prov": 110},
    4:  {"name": "Outpost",      "mult": 1.3, "prov": 210,  "perk": "merchant"},
    5:  {"name": "Lodge",        "mult": 1.4, "prov": 350},
    6:  {"name": "Waystation",   "mult": 1.5, "prov": 550},
    7:  {"name": "Compound",     "mult": 1.6, "prov": 825,  "perk": "upkeep_grace"},
    8:  {"name": "Stronghold",   "mult": 1.7, "prov": 1100},
    9:  {"name": "Fortress",     "mult": 1.8, "prov": 1400},
    10: {"name": "Headquarters", "mult": 2.0, "prov": 1800, "perk": "relic_boost"},
}

CAMP_PERK_DESCRIPTIONS = {
    "merchant": "Unlocks the Frontier Merchant",
    "upkeep_grace": "Outposts hold 3 extra days before ruin",
    "relic_boost": "+5% relic drop rate",
}

POST_UPGRADE_COST = {2: 40, 3: 80, 4: 160, 5: 320}
MAX_POST_LEVEL = 5

# Renown mirrors the Worker's model (lora-worker src/logic/renown.ts): each post
# generates (level x RENOWN_PER_DAY_PER_LEVEL) renown per day, plus a longevity
# bonus of RENOWN_AGE_BONUS_PER_DAY for every day it has survived (uncapped). The
# total is that rising rate integrated over the post's life — the unspendable
# leaderboard score. Mirrored here so the dashboard can show each post's daily
# rate + formula without a Worker round-trip. Keep both constants in sync.
RENOWN_PER_DAY_PER_LEVEL = 3
RENOWN_AGE_BONUS_PER_DAY = 0.5

# Ruin is income-decay, not destruction. An untended post pays full income for
# RUIN_GRACE_DAYS, then its income ramps linearly to zero across RUIN_RAMP_DAYS.
# After that it sits "in ruin" earning nothing. It never loses levels and is
# never destroyed. Only a physical /lora upkeep (from the post's own hex) resets
# the timer — surveying past it does NOT. Camp 7 grants +UPKEEP_GRACE_BONUS_DAYS.
RUIN_GRACE_DAYS = 10
RUIN_RAMP_DAYS = 7
UPKEEP_GRACE_CAMP = 7
UPKEEP_GRACE_BONUS_DAYS = 3

BURIED_CACHE_AMOUNT = 50
# Warding: a wardstone puts an outpost dormant (no income, no ruin, raid-immune)
# for a player-chosen span. Clamp keeps it sane and prevents permanent immunity.
WARD_MIN_DAYS = 1
WARD_MAX_DAYS = 30


def clamp_ward_days(days: int) -> int:
    return max(WARD_MIN_DAYS, min(WARD_MAX_DAYS, int(days)))


def _ward_overlap(post: dict, start: int, end: int) -> int:
    """Seconds of [start, end] that fall inside the post's ward window."""
    ward_start = post.get("warded_at")
    ward_end = post.get("ruin_frozen_until")
    if not ward_start or not ward_end:
        return 0
    lo = max(start, ward_start)
    hi = min(end, ward_end)
    return max(0, hi - lo)


def upkeep_grace_days(player: dict) -> float:
    """Full-income grace window for this player's posts (camp 7 extends it)."""
    bonus = UPKEEP_GRACE_BONUS_DAYS if player.get("base_camp_level", 1) >= UPKEEP_GRACE_CAMP else 0
    return RUIN_GRACE_DAYS + bonus


def ruin_income_factor(age_days: float, grace: float = RUIN_GRACE_DAYS) -> float:
    """Instantaneous income multiplier for a post `age_days` since its last upkeep."""
    ramp_end = grace + RUIN_RAMP_DAYS
    if age_days <= grace:
        return 1.0
    if age_days >= ramp_end:
        return 0.0
    return (ramp_end - age_days) / RUIN_RAMP_DAYS


def ruin_effective_days(age_start: float, age_end: float, grace: float = RUIN_GRACE_DAYS) -> float:
    """Integral of the income factor over an age window [age_start, age_end] (days).

    Because income is collected in lumps but the decay is continuous, we integrate
    the piecewise-linear factor across the window rather than sampling it once — so
    a post that sat untended for 20 days still earns full for its first 10 days,
    ramped for the next 7, and nothing after.
    """
    ramp_end = grace + RUIN_RAMP_DAYS

    def antideriv(a: float) -> float:
        if a <= grace:
            return a
        if a >= ramp_end:
            return grace + RUIN_RAMP_DAYS / 2.0
        return grace + ((ramp_end * (a - grace)) - (a * a - grace * grace) / 2.0) / RUIN_RAMP_DAYS

    return max(0.0, antideriv(age_end) - antideriv(age_start))


RELIC_DROP_TABLE = [
    ("buried_cache", 0.16),
    ("vigor_tonic", 0.10),
    ("wardstone", 0.06),
]

RELIC_ROLLING_WINDOW = 7 * 86400
RELIC_DIMINISH_FACTOR = 0.5
# Base Camp level whose perk grants a standing relic-drop boost (Headquarters),
# and the multiplier it applies. Matches BASE_CAMP_TABLE[10]'s "relic_boost".
RELIC_BOOST_CAMP = 10
CAMP_RELIC_BOOST = 1.05

HEX_AREA_SQ_MI = 0.31

STRONGBOX_PROVISIONS = 200

FIELD_TRAINING_CLASS = "Field Training"
FIELD_TRAINING_POSTCARDS = [
    "Staking Claim",
    "First Contact",
    "Long Range",
    "Cartographer",
    "Relic Hunter",
]

DISPATCH_EFFECTS = [
    {
        "id": "bonus_xp",
        "message": "Society analysts reviewing current surveys with interest. +15% XP on all surveys.",
    },
    {
        "id": "bonus_provisions",
        "message": "Regional supply drop along common routes. +20% provisions on all surveys.",
    },
    {
        "id": "momentum_boost",
        "message": "Morale high across the Society's ranks. Momentum XP bonus doubled today.",
    },
    {
        "id": "relic_boost",
        "message": "Ground teams flagged possible dig sites in the area. Relic drop rate doubled.",
    },
    {
        "id": "charter_discount",
        "message": "The Society is funding outpost expansion. Charter costs halved.",
    },
    {
        "id": "extended_range",
        "message": "Clear conditions across the network. Distance XP bonus doubled.",
    },
    {
        "id": "survey_mark_bonus",
        "message": "Cartography division offering bounties on unmapped territory. +1 extra mark per discovery.",
    },
    {
        "id": "upkeep_grace",
        "message": "Maintenance crews deployed across the territory. Upkeep today buys extra days before ruin.",
    },
]


def get_daily_dispatch(utc_timestamp: float | None = None) -> dict:
    ts = utc_timestamp if utc_timestamp is not None else time.time()
    day_number = int(ts) // 86400
    cycle = day_number // len(DISPATCH_EFFECTS)
    slot = day_number % len(DISPATCH_EFFECTS)
    seed = hashlib.sha256(f"dispatch-cycle-{cycle}".encode()).digest()
    rng = random.Random(int.from_bytes(seed[:8], "big"))
    order = list(range(len(DISPATCH_EFFECTS)))
    rng.shuffle(order)
    return DISPATCH_EFFECTS[order[slot]]


async def generate_analysts_report(
    db, player: dict, posts: list[dict], contracts: list[dict],
    ft_complete: bool, strongbox_claimed: bool, mp: dict | None = None,
) -> str:
    """Build a brief Society Analysts' Report based on player state.

    `mp` carries pre-gathered, cached multiplayer state (inbound raids, the
    player's own raiding party, latest supply drop) so the briefing can surface
    combat and supply events the player may have missed. Its shape:
        {"incoming": [{"post": str, "eta_min": int, "threat": str}, ...],
         "outgoing": {"target": str, "eta_min": int} | None,
         "supply":   str | None}   # item summary, e.g. "2× Blasting Powder I"

    Returns empty string if nothing actionable to report.
    """
    key = player["key"]
    now = int(time.time())
    urgent = []
    nudges = []

    grace = upkeep_grace_days(player)
    decaying = 0  # already losing income
    approaching = []  # days until income starts to fade
    for p in posts:
        frozen_until = p.get("ruin_frozen_until")
        if frozen_until and now < frozen_until:
            continue
        age_days = (now - p["last_tended_at"]) / 86400
        if age_days >= grace:
            decaying += 1
        elif grace - age_days <= 3.0:
            approaching.append(grace - age_days)
    if decaying:
        noun = "Survey Post" if decaying == 1 else "Survey Posts"
        verb = "is" if decaying == 1 else "are"
        urgent.append(f"{decaying} {noun} {verb} falling into ruin — visit and run /lora upkeep")
    if approaching:
        n = len(approaching)
        worst = min(approaching)
        noun = "Survey Post" if n == 1 else "Survey Posts"
        nudges.append(f"{n} {noun} will fall into ruin in {worst:.0f} days — /lora upkeep on site")

    expiring_contracts = []
    period_end = _contract_period_start_utc() + CONTRACT_PERIOD_DAYS * 86400
    hours_left = (period_end - now) / 3600
    if hours_left <= 36:
        for c in contracts:
            if c.get("purchased") and not c.get("completed"):
                expiring_contracts.append(c)
        if expiring_contracts:
            n = len(expiring_contracts)
            if n == 1:
                label = CONTRACT_OBJECTIVES.get(
                    expiring_contracts[0]["objective"], {},
                ).get("label", "contract")
                urgent.append(f"your {label} contract expires soon")
            else:
                urgent.append(f"{n} contracts expire soon")

    if not ft_complete:
        ft_postcards = await db.get_postcards_by_class(key, FIELD_TRAINING_CLASS)
        ft_earned = {c["description"] for c in ft_postcards}
        remaining_ft = [
            name for name in FIELD_TRAINING_POSTCARDS if name not in ft_earned
        ]
        if remaining_ft:
            n = len(remaining_ft)
            nudges.append(
                f"{n} Field Training objective{'s' if n != 1 else ''} remain{'s' if n == 1 else ''} before full Society clearance"
            )
    elif not strongbox_claimed:
        nudges.append("your Society Strongbox is ready to claim")

    mp = mp or {}

    # Latest supply drop the player may have missed — bundles run hourly, so a
    # player who stopped for the day can easily miss one. Surfacing it here is
    # the whole point of a briefing page. Prepended ahead of the standing nudges
    # (FT/strongbox) because it's a transient event, not a persistent status.
    supply = mp.get("supply")
    if supply:
        nudges.insert(0, f"latest supply drop brought {supply}")

    # The player's own raiding party in flight — also transient and worth flagging.
    outgoing = mp.get("outgoing")
    if outgoing:
        eta = outgoing.get("eta_min", 0)
        target = outgoing.get("target", "the target")
        if eta <= 0:
            nudges.insert(0, f"your raiding party has reached {target}")
        else:
            nudges.insert(0, f"your raiding party strikes {target} in ~{eta}m")

    if not urgent and not nudges:
        next_level = player["base_camp_level"] + 1
        if next_level in BASE_CAMP_TABLE:
            req = BASE_CAMP_TABLE[next_level]
            if player["provisions"] >= req["prov"]:
                nudges.append(
                    f"supplies sufficient to upgrade to {req['name']}"
                )

    # Inbound raids are the single most consequential event in the game: they
    # always lead the report and are never dropped by the length cap.
    incoming_lines = []
    incoming = mp.get("incoming") or []
    if len(incoming) == 1:
        r = incoming[0]
        threat = {
            "raze": "projected to raze your outpost",
            "heavy": "projected heavy damage",
            "hold": "your defenses should hold",
        }.get(r.get("threat", "hold"), "inbound")
        incoming_lines.append(
            f"raid inbound on {r['post']} — ETA {r['eta_min']}m, {threat}"
        )
    elif incoming:
        soonest = min(r["eta_min"] for r in incoming)
        incoming_lines.append(
            f"{len(incoming)} raids inbound — soonest ETA {soonest}m"
        )

    items = incoming_lines + (urgent + nudges)[:3]
    if not items:
        return ""

    report = ". ".join(
        s[0].upper() + s[1:] for s in items
    ) + "."
    return report


MAX_SURVEY_POSTS = 3


def max_posts_for_camp(camp_level: int) -> int:
    if camp_level < CHARTER_MIN_CAMP:
        return 0
    return MAX_SURVEY_POSTS


def camp_name(camp_level: int) -> str:
    return BASE_CAMP_TABLE.get(camp_level, BASE_CAMP_TABLE[1])["name"]


def post_multiplier(post_level: int) -> float:
    return 1.0 + post_level * 0.1


CONTRACT_OBJECTIVES = {
    "survey_sweep": {"label": "Survey Sweep", "desc": "Survey {target} territories this week"},
    "new_horizons": {"label": "New Horizons", "desc": "Discover {target} new territories this week"},
    "long_shot": {"label": "Long Shot", "desc": "Survey from {target}+ miles away"},
    "daily_patrol": {"label": "Daily Patrol", "desc": "Survey on {target} different days this week"},
    "grand_traverse": {"label": "Grand Traverse", "desc": "Survey {target}+ sq mi of territory this week"},
}

CONTRACT_OBJECTIVE_TARGETS = {
    "survey_sweep": [6, 8, 10],
    "new_horizons": [2, 3, 4],
    "long_shot": [3, 5, 8],
    "daily_patrol": [3, 4, 5],
    "grand_traverse": [1, 2, 3],
}

# Expedition Contracts are provision *sinks*: you spend provisions up front for a
# reward that is never more provisions. Cost and reward are paired per tier (a
# pricier contract always pays a richer reward) — they used to be rolled
# independently, which let a 40-prov contract pay back 15 prov (a net loss). The
# ladder climbs marks → marks → relic; the top (relic) tier can additionally roll
# a tier-IV combat munition instead of a relic, but only for PvP-enabled players
# (see CONTRACT_PREMIUM_ITEM_CHANCE). Values are deliberately steep — an engaged
# player earns ~40 prov in a couple of surveys, so cheap contracts weren't a sink.
CONTRACT_TIERS = [
    {"cost": (50, 70),   "reward_type": "survey_marks", "reward": (8, 12)},
    {"cost": (90, 120),  "reward_type": "survey_marks", "reward": (15, 22)},
    {"cost": (150, 210), "reward_type": "relic",        "reward": (1, 1)},
]

# The most expensive (relic) tier can instead pay a tier-IV combat munition —
# gear you can otherwise only get from a rare drop. PvP-only: rolled at contract
# generation, and only when the player has PvP enabled (item lives on the Worker).
CONTRACT_PREMIUM_ITEM_CHANCE = 0.35
CONTRACT_ITEM_REWARD_TYPES = ("attack_epic", "defense_epic")

BASE_CONTRACTS_PER_PERIOD = 2
CONTRACT_PERIOD_DAYS = 7


def _week_start_utc() -> int:
    now = datetime.now(timezone.utc)
    monday = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday -= timedelta(days=now.weekday())
    return int(monday.timestamp())


def _contract_period_start_utc() -> int:
    return _week_start_utc()


# How many PvP Supplies the Frontier Merchant stocks each weekly restock,
# chosen at random from MULTIPLAYER_SHOP_CATALOG.
MERCHANT_ITEM_STOCK = 2


def weekly_merchant_item_types(player_key: str, count: int = MERCHANT_ITEM_STOCK):
    """Deterministically choose which PvP Supplies the Frontier Merchant stocks
    this week for a given player. Stable across page reloads within a restock
    window and reshuffles when the merchant restocks; seeded by player + week so
    the display and the buy validation always agree."""
    types = list(MULTIPLAYER_SHOP_CATALOG.keys())
    if count >= len(types):
        return set(types)
    rng = random.Random(f"{player_key}:{_week_start_utc()}")
    return set(rng.sample(types, count))


TRAILBLAZER_MILESTONES = [5, 10, 25, 50, 100]
RELENTLESS_MILESTONES = [10, 25, 50, 100]
STRIDER_MILESTONES = [5, 10, 15, 25, 35]
STEADFAST_MILESTONES = [7, 14, 30, 60, 90]
BOUNDLESS_MILESTONES = [5, 15, 50, 100, 200]


class GameEngine:
    def __init__(
        self,
        adapter: RadioAdapter,
        home_lat: float,
        home_lon: float,
        db: Database | None = None,
    ):
        self._adapter = adapter
        self._home_lat = home_lat
        self._home_lon = home_lon
        self._home_hex = h3.latlng_to_cell(home_lat, home_lon, 8)
        self._db = db or Database()
        self._pending_charters: dict[str, dict] = {}
        # Single slot: survey, charter, upkeep, and charter-naming all run as one
        # backgrounded command at a time so the radio receive loop never blocks
        # on a GPS request.
        self._command_task: asyncio.Task | None = None
        self._recent_messages: dict[tuple, float] = {}
        self._dedup_window = 30
        # Fan-out: every live SSE connection registers its own bounded queue so a
        # published event is broadcast to all of them. (A single shared queue would
        # hand each event to just one consumer, so a second tab — or a zombie
        # coroutine for a dropped connection that the server hasn't reaped yet —
        # would steal events from the live client. See subscribe/unsubscribe.)
        self._subscribers: set[asyncio.Queue] = set()
        # Client-facing monotonic event id, assigned synchronously so live events
        # carry the same id they'll have in the DB. Lets the client advance its
        # last-seen id on live events (not just DB replays) and dedup reconnect
        # replays exactly. Seeded from the DB's max id in start().
        self._event_seq = 0
        self._event_history: list[dict] = []
        self._event_history_max = 20
        self._shutting_down = False
        self._prune_counter = 0

    def subscribe(self) -> "asyncio.Queue":
        """Register a live-event queue for one SSE connection."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue") -> None:
        self._subscribers.discard(q)

    def _publish_event(self, event_type: str, data: dict) -> None:
        import json as _json
        ts = int(time.time())
        self._event_seq += 1
        event = {"id": self._event_seq, "type": event_type, "ts": ts, "data": data}
        self._event_history.append(event)
        if len(self._event_history) > self._event_history_max:
            self._event_history = self._event_history[-self._event_history_max:]
        if not self._shutting_down and self._db._db:
            try:
                fut = asyncio.ensure_future(
                    self._db.insert_event(
                        event_type, ts, _json.dumps(data), event_id=self._event_seq
                    )
                )
                fut.add_done_callback(self._on_event_persisted)
            except RuntimeError:
                pass
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # drop this subscriber's oldest, keep it live
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    def _on_event_persisted(self, fut):
        if fut.exception():
            return
        self._prune_counter += 1
        if self._prune_counter >= 50:
            self._prune_counter = 0
            if not self._shutting_down and self._db._db:
                asyncio.ensure_future(self._db.prune_events())

    # Keep in sync with FEED_MAX_AGE_SEC in spyglass.html (radio feed window).
    FEED_MAX_AGE_SEC = 7200

    async def get_recent_events_from_db(self, limit: int = 20) -> list[dict]:
        import json as _json
        import time as _time
        min_ts = int(_time.time()) - self.FEED_MAX_AGE_SEC
        rows = await self._db.get_recent_events(limit, min_ts=min_ts)
        return [
            {"id": r["id"], "type": r["type"], "ts": r["ts"], "data": _json.loads(r["data"])}
            for r in rows
        ]

    async def get_events_since(self, last_id: int, limit: int = 50) -> list[dict]:
        import json as _json
        rows = await self._db.get_events_since(last_id, limit)
        return [
            {"id": r["id"], "type": r["type"], "ts": r["ts"], "data": _json.loads(r["data"])}
            for r in rows
        ]

    async def start(self) -> None:
        if not self._db._db:
            await self._db.connect()
        # Continue event ids from where the DB left off so live-event ids stay
        # monotonic and unique across restarts.
        self._event_seq = await self._db.get_max_event_id()
        await self._adapter.set_message_handler(self._handle_message)
        await self._adapter.connect()
        log.info(
            "Game engine started. Base camp at %.4f, %.4f",
            self._home_lat,
            self._home_lon,
        )
        try:
            await self.refresh_repeaters()
        except Exception:
            log.exception("Initial repeater refresh failed")
        try:
            await self.reconcile_known_node_names()
        except Exception:
            log.exception("Spyglass name reconcile failed")

    async def refresh_repeaters(self) -> int:
        import time
        repeaters = await self._adapter.get_repeaters()
        if not repeaters:
            return 0
        now = int(time.time())
        for r in repeaters:
            r["updated_at"] = now
        await self._db.replace_mesh_repeaters(repeaters)
        log.info("Cached %d mesh repeaters", len(repeaters))
        return len(repeaters)

    async def reconcile_known_node_names(self) -> int:
        """Refresh stored spyglass display names from the companion's contacts.

        `known_nodes.name` is only written when a command arrives, so a node
        renamed over BLE keeps its old name in the Radio selector and the
        Settings test-message picker until it next transmits. Contacts are
        reloaded fresh from the companion on connect, so reconciling here (on
        start/reconnect) means a container restart — the usual player
        troubleshooting step — picks up renames. Returns the number updated."""
        contacts = self._adapter.get_contacts()
        if not contacts:
            return 0
        updated = 0
        for node in await self._db.get_known_nodes():
            key = node["key"]
            contact = contacts.get(key)
            if contact is None:
                # known_nodes stores the sender-key prefix; contacts are keyed
                # by full public key. Match either direction.
                contact = next(
                    (c for k, c in contacts.items()
                     if k.startswith(key) or key.startswith(k)),
                    None,
                )
            if not contact:
                continue
            new_name = contact.get("adv_name")
            if new_name and new_name != node["name"]:
                await self._db.rename_known_node(key, new_name)
                log.info("Spyglass name updated: %r -> %r", node["name"], new_name)
                updated += 1
        if updated:
            log.info("Reconciled %d spyglass name(s) from companion contacts", updated)
        return updated

    def begin_shutdown(self) -> None:
        """Signal live SSE streams to close without tearing down the engine yet.

        The `/api/events` generators loop `while not self._shutting_down` and
        also break on a broadcast `shutdown` event. Flipping the flag and
        pushing that event here lets uvicorn's graceful drain finish promptly
        instead of blocking on long-lived streams until Docker SIGKILLs us.
        The adapter/db teardown still happens later in `stop()`."""
        self._shutting_down = True
        self._publish_event("shutdown", {})

    async def stop(self) -> None:
        self._shutting_down = True
        self._publish_event("shutdown", {})
        await self._adapter.disconnect()
        await self._db.close()
        log.info("Game engine stopped")

    async def set_home(self, lat: float, lon: float) -> None:
        player = await self._db.get_or_create_player("pending", lat, lon)
        if player["key"] == "pending":
            await self._db.award_postcard(
                "pending", FIELD_TRAINING_CLASS, 1,
                "Staking Claim", 0, None,
            )
        self._home_lat = lat
        self._home_lon = lon
        self._home_hex = h3.latlng_to_cell(lat, lon, 8)

    async def _handle_message(self, msg: IncomingMessage) -> str | None:
        cmd = parse_command(msg.text)
        if cmd is None:
            return None

        now = time.time()
        # sender_timestamp is stable across flood retransmits, so including it
        # distinguishes a re-flooded copy (same timestamp → dropped) from a
        # genuine repeat of the same command (new timestamp → processed).
        dedup_key = (msg.sender_key, msg.text.strip().lower(), msg.timestamp)
        last_seen = self._recent_messages.get(dedup_key)
        if last_seen and now - last_seen < self._dedup_window:
            log.info("Dropping duplicate command from %s: %s", msg.sender_key, msg.text[:30])
            return None
        self._recent_messages[dedup_key] = now
        stale = [k for k, t in self._recent_messages.items() if now - t > self._dedup_window]
        for k in stale:
            del self._recent_messages[k]

        contacts = self._adapter.get_contacts()
        contact = contacts.get(msg.sender_key, {})
        node_name = contact.get("adv_name", msg.sender_key[:8])
        if node_name:
            await self._db.upsert_known_node(msg.sender_key, node_name)

        # A charter-naming reply is only valid while a charter is pending.
        if cmd.type == CommandType.CHARTER_NAME and msg.sender_key not in self._pending_charters:
            return (
                "UNKNOWN COMMAND\n"
                "Try: /lora survey, /lora charter, /lora upkeep"
            )

        if cmd.type in _BACKGROUND_COMMANDS:
            cmd_name = _COMMAND_DISPLAY_NAMES[cmd.type]
            if self._command_task and not self._command_task.done():
                self._publish_event("command_busy", {"command": cmd_name})
                # command_busy only reaches the web dashboard's SSE feed — the
                # player out in the field holding the spyglass would otherwise
                # get total silence and assume the send failed. A GPS exchange
                # can run up to ~90s, so a re-sent command landing in that
                # window needs its own radio-side reply.
                return (
                    "STILL WORKING\n"
                    "Processing your last command.\n"
                    "Try again in a moment."
                )
            self._publish_event("cmd_received", {"command": cmd_name})
            self._command_task = asyncio.create_task(
                self._process_command_async(msg, cmd)
            )
            return None

        return None

    async def _process_command_async(self, msg: IncomingMessage, cmd: ParsedCommand) -> None:
        """Run a GPS-dependent command (survey/charter/upkeep/charter-name) off
        the receive loop and send its reply over the radio. One runs at a time."""
        cmd_name = _COMMAND_DISPLAY_NAMES[cmd.type]
        try:
            if cmd.type == CommandType.CHARTER_NAME:
                # Charter-naming does its own GPS verification internally.
                reply = await self._handle_charter_name(msg, cmd)
            else:
                result = await self._request_position_for_node(msg.sender_key, cmd_name)
                if not result.ok:
                    await self._adapter.send_message(
                        msg.sender_key, _gps_fail_message(result.failure, cmd_name)
                    )
                    return
                msg.lat, msg.lon = result.position
                handler = {
                    CommandType.SURVEY: self._handle_survey,
                    CommandType.CHARTER: self._handle_charter,
                    CommandType.UPKEEP: self._handle_upkeep,
                }[cmd.type]
                reply = await handler(msg, cmd)
            if reply:
                await self._adapter.send_message(msg.sender_key, reply)
        except Exception:
            log.exception("Error processing %s from %s", cmd_name, msg.sender_key)
            await self._adapter.send_message(
                msg.sender_key, "SERVER ERROR\nTry again shortly."
            )

    def _survey_rate_limited(self, player: dict) -> bool:
        """True if the last successful survey was under the current cooldown ago —
        the uniform radio cooldown that only bites fast movers. The cooldown
        doubles while the adapter reports the mesh congested."""
        last = player.get("last_survey_at")
        if last is None:
            return False
        interval = SURVEY_MIN_INTERVAL_S
        adapter = getattr(self, "_adapter", None)
        if adapter is not None and adapter.congested():
            interval *= CONGESTION_INTERVAL_MULT
        return (time.time() - last) < interval

    async def _handle_survey(
        self, msg: IncomingMessage, cmd: ParsedCommand, enforce_rate: bool = True
    ) -> str:
        player = await self._db.get_or_create_player(
            msg.sender_key, self._home_lat, self._home_lon
        )
        # Radio-path rate cap. The web path checks this upstream (before spending
        # mesh telemetry) and passes enforce_rate=False so it isn't re-checked
        # here against the not-yet-updated timestamp.
        if enforce_rate and self._survey_rate_limited(player):
            self._publish_event("survey_rejected", {"reason": "rate"})
            return (
                "TRANSMITTER COOLING\n"
                "Let the set settle a moment\n"
                "before the next survey."
            )
        pk = player["key"]
        p_home_lat, p_home_lon = player["home_lat"], player["home_lon"]
        p_home_hex = h3.latlng_to_cell(p_home_lat, p_home_lon, 8)

        hex_id = self._get_hex_id(msg.lat, msg.lon)
        distance = self._distance_miles(msg.lat, msg.lon, p_home_lat, p_home_lon)

        same_sender = player.get("last_survey_sender") == msg.sender_key
        if same_sender and player["last_survey_at"] is not None and player["last_survey_lat"] is not None:
            elapsed_hours = (time.time() - player["last_survey_at"]) / 3600
            if elapsed_hours > 0:
                travel_dist = distance_between(
                    player["last_survey_lat"], player["last_survey_lon"],
                    msg.lat, msg.lon,
                )
                velocity = travel_dist / elapsed_hours
                if velocity > MAX_VELOCITY_MPH:
                    self._publish_event("survey_rejected", {"reason": "velocity"})
                    return (
                        "SURVEY REJECTED\n"
                        "Position change too fast.\n"
                        "The World's End Society suspects foul play."
                    )

        if await self._db.was_hex_surveyed_today(pk, hex_id):
            name = self._hex_display_name(hex_id, p_home_hex)
            self._publish_event("survey_rejected", {"reason": "cooldown", "hex_name": name})
            return (
                f"ALREADY SURVEYED\n"
                f"{name} logged today.\n"
                f"Push further into the unknown."
            )

        is_discovery = not await self._db.is_hex_discovered(pk, hex_id)
        # First successful survey of the UTC day (this one isn't recorded yet).
        is_first_today = (await self._db.count_surveys_today(pk)) == 0

        post = await self._db.get_post_in_hex(pk, hex_id)
        post_mult = post_multiplier(post["level"]) if post else 1.0

        dispatch = get_daily_dispatch()
        dispatch_id = dispatch["id"]

        momentum_tier = await self._db.update_momentum_tier(pk)
        xp_parts = self._xp_components(distance, player, post_mult, momentum_tier)
        xp = self._calc_xp(distance, player, post_mult, momentum_tier)
        base_xp = xp
        provisions = self._calc_provisions(distance, post_mult)
        base_prov = provisions

        if dispatch_id == "bonus_xp":
            xp = int(xp * 1.15)
        elif dispatch_id == "bonus_provisions":
            provisions = int(provisions * 1.20)
        elif dispatch_id == "momentum_boost":
            xp = int(xp / xp_parts["momentum_mult"] * (1.0 + momentum_tier * 0.05 * 2))
        elif dispatch_id == "extended_range":
            xp = int(xp / xp_parts["distance_mult"] * (1 + distance * DISTANCE_XP_FACTOR * 2))
        # xp/provisions after any daily-dispatch modifier, before the flat
        # discovery bonuses — captured so the first-survey breakdown reconciles.
        xp_after_event = xp
        prov_after_event = provisions

        if is_discovery:
            xp += DISCOVERY_XP_BONUS
            provisions += DISCOVERY_PROVISION_BONUS

        # Every survey mints the base mark; a discovery adds its bonus on top.
        marks = SURVEY_MARK_BASE
        if is_discovery:
            marks += DISCOVERY_SURVEY_MARKS
        if dispatch_id == "survey_mark_bonus" and is_discovery:
            marks += 1
        if is_first_today:
            marks += FIRST_SURVEY_MARK_BONUS

        # Full reward math for every survey, persisted so the Ledger can show an
        # exact, historically-accurate breakdown per entry (the multipliers below
        # can't be reconstructed later once camp/momentum change). Survey reward
        # only — passive outpost income is added later and shown as its own line.
        survey_breakdown = {
            "xp": {
                "base": xp_parts["base"],
                "distance_mult": round(xp_parts["distance_mult"], 2),
                "camp_mult": round(xp_parts["camp_mult"], 2),
                "post_mult": round(xp_parts["post_mult"], 2),
                "momentum_mult": round(xp_parts["momentum_mult"], 2),
                "subtotal": base_xp,
                "event_bonus": xp_after_event - base_xp,
                "discovery_bonus": DISCOVERY_XP_BONUS if is_discovery else 0,
                "total": xp,
            },
            "provisions": {
                "base": BASE_PROVISIONS,
                "distance_bonus": math.floor(distance * DISTANCE_PROVISION_FACTOR),
                "post_mult": round(post_mult, 2),
                "subtotal": base_prov,
                "event_bonus": prov_after_event - base_prov,
                "discovery_bonus": DISCOVERY_PROVISION_BONUS if is_discovery else 0,
                "total": provisions,
            },
            "marks": {
                "base": SURVEY_MARK_BASE,
                "discovery": DISCOVERY_SURVEY_MARKS if is_discovery else 0,
                "event_bonus": 1 if (dispatch_id == "survey_mark_bonus" and is_discovery) else 0,
                "first_survey": FIRST_SURVEY_MARK_BONUS if is_first_today else 0,
                "total": marks,
            },
        }

        async with self._db.transaction():
            if is_discovery:
                await self._db.discover_hex(pk, hex_id)

            await self._db.record_survey(
                player_key=pk,
                hex_id=hex_id,
                lat=msg.lat,
                lon=msg.lon,
                distance_miles=distance,
                snr=msg.snr,
                rssi=msg.rssi,
                hops=msg.hops,
                xp_earned=xp,
                provisions_earned=provisions,
                field_notes_earned=0,
                is_discovery=is_discovery,
                marks_earned=marks,
                reward_breakdown=json.dumps(survey_breakdown),
            )

            totals = await self._db.apply_survey_rewards(
                pk, xp, provisions, marks,
                lat=msg.lat, lon=msg.lon, sender_key=msg.sender_key,
            )
            total_xp = totals["xp"]
            total_provisions = totals["provisions"]

        # Note: surveying a post's hex earns the active-survey bonus but does NOT
        # reset its ruin timer — upkeep is a deliberate act (/lora upkeep on site).
        passive = await self.collect_passive_provisions(pk)

        postcards = await self._check_postcards(pk, distance, is_discovery)

        relic = await self._roll_relic(pk, hex_id, player["base_camp_level"])

        name = self._hex_display_name(hex_id, p_home_hex)

        lines = [f"SURVEY LOGGED: {name}"]
        dist_line = f"{distance:.1f}mi"
        if msg.snr is not None:
            dist_line += f" | {msg.snr:.1f}dB"
        lines.append(dist_line)
        if is_discovery:
            hexes = await self._db.count_discovered_hexes(pk)
            lines.append(f"NEW TERRITORY! #{hexes} discovered")
        if passive["total"] > 0:
            provisions += passive["total"]
            total_provisions += passive["total"]
            lines.append(f"+{passive['total']} provisions from outposts")
        mark_plural = "" if marks == 1 else "s"
        lines.append(f"+{xp}xp +{provisions}prov +{marks} mark{mark_plural}")
        if momentum_tier > 0:
            lines.append(f"Momentum +{momentum_tier * 5}% XP")
        lines.append(f"Total: {total_xp}xp | {total_provisions}prov")
        if is_first_today and FIRST_SURVEY_MARK_BONUS:
            plural = "" if FIRST_SURVEY_MARK_BONUS == 1 else "s"
            lines.append(f"FIRST SURVEY OF THE DAY! (+{FIRST_SURVEY_MARK_BONUS} bonus mark{plural})")

        if relic:
            relic_names = {
                "buried_cache": "BURIED CACHE found! Open it from your dashboard.",
                "vigor_tonic": "VIGOR TONIC found! Use it to clear all survey cooldowns.",
                "wardstone": "WARDSTONE found! Use it to protect a Survey Post.",
            }
            lines.append(relic_names.get(relic["type"], f"RELIC: {relic['type']}"))

        for card in postcards:
            stars = "★" * card["stars"]
            lines.append(f"POSTCARD: {card['class']} [{stars}]")

        promotions = await self._auto_promote(pk, player["rank_level"], total_xp)
        for promo in promotions:
            total_provisions += promo["reward_prov"]
            lines.append(
                f"RANK UP: {promo['name']} (Rank {promo['level']}) "
                f"+{promo['reward_prov']}prov"
            )

        contract_completions = await self.update_contract_progress(pk)
        for cc in contract_completions:
            label = CONTRACT_OBJECTIVES[cc['objective']]['label']
            lines.append(f"CONTRACT COMPLETE: {label} — {cc['reward_desc']}")
            self._publish_event("contract_complete", {
                "label": label, "reward": cc["reward_desc"],
            })

        detail = f"+{xp}xp +{provisions}prov"
        if is_discovery:
            await self._db.log_activity(pk, "survey", f"Marked new territory at {name}", detail)
        else:
            await self._db.log_activity(pk, "survey", f"Returned to {name} for readings", detail)

        log.info(
            "Survey: %s hex=%s dist=%.1f xp=%d prov=%d disc=%s post=%s",
            msg.sender_key, hex_id[:8], distance, xp, provisions, is_discovery,
            post["name"] if post else "none",
        )

        survey_event = {
            "hex_name": name, "distance": round(distance, 1),
            "xp": xp, "provisions": provisions, "marks": marks,
            "discovery": is_discovery,
            "relic": relic["type"] if relic else None,
            "promotions": [p["name"] for p in promotions],
            "first_today": is_first_today,
            "first_survey_bonus": FIRST_SURVEY_MARK_BONUS if is_first_today else 0,
        }
        surveys_today = await self._db.count_surveys_today(pk)
        if surveys_today >= DAILY_SURVEY_WARNING_THRESHOLD:
            remaining = max(0, DAILY_SURVEY_CAP - surveys_today)
            survey_event["surveys_remaining"] = remaining

        self._publish_event("survey", survey_event)

        return "\n".join(lines)

    def _charter_costs(self) -> tuple[int, int]:
        dispatch = get_daily_dispatch()
        prov = CHARTER_PROVISION_COST // 2 if dispatch["id"] == "charter_discount" else CHARTER_PROVISION_COST
        marks = max(1, CHARTER_MARK_COST // 2) if dispatch["id"] == "charter_discount" else CHARTER_MARK_COST
        return prov, marks

    async def _handle_charter(self, msg: IncomingMessage, cmd: ParsedCommand) -> str:
        player = await self._db.get_or_create_player(
            msg.sender_key, self._home_lat, self._home_lon
        )
        pk = player["key"]
        p_home_lat, p_home_lon = player["home_lat"], player["home_lon"]
        p_home_hex = h3.latlng_to_cell(p_home_lat, p_home_lon, 8)

        hex_id = self._get_hex_id(msg.lat, msg.lon)
        distance = self._distance_miles(msg.lat, msg.lon, p_home_lat, p_home_lon)

        charter_prov, charter_marks = self._charter_costs()

        if player["rank_level"] < CHARTER_MIN_LEVEL:
            return (
                f"RANK TOO LOW\n"
                f"Reach rank {CHARTER_MIN_LEVEL} to earn your Charter License.\n"
                f"Keep surveying, explorer."
            )

        allowed = max_posts_for_camp(player["base_camp_level"])
        if allowed == 0:
            return (
                f"CAMP TOO LOW\n"
                f"Upgrade base camp to {camp_name(CHARTER_MIN_CAMP)} to charter.\n"
                f"Keep surveying, explorer."
            )

        current = await self._db.count_player_posts(pk)
        if current >= allowed:
            return (
                f"ALL CHARTERS CLAIMED\n"
                f"Max outposts for camp Lv {player['base_camp_level']}: {current}/{allowed}\n"
                f"Upgrade base camp for more."
            )

        if distance < CHARTER_MIN_DISTANCE_MILES:
            return (
                f"TOO CLOSE TO CAMP\n"
                f"{distance:.1f}mi — minimum {CHARTER_MIN_DISTANCE_MILES:.0f}mi.\n"
                f"Stake your claim in distant lands."
            )

        if player["provisions"] < charter_prov:
            return (
                f"INSUFFICIENT PROVISIONS\n"
                f"Need {charter_prov}, have {player['provisions']}.\n"
                f"Keep surveying."
            )

        if player["survey_marks"] < charter_marks:
            return (
                f"INSUFFICIENT SURVEY MARKS\n"
                f"Need {charter_marks}, have {player['survey_marks']}.\n"
                f"Survey more territories."
            )

        if not await self._db.is_hex_discovered(pk, hex_id):
            return (
                f"UNCHARTED TERRITORY\n"
                f"Survey this territory first to discover it.\n"
                f"Then return to charter."
            )

        existing = await self._db.get_any_post_in_hex(hex_id)
        if existing:
            return (
                f"TERRITORY OCCUPIED\n"
                f'Outpost "{existing["name"]}" already here.\n'
                f"Find unclaimed land."
            )

        self._pending_charters[msg.sender_key] = {
            "hex_id": hex_id,
            "timestamp": time.time(),
            "lat": msg.lat,
            "lon": msg.lon,
        }

        log.info("Charter request from %s at hex %s", msg.sender_key, hex_id[:8])
        name = self._hex_display_name(hex_id, p_home_hex)
        return (
            f"CHARTER READY — {name}\n"
            f"{distance:.1f}mi | Cost: {charter_prov}prov {charter_marks}marks\n"
            f"Name your outpost: /lora [name]\n"
            f"You have 5 min to stake your claim."
        )

    async def _grant_charter_checkpoint_marks(self, pk: str) -> int:
        """Grant bonus marks for each of the player's first CHARTER_CHECKPOINT_COUNT
        charters. Returns the amount granted (0 once the checkpoints are spent)."""
        granted = int(await self._db.get_setting("charter_marks_granted") or 0)
        if granted >= CHARTER_CHECKPOINT_COUNT:
            return 0
        await self._db.add_survey_marks(pk, CHARTER_CHECKPOINT_MARKS)
        await self._db.set_setting("charter_marks_granted", str(granted + 1))
        return CHARTER_CHECKPOINT_MARKS

    async def _handle_charter_name(
        self, msg: IncomingMessage, cmd: ParsedCommand
    ) -> str | None:
        pending = self._pending_charters.get(msg.sender_key)
        if not pending:
            return None

        elapsed = time.time() - pending["timestamp"]
        if elapsed > CHARTER_TIMEOUT:
            del self._pending_charters[msg.sender_key]
            return "CHARTER EXPIRED\nClaim window closed.\nNo resources spent."

        result = await self._adapter.request_position(msg.sender_key)
        if not result.ok:
            del self._pending_charters[msg.sender_key]
            now = datetime.now().strftime("%H:%M:%S")
            return f"NO GPS FIX [{now}]\nCannot verify position.\nCharter cancelled."

        current_hex = self._get_hex_id(result.position[0], result.position[1])
        if current_hex != pending["hex_id"]:
            del self._pending_charters[msg.sender_key]
            return "CHARTER EXPIRED\nHex mismatch.\nNo resources spent."

        name = cmd.args.strip()[:30]
        del self._pending_charters[msg.sender_key]

        charter_prov, charter_marks = self._charter_costs()

        player = await self._db.get_or_create_player(
            msg.sender_key, self._home_lat, self._home_lon
        )
        pk = player["key"]
        if player["provisions"] < charter_prov:
            return "CHARTER FAILED\nInsufficient provisions.\nNo resources spent."
        if player["survey_marks"] < charter_marks:
            return "CHARTER FAILED\nInsufficient survey marks.\nNo resources spent."

        p_home_hex = h3.latlng_to_cell(player["home_lat"], player["home_lon"], 8)

        async with self._db.transaction():
            await self._db.deduct_provisions(pk, charter_prov)
            await self._db.deduct_survey_marks(pk, charter_marks)
            post = await self._db.create_post(pk, current_hex, name)

        region = self._hex_display_name(current_hex, p_home_hex)
        await self._db.log_activity(pk, "charter", f'Established Survey Post "{name}"', region)

        bonus_marks = await self._grant_charter_checkpoint_marks(pk)

        log.info("Outpost chartered: '%s' at hex %s", name, current_hex[:8])
        self._publish_event("charter", {"post_name": name, "territory": region})
        lines = [
            f'OUTPOST CHARTERED: "{name}"',
            f"{region} | Lv 1",
        ]
        if bonus_marks:
            lines.append(f"Charter License bonus: +{bonus_marks} survey marks")
        lines.append("The World's End Society's reach grows.")
        return "\n".join(lines)

    async def _handle_upkeep(self, msg: IncomingMessage, cmd: ParsedCommand) -> str:
        hex_id = self._get_hex_id(msg.lat, msg.lon)

        player = await self._db.get_or_create_player(
            msg.sender_key, self._home_lat, self._home_lon
        )
        pk = player["key"]
        p_home_hex = h3.latlng_to_cell(player["home_lat"], player["home_lon"], 8)

        post = await self._db.get_post_in_hex(pk, hex_id)
        if not post:
            name = self._hex_display_name(hex_id, p_home_hex)
            self._publish_event("cmd_failed", {"command": "upkeep", "reason": f"No outpost at {name}"})
            return (
                f"NO OUTPOST HERE\n"
                f"No outpost at {name}.\n"
                f"Charter one with /lora charter"
            )

        grace = int(upkeep_grace_days(player))
        dispatch = get_daily_dispatch()
        if dispatch["id"] == "upkeep_grace":
            await self._db.tend_post(post["id"], bonus_days=RUIN_RAMP_DAYS)
            bonus = RUIN_RAMP_DAYS
            timer_msg = f"Full income restored (+{RUIN_RAMP_DAYS}d maintenance bonus)"
        else:
            await self._db.tend_post(post["id"])
            bonus = 0
            timer_msg = f"Full income restored — holds {grace} days"

        # Days the freshly-tended post now has before it falls into ruin, so the
        # Radio feed can report the reset timer instead of a bare "all clear".
        days_until_ruined = grace + RUIN_RAMP_DAYS + bonus

        await self._db.log_activity(pk, "upkeep", f'Upkeep on "{post["name"]}" — all clear')

        log.info("Upkeep: %s tended '%s'", msg.sender_key, post["name"])
        self._publish_event("upkeep", {
            "post_name": post["name"],
            "level": post["level"],
            "days_until_ruined": days_until_ruined,
        })
        return (
            f'UPKEEP DONE: "{post["name"]}"\n'
            f"{timer_msg}\n"
            f"Lv {post['level']} | All clear."
        )

    def _xp_components(
        self, distance: float, player: dict, post_mult: float,
        momentum_tier: int = 0,
    ) -> dict:
        return {
            "base": BASE_XP,
            "distance_mult": 1 + (distance * DISTANCE_XP_FACTOR),
            "camp_mult": self._camp_multiplier(player["base_camp_level"]),
            "post_mult": post_mult,
            "momentum_mult": 1.0 + (momentum_tier * 0.05),
        }

    def _calc_xp(
        self, distance: float, player: dict, post_mult: float,
        momentum_tier: int = 0,
    ) -> int:
        c = self._xp_components(distance, player, post_mult, momentum_tier)
        return int(c["base"] * c["distance_mult"] * c["camp_mult"]
                   * c["post_mult"] * c["momentum_mult"])

    def _calc_provisions(self, distance: float, post_mult: float) -> int:
        return int((BASE_PROVISIONS + math.floor(distance * DISTANCE_PROVISION_FACTOR)) * post_mult)

    def _camp_multiplier(self, level: int) -> float:
        return BASE_CAMP_TABLE.get(level, BASE_CAMP_TABLE[1])["mult"]

    def _hex_display_name(self, hex_id: str, home_hex: str | None = None) -> str:
        if hex_id == (home_hex or self._home_hex):
            return "Base Camp"
        return hex_name(hex_id)

    def _get_hex_id(self, lat: float, lon: float) -> str:
        return h3.latlng_to_cell(lat, lon, 8)

    def _distance_miles(
        self, lat: float, lon: float,
        home_lat: float | None = None, home_lon: float | None = None,
    ) -> float:
        return distance_between(
            home_lat if home_lat is not None else self._home_lat,
            home_lon if home_lon is not None else self._home_lon,
            lat, lon,
        )

    def _distance_from_hex(
        self, hex_id: str,
        home_lat: float | None = None, home_lon: float | None = None,
    ) -> float:
        lat, lon = h3.cell_to_latlng(hex_id)
        return self._distance_miles(lat, lon, home_lat, home_lon)

    async def _auto_promote(
        self, player_key: str, current_rank: int, total_xp: float,
    ) -> list[dict]:
        promotions = []
        rank = current_rank
        while rank < 50:
            next_lvl = rank + 1
            req = RANK_THRESHOLDS[next_lvl]
            if total_xp < req["xp"]:
                break
            async with self._db.transaction():
                await self._db.rank_up_with_reward(
                    player_key, next_lvl, req["reward_prov"],
                )
            promotions.append({
                "level": next_lvl, "name": req["name"],
                "reward_prov": req["reward_prov"],
            })
            self._publish_event("rank_up", {"level": next_lvl, "name": req["name"]})
            rank = next_lvl
        return promotions

    async def upgrade_base_camp(self, player_key: str) -> dict:
        player = await self._db.get_player(player_key)
        if not player:
            return {"success": False, "reason": "Player not found"}
        current = player["base_camp_level"]
        next_level = current + 1
        if next_level not in BASE_CAMP_TABLE:
            return {"success": False, "reason": "Already max level"}
        req = BASE_CAMP_TABLE[next_level]
        if player["provisions"] < req["prov"]:
            return {"success": False, "reason": f"Need {req['prov']} provisions"}
        async with self._db.transaction():
            await self._db.upgrade_base_camp(
                player_key, next_level, req["prov"],
            )
        await self._db.log_activity(player_key, "upgrade", f"Expanded base camp to {req['name']}", f"Lv {next_level}")
        return {"success": True, "new_level": next_level, "mult": req["mult"], "name": req["name"]}

    async def upgrade_post(self, player_key: str, post_id: int) -> dict:
        post = await self._db.get_post_by_id(post_id)
        if not post or post["player_key"] != player_key:
            return {"success": False, "reason": "Post not found or not yours"}
        next_level = post["level"] + 1
        if next_level > MAX_POST_LEVEL:
            return {"success": False, "reason": "Already max level"}
        cost = POST_UPGRADE_COST[next_level]
        player = await self._db.get_player(player_key)
        if player["provisions"] < cost:
            return {"success": False, "reason": f"Need {cost} provisions"}
        async with self._db.transaction():
            await self._db.upgrade_post(post_id, next_level, cost, player_key)
        await self._db.log_activity(player_key, "upgrade", f'Reinforced "{post["name"]}" to Lv {next_level}', f"-{cost} provisions")
        return {"success": True, "new_level": next_level, "cost": cost}

    async def collect_passive_provisions(self, player_key: str) -> dict:
        posts = await self._db.get_all_posts(player_key)
        if not posts:
            return {"total": 0, "posts": []}
        player = await self._db.get_player(player_key)
        p_home_lat, p_home_lon = player["home_lat"], player["home_lon"]
        now = int(time.time())
        grace = upkeep_grace_days(player)
        total = 0
        breakdown = []
        post_ids = []
        for post in posts:
            coll = post["last_collected_at"]
            tended = post["last_tended_at"]
            # Warded (dormant) posts earn nothing for the ward window — subtract
            # any overlap between [last_collected_at, now] and the ward period.
            dormant_secs = _ward_overlap(post, coll, now)
            earn_secs = max(0, (now - coll) - dormant_secs)
            earn_days = earn_secs / 86400
            # Ruin decay: income fades with age since last upkeep (ward time, which
            # freezes ruin, is excluded from both the earning window and the age).
            age_start_secs = max(0, (coll - tended) - _ward_overlap(post, tended, coll))
            age_start = age_start_secs / 86400
            eff_days = ruin_effective_days(age_start, age_start + earn_days, grace)
            dist = self._distance_from_hex(post["hex_id"], p_home_lat, p_home_lon)
            base_amount = eff_days * post["level"] * (1 + math.floor(dist / 5))
            amount = int(base_amount)
            if amount > 0:
                total += amount
                post_ids.append(post["id"])
                breakdown.append({"name": post["name"], "provisions": amount})
        if total > 0:
            async with self._db.transaction():
                await self._db.collect_passive_provisions(player_key, total, post_ids)
        return {"total": total, "posts": breakdown}

    async def _check_postcards(
        self, player_key: str, distance: float,
        is_discovery: bool,
    ) -> list[dict]:
        awarded = []

        async def _award_milestone(pc_class, milestones, value, desc_fn):
            existing = await self._db.get_postcards_by_class(player_key, pc_class)
            for i, milestone in enumerate(milestones):
                if value >= milestone:
                    stars = i + 1
                    desc = desc_fn(milestone)
                    if not any(c["description"] == desc for c in existing):
                        card = await self._db.award_postcard(
                            player_key, pc_class, stars, desc, distance, None,
                        )
                        awarded.append(card)

        if is_discovery:
            hex_count = await self._db.count_discovered_hexes(player_key)
            await _award_milestone("Trailblazer", TRAILBLAZER_MILESTONES, hex_count,
                                   lambda m: f"{m} territories discovered")

        streak = await self._db.get_survey_streak(player_key)
        await _award_milestone("Relentless", RELENTLESS_MILESTONES, streak,
                               lambda m: f"{m}-day streak")

        await _award_milestone("Strider", STRIDER_MILESTONES, distance,
                               lambda m: f"Survey at {m}+ miles")

        posts = await self._db.get_all_posts(player_key)
        if posts:
            now = int(time.time())
            max_tenure = max((now - p["created_at"]) // 86400 for p in posts)
            await _award_milestone("Steadfast", STEADFAST_MILESTONES, max_tenure,
                                   lambda m: f"Post held {m} days")

        total_surveys = await self._db.get_survey_count(player_key)
        total_area = total_surveys * HEX_AREA_SQ_MI
        await _award_milestone("Boundless", BOUNDLESS_MILESTONES, total_area,
                               lambda m: f"{m} sq mi surveyed")

        ft = await self._db.get_postcards_by_class(player_key, FIELD_TRAINING_CLASS)
        ft_descs = {c["description"] for c in ft}

        if "First Contact" not in ft_descs:
            card = await self._db.award_postcard(
                player_key, FIELD_TRAINING_CLASS, 1,
                "First Contact", distance, None,
            )
            awarded.append(card)

        if "Long Range" not in ft_descs and distance >= 1.0:
            card = await self._db.award_postcard(
                player_key, FIELD_TRAINING_CLASS, 1,
                "Long Range", distance, None,
            )
            awarded.append(card)

        if "Cartographer" not in ft_descs and is_discovery:
            hex_count = await self._db.count_discovered_hexes(player_key)
            if hex_count >= 5:
                card = await self._db.award_postcard(
                    player_key, FIELD_TRAINING_CLASS, 1,
                    "Cartographer", distance, None,
                )
                awarded.append(card)

        return awarded

    async def _roll_relic(
        self, player_key: str, hex_id: str, base_camp_level: int = 1,
    ) -> dict | None:
        dispatch = get_daily_dispatch()
        boost = 2.0 if dispatch["id"] == "relic_boost" else 1.0
        # Headquarters (Base Camp 10) grants a standing +5% relic-drop boost.
        if base_camp_level >= RELIC_BOOST_CAMP:
            boost *= CAMP_RELIC_BOOST

        # Diminishing returns are per relic type: finding a Buried Cache only
        # suppresses further Buried Caches, not Wardstones or Vigor Tonics.
        recent_by_type = await self._db.count_recent_relics_by_type(
            player_key, int(time.time()) - RELIC_ROLLING_WINDOW,
        )

        roll = random.random()
        cumulative = 0.0
        for relic_type, chance in RELIC_DROP_TABLE:
            diminish = RELIC_DIMINISH_FACTOR ** recent_by_type.get(relic_type, 0)
            cumulative += chance * boost * diminish
            if roll < cumulative:
                relic = await self._db.add_relic(player_key, relic_type, hex_id)
                await self._check_relic_field_training(player_key)
                return relic
        return None

    async def _check_relic_field_training(self, player_key: str) -> None:
        total = await self._db.count_relics(player_key)
        if total == 1:
            ft = await self._db.get_postcards_by_class(player_key, FIELD_TRAINING_CLASS)
            if not any(c["description"] == "Relic Hunter" for c in ft):
                await self._db.award_postcard(
                    player_key, FIELD_TRAINING_CLASS, 1,
                    "Relic Hunter", None, None,
                )

    async def claim_strongbox(self, player_key: str) -> dict:
        ft = await self._db.get_postcards_by_class(player_key, FIELD_TRAINING_CLASS)
        ft_earned = {c["description"] for c in ft} & set(FIELD_TRAINING_POSTCARDS)
        if len(ft_earned) < len(FIELD_TRAINING_POSTCARDS):
            return {"success": False, "reason": "Field Training not complete"}

        already = await self._db.get_setting("strongbox_claimed")
        if already:
            return {"success": False, "reason": "Already claimed"}

        async with self._db.transaction():
            await self._db.add_provisions(player_key, STRONGBOX_PROVISIONS)
            await self._db.add_relic(player_key, "vigor_tonic", "strongbox")
            await self._db.set_setting("strongbox_claimed", "1")

        await self._db.log_activity(
            player_key, "relic",
            "Opened the Society Strongbox",
            f"+{STRONGBOX_PROVISIONS} provisions, vigor tonic",
        )

        return {"success": True, "provisions": STRONGBOX_PROVISIONS, "relic": "Vigor Tonic"}

    async def _request_position_for_node(self, node_key: str, command: str) -> PositionResult:
        self._publish_event("gps_request", {"command": command})

        travel_mode = self._adapter.get_travel_mode()
        # The adapter owns the real per-attempt timeout and route choice; it hands
        # them to us so the Radio signal bar drains against the true deadline
        # (single source of truth — no independent recompute that could drift).
        a1_route = {"value": "flood"}

        async def _on_progress(stage: str, route: str, timeout_secs: float):
            deadline = time.time() + timeout_secs
            try:
                attempt = int(stage.rsplit("_", 1)[1])
            except (ValueError, IndexError):
                attempt = 1
            if attempt == 1:
                a1_route["value"] = route
            # "fallback" marks the flood escalation that follows one or more
            # cached-route tries — the point where the known route was abandoned.
            fallback = route == "flood" and a1_route["value"] == "last_path"
            self._publish_event("gps_triangulating", {
                "command": command, "attempt": attempt, "mode": route,
                "travel_mode": travel_mode, "fallback": fallback,
                "deadline_ts": deadline,
            })

        result = await self._adapter.request_position(node_key, progress_callback=_on_progress)
        if result.ok:
            self._publish_event("gps_fix", {
                "command": command,
                "lat": round(result.position[0], 4),
                "lon": round(result.position[1], 4),
            })
        else:
            self._publish_event("gps_fail", {
                "command": command,
                "reason": result.failure.value if result.failure else "unknown",
            })
            if a1_route["value"] == "last_path":
                self._publish_event("route_lost", {})
        return result

    async def _request_position_for_player(self, player: dict, command: str) -> PositionResult:
        node_key = player.get("last_survey_sender")
        if not node_key:
            return PositionResult(failure=PositionFailure.ERROR)
        return await self._request_position_for_node(node_key, command)

    async def _run_web_command(self, work) -> dict:
        """Route a dashboard-initiated GPS command through the same single-slot
        guard as radio commands. There is one physical companion, so only one
        GPS command may run at a time regardless of where it originated."""
        if self._command_task and not self._command_task.done():
            return {"ok": False, "error": "Another command is in progress — try again in a moment"}
        self._command_task = asyncio.create_task(work())
        return await self._command_task

    async def web_survey(self, player_key: str, auto: bool = False) -> dict:
        return await self._run_web_command(lambda: self._do_web_survey(player_key, auto=auto))

    async def _do_web_survey(self, player_key: str, auto: bool = False) -> dict:
        player = await self._db.get_player(player_key)
        if not player:
            return {"ok": False, "error": "Player not found"}

        # Enforce the uniform survey cap BEFORE spending any mesh telemetry. A
        # hands-free auto-survey that's too soon is a silent no-op (the client
        # swallows it — no feed spam); a manual tap gets a gentle "hold" message.
        if self._survey_rate_limited(player):
            if auto:
                return {"ok": False, "reason": "rate"}
            self._publish_event("survey_rejected", {"reason": "rate"})
            return {
                "ok": False,
                "error": "Transmitter cooling — hold a moment before the next survey.",
            }

        self._publish_event("cmd_received", {"command": "survey"})

        result = await self._request_position_for_player(player, "survey")
        if not result.ok:
            return {"ok": False, "error": _gps_fail_message(result.failure)}

        msg = IncomingMessage(
            sender_key=player.get("last_survey_sender", player_key),
            text="/lora survey",
            lat=result.position[0],
            lon=result.position[1],
        )
        cmd = ParsedCommand(type=CommandType.SURVEY, args="")
        response = await self._handle_survey(msg, cmd, enforce_rate=False)
        if auto:
            self._publish_event("autosurvey_logged", {})
        return {"ok": True, "message": response, "auto": auto}

    async def web_charter(self, player_key: str) -> dict:
        return await self._run_web_command(lambda: self._do_web_charter(player_key))

    async def _do_web_charter(self, player_key: str) -> dict:
        player = await self._db.get_player(player_key)
        if not player:
            return {"ok": False, "error": "Player not found"}

        self._publish_event("cmd_received", {"command": "charter"})

        result = await self._request_position_for_player(player, "charter")
        if not result.ok:
            return {"ok": False, "error": _gps_fail_message(result.failure)}

        lat, lon = result.position
        pk = player["key"]
        p_home_lat, p_home_lon = player["home_lat"], player["home_lon"]
        p_home_hex = h3.latlng_to_cell(p_home_lat, p_home_lon, 8)
        hex_id = self._get_hex_id(lat, lon)
        distance = self._distance_miles(lat, lon, p_home_lat, p_home_lon)

        charter_prov, charter_marks = self._charter_costs()

        def _charter_fail(reason: str) -> dict:
            self._publish_event("cmd_failed", {"command": "charter", "reason": reason})
            return {"ok": False, "error": reason}

        if player["rank_level"] < CHARTER_MIN_LEVEL:
            return _charter_fail(f"Reach rank {CHARTER_MIN_LEVEL} to earn your Charter License")

        allowed = max_posts_for_camp(player["base_camp_level"])
        if allowed == 0:
            return _charter_fail(f"Upgrade base camp to {camp_name(CHARTER_MIN_CAMP)} to charter")

        current = await self._db.count_player_posts(pk)
        if current >= allowed:
            return _charter_fail(f"All charter slots used ({current}/{allowed})")

        if distance < CHARTER_MIN_DISTANCE_MILES:
            return _charter_fail(f"Too close to camp ({distance:.1f}mi — minimum {CHARTER_MIN_DISTANCE_MILES:.0f}mi)")

        if player["provisions"] < charter_prov:
            return _charter_fail(f"Need {charter_prov} provisions, have {player['provisions']}")

        if player["survey_marks"] < charter_marks:
            return _charter_fail(f"Need {charter_marks} marks, have {player['survey_marks']}")

        if not await self._db.is_hex_discovered(pk, hex_id):
            return _charter_fail("Uncharted territory — survey it first")

        existing = await self._db.get_any_post_in_hex(hex_id)
        if existing:
            return _charter_fail(f'Territory occupied by "{existing["name"]}"')

        post_count = current + 1
        auto_name = f"Outpost #{post_count}"

        async with self._db.transaction():
            await self._db.deduct_provisions(pk, charter_prov)
            await self._db.deduct_survey_marks(pk, charter_marks)
            post = await self._db.create_post(pk, hex_id, auto_name)

        region = self._hex_display_name(hex_id, p_home_hex)
        await self._db.log_activity(pk, "charter", f'Established Survey Post "{auto_name}"', region)

        bonus_marks = await self._grant_charter_checkpoint_marks(pk)

        log.info("Web charter: '%s' at hex %s", auto_name, hex_id[:8])
        self._publish_event("charter", {"post_name": auto_name, "territory": region})
        message = f'Outpost chartered: "{auto_name}" in {region}'
        if bonus_marks:
            message += f' — +{bonus_marks} 🪙 Charter License bonus'
        return {
            "ok": True,
            "message": message,
            "post_id": post["id"],
            "bonus_marks": bonus_marks,
        }

    async def web_upkeep(self, player_key: str) -> dict:
        return await self._run_web_command(lambda: self._do_web_upkeep(player_key))

    async def _do_web_upkeep(self, player_key: str) -> dict:
        player = await self._db.get_player(player_key)
        if not player:
            return {"ok": False, "error": "Player not found"}

        self._publish_event("cmd_received", {"command": "upkeep"})

        result = await self._request_position_for_player(player, "upkeep")
        if not result.ok:
            return {"ok": False, "error": _gps_fail_message(result.failure)}

        lat, lon = result.position
        msg = IncomingMessage(
            sender_key=player.get("last_survey_sender", player_key),
            text="/lora upkeep",
            lat=lat,
            lon=lon,
        )
        cmd = ParsedCommand(type=CommandType.UPKEEP, args="")
        response = await self._handle_upkeep(msg, cmd)
        return {"ok": True, "message": response}

    async def rename_post(self, player_key: str, post_id: int, name: str) -> dict:
        post = await self._db.get_post_by_id(post_id)
        if not post or post["player_key"] != player_key:
            return {"ok": False, "error": "Post not found"}
        name = name.strip()[:30]
        if not name:
            return {"ok": False, "error": "Name cannot be empty"}
        old_name = post["name"]
        await self._db.rename_post(post_id, name)
        await self._db.log_activity(player_key, "rename", f'Renamed "{old_name}" → "{name}"')
        return {"ok": True, "name": name}

    # --- Contracts ---

    async def ensure_weekly_contracts(
        self, player_key: str, pvp_enabled: bool = False,
    ) -> list[dict]:
        ws = _contract_period_start_utc()
        existing = await self._db.get_current_contracts(player_key, ws)
        if existing:
            return existing
        num_contracts = BASE_CONTRACTS_PER_PERIOD
        pool = list(CONTRACT_OBJECTIVES.keys())
        chosen = random.sample(pool, min(num_contracts, len(pool)))
        contracts = []
        for obj_key in chosen:
            targets = CONTRACT_OBJECTIVE_TARGETS[obj_key]
            tier = random.choice(CONTRACT_TIERS)
            cost = random.randint(*tier["cost"])
            reward_type = tier["reward_type"]
            reward_amount = random.randint(*tier["reward"])
            # The top (relic) tier can pay a tier-IV munition instead — but only
            # for PvP-enabled players, since the item is minted on the Worker.
            if (
                reward_type == "relic"
                and pvp_enabled
                and random.random() < CONTRACT_PREMIUM_ITEM_CHANCE
            ):
                reward_type = random.choice(CONTRACT_ITEM_REWARD_TYPES)
                reward_amount = 1
            target = random.choice(targets)
            c = await self._db.create_contract(
                player_key, obj_key, target, cost,
                reward_type, reward_amount, ws,
            )
            contracts.append(c)
        return contracts

    async def purchase_contract(self, player_key: str, contract_id: int) -> dict:
        ok = await self._db.purchase_contract(contract_id, player_key)
        if not ok:
            return {"ok": False, "error": "Cannot purchase contract"}
        await self._db.log_activity(player_key, "contract", "Accepted an Expedition Contract")
        return {"ok": True}

    async def update_contract_progress(self, player_key: str) -> list[dict]:
        ws = _contract_period_start_utc()
        contracts = await self._db.get_current_contracts(player_key, ws)
        completed = []
        for c in contracts:
            if not c["purchased"] or c["completed"]:
                continue
            progress = await self._calc_contract_progress(player_key, c, ws)
            await self._db.update_contract_progress(c["id"], progress)
            if progress >= c["target"]:
                result = await self._db.complete_contract(c["id"], player_key)
                if result:
                    if result["reward_type"] in CONTRACT_ITEM_REWARD_TYPES:
                        # Tier-IV munition. The item lives on the Worker's
                        # authoritative store, so it isn't granted here — the
                        # multiplayer manager mints it on the next survey bundle
                        # (the contract stays reward_granted=0 until it lands).
                        # Say so in the notice: it arrives with the supply drop,
                        # not the instant the contract clears.
                        item_name = multiplayer_item_name(result["reward_type"])["full"]
                        result["reward_desc"] = f"{item_name} (inbound next supply drop)"
                    elif result["reward_type"] == "relic":
                        relic_type = random.choice(["vigor_tonic", "wardstone"])
                        await self._db.add_relic(player_key, relic_type, "")
                        result["reward_desc"] = f"1 {relic_type.replace('_', ' ').title()}"
                    elif result["reward_type"] == "provisions":
                        result["reward_desc"] = f"{result['reward_amount']} provisions"
                    else:
                        result["reward_desc"] = f"{result['reward_amount']} survey marks"
                    completed.append(result)
                    await self._db.log_activity(
                        player_key, "contract",
                        f"Completed contract: {CONTRACT_OBJECTIVES[c['objective']]['label']}",
                        result["reward_desc"],
                    )
        return completed

    async def _calc_contract_progress(self, pk: str, contract: dict, week_start: int) -> int:
        obj = contract["objective"]
        now = int(time.time())
        if obj == "survey_sweep":
            row = await self._db._fetchone(
                "SELECT COUNT(DISTINCT hex_id) as cnt FROM surveys WHERE player_key = ? AND surveyed_at >= ? AND surveyed_at <= ?",
                (pk, week_start, now),
            )
            return row["cnt"] if row else 0
        elif obj == "new_horizons":
            row = await self._db._fetchone(
                "SELECT COUNT(*) as cnt FROM hexes WHERE player_key = ? AND discovered_at >= ? AND discovered_at <= ?",
                (pk, week_start, now),
            )
            return row["cnt"] if row else 0
        elif obj == "long_shot":
            row = await self._db._fetchone(
                "SELECT MAX(distance_miles) as mx FROM surveys WHERE player_key = ? AND surveyed_at >= ? AND surveyed_at <= ?",
                (pk, week_start, now),
            )
            return int(row["mx"]) if row and row["mx"] else 0
        elif obj == "daily_patrol":
            row = await self._db._fetchone(
                """SELECT COUNT(DISTINCT date(surveyed_at, 'unixepoch')) as cnt
                   FROM surveys WHERE player_key = ? AND surveyed_at >= ? AND surveyed_at <= ?""",
                (pk, week_start, now),
            )
            return row["cnt"] if row else 0
        elif obj == "grand_traverse":
            row = await self._db._fetchone(
                "SELECT COUNT(DISTINCT hex_id) as cnt FROM surveys WHERE player_key = ? AND surveyed_at >= ? AND surveyed_at <= ?",
                (pk, week_start, now),
            )
            return int((row["cnt"] if row else 0) * HEX_AREA_SQ_MI)
        return 0
