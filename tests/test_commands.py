from lora_explorer.game.commands import parse_command, CommandType


def test_parse_survey():
    cmd = parse_command("/lora survey")
    assert cmd is not None
    assert cmd.type == CommandType.SURVEY


def test_parse_charter():
    cmd = parse_command("/lora charter")
    assert cmd is not None
    assert cmd.type == CommandType.CHARTER


def test_parse_upkeep():
    cmd = parse_command("/lora upkeep")
    assert cmd is not None
    assert cmd.type == CommandType.UPKEEP


def test_parse_reinforce_alias():
    # "reinforce" stays a hidden alias for upkeep so old iOS Shortcuts still work.
    cmd = parse_command("/lora reinforce")
    assert cmd is not None
    assert cmd.type == CommandType.UPKEEP


def test_parse_charter_name():
    cmd = parse_command("/lora Hotdog")
    assert cmd is not None
    assert cmd.type == CommandType.CHARTER_NAME
    assert cmd.args == "Hotdog"


def test_parse_charter_name_with_spaces():
    cmd = parse_command("/lora Midtown East")
    assert cmd is not None
    assert cmd.type == CommandType.CHARTER_NAME
    assert cmd.args == "Midtown East"


def test_parse_ignores_non_lora():
    assert parse_command("hello world") is None
    assert parse_command("hey /lora survey") is None
    assert parse_command("") is None


def test_parse_case_insensitive():
    cmd = parse_command("/LORA SURVEY")
    assert cmd is not None
    assert cmd.type == CommandType.SURVEY

    cmd = parse_command("/Lora Charter")
    assert cmd is not None
    assert cmd.type == CommandType.CHARTER


def test_parse_empty_after_prefix():
    assert parse_command("/lora ") is None
    assert parse_command("/lora") is None
