from __future__ import annotations

import uuid

from flowxer.domain.nmos import FLOWXER_NAMESPACE


def nmos_uuid(seed: str, *parts: str) -> str:
    """Stable UUIDv5 for an NMOS resource. Seed + names survive restarts."""
    key = ":".join((seed, *parts))
    return str(uuid.uuid5(FLOWXER_NAMESPACE, key))


def node_id(seed: str) -> str:
    return nmos_uuid(seed, "node")


def device_id(seed: str) -> str:
    return nmos_uuid(seed, "device")


def receiver_id(seed: str, input_id: str, role: str) -> str:
    return nmos_uuid(seed, "receiver", input_id, role)


def sender_id(seed: str, panel_id: str, role: str) -> str:
    return nmos_uuid(seed, "sender", panel_id, role)


def source_id(seed: str, panel_id: str, role: str) -> str:
    return nmos_uuid(seed, "source", panel_id, role)


def flow_id(seed: str, panel_id: str, role: str) -> str:
    """IS-04 Flow resource UUID (not the MXL mxl_flow_id)."""
    return nmos_uuid(seed, "flow", panel_id, role)


def receiver_name(input_id: str, role: str) -> str:
    return f"{input_id}-{role}"


def sender_name(panel_id: str, role: str) -> str:
    return f"{panel_id}-pgm-{role}"
