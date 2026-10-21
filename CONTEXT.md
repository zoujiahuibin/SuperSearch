# SuperSearch 领域词汇表（Glossary）

> 本文件只收录**领域术语**及其边界，不含任何实现细节。
> 实现决策见 `docs/adr/`；当术语被争论时，以本文件为准。

## 检索源（Search Source）

SuperSearch 的一次检索可并行触及多个检索源，结果合并为统一列表，按来源标签区分。

### 本地源（Local Source）
用户本机文件，经 Everything HTTP 服务检索。结果可双击直接用本机程序打开。

### ima 源（Ima Source）
用户账号下的 ima 知识库文档，经 ima 官方 OpenAPI 检索。结果双击时取文档直链，
优先经 ima 电脑端打开，未安装则回退浏览器。

### 百度网盘源（Baidu Netdisk Source）
用户百度账号网盘中的文件，经百度网盘开放平台检索。文件不在本机磁盘，
双击行为是**结果目录定位**（见下），而非打开本地文件。

## 凭证与授权（Credentials & Authorization）

### 应用凭证（App Credentials）
调用方（本软件）在百度网盘开放平台注册应用后获得的一对标识：client_id 与
client_secret。它标识"哪个应用在请求"，不属于用户个人数据，但不应公开传播。

### 用户令牌（User Token）
用户授权后代表"哪个用户许可该应用做什么"的凭据：access_token（短期）与
refresh_token（长期）。用户令牌是敏感数据，只存用户本机。

### 设备码授权（Device Authorization）
面向无回调地址场景（桌面应用）的 OAuth 2.0 授权方式：应用展示 user_code 与
verification_url，用户在浏览器完成确认，应用轮询直至拿到用户令牌。
授权是有状态的交互过程，可能失败（超时、被拒绝、用户取消）。

### 授权状态（Authorization State）
百度网盘源的三态：**未配置应用凭证**（连 client_id 都没有）/
**已配置未授权**（有应用凭证但无用户令牌）/**已授权**（持有有效用户令牌）。
三态在界面上的提示与可用操作不同。

## 检索能力（Search Capability）

### 关键词检索（Keyword Search）
以文件名关键词匹配网盘文件，全盘覆盖，支持限定目录与文件类型。
响应可能上千条，需在展示侧截断。

### 语义检索（Semantic Search）
以自然语言匹配文件名、文档内容、图片 OCR 等。能力依赖账号等级，
非 VIP 账号可能不可用（返回空）。当前版本不提供。

## 结果目录定位（Result Directory Targeting）
百度网盘结果双击后的行为：打开网页版网盘并定位到该文件所在目录
（path 参数），用户在已登录的浏览器中查看。不产生分享链接，
不改变文件的分享状态。

## 展示截断（Display Truncating）
云端检索源（百度网盘）单次响应可能远大于界面可承载量；
以"最多展示 N 条"的约定在映射层截断，N 是产品参数而非 API 限制。
