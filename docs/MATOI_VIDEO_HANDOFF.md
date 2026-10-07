# Matoi CC 视频接入契约

契约版本：`schema_version=1`；适用插件版本：0.8.2。调用方必须同时检查接口存在及capabilities.modes，不能仅凭文档或方法名启用。

## 责任边界

Grok OAuth负责账户、输入来源校验、受作用域保护的媒体、上游能力验证、幂等提交、查询、下载和最多20 MiB的视频读取。Matoi负责原消息和发送者权限、人设选择、群额度、持久业务任务、等待调度、状态通知、QQ发送及重发。Grok插件不向群发送视频或进度，不读取Matoi私有状态，也不实现其额度策略。

## 能力及真实支持结果

| 模式 | 0.8.2 行为 | 验证 |
| --- | --- | --- |
| `image_to_video` | 单图片资产，6或10秒，480p档位 | 既有OAuth真实生成通过 |
| `text_to_video` | 单次文字请求，提示词原样发送，无额外图片接口调用 | OAuth 实测 6 秒视频完成 |
| `video_edit` | 一个同账户、同conversation的视频资产；接受自产或新导入来源 | 自产编辑及QQ外部来源导入后编辑均实测通过 |
| `external_video_import` | MP4 data URI、严格HTTPS来源或明确配置目录；最大20 MiB、8.7秒 | 真实 QQ 短视频附件导入成功 |
| `video_edit_with_images` | 明确拒绝，提交状态not_submitted，不丢弃输入或切换模式 | 上游文档明确不允许参考图模式与视频编辑组合；拒绝路径零请求测试 |

“来源视频＋人设参考图”即最后一种联合输入。官方的reference-to-video是参考图片生成新视频，不能据此将已有来源视频的联合编辑声明为可用。不得用首帧、截图或重新图生视频冒充视频编辑。[官方参考视频文档](https://docs.x.ai/developers/model-capabilities/video/reference-to-video)

文生视频在本插件侧只有一次 `/videos/generations` 提交，模型为grok-imagine-video-1.5。上游可能内部生成首帧，这是单请求模型内部实现；本插件不调用图片生成、不改写提示词、不进行供应商回退。[官方文生视频文档](https://docs.x.ai/developers/model-capabilities/video/generation)

编辑使用grok-imagine-video，输入最多8.7秒；按上游契约保留时长和比例，输出分辨率最高720p。本插件不会向编辑请求额外加入duration、resolution等不支持参数。[官方编辑文档](https://docs.x.ai/developers/model-capabilities/video/editing)

## 公共方法

保留原六个方法及参数：import_video_image、submit_video、edit_video、get_video_job、wait_video_job、read_video_bytes。新增以下四个异步方法，均从明确选中的同一个Grok Provider实例调用：

```python
caps = await provider.get_video_capabilities(event=event)
job = await provider.submit_text_video(
    prompt, event=event, operation_key=stable_key, duration=6
)
video_asset_id = await provider.import_video_source(reference, event=event)
job = await provider.edit_video_with_images(
    prompt, video_asset_id, ordered_image_asset_ids,
    event=event, operation_key=stable_key,
)
```

最后一种方法保留了交接中的接口形状，但当前总是抛出VideoUnsupported；必须在调用前从modes中确认支持，不应先预留和提交一个已声明不支持的模式。

`get_video_capabilities()`不刷新凭据、不发送网络请求；包含schema_version、available、enabled、authorization、account_entitlement、implemented_modes、modes、unsupported_modes、durations、resolution、max_reference_images、max_video_bytes、max_edit_duration、mode_constraints和delivery_owner。modes反映实现及当前本地配置/绑定状态；`account_entitlement="unknown"`明确表示未实时查询订阅额度或权限，不保证下一请求一定被上游接受。未授权或禁用时modes为空。具体模式限制以mode_constraints为准，不能将图生视频的参数用于编辑。

公共API只读取事件的`get_platform_id()`与`unified_msg_origin`，conversation由本插件持有的宿主context查询。无需读取Matoi的event extra、message_obj或人设状态。Matoi必须传原始事件、保存冻结的conversation，并在调用及发送前比较会话仍一致。同进程插件属于受信调用方，此接口不能验证任意伪造事件的出处。

## 外部视频来源

首选将当前消息或引用消息解析得到的真实QQ视频HTTPS URL传给import_video_source。内置支持的QQ临时视频域名是`multimedia.nt.qq.com.cn`。配置现有出站proxy时，只有这一固定HTTPS来源域名使用专用无凭据代理通道；拒绝重定向、用户信息、片段及非443端口，保持TLS验证。

其他来源仅接受video_source_hosts中明确列出的HTTPS域名，所有DNS地址必须是公网并固定连接地址；每次重定向重新验证。来源下载不会自动复用vidgen.x.ai结果下载权限，不携带OAuth、Cookie或客户端认证。

也可传规范的`data:video/mp4;base64,...`，在解码前后限制大小。调用方必须自行确认它来自真实授权附件，不能把模型编造的URL或数据当附件。

本地路径默认全部拒绝。仅allowed_video_roots明确授权的目录可导入，逐段拒绝符号链接和路径逃逸；不会继承图片目录权限。不要扩大任意路径白名单来绕过QQ附件解析。

导入检查MP4结构、影片与全部轨道声明时长、视频轨道和大小，绑定同账户及同会话后返回资产ID；这些检查不等同逐帧解码或内容审核。QQ链接可能过期，过期后应重新解析原消息获取有效链接，不自动提交生成。

```python
caps = await provider.get_video_capabilities(event=event)
if not {"external_video_import", "video_edit"} <= set(caps["modes"]):
    raise RuntimeError("Required video modes are unavailable")
asset_id = await provider.import_video_source(real_message_video_url, event=event)
# 持久保存asset_id、prompt原文、stable_key；相同业务操作由调用方串行处理。
job = await provider.edit_video(
    original_prompt, asset_id, event=event, operation_key=stable_key
)
# 保存job_id后由调用方调度wait_video_job/get_video_job，再read_video_bytes并发送。
```

## 任务、异常与额度预留

正常返回保留job_id、asset_id、status、model、duration、error、delivery_owner，新增submission_state。Agent工具返回JSON字符串，Provider方法返回dict。Grok插件的delivery_owner始终为caller。

| submission_state | 精确定义 | 调用方处理原则 |
| --- | --- | --- |
| `not_submitted` | 输入/能力/授权准备阶段拒绝，或上游明确拒绝接受任务 | 可按Matoi自身规则释放预留；修正输入后是否新建操作由Matoi决定 |
| `submitted` | 已知上游request_id，远端任务存在 | 继续查询同一job_id；生成失败不等于没有提交或没有计费 |
| `unknown` | 请求可能已提交，未取得可靠确认或恢复信息 | 不自动释放后再次提交，不使用新操作键盲目重试 |

视频提交的类型化异常提供submission_state；创建过本地任务时operation_id为可查询的本地job_id。即使本地保存失败，也不能把已提交/未知降级成not_submitted。调用方遇到未知异常或缺失字段时应保守处理，不自行推断“未提交”。普通查询、导入、读取失败不构成新的生成提交。

同一Provider、同一conversation、相同operation_key与相同参数复用同一任务，不同参数拒绝。必须保存最初的参考资产ID，不能为重试重新导入图片/视频并替换ID。持续磁盘故障同时发生进程退出时仍可能失去远端句柄；保留的未知记录用于防止重复提交，不能承诺一定可恢复远端结果。

旧任务无需迁移：有远端ID推断submitted，无法确认的旧记录保守为unknown。新增元数据字段为增量字段，回退源码保留任务和当前OAuth凭据，不删除或重建状态目录。

## 发送和联调

生成成功只返回任务与资产。调用方使用read_video_bytes获取受控MP4，再调用自己的发送代码。已验证QQ群渠道及可复制示例见[qq_video_sender.py](../examples/qq_video_sender.py)和[开发API](API.md#已验证的qq发送方法)。发送超时/取消可能已送达，不能因此重新生成。

Matoi 的额度预留、人设授权、后台调度、管理命令和具体接线由 Matoi 自身实现。联调前核对实际安装版本、方法存在和 capabilities；Grok 插件的生成成功不代表调用方已完成发送。

## 版本兼容

当前版本提供上述四个公共方法，无新增依赖，无强制持久数据迁移。使用 AstrBot 单插件更新流程升级，保留插件配置、OAuth 凭据及任务目录；升级后先检查能力声明再开放新模式。使用旧版本时，调用方应根据方法存在和 modes 退回对应能力范围。不要删除任务记录来重试状态未知的生成。
