"""Background conversion job queue."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable

from flowxer.engine.formats import VideoFormat, format_by_id
from flowxer.library.conform import (
    crossfade_loop_file,
    cut_frame_after_fps_change,
    samples_until_grain,
)
from flowxer.library.ffmpeg import (
    FFmpeg,
    RunResult,
    build_clip_audio_command,
    build_clip_mux_command,
    build_clip_video_command,
    build_stinger_mezz_command,
    build_thumb_command,
    build_video_filter,
    conversion_timeout,
    has_alpha_pix_fmt,
    parse_frame_rate,
)
from flowxer.library.models import (
    ConversionInfo,
    ConversionStatus,
    ConvertJob,
    ConvertOptions,
    JobState,
    LibraryItem,
    LibraryKind,
)
from flowxer.library.ram import decide_ram_playback
from flowxer.library.sequence import safe_extract_tga_zip, validate_sequence
from flowxer.library.store import LibraryStore

log = logging.getLogger(__name__)

JobListener = Callable[[ConvertJob], None]

_ACTIVE = {JobState.queued, JobState.running}
# Finished jobs kept for GET /jobs.
_KEEP_FINISHED = 100


class _Cancelled(Exception):
    pass


class ConversionQueue:
    def __init__(
        self,
        store: LibraryStore,
        *,
        concurrency: int = 1,
        ffmpeg: FFmpeg | None = None,
        ram_clip_max_s: float = 20.0,
        ram_budget_mb: int = 4096,
        on_update: JobListener | None = None,
    ) -> None:
        self.store = store
        self.concurrency = max(1, min(8, concurrency))
        self.ffmpeg = ffmpeg or FFmpeg()
        self.ram_clip_max_s = ram_clip_max_s
        self.ram_budget_bytes = ram_budget_mb << 20
        self.on_update = on_update
        self._jobs: dict[str, ConvertJob] = {}
        self._queue: deque[str] = deque()
        # Imports of legacy storage: run after everything an operator queued.
        self._low: deque[str] = deque()
        # job id -> cancel event of a running job (kills its ffmpeg)
        self._running: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._workers: list[threading.Thread] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
            for index in range(self.concurrency):
                thread = threading.Thread(
                    target=self._worker_loop,
                    name=f"flowxer-convert-{index}",
                    daemon=True,
                )
                thread.start()
                self._workers.append(thread)

    def stop(self, timeout: float = 3.0) -> None:
        """Kill running conversions and wait at most ``timeout`` seconds in total."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            for event in self._running.values():
                event.set()
        deadline = time.monotonic() + timeout
        for thread in self._workers:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._workers.clear()
        self._started = False

    def list_jobs(self) -> list[ConvertJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get_job(self, job_id: str) -> ConvertJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> ConvertJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.state not in _ACTIVE:
                return job
            if job.state == JobState.queued:
                for queue in (self._queue, self._low):
                    if job_id in queue:
                        queue.remove(job_id)
                job.state = JobState.cancelled
                job.error = "cancelled"
                job.finished_at = time.time()
                self._settle_item(job, "cancelled")
                self._emit(job)
            else:
                # The worker kills ffmpeg, cleans up and marks the job cancelled.
                self._running[job_id].set()
            return job

    def cancel_item(self, item_id: str, wait_s: float = 0.0) -> None:
        """Cancel every job of an item (it is being deleted); wait for running ones to stop."""
        with self._lock:
            ids = [j.id for j in self._jobs.values() if j.item_id == item_id and j.state in _ACTIVE]
        for job_id in ids:
            self.cancel(job_id)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            with self._lock:
                if not any(self._jobs[j].state == JobState.running for j in ids):
                    return
            time.sleep(0.05)

    def enqueue(
        self,
        item_id: str,
        format_id: str,
        options: ConvertOptions | None = None,
        *,
        low_priority: bool = False,
    ) -> ConvertJob:
        format_by_id(format_id)  # validate
        if self.store.load(item_id) is None:
            raise KeyError(item_id)
        if options is not None:
            self.store.update(item_id, lambda item: setattr(item, "options", options))
        with self._lock:
            for existing in self._jobs.values():
                if (
                    existing.item_id == item_id
                    and existing.format_id == format_id
                    and existing.state in _ACTIVE
                ):
                    return existing
            job = ConvertJob(
                id=uuid.uuid4().hex[:12],
                item_id=item_id,
                format_id=format_id,
                created_at=time.time(),
            )
            self._prune_jobs_locked()
            self._jobs[job.id] = job
            (self._low if low_priority else self._queue).append(job.id)

            def queued(item: LibraryItem) -> None:
                # A ready conversion stays playable until the new one replaces it.
                if not self._ready_on_disk(item, format_id):
                    item.conversions[format_id] = ConversionInfo(status=ConversionStatus.queued)

            self.store.update(item_id, queued)
            self._emit(job)
            self._wake.set()
            return job

    def _prune_jobs_locked(self) -> None:
        finished = sorted(
            (j for j in self._jobs.values() if j.state not in _ACTIVE),
            key=lambda j: j.finished_at or j.created_at,
            reverse=True,
        )
        for job in finished[_KEEP_FINISHED:]:
            self._jobs.pop(job.id, None)

    def _emit(self, job: ConvertJob) -> None:
        if self.on_update:
            try:
                self.on_update(job)
            except Exception:  # pragma: no cover - listener errors must not kill workers
                log.exception("job listener failed")

    def _ready_on_disk(self, item: LibraryItem, format_id: str) -> bool:
        return item.is_ready(format_id) and self.store.mezz_path(item.id, format_id).is_file()

    def _settle_item(self, job: ConvertJob, error: str) -> None:
        """After a failed or cancelled job: keep a ready mezzanine, else mark the item failed."""

        def settle(item: LibraryItem) -> None:
            if self._ready_on_disk(item, job.format_id):
                item.conversions[job.format_id].error = f"reconversion {error}"
            else:
                item.conversions[job.format_id] = ConversionInfo(status=ConversionStatus.failed, error=error)

        self.store.update(job.item_id, settle)

    def _next_job(self) -> tuple[ConvertJob, threading.Event] | None:
        with self._lock:
            for queue in (self._queue, self._low):
                while queue:
                    job = self._jobs.get(queue.popleft())
                    if job is None or job.state != JobState.queued:
                        continue
                    job.state = JobState.running
                    job.started_at = time.time()
                    job.progress = 0.05
                    cancel = threading.Event()
                    self._running[job.id] = cancel
                    self._emit(job)
                    return job, cancel
        return None

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            picked = self._next_job()
            if picked is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            job, cancel = picked
            try:
                self._run_job(job, cancel)
            except _Cancelled:
                job.state = JobState.cancelled
                job.error = "cancelled"
            except Exception as exc:
                log.exception("conversion failed for %s", job.item_id)
                job.state = JobState.failed
                job.error = str(exc)
            if job.state != JobState.done:
                job.finished_at = time.time()
                # Stopped by shutdown: the item stays queued/converting and the next start resumes it.
                if not self._stop.is_set():
                    try:
                        self._settle_item(job, job.error or "failed")
                    except Exception:  # the worker must survive (e.g. a full disk)
                        log.exception("cannot record the end of job %s", job.id)
            with self._lock:
                self._running.pop(job.id, None)
            self._emit(job)

    def _run_job(self, job: ConvertJob, cancel: threading.Event) -> None:
        def converting(item: LibraryItem) -> None:
            if not self._ready_on_disk(item, job.format_id):
                info = item.conversion_for(job.format_id)
                info.status = ConversionStatus.converting
                info.error = None
                item.conversions[job.format_id] = info

        item = self.store.update(job.item_id, converting)
        if item is None:
            raise _Cancelled("item deleted")
        fmt = format_by_id(job.format_id)
        if item.kind == LibraryKind.stinger:
            info = self._convert_stinger(item, fmt, job, cancel)
        else:
            info = self._convert_clip(item, fmt, job, cancel)
        if info.status != ConversionStatus.ready:
            raise RuntimeError(info.error or "conversion failed")
        new_fps = fmt.frame_rate_num / fmt.frame_rate_den

        def finished(current: LibraryItem) -> None:
            # Re-read: an operator may have changed the cut while this job ran.
            current.original.update(item.original)
            if current.kind == LibraryKind.stinger:
                old_fps = float(current.original.get("fps") or new_fps)
                info.cut_frame, info.cut_ms = cut_frame_after_fps_change(
                    current.options.cut_ms if current.options.cut_ms is not None else current.cut_ms,
                    current.options.cut_frame if current.options.cut_frame is not None else current.cut_frame,
                    old_fps,
                    new_fps,
                    info.frames,
                )
                current.cut_frame = info.cut_frame
                current.cut_ms = info.cut_ms
                current.has_alpha = info.has_alpha
            current.conversions[job.format_id] = info

        if self.store.update(job.item_id, finished) is None:
            raise _Cancelled("item deleted")
        job.progress = 1.0
        job.state = JobState.done
        job.error = None
        job.finished_at = time.time()

    def _append_log(self, item_id: str, text: str) -> None:
        try:
            with self.store.convert_log_path(item_id).open("a", encoding="utf-8") as handle:
                handle.write(text)
                if not text.endswith("\n"):
                    handle.write("\n")
        except OSError:
            pass  # item deleted meanwhile

    def _run(self, item: LibraryItem, cmd: list[str], timeout: float, cancel: threading.Event) -> RunResult:
        result = self.ffmpeg.run(cmd, timeout=timeout, cancel=cancel)
        self._append_log(item.id, result.output)
        if result.cancelled or cancel.is_set() or self._stop.is_set():
            raise _Cancelled()
        return result

    def _original_path(self, item: LibraryItem) -> Path:
        """item.json names the original: relative to the item directory, or absolute for
        legacy storage that is referenced in place."""
        rel = item.original.get("path")
        if not rel:
            raise FileNotFoundError(f"original missing for {item.id}")
        candidate = Path(rel)
        if not candidate.is_absolute():
            candidate = self.store.item_dir(item.id) / rel
        if not candidate.exists():
            raise FileNotFoundError(f"original missing for {item.id}: {candidate}")
        return candidate

    def _publish(self, temporary: Path, final: Path) -> None:
        """Replace in one step: a pipeline that has the old file open keeps reading it."""
        if temporary.is_file():
            os.replace(temporary, final)

    def _convert_clip(
        self, item: LibraryItem, fmt: VideoFormat, job: ConvertJob, cancel: threading.Event
    ) -> ConversionInfo:
        if not self.ffmpeg.which():
            return ConversionInfo(status=ConversionStatus.failed, error="ffmpeg not available")
        original = self._original_path(item)
        directory = self.store.item_dir(item.id)
        video_tmp = directory / f"video-tmp-{job.id}.mov"
        audio_tmp = directory / f"audio-tmp-{job.id}.f32le"
        mezz_tmp = directory / f"mezz-{fmt.id}.{job.id}.tmp.mov"
        thumb_tmp = directory / f"thumb.{job.id}.tmp.jpg"
        mezz = self.store.mezz_path(item.id, fmt.id)
        thumb = self.store.thumb_path(item.id)

        probe = self.ffmpeg.probe(original)
        vstream = next((s for s in probe.streams if s.codec_type == "video"), None)
        astream = next((s for s in probe.streams if s.codec_type == "audio"), None)
        src_i = bool(vstream and "interlace" in (vstream.field_order or "").lower())
        color_space = (vstream.color_space if vstream else "") or item.original.get("color_space", "")
        src_audio_ch = int(astream.channels) if astream else 0
        # Mono or stereo: GStreamer's MOV demuxer cannot play more PCM channels, and the
        # mixer plays file inputs in stereo anyway (a 5.1 source is downmixed).
        use_ch = 1 if item.options.map_channels == 1 else 2
        timeout = conversion_timeout(probe.duration, motion=item.options.fps_mode.value == "motion")

        vf = build_video_filter(
            width=fmt.width,
            height=fmt.height,
            fps_num=fmt.frame_rate_num,
            fps_den=fmt.frame_rate_den,
            fit=item.options.fit.value,
            fps_mode=item.options.fps_mode.value,
            src_interlaced=src_i,
            color_space=str(color_space or ""),
            dst_pix_fmt="yuv422p10le",
        )
        af = "aresample=48000"
        if item.options.loudness:
            af = "loudnorm=I=-23:TP=-1.5:LRA=11," + af

        try:
            job.progress = 0.2
            self._emit(job)
            result = self._run(
                item, build_clip_video_command(original=original, video_tmp=video_tmp, vf=vf), timeout, cancel
            )
            if result.code != 0:
                return ConversionInfo(status=ConversionStatus.failed, error="video transcode failed")

            probed = self.ffmpeg.probe(video_tmp)
            frames = 0
            for stream in probed.streams:
                if stream.codec_type == "video":
                    if stream.nb_frames.isdigit():
                        frames = int(stream.nb_frames)
                    break
            if frames <= 0 and probed.duration > 0:
                frames = int(round(probed.duration * (fmt.frame_rate_num / fmt.frame_rate_den)))
            if frames <= 0:
                return ConversionInfo(status=ConversionStatus.failed, error="mezzanine has no frames")

            # Sound of exactly the clip's length in whole frames, so it loops without drift.
            job.progress = 0.45
            self._emit(job)
            need = samples_until_grain(frames, fmt.frame_rate_num, fmt.frame_rate_den, 48000)
            audio = None
            if src_audio_ch > 0:
                audio = self._run(
                    item,
                    build_clip_audio_command(
                        original=original,
                        audio_tmp=audio_tmp,
                        af=af,
                        src_channels=src_audio_ch,
                        dst_channels=use_ch,
                        samples=need,
                        src_layout=astream.channel_layout if astream else "",
                    ),
                    timeout,
                    cancel,
                )
            if audio is None or audio.code != 0:
                if audio is not None:
                    log.warning("sound of %s cannot be read, the clip gets silence", item.id)
                audio = self._run(
                    item,
                    build_clip_audio_command(
                        original=None, audio_tmp=audio_tmp, af=af, src_channels=0, dst_channels=use_ch, samples=need
                    ),
                    timeout,
                    cancel,
                )
                if audio.code != 0:
                    return ConversionInfo(status=ConversionStatus.failed, error="audio conform failed")
            crossfade_loop_file(audio_tmp, use_ch, need, int(item.options.crossfade_ms * 48))

            job.progress = 0.75
            self._emit(job)
            mx = self._run(
                item,
                build_clip_mux_command(video_tmp=video_tmp, audio_tmp=audio_tmp, audio_channels=use_ch, mezz=mezz_tmp),
                timeout,
                cancel,
            )
            if mx.code != 0:
                return ConversionInfo(status=ConversionStatus.failed, error="mux failed")
            self._run(item, build_thumb_command(mezz=mezz_tmp, thumb=thumb_tmp), 120.0, cancel)
            self._publish(mezz_tmp, mezz)
            self._publish(thumb_tmp, thumb)
        finally:
            for tmp in (video_tmp, audio_tmp, mezz_tmp, thumb_tmp):
                tmp.unlink(missing_ok=True)

        fps = fmt.frame_rate_num / fmt.frame_rate_den
        decision = decide_ram_playback(
            frames=frames,
            fps=fps,
            width=fmt.width,
            height=fmt.height,
            has_alpha=False,
            ram_clip_max_s=self.ram_clip_max_s,
            ram_budget_bytes=self.ram_budget_bytes,
        )
        return ConversionInfo(
            status=ConversionStatus.ready,
            frames=frames,
            audio_samples=need,
            audio_channels=use_ch,
            mezz=mezz.name,
            has_alpha=False,
            duration_s=frames / fps,
            playback="ram" if decision.use_ram else "decode_ahead",
        )

    def _unpack_zip(self, item: LibraryItem, job: ConvertJob) -> None:
        """Unpack an uploaded TGA ZIP into sequence/ (done here, not in the upload request)."""
        directory = self.store.item_dir(item.id)
        archive = self._original_path(item)
        stage = directory / f"zip-stage-{job.id}"
        try:
            root = safe_extract_tga_zip(archive, stage)
            info = validate_sequence(root)
            if not info.ok:
                errors = "; ".join(i.message for i in info.issues if i.level == "error")
                raise ValueError(errors or "invalid sequence in zip")
            sequence = directory / "sequence"
            if sequence.exists():
                shutil.rmtree(sequence)
            os.replace(root, sequence)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        updates = {
            "path": "sequence",
            "source_kind": "sequence",
            "frame_count": info.frame_count,
            "width": info.width,
            "height": info.height,
            "has_alpha": info.has_alpha,
            "pattern": info.pattern,
            "start_number": info.start_number,
        }
        item.original.update(updates)
        if self.store.update(item.id, lambda current: current.original.update(updates)) is None:
            raise _Cancelled("item deleted")
        archive.unlink(missing_ok=True)

    def _convert_stinger(
        self, item: LibraryItem, fmt: VideoFormat, job: ConvertJob, cancel: threading.Event
    ) -> ConversionInfo:
        if not self.ffmpeg.which():
            return ConversionInfo(status=ConversionStatus.failed, error="ffmpeg not available")
        if item.original.get("source_kind") == "zip":
            self._unpack_zip(item, job)
        original = self._original_path(item)
        directory = self.store.item_dir(item.id)
        mezz = self.store.mezz_path(item.id, fmt.id)
        mezz_tmp = directory / f"mezz-{fmt.id}.{job.id}.tmp.mov"
        thumb = self.store.thumb_path(item.id)
        thumb_tmp = directory / f"thumb.{job.id}.tmp.jpg"
        src_kind = item.original.get("source_kind") or ("sequence" if original.is_dir() else "video")
        old_fps = float(item.original.get("fps") or item.options.sequence_fps or (fmt.frame_rate_num / fmt.frame_rate_den))
        seq_fps = float(item.options.sequence_fps or (fmt.frame_rate_num / fmt.frame_rate_den))
        has_alpha = bool(item.original.get("has_alpha", True))
        input_args: list[str]

        if src_kind == "sequence" or original.is_dir():
            info = validate_sequence(original)
            if not info.ok:
                errors = "; ".join(i.message for i in info.issues if i.level == "error")
                return ConversionInfo(status=ConversionStatus.failed, error=errors or "invalid sequence")
            has_alpha = info.has_alpha
            input_args = [
                "-framerate",
                str(seq_fps),
                "-start_number",
                str(info.start_number),
                "-i",
                str(info.files[0].parent / info.pattern),
            ]
            duration = info.frame_count / seq_fps
            item.original.update(
                {
                    "source_kind": "sequence",
                    "frame_count": info.frame_count,
                    "width": info.width,
                    "height": info.height,
                    "has_alpha": has_alpha,
                    "pattern": info.pattern,
                    "start_number": info.start_number,
                    "fps": seq_fps,
                }
            )
        else:
            probe = self.ffmpeg.probe(original)
            vstream = next((s for s in probe.streams if s.codec_type == "video"), None)
            astream = next((s for s in probe.streams if s.codec_type == "audio"), None)
            if vstream:
                has_alpha = has_alpha_pix_fmt(vstream.pix_fmt) or has_alpha
                old_fps = parse_frame_rate(vstream.r_frame_rate or vstream.avg_frame_rate, old_fps)
            input_args = ["-i", str(original)]
            duration = probe.duration
            item.original.update(
                {
                    "source_kind": "video",
                    "has_alpha": has_alpha,
                    "fps": old_fps,
                    "audio_channels": int(astream.channels) if astream else 0,
                }
            )

        vf = build_video_filter(
            width=fmt.width,
            height=fmt.height,
            fps_num=fmt.frame_rate_num,
            fps_den=fmt.frame_rate_den,
            fit=item.options.fit.value,
            fps_mode=item.options.fps_mode.value,
            src_interlaced=False,
            color_space=str(item.original.get("color_space") or ""),
            dst_pix_fmt="yuva444p10le",
        )
        timeout = conversion_timeout(duration, motion=item.options.fps_mode.value == "motion")
        try:
            job.progress = 0.3
            self._emit(job)
            result = self._run(
                item,
                build_stinger_mezz_command(input_args=input_args, mezz=mezz_tmp, vf=vf),
                timeout,
                cancel,
            )
            if result.code != 0:
                return ConversionInfo(status=ConversionStatus.failed, error="stinger transcode failed")
            self._run(item, build_thumb_command(mezz=mezz_tmp, thumb=thumb_tmp), 120.0, cancel)

            probed = self.ffmpeg.probe(mezz_tmp)
            frames = 0
            for stream in probed.streams:
                if stream.codec_type == "video" and stream.nb_frames.isdigit():
                    frames = int(stream.nb_frames)
                    break
            if frames <= 0 and probed.duration > 0:
                frames = int(round(probed.duration * (fmt.frame_rate_num / fmt.frame_rate_den)))
            if frames <= 0:
                frames = int(item.original.get("frame_count") or 0)
            if frames <= 0:
                return ConversionInfo(status=ConversionStatus.failed, error="stinger mezzanine has no frames")
            self._publish(mezz_tmp, mezz)
            self._publish(thumb_tmp, thumb)
        finally:
            for tmp in (mezz_tmp, thumb_tmp):
                tmp.unlink(missing_ok=True)

        new_fps = fmt.frame_rate_num / fmt.frame_rate_den
        decision = decide_ram_playback(
            frames=frames,
            fps=new_fps,
            width=fmt.width,
            height=fmt.height,
            has_alpha=True,
            ram_clip_max_s=self.ram_clip_max_s,
            ram_budget_bytes=self.ram_budget_bytes,
        )
        # The cut frame is set in _run_job from the item as it is when the job ends.
        return ConversionInfo(
            status=ConversionStatus.ready,
            frames=frames,
            audio_samples=0,
            audio_channels=0,
            mezz=mezz.name,
            has_alpha=has_alpha,
            duration_s=frames / new_fps,
            playback="ram" if decision.use_ram else "decode_ahead",
        )
