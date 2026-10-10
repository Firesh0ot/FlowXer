from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowxer.api.schemas import AudioEssence, InputKind, LogicalInputCreate, VideoEssence
from flowxer.domain.mxl_domain import (
    DomainError,
    assert_writable_domain,
    ensure_output_domain,
    resolve_domain_path,
    scan_domains,
)
from flowxer.engine.mixer import VisionMixer
from flowxer.settings import Settings


def _write_domain(path: Path, domain_id: str, extra: dict | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    payload = {"id": domain_id, "label": domain_id, **(extra or {})}
    (path / "domain_def.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_flow(domain: Path, flow_id: str, media_type: str, group_hint: str) -> None:
    flow_dir = domain / f"{flow_id}.mxl-flow"
    flow_dir.mkdir(parents=True, exist_ok=True)
    (flow_dir / "flow_def.json").write_text(
        json.dumps(
            {
                "id": flow_id,
                "label": group_hint,
                "media_type": media_type,
                "format": "urn:x-nmos:format:video"
                if media_type.startswith("video/")
                else "urn:x-nmos:format:audio",
                "tags": {"urn:x-nmos:tag:grouphint/v1.0": [group_hint]},
            }
        ),
        encoding="utf-8",
    )


def test_scan_includes_mirrors_unknown_fields_and_legacy_root(tmp_path: Path) -> None:
    root = tmp_path / "mxl"
    _write_domain(root / "cam-a", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", extra={"vendor": "decklink"})
    mirror = root / "mirror-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    _write_domain(
        mirror,
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        extra={"x-mxl-fabrics-agent": {"source": "node-b"}, "unexpected": True},
    )
    _write_domain(root, "legacy-root-id")

    found = {item.id: item for item in scan_domains(root)}
    assert found["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"].mirror is False
    assert found["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"].mirror is True
    assert found["legacy-root-id"].path == str(root.resolve())


def test_resolve_domain_id_not_directory_name(tmp_path: Path) -> None:
    root = tmp_path / "mxl"
    _write_domain(root / "pretty-name", "11111111-1111-1111-1111-111111111111")
    resolved = resolve_domain_path(root, "11111111-1111-1111-1111-111111111111")
    assert resolved == (root / "pretty-name").resolve()
    assert resolve_domain_path(root, "missing-id") is None
    assert resolve_domain_path(root, "pretty-name") is None


def test_scan_prefers_local_domain_over_mirror_with_same_id(tmp_path: Path) -> None:
    root = tmp_path / "mxl"
    _write_domain(root / "local", "same-id")
    _write_domain(
        root / "mirror-same-id",
        "same-id",
        extra={"x-mxl-fabrics-agent": {"source": "other"}},
    )
    found = {item.id: item for item in scan_domains(root)}
    assert found["same-id"].mirror is False
    assert found["same-id"].path.endswith("/local")


def test_refuse_write_into_mirror_directory(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror-source"
    _write_domain(mirror, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    with pytest.raises(DomainError, match="mirror"):
        assert_writable_domain(mirror)
    tagged = tmp_path / "looks-local"
    _write_domain(tagged, "cccccccc-cccc-cccc-cccc-cccccccccccc", extra={"x-mxl-fabrics-agent": {}})
    with pytest.raises(DomainError, match="fabrics"):
        ensure_output_domain(
            tagged,
            domain_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            label="x",
            description="x",
            history_duration_ns=200_000_000,
        )


def test_create_output_domain_writes_options_once(tmp_path: Path) -> None:
    path = tmp_path / "flowxer-out"
    ensure_output_domain(
        path,
        domain_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        label="FlowXer",
        description="pgm",
        history_duration_ns=100_000_000,
    )
    options = json.loads((path / "options.json").read_text(encoding="utf-8"))
    assert options["urn:x-mxl:option:history_duration/v1.0"] == 100_000_000
    (path / "options.json").write_text('{"keep": true}\n', encoding="utf-8")
    ensure_output_domain(
        path,
        domain_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        label="FlowXer",
        description="pgm",
        history_duration_ns=999,
    )
    assert json.loads((path / "options.json").read_text(encoding="utf-8")) == {"keep": True}


def test_deprecated_mxl_domain_sets_root_and_output(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    legacy = tmp_path / "legacy-domain"
    settings = Settings(mxl_domain=legacy, storage_root=tmp_path / "storage", simulate=True)
    assert settings.mxl_root == legacy
    assert settings.output_domain == legacy
    assert settings.mxl_domain_deprecated is True
    assert "FLOWXER_MXL_DOMAIN is deprecated" in caplog.text


def _record_engine(mixer: VisionMixer) -> list[tuple[str, str, dict]]:
    """The commands the mixer would send to a running engine."""
    calls: list[tuple[str, str, dict]] = []
    mixer._engine_call = lambda method, path, body, client=None: calls.append((method, path, body))
    return calls


def test_a_live_input_is_routed_to_the_resolved_domain_path(mixer: VisionMixer, tmp_path: Path) -> None:
    other = mixer.settings.mxl_root / "cam"
    _write_domain(other, "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    _write_flow(other, "5fbec3b1-1b0f-417d-9059-8b94a47197ed", "video/v210", "cam")
    mixer.register_input(
        LogicalInputCreate(
            id="studio-a",
            label="Studio A",
            kind=InputKind.mxl_live,
            video=VideoEssence(
                flow_id="5fbec3b1-1b0f-417d-9059-8b94a47197ed",
                domain_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            ),
            audio=AudioEssence(
                flow_id="b3bb5be7-9fe9-4324-a5bb-4c70e1084449",
                domain_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                channels=2,
            ),
        )
    )
    item = mixer.get_input("studio-a")
    # The engine gets the domain directory and the flow id.
    assert mixer._essence_route(item, "video") == {"domain": str(other.resolve()), "flow": "5fbec3b1-1b0f-417d-9059-8b94a47197ed"}
    assert mixer._essence_route(item, "audio")["domain"] == str(other.resolve())
    calls = _record_engine(mixer)
    mixer._push_route(item)
    # Live inputs are in1..N in slot order: studio-a comes after the cameras.
    live = [i.id for i in mixer.list_inputs() if i.kind == InputKind.mxl_live]
    assert calls[-1][1] == f"/input/{live.index('studio-a') + 1}"


def test_group_hint_bind_reads_mirror_domain(mixer: VisionMixer) -> None:
    root = mixer.settings.mxl_root
    mirror = root / "mirror-cam"
    _write_domain(mirror, "fff00000-0000-0000-0000-000000000001", extra={"x-mxl-fabrics-agent": {}})
    _write_flow(
        mirror,
        "5fbec3b1-1b0f-417d-9059-8b94a47197ed",
        "video/v210",
        "studio-b:Video",
    )
    mixer.register_input(
        LogicalInputCreate(
            id="studio-b",
            label="Studio B",
            kind=InputKind.mxl_live,
            group_hint="studio-b",
        )
    )
    mixer._bind_group_hints()
    bound = mixer.get_input("studio-b")
    assert bound.video is not None
    assert str(bound.video.flow_id) == "5fbec3b1-1b0f-417d-9059-8b94a47197ed"
    assert bound.video.domain_id == "fff00000-0000-0000-0000-000000000001"


def test_a_route_to_the_wrong_domain_finds_its_flow(mixer: VisionMixer) -> None:
    # Platform 9.16.33: routes without a domain default to the own output domain, which does not
    # hold the flow. Any domain that holds the flow is used, local first.
    root = mixer.settings.mxl_root
    local = root / "cam"
    mirror = root / "mirror-deck"
    _write_domain(local, "cccccccc-cccc-cccc-cccc-ccccccccccc1")
    _write_domain(mirror, "dddddddd-dddd-dddd-dddd-ddddddddddd1")
    video = "5fbec3b1-1b0f-417d-9059-8b94a47197ed"
    audio = "339fa87e-fdb0-5ad5-9956-6b82751e0957"
    _write_flow(local, video, "video/v210", "cam")
    _write_flow(mirror, video, "video/v210", "cam")
    _write_flow(mirror, audio, "audio/float32", "deck")
    mixer.register_input(
        LogicalInputCreate(
            id="studio-a",
            label="Studio A",
            kind=InputKind.mxl_live,
            video=VideoEssence(flow_id=video),
            audio=AudioEssence(flow_id=audio, channels=2),
        )
    )
    created = mixer.get_input("studio-a")
    assert created.audio is not None and created.audio.domain_id == "flowxer-test"
    assert Path(mixer._essence_route(created, "video")["domain"]).resolve() == local.resolve()
    assert Path(mixer._essence_route(created, "audio")["domain"]).resolve() == mirror.resolve()
    # A flow that is nowhere yet stays in the named domain, where it may appear.
    calls = _record_engine(mixer)
    mixer.apply_nmos_receiver("studio-a", "audio", domain_id=None, flow_id="11111111-2222-3333-4444-555555555555", enabled=True)
    assert Path(calls[-1][2]["audio"]["domain"]).resolve() == mixer.settings.output_domain.resolve()
    assert calls[-1][2]["audio"]["flow"] == "11111111-2222-3333-4444-555555555555"


def test_a_route_to_a_missing_domain_waits_and_is_routed_when_it_appears(mixer: VisionMixer) -> None:
    # Platform vmix after a node reboot: the fabrics mirror domains were not there yet. The
    # input is not routed until the domain appears, then it reads its flow.
    root = mixer.settings.mxl_root
    missing = "abcdef00-0000-4000-8000-000000000001"
    video = "5fbec3b1-1b0f-417d-9059-8b94a47197ed"
    audio = "339fa87e-fdb0-5ad5-9956-6b82751e0957"
    mixer.register_input(
        LogicalInputCreate(
            id="studio-a",
            label="Studio A",
            kind=InputKind.mxl_live,
            video=VideoEssence(flow_id=video, domain_id=missing),
            audio=AudioEssence(flow_id=audio, domain_id=missing, channels=2),
        )
    )
    calls = _record_engine(mixer)
    mixer._push_route(mixer.get_input("studio-a"))
    assert calls[-1][2] == {"video": {"domain": "", "flow": ""}, "audio": {"domain": "", "flow": ""}}
    assert set(mixer._missing_domains) == {("studio-a", "video"), ("studio-a", "audio")}
    mixer._attach_waiting_sources()
    assert len(calls) == 1

    mirror = root / "mirror-cam"
    _write_domain(mirror, missing, extra={"x-mxl-fabrics-agent": {}})
    mixer._attach_waiting_sources()
    assert calls[-1][2] == {
        "video": {"domain": str(mirror.resolve()), "flow": video},
        "audio": {"domain": str(mirror.resolve()), "flow": audio},
    }
    assert mixer._missing_domains == {}
    count = len(calls)
    mixer._attach_waiting_sources()
    assert len(calls) == count


def test_a_waiting_source_that_was_routed_elsewhere_is_left_alone(mixer: VisionMixer) -> None:
    missing = "abcdef00-0000-4000-8000-000000000002"
    mixer.register_input(
        LogicalInputCreate(
            id="studio-a",
            label="Studio A",
            kind=InputKind.mxl_live,
            video=VideoEssence(flow_id="5fbec3b1-1b0f-417d-9059-8b94a47197ed", domain_id=missing),
        )
    )
    calls = _record_engine(mixer)
    mixer._push_route(mixer.get_input("studio-a"))
    assert ("studio-a", "video") in mixer._missing_domains
    item = mixer.get_input("studio-a")
    item.video = item.video.model_copy(update={"flow_id": None})
    _write_domain(mixer.settings.mxl_root / "late", missing)
    mixer._attach_waiting_sources()
    assert len(calls) == 1
    assert mixer._missing_domains == {}
