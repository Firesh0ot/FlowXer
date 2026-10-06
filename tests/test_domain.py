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
from flowxer.engine.pipeline import build_pipeline_description
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


def test_live_input_mxlsrc_uses_resolved_domain_path(mixer: VisionMixer, tmp_path: Path) -> None:
    other = mixer.settings.mxl_root / "cam"
    _write_domain(other, "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
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
    description = build_pipeline_description(
        settings=mixer.settings,
        inputs=mixer.list_inputs(),
        overlay_url=mixer.overlay.url,
        overlay_enabled=False,
        output_video_flow_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        output_audio_flow_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        domain=str(mixer.settings.output_domain),
        use_mxl_sink=True,
        use_cefsrc=False,
        domain_paths=mixer._source_domain_paths(),
    )
    assert f'domain="{other.resolve()}"' in description
    assert f'domain="{mixer.settings.output_domain.resolve()}"' in description


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
