from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Awaitable


class PositionFailure(Enum):
    TIMEOUT = "timeout"
    NO_GPS = "no_gps"
    ERROR = "error"


@dataclass
class PositionResult:
    position: tuple[float, float] | None = None
    failure: PositionFailure | None = None

    @property
    def ok(self) -> bool:
        return self.position is not None


@dataclass
class IncomingMessage:
    sender_key: str
    text: str
    snr: float | None = None
    rssi: int | None = None
    hops: int | None = None
    lat: float | None = None
    lon: float | None = None
    timestamp: int | None = None


MessageHandler = Callable[[IncomingMessage], Awaitable[str | None]]


class RadioAdapter(ABC):
    configured: bool = True
    _mc = None

    @abstractmethod
    async def connect(self) -> None:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    async def send_message(self, recipient_key: str, text: str) -> bool:
        ...

    @abstractmethod
    async def set_message_handler(self, handler: MessageHandler) -> None:
        ...

    @abstractmethod
    async def request_position(
        self, node_key: str, progress_callback: Callable[[str, str, float], Awaitable[None]] | None = None,
    ) -> PositionResult:
        """Request GPS coordinates from a remote node via telemetry."""
        ...

    def get_travel_mode(self) -> str:
        return "walking"

    def set_travel_mode(self, mode: str) -> None:
        pass

    def get_last_contact_ts(self, node_key: str) -> int | None:
        return None

    def congested(self) -> bool:
        """True when the mesh is busy enough that survey cadence should back off.
        Adapters without airtime accounting are never congested."""
        return False

    async def get_companion_status(self) -> dict:
        return {"connected": False}

    def get_contacts(self) -> dict:
        return {}

    async def reboot_companion(self) -> bool:
        return False
