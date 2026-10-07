from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from flowxer.api.schemas import DomainInfo, FlowDescriptor

log = logging.getLogger(__name__)

HISTORY_DURATION_OPTION = "urn:x-mxl:option:history_duration/v1.0"


class DomainError(ValueError):
    """Raised when FlowXer would write into a mirror or foreign domain."""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def is_mirror_domain(path: Path, domain_def: dict[str, Any] | None = None) -> bool:
    if path.name.startswith("mirror-"):
        return True
    payload = domain_def if domain_def is not None else _read_json(path / "domain_def.json")
    return bool(payload and "x-mxl-fabrics-agent" in payload)


def assert_writable_domain(path: Path) -> None:
    payload = _read_json(path / "domain_def.json") if path.exists() else None
    if is_mirror_domain(path, payload):
        raise DomainError(
            f"refusing to write into MXL mirror domain {path} "
            "(directory name mirror-* or domain_def.json contains x-mxl-fabrics-agent)"
        )


def _domain_from_dir(path: Path) -> DomainInfo | None:
    def_path = path / "domain_def.json"
    if not def_path.is_file():
        return None
    domain_def = _read_json(def_path)
    if not isinstance(domain_def, dict):
        return None
    domain_id = str(domain_def.get("id") or path.name)
    flows = list_flows(path, domain_id=domain_id)
    return DomainInfo(
        path=str(path.resolve()),
        exists=True,
        flow_count=len(flows),
        domain_def=domain_def,
        id=domain_id,
        mirror=is_mirror_domain(path, domain_def),
    )


def scan_domains(root: Path) -> list[DomainInfo]:
    """Rescan the MXL root. Identity is domain_def.json `id`, never the directory name.

    Includes the root itself when it holds a legacy single-domain layout, every
    direct child with domain_def.json, and fabrics mirror domains. Unknown JSON
    fields are ignored. Never caches misses.
    """
    if not root.is_dir():
        return []
    seen: dict[str, DomainInfo] = {}
    for path in [root, *sorted(p for p in root.iterdir() if p.is_dir())]:
        info = _domain_from_dir(path)
        if info is None:
            continue
        previous = seen.get(info.id)
        if previous is None or (previous.mirror and not info.mirror):
            seen[info.id] = info
    return list(seen.values())


def resolve_domain_path(root: Path, domain_id: str) -> Path | None:
    needle = domain_id.strip()
    if not needle:
        return None
    for info in scan_domains(root):
        if info.id == needle:
            return Path(info.path)
    return None


def find_flow_domain(root: Path, flow_id: str) -> Path | None:
    """The domain under `root` that holds `flow_id`; a local domain wins over a fabrics mirror."""
    needle = flow_id.strip()
    if not needle:
        return None
    found: Path | None = None
    for info in scan_domains(root):
        if (Path(info.path) / f"{needle}.mxl-flow").is_dir():
            if not info.mirror:
                return Path(info.path)
            found = found or Path(info.path)
    return found


def load_domain_info(domain: Path) -> DomainInfo:
    info = _domain_from_dir(domain)
    if info is not None:
        return info
    return DomainInfo(
        path=str(domain),
        exists=domain.is_dir(),
        flow_count=0,
        domain_def=None,
        id="",
        mirror=is_mirror_domain(domain),
    )


def list_flows(domain: Path, *, domain_id: str = "") -> list[FlowDescriptor]:
    if not domain.is_dir():
        return []
    if not domain_id:
        payload = _read_json(domain / "domain_def.json") or {}
        domain_id = str(payload.get("id") or "")
    flows: list[FlowDescriptor] = []
    for flow_dir in sorted(domain.glob("*.mxl-flow")):
        flow_id = flow_dir.name.removesuffix(".mxl-flow")
        payload = _read_json(flow_dir / "flow_def.json") or {}
        tags = payload.get("tags") or {}
        group_hint = ""
        group_values = tags.get("urn:x-nmos:tag:grouphint/v1.0") or []
        if group_values:
            group_hint = str(group_values[0])
        flows.append(
            FlowDescriptor(
                id=payload.get("id", flow_id),
                label=payload.get("label", flow_id),
                description=payload.get("description", ""),
                media_type=payload.get("media_type", ""),
                format=payload.get("format", ""),
                group_hint=group_hint,
                path=str(flow_dir),
                domain_id=domain_id,
                extra={
                    k: payload[k]
                    for k in (
                        "frame_width",
                        "frame_height",
                        "grain_rate",
                        "channel_count",
                        "sample_rate",
                        "interlace_mode",
                    )
                    if k in payload
                },
            )
        )
    return flows


def list_flows_in_root(root: Path) -> list[FlowDescriptor]:
    flows: list[FlowDescriptor] = []
    for info in scan_domains(root):
        flows.extend(list_flows(Path(info.path), domain_id=info.id))
    return flows


def flows_by_group_hint(domain: Path, group_hint: str) -> dict[str, FlowDescriptor]:
    """Return video/audio essences that share an NMOS grouphint prefix."""
    matched: dict[str, FlowDescriptor] = {}
    needle = group_hint.lower()
    for flow in list_flows(domain):
        if needle not in flow.group_hint.lower() and needle not in flow.label.lower():
            continue
        if flow.media_type.startswith("video/"):
            matched["video"] = flow
        elif flow.media_type.startswith("audio/"):
            matched["audio"] = flow
    return matched


def flows_by_group_hint_in_root(root: Path, group_hint: str) -> dict[str, FlowDescriptor]:
    matched: dict[str, FlowDescriptor] = {}
    needle = group_hint.lower()
    for flow in list_flows_in_root(root):
        if needle not in flow.group_hint.lower() and needle not in flow.label.lower():
            continue
        if flow.media_type.startswith("video/"):
            matched["video"] = flow
        elif flow.media_type.startswith("audio/"):
            matched["audio"] = flow
    return matched


def ensure_output_domain(
    path: Path,
    *,
    domain_id: str,
    label: str,
    description: str,
    history_duration_ns: int,
) -> DomainInfo:
    """Create the output domain directory and domain_def.json if missing.

    Writes options.json (history_duration) only on first create. Never rewrites
    an existing domain's options.json. Refuses fabrics mirror directories.
    """
    assert_writable_domain(path)
    def_path = path / "domain_def.json"
    created = not def_path.is_file()
    path.mkdir(parents=True, exist_ok=True)
    assert_writable_domain(path)
    if created:
        domain_def = {
            "id": domain_id,
            "label": label,
            "description": description,
        }
        def_path.write_text(json.dumps(domain_def, indent=2) + "\n", encoding="utf-8")
        options_path = path / "options.json"
        if not options_path.exists():
            options_path.write_text(
                json.dumps({HISTORY_DURATION_OPTION: int(history_duration_ns)}, indent=2)
                + "\n",
                encoding="utf-8",
            )
            log.info(
                "created MXL output domain %s id=%s history_duration_ns=%s",
                path,
                domain_id,
                history_duration_ns,
            )
    else:
        existing = _read_json(def_path) or {}
        existing_id = str(existing.get("id") or "")
        if existing_id and existing_id != domain_id:
            log.error(
                "output domain %s already has id %s; not overwriting with %s",
                path,
                existing_id,
                domain_id,
            )
    return load_domain_info(path)


def remove_output_domain(path: Path, *, domain_id: str, root: Path) -> bool:
    """MXL_CLEANUP_ON_EXIT: delete the own output domain directory.

    Only when its domain_def.json carries `domain_id`, it is not a mirror and it is
    not the MXL root itself (the deprecated single-domain layout).
    """
    if not path.is_dir() or path.resolve() == root.resolve():
        return False
    payload = _read_json(path / "domain_def.json")
    if not isinstance(payload, dict) or str(payload.get("id") or "") != domain_id:
        log.warning("not removing %s: it is not the output domain %s", path, domain_id)
        return False
    if is_mirror_domain(path, payload):
        return False
    shutil.rmtree(path, ignore_errors=True)
    log.info("removed MXL output domain %s", path)
    return True
