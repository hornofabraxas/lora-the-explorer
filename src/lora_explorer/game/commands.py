import logging
from dataclasses import dataclass
from enum import Enum, auto

log = logging.getLogger(__name__)

COMMAND_PREFIX = "/lora "


class CommandType(Enum):
    SURVEY = auto()
    CHARTER = auto()
    CHARTER_NAME = auto()
    UPKEEP = auto()
    UNKNOWN = auto()


@dataclass
class ParsedCommand:
    type: CommandType
    args: str = ""


def parse_command(text: str) -> ParsedCommand | None:
    text = text.strip()
    if not text.lower().startswith(COMMAND_PREFIX):
        return None

    body = text[len(COMMAND_PREFIX):].strip()
    if not body:
        return None

    lower = body.lower()

    if lower == "survey":
        return ParsedCommand(type=CommandType.SURVEY)

    if lower == "charter":
        return ParsedCommand(type=CommandType.CHARTER)

    # "reinforce" kept as a hidden alias so older iOS Shortcuts keep working.
    if lower in ("upkeep", "reinforce"):
        return ParsedCommand(type=CommandType.UPKEEP)

    # Anything else during an active charter session is treated as a name
    return ParsedCommand(type=CommandType.CHARTER_NAME, args=body)
