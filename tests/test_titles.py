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


def test_rank_one_earns_warlord_and_vanguard():
    earned = evaluate_multiplayer_titles(rank=1, raids_won=0, raids_repelled=0, scouts=0)
    assert earned == {"warlord", "vanguard"}


def test_rank_three_earns_vanguard_only():
    earned = evaluate_multiplayer_titles(rank=3, raids_won=0, raids_repelled=0, scouts=0)
    assert earned == {"vanguard"}


def test_rank_four_earns_nothing_from_rank():
    earned = evaluate_multiplayer_titles(rank=4, raids_won=0, raids_repelled=0, scouts=0)
    assert earned == set()


def test_unknown_rank_skips_rank_titles():
    earned = evaluate_multiplayer_titles(rank=None, raids_won=99, raids_repelled=99, scouts=99)
    assert "warlord" not in earned and "vanguard" not in earned


def test_combat_and_scout_thresholds():
    assert "reaver" not in evaluate_multiplayer_titles(rank=None, raids_won=4, raids_repelled=0, scouts=0)
    assert "reaver" in evaluate_multiplayer_titles(rank=None, raids_won=5, raids_repelled=0, scouts=0)
    assert "bulwark" not in evaluate_multiplayer_titles(rank=None, raids_won=0, raids_repelled=2, scouts=0)
    assert "bulwark" in evaluate_multiplayer_titles(rank=None, raids_won=0, raids_repelled=3, scouts=0)
    assert "pathfinder" not in evaluate_multiplayer_titles(rank=None, raids_won=0, raids_repelled=0, scouts=9)
    assert "pathfinder" in evaluate_multiplayer_titles(rank=None, raids_won=0, raids_repelled=0, scouts=10)
