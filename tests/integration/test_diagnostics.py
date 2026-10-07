import asyncio
import json
import stat
import threading
from types import SimpleNamespace

import pytest
from astrbot_plugin_grok_oauth.astrbot_adapter.diagnostics import TransportDiagnostics

TEMPLATE = "grok_oauth_transport_diag %s"


def emit(sink, **extra):
    sink.info(
        TEMPLATE, json.dumps({"operation": "json", "outcome": "success", "status": 200, **extra})
    )


def recorder():
    records, warnings = [], []
    return (
        SimpleNamespace(
            info=lambda template, value: records.append(json.loads(value)),
            warning=lambda value: warnings.append(value),
        ),
        records,
        warnings,
    )


@pytest.mark.parametrize(
    "native,file", [(False, False), (True, False), (False, True), (True, True)]
)
async def test_switches_redaction_permissions_and_close(tmp_path, native, file):
    logger, records, warnings = recorder()
    sink = TransportDiagnostics(
        tmp_path, native_enabled=native, file_enabled=file, native_logger=logger
    )
    emit(
        sink,
        elapsed_ms=4,
        body_bytes=10,
        headers_received=True,
        provider_id="grok_oauth/grok-4.7",
        actual_model="grok-4.7",
        cause="ReadTimeout",
        request_id="https://x.ai/?token=secret",
        response_id="eyJ.secret.token",
        url="https://x.ai/?token=secret",
        token="secret",
        headers={"Authorization": "secret"},
        body="secret",
        error="secret",
        configured_model="xai-secret",
    )
    await sink.close()
    emit(sink)
    await sink.close()
    path = tmp_path / "debug/transport.jsonl"
    assert len(records) == int(native)
    assert path.exists() == file
    if file:
        text = path.read_text()
        result = json.loads(text)
        assert "secret" not in text
        assert result["provider_id"] == "grok_oauth/grok-4.7"
        assert result["cause"] == "ReadTimeout"
        assert result["elapsed_ms"] == 4
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert len(text.splitlines()) == 1
        if native:
            assert result.pop("timestamp_utc").endswith("+00:00")
            assert records == [result]
    assert not warnings


async def test_rotation_has_single_private_backup(tmp_path):
    logger, _, _ = recorder()
    sink = TransportDiagnostics(
        tmp_path, native_enabled=False, file_enabled=True, native_logger=logger
    )
    folder = tmp_path / "debug"
    folder.mkdir()
    path = folder / "transport.jsonl"
    path.write_bytes(b" " * (1024 * 1024))
    emit(sink, elapsed_ms=1)
    await sink.close()
    backup = folder / "transport.jsonl.1"
    assert backup.stat().st_size == 1024 * 1024
    assert json.loads(path.read_text())["elapsed_ms"] == 1
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    sink = TransportDiagnostics(
        tmp_path, native_enabled=False, file_enabled=True, native_logger=logger
    )
    path.write_bytes(b" " * (1024 * 1024))
    emit(sink, elapsed_ms=2)
    await sink.close()
    assert sorted(p.name for p in folder.iterdir()) == ["transport.jsonl", "transport.jsonl.1"]
    assert json.loads(path.read_text())["elapsed_ms"] == 2


@pytest.mark.parametrize("target", ["root", "debug", "transport.jsonl", "transport.jsonl.1"])
async def test_symlinks_are_rejected_without_disclosing_exception(tmp_path, target):
    logger, records, warnings = recorder()
    outside = tmp_path / "outside"
    outside.mkdir()
    private = outside / "private"
    private.write_text("DO-NOT-CHANGE")
    root = tmp_path / "root"
    root.mkdir()
    if target == "root":
        root.rmdir()
        root.symlink_to(outside, target_is_directory=True)
    elif target == "debug":
        (root / "debug").symlink_to(outside, target_is_directory=True)
    else:
        (root / "debug").mkdir()
        (root / "debug" / target).symlink_to(private)
    sink = TransportDiagnostics(root, native_enabled=True, file_enabled=True, native_logger=logger)
    emit(sink)
    emit(sink)
    await sink.close()
    assert len(records) == 2
    assert len(warnings) == 1
    assert "DO-NOT-CHANGE" not in warnings[0]
    assert private.read_text() == "DO-NOT-CHANGE"
    assert not (outside / "transport.jsonl").exists()


async def test_native_failure_does_not_prevent_file_output(tmp_path):
    def broken(*args):
        raise RuntimeError("secret exception")

    sink = TransportDiagnostics(
        tmp_path,
        native_enabled=True,
        file_enabled=True,
        native_logger=SimpleNamespace(info=broken, warning=broken),
    )
    emit(sink)
    await sink.close()
    assert json.loads((tmp_path / "debug/transport.jsonl").read_text())["outcome"] == "success"


async def test_invalid_input_and_failures_do_not_escape(tmp_path, monkeypatch):
    logger, records, warnings = recorder()
    sink = TransportDiagnostics(
        tmp_path, native_enabled=True, file_enabled=True, native_logger=logger
    )
    for value in ("invalid-json", "[]", "{}", json.dumps({"token": "secret"}), " " * 8193):
        sink.info(TEMPLATE, value)
    sink.info("unsafe %s", '{"operation":"json"}')
    assert not records

    def broken(data):
        raise OSError("secret signed URL")

    monkeypatch.setattr(sink, "_append", broken)
    emit(sink)
    emit(sink)
    await sink.close()
    assert len(records) == 2
    assert len(warnings) == 1
    assert "secret" not in warnings[0]


async def test_bounded_queue_background_io_and_close_drain(tmp_path, monkeypatch):
    logger, _, warnings = recorder()
    sink = TransportDiagnostics(
        tmp_path, native_enabled=False, file_enabled=True, native_logger=logger
    )
    entered = threading.Event()
    release = threading.Event()
    original = sink._append

    def blocked(data):
        entered.set()
        assert release.wait(5)
        original(data)

    monkeypatch.setattr(sink, "_append", blocked)
    emit(sink, elapsed_ms=0)
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    # Worker file I/O is blocked in a thread; the event loop keeps accepting input.
    for number in range(300):
        emit(sink, elapsed_ms=number + 1)
    assert sink._queue.qsize() == sink._queue.maxsize == 128
    assert len(warnings) == 1
    closer = asyncio.create_task(sink.close())
    await asyncio.sleep(0)
    assert not closer.done()
    emit(sink, elapsed_ms=999)
    release.set()
    await closer
    results = [
        json.loads(line) for line in (tmp_path / "debug/transport.jsonl").read_text().splitlines()
    ]
    assert len(results) == 129
    assert [item["elapsed_ms"] for item in results] == list(range(129))
    assert sink._task.done()
    assert sink._queue.empty()


async def test_cancelled_and_concurrent_close_still_drains(tmp_path, monkeypatch):
    logger, _, _ = recorder()
    sink = TransportDiagnostics(
        tmp_path, native_enabled=False, file_enabled=True, native_logger=logger
    )
    entered = threading.Event()
    release = threading.Event()
    original = sink._append

    def blocked(data):
        entered.set()
        assert release.wait(5)
        original(data)

    monkeypatch.setattr(sink, "_append", blocked)
    emit(sink)
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    for _ in range(sink._queue.maxsize):
        emit(sink)
    assert sink._queue.full()
    first = asyncio.create_task(sink.close())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(sink.close())
    release.set()
    await second
    assert len((tmp_path / "debug/transport.jsonl").read_text().splitlines()) == 129
    assert sink._queue.empty()
    assert sink._task.done()
