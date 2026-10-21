# ADR-0001: 百度网盘源采用 OAuth 2.0 设备码授权，用户令牌存本机 JSON 文件

- 状态：已接受（2026-10-22）
- 影响范围：core/baidu_pan.py、ui/main_window.py、.gitignore、SuperSearch.iss

## 背景

百度网盘开放平台要求每个请求携带用户级 access_token（30 天有效期），
获取令牌必须经用户授权。桌面应用没有公网回调地址。

## 备选方案

| 方案 | 优势 | 劣势 |
| --- | --- | --- |
| A. 设备码授权（device code） | 官方为无回调场景设计；用户只需浏览器输码；轮询即可，无需起本地 HTTP 服务 | 需用户手动完成一次浏览器确认 |
| B. 授权码模式 + 本地回环 | 授权体验更"自动" | 需监听 localhost 端口、处理 redirect_uri 注册，桌面端实现更重 |
| C. 用户手动粘贴 access_token | 实现最简 | 30 天过期后需反复手动获取，体验不可接受 |

## 决策

选 **A**：设备码授权为主路径，不提供手动粘贴入口（YAGNI）。

## 令牌存储

存 `%APPDATA%\SuperSearch\baidu_netdisk.json`（与 settings.json 同目录，
打包后路径；开发环境存仓库根并已被 .gitignore 忽略）。内容含应用凭证与
用户令牌。理由：与现有设置体系一致、实现简单；DPAPI 加密留作后续加固项，
不阻塞首版。卸载时经安装脚本清理该目录。

# 或回退

若未来接入 Windows 凭据管理器（DPAPI），只需替换存取两个函数，
上层调用不变（token 结构稳定）。
