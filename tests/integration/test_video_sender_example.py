import asyncio
import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_grok_oauth.astrbot_adapter.compat import Video

SOURCE = Path(__file__).resolve().parents[2] / "examples" / "qq_video_sender.py"
spec = importlib.util.spec_from_file_location("qq_video_sender_example", SOURCE)
sender = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sender)


def context(send, *, platform="aiocqhttp"):
    return SimpleNamespace(
        get_platform_inst=lambda name: SimpleNamespace(meta=lambda: SimpleNamespace(name=platform)),
        send_message=send,
    )


async def test_sender_uses_standard_video_and_exact_captured_umo():
    calls = []

    async def send(umo, chain):
        calls.append((umo, chain))
        return True

    result = await sender.send_qq_video(context(send), "qq2:GroupMessage:123", b"fixture-video")
    assert result == {"status": "sent"}
    assert len(calls) == 1 and calls[0][0] == "qq2:GroupMessage:123"
    segment = calls[0][1].chain[0]
    assert isinstance(segment, Video)
    assert segment.file == "base64://" + base64.b64encode(b"fixture-video").decode()


@pytest.mark.parametrize(
    "outcome,expected", [(False, "rejected"), (None, "unknown"), ("raise", "unknown")]
)
async def test_sender_does_not_retry_uncertain_or_rejected_send(outcome, expected):
    calls = 0

    async def send(umo, chain):
        nonlocal calls
        calls += 1
        if outcome == "raise":
            raise RuntimeError("private-upstream-url")
        return outcome

    result = await sender.send_qq_video(context(send), "qq:GroupMessage:123", b"fixture")
    assert result["status"] == expected and calls == 1
    assert "private-upstream-url" not in str(result)


async def test_sender_timeout_is_unknown_and_cancel_propagates():
    entered = asyncio.Event()

    async def send(umo, chain):
        entered.set()
        await asyncio.Event().wait()

    result = await sender.send_qq_video(
        context(send), "qq:GroupMessage:123", b"fixture", timeout_seconds=0.01
    )
    assert result["status"] == "unknown"
    entered.clear()
    task = asyncio.create_task(
        sender.send_qq_video(context(send), "qq:GroupMessage:123", b"fixture")
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "kwargs",
    [
        {"original_umo": "missing"},
        {"original_umo": "qq:OtherMessage:123"},
        {"video_bytes": b""},
        {"video_bytes": "notbytes"},
        {"timeout_seconds": True},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 181},
    ],
)
async def test_sender_rejects_invalid_inputs_without_sending(kwargs):
    async def send(*args):
        raise AssertionError("must not send")

    args = dict(original_umo="qq:GroupMessage:123", video_bytes=b"fixture")
    args.update(kwargs)
    with pytest.raises(ValueError):
        await sender.send_qq_video(context(send), **args)


async def test_sender_rejects_other_platform_and_large_video(monkeypatch):
    async def send(*args):
        raise AssertionError("must not send")

    with pytest.raises(ValueError):
        await sender.send_qq_video(
            context(send, platform="telegram"), "tg:GroupMessage:123", b"fixture"
        )
    monkeypatch.setattr(sender, "MAX_VIDEO_BYTES", 2)
    with pytest.raises(ValueError):
        await sender.send_qq_video(context(send), "qq:GroupMessage:123", b"large")
