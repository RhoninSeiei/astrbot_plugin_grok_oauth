# AstrBot Grok OAuth

通过 Grok 账号的 OAuth 授权，在 AstrBot 中使用对话、网页搜索、图片理解、图片及视频生成与编辑，并查询订阅额度。

**版本 0.8.2 · Python 3.12+**

[配图配置指南](docs/USER_GUIDE.md) · [常见问题](docs/FAQ.md) · [路线图](ROADMAP.md) · [更新记录](CHANGELOG.md) · [开发接口](docs/API.md)

## 能力

| 功能 | 当前支持情况 |
| --- | --- |
| 对话 | 文本、流式回复、推理参数、函数工具、多轮状态保留 |
| 图片理解 | Grok 4.7 等支持图片输入的模型 |
| 网页搜索 | `grok_web_search`，由模型按需调用并返回来源 |
| 图片生成与编辑 | Imagine 工具，发送图片附件，支持在原会话重发 |
| OAuth 额度 | 授权页、管理员命令、两个按需工具，显示总量与消耗来源 |
| 模型管理 | AstrBot 原生提供商源与模型设置，多个模型共享一次账号授权 |
| 视频生成与编辑 | 文生视频、图生视频、外部视频导入与编辑、公共任务与bytes接口；由调用插件发送 |
| 音频、X 搜索 | 当前未接入，见[路线图](ROADMAP.md) |

**Grok 4.7 本身接受文本和图片、输出文本，不接受音频。** xAI 的转录、语音合成和实时语音是独立服务，公开 API 文档不代表订阅 OAuth 已获对应权限。[官方 Grok 4.7 文档](https://docs.x.ai/developers/grok-4-7)

## 快速开始

1. 在 AstrBot「插件」页面通过仓库地址安装：`https://github.com/RhoninSeiei/astrbot_plugin_grok_oauth`；也可上传对应版本的插件 ZIP。
2. 打开「插件页面 → Grok Oauth → 授权与设置」，点击「连接 Grok 账号」。复制认证码和官方链接，在浏览器完成授权，再返回查看连接状态。
3. 进入「模型提供商」，新增 **Grok Oauth** 提供商源，保存并获取模型，添加 `grok-4.7`。
4. 在模型设置中仅勾选文本、图像和工具调用能力，在目标会话或所用 Agent 的模型配置中选择该模型，然后发送一条消息验证。

完整的界面位置、填写示例和检查步骤见[配图配置指南](docs/USER_GUIDE.md)。模型添加完成不会自动切换所有会话。

![授权与设置页面，使用合成账号和示例额度](docs/images/04-usage.png)

图中为 AstrBot 4.28.1 的合成演示环境，认证码、账号状态和额度不代表真实账户。

## 安装前确认

- 需要可完成 Grok OAuth 授权、并具有目标模型使用权限的账号。订阅名称、模型列表或一次成功不能保证其他账号具有相同权限。
- 允许安装于 AstrBot `>=4.28.2,<5.0`；后续 4.x 版本尚未逐版验证。普通安装、业务回归与定制核心扩展的受测版本和范围见[测试说明](docs/TESTING.md)。
- 插件声明的依赖为 `httpx`、`Pillow`、`filelock`，由 AstrBot 安装流程处理；无需另外安装 OAuth 插件或代理服务。
- 网络需要能访问 xAI 认证与推理服务；额度查询另访问 Grok CLI 的固定额度服务。出站代理在本插件 `proxy` 中设置，见[数据边界](docs/SECURITY.md)。

插件为独立社区实现，未获得 xAI 官方背书。授权使用公开 Grok CLI 客户端参考配置，页面会显示实际客户端 ID 与配置名称；管理员确认后才开始设备授权。无需申请 API Key、填写客户端密钥或复制令牌文件。源编辑器的通用 API Key 字段留空；自有客户端设置与管理员私聊授权流程见[配置指南](docs/USER_GUIDE.md#2-连接-grok-账号)。

## 后台传输诊断

插件普通配置中的 `transport_diagnostics` 默认关闭。排查 Grok 推理传输故障时，管理员可开启并重载本插件；之后在 AstrBot 后台日志中按 `grok_oauth_transport_diag` 查找新增记录。排查结束后关闭并重载插件即可停止新增诊断记录。原生诊断仅通过 `astrbot.api.logger` 输出。

需要独立文件时，开启 `transport_debug_file` 并重载插件。文件位于 `data/plugin_data/astrbot_plugin_grok_oauth/debug/transport.jsonl`，权限为 `0600`，按 1 MiB 轮转并保留一个备份；写入采用有界异步队列。该开关与后台日志开关独立，均默认关闭。文件只保存本插件的脱敏诊断事件，不复制第三方原始日志；关闭插件时排空队列。

`httpx/httpcore` 自身日志由 AstrBot 管理，本插件不获取、修改或过滤第三方日志器；第三方输出可能包含请求 URL，不属于上述诊断字段白名单。

记录包含请求阶段、耗时、剩余超时预算、已收到的 HTTP 状态与安全请求编号、重试次数、错误类别，以及成功完成的 Provider、模型和响应编号。流式日志的 `stream_exhausted` 只表示 HTTP 正文读取到末尾，`scope_returned` 表示调用方提前结束读取；业务完成情况以另行记录的 `provider_response` 为准。本插件诊断日志不含令牌、正文、URL、查询参数、原始异常文本或图片内容。关闭开关不会删除历史日志，也不影响 AstrBot 核心和其他插件的日志设置。

## 常用命令

| 命令 | 用途 | 权限 |
| --- | --- | --- |
| `/grok_oauth_status` | 查看授权状态 | 管理员私聊 |
| `/grok_oauth_login` | 查看客户端说明，追加 `confirm` 发起授权 | 管理员私聊 |
| `/grok_oauth_cancel` | 取消正在进行的授权 | 管理员私聊 |
| `/grok_oauth_disconnect` | 解除本插件账号绑定 | 管理员私聊 |
| `/grok_oauth_test <provider_id>` | 测试指定模型聊天 | 管理员私聊 |
| `/grok_oauth_usage` | 查询额度总量 | 管理员私聊或白名单群 |
| `/grok_oauth_usage_breakdown` | 查询消耗来源 | 管理员私聊或白名单群 |
| `/grok_image_resend <operation_id>` | 使用原图片在原会话重发 | 遵循图片工具的会话与模型权限 |

普通用户可通过 Grok 模型询问额度；群聊需由管理员配置 `usage_group_allowlist`。直接命令不依赖聊天模型，且仍要求管理员身份。额度展示未知值、缓存和历史快照，不推算可用 token 或图片数量。

## 文档与贡献

- [配图配置指南](docs/USER_GUIDE.md)：从授权到第一条消息，常用开关与权限。
- [常见问题](docs/FAQ.md)：音频、API Key 字段、模型选择、429、额度和图片。
- [开发接口](docs/API.md)、[测试说明](docs/TESTING.md)、[贡献指南](CONTRIBUTING.md)。
- [安全与数据边界](docs/SECURITY.md)、[漏洞报告](SECURITY.md)、[来源与许可说明](NOTICE.md)。
- [路线图](ROADMAP.md)与[更新记录](CHANGELOG.md)。

当前修复代码在 AstrBot 4.28.2 定制环境共 868 项测试通过；首次公开快照的官方原版安装证据保留于测试说明；合成界面验证和真实账号检查的范围见[测试说明](docs/TESTING.md)。账号能力与实际消息平台交付分别核验。

本插件采用 [MIT 许可证](LICENSE)，允许按许可条件使用、修改和商业使用。第三方项目与依赖各自适用其许可证，见[来源与许可说明](NOTICE.md)。

传输诊断区分 `request_deadline`（本次请求截止时间耗尽）、`transport_timeout`（HTTP传输超时）与 `external_cancel`（截止前被调用方或生命周期取消）。外部取消不能仅凭本插件日志区分用户停止、调用方较短超时或插件关闭。

## 视频工具

支持直接文字或当前会话图片的6或10秒视频生成，以及对本插件生成或受控导入、时长不超过8.7秒视频的编辑。本插件只负责生成、编辑、查询及视频读取；调用插件负责等待、通知、发送与重发，详见[使用指南](docs/USER_GUIDE.md#视频生成与编辑)。

面向其他插件的参数、任务状态和发送责任见[跨插件视频调用契约](docs/API.md#跨插件视频调用契约)。提供Agent工具和公共Provider视频方法；视频HTTP接口尚未提供。

调用插件接入能力声明、外部视频来源和提交状态见[视频接入契约](docs/MATOI_VIDEO_HANDOFF.md)。视频与人设参考图联合编辑明确不支持，不会替换成其他模式。
