; ============================================================================
; SuperSearch — Inno Setup 安装脚本
; ----------------------------------------------------------------------------
; 用途：把 PyInstaller 产出的单文件 exe 打包成"可直接安装"的 setup.exe。
;       内置 Everything 便携版已嵌入 SuperSearch.exe（PyInstaller data 文件），
;       因此安装包只需复制这一个 exe 即可"开箱即用"，无需用户单独安装 Everything。
;
; 步骤：
;   1) 先构建单文件 exe（已在项目 venv 中安装 PyInstaller 6.x）：
;        venv\Scripts\python.exe -m PyInstaller build\SuperSearch.spec
;      → 得到 dist\SuperSearch.exe
;   2) 安装 Inno Setup 6（https://jrsoftware.org/isinfo.php），然后编译本脚本：
;        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" SuperSearch.iss
;      → 得到 dist\SuperSearch-Setup.exe（双击即可安装使用）
; ============================================================================

#define MyAppName "SuperSearch"
#define MyAppVersion "1.2.1"
#define MyAppPublisher "Jiahui Zou"
#define MyAppExeName "SuperSearch.exe"
#define MyAppIco "assets\SuperSearch.ico"

[Setup]
; 生成一个稳定 GUID（同一应用请保持不变，便于升级安装）
AppId={{8E5A7C2E-9B1D-4E3A-9C1F-D3ADBEEF0001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 安装包图标（出现在 setup.exe 与任务栏）
SetupIconFile={#MyAppIco}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
; 允许用户自定义安装路径（向导中显示"选择目标位置"页）
DisableDirPage=no
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=SuperSearch-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; PySide6 仅提供 64 位轮子，安装 64 位
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 需要管理员权限：安装一次 Everything 服务，让标准用户的客户端经服务索引 NTFS，
; 从此不再弹“需要管理员权限索引 NTFS”对话框、也不触发 UAC。应用数据仍写在
; %LOCALAPPDATA%（每用户），服务仅用于后台 NTFS 索引（headless，无界面）。
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; 单文件 exe 自包含（已内嵌 Everything），安装时只需复制这一个文件
[Files]
Source: "dist\SuperSearch.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
; 桌面快捷方式默认勾选（任务默认即为选中；用户可在安装向导中取消）
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"

[Run]
; 先安装 Everything 服务（安装器已提权，静默完成、headless 无窗口）：让标准用户
; 客户端经服务索引 NTFS，从此不再弹“需要管理员权限”对话框、也不触发 UAC。
; 必须在启动应用前完成，客户端才能用上服务。安装服务是幂等的，重装/升级再跑无害
; （注意：RunOnceId 只能用于 [UninstallRun]，故此处不加）。
Filename: "{app}\{#MyAppExeName}"; Parameters: "--install-everything-service"; \
    Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

; 卸载时清理：内置 Everything 的运行数据（位于 %LOCALAPPDATA%\SuperSearch）、
; 以及尝试退出仍可能在运行的内置 Everything 实例（best-effort）。
[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\SuperSearch"

[UninstallRun]
; 退出内置 Everything 独立实例（若仍在运行）；失败不影响卸载流程
Filename: "{app}\{#MyAppExeName}"; Parameters: "--quit-everything"; \
    RunOnceId: "QuitBundledEverything"; Flags: runhidden skipifdoesntexist
; 卸载 Everything 服务（卸载器已提权；-uninstall-service 按服务名经 SCM 移除）
Filename: "{app}\{#MyAppExeName}"; Parameters: "--uninstall-everything-service"; \
    RunOnceId: "UninstallEverythingService"; Flags: runhidden skipifdoesntexist
