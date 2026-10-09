import time
from types import SimpleNamespace

import pytest

from flowxer.api.schemas import AudioEssence, InputKind, LogicalInputUpdate, MixerStartRequest, VideoEssence
from flowxer.engine.mixer import MixerError, VisionMixer
from flowxer.api.metrics import render_prometheus
from flowxer.api.schemas import MixerState
from flowxer.engine.pipeline import (
    UNROUTED_FLOW,
    build_pipeline_description,
    stinger_bin_description,
)


def test_start_publishes_uncompressed_output_flows(mixer: VisionMixer) -> None:
    status = mixer.start(MixerStartRequest(program_input_id="cam-1"))
    assert status.state.value == "running"
    assert status.backend == "simulate"
    assert status.outputs is not None
    assert status.outputs.media_type_video == "video/v210"
    assert status.outputs.media_type_audio == "audio/float32"
    assert status.outputs.nmos_video["media_type"] == "video/v210"
    assert status.outputs.nmos_video["frame_width"] == 64
    assert status.outputs.nmos_audio["bit_depth"] == 32
    assert "format=v210" in (status.pipeline or "")
    assert "format=F32LE" in (status.pipeline or "")
    mixer.stop()


def test_take_switches_program(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.take("cam-2")
    assert mixer.program_input_id == "cam-2"
    assert mixer.program_bus.value == "live"
    mixer.stop()


def test_pipeline_uses_mxl_elements_when_requested(mixer: VisionMixer) -> None:
    mixer.update_input(
        "cam-1",
        LogicalInputUpdate(kind=InputKind.mxl_live, video=VideoEssence(), audio=AudioEssence()),
    )
    description = build_pipeline_description(
        settings=mixer.settings,
        inputs=mixer.list_inputs(),
        overlay_url=mixer.overlay.url,
        overlay_enabled=True,
        output_video_flow_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        output_audio_flow_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        domain="/mxl-domain",
        use_mxl_sink=True,
        use_cefsrc=True,
    )
    assert "mxlsink" in description
    assert "cefsrc" in description
    # A (Program) and B (mix) buses, video and audio.
    for name in ("vsel", "vselb", "asel", "aselb"):
        assert f"input-selector name={name} " in description
    assert "audiomixer name=amix" in description
    assert "video/x-raw,format=v210" in description
    # GStreamer 1.24 (Ubuntu 24.04) has no compositor property of that name; parsing failed.
    assert "zero-size-is-unconfigured" not in description
    # MXL audio gets a channel map when its caps arrive (GstRuntime.map_audio_channels).
    assert "audioconvert name=amap_cam-1 " in description
    # Stingers are not part of the pipeline; each playback gets its own bin.
    assert "stinger" not in description
    # GUI monitors: one picture per source and one of Program.
    assert "appsink name=mon_cam-1 " in description
    assert "appsink name=mon__program " in description
    # A source without data must not hold the pipeline out of PLAYING through its monitor.
    assert "appsink name=mon_cam-1 max-buffers=1 drop=true sync=false async=false" in description
    # Unrouted mxl_live essences wait on a flow id that never exists ("UNBOUND" made mxlsrc fail).
    assert f"video-flow-id={UNROUTED_FLOW} " in description
    assert f"audio-flow-id={UNROUTED_FLOW} " in description
    # force-live made the mixers drop late buffers: dark frames at the end of fades.
    assert "force-live" not in description
    mixer.settings.monitor_fps = 0
    without = build_pipeline_description(
        settings=mixer.settings,
        inputs=mixer.list_inputs(),
        overlay_url=mixer.overlay.url,
        overlay_enabled=True,
        output_video_flow_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        output_audio_flow_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        domain="/mxl-domain",
        use_mxl_sink=True,
        use_cefsrc=True,
    )
    assert "appsink" not in without


def test_stinger_bin_for_sequences_and_videos(mixer: VisionMixer) -> None:
    sequence = mixer.get_stinger("replay-wipe").model_dump()
    description = stinger_bin_description(sequence, mixer.settings)
    assert description.startswith("multifilesrc ")
    # A TGA sequence needs the mixer rate in its caps, or the BGRA caps cannot negotiate.
    assert "caps=image/x-tga,framerate=50/1" in description
    assert f"stop-index={sequence['frame_count'] - 1} loop=false" in description
    video = dict(sequence, kind="video", media_path="/tmp/sting.webm")
    video_description = stinger_bin_description(video, mixer.settings)
    assert video_description.startswith('filesrc location="/tmp/sting.webm"')
    assert "videorate" in video_description


def test_failed_pipeline_does_not_fall_back_to_simulate(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import flowxer.engine.mixer as mixer_module

    live = VisionMixer(settings.model_copy(update={"simulate": False, "gst_mode": "auto"}))
    monkeypatch.setattr(
        mixer_module,
        "probe_backend",
        lambda: {"gstreamer": True, "mxlsrc": True, "mxlsink": True, "cefsrc": False, "mxl_plugins": True},
    )
    monkeypatch.setattr(mixer_module, "try_start_gst", lambda *_args: (None, "no element \"x\""))
    with pytest.raises(MixerError, match="GStreamer pipeline failed"):
        live.start(MixerStartRequest(program_input_id="cam-1"))
    assert live.state.value == "error"
    assert live.backend == "idle"
    assert "no element" in (live.status().error or "")


def test_pipeline_errors_are_reported(mixer: VisionMixer) -> None:
    mixer._on_pipeline_error("asrc_cam-2: Internal data stream error.")
    assert mixer.pipeline_errors == 1
    assert mixer.status().error == "asrc_cam-2: Internal data stream error."


def test_file_player_location_is_quoted(mixer: VisionMixer) -> None:
    clip = mixer.settings.clips_dir / "sizzle.mp4"
    clip.write_bytes(b"fake")
    mixer.load_clip("replay", "sizzle.mp4")
    description = build_pipeline_description(
        settings=mixer.settings,
        inputs=mixer.list_inputs(),
        overlay_url=mixer.overlay.url,
        overlay_enabled=False,
        output_video_flow_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        output_audio_flow_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        domain="/mxl-domain",
        use_mxl_sink=False,
        use_cefsrc=False,
    )
    assert str(clip) in description
    assert "filesrc name=vsrc_replay" in description


def test_status_reports_a_program_without_frames(mixer: VisionMixer) -> None:
    # Platform 9.16.33: Program stopped after a few frames while the status said "running" without
    # an error. The state stays "running" (on air); the error and a gauge say that no frames come.
    mixer.state = MixerState.running
    mixer.gst = SimpleNamespace(program_frames=4)
    mixer._frame_mark = (4, time.monotonic() - 10)
    status = mixer.status()
    assert status.state == MixerState.running
    assert status.error is not None and status.error.startswith("Program renders no frames")
    assert "flowxer_program_stalled 1" in render_prometheus(mixer)
    mixer.gst.program_frames = 5
    assert mixer.status().error is None
    assert "flowxer_program_stalled 0" in render_prometheus(mixer)
    mixer.gst = None
    mixer.state = MixerState.idle


def test_program_autostart_tries_again_until_program_runs(
    mixer: VisionMixer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Platform vmix after a node reboot: the first start failed and Program stayed off until an
    # operator started it two minutes later.
    import flowxer.engine.mixer as mixer_module

    monkeypatch.setattr(mixer_module, "AUTOSTART_BACKOFF_S", (0.01, 0.02))
    start = mixer._start
    attempts: list[int] = []

    def flaky(request=None):
        attempts.append(1)
        if len(attempts) < 3:
            mixer.state = MixerState.error
            raise MixerError("GStreamer pipeline failed: failed to set GStreamer pipeline to PLAYING")
        return start(request)

    monkeypatch.setattr(mixer, "_start", flaky)
    caplog.set_level("INFO", logger="flowxer.engine.mixer")
    mixer.autostart()
    deadline = time.monotonic() + 5
    while mixer.state != MixerState.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert mixer.state == MixerState.running
    assert len(attempts) == 3
    messages = [record.getMessage() for record in caplog.records if "AUTOSTART" in record.getMessage()]
    assert "attempt 1, next in 0 s" in messages[0]
    assert "attempt 2" in messages[1]
    assert messages[2].endswith("(attempt 3)")


def test_a_stop_ends_the_program_autostart_attempts(mixer: VisionMixer, monkeypatch: pytest.MonkeyPatch) -> None:
    import flowxer.engine.mixer as mixer_module

    monkeypatch.setattr(mixer_module, "AUTOSTART_BACKOFF_S", (0.05,))
    attempts: list[int] = []

    def failing(request=None):
        attempts.append(1)
        raise MixerError("GStreamer pipeline failed")

    monkeypatch.setattr(mixer, "_start", failing)
    mixer.autostart()
    mixer.stop()
    count = len(attempts)
    time.sleep(0.3)
    assert len(attempts) == count <= 2
