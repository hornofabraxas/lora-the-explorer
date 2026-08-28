"""Title registry — the honorifics a player can display beneath their name.

Two families:

- **Postcard titles** (single-player): earned by completing a 5-star postcard
  class. The title *is* the class name, so these mirror
  ``STARRED_POSTCARD_CLASSES`` in ``web/routes.py``. Selection lives on the
  Postcards page.
- **Multiplayer titles**: earned through Warfront / PvP play. Selection lives on
  the Multiplayer page (alongside postcard titles).

``active_title`` stores the human-readable **label** verbatim (e.g. ``Boundless``,
``Warlord``). That way the Worker/leaderboard can echo it to every player with no
lookup, and ``TITLE_MEANINGS`` maps a displayed label back to a short
"how it was earned" blurb for the tap-to-open popup.
"""

# --- Postcard titles: label -> the level-5 feat that earns it. ---------------
# Kept in sync with POSTCARD_DESCRIPTIONS[class][5] in web/routes.py (short form).
POSTCARD_TITLE_MEANINGS = {
    "Strider": "Survey at 35+ miles from home",
    "Trailblazer": "Discover 100 territories",
    "Relentless": "Sustain a 200-day survey streak",
    "Steadfast": "Hold a Survey Post for 90 days",
    "Boundless": "Survey 200 square miles total",
}

# --- Multiplayer titles: id -> (label, how it was earned). -------------------
# ids persist in the multiplayer `earned_titles` setting once earned; labels are
# what actually display and what get pushed to the Worker.
MULTIPLAYER_TITLES = {
    "warlord": ("Warlord", "Reach #1 on the Warfront"),
    "vanguard": ("Vanguard", "Break into the Warfront top 3"),
    "reaver": ("Reaver", "Win 5 raids"),
    "bulwark": ("Bulwark", "Repel 3 raids on your outposts"),
    "pathfinder": ("Pathfinder", "Scout 10 rival explorers"),
}

# id -> label, for turning earned-title ids into display labels.
MP_TITLE_LABELS = {tid: label for tid, (label, _desc) in MULTIPLAYER_TITLES.items()}

# Combined label -> meaning, powering the "what does this title mean?" popup for
# every title shown anywhere (your card, the picker, other players' rows).
TITLE_MEANINGS = {
    **POSTCARD_TITLE_MEANINGS,
    **{label: desc for (label, desc) in MULTIPLAYER_TITLES.values()},
}

# --- Award thresholds (deliberately low for a small player base; tweakable). --
REAVER_RAIDS_WON = 5
BULWARK_RAIDS_REPELLED = 3
PATHFINDER_SCOUTS = 10

# Rank-based titles (Warlord/Vanguard) carry two extra gates so that being #1 of a
# near-empty board — the state every launch and every fresh self-hosted ledger
# starts in — can't hand out a top honorific for free. You must clear an absolute
# renown floor AND stand in a field of at least MIN_RANKED_FIELD players. The
# raid/scout titles are already absolute achievements and need no such gate.
# These also make the seeded NPC "garrison" honest: a real player only takes the
# title by genuinely out-holding the field, not by outlasting the scaffolding.
WARLORD_MIN_RENOWN = 500
VANGUARD_MIN_RENOWN = 250
MIN_RANKED_FIELD = 4


def evaluate_multiplayer_titles(
    *,
    rank: int | None,
    raids_won: int,
    raids_repelled: int,
    scouts: int,
    my_renown: float = 0.0,
    field_size: int = 0,
) -> set[str]:
    """Return the set of multiplayer title **ids** the given stats currently
    qualify for. Awarding is monotonic — callers persist earned ids, so dropping
    back below a threshold (e.g. losing rank 1) never revokes a title.

    ``my_renown`` and ``field_size`` gate the rank-based titles only: a top rank
    awards nothing until the player clears the renown floor in a field of at
    least ``MIN_RANKED_FIELD``. Both default to 0, i.e. no rank title, so a caller
    that can't supply them never mints one by omission."""
    earned: set[str] = set()
    if rank is not None and field_size >= MIN_RANKED_FIELD:
        if rank <= 1 and my_renown >= WARLORD_MIN_RENOWN:
            earned.add("warlord")
        if rank <= 3 and my_renown >= VANGUARD_MIN_RENOWN:
            earned.add("vanguard")
    if raids_won >= REAVER_RAIDS_WON:
        earned.add("reaver")
    if raids_repelled >= BULWARK_RAIDS_REPELLED:
        earned.add("bulwark")
    if scouts >= PATHFINDER_SCOUTS:
        earned.add("pathfinder")
    return earned
