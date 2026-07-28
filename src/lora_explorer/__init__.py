from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("lora-the-explorer")
except PackageNotFoundError:
    # Running from a source tree that was never `pip install`-ed (e.g. as a
    # loose checkout rather than the .venv editable install everything else
    # here assumes).
    __version__ = "0.0.0+unknown"
