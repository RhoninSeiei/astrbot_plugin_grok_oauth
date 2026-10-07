# 开发 API

通过 AstrBot 的 `context.get_provider_by_id(provider_id)` 获取配置的 Grok OAuth 实例。文本接口遵循当前宿主 Provider 契约；认证完全由插件共享服务管理。

```python
provider = context.get_provider_by_id("grok-main")
reply = await provider.text_chat(prompt="解释这个算法", model="grok-4.6")
async for chunk in provider.text_chat_stream(prompt="分步说明"):
    if chunk.is_chunk:
        # 增量显示。
        pass
    else:
        # 最终聚合响应，用于历史与统计，不再次发送累计文本。
        pass
```

支持上下文、system_prompt、image_urls、func_tool、tool_calls_result、extra_user_content_parts、单次 model、reasoning.effort 或 reasoning_effort、temperature、top_p、max_output_tokens 和 timeout。单次参数优先于 Provider 配置及插件默认。未知参数明确拒绝；当前原生web_search按显式策略开放，X搜索、代码执行、原生图片、音频及对话接口的视频输出仍未实现；独立 Imagine 视频工具见本文视频章节。

图片输入接受 PNG、JPEG 和 WebP，GIF 原文件不会自动转码或抽帧。Grok 4.7 的 OAuth `/v1/responses` 路径实测拒绝静态及动画 GIF；调用方需要先转换图片或显式抽帧。

`get_models()` 与 `capabilities` 读取本地快照，不刷新凭据、不发送请求。授权成功后可以低成本刷新模型目录；目录失败不会取消已完成的授权。

## 图片开发接口

```python
images = await provider.generate_image(
    prompt="绘制一幅山水画",
    model="grok-imagine-image-2.0",
    n=1,
    aspect_ratio="16:9",
    resolution="1k",
    timeout=180,
)
# 方法仅生成并保存；由调用插件决定如何发送。
image = images[0]
# image.path、mime_type、asset_id、request_id、model、width、height
```

完整签名：

```python
async def generate_image(
    prompt, model=None, size=None, n=1, reference_images=None,
    action=None, timeout=None, *, aspect_ratio=None, resolution=None,
): ...
```

`model` 在此表示 Imagine 图片模型。`reference_images` 接收允许根目录中的路径、file URI、经过验证的 data URI 或管理员白名单允许的 HTTPS URL。调用方不能通过传参扩大根目录或下载范围。Agent 图像工具使用独立会话资产 ID，不能把开发接口的文件路径放入模型工具参数。

`size` 兼容值为 `1024x1024`、`2048x2048` 和 `auto`，分别映射为分辨率与比例档位，不保证实际像素尺寸完全相等。`size` 与原生比例或分辨率参数不能同时设置。总超时涵盖输入解析、排队、认证、请求与保存。

## 管理 HTTP API

路径前缀 `/api/plug/grok-oauth/`，由当前 AstrBot Dashboard JWT 鉴权。POST 使用 JSON，设备流程归属于发起管理员。OpenAPI key 不能代替 Dashboard JWT。

| 方法与路径 | 输入 | 用途 |
| --- | --- | --- |
| GET auth/client | 无 | 查看实际客户端 ID、profile 与说明 |
| POST auth/start | confirmed_client_id、client_profile，可选 account_slot=default | 确认客户端后发起设备码授权 |
| GET auth/status | 可选 flow_id | 查看授权状态，永不返回 token 或 device_code |
| POST auth/cancel | flow_id | 取消当前管理员的流程 |
| POST auth/disconnect | 可选 account_slot=default | 清除绑定并使迟到认证结果失效 |
| GET capabilities | provider_id | 读取实现、配置、模型和账号状态快照 |
| POST test/chat | provider_id、run=true | 显式文本测试 |
| POST test/image | provider_id、run=true | 显式图片测试，可能消耗账号额度 |

错误响应使用固定 `error` 类型、受限 request_id、operation_id 和 partial 标记，不回显上游响应体。`OutcomeUnknown` 表示请求可能已执行，禁止自动重新生成。部分图片成功时内部类型化错误保留资产，工具返回安全的资产句柄。

HTTP 402映射为 `PaymentRequired`，继承 `PermissionDenied`，表示上游要求可用额度或相应订阅。该拒绝不会触发刷新或自动重试，也不会回显上游错误正文。


## 原生插件页面与来源兼容

`pages/oauth/index.html` 使用宿主 `AstrBotPluginPage` bridge。相对端点 `auth/client`、`auth/start`、`auth/status`、`auth/cancel`、`auth/disconnect` 由宿主映射到 `/api/v1/plugins/extensions/astrbot_plugin_grok_oauth/...`，与已有 `/api/plug/grok-oauth/...` 共享同一处理函数及所有者校验。页面不接收或保存 Dashboard JWT、access token 或 refresh token。

“我已认证”只读取实际状态，不能改变授权结果。设备码流程由服务器持续轮询；刷新页面可以继续显示当前管理员的未完成流程。解除绑定在页面中需要再次确认。

提供商源模板显示名为 `Grok Oauth`，内部类型保持 `grok_oauth_chat_completion`。使用标准来源及模型 API 管理，模型应指定 `provider_source_id`。旧模型兼容仅识别本插件类型及默认账号槽，不修改已有有效归属、模型 ID 或其他提供商。无明确唯一来源时新建不冲突的兼容来源；禁用来源不会被自动选作迁移目标。

当前核心模板接口保留浅引用，旧版本的技术名称模板可能残留。只清理已知早期完整模板形状的旧别名；陌生形状报告注册冲突，命名模板仍按对象所有权清理。

宿主插件 iframe 当前不允许弹窗。页面提供官方链接复制方式，未绕过沙箱。允许弹窗的宿主中，新页面会移除 opener；链接仅接受 HTTPS 的 `auth.x.ai` 或 `accounts.x.ai`。


## AstrBot 请求策略兼容

Provider 的普通与流式文本入口接收 `oauth_web_search` 和 `retry_rate_limits`，在适配层消费，不作为模型参数发送。搜索参数省略或为 `None` 时读取当前核心请求上下文。

| 搜索策略 | 行为 |
| --- | --- |
| `inherit` | 不注入原生搜索；保留调用方提供的函数工具，由模型按需选择 |
| `disabled` | 不提供原生搜索，并移除本插件搜索函数；不修改其他插件的工具 |
| `live` | 提供xAI原生web_search，默认auto由模型选择，移除重复的grok_web_search函数；支持普通及流式调用 |
| `cached` | 返回UnsupportedModelParameter；不执行实时搜索 |

`tool_choice="none"` 仍禁止工具调用；`required` 在可用工具中要求至少一次调用，不等价于必定搜索。`search_allowed_domains` 可在live请求中限制最多5个精确公共域名。全局 `search_enabled=False` 时live返回PermissionDenied，disabled仍可正常聊天。

`retry_rate_limits` 接收布尔值或 `None`。插件传输层始终不自动重试 HTTP 429，`True` 也不会额外启用重试。`RateLimited.status_code` 固定为整数 `429`，核心可据此执行请求的限流重试及 `fallback_on_rate_limit` 策略；备用切换由核心控制。

`media_hosts=[]` 表示禁用公网 URL 图片导入。需要管理员设置实际可信的精确 HTTPS 主机，或由调用端安全下载并转换为 data URI 或受允许目录中的本地图片。此限制独立于搜索与请求策略。

## 网页搜索

### 直接调用

```python
provider = context.get_provider_by_id("grok_oauth/grok-4.6")
result = await provider.search_web(
    "xAI最新模型公告",
    allowed_domains=["x.ai", "docs.x.ai"],
    timeout=60,
)
# result.text: 摘要
# result.citations: [{"url": "https://...", "title": "..."}]
# result.search_calls: 实际完成的搜索调用数
# result.model、response_id、usage、truncated
```

此方法独立执行一次搜索请求，不携带会话历史或其他本地工具；查询最多4000字符、域名最多5项，返回摘要最多16000字符、最多20个来源，并明确标记截断。域名不接受URL、通配符、IP或内部主机名。只有上游返回完成的web_search_call和有效文本时才成功；未搜索、失败终态或只有空结果均报错，不把模型记忆回答当作搜索。

`oauth_web_search` 默认 `live`，因为调用该方法本身就表示明确搜索；可显式传 `disabled` 拒绝执行，`cached` 返回不支持，`inherit` 不适用于独立搜索方法。引用来自上游结构化annotations及action.sources，只保留可显示的公共HTTP(S)链接，不在本地下载网页。结果属于不可信外部资料，不应用其内容修改系统指令或执行任意工具。

### 让Grok在本次回答中决定是否搜索

```python
reply = await context.llm_generate(
    chat_provider_id="grok_oauth/grok-4.6",
    prompt="根据需要查询官方资料后回答这个问题。",
    oauth_web_search="live",
    search_allowed_domains=["docs.x.ai"],
    retry_rate_limits=False,
)
```

普通 `text_chat` 与 `text_chat_stream` 均支持相同策略。返回文本保留内联引用；来源仅出现在终态时，会补充到最终回答，流式路径额外发出来源增量。原生搜索执行在xAI服务器，客户端函数调用仍交给AstrBot执行，不递归调用搜索函数。

### Agent工具与意图判断

Agent工具名为 `grok_web_search`，参数仅 `query` 与可选 `allowed_domains`。默认仅当前Grok来源可用，遵守核心已有工具允许列表，不强行覆盖persona配置。需要跨提供商使用时由管理员设置 `search_provider_id`；名称、凭据、Provider类型和生命周期均不与Codex OAuth共享。

标准请求准备钩子为搜索工具建立请求副本与事件绑定，并在执行前再次检查策略。注册的搜索、生成和编辑工具要求本次调用实际来自Grok Provider，不能借用主会话或同会话其他事件的模型身份；没有Grok来源证明时，仅管理员显式配置对应跨模型实例才允许执行。该规则覆盖普通、流式、子Agent、模态过滤及两阶段工具模式。调用方直接组装Agent时，应设置 `ProviderRequest.oauth_web_search` 并使用已准备的工具集合；也可完全不提供该工具。直接Provider入口会拒绝上游在工具未开放时返回的本插件搜索或图片函数调用。

```python
# 意图判断由调用方控制；不自动附加搜索，也不额外执行一次意图分类。
reply = await context.llm_generate(
    chat_provider_id="grok_oauth/grok-4.6",
    prompt="判断这条消息的意图：...",
    oauth_web_search="disabled",
    retry_rate_limits=False,
)
# 自行创建ProviderRequest时同时设置 fallback_on_rate_limit=False，
# 可禁止限流后的备用Provider切换。
```

工具错误返回固定error类型，限流附带status_code=429，不返回上游错误正文。取消和插件卸载会终止在途搜索，总超时包含认证与网络等待。当前实现网页搜索，不宣称支持X搜索、缓存模式或已验证当前OAuth账号的搜索权限。

工具说明声明默认供Grok OAuth主模型使用，管理员指定实例为跨模型例外。AstrBot的全局工具列表仍可显示这些独立注册的工具；模型可用性由请求筛选和执行校验实现，并非宿主工具元数据中的强制Provider绑定。对非Grok自建Agent，如果管理员已开放跨模型搜索而调用方绕过准备钩子，仅传通用oauth_web_search=disabled不能保证该策略到达全局工具handler；应使用已准备的ToolSet，或显式从工具集合移除grok_web_search。意图判断最直接的方式是不提供搜索工具。Grok Provider自身的disabled参数仍在Provider层强制执行。

注册工具的来源校验保留在原始Python参数中，不改变字符串值和JSON结构；序列化再反序列化会失去校验信息。自定义执行器应传递原始工具参数；需要显式程序调用时，使用provider.search_web或provider.generate_image接口。直接SDK调用由调用方决定，不自动认定为某个主模型的工具请求。


## 模型能力与 Core 图片路由

已知 Grok 对话模型在模型项缺少 `modalities` 或为 `null` 时，Provider 初始化根据能力目录补齐运行实例的 `text`、`image`、`tool_use`，供 Core 识图选择及上下文处理使用。显式列表保持原值，包括历史空列表；未知模型不推定视觉能力。不修改持久配置，因此 Dashboard 配置接口仍可能省略该字段，不能仅凭该返回值判断运行实例不支持图片。


## OAuth额度查询

- `await provider.get_usage(force_refresh=False)`：后端开发接口，返回规范化字典；调用方负责账户信息权限检查。
- `/grok_oauth_usage_breakdown`：管理员直接查询来源明细，与总量命令采用相同群白名单及独立于LLM的权限；缺失显示未知，比例不归一化。
- `/grok_oauth_usage`：管理员直接命令，不调用LLM，聊天额度耗尽或聊天429时仍可执行额度查询。
- `grok_usage_breakdown({})`：仅在询问额度消耗来源、按产品用量时调用。与总量工具共享请求、缓存和权限，返回 `products` 数组，每项仅含 `product`、`display_name`、`usage_percent`；识别 GrokChat、GrokBuild、GrokImagine，旧 PRODUCT_GROK_BUILD 映射为 GrokBuild。未知产品只计入 `unrecognized_product_count`，不回传原始名称。`percent_basis=upstream_product_usage` 表示保留上游百分比，不保证合计为 100%，不代表独立产品额度。
- `grok_usage_status({})`：按需空参数工具，仅供Grok请求使用，普通用户可查询；不开启每轮对话查询。
- `GET /api/plug/grok-oauth/usage`：已认证Dashboard查询；`?refresh=true`受控刷新，不能绕过上游冷却。

额度工具允许普通用户使用，仅接受Grok模型实际产生的调用。群聊默认拒绝；`usage_group_allowlist`填完整会话标识后，普通群成员可查询。`usage_tools_enabled`只控制LLM入口。两个额度命令均检查管理员身份并遵循群白名单，不受模型限制；Dashboard鉴权不变。额度工具的旧`usage_cross_model_enabled`与`usage_provider_id`配置已移除，残留值不会开放其他模型。

默认工具来源证明附着于内存中的工具名，参数仍为`{}`。请求专属`UsageToolSet`在查找时创建一次性handler，避免改写共享工具。证明与请求集合绑定当前授权代次，换绑后旧调用拒绝。普通及精简工具模式均支持；默认拒绝JSON重放。全局handler无法证明请求来源时拒绝，包括未准备的自建Agent。自建Agent使用插件`usage_tools.prepare(event, req)`处理`ProviderRequest`，再把`req.func_tool`交给宿主Agent；不能把准备前的普通ToolSet重新传入。其他模型、备用模型及子Agent不能借用主会话的Grok身份；JSON重放与全局直接执行均拒绝。

额度白名单字段：`status`、`scope`、`period_type`、`used_percent`、`remaining_percent`、`period_start`、`reset_at`、`reset_at_local`、`observed_at`、`cached`、`stale`、`source`。状态包括`success`、`unknown`、`unparseable`、`unbound`、`identity_unavailable`、`reauth_required`、`forbidden`、`rate_limited`、`unavailable`、`authorization_changed`。权限拒绝只返回`status=denied`。

只有明确统一计费标记才显示`scope=account_shared`。`period_type`为weekly、monthly或unknown；UTC时间用Z，`reset_at_local`为Asia/Shanghai。缺失比例为null，明确0与100均保留。合法比例缺失周期时status=unknown并保留比例。异常数据不会解释为零；旧字段仅在明确同单位与完整周期时换算。

同运行时模型共享120秒缓存及并发请求；普通刷新令牌不清除缓存，换绑、解绑和关闭使旧账户结果失效。429不立即重试，刷新按钮不能绕过Retry-After冷却。上游暂时不可用或限流时可返回最长10分钟历史快照，status仍为错误并标记stale，不能跨已知周期终点；403与未绑定不返回旧额度。单请求15秒、总操作30秒，401至多一次版本感知刷新；账务拒绝不主动解除聊天绑定。

固定协议来源见[参考资料](REFERENCES.md)。身份优先取本插件固定HTTPS令牌端点返回的ID token sub。同时兼容已有access token：仅当issuer为https://auth.x.ai、client_id匹配绑定、主体类型为User且principal_id与sub一致时读取身份，支持官方snake_case和camelCase字段。仅有sub或存在字段冲突时拒绝回退；上游继续验证Bearer签名。旧凭证可直接使用，新认证与刷新也自动保存可用身份，无需单独额度授权。只有两种身份来源均不可用时才尝试一次刷新，再返回明确未知状态。不使用其他客户端凭据，不提供模型可指定的账号字段。真实账户可用性与离线实现分开核验；本地能力声明不保证账号获上游授权。

来源视图独立校验 `config.productUsage`，缺失为 unknown，部分已知数据为 partial，非法或重复记录为 unparseable；来源解析失败不改变既有总量结果。来源视图还包含 status、scope、period_type、period_start、reset_at、reset_at_local、observed_at、cached、stale、source。网络及认证失败状态沿用共享快照，历史数据仍标明 stale。总量工具原有 12 字段保持不变，不新增对外 HTTP 路由。

直接命令的人类可读重置时间与采集时间统一从绝对时间转换为AstrBot进程本地时区，带UTC偏移，并按各时间点处理夏令时。Docker部署以容器时区为准。协议层JSON字段与页面格式保持原约定，命令不再使用固定Asia/Shanghai的reset_at_local。


## 4.7模型与版本信息

`grok-4.7`与`grok-4.7-build-fast`支持low、medium、high、xhigh，拒绝none/max。未指定effort时不强行覆盖上游默认。两型号在已知模型能力中包含vision与function_tools；observed仍独立记录，静态支持不代表每个账号已验收。保留现有4.6默认值与管理员指定modalities。

所有插件HTTP客户端从grok_oauth/version.py读取统一USER_AGENT，包导出__version__使用同一值；构建脚本校验metadata.yaml及pyproject.toml一致，不再各自维护请求版本字面量。

Responses输出通过版本化grok_oauth_reasoning历史封装保留原始顺序，包括reasoning、message、function_call及已支持的web_search_call。新的output字段不补写reasoning缺省数组；旧items字段保留作为降级视图。宿主可见文本（包含追加Sources）和规范化工具调用未变时回放原output；宿主编辑后放弃旧不透明状态，以当前内容重建。未知版本、未知项、重复函数call_id、损坏或混合本插件状态拒绝；外来提供商签名忽略。该历史状态不是执行工具的权限凭证，工具执行仍检查一次性内存来源证明。

新增能力核查：官方建议prompt_cache_key帮助缓存路由、context compaction用于长任务；本版没有自动生成会话键、调用压缩端点或启用新远程工具。Fast在未指定include时的加密字段与标准4.7存在实测差异，因此保留显式include。官方模型与协议来源见[参考资料](REFERENCES.md)。


## 原生提供商与模型配置

注册沿用AstrBot register_provider_adapter与ProviderType.CHAT_COMPLETION；连接模板只注册为提供商源，模型由原生provider_source_id关联。私有Core兼容访问仍集中compat.py，未修改宿主源码、全局通用schema或自建模型编辑页。

启动迁移在ProviderManager.resource_lock内通过默认配置的原子save_config一次保存：先修复旧来源关联，再规范化有效Grok源下的模型条目。仅移除与所属源一致的冗余连接字段；不同的timeout等覆盖保留，旧oauth-managed占位只在可证明安全时移除。保留ID、model、enable及所有显式模型参数。缺失modalities按已知模型能力补齐，未知模型保守text；max_context_tokens默认0，custom_extra_body默认{}。来源已有这些字段时优先继承，避免默认值遮蔽来源设置。重复启动不再修改配置，非Grok或有歧义来源保持原状。

custom_extra_body支持reasoning.effort、reasoning_effort、temperature、top_p、max_tokens、max_output_tokens。max_tokens映射为max_output_tokens；同时指定两种上限拒绝。优先级为本次调用参数、原生模型custom_extra_body、旧顶层模型参数、插件默认；两种reasoning写法在本次调用中覆盖自定义设置。所有值仍通过原有类型和范围校验，禁止从额外参数覆盖model/input/tools/store或认证目标。

## 视频生成与编辑

视频接口按职责拆分：本插件负责OAuth视频提交、编辑、查询、下载和受作用域约束的资产读取；调用插件负责等待调度、文字通知、选择目标渠道、发送和重发。本插件不创建视频后台发送任务，不自动发视频或状态通知。图片工具原有发送行为不受影响。

支持文生及图生视频 `grok-imagine-video-1.5`，6或10秒、480p档位；视频编辑 `grok-imagine-video` 接受本插件同账户、同会话已生成或受控导入且不超过8.7秒的视频。暂不提供视频与参考图联合编辑、续写或远端取消。单文件最大20 MiB，资产默认保留7天；具体像素尺寸由上游确定。

## 跨插件视频调用契约

### 公共Provider接口

使用 `context.get_provider_by_id(provider_id)` 获取本插件的Grok提供商，无需导入本插件内部类或读取OAuth凭据。以下方法均为异步方法，除媒体导入和字节读取外均返回普通字典。

| 方法 | 参数 | 结果及副作用 |
| --- | --- | --- |
| `import_video_image(reference, *, event)` | 图片来源字符串和真实消息事件 | 验证并保存当前会话图片，返回图片资产ID |
| `submit_video(prompt, reference_asset_id, *, event, operation_key, duration=6)` | 图片资产ID、稳定操作键，duration为6或10 | 提交图生视频，返回任务；不发送消息 |
| `edit_video(prompt, reference_asset_id, *, event, operation_key)` | 已生成或导入视频的资产ID、新的稳定操作键 | 提交编辑任务；不发送消息 |
| `get_video_job(job_id, *, event, timeout=30)` | 本插件任务ID | 查询一次；完成时下载保存结果；不发送消息 |
| `wait_video_job(job_id, *, event, timeout=300)` | 本插件任务ID | 有界等待，超时可返回pending，后续继续查同一任务 |
| `read_video_bytes(job_id, *, event)` | 已完成任务的ID | 在资产租约内校验账户及大小，返回MP4 bytes；不发送消息 |
| `get_video_capabilities(*, event)` | 原始事件 | 无网络能力声明，schema_version=1 |
| `submit_text_video(prompt, *, event, operation_key, duration=6)` | 提示词原文、稳定键 | 单次文字生视频，不先画图片 |
| `import_video_source(reference, *, event)` | 真实附件来源 | 安全导入不超过8.7秒的MP4并返回资产ID |
| `edit_video_with_images(prompt, reference_asset_id, reference_image_asset_ids, *, event, operation_key)` | 来源视频与有序参考图 | 当前明确VideoUnsupported、not_submitted；零上游请求 |

`event` 必须保留原始平台和UMO。方法通过宿主conversation manager取得当前会话ID，不接受调用方自行填入资产scope。换会话、解绑、账户切换或过期会使旧资产不可用。调用插件应在收到事件时保存原始UMO，恢复时核对仍属于同一conversation；调用插件必须传入原始事件并保存原始会话；此接口不作为同进程插件之间的安全隔离边界。

Provider方法由调用插件显式选择实例，受 `videos_enabled`、OAuth和会话约束，不要求LLM调用证明，也不依赖 `tools_enabled`。图片来源接受受控data URI、管理员允许目录内路径以及媒体白名单HTTPS URL；不会因调用插件传参扩大文件或网络权限。

`operation_key` 为必填、非空、最多256字符的稳定键。调用插件应按原始业务请求生成，并持久保存图片资产ID、原始参数、操作键和任务ID。重试相同请求复用同一图片资产ID和操作键；不要重新导入图片或生成新键，否则无法保持原请求去重语义。相同键但参数不同会拒绝。编辑是另一个业务操作，需要新的键。不同Provider实例的键空间相互独立；请在键中加入调用插件自身的名称。

### 调用顺序

以下await示例放在调用插件的异步方法或受生命周期管理的任务中执行。

```python
# context和event来自调用插件；示例不导入Grok插件内部模块。
provider = context.get_provider_by_id("grok-main")
original_umo = event.unified_msg_origin

image_id = await provider.import_video_image(image_reference, event=event)
# 调用插件先持久保存image_id、固定prompt、operation_key和original_umo。
job = await provider.submit_video(
    prompt="让球体缓慢向右移动，镜头保持固定",
    reference_asset_id=image_id,
    event=event,
    operation_key="my-plugin:original-message-id:video-1",
    duration=6,
)
# 立即持久保存job["job_id"]。同一业务操作的执行应由调用插件串行化。
job = await provider.wait_video_job(job["job_id"], event=event, timeout=300)
if job["status"] == "done":
    video_bytes = await provider.read_video_bytes(job["job_id"], event=event)
    # 由调用插件决定发送；发送示例见下文。
```

若上次已保存job_id，恢复时直接调用 `get_video_job` 或 `wait_video_job`，不再次提交生成。`wait_video_job` 由调用插件安排到自己的受生命周期管理任务中，卸载时负责取消；取消本地等待不等于取消xAI远端任务。异常可能以本插件的类型化异常抛出，应记录受限错误类型，不将原始响应或令牌写入日志。

编辑示例：

```python
edited = await provider.edit_video(
    prompt="把球体改为红色，保持原有运动",
    reference_asset_id=completed_job["asset_id"],
    event=event,
    operation_key="my-plugin:original-message-id:edit-1",
)
```

### Agent工具

保留 `grok_video_generate`、`grok_video_edit`、`grok_video_status` 三个工具。生成参数为 `prompt`、`reference_asset_id`、可选 `duration`；编辑为前两项；状态工具为 `job_id`。工具返回JSON字符串，含 `delivery_owner="caller"`，不包含视频bytes、base64、文件路径或签名URL。

正常AstrBot消息准备流程会导入当前及引用消息中的图片，向模型提供图片资产ID。自建Agent若绕过准备钩子，可通过上述 `import_video_image()` 显式导入，向模型提供资产ID，再让宿主执行工具。仅调用 `text_chat()` 获取工具调用结构并不会自动执行工具；可参考[AstrBot Agent调用文档](https://docs.astrbot.app/dev/star/guides/ai.html)。工具默认只允许Grok OAuth实际调用方；其他主模型需要管理员配置 `tools_provider_id`，并保留真实事件和宿主调用上下文。

调用插件从工具结果取得job_id后，可使用上述Provider方法等待和读取，之后自行发送。0.8新增文生视频与外部导入通过公共Provider方法提供，三个既有Agent工具参数保持。只有默认Agent工具循环而没有调用插件的后处理时，Grok插件不会自动把视频发到聊天中。`/grok_video_status <job_id>` 为用户主动查询命令。

### 状态及恢复

```json
{"job_id":"11111111111111111111111111111111","status":"pending","submission_state":"submitted","asset_id":"","error":"","model":"grok-imagine-video-1.5","duration":6,"delivery_owner":"caller"}
```

| 状态或字段 | 含义及调用方处理 |
| --- | --- |
| `pending` | 已提交、仍需查询；不要因此再生成一次 |
| `done` | 视频已下载保存，可以read_video_bytes；不表示已经发送 |
| `failed` | 明确任务失败，调用插件决定如何通知用户 |
| `unknown` | 提交结果不明，不自动重新提交；可能没有可恢复的远端任务ID |
| 工具 `status=error` | 工具执行被拒绝或异常，查看安全的error字段 |
| `job_id` | 本插件任务ID，用于查询和读取；不是远端request_id |
| `asset_id` | 完成后的视频资产ID，可用于edit_video |
| `delivery_owner=caller` | Grok插件未执行任何视频投递 |

视频结果下载固定允许vidgen.x.ai，配置代理时仅该固定HTTPS结果域名走代理且禁止重定向，不附带OAuth或Cookie。额外media_hosts继续执行原有公网DNS校验和地址固定。新增 `submission_state` 用于明确not_submitted、submitted、unknown；它与任务status独立，失败或取消不能自动解释为未提交。完整能力、异常状态、外部来源和Matoi配额衔接见[接入交接](MATOI_VIDEO_HANDOFF.md)。

文件保存在Grok插件数据目录，不要求NapCat能访问该目录；调用插件通过bytes接口取得受控副本。

### 已验证的QQ发送方法

完整可复制实现：[examples/qq_video_sender.py](../examples/qq_video_sender.py)。该文件仅为调用插件参考，不会由Grok插件加载。它使用调用方的Context和原始UMO，通过标准 `MessageChain`、`Video.fromBase64()`、`Context.send_message()` 发送一次，复用已有aiocqhttp连接。

```python
# 将示例文件复制到调用插件后使用相对导入。
from .qq_video_sender import send_qq_video

video_bytes = await provider.read_video_bytes(job_id, event=event)
# 调用插件先持久记录sending，再执行下列调用。
delivery = await send_qq_video(context, original_umo, video_bytes)
# 调用插件持久保存delivery，不将生成状态done当作发送成功。
```

发送返回 `sent`、`rejected` 或 `unknown`。异常、超时、无法解释的返回值均归为unknown，示例不会重试；外部取消会继续抛出，调用方应将原sending记录标为unknown。进程重启时也应将未完成sending视为unknown。只有确认未收到或用户明确要求重发后，才能再次发送已有bytes，不能重新生成视频。

`sent` 仅表示宿主平台调用正常返回，不代表用户阅读或播放；`Context.send_message()`不提供统一QQ消息ID。不得将上游下载URL直接塞给NapCat，也无需登录NapCat WebUI、创建调试适配器或更改共享目录权限。

| 发送渠道 | 验证状态 |
| --- | --- |
| QQ群，AstrBot aiocqhttp → NapCat | 调用方示例真实发送成功，群消息回读为video；使用既有机器人连接 |
| QQ私聊，同一适配器 | 示例接受FriendMessage；尚未做真实私聊验证 |
| Telegram、微信等其他适配器 | 不在此示例验证范围，示例会拒绝；由调用插件另行适配 |

发送验证使用已生成的合成短视频。当前版本提供六个基础 Provider 方法，以及能力声明、文生视频、外部视频导入和联合编辑拒绝接口。接入时以实际方法存在和 capabilities 为准。[AstrBot消息发送文档](https://docs.astrbot.app/dev/star/guides/send-message.html)说明了标准组件和UMO主动发送机制。
