; Inno Setup script for LoRa the Explorer.
;
; Build (after `pyinstaller packaging\windows\lora-explorer.spec` has produced
; dist\LoRaTheExplorer\):
;
;   iscc /DAppVersion=0.2.0 packaging\windows\installer.iss
;
; AppVersion falls back to 0.0.0 for a local test build that doesn't pass it.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppName=LoRa the Explorer
AppVersion={#AppVersion}
AppPublisher=hornofabraxas
AppPublisherURL=https://github.com/hornofabraxas/lora-the-explorer
DefaultDirName={autopf}\LoRaTheExplorer
DefaultGroupName=LoRa the Explorer
UninstallDisplayIcon={app}\LoRaTheExplorer.exe
OutputDir=..\..\dist-installer
OutputBaseFilename=LoRaTheExplorer-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

; An in-place upgrade has to clear the previous build's payload, not just
; overwrite the files the new one happens to ship. PyInstaller puts the app's
; own metadata in `_internal\lora_the_explorer-<version>.dist-info`, so the
; folder NAME changes every release and the old one has no matching entry to
; overwrite it. Both then sit side by side, and importlib.metadata (which is
; where __version__ comes from) returns whichever it enumerates first, which on
; NTFS is the alphabetically lower, older one. That is why 0.4.2 installed over
; 0.4.1 kept reporting itself as 0.4.1. Wiping the payload also clears modules
; that were deleted between releases.
;
; Scoped to _internal rather than {app}\*: UninstallFilesDir is unset, so Inno's
; own unins*.exe/.dat live directly in {app} and must survive the upgrade.
; Player data (database, logs) is in %LOCALAPPDATA%\LoRaTheExplorer and is not
; touched by any of this.
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

; ignoreversion stays: if anything survives the delete above (a file locked by a
; still-running instance, say), version/timestamp comparison could otherwise
; skip replacing it.
[Files]
Source: "..\..\dist\LoRaTheExplorer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\LoRa the Explorer"; Filename: "{app}\LoRaTheExplorer.exe"
Name: "{group}\Uninstall LoRa the Explorer"; Filename: "{uninstallexe}"
Name: "{autodesktop}\LoRa the Explorer"; Filename: "{app}\LoRaTheExplorer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\LoRaTheExplorer.exe"; Description: "Launch LoRa the Explorer"; Flags: nowait postinstall skipifsilent

; Deliberately no [UninstallDelete] entry for %LOCALAPPDATA%\LoRaTheExplorer.
; That directory holds the SQLite database — full survey/location history —
; and the log file. Uninstalling removes the program; it must not silently
; delete a player's save data. If someone wants that gone too, they remove it
; themselves.
