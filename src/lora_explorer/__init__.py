import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _stamped_version() -> str | None:
    """The version written into a PyInstaller bundle at build time, if any.

    A frozen build cannot trust importlib.metadata for its own version. The
    app's metadata lives in a version-stamped folder
    (`_internal/lora_the_explorer-0.4.2.dist-info`), so a Windows in-place
    upgrade left the previous release's folder sitting beside the new one, and
    metadata lookup returns whichever the filesystem enumerates first (the
    alphabetically lower, older one on NTFS) rather than the newest. That is how
    0.4.2 came to report itself as 0.4.1. The installer now clears the old
    payload; this stamp makes the answer independent of whatever else happens to
    be lying around next to it. Returns None outside a frozen build, or when the
    stamp is missing, so the metadata lookup stays the source of truth there."""
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    root = Path(meipass) / "lora_explorer" if meipass else Path(__file__).parent
    try:
        return (root / "_version.txt").read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        # This runs while the root package is being imported, so anything raised
        # here takes down every entry point. A damaged stamp (interrupted
        # install, corrupt bytes) must degrade to the metadata lookup below,
        # exactly like a missing one.
        return None


def _resolve_version() -> str:
    stamped = _stamped_version()
    if stamped:
        return stamped
    try:
        return version("lora-the-explorer")
    except PackageNotFoundError:
        # Running from a source tree that was never `pip install`-ed (e.g. as a
        # loose checkout rather than the .venv editable install everything else
        # here assumes).
        return "0.0.0+unknown"


__version__ = _resolve_version()
