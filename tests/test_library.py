"""Unit and integration tests for the media library pipeline."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from flowxer.api.schemas import (
    AudioEssence,
    InputKind,
    LogicalInputUpdate,
    StingerSlotUpdate,
    VideoEssence,
)
from flowxer.engine.mixer import VisionMixer
from flowxer.library import service as service_module
from flowxer.library.conform import (
    crossfade_loop_file,
    cut_frame_after_fps_change,
    samples_in_grain,
    samples_until_grain,
)
from flowxer.library.ffmpeg import (
    FFmpeg,
    RunResult,
    build_clip_audio_command,
    build_clip_video_command,
    build_still_command,
    build_still_filter,
    build_video_filter,
    conversion_threads,
    default_runner,
)
from flowxer.library.models import ConversionInfo, ConversionStatus, LibraryItem, LibraryKind
from flowxer.library.ram import decide_ram_playback, estimate_frame_bytes
from flowxer.library.sequence import safe_extract_tga_zip, validate_sequence
from flowxer.library.uploads import UploadManager
from flowxer.nmos import ids
from flowxer.nmos.http import create_nmos_app
from flowxer.settings import Settings

VIDEO_FLOW = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"
SENDER_ID = "dddddddd-dddd-dddd-dddd-ddddddddddd1"


def _ready_item(mixer: VisionMixer, item_id: str, kind: LibraryKind, name: str, ready: bool = True) -> None:
    """A library item as a finished (or still running) conversion leaves it, without ffmpeg."""
    store = mixer.library.store
    fmt = mixer.workspace.format_id
    item = LibraryItem(id=item_id, kind=kind, name=name, original={"path": "original.mov", "source_kind": "video"})
    if ready:
        mezz = store.mezz_path(item_id, fmt, kind)
        item.conversions[fmt] = ConversionInfo(
            status=ConversionStatus.ready, frames=20, audio_channels=2, mezz=mezz.name,
            audio=store.audio_path(item_id, fmt).name if kind == LibraryKind.clip else None,
        )
        store.item_dir(item_id).mkdir(parents=True, exist_ok=True)
        mezz.write_bytes(b"x")
        if kind == LibraryKind.clip:
            store.audio_path(item_id, fmt).write_bytes(b"\0" * 8)
    else:
        item.conversions[fmt] = ConversionInfo(status=ConversionStatus.converting, frames=20)
    store.save(item)


def _wait_legacy(mixer: VisionMixer) -> None:
    assert mixer.library.legacy_import_done.wait(10)


def test_build_video_filter_fit_and_bt601():
    vf = build_video_filter(
        width=1920,
        height=1080,
        fps_num=50,
        fps_den=1,
        fit="fit",
        fps_mode="drop",
        src_interlaced=True,
        color_space="smpte170m",
    )
    assert "bwdif=mode=send_frame" in vf
    assert "colorspace=all=bt709:iall=bt601" in vf
    assert "force_original_aspect_ratio=decrease" in vf
    assert "fps=50/1" in vf
    assert vf.endswith("format=yuv422p10le")


def test_build_video_filter_fill_motion():
    vf = build_video_filter(
        width=1280,
        height=720,
        fps_num=60000,
        fps_den=1001,
        fit="fill",
        fps_mode="motion",
    )
    assert "force_original_aspect_ratio=increase" in vf
    assert "minterpolate=fps=60000/1001" in vf


def test_clip_commands(tmp_path: Path):
    """The clip mezzanine is HEVC 4:4:4 10-bit (NVDEC has no 4:2:2), never 4:2:0: the 4:2:2
    picture widened by repeating each chroma sample; no B-frames, a key frame per second. NVENC
    with constant QP (decoding and scaling get the thread budget), else x265 with CRF (x265 gets
    the budget, decoding and scaling one thread each)."""
    common = dict(original=tmp_path / "in.mov", video_tmp=tmp_path / "v.mkv", vf="fps=50/1", keyint=50, threads=4)
    video = build_clip_video_command(**common, nvenc=True)
    assert video[0] == "ffmpeg"
    assert video[video.index("-c:v") + 1] == "hevc_nvenc"
    assert video[video.index("-profile:v") + 1] == "rext"
    assert video[video.index("-rc") + 1] == "constqp" and video[video.index("-qp") + 1] == "8"
    assert video[video.index("-bf") + 1] == "0" and video[video.index("-g") + 1] == "50"
    assert video[video.index("-vf") + 1] == "fps=50/1,scale=flags=neighbor,format=yuv444p16le"
    before_input = video[: video.index("-i")]
    # The last -threads/-filter_threads before the input win: half the budget each.
    assert before_input[len(before_input) - 1 - before_input[::-1].index("-threads") + 1] == "2"
    assert before_input[len(before_input) - 1 - before_input[::-1].index("-filter_threads") + 1] == "2"
    assert video[video.index("-protocol_whitelist") + 1] == "file"
    assert video[-3:] == ["-f", "matroska", str(tmp_path / "v.mkv")]

    video = build_clip_video_command(**common, nvenc=False)
    assert video[video.index("-threads") + 1] == "1" and video.index("-threads") < video.index("-i")
    assert video[video.index("-filter_threads") + 1] == "1"
    assert video.count("-threads") == 1 and video.count("-filter_threads") == 1
    assert video[video.index("-c:v") + 1] == "libx265"
    assert video[video.index("-profile:v") + 1] == "main444-10"
    assert video[video.index("-vf") + 1] == "fps=50/1,scale=flags=neighbor,format=yuv444p10le"
    params = video[video.index("-x265-params") + 1]
    assert "keyint=50:min-keyint=50:bframes=0" in params and "crf=8" in params and "pools=4" in params
    assert video[-3:] == ["-f", "matroska", str(tmp_path / "v.mkv")]


def test_nvenc_is_probed_once_and_x265_is_the_fallback(caplog):
    calls = []

    def runner(cmd, timeout=None, cancel=None):
        calls.append(cmd)
        return RunResult(code=1, stderr="[hevc_nvenc] Cannot load libnvidia-encode.so.1")

    ff = FFmpeg(runner=runner)
    with caplog.at_level("WARNING"):
        assert ff.has_nvenc() is False
        assert ff.has_nvenc() is False
    assert len(calls) == 1 and "hevc_nvenc" in calls[0]
    assert [r.getMessage() for r in caplog.records if "NVENC" in r.getMessage()] == [
        "NVENC cannot be opened ([hevc_nvenc] Cannot load libnvidia-encode.so.1): clip mezzanines are encoded with x265"
    ]
    assert FFmpeg(runner=lambda cmd, timeout=None, cancel=None: RunResult(code=0)).has_nvenc() is True


def test_conversion_threads_leave_the_engine_two_cores(tmp_path: Path):
    """A conversion must not fill the container's CPU limit: the whole container would be
    throttled, the engine with it."""
    assert conversion_threads(cgroup=tmp_path) == 8  # no cgroup file: no limit
    (tmp_path / "cpu.max").write_text("max 100000")
    assert conversion_threads(cgroup=tmp_path) == 8
    (tmp_path / "cpu.max").write_text("600000 100000")
    assert conversion_threads(cgroup=tmp_path) == 4
    assert conversion_threads(2, cgroup=tmp_path) == 2
    (tmp_path / "cpu.max").write_text("250000 100000")
    assert conversion_threads(cgroup=tmp_path) == 1


def test_still_command_and_filter(tmp_path: Path):
    vf = build_still_filter(width=1920, height=1080, fit="fit")
    assert vf.startswith("scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080")
    # An RGB image or a full-range JPEG becomes limited-range BT.709.
    assert vf.endswith("scale=out_color_matrix=bt709:out_range=tv,format=yuv422p10le")
    cmd = build_still_command(original=tmp_path / "in.png", still_tmp=tmp_path / "s.v210", vf=vf)
    assert cmd[cmd.index("-frames:v") + 1] == "1"
    assert cmd[cmd.index("-c:v") + 1] == "v210"
    assert cmd[-3:] == ["-f", "rawvideo", str(tmp_path / "s.v210")]
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file"


def test_player_media_of_a_clip_and_a_still(mixer):
    """The engine loads a clip's mezzanine with its sound file, channels and frames; a still's
    v210 picture."""
    fmt = mixer.workspace.format_id
    store = mixer.library.store
    _ready_item(mixer, "clipaaa00001", LibraryKind.clip, "clip.mov")
    _ready_item(mixer, "stilaaa00001", LibraryKind.still, "bars.png")
    mixer.update_input("clip-1", LogicalInputUpdate(library_item_id="clipaaa00001", loop=True))
    media = mixer._player_media(mixer.get_input("clip-1"))
    assert media == {
        "path": str(store.mezz_path("clipaaa00001", fmt, LibraryKind.clip)),
        "kind": "clip",
        "audio": str(store.audio_path("clipaaa00001", fmt)),
        "channels": 2,
        "frames": 20,
        "loop": True,
        "play_on_cut": False,
    }
    assert media["path"].endswith(f"mezz-{fmt}.mkv") and media["audio"].endswith(f"mezz-{fmt}.f32")
    mixer.update_input("clip-1", LogicalInputUpdate(library_item_id="stilaaa00001"))
    media = mixer._player_media(mixer.get_input("clip-1"))
    assert media["kind"] == "still" and media["path"].endswith(f"still-{fmt}.v210") and "audio" not in media


def test_a_clip_without_its_new_mezzanine_is_converted_again(settings: Settings, monkeypatch):
    """A clip converted before the engine (a ProRes mezz-<format>.mov) or whose files were deleted
    is converted again from its original at the next start; the original is kept."""
    mixer = VisionMixer(settings)
    _wait_legacy(mixer)
    fmt = mixer.workspace.format_id
    store = mixer.library.store
    item = LibraryItem(id="oldaaa000001", kind=LibraryKind.clip, name="old.mov", original={"path": "original.mov", "source_kind": "video"})
    item.conversions[fmt] = ConversionInfo(status=ConversionStatus.ready, frames=20, mezz=f"mezz-{fmt}.mov")
    store.item_dir(item.id).mkdir(parents=True)
    (store.item_dir(item.id) / "original.mov").write_bytes(b"original")
    (store.item_dir(item.id) / f"mezz-{fmt}.mov").write_bytes(b"prores")
    store.save(item)
    assert mixer.library.mezzanine_path(item.id) is None  # the engine cannot play ProRes
    mixer.library.stop()
    queued = []
    monkeypatch.setattr(
        service_module.ConversionQueue, "enqueue",
        lambda self, item_id, format_id, options=None, *, low_priority=False: queued.append((item_id, format_id, low_priority)),
    )
    again = VisionMixer(settings)
    assert ("oldaaa000001", fmt, True) in queued
    assert (store.item_dir(item.id) / "original.mov").read_bytes() == b"original"
    again.library.stop()


def test_colour_bars_are_seeded_once(settings: Settings):
    mixer = VisionMixer(settings)
    bars = [i for i in mixer.library.list_items(kind=LibraryKind.still) if i.name == "Colour bars"]
    assert len(bars) == 1 and bars[0].tags == ["bars"]
    original = mixer.library.store.item_dir(bars[0].id) / bars[0].original["path"]
    assert original.read_bytes() == service_module.COLOUR_BARS.read_bytes()
    mixer.library.delete(bars[0].id)
    mixer.library.stop()
    again = VisionMixer(settings)  # deleted by the user: not seeded again
    assert not [i for i in again.library.list_items(kind=LibraryKind.still) if i.name == "Colour bars"]
    again.library.stop()


def test_import_dir_image_becomes_a_still(mixer, tmp_path: Path):
    _wait_legacy(mixer)
    library = mixer.library
    import_dir = tmp_path / "import"
    import_dir.mkdir()
    (import_dir / "logo.png").write_bytes(b"png")
    before = set(library.store.list_ids())
    seen: dict = {}
    library._scan_import_dir(import_dir, seen)
    library._scan_import_dir(import_dir, seen)
    new = set(library.store.list_ids()) - before
    assert len(new) == 1
    assert library.get(new.pop()).kind == LibraryKind.still


def test_clip_audio_is_conformed_by_ffmpeg(tmp_path: Path):
    """The sound is padded or cut to the frame-exact sample count by ffmpeg (streamed to
    disk), not decoded into the mixer process."""
    cmd = build_clip_audio_command(
        original=tmp_path / "in.mov",
        audio_tmp=tmp_path / "a.f32le",
        af="aresample=48000",
        src_channels=6,
        dst_channels=2,
        samples=48048,
    )
    chain = cmd[cmd.index("-af") + 1]
    # Unknown layout: the first two channels.
    assert chain == "aresample=48000,pan=2c|c0=c0|c1=c1,apad=whole_len=48048,atrim=end_sample=48048"
    assert cmd[cmd.index("-map") + 1] == "0:a:0"
    assert cmd[-3:] == ["-f", "f32le", str(tmp_path / "a.f32le")]
    known = build_clip_audio_command(
        original=tmp_path / "in.mov",
        audio_tmp=tmp_path / "a.f32le",
        af="aresample=48000",
        src_channels=6,
        dst_channels=2,
        samples=960,
        src_layout="5.1",
    )
    # Known layout: a proper downmix.
    assert known[known.index("-af") + 1] == "aresample=48000,aformat=channel_layouts=stereo,apad=whole_len=960,atrim=end_sample=960"

    silence = build_clip_audio_command(
        original=None, audio_tmp=tmp_path / "a.f32le", af="x", src_channels=0, dst_channels=2, samples=960
    )
    assert "anullsrc=r=48000:cl=mono" in silence
    assert silence[silence.index("-af") + 1] == "pan=2c|c0=c0,apad=whole_len=960,atrim=end_sample=960"


def test_samples_until_grain_5994_cadence():
    # 60000/1001 → 800, then 801 repeating in the classic pattern.
    sizes = [samples_in_grain(i, 60000, 1001) for i in range(6)]
    assert sizes[0] == 800
    assert all(s in {800, 801} for s in sizes)
    assert samples_until_grain(1001, 60000, 1001) == (1001 * 48000 * 1001) // 60000


def test_crossfade_loop_file_blends_head_and_tail(tmp_path: Path):
    # 8 stereo samples: head loud, tail quiet — the crossfade blends the ends.
    src = []
    for i in range(8):
        src.extend([1.0 if i < 4 else 0.0, 0.0])
    path = tmp_path / "a.f32le"
    path.write_bytes(np.array(src, dtype="<f4").tobytes())
    assert crossfade_loop_file(path, 2, 8, 2)
    out = np.frombuffer(path.read_bytes(), dtype="<f4")
    assert len(out) == 16
    # i=1 → a=0.5 blends head sample 1 (1.0) with tail sample 7 (0.0)
    assert abs(out[2] - 0.5) < 1e-6
    assert abs(out[2] - out[14]) < 1e-6
    assert out[4] == 1.0  # the middle is untouched
    assert not crossfade_loop_file(path, 2, 8, 4)  # needs more than two windows


def test_starting_the_app_does_not_load_numpy():
    # numpy loaded before GStreamer made libmxl unwind with libunwind: the mixer crashed
    # (SIGSEGV) when Program started with a missing or unrouted MXL flow (lab, 9.16.39).
    code = "import sys, flowxer.app; print('numpy' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def test_cut_frame_after_fps_change_keeps_ms():
    frame, ms = cut_frame_after_fps_change(cut_ms=500, cut_frame=None, old_fps=25, new_fps=50, new_frame_count=100)
    assert frame == 25
    assert ms == 500
    frame2, _ = cut_frame_after_fps_change(cut_ms=None, cut_frame=10, old_fps=25, new_fps=50, new_frame_count=100)
    assert frame2 == 20


def test_ram_budget_fallback():
    ok = decide_ram_playback(
        frames=100,
        fps=50,
        width=1920,
        height=1080,
        has_alpha=True,
        ram_clip_max_s=20,
        ram_budget_bytes=4096 << 20,
    )
    assert ok.use_ram
    big = decide_ram_playback(
        frames=50 * 60,
        fps=50,
        width=1920,
        height=1080,
        has_alpha=True,
        ram_clip_max_s=20,
        ram_budget_bytes=64 << 20,
    )
    assert not big.use_ram
    assert estimate_frame_bytes(1920, 1080, has_alpha=True) == 1920 * 1080 * 4


def _write_tga(path: Path, color=(255, 0, 0, 128), size=(16, 9)) -> None:
    Image.new("RGBA", size, color).save(path, format="TGA")


def _tga_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (8, 8), (0, 255, 0, 200)).save(buffer, format="TGA")
    return buffer.getvalue()


def test_sequence_validation_gap_and_alpha(tmp_path: Path):
    _write_tga(tmp_path / "frame_00000.tga")
    _write_tga(tmp_path / "frame_00002.tga")
    info = validate_sequence(tmp_path)
    assert not info.ok
    assert any("gaps" in i.message for i in info.issues)

    shutil.rmtree(tmp_path)
    tmp_path.mkdir()
    for i in range(3):
        _write_tga(tmp_path / f"frame_{i:05d}.tga")
    info = validate_sequence(tmp_path)
    assert info.ok
    assert info.frame_count == 3
    assert info.has_alpha


def test_sequence_pattern_keeps_case_padding_and_start(tmp_path: Path):
    for i in range(98, 102):
        _write_tga(tmp_path / f"Sting_{i:02d}.TGA")
    info = validate_sequence(tmp_path)
    assert info.ok, info.issues
    assert info.pattern == "Sting_%02d.TGA"
    assert info.start_number == 98


def test_zip_slip_and_limits(tmp_path: Path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        # Absolute-style / parent traversal must be rejected.
        info = zipfile.ZipInfo("../escape.tga")
        zf.writestr(info, b"nope")
    with pytest.raises(ValueError):
        safe_extract_tga_zip(evil, tmp_path / "out")

    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("seq/frame_00000.tga", _tga_bytes())
        zf.writestr("seq/frame_00001.tga", _tga_bytes())
        zf.writestr("__MACOSX/._frame.tga", b"ignore")
    root = safe_extract_tga_zip(good, tmp_path / "extracted")
    assert (root / "frame_00000.tga").is_file() or list(root.rglob("*.tga"))


def test_ffmpeg_abstraction_without_binary(tmp_path: Path):
    calls: list[list[str]] = []

    def runner(cmd, timeout=None, cancel=None):
        calls.append(list(cmd))
        return RunResult(code=0, stdout='{"streams":[],"format":{}}')

    ff = FFmpeg(runner=runner)
    probed = ff.probe(tmp_path / "missing.mov")
    assert probed.streams == []
    result = ff.run(["ffmpeg", "-version"])
    assert result.code == 0
    assert calls[0][0] == "ffprobe"
    assert calls[-1][0] == "ffmpeg"


def test_default_runner_cancel_kills_the_process():
    import sys

    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    result = default_runner([sys.executable, "-c", "import time; time.sleep(30)"], cancel=cancel)
    assert result.cancelled
    assert time.monotonic() - started < 10


# ── finding 1: restart / config import keep library inputs and slots ─────────


def test_library_bindings_survive_restart_and_config_import(settings: Settings) -> None:
    settings.nmos_enable = True
    mixer = VisionMixer(settings)
    _wait_legacy(mixer)
    fmt = mixer.workspace.format_id
    _ready_item(mixer, "clipaaa00001", LibraryKind.clip, "clip one.mov")
    _ready_item(mixer, "stingaaa0001", LibraryKind.stinger, "My Sting.zip")
    mixer.update_input("clip-1", LogicalInputUpdate(library_item_id="clipaaa00001"))
    slot_id = mixer.stinger_slots[0].id
    slot = mixer.configure_stinger_slot(slot_id, StingerSlotUpdate(library_item_id="stingaaa0001", cut_frame=5))
    assert slot.stinger_id == "stingaaa0001"  # the item id, never its display name
    assert slot.ready and slot.cut_frame == 5
    mixer.update_input(
        "cam-1",
        LogicalInputUpdate(kind=InputKind.mxl_live, video=VideoEssence(), audio=AudioEssence()),
    )
    rid = ids.receiver_id(settings.resolved_nmos_seed, "cam-1", "video")
    nmos = TestClient(create_nmos_app(mixer.nmos))
    response = nmos.patch(
        f"/x-nmos/connection/v1.2/single/receivers/{rid}/staged",
        json={
            "sender_id": SENDER_ID,
            "master_enable": False,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_flow_id": VIDEO_FLOW}],
        },
    )
    assert response.status_code == 200, response.text
    mixer.persist()
    mixer.library.stop()

    saved = json.loads((settings.state_dir / "state.json").read_text(encoding="utf-8"))
    player = next(item for item in saved["inputs"] if item["id"] == "clip-1")
    assert player["library_item_id"] == "clipaaa00001" and "file_path" not in player
    assert "media_path" not in saved["stinger_slots"][0]

    again = VisionMixer(settings)
    mezz = again.library.store.mezz_path("clipaaa00001", fmt, LibraryKind.clip)
    assert again.get_input("clip-1").library_item_id == "clipaaa00001"
    assert again._player_media(again.get_input("clip-1"))["path"] == str(mezz)
    restored = again.get_stinger_slot(slot_id)
    assert restored.library_item_id == "stingaaa0001"
    assert restored.stinger_id == "stingaaa0001"
    assert restored.ready and restored.cut_frame == 5
    assert restored.media_path == str(again.library.store.mezz_path("stingaaa0001", fmt, LibraryKind.stinger))
    assert again.get_input("cam-1").kind == InputKind.mxl_live
    assert again.nmos.active(rid, "receivers")["transport_params"][0]["mxl_flow_id"] == VIDEO_FLOW
    # In use again after the restart: neither item can be deleted.
    for item_id in ("clipaaa00001", "stingaaa0001"):
        with pytest.raises(PermissionError):
            again.library.delete(item_id)

    client = TestClient(_app(settings, again))
    exported = client.get("/api/v1/config/export").json()
    client.patch("/api/v1/inputs/clip-1", json={"library_item_id": None})
    assert again._player_media(again.get_input("clip-1"))["path"] == ""
    response = client.post("/api/v1/config/import", json=exported)
    assert response.status_code == 200, response.text
    assert again._player_media(again.get_input("clip-1"))["path"] == str(mezz)
    assert again.get_stinger_slot(slot_id).ready
    again.library.stop()


def _app(settings: Settings, mixer: VisionMixer):
    from flowxer.app import create_app

    return create_app(settings, mixer)


def test_label_patch_on_library_slot_keeps_the_item(client, mixer):
    _ready_item(mixer, "stingaaa0001", LibraryKind.stinger, "My Sting.zip")
    slot_id = mixer.stinger_slots[0].id
    assert client.patch(f"/api/v1/stinger-slots/{slot_id}", json={"library_item_id": "stingaaa0001"}).status_code == 200
    response = client.patch(f"/api/v1/stinger-slots/{slot_id}", json={"label": "renamed", "cut_frame": 3})
    assert response.status_code == 200, response.text
    assert response.json()["library_item_id"] == "stingaaa0001"
    assert response.json()["cut_frame"] == 3
    assert mixer.library.get("stingaaa0001").cut_frame == 3
    # Back to a legacy stinger.
    response = client.patch(f"/api/v1/stinger-slots/{slot_id}", json={"stinger_id": mixer.settings.default_stinger})
    assert response.status_code == 200, response.text
    assert response.json()["library_item_id"] is None
    assert not mixer.library.is_in_use("stingaaa0001")


def test_library_delete_in_use_409(client, mixer):
    _ready_item(mixer, "busyaaa00001", LibraryKind.clip, "busy.mov")
    mixer.update_input("clip-1", LogicalInputUpdate(library_item_id="busyaaa00001"))
    response = client.delete("/api/v1/library/busyaaa00001")
    assert response.status_code == 409
    mixer.update_input("clip-1", LogicalInputUpdate(library_item_id=None))
    assert client.delete("/api/v1/library/busyaaa00001").status_code == 204
    assert client.get("/api/v1/library/busyaaa00001").status_code == 404


def test_invalid_library_ids_are_404(client):
    assert client.get("/api/v1/library/bad%20id").status_code == 404
    assert client.get("/api/v1/library/bad%20id/thumb.jpg").status_code == 404
    assert client.post("/api/v1/library/bad%20id/reconvert", json={}).status_code == 404


# ── finding 2: a stinger that is not ready cuts without recursion ────────────


def test_auto_stinger_sources_cut(client, mixer):
    # Stingers come back in P3: a source with an auto-stinger is taken with a cut.
    _ready_item(mixer, "clipaaa00001", LibraryKind.clip, "clip.mov")
    _ready_item(mixer, "stingbbb0002", LibraryKind.stinger, "pending", ready=False)
    slot_id = mixer.stinger_slots[0].id
    response = client.patch(f"/api/v1/stinger-slots/{slot_id}", json={"library_item_id": "stingbbb0002"})
    assert response.json()["ready"] is False
    for input_id in ("clip-1", "cam-1"):
        assert client.patch(f"/api/v1/inputs/{input_id}", json={"stinger_slot_id": slot_id}).status_code == 200
    client.patch("/api/v1/inputs/clip-1", json={"library_item_id": "clipaaa00001"})

    response = client.post("/api/v1/mixer/take", json={"input_id": "clip-1", "transition": "cut"})
    assert response.status_code == 200, response.text
    assert mixer.program_input_id == "clip-1"

    assert client.post("/api/v1/mixer/preview", json={"input_id": "cam-1"}).status_code == 200
    response = client.post("/api/v1/mixer/cut", json={})
    assert response.status_code == 200, response.text
    assert mixer.program_input_id == "cam-1"
    assert mixer.preview_input_id == "clip-1"  # flip-flop like a plain cut

def test_stinger_not_ready_hard_cut(client, mixer):
    _ready_item(mixer, "pendaaa00001", LibraryKind.stinger, "pending-sting", ready=False)
    slot = mixer.stinger_slots[0]
    mixer.configure_stinger_slot(slot.id, StingerSlotUpdate(library_item_id="pendaaa00001"))
    client.post("/api/v1/mixer/start", json={})
    response = client.post(
        "/api/v1/stinger/play",
        json={
            "stinger_id": "pending-sting",
            "target_input_id": mixer.list_inputs()[0].id,
            "flip_flop": True,
            "panel_id": mixer.panels[0].id,
        },
    )
    assert response.status_code == 200


def test_conversion_end_refreshes_inputs_and_slots(client, mixer):
    _ready_item(mixer, "clipccc00003", LibraryKind.clip, "late.mov", ready=False)
    client.patch("/api/v1/inputs/clip-1", json={"library_item_id": "clipccc00003"})
    assert mixer._player_media(mixer.get_input("clip-1"))["path"] == ""
    _ready_item(mixer, "clipccc00003", LibraryKind.clip, "late.mov")
    calls = []
    mixer.engine = type("Engine", (), {"ready": True})()
    mixer._engine_call = lambda method, path, body, client=None: calls.append((method, path, body))
    mixer._on_library_job(type("Job", (), {"state": "done", "format_id": mixer.workspace.format_id})())
    mixer.engine = None
    # The engine loads the clip as soon as it is converted.
    assert calls[-1][1] == "/player/1/load"
    assert calls[-1][2]["path"] == mixer._player_media(mixer.get_input("clip-1"))["path"] != ""

def test_gui_proxy_passes_upload_chunks():
    conf = (Path(__file__).parents[1] / "gui" / "nginx.conf").read_text(encoding="utf-8")
    block = re.search(r"location /api/v1/uploads \{(.*?)\}", conf, re.S)
    assert block, "uploads need their own location"
    assert "client_max_body_size 0;" in block.group(1)
    assert "proxy_request_buffering off;" in block.group(1)
    assert "flowxer-auth.inc" in block.group(1)


def test_sequence_upload_streams_frames_to_disk(client, mixer):
    files = [("files", (f"frame_{i:04d}.tga", _tga_bytes(), "image/x-tga")) for i in range(1, 4)]
    files.append(("files", ("notes.txt", b"ignored", "text/plain")))
    response = client.post(
        "/api/v1/uploads/sequence",
        data={"name": "Folder Sting", "sequence_fps": "25", "cut_frame": "1"},
        files=files,
    )
    assert response.status_code == 201, response.text
    item = mixer.library.get(response.json()["id"])
    assert item.original["start_number"] == 1
    assert item.original["pattern"] == "frame_%04d.tga"
    assert sorted(p.name for p in (mixer.library.store.item_dir(item.id) / "sequence").iterdir()) == [
        "frame_0001.tga",
        "frame_0002.tga",
        "frame_0003.tga",
    ]
    assert not any(mixer.library.uploads.root.iterdir())  # stage moved, nothing left

    mixer.library.uploads.upload_limit_bytes = 100
    response = client.post("/api/v1/uploads/sequence", data={"name": "big"}, files=files)
    assert response.status_code == 413
    assert not any(mixer.library.uploads.root.iterdir())


# ── finding 5: chunked uploads are enforced and complete by rename ───────────


def test_chunked_upload_size_limit(client, mixer, settings):
    mixer.library.uploads.upload_limit_bytes = 10
    response = client.post(
        "/api/v1/uploads",
        json={"name": "big.bin", "size": 1000, "kind": "clip", "mode": "video"},
    )
    assert response.status_code == 413


def test_chunked_upload_checks_every_chunk(client, mixer):
    uploads = mixer.library.uploads
    uploads.chunk_size = 4
    body = b"0123456789"  # chunks 4 + 4 + 2
    session = client.post("/api/v1/uploads", json={"name": "..", "size": len(body), "kind": "clip"}).json()
    upload_id = session["id"]
    assert session["chunk_size"] == 4
    put = lambda index, data: client.put(f"/api/v1/uploads/{upload_id}/chunks/{index}", content=data)  # noqa: E731
    assert put(0, b"x" * 20).status_code == 413  # longer than the chunk
    assert put(0, b"012").status_code == 422  # shorter than the chunk
    assert put(3, b"89").status_code == 422  # no such chunk
    assert put(999999, b"0123").status_code == 422
    assert put(0, b"0123").status_code == 200
    assert put(2, b"89").status_code == 200
    response = client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert response.status_code == 422 and "missing chunks: 1" in response.text
    assert put(1, b"4567").status_code == 200
    assert uploads.data_path(upload_id).stat().st_size == len(body)

    response = client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert response.status_code == 201, response.text
    item = mixer.library.get(response.json()["id"])
    original = mixer.library.store.item_dir(item.id) / item.original["path"]
    assert item.original["path"] == "original.bin"  # the name '..' is never used as a path
    assert original.read_bytes() == body
    assert not (uploads.root / upload_id).exists()  # moved, not copied
    # A retried complete returns the same item instead of a duplicate.
    again = client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert again.status_code == 201 and again.json()["id"] == item.id


def test_upload_sessions_expire_and_leftovers_are_removed(tmp_path: Path):
    root = tmp_path / "_uploads"
    (root / "old-session").mkdir(parents=True)
    (root / "old-session" / "data").write_bytes(b"x" * 100)
    uploads = UploadManager(root, 1 << 20, idle_s=0.0)
    assert not any(root.iterdir())  # data of an earlier process is gone
    session = uploads.create(name="a.mov", size=10, kind=LibraryKind.clip, mode="video")
    assert (root / session.id).is_dir()
    time.sleep(0.01)
    assert uploads.get(session.id) is None  # idle → dropped with its data
    assert not (root / session.id).exists()


def test_upload_needs_free_disk_space(client, mixer, monkeypatch):
    usage = shutil.disk_usage(mixer.library.uploads.root)
    monkeypatch.setattr(
        "flowxer.library.uploads.shutil.disk_usage",
        lambda _path: usage._replace(free=100 << 20),
    )
    response = client.post("/api/v1/uploads", json={"name": "a.mov", "size": 50 << 20, "kind": "clip"})
    assert response.status_code == 507


def test_zip_upload_is_checked_quickly_and_unpacked_by_the_job(client, mixer):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("seq/frame_00000.tga", _tga_bytes())
    data = buffer.getvalue()
    session = client.post(
        "/api/v1/uploads", json={"name": "s.zip", "size": len(data), "kind": "stinger", "mode": "zip"}
    ).json()
    assert client.put(f"/api/v1/uploads/{session['id']}/chunks/0", content=data).status_code == 200
    response = client.post(f"/api/v1/uploads/{session['id']}/complete")
    assert response.status_code == 201, response.text
    item = mixer.library.get(response.json()["id"])
    assert item.original == {"path": "original.zip", "source_kind": "zip"}

    broken = client.post("/api/v1/uploads", json={"name": "b.zip", "size": 9, "kind": "stinger", "mode": "zip"})
    upload_id = broken.json()["id"]
    client.put(f"/api/v1/uploads/{upload_id}/chunks/0", content=b"not a zip")
    before = set(mixer.library.store.list_ids())
    response = client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert response.status_code == 422 and "not a zip" in response.text
    assert set(mixer.library.store.list_ids()) == before
    assert len([p for p in mixer.library.store.root.iterdir() if not p.name.startswith("_")]) == len(before)


# ── finding 6: the import dir never retries a file ───────────────────────────


def test_import_dir_moves_failures_aside(mixer, tmp_path: Path):
    _wait_legacy(mixer)
    library = mixer.library
    import_dir = tmp_path / "import"
    import_dir.mkdir()
    (import_dir / "broken.zip").write_bytes(b"not a zip")
    (import_dir / "clip.mov").write_bytes(b"movie")
    before = set(library.store.list_ids())
    seen: dict = {}
    library._scan_import_dir(import_dir, seen)  # first sight: wait for a stable size
    assert (import_dir / "broken.zip").exists()
    library._scan_import_dir(import_dir, seen)
    assert (import_dir / ".failed" / "broken.zip").is_file()
    assert not (import_dir / "clip.mov").exists()
    new = set(library.store.list_ids()) - before
    assert len(new) == 1  # the clip; no directory left for the broken zip
    item = library.get(new.pop())
    assert item.name == "clip.mov"
    assert (library.store.item_dir(item.id) / "original.mov").read_bytes() == b"movie"
    dirs = {p.name for p in library.store.root.iterdir() if not p.name.startswith("_")}
    assert dirs == set(library.store.list_ids())
    library._scan_import_dir(import_dir, seen)
    library._scan_import_dir(import_dir, seen)
    assert set(library.store.list_ids()) - before == {item.id}


def test_read_only_import_dir_imports_each_file_once(mixer, tmp_path: Path, monkeypatch):
    _wait_legacy(mixer)
    library = mixer.library
    import_dir = tmp_path / "import"
    import_dir.mkdir()
    (import_dir / "broken.zip").write_bytes(b"not a zip")
    (import_dir / "clip.mov").write_bytes(b"movie")
    real_replace = os.replace

    def read_only(src, dst):
        if Path(src).parent == import_dir:
            raise PermissionError("read-only file system")
        return real_replace(src, dst)

    monkeypatch.setattr(service_module.os, "replace", read_only)
    before = set(library.store.list_ids())
    seen: dict = {}
    for _ in range(5):
        library._scan_import_dir(import_dir, seen)
    assert (import_dir / "clip.mov").exists() and (import_dir / "broken.zip").exists()
    assert len(set(library.store.list_ids()) - before) == 1  # the clip, once; the zip never retried
    dirs = {p.name for p in library.store.root.iterdir() if not p.name.startswith("_")}
    assert dirs == set(library.store.list_ids())


# ── finding 7: legacy import in the background, referenced in place ─────────


def test_legacy_import_runs_after_start_and_references_in_place(settings: Settings, monkeypatch):
    clip = settings.clips_dir / "sizzle.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"legacy clip")
    release = threading.Event()
    original_import = service_module.LibraryService.import_legacy

    def slow_import(self):
        release.wait(10)
        return original_import(self)

    monkeypatch.setattr(service_module.LibraryService, "import_legacy", slow_import)
    mixer = VisionMixer(settings)  # returns although the import has not run yet
    assert not mixer.library.legacy_import_done.is_set()
    release.set()
    _wait_legacy(mixer)

    items = [i for i in mixer.library.list_items(kind=LibraryKind.clip) if i.legacy_path == "sizzle.mp4"]
    assert len(items) == 1
    item = items[0]
    assert Path(item.original["path"]) == clip.resolve()  # referenced, not copied
    assert sorted(p.name for p in mixer.library.store.item_dir(item.id).iterdir() if p.is_file()) in (
        ["item.json"],
        ["convert.log", "item.json"],
    )
    stinger = next(i for i in mixer.library.list_items(kind=LibraryKind.stinger) if i.name == settings.default_stinger)
    mixer.library.stop()

    again = VisionMixer(settings)  # idempotent: nothing imported twice
    _wait_legacy(again)
    assert len([i for i in again.library.list_items() if i.legacy_path == "sizzle.mp4"]) == 1
    again.library.delete(item.id)
    again.library.delete(stinger.id)
    again.library.stop()
    assert clip.read_bytes() == b"legacy clip"  # the legacy file is never touched

    third = VisionMixer(settings)  # a deleted item is not imported again
    _wait_legacy(third)
    assert not [i for i in third.library.list_items() if i.legacy_path in {"sizzle.mp4", stinger.legacy_path}]
    third.library.stop()


def test_legacy_import_and_list(client, mixer, settings):
    _wait_legacy(mixer)
    items = client.get("/api/v1/library?kind=stinger").json()
    assert any(item["name"] == settings.default_stinger for item in items)
    assert client.get("/api/v1/jobs").status_code == 200


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_upload_and_convert_clip_with_ffmpeg(client, mixer, tmp_path: Path):
    # Tiny synthetic video via ffmpeg.
    src = tmp_path / "clip.mp4"
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x36:d=0.2",
            "-f",
            "lavfi",
            "-i",
            "sine=f=440:d=0.2",
            "-shortest",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    data = src.read_bytes()
    init = client.post(
        "/api/v1/uploads",
        json={"name": "clip.mp4", "size": len(data), "kind": "clip", "mode": "video", "options": {"fit": "fit"}},
    )
    assert init.status_code == 200
    upload_id = init.json()["id"]
    chunk = client.put(f"/api/v1/uploads/{upload_id}/chunks/0", content=data)
    assert chunk.status_code == 200
    done = client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert done.status_code == 201
    item_id = done.json()["id"]
    for _ in range(60):
        item = client.get(f"/api/v1/library/{item_id}").json()
        if item["status"] in {"ready", "failed"}:
            break
        time.sleep(0.5)
    assert item["status"] == "ready", item
    assert item["ready"] is True
    fmt = mixer.workspace.format_id
    stored = mixer.library.get(item_id).conversion_for(fmt)
    assert stored.audio_samples == samples_until_grain(stored.frames, 50, 1)
    # The mezzanine: HEVC 4:4:4 10-bit; the sound: raw float32 of exactly the clip's frames.
    store = mixer.library.store
    probe = FFmpeg().probe(store.mezz_path(item_id, fmt, LibraryKind.clip))
    video = next(s for s in probe.streams if s.codec_type == "video")
    assert (video.codec_name, video.pix_fmt) == ("hevc", "yuv444p10le")
    assert store.audio_path(item_id, fmt).stat().st_size == stored.audio_samples * stored.audio_channels * 4
    assert (store.item_dir(item_id) / "original.mp4").read_bytes() == data


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_upload_and_convert_still_with_ffmpeg(client, mixer, tmp_path: Path):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 18), (255, 0, 0)).save(buffer, format="PNG")
    data = buffer.getvalue()
    init = client.post("/api/v1/uploads", json={"name": "red.png", "size": len(data), "kind": "still", "mode": "video"})
    assert init.status_code == 200, init.text
    upload_id = init.json()["id"]
    assert client.put(f"/api/v1/uploads/{upload_id}/chunks/0", content=data).status_code == 200
    item_id = client.post(f"/api/v1/uploads/{upload_id}/complete").json()["id"]
    for _ in range(60):
        item = client.get(f"/api/v1/library/{item_id}").json()
        if item["status"] in {"ready", "failed"}:
            break
        time.sleep(0.5)
    assert item["status"] == "ready", item
    fmt = mixer.workspace.format_id
    still = mixer.library.store.mezz_path(item_id, fmt, LibraryKind.still)
    width, height = mixer.settings.width, mixer.settings.height
    raw = still.read_bytes()
    assert len(raw) == (width + 47) // 48 * 128 * height
    # Red in limited-range BT.709 10-bit: Y 250, Cb 409, Cr 960 (first v210 word: Cb, Y, Cr).
    word = int.from_bytes(raw[:4], "little")
    assert abs((word >> 10 & 1023) - 250) <= 2 and abs((word & 1023) - 409) <= 2 and abs((word >> 20 & 1023) - 960) <= 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_replay_wipe_mezzanine_matches_tga(mixer, tmp_path: Path):
    """Opaque wipe bar after ProRes 4444 round-trip stays visually close to the TGA."""
    import subprocess

    _wait_legacy(mixer)
    item = next(i for i in mixer.library.list_items() if i.name == mixer.settings.default_stinger)
    for _ in range(80):
        item = mixer.library.get(item.id)
        if item and item.is_ready(mixer.workspace.format_id):
            break
        time.sleep(0.1)
    assert item is not None and item.is_ready(mixer.workspace.format_id)
    mezz = mixer.library.mezzanine_path(item.id)
    assert mezz is not None
    cut = item.cut_frame or item.conversion_for(mixer.workspace.format_id).cut_frame or 0
    orig_path = mixer.settings.stingers_dir / mixer.settings.default_stinger / f"frame_{cut:05d}.tga"
    out = tmp_path / "decoded.png"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(mezz),
            "-vf",
            f"select=eq(n\\,{cut})",
            "-frames:v",
            "1",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    orig = Image.open(orig_path).convert("RGBA")
    dec = Image.open(out).convert("RGBA")
    if orig.size != dec.size:
        orig = orig.resize(dec.size, Image.Resampling.NEAREST)
    a = np.asarray(orig, dtype=np.int16)
    b = np.asarray(dec, dtype=np.int16)
    mask = a[:, :, 3] > 200
    mad = float(np.abs(a[mask][:, :3] - b[mask][:, :3]).mean())
    assert mad < 40, f"opaque RGB mean abs diff too high: {mad}"
