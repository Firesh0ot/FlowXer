from __future__ import annotations

from typing import Any


def _esc(value: Any) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _line(name: str, value: float | int, labels: dict[str, Any] | None = None) -> str:
    if not labels:
        return f"{name} {value}"
    inner = ",".join(f'{key}="{_esc(val)}"' for key, val in labels.items())
    return f"{name}{{{inner}}} {value}"


def render_prometheus(mixer) -> str:
    settings = mixer.settings
    resources = None
    try:
        from flowxer.engine.resources import collect_resources

        resources = collect_resources(mixer)
    except Exception:
        resources = {}
    nmos = mixer.nmos.status() if getattr(mixer, "nmos", None) else {}
    on_air = 1 if getattr(mixer.state, "value", mixer.state) == "running" else 0
    gst_mode = mixer.backend if mixer.backend != "idle" else settings.gst_mode
    lines = [
        "# HELP flowxer_info FlowXer build and mode",
        "# TYPE flowxer_info gauge",
        _line(
            "flowxer_info",
            1,
            {
                "version": settings.version,
                "mxl_revision": settings.resolved_mxl_revision,
                "gst_mode": gst_mode,
                "nmos_enabled": str(bool(nmos.get("enabled"))).lower(),
                "media_path": getattr(getattr(mixer, "media", None), "path", "cpu"),
            },
        ),
        "# HELP flowxer_on_air 1 when the mixer pipeline is running",
        "# TYPE flowxer_on_air gauge",
        _line("flowxer_on_air", on_air),
        "# HELP flowxer_program_input Currently selected program source",
        "# TYPE flowxer_program_input gauge",
        _line(
            "flowxer_program_input",
            1 if mixer.program_input_id else 0,
            {"input": mixer.program_input_id or ""},
        ),
        "# HELP flowxer_preview_input Currently selected preview source",
        "# TYPE flowxer_preview_input gauge",
        _line(
            "flowxer_preview_input",
            1 if mixer.preview_input_id else 0,
            {"input": mixer.preview_input_id or ""},
        ),
        "# HELP flowxer_frames_rendered_total Program frames that reached the video output",
        "# TYPE flowxer_frames_rendered_total counter",
        _line("flowxer_frames_rendered_total", int(getattr(mixer, "frames_rendered", 0))),
        "# HELP flowxer_program_stalled 1 while on air without a new Program frame for 3 s",
        "# TYPE flowxer_program_stalled gauge",
        _line("flowxer_program_stalled", int(mixer.program_stalled_s() is not None) if hasattr(mixer, "program_stalled_s") else 0),
        "# HELP flowxer_frames_dropped_total Program frames skipped to stay on the MXL timeline (mixer late)",
        "# TYPE flowxer_frames_dropped_total counter",
        _line("flowxer_frames_dropped_total", int(getattr(mixer, "frames_dropped", 0))),
        "# HELP flowxer_program_buffers_dropped_total Program buffers dropped before mxlsink because they went back in time",
        "# TYPE flowxer_program_buffers_dropped_total counter",
        *(
            _line("flowxer_program_buffers_dropped_total", count, {"essence": essence})
            for essence, count in (getattr(mixer, "program_dropped", None) or {"video": 0, "audio": 0}).items()
        ),
        "# HELP flowxer_control_plane_busy_seconds Age of the oldest running control-plane operation (0: none)",
        "# TYPE flowxer_control_plane_busy_seconds gauge",
        _line("flowxer_control_plane_busy_seconds", round(_busy_seconds(mixer), 3)),
        "# HELP flowxer_input_late_grains_total MXL grains arrived late",
        "# TYPE flowxer_input_late_grains_total counter",
        _line("flowxer_input_late_grains_total", int(getattr(mixer, "late_grains", 0))),
        "# HELP flowxer_input_resyncs_total MXL reader resyncs",
        "# TYPE flowxer_input_resyncs_total counter",
        _line("flowxer_input_resyncs_total", int(getattr(mixer, "resyncs", 0))),
        "# HELP flowxer_pipeline_errors_total GStreamer pipeline error messages",
        "# TYPE flowxer_pipeline_errors_total counter",
        _line("flowxer_pipeline_errors_total", int(getattr(mixer, "pipeline_errors", 0))),
        "# HELP flowxer_webrtc_peers Active WHEP peers",
        "# TYPE flowxer_webrtc_peers gauge",
    ]
    try:
        from flowxer.engine.webrtc import peer_count

        peers = peer_count()
    except Exception:
        peers = 0
    lines.append(_line("flowxer_webrtc_peers", peers))
    lines += [
        "# HELP flowxer_nmos_registry_up 1 when the last registry heartbeat succeeded",
        "# TYPE flowxer_nmos_registry_up gauge",
        _line("flowxer_nmos_registry_up", 1 if nmos.get("registry_up") else 0),
        "# HELP flowxer_nmos_activations_total IS-05 activations by result",
        "# TYPE flowxer_nmos_activations_total counter",
        _line(
            "flowxer_nmos_activations_total",
            int(getattr(mixer.nmos, "activations_ok", 0)),
            {"result": "ok"},
        ),
        _line(
            "flowxer_nmos_activations_total",
            int(getattr(mixer.nmos, "activations_error", 0)),
            {"result": "error"},
        ),
        "# HELP flowxer_tally_send_errors_total Failed TSL sends",
        "# TYPE flowxer_tally_send_errors_total counter",
        _line(
            "flowxer_tally_send_errors_total",
            int(getattr(mixer.tally, "send_errors", 0)),
        ),
    ]
    export = getattr(mixer, "tally_export", None)
    if export is not None:
        lines += [
            "# HELP flowxer_tally_export_packets_total TSL packets sent to FLOWXER_TALLY_TSL",
            "# TYPE flowxer_tally_export_packets_total counter",
            _line("flowxer_tally_export_packets_total", export.packets_sent),
            "# HELP flowxer_tally_export_send_errors_total Failed sends to FLOWXER_TALLY_TSL",
            "# TYPE flowxer_tally_export_send_errors_total counter",
            _line("flowxer_tally_export_send_errors_total", export.send_errors),
            "# HELP flowxer_tally_export_last_success_timestamp_seconds Unix time of the last good send (0: none yet)",
            "# TYPE flowxer_tally_export_last_success_timestamp_seconds gauge",
            _line("flowxer_tally_export_last_success_timestamp_seconds", export.last_success),
        ]
    lines += [
        "# HELP flowxer_process_cpu_percent Process CPU percent",
        "# TYPE flowxer_process_cpu_percent gauge",
        _line("flowxer_process_cpu_percent", float(resources.get("cpu_percent") or 0)),
        "# HELP flowxer_process_memory_bytes Process memory bytes",
        "# TYPE flowxer_process_memory_bytes gauge",
        _line("flowxer_process_memory_bytes", int(resources.get("memory_bytes") or 0)),
        "# HELP flowxer_transitions_total Mixer transitions by type",
        "# TYPE flowxer_transitions_total counter",
    ]
    counts = getattr(mixer, "transition_counts", {}) or {}
    for kind in ("cut", "mix", "stinger"):
        lines.append(_line("flowxer_transitions_total", int(counts.get(kind, 0)), {"type": kind}))
    lines += [
        "# HELP flowxer_input_restarts_total MXL sources started again after they failed",
        "# TYPE flowxer_input_restarts_total counter",
    ]
    restarts = getattr(mixer, "input_restarts", {}) or {}
    for (input_id, essence), count in sorted(restarts.items()):
        lines.append(_line("flowxer_input_restarts_total", count, {"input": input_id, "essence": essence}))
    lines += [
        "# HELP flowxer_input_state 1 for the current per-essence input state",
        "# TYPE flowxer_input_state gauge",
    ]
    for item in mixer.list_inputs():
        for role in ("video", "audio"):
            if getattr(item.kind, "value", item.kind) == "mxl_live" and getattr(mixer, "nmos", None):
                state = mixer.nmos.input_state(item.id, role)
            else:
                state = "running" if on_air else "idle"
            for candidate in ("not_routed", "waiting", "no_signal", "running", "error", "idle"):
                lines.append(
                    _line(
                        "flowxer_input_state",
                        1 if state == candidate else 0,
                        {"input": item.id, "essence": role, "state": candidate},
                    )
                )
    return "\n".join(lines) + "\n"


def _busy_seconds(mixer) -> float:
    watchdog = getattr(mixer, "watchdog", None)
    oldest = watchdog.oldest() if watchdog is not None else None
    return oldest[1] if oldest else 0.0


def live_payload(mixer) -> tuple[int, dict[str, Any]]:
    """503 when a control-plane operation (IS-05 activation, Program start or stop, a source
    restart) has run longer than the watchdog allows: the pod needs a restart."""
    watchdog = getattr(mixer, "watchdog", None)
    stuck = watchdog.stuck() if watchdog is not None else None
    if stuck is None:
        return 200, {"status": "live"}
    operation, seconds = stuck
    return 503, {
        "status": "stuck",
        "reason": f"{operation} has not finished for {seconds:.0f} s (limit {watchdog.stuck_after_s:.0f} s)",
    }


def ready_payload(mixer) -> tuple[int, dict[str, Any]]:
    settings = mixer.settings
    reasons: list[str] = []
    root = settings.mxl_root
    if not root.exists() or not root.is_dir():
        reasons.append(f"MXL root not readable: {root}")
    output = settings.output_domain
    try:
        output.mkdir(parents=True, exist_ok=True)
        probe = output / ".flowxer-ready"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        reasons.append(f"output domain not writable: {output} ({exc})")
    nmos = getattr(mixer, "nmos", None)
    if settings.nmos_enable and nmos is None:
        reasons.append("NMOS enabled but node is missing")
    elif nmos is not None and not nmos.registered():
        reasons.append(f"not registered with {settings.resolved_registry_url}")
    body = {
        "ready": not reasons,
        "reasons": reasons,
        "nmos_registry_up": bool(getattr(nmos, "registry_up", False)),
        "mixer_state": getattr(mixer.state, "value", str(mixer.state)),
    }
    return (200 if not reasons else 503, body)
