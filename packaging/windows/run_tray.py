"""PyInstaller entry point for the Windows tray build.

A plain script calling windows_tray.main() is the most reliable thing to
point PyInstaller's Analysis at — more reliable than trying to resolve the
`lora-explorer-tray` console-script entry point through PyInstaller's own
import machinery.
"""
from lora_explorer.windows_tray import main

if __name__ == "__main__":
    main()
