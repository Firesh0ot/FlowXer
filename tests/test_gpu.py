"""FLOWXER_GPU: media path selection, its contract, and the GPU pipeline description.

The GPU pipeline itself runs in tests/test_gst_media.py where a GPU is available
(those cases are skipped elsewhere)."""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from flowxer import app as app_module
from flowxer.api.metrics import render_prometheus
from flowxer.api.schemas import InputKind, LogicalInputUpdate, MixerStartRequest
from flowxer.engine import gpu
from flowxer.engine.mixer import VisionMixer
from flowxer.engine.pipeline import build_pipeline_description, stinger_bin_description
from flowxer.settings import Settings, get_settings

GL_ELEMENT = re.compile(r"\bgl(upload|download|shader|colorconvert|videomixerelement)\b")


def _description(mixer: VisionMixer, *, gpu_path: bool, use_cefsrc: bool = True) -> str:
    return build_pipeline_description(
        settings=mixer.settings,
        inputs=mixer.list_inputs(),
        overlay_url=mixer.overlay.url,
        overlay_enabled=True,
        output_video_flow_id="11111111-1111-1111-1111-111111111111",
        output_audio_flow_id="22222222-2222-2222-2222-222222222222",
        domain="/Volumes/mxl/out",
        use_mxl_sink=True,
        use_cefsrc=use_cefsrc,
        gpu=gpu_path,
    )


@pytest.fixture
def full_hd(mixer: VisionMixer) -> VisionMixer:
    mixer.settings.width = 1920
    mixer.settings.height = 1080
    mixer.update_input("cam-1", LogicalInputUpdate(kind=InputKind.mxl_live, group_hint="cam-1"))
    return mixer


def test_v210_words_travel_as_an_rgba_texture_of_a_quarter_stride() -> None:
    assert gpu.v210_stride(1920) == 5120
    assert gpu.proxy_width(1920) == 1280
    # 1280 is not a multiple of 48: the line is padded to 27 blocks.
    assert gpu.proxy_width(1280) == 864
    assert gpu.proxy_width(3840) == 2560


def test_every_glshader_gets_a_fragment(full_hd: VisionMixer) -> None:
    description = _description(full_hd, gpu_path=True)
    stinger = stinger_bin_description({"kind": "video", "media_path": "/s/a.mov", "path": "/s"}, full_hd.settings, gpu=True)
    names = re.findall(r"glshader name=(\S+)", description + "\n" + stinger)
    assert names
    for name in names:
        fragment = gpu.fragment_for(name, 1920)
        assert fragment is not None, name
        assert "gl_FragColor" in fragment
    assert gpu.fragment_for("comp", 1920) is None


def test_gpu_description_uploads_each_source_once_and_downloads_program_once(full_hd: VisionMixer) -> None:
    description = _description(full_hd, gpu_path=True)
    video_inputs = len(full_hd.list_inputs())
    # One upload per video source and one for the HTML keyer.
    assert description.count("glupload") == video_inputs + 1
    assert "glvideomixerelement name=comp background=transparent emit-signals=true" in description
    assert "compositor " not in description
    # MXL v210 goes up as its words and is unpacked on the GPU.
    assert (
        'capssetter replace=true caps="video/x-raw,format=RGBA,width=1280,height=1080,framerate=50/1,'
        'pixel-aspect-ratio=1/1,interlace-mode=progressive" ! identity drop-allocation=true '
        "! glupload ! glshader name=gpu_unpack_cam-1"
    ) in description
    # Program: packed to v210 words, downloaded once, labelled v210 for mxlsink.
    program = next(line for line in description.splitlines() if line.startswith("comp. !"))
    assert program.count("gldownload") == 1
    assert "glshader name=gpu_pack ! video/x-raw(memory:GLMemory),format=RGBA,width=1280,height=1080" in program
    assert 'capssetter replace=true caps="video/x-raw,format=v210,width=1920,height=1080' in program
    assert program.endswith('mxlsink name=vout flow-id=11111111-1111-1111-1111-111111111111 domain="/Volumes/mxl/out"')
    # Monitors: scaled on the GPU, only the small picture is downloaded.
    assert "videoconvertscale" not in description
    assert description.count("gldownload") == 1 + video_inputs + 1
    # The keyer is BGRA and converted on the GPU too.
    assert "! glupload ! glcolorconvert ! video/x-raw(memory:GLMemory),format=RGBA ! glshader name=gpu_yuv_html5" in description


def test_cpu_description_has_no_gl_elements(full_hd: VisionMixer) -> None:
    for use_cefsrc in (True, False):
        description = _description(full_hd, gpu_path=False, use_cefsrc=use_cefsrc)
        assert not GL_ELEMENT.search(description)
        assert "capssetter" not in description
        assert "compositor name=comp background=black emit-signals=true" in description


def test_gpu_stinger_is_uploaded_before_its_pad() -> None:
    settings = Settings(width=1920, height=1080, simulate=True)
    sequence = {"kind": "sequence", "path": "/s/x", "pattern": "%04d.tga", "frame_count": 10}
    description = stinger_bin_description(sequence, settings, gpu=True)
    assert description.endswith(
        "! glupload ! glshader name=gpu_yuv_stinger "
        "! video/x-raw(memory:GLMemory),format=RGBA,width=1920,height=1080 ! queue name=stingerq"
    )
    assert "glupload" not in stinger_bin_description(sequence, settings)


def test_gpu_setting_values() -> None:
    assert Settings().gpu == "off"
    assert Settings(gpu=" ON ").gpu == "on"
    with pytest.raises(ValidationError):
        Settings(gpu="cuda")


def test_off_is_the_cpu_path_without_a_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu, "probe_gpu", lambda: pytest.fail("probed"))
    assert gpu.select_media_path("off") == gpu.MediaPath("cpu", "FLOWXER_GPU=off")


def test_auto_falls_back_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu, "probe_gpu", lambda: ("", "missing GStreamer elements: glshader"))
    assert gpu.select_media_path("auto") == gpu.MediaPath("cpu", "missing GStreamer elements: glshader")
    with pytest.raises(gpu.GpuUnavailableError, match="missing GStreamer elements"):
        gpu.select_media_path("on")


def test_auto_and_on_take_a_working_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu, "probe_gpu", lambda: ("NVIDIA RTX A4000/PCIe/SSE2", None))
    for mode in ("auto", "on"):
        media = gpu.select_media_path(mode)
        assert media.gpu
        assert media.reason == "OpenGL through EGL on NVIDIA RTX A4000/PCIe/SSE2"


def test_probe_without_an_nvidia_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu, "_nvidia_device", lambda: False)
    assert gpu.probe_gpu() == ("", gpu.NO_NVIDIA_DEVICE)


def test_status_and_metrics_name_the_media_path(client: TestClient, mixer: VisionMixer) -> None:
    # The test settings simulate media: always the CPU path.
    assert mixer.media == gpu.MediaPath("cpu", "simulated media")
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    status = client.get("/api/v1/mixer").json()
    assert status["media_path"] == "cpu"
    assert status["media_path_reason"] == "simulated media"
    assert 'media_path="cpu"' in client.get("/metrics").text
    mixer.stop()


def test_live_mixer_uses_the_selected_path(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu, "probe_gpu", lambda: ("NVIDIA A16/PCIe/SSE2", None))
    live = VisionMixer(settings.model_copy(update={"simulate": False, "gst_mode": "auto", "gpu": "auto"}))
    assert live.media.gpu
    assert 'media_path="gpu"' in render_prometheus(live)


def _free_port() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return str(probe.getsockname()[1])


def test_on_without_a_gpu_exits_78(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gpu, "probe_gpu", lambda: ("", gpu.NO_NVIDIA_DEVICE))
    env = {
        "FLOWXER_GPU": "on",
        "FLOWXER_PORT": _free_port(),
        "FLOWXER_NMOS_ENABLE": "false",
        "FLOWXER_MXL_ROOT": str(tmp_path / "mxl"),
        "FLOWXER_STORAGE_ROOT": str(tmp_path / "storage"),
        "FLOWXER_STATE_DIR": str(tmp_path / "config"),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)  # no .env file
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc:
            app_module.run()
    finally:
        get_settings.cache_clear()
    assert exc.value.code == 78
