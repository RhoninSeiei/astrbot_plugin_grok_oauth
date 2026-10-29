# 测试说明

当前公开快照版本为 0.8.11，包含完整日志规范修复。安装范围为 `>=4.28.2,<5.0`；当前修复验证基于 4.29.4，扩大安装范围不代表后续 4.x 版本已经逐版验收。Python 3.12 或更新版本；协议测试不依赖 AstrBot，集成测试使用真实核心源码，导入失败不会跳过。

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

0.8.4 在 AstrBot 4.29.3 生产镜像的独立无网络容器中完成 974 项回归，0 失败、0 错误、0 跳过，2 项既有警告。新增 8 项覆盖宿主 conversation_id 为 None 或字符串时的普通和流式 Provider 及真实核心 Agent 路径，修复前复现参数拒绝，修复后确认上游 JSON 不含会话 ID，Grok 主模型保持且备用模型零调用。其他未知参数在普通和流式请求中仍拒绝。

19 项独立诊断测试覆盖原生与文件开关、字段过滤、文件与目录权限、轮转、链接拒绝、异步队列、写入失败、满队列关闭取消及并发关闭。运行时另验证仅文件开关的请求链路和关闭排空。禁止内置日志器及第三方日志器操作的回归检查通过；第三方原始日志由宿主管理，不被独立文件截获。

首次公开源码快照曾在官方 `soulter/astrbot:v4.28.2` 独立容器完成 849 项通用测试及公开 URL 安装；此证据早于本轮日志修复，不等同当前快照重新运行官方镜像。该次容器无生产数据或网络，安装前后依赖无变化，`pip check` 通过，容器与下载镜像随后清理。

## 打包与隔离安装

```sh
python -B scripts/build_package.py --output dist/astrbot_plugin_grok_oauth-0.8.11.zip
python -B scripts/build_public_source.py --output dist/public-source.zip
ASTRBOT_SOURCE=/AstrBot python -B scripts/verify_standard_install.py dist/astrbot_plugin_grok_oauth-0.8.11.zip --allow-dependency-install
ASTRBOT_SOURCE=/AstrBot python -B scripts/verify_standard_install.py --repo-url https://github.com/RhoninSeiei/astrbot_plugin_grok_oauth --allow-dependency-install
```

构建采用文件白名单与 SHA-256 清单。`--allow-dependency-install` 允许宿主安装缺少的依赖，只能在可清理的独立环境使用。公开源码快照不携带内部运行记录、旧发布包或私有 Git 历史。

## 实际服务与发送边界

既有账号曾分别验证授权、聊天、流式、函数工具、视觉、网页搜索、图片生成编辑、刷新与额度。视频实验分别验证图生视频、直接文生视频、外部 QQ HTTPS 素材导入及视频编辑；受测素材约 6 秒，不代表所有账号、时长或画面均成功。能力声明保留 `account_entitlement=unknown`，视频加参考图联合编辑明确不支持。

调用方示例通过标准 `Video.fromBase64`、`Context.send_message` 和 aiocqhttp 在 QQ 群发送合成视频，并回读为 video；Grok 插件自身不发送视频。真实 QQ 私聊及其他平台尚未验证。普通安装与回归使用合成授权及替代 HTTP 服务，不能替代账号权限或其他调用插件的独立验收。

## 0.8.4 稳定性增强验证

0.8.4 在4.29.3生产镜像的无网络隔离容器完整回归974项通过，0失败/错误/跳过，2项既有警告。新增覆盖独立连接预算、短退避成功、剩余时间不足、退避中取消、账号切换、HTTP400安全分类、错误体过大/过慢/损坏、解析器异常，以及原生与独立文件字段白名单。普通请求和有副作用请求在JSON及SSE错误体读取取消时分别保持CancelledError或OutcomeUnknown；响应正常关闭。HTTP429、读失败、协议断连与未知提交不自动重试。

日志字段解析单元测试39项；独立文件及原生日志集成19项。新增诊断读取上限为8KiB和250毫秒，原有403授权分类读取保持原语义。源码与生产依赖均无新增。

## 0.8.5 OAuth 诊断验证

在 AstrBot 4.29.3 生产镜像的独立容器中，985 项通用回归与 4 项定制核心扩展通过，均无失败、错误或跳过。通用回归有 2 项既有弃用警告。新增 15 项覆盖 OAuth 正常授权、连接失败退避、非 JSON 响应、HTTP 错误、取消、整体期限、日志及回调故障、字段脱敏、流程所属管理员隔离、新流程清理旧诊断、凭据持久化失败与命令和 Web API 输出。新增测试修复前观察到缺失诊断接口导致失败。

原生诊断仍仅通过 AstrBot API 日志器输出；未改变凭据目的地、TLS 校验、重试范围或依赖。上述测试使用合成凭据和替代 HTTP 服务，未复现 Issue #1 的 Windows 与 Mihomo 实际网络环境；更新后仍需提交者提供新的脱敏诊断才能确定其连接故障原因。

## 0.8.6 Windows 凭据兼容验证

Windows Python 3.12 与 3.13、Linux Python 3.12 的 GitHub Actions 矩阵分别通过 53 项凭据与 OAuth 协议测试，零失败、错误和跳过。Windows 实际读回 DACL，验证仅当前运行用户和 SYSTEM 具备完全控制权限、继承保护生效，并覆盖旧凭据权限收紧、原子替换、关闭重开、清除和跨进程锁。Windows 测试最初因旧 PowerShell 继承新版模块路径导致 Get-Acl 加载失败，修正测试子进程环境后按原权限断言通过。

当前 AstrBot 4.29.3 镜像的独立 Linux 容器完整回归 996 项通过，零失败、错误和跳过，1 项既有 audioop 弃用警告。新增覆盖缺失 POSIX 文件接口、临时句柄与权限失败清理、锁释放、保存失败诊断和管理员隔离。Windows 验证针对凭据与 OAuth 协议，不代表全部媒体功能或提交者的完整桌面环境已验收；Python 最低要求仍为 3.12。

## 0.8.7 Windows 媒体与日志验证

Windows Python 3.12 和 3.13 分别通过 195 项凭据、OAuth、本地图片视频、独立日志及发送记录测试；Linux Python 3.12 同一跨平台矩阵通过 190 项。当前 AstrBot 4.29.3 镜像的独立 Linux 容器完整回归 1027 项通过，均无失败、错误或跳过，完整回归仅有 1 项既有 audioop 弃用警告。

Windows 实际验证私有 ACL 读回、日志轮转、硬链接及 junction 拒绝、盘符路径与 file URI、元数据重开、Unicode 发送记录及取消清理。原地 FSCTL_SET_REPARSE_POINT 在两个 Windows 版本均实际安装成功，随后相对读取和写入只能操作原对象或安全拒绝，外部标记文件保持；SUBST 盘符重绑定另验证轮转、替换和失败清理仍留在原目录。Linux 的安全打开、文件权限和目录同步分支保留。

测试发现并修正了原生目录共享权限和写句柄属性读取权限差异；原图片过期测试改用可控时钟，继续验证相同租约与过期行为，避免依赖 CI 磁盘在 20 毫秒内完成操作。没有新增运行依赖或修改网络重试策略。Windows 媒体测试使用合成图片、MP4 和替代上游，未进行真实生成或聊天渠道发送。

## 0.8.8 Pillow 依赖兼容与图标验证

真实 Pillow 12.2.0 与 13.0.0 环境中各完成 1029 项 AstrBot 4.29.3 完整回归，零失败、错误或跳过，各有 1 项既有 audioop 弃用警告。两版均包含标准插件安装与重载测试，宿主已有依赖不被替换。Pillow 13 在无网络、无生产挂载的隔离镜像中通过官方 wheel 安装，包变更仅限 Pillow，未修改生产环境。

跨平台 CI 保留 Windows Python 3.12、3.13 与 Linux Python 3.12，并分别固定 Pillow 12.2.0 和 13.0.0，六组合全部通过。每个组合以宿主同版本 Pillow 固定约束执行离线 pip dry-run，要求安装报告为空；Windows 文件系统与媒体安全回归继续保留。`constraints-production.txt` 是维护环境基线，不用于要求其他宿主降级。

插件根目录提供 256×256 RGB PNG `logo.png`，按 AstrBot 标准自动识别，运行包与公开源码都包含相同文件。图标基于 Grok 黑白轮廓组合 AstrBot 双星元素。

## 0.8.9 工具容器兼容

请求工具通过宿主 `get_full_tool_set()` 从 FunctionToolManager 创建快照，已有 ToolSet 与请求授权保持。搜索、额度绑定保留宿主权限包装，拒绝执行时不调用上游；普通、主动形态及直接 Provider 的普通与流式路径均纳入回归。主动形态测试不代表发送真实群消息或完成其他插件的端到端验收。

0.8.9 在当前 AstrBot 4.29.4 精确镜像中完整 1109 项通过，零失败、错误或跳过，1 项既有弃用警告。新增图片回归覆盖 GIF 首帧、BMP、TIFF、AVIF 实际转换、原生格式字节保留、输入与输出容量限制、解码前预算、未批准解码器拒绝和转码取消。普通与流式 Provider 均确认 GIF 转换后的 PNG 进入请求；未读取真实用户图片或发送群测试消息。

## 0.8.10 调用用量保留

当前 4.29.4 精确镜像完整 1196 项通过，零失败、错误或跳过，1 项既有弃用警告。覆盖单边计数与明确零值、异常缓存和总量不扩大计数、usage 元数据白名单、失败终态、终态缺省回退、连接关闭异常、取消及超时显式异常链。真实核心记录器验证 partial、missing 和每调用单次入账；未以测试结果推断 xAI 在所有失败调用中都会返回用量。

## 0.8.11 请求内快照

无快照接口的旧核心完整1217项通过；带最终接口的固定核心候选完整1228项通过，均无失败、错误或跳过。新增真实核心记录器测试验证提前关闭、缺失及单边零值、重复快照、正常单次记录、并发隔离与部分终态方向保持。数据库写入边界采用替身核对行内容与次数，不代表测试期间执行了生产数据库或真实上游调用。
