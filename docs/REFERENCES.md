# 文档与协议来源

固定提交、文件 blob 和 SHA-256 见[公开来源清单](SOURCE_REFS.json)。该清单不包含部署指纹。

固定源码与文件哈希最近核对：2026-10-07。官方视频协议于 2026-10-03 核对；文档说明产品协议，OAuth 实际权限仍须分别验证。

| 来源 | 核对内容 |
| --- | --- |
| [AstrBot 插件发布](https://docs.astrbot.app/dev/star/plugin-publish.html) | GitHub 仓库、AstrBot Cloud 提交、元数据和 16 MB ZIP 限制 |
| [AstrBot 市场规范](https://docs.astrbot.app/dev/plugin-market/2026-06-27.html) | author/name 身份、版本与仓库字段一致性；logo 为可选字段 |
| [AstrBot 插件开发](https://docs.astrbot.app/dev/star/plugin-new.html) | 插件结构与依赖声明 |
| [Grok 4.7](https://docs.x.ai/developers/grok-4-7) | 文本和图片输入、文本输出、四档推理、上下文与 Fast 渠道边界 |
| [xAI 发布记录](https://docs.x.ai/developers/release-notes) | 新模型与独立语音服务的发布说明 |
| [xAI 实时语音](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech) | 独立 WebSocket 服务；不据此认定当前插件支持音频 |
| [Grok Build 额度参考提交](https://github.com/xai-org/grok-build/tree/4247f661689354b831191f11eeeac8424993fe3d) | OAuth 额度目标、身份与百分比字段，参考实现而非复制源码 |

额度参考文件为 `crates/codegen/xai-grok-shell/src/extensions/billing.rs`；同时追踪该提交的 agent/config、login/config、device_code、oidc/protocol 与 HTTP 客户端，核对请求目标和字段来源。对应 Apache-2.0 许可见 [LICENSE](https://github.com/xai-org/grok-build/blob/4247f661689354b831191f11eeeac8424993fe3d/LICENSE)。

## 视频协议与 OAuth 来源

| 来源 | 核对内容 |
| --- | --- |
| [Grok Build 视频参考提交](https://github.com/xai-org/grok-build/tree/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8) | 官方图生视频工具、任务提交与查询、媒体 Bearer 解析；参考实现而非复制源码 |
| [视频 REST API](https://docs.x.ai/developers/rest-api-reference/inference/videos) | 视频任务提交、查询及结果字段 |
| [视频生成](https://docs.x.ai/developers/model-capabilities/video/generation) | 文生及图生视频请求参数 |
| [视频编辑](https://docs.x.ai/developers/model-capabilities/video/editing) | 视频输入编辑、时长限制与输出继承规则 |
| [参考图生成视频](https://docs.x.ai/developers/model-capabilities/video/reference-to-video) | 参考图模式不能与视频编辑组合，不据此开放联合编辑 |
| [AstrBot 消息发送](https://docs.astrbot.app/dev/star/guides/send-message.html) | 标准视频组件、Context 和 UMO 主动消息发送 |

视频源码参考包括 `video_gen/mod.rs`、`media_bearer.rs` 与 `side_call_bearer.rs`，完整路径、Git blob 和 SHA-256 见[来源清单](SOURCE_REFS.json)。这些来源说明 xAI OAuth 凭据可以用于官方媒体工具，并不保证任意订阅或模式均可用。本插件只使用自身绑定的 OAuth 凭据，不复制官方客户端的 API Key 回退行为。该固定提交的 [Apache-2.0 许可](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/LICENSE)已核对；源码未包含于本插件发行包。

## 用户文档编排参考

通过 GitHub 仓库检索选择较受关注的插件，参考安装说明与截图编排，不复制其文字、图片或代码：

- [群聊日常分析](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)：功能预览、配置表、独立贡献文档。
- [表情包管理器](https://github.com/anka-afk/astrbot_plugin_meme_manager)：简短首次使用步骤、逐页截图、FAQ 与更新记录分离。

本项目采用原创说明及合成账号截图，不附带参考插件的品牌图片、群二维码、访问统计或第三方跟踪资源。
