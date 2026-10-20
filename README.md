# SuperSearch：统一检索（本地文件 + ima 知识库）

一个独立的桌面 GUI（PySide6），同时检索：

- **本地文件**：通过 Everything HTTP Server（零额外下载，复用已安装的 Everything 索引）。
- **ima 知识库文档/笔记**：通过 ima 官方 OpenAPI。

结果合并在同一张列表里，用**来源标签**区分（蓝色=本地，绿色=ima），支持按来源筛选、双击打开本地文件、查看 ima 高亮摘要。

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
| Everything | 1.4+（可选） | 仅本地检索需要；启用其 HTTP Server（默认 `127.0.0.1:80`）。不启用则本地检索不可用，ima 检索不受影响。 |
| 网络 | 需能访问 `ima.qq.com` | 仅 ima 检索需要；本地检索纯内网。 |
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
3. **选择来源**：用「本地(Everything)」「ima 知识库」两个勾选框控制是否检索对应来源。
4. **排序**：「排序」下拉框切换 名称/路径/大小/修改时间 × 升/降，与 Everything GUI 排序列一致（服务端排序，名称是自然序）。
5. **筛选**：结果区下方「筛选」下拉可只看 全部 / 本地 / ima。
6. **打开**：**双击**本地结果会用系统默认程序打开（文件则打开文件、否则打开所在文件夹）；**双击 ima 结果**会取其真实直链，**优先用 ima 电脑端打开**（未安装电脑端则用默认浏览器：PDF / 图片直接预览，Office 等下载到本地）——需已配置 ima 凭证，取不到直链时回退为打开 ima 网页。也可在任一行上**右键**，选：
   - **打开**：与双击效果相同；
   - **打开路径**：本地结果打开其**所在文件夹**（即“路径”列所示的目录；文件夹结果则打开该文件夹本身）；ima 结果**优先用 ima 电脑端打开该知识库**（未装电脑端则打开网页 `https://ima.qq.com/wikis?knowledgeBaseId=<数字ID>`）——程序会自动经 `get_knowledge_list` 根目录解析出网页端数字 ID（OpenAPI 只给 base64 kb_id，无法直接用于网页），并按库缓存；解析失败则回退打开 ima 网页。

> ima 电脑端深链：安装 ima.copilot 客户端后会注册 `imacopilot://` 协议，程序用 `imacopilot://knowledgeDetail?knowledgeBaseId=<数字ID>[&mediaId=<媒体ID>]` **精准定位**并调起客户端打开对应知识库/文档（经 `os.startfile` 保留大小写）；未安装时自动回退默认浏览器（对分发到其它机器也稳妥）。
7. **查看详情**：单击任一行，底部显示路径 / 大小 / 时间，或 ima 高亮摘要。
8. **设置**（菜单栏「设置」）：
   - **文件格式排序…**：自定义扩展名优先级，让结果按你设定的格式顺序排列。
   - **ima 凭证…**：填入 Client ID / API Key（写入 `~/.config/ima`，即时生效）。
   - **ima 提速（并发）…**：调节并发上限 / 每秒请求数 / 线程数，加快 ima 跨库检索。
9. **进度**：底部状态栏在检索时显示百分比进度条，完成显示"共 N 条结果"。
10. **表格**：结果表格每列宽可拖动调整，表头还可拖动换列序。

### 运行测试

```bash
venv/Scripts/python.exe tests/smoke_test.py   # 20 项纯逻辑冒烟测试
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
core/models.py          统一结果模型 SearchResult（含 source 标签）
core/format_sort.py     按文件格式优先级在客户端重排
search/local_search.py  Everything HTTP 检索
search/ima_search.py    ima OpenAPI 检索（受控并发 + 令牌桶限速 + 游标分页）
ui/main_window.py       PySide6 主窗口（后台线程检索，合并列表）
tests/smoke_test.py     纯逻辑冒烟测试
SuperSearch.iss         Inno Setup 安装脚本（用于生成 setup.exe，见下）
```

---

## 打包为 Windows 可执行文件（.exe）

本项目用 **PyInstaller** 打成单文件、无控制台的 Windows 程序（已内置 PySide6 运行库，
目标机器无需安装 Python / PySide6，双击即用；原项目源码不受影响，仅输出到 `dist/`）。

### 已产出
- `dist/SuperSearch.exe` —— 单文件可执行程序（约 47 MB）。直接双击运行即可使用；
  也可拷贝到任意 Windows x64 机器运行（仍需目标机安装并启用 Everything 才能本地检索，
  ima 检索需网络 + 已配置凭证）。

### 自行重新打包
```bash
# 在项目 venv 中（已含 PySide6 / requests）安装 PyInstaller
venv/Scripts/python.exe -m pip install pyinstaller
# 打单文件、无控制台窗口；输出到 dist/，中间产物在 build/（均已被 .gitignore 忽略）
venv/Scripts/python.exe -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name SuperSearch --distpath dist --workpath build main.py
```
> 说明：本应用文件图标走 Windows shell（`QFileIconProvider`），无自带资源文件，
> 故打包无需额外 `--add-data`；PyInstaller 的 PySide6 钩子会自动带上 Qt 平台插件
> （`qwindows`/`qoffscreen` 等）。

### 生成"可安装"的 setup.exe（可选）
单文件 exe 已是"便携 / 直接使用"版。若想要带开始菜单/桌面快捷方式的安装程序，
用 **Inno Setup** 编译随附的 `SuperSearch.iss`：

1. 安装 Inno Setup 6（https://jrsoftware.org/isinfo.php）。
2. 先按上面步骤生成 `dist\SuperSearch.exe`。
3. 编译脚本：
   ```
   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" SuperSearch.iss
   ```
   得到 `dist\SuperSearch-Setup.exe`，双击即可安装使用。

> 注：本仓库的 `.gitignore` 已忽略 `dist/`、`build/` 与 `settings.json`，
> 因此打包产物与个人设置不会进入版本库；`SuperSearch.iss`、`requirements.txt` 等源文件保留。

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
