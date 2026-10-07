"""Caller-owned QQ delivery example; never loaded or invoked by Grok OAuth.

Copy into the calling plugin. Persist a 'sending' record before invocation;
never automatically retry an 'unknown' result or a cancelled invocation.
"""

import asyncio
import base64
import math

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Video

MAX_VIDEO_BYTES = 20 * 1024 * 1024


async def send_qq_video(context, original_umo, video_bytes, *, timeout_seconds=120):
    """Send once through the caller's Context and its existing OneBot connection.

    QQ group delivery with NapCat is live verified. Private-message transport
    shares this adapter but has not been live verified. Other adapters fail closed.
    'sent' means the platform call returned normally, not that a user played it.
    """
    if not isinstance(original_umo, str):
        raise ValueError("A captured original UMO is required")
    parts = original_umo.split(":", 2)
    if len(parts) != 3 or not all(parts) or parts[1] not in {"GroupMessage", "FriendMessage"}:
        raise ValueError("A QQ group or private-message UMO is required")
    platform = context.get_platform_inst(parts[0])
    if platform is None or platform.meta().name != "aiocqhttp":
        raise ValueError("This sender is verified for the aiocqhttp QQ adapter only")
    if not isinstance(video_bytes, bytes) or not 0 < len(video_bytes) <= MAX_VIDEO_BYTES:
        raise ValueError("Video must contain at most 20 MiB of bytes")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 180
    ):
        raise ValueError("Delivery timeout must be between 0 and 180 seconds")
    encoded = await asyncio.to_thread(lambda: base64.b64encode(video_bytes).decode("ascii"))
    chain = MessageChain(chain=[Video.fromBase64(encoded)])
    try:
        async with asyncio.timeout(timeout_seconds):
            accepted = await context.send_message(original_umo, chain)
    except asyncio.CancelledError:
        # The remote platform might have accepted it. Caller must retain unknown.
        raise
    except Exception:
        return {"status": "unknown", "reason": "DeliveryOutcomeUnknown"}
    if accepted is True:
        return {"status": "sent"}
    if accepted is False:
        return {"status": "rejected", "reason": "PlatformNotMatched"}
    return {"status": "unknown", "reason": "UnexpectedDeliveryResult"}
