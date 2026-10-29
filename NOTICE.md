# 来源与许可记录

本插件为独立实现，不包含 AstrBot、OpenCode 或 Grok Build 的源码副本，也不在运行时导入其他 OAuth 插件。

- AstrBot：依据宿主 Provider、插件页面和消息组件接口开发适配层。上游许可证标识为 AGPL-3.0；本插件包不附带宿主。宿主源码见 https://github.com/AstrBotDevs/AstrBot 。
- OpenCode：只读参考 xAI 设备码协议、公开客户端常量和刷新行为；固定提交 `57ef3828431790c53f8f333c7ffbfe88770a1812`，文件 blob `a455121d6aedfac1ee9f68bd4f6d455ce5a86eba`，上游 MIT。插件协议实现与测试独立编写，客户端来源标记为本插件。
- Grok Build：只读参考 OAuth 额度接口、身份与计量字段，固定提交 `4247f661689354b831191f11eeeac8424993fe3d`，未复制源码。上游 [Apache-2.0](https://github.com/xai-org/grok-build/blob/4247f661689354b831191f11eeeac8424993fe3d/LICENSE)，许可文件 blob `90b1793cf8eb2d6444863591e8405ecc707dc62d`。
- Grok Build 视频：只读参考官方图生视频提交、任务查询及媒体 OAuth Bearer 解析，固定提交 `2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8`。三个文件的 Git blob、SHA-256 及该提交的 Apache-2.0 许可身份于 2026-10-07 核对，见[来源清单](docs/SOURCE_REFS.json)；未复制源码。
- xAI 官方文档：Responses、reasoning、Images 生成与 JSON 编辑协议、模型及参考图上限，初始核对日期 2026-09-07；Grok 4.7 与音频边界于 2026-09-21 复核，视频生成、编辑及参考图组合限制于 2026-10-03 核对。模型与账号实际权限仍由服务端决定。
- httpx、Pillow、filelock：通过依赖声明使用，发行包使用各自许可。本项目不捆绑 wheel 或修改其代码。

公开 Grok CLI client ID 只是一项参考协议事实，不表示 xAI 已批准本插件。首次设备码授权显示实际客户端 ID 与配置名称；管理员需要明确确认，服务端拒绝时保持类型化错误。没有冒充其他客户端或 API Key 回退。

本插件原创代码及文档采用 [MIT 许可证](LICENSE)。上游项目、依赖与服务分别适用其许可证和使用条款；本插件的 MIT 许可不改变这些条款，也不授予对第三方名称、商标或服务的权利。

用户指南的截图由合成账号的 AstrBot 4.28.1 原生 Dashboard 生成，未包含真实账号、令牌或群标识。AstrBot 名称和界面归其相应权利人所有；Grok 与 xAI 名称仅用于标识兼容服务。
