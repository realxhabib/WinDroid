; The WinDroid installer: for the current user, no administrator needed. Installs dist\WinDroid (see
; windroid.spec) to %LOCALAPPDATA%\Programs\WinDroid, with a Start menu entry and an Apps & features entry.
; tools/build_installer.py builds it:  ISCC /DAppVersion=1.2.3 packaging\windroid.iss
;
; The same installer updates an installed WinDroid: it has WinDroid quit first (closing its windows and
; shutting Android down), replaces the program, and with /relaunch=1 (the auto-updater passes it) starts it
; again. Uninstalling removes the program and its shortcuts, and asks whether to delete Android too.

#ifndef AppVersion
  #error Pass the version: ISCC /DAppVersion=1.2.3 windroid.iss
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\WinDroid"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
; AppId never changes: it's how an update finds the installed WinDroid, and Windows its uninstaller.
AppId={{BF1B5625-9C01-4195-8D9A-402D3B89FEA0}
AppName=WinDroid
AppVersion={#AppVersion}
AppVerName=WinDroid {#AppVersion}
AppPublisher=realxhabib
AppPublisherURL=https://github.com/realxhabib/WinDroid
AppSupportURL=https://github.com/realxhabib/WinDroid/issues
AppUpdatesURL=https://github.com/realxhabib/WinDroid/releases
VersionInfoVersion={#AppVersion}
VersionInfoProductName=WinDroid
VersionInfoDescription=WinDroid Setup
PrivilegesRequired=lowest
DefaultDirName={userpf}\WinDroid
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1809: Windows Hypervisor Platform and WebView2 are there
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=WinDroid-Setup-{#AppVersion}
SetupIconFile=..\androiddesktop\assets\icon.ico
UninstallDisplayIcon={app}\WinDroid.exe
UninstallDisplayName=WinDroid
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
SetupMutex=WinDroidSetup
; MCP servers (WinDroid.exe mcp, run by an AI app) may still have the program open: close them.
CloseApplications=force
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An update replaces the program's libraries wholesale: nothing of the old version lingers.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
; The launcher window's AppUserModelID (shortcuts.LAUNCHER_ID), so it and this entry are one app to Windows.
Name: "{userprograms}\WinDroid"; Filename: "{app}\WinDroid.exe"; Comment: "Android apps in their own windows"; AppUserModelID: "AndroidDesktop.Launcher"
Name: "{userdesktop}\WinDroid"; Filename: "{app}\WinDroid.exe"; Comment: "Android apps in their own windows"; AppUserModelID: "AndroidDesktop.Launcher"; Tasks: desktopicon

[Run]
Filename: "{app}\WinDroid.exe"; Description: "{cm:LaunchProgram,WinDroid}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\WinDroid.exe"; Flags: nowait; Check: Relaunch

[UninstallRun]
; Quit WinDroid (Android shuts down cleanly) and remove the app shortcuts it made (Start menu, desktop).
Filename: "{app}\WinDroid.exe"; Parameters: "uninstall"; RunOnceId: "QuitAndRemoveShortcuts"; Flags: waituntilterminated

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// After an update the auto-updater ran (/SILENT /relaunch=1): start WinDroid again.
function Relaunch: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:relaunch|0}') = '1');
end;

// An update: WinDroid must be gone before its files can be replaced. "quit" closes its windows, shuts
// Android down cleanly, and returns once WinDroid has exited.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\WinDroid.exe')) then
    Exec(ExpandConstant('{app}\WinDroid.exe'), 'quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

// Where WinDroid keeps Android and its data (androiddesktop/paths.py: DATA).
function DataDir: String;
begin
  Result := GetEnv('ANDROIDDESKTOP_DATA');
  if Result = '' then
    Result := ExpandConstant('{localappdata}\AndroidDesktop');
end;

// Bytes in a folder, not counting what links in it point to.
function FolderSize(const Dir: String): Extended;
var
  F: TFindRec;
begin
  Result := 0;
  if FindFirst(Dir + '\*', F) then
  try
    repeat
      if (F.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then
        Result := Result + F.SizeHigh * 4294967296.0 + F.SizeLow
      else if (F.Name <> '.') and (F.Name <> '..') and ((F.Attributes and FILE_ATTRIBUTE_REPARSE_POINT) = 0) then
        Result := Result + FolderSize(Dir + '\' + F.Name);
    until not FindNext(F);
  finally
    FindClose(F);
  end;
end;

function SizeText(Bytes: Extended): String;
begin
  if Bytes >= 1073741824.0 then
    Result := Format('%.1f GB', [Bytes / 1073741824.0])
  else
    Result := Format('%d MB', [Round(Bytes / 1048576.0)]);
end;

// The program is gone: ask whether Android goes too. No by default, and in a silent uninstall.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  Dir := DataDir;
  if not DirExists(Dir) then
    exit;
  if SuppressibleMsgBox('Also delete Android and everything in it?' + #13#10#13#10 +
      'That''s the Android that WinDroid downloaded and the apps you installed in it, with their data and ' +
      'your Google sign-in: ' + SizeText(FolderSize(Dir)) + ' in ' + Dir + '.' + #13#10#13#10 +
      'Keep it if you might install WinDroid again: it carries on where you left off.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
    if not DelTree(Dir, True, True, True) then
      SuppressibleMsgBox('Some of ' + Dir + ' couldn''t be deleted. You can delete what''s left yourself.',
        mbInformation, MB_OK, IDOK);
end;
