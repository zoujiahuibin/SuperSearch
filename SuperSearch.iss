; ============================================================================
; SuperSearch — Inno Setup 安装脚本
; ----------------------------------------------------------------------------
; 用途：把 PyInstaller 产出的单文件 exe 打包成"可直接安装"的 setup.exe。
;
; 步骤：
;   1) 先构建单文件 exe（已在项目 venv 中安装 PyInstaller 6.x）：
;        venv/Scripts/python.exe -m PyInstaller --noconfirm --clean ^
;          --onefile --windowed --name SuperSearch --distpath dist --workpath build main.py
;      → 得到 dist\SuperSearch.exe
;   2) 安装 Inno Setup 6（https://jrsoftware.org/isinfo.php），然后编译本脚本：
;        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" SuperSearch.iss
;      → 得到 dist\SuperSearch-Setup.exe（双击即可安装使用）
; ============================================================================

#define MyAppName "SuperSearch"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "Jiahui Zou"
#define MyAppExeName "SuperSearch.exe"

[Setup]
; 生成一个稳定 GUID（同一应用请保持不变，便于升级安装）
AppId={{8E5A7C2E-9B1D-4E3A-9C1F-D3ADBEEF0001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=SuperSearch-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; PySide6 仅提供 64 位轮子，安装 64 位
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; 单文件 exe 自包含，安装时只需复制这一个文件
[Files]
Source: "dist\SuperSearch.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent
