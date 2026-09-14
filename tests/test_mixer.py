from flowxer.api.schemas import MixerStartRequest
from flowxer.engine.mixer import VisionMixer
from flowxer.engine.pipeline import build_pipeline_description


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
    description = build_pipeline_description(
        settings=mixer.settings,
        inputs=mixer.list_inputs(),
        overlay_url=mixer.overlay.url,
        overlay_enabled=True,
        stinger=mixer.get_stinger("replay-wipe").model_dump(),
        output_video_flow_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        output_audio_flow_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        domain="/mxl-domain",
        use_mxl_sink=True,
        use_cefsrc=True,
    )
    assert "mxlsink" in description
    assert "cefsrc" in description
    assert "input-selector name=vsel" in description
    assert "multifilesrc name=stinger" in description
    assert "video/x-raw,format=v210" in description


def test_file_player_location_is_quoted(mixer: VisionMixer) -> None:
    clip = mixer.settings.clips_dir / "sizzle.mp4"
    clip.write_bytes(b"fake")
    mixer.load_clip("replay", "sizzle.mp4")
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
    assert str(clip) in description
    assert "filesrc name=vsrc_replay" in description
