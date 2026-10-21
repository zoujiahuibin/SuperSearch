# SuperSearch：统一检索（本地文件 + ima 知识库）

一个独立的桌面 GUI（PySide6），同时检索：

- **本地文件**：通过 Everything HTTP Server。v1.2.0 起**内置 Everything 便携版**（随 exe 打包，首次运行自动拉起），无需单独安装 Everything。v1.2.1 起安装器会装好 **Everything 服务**，内置实例改为**后台无界面**运行——不再弹“需要管理员权限索引 NTFS”对话框，也不再显示 Everything 窗口/托盘图标。v1.2.2 修正了 HTTP 服务器的启用方式（ini 键名），并新增索引就绪等待与应用退出时的数据库保存。
- **百度网盘文件**：经百度网盘开放平台检索（v1.3.0 新增）。在「设置 → 百度网盘授权…」完成一次设备码授权后即可检索账号网盘内的文件（文件名关键词，全盘）；双击结果会在浏览器打开网盘并定位到所在目录。
- **ima 知识库文档/笔记**：通过 ima 官方 OpenAPI。

结果合并在同一张列表里，用**来源标签**区分（蓝色=本地，绿色=ima），支持按来源筛选、双击打开本地文件、查看 ima 高亮摘要。

界面顶部勾选「ima 知识库」后会展开**知识库类型**勾选区（个人知识库 / 共享知识库 / 订阅知识库），默认三类全选（等于检索全部）；取消某类即只检索该类型知识库（按 ima 返回的 `base_type` 字段过滤）；**三类全不选则完全不检索 ima 知识库**。勾选「本地」后同理展开**盘符**勾选区，限定本地检索范围。

> 注意：ima 对**订阅知识库**的「资料获取」有**每日额度限制**（错误码 220021，提示"资料获取次数已达上限，请明天再尝试"）。额度耗尽当日订阅库无法全文检索，个人知识库不受影响；次日额度恢复后订阅库即可正常检索。程序会把这类重复报错合并成一条明确提示，不会刷屏。

底部**状态栏**在检索进行时显示**百分比进度条**（格式 `%p%`，如 `45%`）：其中本地检索（Everything，较快）占前 20%，ima 跨知识库检索（较慢，逐库推进）占后 80%；检索完成进度条自动归 100% 并隐藏，左侧文本显示"共 N 条结果"。

结果表格的**每一列宽度都可由用户拖动表头边界手动调整**（列为 `QHeaderView.Interactive` 模式，而非自动拉伸）；表头还支持**拖动换列顺序**。首次填充结果时会按内容自适应一次列宽，之后尊重你的手动调整（切换来源筛选或再次检索都不会覆盖）。

本地检索结果的排序与 **Everything 原软件一致**：默认按名称升序（与 Everything GUI 默认相同），可在界面「排序」下拉框切换为名称/路径/大小/修改时间的升序或降序，以对齐你 Everything 里的排序列与方向。

结果列表的**标题列带系统文件/文件夹小图标**（16×16，与 Everything / 资源管理器同源，按扩展名区分文件类型，文件夹为文件夹图标）；ima 文档显示通用文档图标。图标由 `QFileIconProvider` 在 Windows 上调用系统 shell 图标，故与 Everything 显示一致。

---

## 环境要求

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | **Windows**（推荐） | 本地检索依赖 Everything HTTP Server；文件图标走 Windows shell 图标，故与 Everything / 资源管理器一致。ima 检索跨平台可用，但未在 macOS/Linux 验证。 |
| Python | **3.9+**（开发用 3.10） | 需能安装 PySide6 6.x。 |
| 依赖 | `PySide6>=6.5`、`requests>=2.28` | 见 `requirements.txt`，用 `pip install -r requirements.txt` 安装。 |
| Everything | 1.4+（已内置） | 仅本地检索需要。v1.2.0 起程序已内置 Everything 便携版并自动拉起其 HTTP Server（默认 `127.0.0.1:80`，被占用则退 8180）；若本机已装并启用了 Everything HTTP 服务，则直接复用，不再重复拉起。v1.2.1 起安装器安装 Everything 服务（需管理员），内置实例以 `-startup` 后台运行、无窗口、无托盘图标。v1.2.2 修正 ini 键名为合法的 `http_server_enabled=1`，检索时等待索引就绪（冷启动重建约 80s，期间提示"正在建立本地文件索引"），应用退出时保存数据库使下次启动数秒内可用。 |
| 网络 | 需能访问 `ima.qq.com` | 仅 ima 检索需要；本地检索纯内网。 |
| 百度网盘 | 可选（v1.3.0 起） | 需在[百度网盘开放平台](https://pan.baidu.com/union/)自建应用取得 Client ID / Secret，并在软件内完成一次设备码授权（浏览器输码）。仅网盘文件检索需要，不影响本地 / ima 检索。 |
| ima 凭证 | Client ID / API Key | 仅 ima 检索需要，在 [ima 开放平台](https://ima.qq.com/agent-interface) 申请。 |

---

## 前置条件

### 1. Everything HTTP Server（本地检索）
Everything 1.4 自带 HTTP 服务器：

- 打开 Everything → **工具 / 选项 → HTTP 服务器**
- 勾选「启用 HTTP 服务器」，绑定 `127.0.0.1`，记下端口（默认 `80`）
- 重启 Everything 使设置生效

> 本工具通过 `http://127.0.0.1:端口/?search=关键词&json=1` 查询，无需 Everything SDK 或额外下载。

### 2. ima 凭证（知识库检索）
三种配置方式任选其一（优先级：环境变量 > GUI 配置 > 文件）：

- **GUI 配置（推荐）**：菜单栏「设置 → ima 凭证…」填入 Client ID / API Key，确认后写入
  `~/.config/ima/`（API Key 以掩码显示），下一次检索即时生效，无需重启。
- **文件**（与 ima 官方 skill 一致）：

```
~/.config/ima/client_id
~/.config/ima/api_key
```

- **环境变量**：

```
IMA_OPENAPI_CLIENTID=xxx
IMA_OPENAPI_APIKEY=xxx
```

> 凭证在 `https://ima.qq.com/agent-interface` 申请（即 ima 开放平台 / Agent 接口页），得到一对
> **Client ID**（较短，约 32 字符）与 **API Key**（较长，约 76 字符）。二者务必**对应同一应用**，
> 若填反会出现 `skill auth failed (code 200002)`。未配置时本地检索仍可正常使用，ima 检索会给出提示。

### 3. （可选）指定知识库
默认（未设置 `IMA_KB_IDS`）会**列出你账号下的全部知识库并跨库检索**文档（不再用检索词去匹配知识库名称，
那样文档关键词往往匹配不到库名而返回空）。若想固定只检索某些知识库，可设置：

```
IMA_KB_IDS=kb_id_1,kb_id_2
```

或写入 `~/.config/ima/kb_ids`（逗号或换行分隔）。跨数十个知识库检索时采用串行 + 限流退避
（账户级限流 `code 200001`），以保证不丢结果。

---

## 安装与运行

```bash
# 1. 克隆并进入项目
git clone https://github.com/zoujiahuibin/SuperSearch.git
cd SuperSearch

# 2. 创建虚拟环境并安装依赖（Python 3.9+）
python -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt

# 3. 运行（Windows 下；macOS/Linux 用 venv/bin/python main.py）
venv/Scripts/python.exe main.py
```

### 使用方法

1. **前置就绪**：确认 Everything 已启用 HTTP 服务器（见「前置条件」），ima 凭证已配置（见「前置条件 / 设置」）。
2. **检索**：在顶部输入框键入关键词，回车或点「检索」，即同时搜索本地文件与 ima 知识库。
3. **选择来源**：用「本地」「ima 知识库」两个勾选框控制是否检索对应来源。勾选「本地」后，下方出现**盘符**勾选区（C盘 / D盘 / E盘…，自动探测本机已挂载盘符），默认全选即检索全部盘符；取消某些盘符（如只勾 C盘、E盘）即只在这几个盘符内检索本地文件。盘符过滤通过 Everything 的 `path:` 语法实现（多盘符用 OR 组合）。
4. **排序**：「排序」下拉框切换 名称/路径/大小/修改时间 × 升/降，与 Everything GUI 排序列一致（服务端排序，名称是自然序）。
5. **筛选**：结果区下方「筛选」下拉可只看 全部 / 本地 / ima。
6. **打开**：**双击**本地结果会用系统默认程序打开（文件则打开文件、否则打开所在文件夹）；**双击 ima 结果**会取其真实直链，**优先用 ima 电脑端打开**（未安装电脑端则用默认浏览器：PDF / 图片直接预览，Office 等下载到本地）——需已配置 ima 凭证，取不到直链时回退为打开 ima 网页。也可在任一行上**右键**，选：
   - **打开**：与双击效果相同；
   - **打开路径**：本地结果打开其**所在文件夹**（即“路径”列所示的目录；文件夹结果则打开该文件夹本身）；ima 结果**优先用 ima 电脑端打开该知识库**（未装电脑端则打开网页 `https://ima.qq.com/wikis?knowledgeBaseId=<数字ID>`）——程序会自动经 `get_knowledge_list` 根目录解析出网页端数字 ID（OpenAPI 只给 base64 kb_id，无法直接用于网页），并按库缓存；解析失败则回退打开 ima 网页。

> ima 电脑端深链（经 REA 客户端逆向确认）：安装 ima.copilot 客户端后注册 `imacopilot://` 协议。**打开路径（知识库）**复刻客户端原生 `TQ` 桥：`imacopilot://open?url=chrome-extension://nkohmbngmopdajidckglcoehlaeepeoi/index.html?knowledgeBaseId=<数字ID>`；**打开（文档）**复用同一 `open?url=` 桥，并在内层地址显式带上 `action=autoOpenMedia`：`imacopilot://open?url=chrome-extension://nkohmbngmopdajidckglcoehlaeepeoi/index.html?action=autoOpenMedia&knowledgeBaseId=<数字ID>&mediaId=<媒体ID>`。关键：客户端「知识库页」(扩展 `nkohmbngmopdajidckglcoehlaeepeoi` 的 `index-Bdu-6Zli.js`) 仅在 `action===autoOpenMedia` 且 `knowledgeBaseId` 与 `mediaId` 同时存在时才会真正打开该篇文档（`k()` 函数：`if(!(wr()===it.AutoOpenMedia&&!!H&&!!y))return`）；`knowledgeDetail` 协议不会注入该 action，故文档必须用 `open?url=` 桥显式携带 `action=autoOpenMedia`。数字 ID 由 `get_knowledge_list` 根目录 `folder_id` 解析（客户端本地缓存证实其内部用数字 knowledgeBaseId，而非 OpenAPI 的 base64 kb_id）。深链经 `os.startfile` 调起并保留大小写；未安装客户端时自动回退默认浏览器（对分发到其它机器也稳妥）。
7. **查看详情**：单击任一行，底部显示路径 / 大小 / 时间，或 ima 高亮摘要。
8. **设置**（菜单栏「设置」）：
   - **文件格式排序…**：自定义扩展名优先级，让结果按你设定的格式顺序排列。
   - **ima 凭证…**：填入 Client ID / API Key（写入 `~/.config/ima`，即时生效）。
   - **ima 提速（并发）…**：调节并发上限 / 每秒请求数 / 线程数，加快 ima 跨库检索。
9. **进度**：底部状态栏在检索时显示百分比进度条，完成显示"共 N 条结果"。
10. **表格**：结果表格每列宽可拖动调整，表头还可拖动换列序。
11. **系统托盘**：关闭主窗口不会退出程序，而是**最小化到 Windows 右下角系统托盘**（状态栏提示"已最小化到系统托盘，右键托盘图标可退出"）。**双击托盘图标**或右键菜单「显示主窗口」可恢复窗口；右键菜单「退出 SuperSearch」才真正退出程序（退出时会一并关掉内置 Everything 便携版）。
12. **Everything 后台静默运行**：内置 Everything 以 `-startup` 方式在后台提供 HTTP 检索服务，**不显示其搜索窗口、也无托盘图标**；NTFS 索引经由安装器装好的 Everything 服务完成，因此**不会弹出“需要管理员权限索引 NTFS”对话框、也不触发 UAC**。
13. **百度网盘**：勾选「百度网盘」后，首次使用需授权（菜单「设置 → 百度网盘授权…」）：填入百度开放平台应用的 Client ID / Secret → 在浏览器中打开授权页并输入用户码 → 自动完成授权（设备码 5 分钟内有效）。之后检索即包含网盘文件（文件名关键词、全盘）；**双击结果在浏览器打开网盘并定位到所在目录**（不产生分享链接）。云端单次响应可能上千条，界面默认只展示前 100 条；「清除授权」可随时移除本机令牌（应用凭证保留）。

### 运行测试

```bash
venv/Scripts/python.exe tests/smoke_test.py   # 100 项纯逻辑冒烟测试（含系统托盘 + Everything 服务逻辑 + 索引就绪等待 + 退出等待落盘 + 百度网盘授权/检索/映射）
# 可选：无界面验证 GUI 可构造
QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe tests/verify_gui.py
```

---

## 配置项（环境变量）

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `EVERYTHING_HTTP_HOST` | Everything HTTP 主机 | `127.0.0.1` |
| `EVERYTHING_HTTP_PORT` | Everything HTTP 端口 | `80` |
| `EVERYTHING_HTTP_TIMEOUT` | 请求超时（秒） | `5.0` |
| `EVERYTHING_SORT` | 本地排序字段：`name`/`path`/`date_modified`/`size` | `name` |
| `EVERYTHING_SORT_ASC` | 是否升序（`1`/`0`） | `1` |
| `IMA_OPENAPI_CLIENTID` | ima Client ID | 读配置文件 |
| `IMA_OPENAPI_APIKEY` | ima API Key | 读配置文件 |
| `IMA_KB_IDS` | 显式知识库 ID 列表（限定检索范围，减少请求数） | 空（自动发现全部） |
| `IMA_MAX_WORKERS` | ima 跨库检索的最大并发线程数 | `4` |
| `IMA_REQ_PER_SEC` | ima 全局请求速率上限（令牌桶，请求/秒） | `4.0` |
| `IMA_BURST` | ima 令牌桶突发容量（最大瞬时并发） | `2` |

> **并发与限速**：ima 为账户级限流（`code 200001` 请求频率超限）且另有**每日调取次数上限**（提示"资料获取次数已达上限，请明天再尝试"）。
> 工具用**令牌桶 + 受控线程池**在"重叠网络延迟提速"与"不超阈值"之间折中：默认 `IMA_BURST=2`（最多 2 个请求并发）、`IMA_REQ_PER_SEC=4`（约 4 请求/秒），并对 `200001` 做指数退避重试兜底。
> 若你的账号更宽松，可临时调大 `IMA_BURST`（如 3）提速；若频繁出现 `200001`，调小 `IMA_BURST`。**注意：并发不会多用额度**——无论串行还是并发，检索 N 个知识库都是 N 次请求，并发只是把 N 次请求的等待时间重叠起来，从而更快出结果。

---

## 测试

```bash
venv/Scripts/python.exe tests/smoke_test.py
```

覆盖配置加载、FILETIME 转换、Everything JSON 解析、ima 列表/检索（mock）。

---

## 项目结构

```
main.py                 入口
core/config.py          配置（ima 凭证 / Everything 端口 / 用户设置 Settings）
core/baidu_pan.py       百度网盘开放平台客户端（设备码授权 + 关键词检索 + 令牌管理）
core/models.py          统一结果模型 SearchResult（含 source 标签）
core/format_sort.py     按文件格式优先级在客户端重排
search/local_search.py  Everything HTTP 检索
search/ima_search.py    ima OpenAPI 检索（受控并发 + 令牌桶限速 + 游标分页）
search/baidu_search.py  百度网盘结果映射（SearchResult + 展示截断）
ui/main_window.py       PySide6 主窗口（后台线程检索，合并列表）
tests/smoke_test.py     纯逻辑冒烟测试
CONTEXT.md              领域词汇表（glossary）
docs/adr/               架构决策记录（ADR）
SuperSearch.iss         Inno Setup 安装脚本（用于生成 setup.exe，见下）
```

---

## 打包为 Windows 可执行文件（.exe）

本项目用 **PyInstaller** 打成单文件、无控制台的 Windows 程序（已内置 PySide6 运行库，
目标机器无需安装 Python / PySide6，双击即用；原项目源码不受影响，仅输出到 `dist/`）。

**v1.2.1 起后台静默 + Everything 服务**：内置实例改用 `-startup` 后台启动（不显示搜索窗口），
ini 写入 `run_as_admin=0` / `run_in_background=1` / `show_tray_icon=0` / `auto_include_fixed_volumes=1`，
实现完全无界面、且不触发 UAC / 管理员对话框。NTFS 索引改由 **Everything 服务** 提供：安装器
（`SuperSearch.iss`，`PrivilegesRequired=admin`）在装包时以提权方式执行
`SuperSearch.exe --install-everything-service`（内部 `everything.exe -install-service`，服务以 SYSTEM
后台运行、无界面），卸载时经 `--uninstall-everything-service` 移除；服务缺失时应用也会尝试补装一次
（标准用户触发一次性 UAC）。`core/ev_bundler.py` 相应新增 `service_installed/install_service/uninstall_service`。

**v1.3.0 新增百度网盘检索源**：新增 `core/baidu_pan.py`（OAuth 2.0 **设备码授权**：
无回调地址，浏览器输 user_code 确认，轮询取令牌；access_token 30 天有效、
剩余不足 5 天自动用 refresh_token 续期）与 `search/baidu_search.py`（文件名
**关键词检索**，全盘覆盖，支持目录/文件类型过滤；云端响应上千条时按展示上限
截断，默认 100 条）。应用凭证与用户令牌只存本机
`%APPDATA%\SuperSearch\baidu_netdisk.json`（`.gitignore` 已忽略；卸载时经
安装脚本清理）。双击网盘结果执行**结果目录定位**（打开网页版网盘并定位到所在
目录，不产生分享链接）。语义检索（`/xpan/unisearch`，文档内容/OCR/ASR）因
非 VIP 账号实测不可用，留作后续增量——决策见 `docs/adr/`。

**v1.2.2 修复 HTTP 服务器启用 + 索引就绪等待**：修正 ini 键名（`http_server=1` → 合法的
`http_server_enabled=1`，并补上 `[Everything]` 节头——v1.2.1 升级用户本地检索不可用的根因）；
启动旧实例（内存中 HTTP 关闭）时先退出再启动，防"激活旧实例不重载配置"与退出存盘期间的
"吸收"竞态（最多 5 次重试）。检索时新增**索引就绪等待**：HTTP 就绪 ≠ 索引就绪（冷启动重建
索引实测约 80s，期间直接检索会静默返回空结果），故就绪后继续等待 `totalResults>0`（最多 120s，
状态栏显示进度），未就绪则提示"正在建立本地文件索引"而非返回空结果。应用退出时退出内置实例，
使其保存数据库——下次冷启动仅需数秒（实测新鲜库索引 4.5s 就绪 vs 陈旧库约 80s 全量重建）；
且退出/卸载前会等待 `.db.tmp` 落盘完成（出现后消失，孤儿 `.tmp` 不阻塞），避免存盘竞态拖慢
下次 HTTP 绑定、也避免卸载时删到半截数据库。

**v1.2.0 起内置 Everything 便携版**：`vendor/Everything/`（官方 zip，未改动）随包打进
`dist/SuperSearch.exe`，运行 / 检索时由 `core/ev_bundler.py` 自动探测并拉起内置 Everything
（独立实例名 `SuperSearch`，其配置写入 `http_server_enabled=1` 即**自动开启 HTTP 服务**，
端口优先 80、被占用则退 8180），无需用户在目标机单独安装 Everything 即可使用本地检索。
ima 检索仅需网络 + 已配置凭证。
> 注：v1.2.0/v1.2.1 曾把该键误写为 `http_server=1`（非法键，被 Everything 静默忽略，
> 导致 HTTP 服务器从未真正启用），且 ini 缺少 `[Everything]` 节头；v1.2.2 已按官方
> INI 文档修正为 `http_server_enabled=1` + `[Everything]` 节头。

**开箱即用 / 隐私默认**：首次安装后 ima 凭证为空（程序不内置任何 Client ID / API Key，凭证
仅写入用户本机 `~/.config/ima`）；设置文件改存用户可写的 `%APPDATA%\SuperSearch\settings.json`
（打包后），保证偏好跨运行持久化且首装即为空态。程序启动后常驻 **Windows 系统托盘**
（关闭窗口最小化到托盘而非退出），详见「使用方法 / 系统托盘」。

### 已产出
- `dist/SuperSearch.exe` —— 单文件可执行程序（约 50 MB，已内嵌 Everything）。直接双击运行。
- `dist/SuperSearch-Setup.exe` —— Inno Setup 安装包（含应用图标、默认勾选的桌面快捷方式、
  可选安装路径）。v1.2.1 起安装器**需要管理员权限**（用于安装 Everything 服务，使本地检索不再弹
  管理员对话框）；安装时会复制 exe 并以提权方式装好 Everything 服务，卸载时一并移除。

### 自行重新打包
```bash
# 在项目 venv 中（已含 PySide6 / requests / pyinstaller）执行：
venv/Scripts/python.exe -m PyInstaller --noconfirm --clean build/SuperSearch.spec
#  → dist/SuperSearch.exe（已带应用图标、版本信息、内嵌 Everything）
```
> 说明：`build/SuperSearch.spec` 已固定图标（`assets/SuperSearch.ico`）、版本文件
> （`version_info.txt`，现为 1.3.0）、并把 `vendor/Everything` 作为 data 文件打进包内；
> 不需要手动加 `--add-data` 或 `--icon`。PyInstaller 的 PySide6 钩子会自动带上 Qt 平台插件。

### 生成"可安装"的 setup.exe
单文件 exe 已是"便携 / 直接使用"版。若想要带开始菜单 / 桌面快捷方式、可选安装路径的
安装程序，用 **Inno Setup** 编译随附的 `SuperSearch.iss`：

1. 安装 Inno Setup 6（https://jrsoftware.org/isinfo.php）。
2. 先按上面步骤生成 `dist\SuperSearch.exe`。
3. 编译脚本（ISCC.exe 路径以实际安装位置为准）：
   ```
   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" SuperSearch.iss
   ```
   得到 `dist\SuperSearch-Setup.exe`。安装向导中可**选择安装位置**，并默认**创建桌面快捷方式**
   （可在向导中取消）；卸载时会清理内置 Everything 的运行数据（`%LOCALAPPDATA%\SuperSearch`）。

> 注：本仓库的 `.gitignore` 已忽略 `dist/`、`build/` 与 `settings.json`，
> 因此打包产物与个人设置不会进入版本库；`SuperSearch.iss`、`build/SuperSearch.spec`、
> `version_info.txt`、`requirements.txt`、`assets/`、`vendor/` 等源文件保留。

---

## 说明

- 本地检索依赖 Everything 索引实时性；ima 检索内容来自已加入知识库的文档与笔记。
- ima 高亮摘要（`highlight_content`）原样展示，双击本地结果会用系统默认程序打开。
- 该工具与 REA（Reverse Engineer Anything）无关；REA 是另一套用于逆向工程的 MCP/CLI，
  二者定位不同。

---

## 设置：文件格式优先级排序

菜单栏「设置 → 文件格式排序」可打开对话框：

- **启用按文件格式优先级排序**：勾选后，搜索结果以你定义的**格式顺序**为**主键**排列
  （如先 `pdf`、再 `docx`、再 `xlsx`…），未列入的格式排在最后。
- **同名结果归组（本地 / ima 混排相邻）**：勾选后，把本地与 ima 中**同名的结果排在一起**，
  不再分成"本地一堆 / ima 一堆"——ima 副本会贴到同名本地项旁，ima 独有项跟在后面（默认开启）。
- **按格式混排（本地 + ima）**：ima 文档与本地文件一样按其扩展名参与排序，而**不再固定排在最后**——
  同一格式的本地文件与 ima 文档会相邻显示（本地在前），实现按格式混排。
- **文件夹置顶**：当格式列表**未**包含 `folder` 时生效——勾选则文件夹排在最前；取消则文件夹按"未列入格式"排在最后。
- **folder 作为格式**：点「＋文件夹(folder)」可把文件夹作为一种格式加入列表，用「上移 / 下移」把它放到任意位置，
  从而让文件夹与各文件格式**按位置混排**（加入后以其所列位置为准，覆盖"文件夹置顶"）。
- **格式列表**：每行一个扩展名（不含点，如 `pdf`）或 `folder`，可用「添加 / ＋文件夹 / 删除 / 上移 / 下移」调整顺序。

说明：Everything 服务端仅支持 `name/path/size/date_modified` 等固定字段排序，不支持
"自定义扩展名优先级"，因此"格式优先级"在客户端完成，并以结果列表的既有顺序作为**次级键**——
同格式内保持原有顺序（本地保持 Everything 排序、ima 保持接口返回序；本地名称仍是自然序，`file2` 在 `file10` 前）。
设置保存在项目根目录 `settings.json`，即时生效。

---

## 设置：ima 提速（并发）

菜单栏「设置 → ima 提速（并发）」可打开对话框，直接调节三个旋钮（无需改代码 / 环境变量），
**下一次检索即时生效**：

- **并发上限（瞬时）**：同一时刻最多多少个请求并发在飞（令牌桶突发容量）。调大提速，但更可能触发 ima 频率限流（`200001`）。默认 `2`。
- **每秒请求数上限**：全局请求速率（请求/秒，令牌桶速率）。与并发上限共同决定实际速度。默认 `4.0`。
- **并发线程数**：工作线程数，建议 ≥ 并发上限；多余的线程会在令牌桶处排队。默认 `4`。

> 提速的本质是**重叠网络往返延迟**，而不是多用额度：无论串行还是并发，检索 N 个知识库都是 N 次
> 请求，并发只是让等待时间重叠。若想进一步提速，把"并发上限 / 每秒请求数"调大即可（前提是你的
> ima 账号不被限流）；若频繁出现 `200001`，则调小。注意：**多进程不会更快**——本工具瓶颈是网络
> I/O，Python 多线程在 `requests` 网络调用时已释放 GIL，线程池已能充分重叠等待。ima 另有**每日
> 调取次数配额**，与并发/速率无关，无法靠调参突破。

---

## 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 状态栏提示 Everything HTTP 不可达 | 在 Everything → 工具 → 选项 → HTTP 服务器 勾选「启用 HTTP 服务器」并确认端口（默认 80），点击确定即时生效。 |
| ima 检索报错 `连接 ima 失败：401` 或 `skill auth failed (200002)` | `~/.config/ima/client_id` / `api_key` 无效或已过期。请在 ima 开放平台重新申请 OpenAPI 的 Client ID / API Key，覆盖写入这两个文件（或设置 `IMA_OPENAPI_CLIENTID` / `IMA_OPENAPI_APIKEY` 环境变量），无需改代码。 |
| ima 检索返回 0 条且无报错 | 默认会列出账号下**全部**知识库并跨库检索文档；若仍为空，可能是该关键词在所有知识库文档中确实无匹配，或凭证过期导致列举知识库失败（见上一条）。可在界面「设置 → ima 凭证…」复核凭证，或设置 `IMA_KB_IDS` 限定范围。 |
| ima 检索部分知识库提示 `请求频率超限 (code 200001)` | ima 为账户级限流。工具已对限流做指数退避重试（最多 4 次，0.5/1/2/4 秒）并在库间限速 0.15 秒；绝大多数情况下重试后即可成功。若极偶发仍有个别库失败，稍后重试即可。 |
| ima 检索部分知识库提示 `资料获取次数已达上限，请明天再尝试` | 这是 ima 的**每日调取次数配额**（与 `200001` 限流不同，是日级上限），并非代码 bug，也无法靠重试绕过——重试反而更费额度。账号当天检索次数已用尽。**解决**：缩小检索范围（设置 `IMA_KB_IDS` 只检常用库，而非每次都跨全部几十个库），或等次日额度重置。 |
| 本地结果排序与 Everything 不一致 | Everything 默认按「名称升序」；若你的 Everything GUI 改过排序列/方向，请在界面「排序」下拉框选对应的 名称/路径/大小/修改时间 × 升/降。排序由 Everything 服务端完成（名称是自然序，file2 在 file10 前），与 Everything 本身保持一致。 |
| 百度网盘提示"未授权"或勾选后报警 | 设置 → 百度网盘授权…：先在百度网盘开放平台（pan.baidu.com/union）自建应用取得 Client ID / Secret，再完成设备码授权（浏览器输用户码）。未授权时该源不参与检索，不影响本地 / ima。 |
| 百度网盘检索失败（错误码 -6） | 授权失效（令牌被撤或异常）：重新走一次「设置 → 百度网盘授权…」即可。 |
| 百度网盘只返回前 100 条 | 云端单次响应可能上千条，界面按展示上限截断（默认 100）。可改 settings.json 的 `baidu_limit`（1-500）调整。 |
