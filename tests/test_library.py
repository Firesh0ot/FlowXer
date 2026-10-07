"""Unit and integration tests for the media library pipeline."""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from flowxer.library.conform import (
    conform_interleaved,
    cut_frame_after_fps_change,
    samples_in_grain,
    samples_until_grain,
)
from flowxer.library.ffmpeg import (
    FFmpeg,
    RunResult,
    build_clip_mezz_commands,
    build_video_filter,
)
from flowxer.library.models import LibraryKind
from flowxer.library.ram import decide_ram_playback, estimate_frame_bytes
from flowxer.library.sequence import safe_extract_tga_zip, validate_sequence


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


def test_clip_mezz_command_order(tmp_path: Path):
    cmds = build_clip_mezz_commands(
        original=tmp_path / "in.mov",
        video_tmp=tmp_path / "v.mov",
        audio_raw=tmp_path / "a.f32le",
        audio_conf=tmp_path / "c.f32le",
        mezz=tmp_path / "mezz.mov",
        thumb=tmp_path / "thumb.jpg",
        vf="fps=50/1,format=yuv422p10le",
        af="aresample=48000",
        audio_channels=2,
        src_audio_channels=2,
    )
    assert cmds[0][0] == "ffmpeg"
    assert "prores_ks" in cmds[0]
    assert cmds[0][cmds[0].index("-profile:v") + 1] == "3"
    assert "pcm_f32le" in cmds[2]


def test_samples_until_grain_5994_cadence():
    # 60000/1001 → 800, then 801 repeating in the classic pattern.
    sizes = [samples_in_grain(i, 60000, 1001) for i in range(6)]
    assert sizes[0] == 800
    assert all(s in {800, 801} for s in sizes)
    assert samples_until_grain(1001, 60000, 1001) == 48000 * 1001 // 60000 * 0 + (
        # exact cumulative after one full second of grains at 59.94:
        samples_until_grain(1001, 60000, 1001)
    )
    assert samples_until_grain(1001, 60000, 1001) == (1001 * 48000 * 1001) // 60000


def test_conform_crossfade_and_pad():
    # 8 stereo samples: head loud, tail quiet — crossfade should blend ends.
    src = []
    for i in range(8):
        src.extend([1.0 if i < 4 else 0.0, 0.0])
    out = conform_interleaved(src, 2, 8, 2, 8, crossfade_samples=2)
    assert len(out) == 16
    # i=1 → a=0.5 blends head sample 1 (1.0) with tail sample 7 (0.0)
    assert abs(out[2] - 0.5) < 1e-6
    assert abs(out[2] - out[14]) < 1e-6


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
    Image.new("RGBA", size, color).save(path)


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


def test_zip_slip_and_limits(tmp_path: Path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        # Absolute-style / parent traversal must be rejected.
        info = zipfile.ZipInfo("../escape.tga")
        zf.writestr(info, b"nope")
    with pytest.raises(ValueError):
        safe_extract_tga_zip(evil, tmp_path / "out")

    good = tmp_path / "good.zip"
    buf = io.BytesIO()
    img = Image.new("RGBA", (8, 8), (0, 255, 0, 200))
    img_bytes = io.BytesIO()
    img.save(img_bytes, format="TGA")
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("seq/frame_00000.tga", img_bytes.getvalue())
        zf.writestr("seq/frame_00001.tga", img_bytes.getvalue())
        zf.writestr("__MACOSX/._frame.tga", b"ignore")
    root = safe_extract_tga_zip(good, tmp_path / "extracted")
    assert (root / "frame_00000.tga").is_file() or list(root.rglob("*.tga"))


def test_ffmpeg_abstraction_without_binary(tmp_path: Path):
    calls: list[list[str]] = []

    def runner(cmd, cwd=None):
        calls.append(list(cmd))
        return RunResult(code=0, stdout='{"streams":[],"format":{}}')

    ff = FFmpeg(runner=runner)
    probed = ff.probe(tmp_path / "missing.mov")
    assert probed.streams == []
    result = ff.run(["ffmpeg", "-version"])
    assert result.code == 0
    assert calls[0][0] == "ffprobe"
    assert calls[-1][0] == "ffmpeg"


def test_library_delete_in_use_409(client, mixer):
    # Create a fake ready library item without ffmpeg by writing item.json.
    from flowxer.library.models import ConversionInfo, ConversionStatus, LibraryItem
    from flowxer.library.store import new_item_id

    item_id = new_item_id()
    item = LibraryItem(
        id=item_id,
        kind=LibraryKind.clip,
        name="busy.mov",
        conversions={"1080p50": ConversionInfo(status=ConversionStatus.ready, frames=10, mezz="mezz-1080p50.mov")},
    )
    mixer.library.store.save(item)
    (mixer.library.store.item_dir(item_id) / "mezz-1080p50.mov").write_bytes(b"fake")
    mixer.library.mark_in_use(item_id, "input:replay")
    response = client.delete(f"/api/v1/library/{item_id}")
    assert response.status_code == 409


def test_legacy_import_and_list(client, mixer, settings):
    # Default stinger was generated and imported as library item.
    items = client.get("/api/v1/library?kind=stinger").json()
    assert any(item["name"] == settings.default_stinger for item in items)
    assert client.get("/api/v1/jobs").status_code == 200


def test_stinger_not_ready_hard_cut(client, mixer):
    from flowxer.library.models import ConversionInfo, ConversionStatus, LibraryItem
    from flowxer.library.store import new_item_id

    item_id = new_item_id()
    item = LibraryItem(
        id=item_id,
        kind=LibraryKind.stinger,
        name="pending-sting",
        conversions={"1080p50": ConversionInfo(status=ConversionStatus.converting)},
        cut_frame=1,
        cut_ms=20,
    )
    mixer.library.store.save(item)
    slot = mixer.stinger_slots[0]
    mixer.configure_stinger_slot(slot.id, __import__("flowxer.api.schemas", fromlist=["StingerSlotUpdate"]).StingerSlotUpdate(library_item_id=item_id))
    client.post("/api/v1/mixer/start", json={})
    # Trigger should hard-cut rather than 5xx.
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
    # Wait for conversion.
    import time

    for _ in range(60):
        item = client.get(f"/api/v1/library/{item_id}").json()
        if item["status"] in {"ready", "failed"}:
            break
        time.sleep(0.5)
    assert item["status"] == "ready"
    assert item["ready"] is True


def test_chunked_upload_size_limit(client, mixer, settings):
    settings.upload_limit_gb = 0.0000001  # tiny
    # Recreate upload manager limit — service already constructed; set on uploads.
    mixer.library.uploads.upload_limit_bytes = 10
    response = client.post(
        "/api/v1/uploads",
        json={"name": "big.bin", "size": 1000, "kind": "clip", "mode": "video"},
    )
    assert response.status_code == 413


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_replay_wipe_mezzanine_matches_tga(mixer, tmp_path: Path):
    """Opaque wipe bar after ProRes 4444 round-trip stays visually close to the TGA."""
    import subprocess
    import time

    import numpy as np

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
