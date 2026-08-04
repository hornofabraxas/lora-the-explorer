import hashlib
import hmac
import logging
import time

from ..game.engine import upkeep_grace_days
from ..game.hex_names import hex_name

log = logging.getLogger(__name__)

# Grid step (degrees) the home centroid is snapped to before it leaves this
# install. ~0.75° ≈ 50mi cells; true home lands within ~26mi of the reported
# point. Keep this coarse — it is the only geography the Worker ever sees.
COARSE_CENTROID_STEP_DEG = 0.75


def _snap_coarse(value: float) -> float:
    """Snap a coordinate to the coarse privacy grid (nearest ~0.75°)."""
    return round(round(value / COARSE_CENTROID_STEP_DEG) * COARSE_CENTROID_STEP_DEG, 3)


async def build_bundle(db, since_timestamp: int | None = None, force: bool = False) -> dict | None:
    player = await db.get_first_player()
    if not player:
        return None

    player_key = player["key"]
    now = int(time.time())
    since = since_timestamp or 0

    surveys = await db.fetch_surveys_since(player_key, since) or []

    posts = await db.get_all_posts(player_key)

    if not surveys and not force:
        return None

    # Effective ruin grace for this player (camp 7 extends it). The Worker fades &
    # freezes renown the same way the Outposts card does, so it needs the grace.
    grace_days = upkeep_grace_days(player)

    survey_count = len(surveys)
    discoveries = sum(1 for s in surveys if s.get("is_discovery"))
    post_summaries = []

    for post in posts:
        # The post's identity outside this install is its opaque mp_token — the
        # real H3 hex_id decodes straight to coordinates and must never cross
        # the trust boundary. Every post has a token (assigned at charter, and
        # backfilled on upgrade), so a missing one means something is wrong
        # locally — skip the post rather than fall back to leaking the hex.
        token = post.get("mp_token")
        if not token:
            log.warning("Post %s has no mp_token — omitted from bundle", post.get("id"))
            continue
        # Only a custom name is sent. The auto-name is hex_name(hex_id) — a
        # deterministic hash of the real hex, brute-forceable over a region —
        # so an auto-named post ships "" and rivals' clients render a name
        # from the token instead.
        name = post.get("name") or ""
        if name == hex_name(post["hex_id"]):
            name = ""
        post_summaries.append({
            "post_token": token,
            "level": post.get("level", 1),
            "name": name,
            # survey_posts stores the charter time in `created_at`; send that so
            # the Worker's renown reflects true post age. (Falling back to `now`
            # would reset chartered_at on every push, pinning renown at its floor.)
            "chartered_at": post.get("created_at") or now,
            # Warded (dormant) outposts can't be raided — the Worker enforces
            # this against dispatch. 0 = not warded.
            "dormant_until": post.get("ruin_frozen_until") or 0,
            # Ruin inputs so the Worker can fade renown/day and freeze the
            # leaderboard total for posts falling into ruin (mirrors the card).
            "last_tended_at": post.get("last_tended_at") or post.get("created_at") or now,
            "warded_at": post.get("warded_at") or 0,
            "grace_days": grace_days,
        })

    # Coarse centroid (home location snapped to a ~0.75° grid, ~50mi cells) for
    # raid travel-time distance. Deliberately much coarser than exact GPS — a
    # reported centroid places true home anywhere within ~26mi of it — so it
    # stays well inside the privacy boundary while still letting the Worker
    # compute inter-player distance. That centroid is the ONLY geography in the
    # bundle. (Raid travel time only needs order-of-magnitude distance, so this
    # coarsening costs nothing gameplay-wise.)
    bundle = {
        "survey_count": survey_count,
        "discoveries": discoveries,
        "post_summaries": post_summaries,
        "timestamp": now,
        # Chosen title (a registry label, or "" for none) so the Worker can echo
        # it on every player's leaderboard row. Worker validates it against the
        # same registry.
        "active_title": player.get("active_title") or "",
    }
    if player.get("home_lat") is not None and player.get("home_lon") is not None:
        bundle["coarse_centroid"] = {
            "lat": _snap_coarse(float(player["home_lat"])),
            "lng": _snap_coarse(float(player["home_lon"])),
        }
    return bundle


def sign_bundle(bundle_json: str, player_id: str, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    message = player_id + timestamp + bundle_json
    signature = hmac.new(
        secret.encode(), message.encode(), hashlib.sha256,
    ).hexdigest()
    return {
        "X-Player-ID": player_id,
        "X-Timestamp": timestamp,
        "X-Signature": signature,
    }
