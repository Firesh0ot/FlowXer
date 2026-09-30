from flowxer.api.schemas import (
    AudioEssence,
    InputKind,
    LogicalInputCreate,
    MixerStartRequest,
    VideoEssence,
)
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
    assert status.outputs.nmos_video["tags"]["urn:x-nmos:tag:grouphint/v1.0"] == [
        "FlowXerTest:Video"
    ]
    assert status.outputs.nmos_audio["bit_depth"] == 32
    assert status.outputs.nmos_audio["tags"]["urn:x-nmos:tag:grouphint/v1.0"] == [
        "FlowXerTest:Audio"
    ]
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
        output_video_label="FlowXerTest PGM video",
        output_video_description="FlowXer program video (uncompressed v210 / VP210)",
        output_video_group_hint="FlowXerTest:Video",
        output_audio_label="FlowXerTest PGM audio",
        output_audio_description="FlowXer program audio (uncompressed float32)",
        output_audio_group_hint="FlowXerTest:Audio",
    )
    assert "mxlsink" in description
    assert "cefsrc" in description
    assert "input-selector name=vsel" in description
    assert "multifilesrc name=stinger" in description
    assert 'mxlsink name=vout flow-id=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa domain="/mxl-domain"' in description
    assert 'group-hint="FlowXerTest:Video"' in description
    assert 'group-hint="FlowXerTest:Audio"' in description
    assert 'label="FlowXerTest PGM video"' in description
    assert 'label="FlowXerTest PGM audio"' in description
    video = mixer.get_stinger("replay-wipe").model_dump()
    video["kind"] = "video"
    video["media_path"] = "/tmp/sting.webm"
    video_description = build_pipeline_description(
        settings=mixer.settings,
        inputs=mixer.list_inputs(),
        overlay_url=mixer.overlay.url,
        overlay_enabled=False,
        stinger=video,
        output_video_flow_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        output_audio_flow_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        domain="/mxl-domain",
        use_mxl_sink=False,
        use_cefsrc=False,
    )
    assert 'filesrc name=stinger location="/tmp/sting.webm"' in video_description
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


def test_nmos_flow_def_shape_matches_mxl_sdk_examples() -> None:
    from flowxer.domain import nmos

    video = nmos.video_flow_def(
        flow_id="5fbec3b1-1b0f-417d-9059-8b94a47197ed",
        group_hint="Media Function XYZ",
        label="MXL Test Flow, 1080p29",
        description="MXL Test Flow, 1080p29",
        width=1920,
        height=1080,
        frame_rate_num=30000,
        frame_rate_den=1001,
    )
    audio = nmos.audio_flow_def(
        flow_id="b3bb5be7-9fe9-4324-a5bb-4c70e1084449",
        group_hint="Media Function XYZ",
        label="MXL Audio Flow",
        description="MXL Audio Flow",
        channels=2,
        sample_rate=48000,
    )
    assert video["media_type"] == "video/v210"
    assert video["format"] == "urn:x-nmos:format:video"
    assert video["interlace_mode"] == "progressive"
    assert video["colorspace"] == "BT709"
    assert video["grain_rate"] == {"numerator": 30000, "denominator": 1001}
    assert {c["name"] for c in video["components"]} == {"Y", "Cb", "Cr"}
    assert video["tags"]["urn:x-nmos:tag:grouphint/v1.0"] == ["Media Function XYZ:Video"]
    assert audio["media_type"] == "audio/float32"
    assert audio["format"] == "urn:x-nmos:format:audio"
    assert audio["sample_rate"] == {"numerator": 48000}
    assert audio["channel_count"] == 2
    assert audio["bit_depth"] == 32
    assert audio["tags"]["urn:x-nmos:tag:grouphint/v1.0"] == ["Media Function XYZ:Audio"]


def test_mxl_live_inputs_use_sdk_mxlsrc_properties(mixer: VisionMixer) -> None:
    mixer.register_input(
        LogicalInputCreate(
            id="studio-a",
            label="Studio A",
            kind=InputKind.mxl_live,
            video=VideoEssence(flow_id="5fbec3b1-1b0f-417d-9059-8b94a47197ed"),
            audio=AudioEssence(flow_id="b3bb5be7-9fe9-4324-a5bb-4c70e1084449", channels=2),
        )
    )
    description = build_pipeline_description(
        settings=mixer.settings,
        inputs=mixer.list_inputs(),
        overlay_url=mixer.overlay.url,
        overlay_enabled=False,
        stinger=mixer.get_stinger("replay-wipe").model_dump(),
        output_video_flow_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        output_audio_flow_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        domain="/mxl-domain",
        use_mxl_sink=True,
        use_cefsrc=False,
        output_video_group_hint="FlowXer:Video",
        output_audio_group_hint="FlowXer:Audio",
    )
    assert (
        'mxlsrc name=vsrc_studio-a video-flow-id=5fbec3b1-1b0f-417d-9059-8b94a47197ed '
        'domain="/mxl-domain"'
    ) in description
    assert (
        'mxlsrc name=asrc_studio-a audio-flow-id=b3bb5be7-9fe9-4324-a5bb-4c70e1084449 '
        'domain="/mxl-domain"'
    ) in description
    assert "data-flow-id=" not in description
