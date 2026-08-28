from lora_explorer.game.names import name_is_blocked


def test_blocks_clear_profanity_with_evasion():
    for n in ["Fuck Palace", "fuckpalace", "f u c k", "f.u.c.k off", "SHIT show", "total bitch"]:
        assert name_is_blocked(n), n


def test_blocks_ambiguous_terms_only_as_whole_words():
    assert name_is_blocked("Anal Palace")
    assert name_is_blocked("big ass")
    assert name_is_blocked("Cock Fort")


def test_blocks_reviewed_crude_compounds_and_stems():
    for n in [
        "Pussycat Palace",  # 'pussy' is now substring
        "Dickhead Pussyhole",  # the motivating example
        "Dickhead Manor",  # compound with no 'pussy' to lean on
        "total asshole",
        "what a twat",
        "Moby Dick",  # bare 'dick' now blocked as a whole word
        "Dick",  # ...including a standalone handle
    ]:
        assert name_is_blocked(n), n


def test_no_false_positive_on_innocent_names():
    for n in [
        "Analysis Master",
        "Grand Bass Player",
        "First Class",
        "Assassin's Guild",
        "Cocoon Keeper",
        "Scunthorpe Rambler",  # classic Scunthorpe problem — 'cunt' is word-mode
        "Document Reviewer",  # contains 'cum'
        "Titan Surveyor",  # contains 'tit'
        "Peacock Hollow",  # 'cock' is word-mode, not substring
        "Cockpit Ridge",
        "Dickens Trading Post",  # 'dick' is word-mode, so 'Dickens' passes
        "Dickinson Ridge",
    ]:
        assert not name_is_blocked(n), n


def test_empty_and_ordinary_names_pass():
    assert not name_is_blocked("")
    assert not name_is_blocked("Wandering Rhea")
