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
    engine = getattr(mixer, "engine", None)
    engine_status = (engine.status if engine is not None else None) or {}
    engine_info = engine_status.get("engine") or {}
    lines = [
        "# HELP flowxer_info FlowXer build and mode",
        "# TYPE flowxer_info gauge",
        _line(
            "flowxer_info",
            1,
            {
                "version": settings.version,
                "mxl_revision": settings.resolved_mxl_revision,
                "media": "simulate" if settings.simulate else "engine",
                "nmos_enabled": str(bool(nmos.get("enabled"))).lower(),
                "engine_version": engine_info.get("version", ""),
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
        "# HELP flowxer_frames_rendered_total Program grains the engine wrote",
        "# TYPE flowxer_frames_rendered_total counter",
        _line("flowxer_frames_rendered_total", int(getattr(mixer, "frames_rendered", 0))),
        "# HELP flowxer_program_stalled 1 while on air without a new Program frame for 3 s",
        "# TYPE flowxer_program_stalled gauge",
        _line("flowxer_program_stalled", int(mixer.program_stalled_s() is not None) if hasattr(mixer, "program_stalled_s") else 0),
        "# HELP flowxer_frames_dropped_total Output grains skipped to stay on the TAI grid (engine late)",
        "# TYPE flowxer_frames_dropped_total counter",
        _line("flowxer_frames_dropped_total", int(getattr(mixer, "frames_dropped", 0))),
        "# HELP flowxer_engine_up 1 while the media engine answers",
        "# TYPE flowxer_engine_up gauge",
        _line("flowxer_engine_up", 1 if engine is not None and engine.ready else 0),
        "# HELP flowxer_engine_restarts_total Media engine restarts after it exited (this Program run)",
        "# TYPE flowxer_engine_restarts_total counter",
        _line("flowxer_engine_restarts_total", int(engine.restarts) if engine is not None else 0),
        "# HELP flowxer_latency_grains Fixed processing latency (FLOWXER_LATENCY)",
        "# TYPE flowxer_latency_grains gauge",
        _line("flowxer_latency_grains", settings.latency),
        "# HELP flowxer_recommended_latency_grains The latency that covers the latest input arrival seen",
        "# TYPE flowxer_recommended_latency_grains gauge",
        _line("flowxer_recommended_latency_grains", int(engine_info.get("recommended_latency_grains") or 0)),
        "# HELP flowxer_control_plane_busy_seconds Age of the oldest running control-plane operation (0: none)",
        "# TYPE flowxer_control_plane_busy_seconds gauge",
        _line("flowxer_control_plane_busy_seconds", round(_busy_seconds(mixer), 3)),
    ]
    lines += _input_lines(mixer)
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
    for kind in ("cut", "mix", "wipe", "stinger"):
        lines.append(_line("flowxer_transitions_total", int(counts.get(kind, 0)), {"type": kind}))
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
    lines += _library_lines(getattr(getattr(mixer, "library", None), "queue", None))
    text = "\n".join(lines) + "\n"
    # The engine's own metrics (flowxer_engine_*: GPU time per stage, inputs by engine name).
    if engine is not None:
        text += engine.metrics()
    return text


def _input_lines(mixer) -> list[str]:
    """Per input (FlowXer ids): how late its grains arrive (ms after their TAI time, the 5 s
    maximum and average), and grains or audio blocks that were missing."""
    runtime = mixer.source_runtime() if hasattr(mixer, "source_runtime") else {}
    lines = [
        "# HELP flowxer_input_arrival_ms How late an input's grains arrive after their TAI time (video), how far its audio head is behind TAI (audio)",
        "# TYPE flowxer_input_arrival_ms gauge",
    ]
    missing = [
        "# HELP flowxer_input_missing_total Output grains without the input's grain (video) or samples (audio), start-up windows not counted",
        "# TYPE flowxer_input_missing_total counter",
    ]
    late = [
        "# HELP flowxer_input_late_total Grains of the input that came too late for their output grain (its last grain was shown again)",
        "# TYPE flowxer_input_late_total counter",
    ]
    for input_id, entry in sorted(runtime.items()):
        for essence, key in (("video", "arrival_ms"), ("audio", "audio_lag_ms")):
            value = entry.get(key)
            if not isinstance(value, dict):
                continue
            for stat in ("avg", "max"):
                lines.append(_line("flowxer_input_arrival_ms", value.get(stat, 0), {"input": input_id, "essence": essence, "stat": stat}))
        if "missing" in entry:
            missing.append(_line("flowxer_input_missing_total", int(entry.get("missing") or 0), {"input": input_id, "essence": "video"}))
            missing.append(_line("flowxer_input_missing_total", int(entry.get("audio_missing") or 0), {"input": input_id, "essence": "audio"}))
        if "late" in entry and "missing" in entry:
            late.append(_line("flowxer_input_late_total", int(entry.get("late") or 0), {"input": input_id}))
    return lines + missing + late


def _busy_seconds(mixer) -> float:
    watchdog = getattr(mixer, "watchdog", None)
    oldest = watchdog.oldest() if watchdog is not None else None
    return oldest[1] if oldest else 0.0


def _library_lines(queue) -> list[str]:
    """The media library's conversions: jobs waiting and running, finished ones by result, the
    last one's duration, and whether NVENC encodes the clip mezzanines (once it was tried)."""
    if queue is None:
        return []
    jobs = queue.list_jobs()
    lines = [
        "# HELP flowxer_library_jobs Conversion jobs queued or running",
        "# TYPE flowxer_library_jobs gauge",
    ]
    for state in ("queued", "running"):
        lines.append(_line("flowxer_library_jobs", sum(1 for j in jobs if j.state.value == state), {"state": state}))
    lines += [
        "# HELP flowxer_library_conversions_total Finished conversions by result",
        "# TYPE flowxer_library_conversions_total counter",
    ]
    for result in ("done", "failed", "cancelled"):
        lines.append(_line("flowxer_library_conversions_total", queue.finished.get(result, 0), {"result": result}))
    lines += [
        "# HELP flowxer_library_last_conversion_seconds How long the last finished conversion took",
        "# TYPE flowxer_library_last_conversion_seconds gauge",
        _line("flowxer_library_last_conversion_seconds", round(queue.last_seconds, 3)),
    ]
    nvenc = getattr(queue.ffmpeg, "_nvenc", None)
    if nvenc is not None:
        lines += [
            "# HELP flowxer_library_nvenc 1 when NVENC encodes the clip mezzanines, 0 when x265 does",
            "# TYPE flowxer_library_nvenc gauge",
            _line("flowxer_library_nvenc", 1 if nvenc else 0),
        ]
    return lines


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
