# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Windows tray build. Build from the repo root:

    pip install .[windows]
    pyinstaller packaging/windows/lora-explorer.spec --noconfirm

Produces dist/LoRaTheExplorer/ (onedir, not onefile) — a self-contained
folder rather than a single slow-to-unpack .exe, and the shape
installer.iss then wraps into an actual installer.
"""
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None

REPO_ROOT = Path(SPECPATH).resolve().parent.parent  # packaging/windows/ -> repo root
SRC = REPO_ROOT / "src"

# Packages whose dynamic-import or native-loader behavior PyInstaller's static
# analysis can't fully see on its own: uvicorn picks its protocol/loop
# backends at runtime, bleak bridges to WinRT for BLE, meshcore/h3 wrap
# compiled extensions, authlib/joserfc pick crypto backends dynamically.
# collect_all is the defensive "make it work" answer for exactly this —
# costs bundle size, not correctness. pystray needs the same treatment for
# its per-platform backend selection, and pyserial's list_ports imports its
# per-OS backend (list_ports_windows) by name at call time.
COLLECT_ALL = ["uvicorn", "bleak", "meshcore", "h3", "authlib", "joserfc", "pystray",
               "serial"]

datas = [
    (str(SRC / "lora_explorer" / "web" / "templates"), "lora_explorer/web/templates"),
    (str(SRC / "lora_explorer" / "web" / "static"), "lora_explorer/web/static"),
]
binaries = []
hiddenimports = []

for pkg in COLLECT_ALL:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Stamp the version into the bundle. A frozen app can't trust
# importlib.metadata for its own version: the metadata folder name carries the
# version, so an in-place Windows upgrade could leave the previous release's
# folder beside the new one and the lookup returns whichever is enumerated
# first (see lora_explorer/__init__.py). pyproject.toml stays the single
# source; this only copies it to where the frozen app can read it directly.
_version = tomllib.loads(
    (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
)["project"]["version"]
_version_txt = REPO_ROOT / "packaging" / "windows" / "_version.txt"
_version_txt.write_text(_version, encoding="utf-8")
datas += [(str(_version_txt), "lora_explorer")]

# Generated here (not committed as a binary asset) so the installer icon is
# always derived from the same source art the web UI already uses.
from PIL import Image  # noqa: E402  (after collect_all, which needs pillow available regardless)

_icon_ico = REPO_ROOT / "packaging" / "windows" / "icon.ico"
Image.open(SRC / "lora_explorer" / "web" / "static" / "icon-512.png").save(
    _icon_ico, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)],
)

a = Analysis(
    [str(REPO_ROOT / "packaging" / "windows" / "run_tray.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LoRaTheExplorer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # tray app — no console window
    icon=str(_icon_ico),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="LoRaTheExplorer",
)
