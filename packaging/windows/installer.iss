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
