from __future__ import annotations

import io

from PIL import Image

from flowxer.api.schemas import MixerStartRequest
from flowxer.engine.mixer import VisionMixer
from flowxer.engine.pipeline import build_pipeline_description
from flowxer.engine.preview import render_monitor


def _mean(image: Image.Image) -> float:
    raw = image.tobytes()
    return sum(raw) / len(raw) if raw else 0.0


def test_black_source_is_a_black_frame(mixer: VisionMixer) -> None:
    assert mixer.get_input("black").kind.value == "black"
    frame = render_monitor(mixer, "source:black", width=96, height=54)
    assert frame.size == (96, 54)
    assert _mean(frame) == 0


def test_same_source_picture_on_src_pvw_and_pgm(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-1"))
    src = render_monitor(mixer, "source:cam-1", width=96, height=54)
    pvw = render_monitor(mixer, "panel:me-1:pvw", width=96, height=54)
    pgm = render_monitor(mixer, "panel:me-1:pgm", width=96, height=54)
    assert src.tobytes() == pvw.tobytes() == pgm.tobytes()
    other = render_monitor(mixer, "source:cam-2", width=96, height=54)
    assert other.tobytes() != src.tobytes()
    mixer.take("black")
    black_tile = render_monitor(mixer, "source:black", width=96, height=54)
    black_pgm = render_monitor(mixer, "panel:me-1:pgm", width=96, height=54)
    assert black_tile.tobytes() == black_pgm.tobytes()
    assert _mean(black_pgm) == 0
    mixer.stop()


def test_preview_jpeg_api_reuses_source_picture(client, mixer: VisionMixer) -> None:
    client.post("/api/v1/mixer/start", json={"program_input_id": "cam-1", "preview_input_id": "cam-1"})
    src = client.get("/api/v1/preview/jpeg/source:cam-1")
    pvw = client.get("/api/v1/preview/jpeg/panel:me-1:pvw")
    pgm = client.get("/api/v1/preview/jpeg/panel:me-1:pgm")
    assert src.status_code == 200
    assert src.content == pvw.content == pgm.content
    black = client.get("/api/v1/preview/jpeg/source:black")
    assert black.status_code == 200
    frame = Image.open(io.BytesIO(black.content)).convert("RGB")
    assert _mean(frame) < 8


def test_pipeline_black_is_black_not_smpte(mixer: VisionMixer) -> None:
    description = build_pipeline_description(
        settings=mixer.settings,
        inputs=mixer.list_inputs(),
        overlay_url=mixer.overlay.url,
        overlay_enabled=False,
        stinger=mixer.get_stinger("replay-wipe").model_dump(),
        output_video_flow_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        output_audio_flow_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        domain="/mxl-domain",
        use_mxl_sink=False,
        use_cefsrc=False,
    )
    black_line = next(line for line in description.splitlines() if "vsrc_black" in line)
    assert "pattern=black" in black_line
    assert "smpte" not in black_line
    assert "timeoverlay" not in black_line
    cam = next(line for line in description.splitlines() if "vsrc_cam-1" in line)
    assert "pattern=smpte" in cam
