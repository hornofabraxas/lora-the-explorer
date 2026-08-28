from lora_explorer.game.titles import (
    MP_TITLE_LABELS,
    MULTIPLAYER_TITLES,
    POSTCARD_TITLE_MEANINGS,
    TITLE_MEANINGS,
    evaluate_multiplayer_titles,
)


def test_title_meanings_covers_every_label():
    # Every displayable label (postcard class + MP label) has a meaning blurb.
    for label in POSTCARD_TITLE_MEANINGS:
        assert label in TITLE_MEANINGS
    for _tid, (label, _desc) in MULTIPLAYER_TITLES.items():
        assert label in TITLE_MEANINGS
    assert len(TITLE_MEANINGS) == len(POSTCARD_TITLE_MEANINGS) + len(MULTIPLAYER_TITLES)


def test_mp_title_labels_map_matches_registry():
    assert set(MP_TITLE_LABELS) == set(MULTIPLAYER_TITLES)
    for tid, (label, _desc) in MULTIPLAYER_TITLES.items():
        assert MP_TITLE_LABELS[tid] == label


# A full field with ample renown — enough to clear both the field-size and renown
# gates, so these exercise the pure rank logic.
_FIELD = dict(field_size=8, my_renown=10_000)


def test_rank_one_earns_warlord_and_vanguard():
    earned = evaluate_multiplayer_titles(rank=1, raids_won=0, raids_repelled=0, scouts=0, **_FIELD)
    assert earned == {"warlord", "vanguard"}


def test_rank_three_earns_vanguard_only():
    earned = evaluate_multiplayer_titles(rank=3, raids_won=0, raids_repelled=0, scouts=0, **_FIELD)
    assert earned == {"vanguard"}


def test_rank_four_earns_nothing_from_rank():
    earned = evaluate_multiplayer_titles(rank=4, raids_won=0, raids_repelled=0, scouts=0, **_FIELD)
    assert earned == set()


def test_unknown_rank_skips_rank_titles():
    earned = evaluate_multiplayer_titles(
        rank=None, raids_won=99, raids_repelled=99, scouts=99, **_FIELD
    )
    assert "warlord" not in earned and "vanguard" not in earned


def test_rank_titles_gated_by_renown_floor():
    # #1, but below the renown floors: no rank title despite the top rank.
    earned = evaluate_multiplayer_titles(
        rank=1, raids_won=0, raids_repelled=0, scouts=0, field_size=8, my_renown=100
    )
    assert earned == set()
    # Between the two floors: Vanguard clears (>=250), Warlord doesn't (<500).
    earned = evaluate_multiplayer_titles(
        rank=1, raids_won=0, raids_repelled=0, scouts=0, field_size=8, my_renown=300
    )
    assert earned == {"vanguard"}


def test_rank_titles_gated_by_field_size():
    # #1 of a near-empty board earns nothing, however high the renown — the whole
    # point of the gate: launch state / a fresh self-hosted ledger.
    earned = evaluate_multiplayer_titles(
        rank=1, raids_won=0, raids_repelled=0, scouts=0, field_size=3, my_renown=10_000
    )
    assert earned == set()


def test_rank_titles_omitted_context_mints_nothing():
    # A caller that supplies neither renown nor field size never mints a rank
    # title by omission (defaults are 0).
    earned = evaluate_multiplayer_titles(rank=1, raids_won=0, raids_repelled=0, scouts=0)
    assert "warlord" not in earned and "vanguard" not in earned


def test_combat_and_scout_thresholds():
    assert "reaver" not in evaluate_multiplayer_titles(rank=None, raids_won=4, raids_repelled=0, scouts=0)
    assert "reaver" in evaluate_multiplayer_titles(rank=None, raids_won=5, raids_repelled=0, scouts=0)
    assert "bulwark" not in evaluate_multiplayer_titles(rank=None, raids_won=0, raids_repelled=2, scouts=0)
    assert "bulwark" in evaluate_multiplayer_titles(rank=None, raids_won=0, raids_repelled=3, scouts=0)
    assert "pathfinder" not in evaluate_multiplayer_titles(rank=None, raids_won=0, raids_repelled=0, scouts=9)
    assert "pathfinder" in evaluate_multiplayer_titles(rank=None, raids_won=0, raids_repelled=0, scouts=10)
