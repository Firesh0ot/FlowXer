from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from flowxer.api.schemas import InputKind
from flowxer.domain.mxl_domain import resolve_domain_path
from flowxer.nmos import ids
from flowxer.nmos.registry import RegistryClient

log = logging.getLogger(__name__)

TAI_OFFSET_NS = 37 * 10**9
TRANSPORT_MXL = "urn:x-nmos:transport:mxl"
FORMAT_VIDEO = "urn:x-nmos:format:video"
FORMAT_AUDIO = "urn:x-nmos:format:audio"
GROUPHINT = "urn:x-nmos:tag:grouphint/v1.0"
NULL_ACTIVATION = {"mode": None, "requested_time": None, "activation_time": None}
STAGED_PATCH_FIELDS = {
    "sender_id",
    "receiver_id",
    "master_enable",
    "activation",
    "transport_params",
    "transport_file",
}


def nmos_version() -> str:
    tai_ns = time.time_ns() + TAI_OFFSET_NS
    return f"{tai_ns // 10**9}:{tai_ns % 10**9}"


def first_non_loopback_ip() -> str:
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                return ip
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("1.1.1.1", 80))
        ip = probe.getsockname()[0]
        probe.close()
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


def _uuid_or_none(value: Any) -> str | None:
    if value is None or value == "" or value == "null":
        return None
    if value == "auto":
        return "auto"
    text = str(value)
    try:
        return str(uuid.UUID(text))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"invalid UUID {value!r}") from None


def flow_presence(root: Path, domain_id: str | None, flow_id: str | None) -> str:
    """Map on-disk state to waiting / no_signal / running."""
    if not domain_id or not flow_id:
        return "waiting"
    path = resolve_domain_path(root, str(domain_id))
    if path is None:
        return "waiting"
    flow_dir = path / f"{flow_id}.mxl-flow"
    if not flow_dir.is_dir():
        return "waiting"
    extra = [
        child
        for child in flow_dir.iterdir()
        if child.name != "flow_def.json"
    ]
    if not extra:
        return "no_signal"
    return "running"


class NmosActivationError(ValueError):
    """Malformed IS-05 parameters (HTTP 400)."""


class NmosNode:
    """In-process IS-04 v1.3 / IS-05 v1.2 node for BCP-007-03 MXL (plan Option C).

    NvNmos (`nvnmosd`) was evaluated first; this in-process FastAPI node is used
    because activations must ACK when the domain/flow is not on disk yet.
    """

    def __init__(self, mixer) -> None:
        self.mixer = mixer
        self.settings = mixer.settings
        self.lock = threading.RLock()
        self.registry_up = False
        self.activations_ok = 0
        self.activations_error = 0
        self._staged: dict[str, dict[str, Any]] = {}
        self._active: dict[str, dict[str, Any]] = {}
        self._scheduled: dict[str, threading.Timer] = {}
        self._input_states: dict[str, dict[str, str]] = {}
        self._stop = threading.Event()
        self._server = None
        self._http_thread: threading.Thread | None = None
        self._wait_thread: threading.Thread | None = None
        self._registry_thread: threading.Thread | None = None
        self.host_ip = (self.settings.nmos_host_ip or "").strip() or first_non_loopback_ip()
        self.seed = self.settings.resolved_nmos_seed
        self.node_uuid = ids.node_id(self.seed)
        self.device_uuid = ids.device_id(self.seed)

    def enabled(self) -> bool:
        return bool(self.settings.nmos_enable)

    def href(self) -> str:
        return f"http://{self.host_ip}:{self.settings.nmos_port}/"

    def input_state(self, input_id: str, role: str) -> str:
        return self._input_states.get(input_id, {}).get(role, "not_routed")

    def status(self) -> dict[str, Any]:
        with self.lock:
            receivers = []
            for item in self.mixer.list_inputs():
                if item.kind != InputKind.mxl_live:
                    continue
                for role in ("video", "audio"):
                    rid = ids.receiver_id(self.seed, item.id, role)
                    active = self._active.get(rid, self._empty_receiver_active())
                    params = (active.get("transport_params") or [{}])[0]
                    receivers.append(
                        {
                            "input_id": item.id,
                            "role": role,
                            "receiver_id": rid,
                            "state": self.input_state(item.id, role),
                            "master_enable": bool(active.get("master_enable")),
                            "sender_id": active.get("sender_id"),
                            "mxl_domain_id": params.get("mxl_domain_id"),
                            "mxl_flow_id": params.get("mxl_flow_id"),
                        }
                    )
            return {
                "enabled": self.enabled(),
                "registry_url": self.settings.nmos_registry_url,
                "registry_up": self.registry_up,
                "node_id": self.node_uuid,
                "device_id": self.device_uuid,
                "href": self.href(),
                "host_ip": self.host_ip,
                "port": self.settings.nmos_port,
                "dns_sd": self.settings.nmos_dns_sd,
                "receivers": receivers,
            }

    def boot(self) -> None:
        if not self.enabled():
            log.info("NMOS node disabled (FLOWXER_NMOS_ENABLE=false)")
            return
        if self.settings.nmos_dns_sd:
            log.warning("FLOWXER_NMOS_DNS_SD=true is ignored; DNS-SD/mDNS is not implemented")
        self._stop.clear()
        if self.settings.nmos_bind:
            self._start_http()
        self._wait_thread = threading.Thread(
            target=self._refresh_loop, daemon=True, name="nmos-wait"
        )
        self._wait_thread.start()
        if (self.settings.nmos_registry_url or "").strip():
            self._registry_thread = threading.Thread(
                target=self._registry_loop, daemon=True, name="nmos-registry"
            )
            self._registry_thread.start()
        log.info(
            "NMOS node %s on %s:%s (bind=%s)",
            self.node_uuid,
            self.host_ip,
            self.settings.nmos_port,
            self.settings.nmos_bind,
        )

    def shutdown(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.should_exit = True
        self._server = None

    def _start_http(self) -> None:
        import uvicorn

        from flowxer.nmos.http import create_nmos_app

        app = create_nmos_app(self)
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.settings.nmos_port,
            log_level="info",
        )
        self._server = uvicorn.Server(config)
        self._http_thread = threading.Thread(
            target=self._server.run, daemon=True, name="nmos-http"
        )
        self._http_thread.start()

    def _refresh_loop(self) -> None:
        delay = 0.25
        while not self._stop.wait(delay):
            try:
                self.refresh_waiting()
            except Exception:
                log.debug("NMOS waiting refresh failed", exc_info=True)
            waiting = any(
                state in {"waiting", "no_signal"}
                for roles in self._input_states.values()
                for state in roles.values()
            )
            delay = min(delay * 2, 5.0) if waiting else 0.25

    def _registry_loop(self) -> None:
        client = RegistryClient(self.settings.nmos_registry_url)
        while not self._stop.is_set():
            try:
                self._register_all(client)
                client.heartbeat(self.node_uuid)
                self.registry_up = True
            except Exception as exc:
                self.registry_up = False
                log.warning("NMOS registry %s: %s", self.settings.nmos_registry_url, exc)
            self._stop.wait(5.0)

    def _register_all(self, client: RegistryClient) -> None:
        with self.lock:
            client.register("node", self.self_resource())
            client.register("device", self.device_resource())
            for source in self.sources():
                client.register("source", source)
            for flow in self.flows():
                client.register("flow", flow)
            for sender in self.senders():
                client.register("sender", sender)
            for receiver in self.receivers():
                client.register("receiver", receiver)

    def _empty_receiver_active(self) -> dict[str, Any]:
        return {
            "sender_id": None,
            "master_enable": False,
            "activation": dict(NULL_ACTIVATION),
            "transport_params": [{"mxl_domain_id": None, "mxl_flow_id": None}],
            "transport_file": {"data": None, "type": None},
        }

    def _empty_sender_active(self, domain_id: str | None, flow_id: str | None) -> dict[str, Any]:
        return {
            "receiver_id": None,
            "master_enable": True,
            "activation": dict(NULL_ACTIVATION),
            "transport_params": [{"mxl_domain_id": domain_id, "mxl_flow_id": flow_id}],
        }

    def _live_inputs(self):
        return [item for item in self.mixer.list_inputs() if item.kind == InputKind.mxl_live]

    def _sender_ids(self) -> set[str]:
        out = set()
        for panel in self.mixer.panels or []:
            out.add(ids.sender_id(self.seed, panel.id, "video"))
            out.add(ids.sender_id(self.seed, panel.id, "audio"))
        return out

    def self_resource(self) -> dict[str, Any]:
        return {
            "id": self.node_uuid,
            "version": nmos_version(),
            "label": self.settings.title,
            "description": "FlowXer Vision Mixer",
            "tags": {},
            "href": self.href(),
            "caps": {},
            "services": [
                {
                    "href": f"http://{self.host_ip}:{self.settings.nmos_port}/x-nmos/node/v1.3/",
                    "type": "urn:x-nmos:service:node/v1.3",
                    "authorization": False,
                },
                {
                    "href": f"http://{self.host_ip}:{self.settings.nmos_port}/x-nmos/connection/v1.2/",
                    "type": "urn:x-nmos:service:connection/v1.2",
                    "authorization": False,
                },
            ],
            "clocks": [{"name": "clk0", "ref_type": "internal"}],
            "interfaces": [
                {
                    "chassis_id": None,
                    "port_id": "00-00-00-00-00-00",
                    "name": "mgmt",
                }
            ],
            "api": {
                "versions": ["v1.3"],
                "endpoints": [
                    {
                        "host": self.host_ip,
                        "port": self.settings.nmos_port,
                        "protocol": "http",
                        "authorization": False,
                    }
                ],
            },
        }

    def device_resource(self) -> dict[str, Any]:
        senders = [s["id"] for s in self.senders()]
        receivers = [r["id"] for r in self.receivers()]
        return {
            "id": self.device_uuid,
            "version": nmos_version(),
            "label": "FlowXer Vision Mixer",
            "description": "FlowXer Vision Mixer",
            "tags": {},
            "type": "urn:x-nmos:device:generic",
            "node_id": self.node_uuid,
            "senders": senders,
            "receivers": receivers,
            "controls": [
                {
                    "href": f"http://{self.host_ip}:{self.settings.nmos_port}/x-nmos/connection/v1.2/",
                    "type": "urn:x-nmos:control:sr-ctrl/v1.2",
                    "authorization": False,
                }
            ],
        }

    def receivers(self) -> list[dict[str, Any]]:
        out = []
        for item in self._live_inputs():
            for role, fmt in (("video", FORMAT_VIDEO), ("audio", FORMAT_AUDIO)):
                rid = ids.receiver_id(self.seed, item.id, role)
                active = self._active.get(rid, self._empty_receiver_active())
                if role == "video":
                    caps: dict[str, Any] = {
                        "media_types": ["video/v210", "video/v210a"],
                        "constraint_sets": [
                            {
                                "urn:x-nmos:cap:format:frame_width": {
                                    "enum": [self.settings.width]
                                },
                                "urn:x-nmos:cap:format:frame_height": {
                                    "enum": [self.settings.height]
                                },
                            }
                        ],
                    }
                else:
                    caps = {
                        "media_types": ["audio/float32"],
                        "constraint_sets": [
                            {
                                "urn:x-nmos:cap:format:channel_count": {
                                    "enum": [self.settings.audio_channels]
                                },
                                "urn:x-nmos:cap:format:sample_rate": {
                                    "enum": [
                                        {
                                            "numerator": self.settings.audio_rate,
                                            "denominator": 1,
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                out.append(
                    {
                        "id": rid,
                        "version": nmos_version(),
                        "label": f"{item.label} {role.title()}",
                        "description": f"{item.label} {role}",
                        "tags": {GROUPHINT: [f"{item.id}:{role.title()}"]},
                        "device_id": self.device_uuid,
                        "transport": TRANSPORT_MXL,
                        "interface_bindings": [],
                        "subscription": {
                            "sender_id": active.get("sender_id"),
                            "active": bool(active.get("master_enable")),
                        },
                        "format": fmt,
                        "caps": caps,
                    }
                )
        return out

    def senders(self) -> list[dict[str, Any]]:
        out = []
        outputs = self.mixer.outputs
        domain_id = self.settings.resolved_output_domain_id
        for panel in self.mixer.panels or []:
            for role, fmt, mxl_flow in (
                ("video", FORMAT_VIDEO, outputs.video_flow_id if outputs else None),
                ("audio", FORMAT_AUDIO, outputs.audio_flow_id if outputs else None),
            ):
                sid = ids.sender_id(self.seed, panel.id, role)
                out.append(
                    {
                        "id": sid,
                        "version": nmos_version(),
                        "label": f"{panel.label} PGM {role.title()}",
                        "description": f"{panel.label} program {role}",
                        "tags": {GROUPHINT: [f"{panel.id}:Pgm{role.title()}"]},
                        "device_id": self.device_uuid,
                        "manifest_href": None,
                        "transport": TRANSPORT_MXL,
                        "interface_bindings": [],
                        "subscription": {
                            "receiver_id": None,
                            "active": bool(outputs),
                        },
                        "flow_id": ids.flow_id(self.seed, panel.id, role),
                    }
                )
                self._active.setdefault(
                    sid,
                    self._empty_sender_active(domain_id, str(mxl_flow) if mxl_flow else None),
                )
        return out

    def sources(self) -> list[dict[str, Any]]:
        out = []
        grain = {
            "numerator": self.settings.frame_rate_num,
            "denominator": self.settings.frame_rate_den,
        }
        count = max(1, self.settings.audio_channels)
        if count == 2:
            channels = [
                {"label": "Left Channel", "symbol": "L"},
                {"label": "Right Channel", "symbol": "R"},
            ]
        else:
            channels = [
                {"label": f"Channel {index + 1}", "symbol": f"NSC{index + 1:03d}"}
                for index in range(count)
            ]
        for panel in self.mixer.panels or []:
            video = {
                "id": ids.source_id(self.seed, panel.id, "video"),
                "version": nmos_version(),
                "label": f"{panel.label} PGM Video source",
                "description": "",
                "tags": {GROUPHINT: [f"{panel.id}:PgmVideo"]},
                "device_id": self.device_uuid,
                "parents": [],
                "clock_name": "clk0",
                "caps": {},
                "format": FORMAT_VIDEO,
                "grain_rate": dict(grain),
            }
            audio = {
                "id": ids.source_id(self.seed, panel.id, "audio"),
                "version": nmos_version(),
                "label": f"{panel.label} PGM Audio source",
                "description": "",
                "tags": {GROUPHINT: [f"{panel.id}:PgmAudio"]},
                "device_id": self.device_uuid,
                "parents": [],
                "clock_name": "clk0",
                "caps": {},
                "format": FORMAT_AUDIO,
                "grain_rate": dict(grain),
                "channels": channels,
            }
            out.extend([video, audio])
        return out

    def flows(self) -> list[dict[str, Any]]:
        out = []
        outputs = self.mixer.outputs
        grain = {
            "numerator": self.settings.frame_rate_num,
            "denominator": self.settings.frame_rate_den,
        }
        width = self.settings.width
        height = self.settings.height
        for panel in self.mixer.panels or []:
            video = {
                "id": ids.flow_id(self.seed, panel.id, "video"),
                "version": nmos_version(),
                "label": f"{panel.label} PGM Video",
                "description": "",
                "tags": {GROUPHINT: [f"{panel.id}:PgmVideo"]},
                "source_id": ids.source_id(self.seed, panel.id, "video"),
                "device_id": self.device_uuid,
                "parents": [],
                "format": FORMAT_VIDEO,
                "media_type": self.settings.video_media_type,
                "grain_rate": dict(grain),
                "frame_width": width,
                "frame_height": height,
                "colorspace": "BT709",
                "interlace_mode": "progressive",
                "transfer_characteristic": "SDR",
                "components": [
                    {"name": "Y", "width": width, "height": height, "bit_depth": 10},
                    {"name": "Cb", "width": width // 2, "height": height, "bit_depth": 10},
                    {"name": "Cr", "width": width // 2, "height": height, "bit_depth": 10},
                ],
            }
            audio = {
                "id": ids.flow_id(self.seed, panel.id, "audio"),
                "version": nmos_version(),
                "label": f"{panel.label} PGM Audio",
                "description": "",
                "tags": {GROUPHINT: [f"{panel.id}:PgmAudio"]},
                "source_id": ids.source_id(self.seed, panel.id, "audio"),
                "device_id": self.device_uuid,
                "parents": [],
                "format": FORMAT_AUDIO,
                "media_type": self.settings.audio_media_type,
                "grain_rate": dict(grain),
                "sample_rate": {"numerator": self.settings.audio_rate, "denominator": 1},
                "bit_depth": 32,
            }
            if outputs:
                video["tags"]["urn:x-nmos:tag:mxl_flow_id"] = [outputs.video_flow_id]
                audio["tags"]["urn:x-nmos:tag:mxl_flow_id"] = [outputs.audio_flow_id]
            out.extend([video, audio])
        return out

    def list_ids(self, collection: str) -> list[str]:
        mapping = {
            "receivers": self.receivers,
            "senders": self.senders,
            "sources": self.sources,
            "flows": self.flows,
            "devices": lambda: [self.device_resource()],
        }
        if collection not in mapping:
            return []
        return [f"{item['id']}/" for item in mapping[collection]()]

    def get_resource(self, collection: str, resource_id: str) -> dict[str, Any] | None:
        collection = collection.rstrip("/")
        resource_id = resource_id.rstrip("/")
        if collection == "devices" and resource_id == self.device_uuid:
            return self.device_resource()
        mapping = {
            "receivers": self.receivers,
            "senders": self.senders,
            "sources": self.sources,
            "flows": self.flows,
        }
        if collection not in mapping:
            return None
        for item in mapping[collection]():
            if item["id"] == resource_id:
                return item
        return None

    def _lookup_receiver(self, receiver_id: str) -> tuple[str, str] | None:
        for item in self._live_inputs():
            for role in ("video", "audio"):
                if ids.receiver_id(self.seed, item.id, role) == receiver_id:
                    return item.id, role
        return None

    def constraints(self, resource_id: str, side: str) -> dict[str, Any]:
        resource_id = resource_id.rstrip("/")
        side = side.rstrip("/")
        if side == "receivers":
            return {
                "mxl_domain_id": {},
                "mxl_flow_id": {},
            }
        domain_id = self.settings.resolved_output_domain_id
        flow = self._sender_mxl_flow(resource_id)
        return {
            "mxl_domain_id": {"enum": [domain_id]},
            "mxl_flow_id": {"enum": [flow]} if flow else {},
        }

    def staged(self, resource_id: str, side: str) -> dict[str, Any]:
        resource_id = resource_id.rstrip("/")
        side = side.rstrip("/")
        if resource_id in self._staged:
            return self._staged[resource_id]
        return self.active(resource_id, side)

    def active(self, resource_id: str, side: str) -> dict[str, Any]:
        resource_id = resource_id.rstrip("/")
        side = side.rstrip("/")
        if resource_id in self._active:
            return self._active[resource_id]
        if side == "receivers":
            return self._empty_receiver_active()
        domain_id = self.settings.resolved_output_domain_id
        return self._empty_sender_active(domain_id, self._sender_mxl_flow(resource_id))

    def _sender_mxl_flow(self, resource_id: str) -> str | None:
        outputs = self.mixer.outputs
        if not outputs:
            return None
        for panel in self.mixer.panels or []:
            if ids.sender_id(self.seed, panel.id, "video") == resource_id:
                return outputs.video_flow_id
            if ids.sender_id(self.seed, panel.id, "audio") == resource_id:
                return outputs.audio_flow_id
        return None

    def patch_staged(self, resource_id: str, side: str, body: dict[str, Any]) -> dict[str, Any]:
        resource_id = resource_id.rstrip("/")
        side = side.rstrip("/")
        unknown = sorted(set(body) - STAGED_PATCH_FIELDS)
        if unknown:
            raise NmosActivationError(f"unknown fields: {', '.join(unknown)}")
        with self.lock:
            current = dict(self.staged(resource_id, side))
            if "master_enable" in body:
                current["master_enable"] = bool(body["master_enable"])
            if "sender_id" in body:
                try:
                    current["sender_id"] = _uuid_or_none(body["sender_id"])
                except ValueError as exc:
                    raise NmosActivationError(str(exc)) from exc
                if current["sender_id"] == "auto":
                    raise NmosActivationError("sender_id must not be auto")
            if "receiver_id" in body:
                try:
                    current["receiver_id"] = _uuid_or_none(body["receiver_id"])
                except ValueError as exc:
                    raise NmosActivationError(str(exc)) from exc
            if "transport_params" in body:
                current["transport_params"] = self._validate_params(
                    resource_id, side, body["transport_params"], current
                )
            if "activation" in body and body["activation"]:
                current["activation"] = body["activation"]
            if "transport_file" in body:
                tf = body["transport_file"]
                if tf not in (None, {}) and not (
                    isinstance(tf, dict)
                    and tf.get("data") is None
                    and tf.get("type") is None
                ):
                    raise NmosActivationError("MXL receivers must not be given a transport file")
            self._staged[resource_id] = current
            activation = (body.get("activation") or {}).get("mode")
            if activation in {None, ""}:
                return current
            if activation == "activate_immediate":
                return self.activate(resource_id, side)
            if activation in {"activate_scheduled_relative", "activate_scheduled_absolute"}:
                return self._schedule_activation(resource_id, side, current["activation"])
            raise NmosActivationError(f"unsupported activation mode {activation!r}")

    def _validate_params(
        self,
        resource_id: str,
        side: str,
        params: Any,
        current: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not isinstance(params, list) or not params:
            raise NmosActivationError("transport_params must be a non-empty array")
        entry = params[0] if isinstance(params[0], dict) else {}
        prev = (current.get("transport_params") or [{}])[0]
        try:
            if "mxl_domain_id" in entry:
                domain_id = _uuid_or_none(entry.get("mxl_domain_id"))
            else:
                domain_id = prev.get("mxl_domain_id")
            if "mxl_flow_id" in entry:
                flow_id = _uuid_or_none(entry.get("mxl_flow_id"))
            else:
                flow_id = prev.get("mxl_flow_id")
        except ValueError as exc:
            raise NmosActivationError(str(exc)) from exc
        if side == "receivers" and flow_id == "auto":
            raise NmosActivationError("receiver mxl_flow_id must not be auto")
        if domain_id == "auto":
            domain_id = self.settings.resolved_output_domain_id
        if side == "senders" and flow_id == "auto":
            flow_id = self._sender_mxl_flow(resource_id)
        return [{"mxl_domain_id": domain_id, "mxl_flow_id": flow_id}]

    def activate(self, resource_id: str, side: str) -> dict[str, Any]:
        staged = dict(self.staged(resource_id, side))
        now = nmos_version()
        pending = staged.get("activation") or {}
        mode = pending.get("mode")
        if mode in {"activate_scheduled_relative", "activate_scheduled_absolute"}:
            activation = {
                "mode": mode,
                "requested_time": pending.get("requested_time"),
                "activation_time": pending.get("activation_time") or now,
            }
        else:
            activation = {
                "mode": "activate_immediate",
                "requested_time": None,
                "activation_time": now,
            }
        active = dict(staged)
        active["activation"] = activation
        self._cancel_scheduled(resource_id)
        self._active[resource_id] = active
        staged_reset = dict(active)
        staged_reset["activation"] = dict(NULL_ACTIVATION)
        self._staged[resource_id] = staged_reset
        try:
            if side == "receivers":
                self._apply_receiver(resource_id, active)
            self.activations_ok += 1
        except NmosActivationError:
            self.activations_error += 1
            raise
        return active

    def _cancel_scheduled(self, resource_id: str) -> None:
        timer = self._scheduled.pop(resource_id, None)
        if timer is not None:
            timer.cancel()

    def _schedule_activation(
        self, resource_id: str, side: str, activation: dict[str, Any]
    ) -> dict[str, Any]:
        mode = activation.get("mode")
        requested = activation.get("requested_time")
        if not isinstance(requested, str) or ":" not in requested:
            raise NmosActivationError(
                "scheduled activation requires requested_time as seconds:nanoseconds"
            )
        try:
            seconds_s, nanos_s = requested.split(":", 1)
            seconds = int(seconds_s)
            nanos = int(nanos_s)
        except ValueError as exc:
            raise NmosActivationError("invalid requested_time") from exc
        if mode == "activate_scheduled_relative":
            delay = max(0.0, seconds + nanos / 1_000_000_000)
            fire_at = time.time_ns() + TAI_OFFSET_NS + int(delay * 1_000_000_000)
        else:
            fire_at = seconds * 1_000_000_000 + nanos
            now = time.time_ns() + TAI_OFFSET_NS
            delay = max(0.0, (fire_at - now) / 1_000_000_000)
        activation_time = f"{fire_at // 10**9}:{fire_at % 10**9}"
        current = dict(self._staged[resource_id])
        current["activation"] = {
            "mode": mode,
            "requested_time": requested,
            "activation_time": activation_time,
        }
        self._staged[resource_id] = current
        self._cancel_scheduled(resource_id)

        def _fire() -> None:
            with self.lock:
                staged = self._staged.get(resource_id)
                if not staged:
                    return
                pending = staged.get("activation") or {}
                if pending.get("activation_time") != activation_time:
                    return
                try:
                    self.activate(resource_id, side)
                except NmosActivationError:
                    log.debug("scheduled activation failed for %s", resource_id, exc_info=True)

        timer = threading.Timer(delay, _fire)
        timer.daemon = True
        self._scheduled[resource_id] = timer
        timer.start()
        return current

    def _apply_receiver(self, resource_id: str, active: dict[str, Any]) -> None:
        found = self._lookup_receiver(resource_id)
        if found is None:
            raise NmosActivationError("unknown receiver")
        input_id, role = found
        params = (active.get("transport_params") or [{}])[0]
        enabled = bool(active.get("master_enable"))
        domain_id = params.get("mxl_domain_id") or self.settings.resolved_output_domain_id
        flow_id = params.get("mxl_flow_id")
        states = self._input_states.setdefault(input_id, {})
        if not enabled or not flow_id:
            states[role] = "not_routed"
        else:
            states[role] = flow_presence(self.settings.mxl_root, str(domain_id), str(flow_id))
        self.mixer.apply_nmos_receiver(
            input_id,
            role,
            domain_id=str(domain_id) if domain_id else None,
            flow_id=str(flow_id) if flow_id else None,
            enabled=enabled,
        )

    def sync_from_rest(self, input_id: str) -> None:
        """REST PATCH /inputs/{id} → IS-05 active + IS-04 subscription."""
        try:
            item = self.mixer.get_input(input_id)
        except Exception:
            return
        if item.kind != InputKind.mxl_live:
            self._drop_input_receivers(input_id)
            return
        with self.lock:
            for role, essence in (("video", item.video), ("audio", item.audio)):
                rid = ids.receiver_id(self.seed, item.id, role)
                flow_id = str(essence.flow_id) if essence and essence.flow_id else None
                domain_id = (
                    str(essence.domain_id)
                    if essence and essence.domain_id
                    else self.settings.resolved_output_domain_id
                )
                enabled = bool(flow_id)
                active = {
                    "sender_id": (self._active.get(rid) or {}).get("sender_id"),
                    "master_enable": enabled,
                    "activation": {
                        "mode": "activate_immediate",
                        "requested_time": None,
                        "activation_time": nmos_version(),
                    },
                    "transport_params": [
                        {"mxl_domain_id": domain_id, "mxl_flow_id": flow_id}
                    ],
                    "transport_file": {"data": None, "type": None},
                }
                self._active[rid] = active
                staged = dict(active)
                staged["activation"] = dict(NULL_ACTIVATION)
                self._staged[rid] = staged
                states = self._input_states.setdefault(input_id, {})
                if not enabled:
                    states[role] = "not_routed"
                else:
                    states[role] = flow_presence(
                        self.settings.mxl_root, domain_id, flow_id
                    )

    def reconcile_inputs(self) -> None:
        """Create/remove receivers when live inputs appear or disappear."""
        with self.lock:
            live_ids = {item.id for item in self._live_inputs()}
            for item in self._live_inputs():
                self.sync_from_rest(item.id)
            for input_id in list(self._input_states):
                if input_id not in live_ids:
                    self._drop_input_receivers(input_id)
            valid_receivers = {
                ids.receiver_id(self.seed, item.id, role)
                for item in self._live_inputs()
                for role in ("video", "audio")
            }
            senders = self._sender_ids()
            for rid in list(self._active):
                if rid not in valid_receivers and rid not in senders:
                    self._active.pop(rid, None)
                    self._staged.pop(rid, None)

    def _drop_input_receivers(self, input_id: str) -> None:
        self._input_states.pop(input_id, None)
        for role in ("video", "audio"):
            rid = ids.receiver_id(self.seed, input_id, role)
            self._active.pop(rid, None)
            self._staged.pop(rid, None)

    def refresh_waiting(self) -> None:
        """Promote waiting/no_signal receivers when the flow appears (no extra PATCH)."""
        with self.lock:
            for item in self._live_inputs():
                for role in ("video", "audio"):
                    if self.input_state(item.id, role) not in {
                        "waiting",
                        "no_signal",
                        "running",
                    }:
                        continue
                    rid = ids.receiver_id(self.seed, item.id, role)
                    active = self._active.get(rid)
                    if not active:
                        continue
                    try:
                        self._apply_receiver(rid, active)
                    except NmosActivationError:
                        log.debug("refresh waiting failed for %s", rid, exc_info=True)

    def sync_senders_from_outputs(self) -> None:
        outputs = self.mixer.outputs
        if outputs is None:
            return
        domain_id = self.settings.resolved_output_domain_id
        with self.lock:
            for panel in self.mixer.panels or []:
                for role, flow in (
                    ("video", outputs.video_flow_id),
                    ("audio", outputs.audio_flow_id),
                ):
                    sid = ids.sender_id(self.seed, panel.id, role)
                    self._active[sid] = self._empty_sender_active(domain_id, flow)
                    self._staged[sid] = dict(self._active[sid])
