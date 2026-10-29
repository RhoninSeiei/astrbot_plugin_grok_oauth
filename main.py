"""AstrBot plugin entry, private administrator commands and image tools."""

import asyncio
import json
from pathlib import Path

from .astrbot_adapter.compat import (
    AstrBotConfig,
    AstrMessageEvent,
    Context,
    Star,
    TextPart,
    ToolSet,
    detach_owned_providers,
    filter,
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
    install_owned_tools,
    migrate_legacy_provider_sources,
    remove_owned_tools,
    restore_owned_providers,
)
from .astrbot_adapter.media_delivery import MediaDelivery
from .astrbot_adapter.provider import GrokOAuthProvider
from .astrbot_adapter.registration import register_provider, unregister_provider
from .astrbot_adapter.registry_state import bind_runtime, clear_runtime
from .astrbot_adapter.runtime import GrokRuntime, _finish_owned_operation
from .astrbot_adapter.search_tools import SearchToolService
from .astrbot_adapter.tools import TOOL_NAMES, ImageToolService
from .astrbot_adapter.usage_tools import (
    UsageToolService,
    format_usage,
    format_usage_breakdown,
    require_usage_access,
)
from .astrbot_adapter.video_tools import VideoToolService
from .astrbot_adapter.web_api import GrokWebAPI
from .grok_oauth.errors import GrokOAuthError, PermissionDenied


class GrokOAuthPlugin(Star):
    name = "astrbot_plugin_grok_oauth"

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.runtime = None
        self.image_tools = None
        self.video_tools = None
        self.search_tools = None
        self.usage_tools = None
        self.web_api = None
        self.registration = None
        self._tools = []
        self._lifecycle = asyncio.Lock()
        self._close_task = None

    async def initialize(self):
        async with self._lifecycle:
            if self.runtime and not self.runtime.closed:
                return
            root = Path(get_astrbot_plugin_data_path()) / self.name
            roots = [
                Path(get_astrbot_temp_path()),
                *map(Path, self.config.get("allowed_image_roots", [])),
            ]
            roots[0].mkdir(parents=True, exist_ok=True)
            self.runtime = GrokRuntime(
                self.config,
                root,
                allowed_roots=roots,
                host_context=self.context,
            )
            try:
                await self.runtime.open()
                bind_runtime(self.runtime)
                self.registration = register_provider(self.runtime.owner_id, GrokOAuthProvider)
                delivery = MediaDelivery(root / "outbox.json", self.runtime.assets)
                self.image_tools = ImageToolService(self.context, self.runtime, delivery)
                self.video_tools = VideoToolService(self.context, self.runtime, self.image_tools)
                self.search_tools = SearchToolService(self.context, self.runtime)
                self.usage_tools = UsageToolService(self.context, self.runtime)
                self._tools = (
                    self.image_tools.build_tools()
                    + self.video_tools.build_tools()
                    + self.search_tools.build_tools()
                    + self.usage_tools.build_tools()
                )
                install_owned_tools(self.context, self._tools)
                self.web_api = GrokWebAPI(self.context, self.runtime, logger=self.logger)
                self.web_api.register()
                migrated = await migrate_legacy_provider_sources(self.context)
                if migrated:
                    self.logger.info(
                        "Normalized %d Grok models for native provider-source configuration",
                        migrated,
                    )
                await restore_owned_providers(self.context, self.runtime)
                self.logger.info("Grok OAuth provider and control endpoints initialized")
            except BaseException:
                await _finish_owned_operation(self._cleanup())
                raise

    async def terminate(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._terminate(), name="grok-plugin-close")
        await _finish_owned_operation(self._close_task)

    async def _terminate(self):
        async with self._lifecycle:
            await self._cleanup()

    async def _cleanup(self):
        errors = []
        if self.runtime:
            try:
                await detach_owned_providers(self.context, self.runtime)
            except Exception as error:
                errors.append(type(error).__name__)
        if self.video_tools:
            try:
                await self.video_tools.close()
            except Exception as error:
                errors.append(type(error).__name__)
        if self.image_tools:
            try:
                await self.image_tools.close()
            except Exception as error:
                errors.append(type(error).__name__)
        if self.search_tools:
            await self.search_tools.close()
        if self.usage_tools:
            await self.usage_tools.close()
        if self.web_api:
            self.web_api.unregister()
        remove_owned_tools(self.context, self._tools)
        if self.runtime:
            try:
                await self.runtime.close()
            except Exception as error:
                errors.append(type(error).__name__)
            clear_runtime(self.runtime)
        if self.registration:
            unregister_provider(self.registration)
        if errors:
            raise RuntimeError("Grok plugin resource cleanup failed")

    @staticmethod
    def _admin(event):
        if not event.is_admin() or not event.is_private_chat():
            raise PermissionDenied("此命令仅允许管理员在私聊使用。")
        return f"{event.get_platform_id()}:{event.get_sender_id()}"

    @filter.command("grok_oauth_login")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def login(self, event: AstrMessageEvent, confirmation: str = ""):
        try:
            owner = self._admin(event)
            profile = self.runtime.client_description()
            if confirmation != "confirm":
                yield event.plain_result(
                    "客户端："
                    + profile["client_profile"]
                    + "\nID："
                    + profile["client_id"]
                    + "\n默认客户端来自公开 Grok CLI 参考实现，是否适用于本插件及当前账号需要实际确认。"
                    + "\n确认使用此客户端后，发送 /grok_oauth_login confirm。"
                )
                return
            result = await self.runtime.start_flow(
                owner,
                confirmed_client_id=profile["client_id"],
                client_profile=profile["client_profile"],
            )
            if result["status"] == "pending":
                yield event.plain_result(
                    "打开 "
                    + result["verification_uri"]
                    + "\n输入代码："
                    + result["user_code"]
                    + "\n完成后发送 /grok_oauth_status 查看结果。"
                )
            else:
                yield event.plain_result("授权状态：" + result["status"])
        except GrokOAuthError as error:
            yield event.plain_result("Grok OAuth：" + error.code)

    @filter.command("grok_oauth_usage")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def usage(self, event: AstrMessageEvent):
        try:
            require_usage_access(self.runtime, event)
            yield event.plain_result(format_usage(await self.runtime.get_usage()))
        except GrokOAuthError as error:
            yield event.plain_result("Grok 额度：" + error.code)

    @filter.command("grok_oauth_usage_breakdown")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def usage_breakdown(self, event: AstrMessageEvent):
        try:
            require_usage_access(self.runtime, event)
            yield event.plain_result(
                format_usage_breakdown(await self.runtime.get_usage_breakdown())
            )
        except GrokOAuthError as error:
            yield event.plain_result("Grok 额度消耗来源：" + error.code)

    @filter.command("grok_oauth_status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def status(self, event: AstrMessageEvent):
        try:
            result = self.runtime.status(self._admin(event))
            yield event.plain_result(
                "授权状态："
                + result["status"]
                + ("；" + result["error"] if result.get("error") else "")
            )
        except GrokOAuthError as error:
            yield event.plain_result("Grok OAuth：" + error.code)

    @filter.command("grok_oauth_cancel")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def cancel(self, event: AstrMessageEvent):
        try:
            owner = self._admin(event)
            state = self.runtime.status(owner)
            if state.get("flow_id"):
                result = await self.runtime.cancel_flow(owner, state["flow_id"])
                yield event.plain_result("授权状态：" + result["status"])
            else:
                yield event.plain_result("当前没有设备码授权流程。")
        except GrokOAuthError as error:
            yield event.plain_result("Grok OAuth：" + error.code)

    @filter.command("grok_oauth_disconnect")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def disconnect(self, event: AstrMessageEvent):
        try:
            await self.runtime.disconnect(self._admin(event))
            yield event.plain_result("Grok OAuth 绑定已解除，图片缓存保留。")
        except GrokOAuthError as error:
            yield event.plain_result("Grok OAuth：" + error.code)

    @filter.command("grok_oauth_test")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def test_chat(self, event: AstrMessageEvent, provider_id: str):
        try:
            self._admin(event)
            provider = self.context.get_provider_by_id(provider_id)
            if not isinstance(provider, GrokOAuthProvider) or provider._runtime is not self.runtime:
                raise PermissionDenied()
            response = await provider.text_chat(prompt="Reply PONG only.")
            yield event.plain_result("Grok 测试成功，响应 ID：" + str(response.id))
        except GrokOAuthError as error:
            yield event.plain_result("Grok OAuth：" + error.code)

    @filter.command("grok_image_resend")
    async def resend(self, event: AstrMessageEvent, operation_id: str):
        try:
            await self.image_tools.resolve_provider(event)
            scope = await self.image_tools.scope(event)
            result = await self.image_tools.delivery.resend(operation_id, scope=scope, event=event)
            yield event.plain_result("图片发送状态：" + result["status"])
        except GrokOAuthError as error:
            yield event.plain_result("Grok 图片：" + error.code)

    @filter.command("grok_video_status")
    async def video_status(self, event: AstrMessageEvent, job_id: str):
        result = await self.video_tools.status(event, job_id)
        yield event.plain_result("Grok 视频任务：" + result)

    @filter.on_llm_request()
    async def prepare_tools(self, event: AstrMessageEvent, req):
        await self._prepare_media_search_tools(event, req)
        if self.video_tools:
            await self.video_tools.prepare(event, req)
        if self.usage_tools:
            await self.usage_tools.prepare(event, req)

    async def _prepare_media_search_tools(self, event, req):
        if self.search_tools:
            await self.search_tools.prepare(event, req)
        if not req.func_tool:
            return
        try:
            await self.image_tools.resolve_provider(event)
        except GrokOAuthError:
            # Copy the request's set; never deactivate a global tool for others.
            req.func_tool = ToolSet(
                [tool for tool in req.func_tool.tools if tool.name not in TOOL_NAMES]
            )
            return
        if not any(tool.name in TOOL_NAMES for tool in req.func_tool.tools):
            return
        try:
            assets = await self.image_tools.import_message_images(event)
            event.set_extra(
                "grok_imported_image_assets",
                {"scope": await self.image_tools.scope(event), "asset_ids": list(assets)},
            )
        except GrokOAuthError as error:
            req.extra_user_content_parts.append(
                TextPart(text="Grok 图片附件无法导入：" + error.code)
            )
            return
        if assets:
            req.extra_user_content_parts.append(
                TextPart(text="当前会话可编辑图片资产，按附件顺序：" + json.dumps(assets))
            )
