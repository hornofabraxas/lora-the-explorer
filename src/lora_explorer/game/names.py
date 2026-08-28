"""Proactive profanity denylist for player-VISIBLE names — display names and
outpost names, the fields other players see (leaderboard / scout / raid).

Deliberately conservative: it blocks the clearest slurs and profanity at
name-set time so an obvious one can't reach other players, and it signals to the
player that the field is moderated. It is NOT the whole system — the operator
censor ladder on the Worker remains the backstop for anything that slips through.

This is the game client's instant-feedback copy. The authoritative check (a
modified client can't skip it) lives in the Worker (``lora-worker`` –
``src/logic/names.ts``). **Keep the two lists identical when editing.**

Two match modes manage the Scunthorpe problem (blocking "Scunthorpe" for a
contained slur, or "Analysis" for "anal"):

    SUBSTRING — terms with no innocent-word collisions. Matched anywhere, on the
    name with separators stripped, so "f u c k" and "f.u.c.k" are caught too.

    WORD — terms that also appear inside ordinary words. Matched only as whole
    words, so "class"/"analysis"/"cocoon" pass while standalone "ass"/"anal"/
    "coon" are blocked.

Editing guidance: a term is SUBSTRING-safe only if no common word contains it.
When unsure, put it in ``WORD_TERMS`` — a whole-word match is the safe default.
The cost of a false block is low (the player picks another name and is told why);
the cost of a miss is a slur on everyone's board — so we lean toward blocking.

Ambiguous stems stay WORD to protect real names (Bass, Canal, Scunthorpe,
Raccoon, Spice, Peacock, Shoe...), but their clearly-offensive COMPOUNDS are
listed as SUBSTRING (asshole, dickhead...) since those collide with nothing.
Bare "dick" IS included as WORD by choice: it blocks a standalone "Dick" and
"Moby Dick" too, accepting that a player named Richard must pick another handle
— the crude use outweighs the collision here. It stays WORD (not substring) so
"Dickens"/"Dickinson" still pass.
"""

import re

SUBSTRING_TERMS = [
    "fuck", "shit", "bitch", "nigger", "nigga", "faggot",
    "whore", "wetback", "tranny", "beaner", "kike", "slut",
    "pussy", "twat", "asshole", "cocksucker",
    "dickhead", "dickface", "dickhole", "dickwad",
]
WORD_TERMS = [
    "ass", "anal", "cum", "cunt", "coon", "spic", "chink",
    "fag", "tit", "tits", "hoe", "cock", "dick",
]


def name_is_blocked(raw: str) -> bool:
    """True if ``raw`` contains a denylisted term. Case-insensitive. See the
    module docstring for the two match modes. Empty/blank names are never blocked
    (length rules handle those elsewhere)."""
    lower = raw.lower()
    collapsed = re.sub(r"[^a-z0-9]", "", lower)
    if any(term in collapsed for term in SUBSTRING_TERMS):
        return True
    words = set(re.split(r"[^a-z0-9]+", lower))
    return any(term in words for term in WORD_TERMS)
