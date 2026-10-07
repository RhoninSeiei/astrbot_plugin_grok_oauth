# 测试说明

当前公开快照版本为 0.8.2，包含完整日志规范修复。安装范围为 `>=4.28.2,<5.0`；实际核心验证基于 4.28.2，扩大安装范围不代表后续 4.x 版本已经逐版验收。Python 3.12 或更新版本；协议测试不依赖 AstrBot，集成测试使用真实核心源码，导入失败不会跳过。

## 运行检查

在独立虚拟环境安装项目依赖以及 pytest、pytest-asyncio、Ruff，不要修改正在运行的机器人共享环境。

```sh
python -m pip install -r requirements.txt pytest pytest-asyncio ruff
python -B scripts/run_tests.py tests/unit -q
python -B scripts/check_architecture.py
python -B -m ruff check .
python -B -m ruff format --check .
```

集成测试通过 `ASTRBOT_SOURCE` 指定完整 AstrBot 源码目录，省略时为 `/AstrBot`。`run_tests.py` 在导入宿主前创建临时 `ASTRBOT_ROOT`，结束自动清理；不要挂载真实账号、生产配置或运行数据。

```sh
ASTRBOT_SOURCE=/path/to/AstrBot python -B scripts/run_tests.py tests/unit tests/integration -q --junitxml=/tmp/grok-tests.xml
python -B scripts/verify_integration_report.py /tmp/grok-tests.xml
```

默认入口 `python -B scripts/run_tests.py` 运行通用测试；`tests/host_extensions` 的四项定制宿主契约仅供具有对应扩展的核心显式运行。

## 当前验收范围

日志修复代码在 AstrBot 4.28.2 定制核心的完整回归中共 868 项通过，0 失败、0 错误、0 跳过，2 项既有警告。版本统一时仅修改版本标识和公开文档，复用未变化的业务回归；另行完成 5 项版本与架构检查，以及标准安装器替换现有包、指定重载、合成凭据与模型恢复和卸载清理验证，均通过且无依赖安装。

13 项独立诊断测试覆盖原生与文件开关、字段过滤、文件与目录权限、轮转、链接拒绝、异步队列、写入失败、满队列关闭取消及并发关闭。运行时另验证仅文件开关的请求链路和关闭排空。禁止内置日志器及第三方日志器操作的回归检查通过；第三方原始日志由宿主管理，不被独立文件截获。

首次公开源码快照曾在官方 `soulter/astrbot:v4.28.2` 独立容器完成 849 项通用测试及公开 URL 安装；此证据早于本轮日志修复，不等同当前快照重新运行官方镜像。该次容器无生产数据或网络，安装前后依赖无变化，`pip check` 通过，容器与下载镜像随后清理。

## 打包与隔离安装

```sh
python -B scripts/build_package.py --output dist/astrbot_plugin_grok_oauth-0.8.2.zip
python -B scripts/build_public_source.py --output dist/public-source.zip
ASTRBOT_SOURCE=/AstrBot python -B scripts/verify_standard_install.py dist/astrbot_plugin_grok_oauth-0.8.2.zip --allow-dependency-install
ASTRBOT_SOURCE=/AstrBot python -B scripts/verify_standard_install.py --repo-url https://github.com/RhoninSeiei/astrbot_plugin_grok_oauth --allow-dependency-install
```

构建采用文件白名单与 SHA-256 清单。`--allow-dependency-install` 允许宿主安装缺少的依赖，只能在可清理的独立环境使用。公开源码快照不携带内部运行记录、旧发布包或私有 Git 历史。

## 实际服务与发送边界

既有账号曾分别验证授权、聊天、流式、函数工具、视觉、网页搜索、图片生成编辑、刷新与额度。视频实验分别验证图生视频、直接文生视频、外部 QQ HTTPS 素材导入及视频编辑；受测素材约 6 秒，不代表所有账号、时长或画面均成功。能力声明保留 `account_entitlement=unknown`，视频加参考图联合编辑明确不支持。

调用方示例通过标准 `Video.fromBase64`、`Context.send_message` 和 aiocqhttp 在 QQ 群发送合成视频，并回读为 video；Grok 插件自身不发送视频。真实 QQ 私聊及其他平台尚未验证。普通安装与回归使用合成授权及替代 HTTP 服务，不能替代账号权限或其他调用插件的独立验收。
