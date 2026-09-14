from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flowxer.api.schemas import DomainInfo, FlowDescriptor


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_domain_info(domain: Path) -> DomainInfo:
    domain_def = None
    def_path = domain / "domain_def.json"
    if def_path.is_file():
        domain_def = _read_json(def_path)
    flows = list_flows(domain)
    return DomainInfo(
        path=str(domain),
        exists=domain.is_dir(),
        flow_count=len(flows),
        domain_def=domain_def,
    )


def list_flows(domain: Path) -> list[FlowDescriptor]:
    if not domain.is_dir():
        return []
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
