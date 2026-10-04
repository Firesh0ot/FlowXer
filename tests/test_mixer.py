import pytest

from flowxer.api.schemas import AudioEssence, InputKind, LogicalInputUpdate, MixerStartRequest, VideoEssence
from flowxer.engine.mixer import MixerError, VisionMixer
from flowxer.engine.pipeline import build_pipeline_description, stinger_bin_description


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
    # MXL audio channels are separate signals: unpositioned, the first two go to Program.
    assert 'capssetter caps="audio/x-raw,channel-mask=(bitmask)0x0"' in description
    # Stingers are not part of the pipeline; each playback gets its own bin.
    assert "stinger" not in description


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
