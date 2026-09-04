# astrbot_plugin_bili_up

AstrBot 插件：在 QQ 内**扫码绑定 B站账号**，并通过**交互式对话**投稿视频到 Bilibili。

配套 [NapCat](https://napneko.github.io/)（OneBot v11）+ [AstrBot](https://astrbot.app/)（v4.x，Star 插件体系）使用。

## 功能流程

```
/biliup绑定 ────▶ 生成B站登录二维码 ─▶ App扫码并确认 ─▶ Cookie 持久化(按会话隔离, 存于AstrBot库)
/biliup上传视频 ─▶ 发送视频文件 ─▶ 标题 → 原创/转载 → (转载来源) → 分区 → 简介 → 标签
              ─▶ 封面(可选) → 动态(可选) ─▶ 信息摘要 ─▶ 回复【投稿】─▶ 分片上传+提交 ─▶ 回传稿件链接
```

## 命令（前缀 `/biliup`）

| 命令 | 说明 | 旧版别名 |
|---|---|---|
| `/biliup绑定` | 生成二维码扫码登录（3 分钟有效），成功后自动保存 Cookie | `/b站绑定` |
| `/biliup状态` | 校验当前绑定账号是否有效（昵称 / mid） | `/b站状态` |
| `/biliup解绑` | 删除本地 Cookie | `/b站解绑` |
| `/biliup分区` | 查看常用分区(tid)对照表 | `/b站分区` |
| `/biliup上传视频` | 投稿主流程；任意步骤回复【取消】终止，回复【投稿】确认提交 | `/上传视频` |

> 也支持 `/biliup 上传视频`（前缀与命令间带空格）的写法。
> 🔒 **会话隔离**：B站凭证按会话独立存储——每个群、每个私聊各自绑定各自的账号。A 群投稿只用 A 群的账号，B 群与私聊互不影响、互不通用。
> ⚠️ **建议私聊使用**。群聊时整个投稿流程期间，该群的机器人消息会被"会话等待器"接管，他人发言不会干扰，但也请不要让其他功能在流程中被打断。

## 安装

1. 将本目录放入 AstrBot 的 `data/plugins/`（目录名 `astrbot_plugin_bili_up`）；
2. 在 AstrBot WebUI 中启用插件，或通过「插件市场 → 从仓库安装」填入本仓库地址：
   `https://github.com/Lillna-Rina/astrbot_plugin_bili_up`
3. AstrBot 会自动安装 `requirements.txt` 依赖；若失败，请在 AstrBot 的 Python 环境手动执行：
   ```bash
   pip install -r requirements.txt
   ```
4. 重启 AstrBot，向机器人私聊发送 `/biliup绑定` 开始使用。

## 技术说明（与 API 核对结论）

插件面向 **AstrBot ≥ 4.0**，代码与 `astrbot 4.27.x` 源码逐项核对：

- 插件类直接继承 `star.Star`（**不再使用已废弃的 `@register` 装饰器**），元信息来自 `metadata.yaml`；
- 多轮交互使用 AstrBot 官方 **一次性会话机制** `astrbot.api.util.session_waiter`（由内置 Main 星在收到新消息时触发），不需要全局状态机；
- 视频/文件消息段使用组件内置能力落地：
  - `File` 段 → `await comp.get_file()`（NapCat 群文件会自动经 `get_group_file_url` 换取临时链接并下载）
  - `Video` 段 → `await comp.convert_to_file_path()`
- Cookie 通过 `Star` 自带的 `put_kv_data/get_kv_data` 持久化到 AstrBot 数据库（不落明文文件），并按 **会话（unified_msg_origin：群号/私聊对端）隔离**存储，各会话绑定互不可见；
- 上传协议（preupload → upos 分片 PUT → 合并 → 封面 → submit）**内置实现**于 `bili_upload.py`，与 biliup 1.2.x 现行协议一致，避免引入 biliup 及其重型依赖（yt-dlp/streamlink 等）。

## 依赖

```
qrcode, Pillow, requests, aiohttp
```

## 已知风险与说明

- **B站风控**：Web 分片上传并非官方开放能力，高频投稿可能触发验证码/风控（投稿返回错误或需要滑块），请合理使用；失败时请稍后重试；
- **Cookie 安全**：SESSDATA 属于完整登录态，插件仅保存在 AstrBot 数据库并按会话（每个群/私聊）隔离，请勿将数据库/日志外泄；
- **上传进度**：每完成 10% 推送一次进度到会话（按分片估算）；
- 封面将自动裁剪为 16:10 后上传。

## 项目结构

```
main.py              Star 入口：命令 + session_waiter 交互流程
bili_login.py        B站扫码登录（二维码生成/轮询/校验，纯网络）
bili_upload.py       B站 Web 投稿协议实现（同步，分片上传+submit）
metadata.yaml        AstrBot 插件元信息
_conf_schema.json    WebUI 可配置项
requirements.txt     依赖
```

## License

MIT
