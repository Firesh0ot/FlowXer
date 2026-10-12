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
    build_clip_video_command,
    build_still_command,
    build_still_filter,
    build_stinger_alpha_command,
    build_thumb_command,
    build_video_filter,
    conversion_threads,
    conversion_timeout,
    has_alpha_pix_fmt,
    is_rgb_pix_fmt,
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
        on_update: JobListener | None = None,
    ) -> None:
        self.store = store
        self.concurrency = max(1, min(8, concurrency))
        self.threads = conversion_threads(self.concurrency)
        self.ffmpeg = ffmpeg or FFmpeg()
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
        # For /metrics: finished conversions by result, and how long the last one took.
        self.finished: dict[str, int] = {}
        self.last_seconds = 0.0

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
                if not self.ready_on_disk(item, format_id):
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

    def ready_on_disk(self, item: LibraryItem, format_id: str) -> bool:
        """Ready, and its files are there (a clip: its mezzanine and its sound; a stinger: its
        colour, its alpha and its sound when it has them)."""
        if not item.is_ready(format_id) or not self.store.mezz_path(item.id, format_id, item.kind).is_file():
            return False
        conv = item.conversion_for(format_id)
        if item.kind == LibraryKind.stinger:
            return (not conv.has_alpha or self.store.alpha_path(item.id, format_id).is_file()) and (
                not conv.audio or self.store.audio_path(item.id, format_id).is_file()
            )
        return item.kind != LibraryKind.clip or self.store.audio_path(item.id, format_id).is_file()

    def _settle_item(self, job: ConvertJob, error: str) -> None:
        """After a failed or cancelled job: keep a ready mezzanine, else mark the item failed."""

        def settle(item: LibraryItem) -> None:
            if self.ready_on_disk(item, job.format_id):
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
                if not self._stop.is_set():
                    self.finished[job.state.value] = self.finished.get(job.state.value, 0) + 1
                    if job.started_at and job.finished_at:
                        self.last_seconds = job.finished_at - job.started_at
            self._emit(job)

    def _run_job(self, job: ConvertJob, cancel: threading.Event) -> None:
        def converting(item: LibraryItem) -> None:
            if not self.ready_on_disk(item, job.format_id):
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
        elif item.kind == LibraryKind.still:
            info = self._convert_still(item, fmt, job, cancel)
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
        # x265 cannot set its NUMA policy in a container; it says so once per thread.
        text = "\n".join(line for line in text.splitlines() if "set_mempolicy" not in line)
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
        video_tmp = directory / f"mezz-{fmt.id}.{job.id}.tmp.mkv"
        audio_tmp = directory / f"mezz-{fmt.id}.{job.id}.tmp.f32"
        thumb_tmp = directory / f"thumb.{job.id}.tmp.jpg"
        mezz = self.store.mezz_path(item.id, fmt.id, LibraryKind.clip)
        audio_file = self.store.audio_path(item.id, fmt.id)
        thumb = self.store.thumb_path(item.id)

        probe = self.ffmpeg.probe(original)
        vstream = next((s for s in probe.streams if s.codec_type == "video"), None)
        astream = next((s for s in probe.streams if s.codec_type == "audio"), None)
        src_i = bool(vstream and "interlace" in (vstream.field_order or "").lower())
        color_space = (vstream.color_space if vstream else "") or item.original.get("color_space", "")
        src_audio_ch = int(astream.channels) if astream else 0
        # Mono or stereo: the PGM sound is stereo (a 5.1 source is downmixed, mono plays on both).
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
            keyint = max(1, round(fmt.frame_rate_num / fmt.frame_rate_den))
            result = self._run(
                item,
                build_clip_video_command(
                    original=original,
                    video_tmp=video_tmp,
                    vf=vf,
                    keyint=keyint,
                    threads=self.threads,
                    nvenc=self.ffmpeg.has_nvenc(),
                ),
                timeout,
                cancel,
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

            job.progress = 0.85
            self._emit(job)
            self._run(item, build_thumb_command(mezz=video_tmp, thumb=thumb_tmp), 120.0, cancel)
            self._publish(audio_tmp, audio_file)
            self._publish(video_tmp, mezz)
            self._publish(thumb_tmp, thumb)
            self._remove_prores_mezzanine(item, fmt.id)
        finally:
            for tmp in (video_tmp, audio_tmp, thumb_tmp):
                tmp.unlink(missing_ok=True)

        fps = fmt.frame_rate_num / fmt.frame_rate_den
        return ConversionInfo(
            status=ConversionStatus.ready,
            frames=frames,
            audio_samples=need,
            audio_channels=use_ch,
            mezz=mezz.name,
            audio=audio_file.name,
            has_alpha=False,
            duration_s=frames / fps,
            # The engine decodes ahead on the GPU.
            playback="decode_ahead",
        )

    def _remove_prores_mezzanine(self, item: LibraryItem, format_id: str) -> None:
        """A clip converted before the engine, or a stinger converted before P3, had a ProRes
        mezzanine (mezz-<format>.mov). It is derived, never the original, and the new mezzanine
        replaces it."""
        old = self.store.item_dir(item.id) / f"mezz-{format_id}.mov"
        if old.is_file() and item.original.get("path") != old.name:
            old.unlink(missing_ok=True)
            log.info("removed the ProRes mezzanine of %s (%s)", item.id, format_id)

    def _convert_still(
        self, item: LibraryItem, fmt: VideoFormat, job: ConvertJob, cancel: threading.Event
    ) -> ConversionInfo:
        """A still: its first picture at the raster, as one raw v210 picture."""
        if not self.ffmpeg.which():
            return ConversionInfo(status=ConversionStatus.failed, error="ffmpeg not available")
        original = self._original_path(item)
        directory = self.store.item_dir(item.id)
        still = self.store.mezz_path(item.id, fmt.id, LibraryKind.still)
        still_tmp = directory / f"{still.stem}.{job.id}.tmp.v210"
        thumb = self.store.thumb_path(item.id)
        thumb_tmp = directory / f"thumb.{job.id}.tmp.jpg"
        vf = build_still_filter(width=fmt.width, height=fmt.height, fit=item.options.fit.value)
        picture_bytes = (fmt.width + 47) // 48 * 128 * fmt.height
        try:
            job.progress = 0.3
            self._emit(job)
            result = self._run(item, build_still_command(original=original, still_tmp=still_tmp, vf=vf), 300.0, cancel)
            size = still_tmp.stat().st_size if still_tmp.is_file() else 0
            if result.code != 0 or size != picture_bytes:
                return ConversionInfo(status=ConversionStatus.failed, error="still conversion failed")
            self._run(item, build_thumb_command(mezz=original, thumb=thumb_tmp), 120.0, cancel)
            self._publish(still_tmp, still)
            self._publish(thumb_tmp, thumb)
        finally:
            for tmp in (still_tmp, thumb_tmp):
                tmp.unlink(missing_ok=True)
        return ConversionInfo(status=ConversionStatus.ready, frames=1, mezz=still.name, playback="ram")

    def _unpack_zip(self, item: LibraryItem, job: ConvertJob) -> None:
        """Unpack an uploaded sequence ZIP (frames and a WAV) into sequence/ (done here, not in the
        upload request)."""
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
        """A stinger as the engine plays it (docs/engine-p3.md §6): its colour as the clip
        mezzanine (HEVC 4:4:4 10-bit), its alpha as the luma of a lossless HEVC 4:2:0 10-bit stream
        (none for an opaque stinger) and its sound as a clip's (none when it is silent). The
        original stays."""
        if not self.ffmpeg.which():
            return ConversionInfo(status=ConversionStatus.failed, error="ffmpeg not available")
        if item.original.get("source_kind") == "zip":
            self._unpack_zip(item, job)
        original = self._original_path(item)
        directory = self.store.item_dir(item.id)
        mezz = self.store.mezz_path(item.id, fmt.id, LibraryKind.stinger)
        alpha_file = self.store.alpha_path(item.id, fmt.id)
        audio_file = self.store.audio_path(item.id, fmt.id)
        colour_tmp = directory / f"mezz-{fmt.id}.{job.id}.tmp.mkv"
        alpha_tmp = directory / f"mezz-{fmt.id}-alpha.{job.id}.tmp.mkv"
        audio_tmp = directory / f"mezz-{fmt.id}.{job.id}.tmp.f32"
        thumb = self.store.thumb_path(item.id)
        thumb_tmp = directory / f"thumb.{job.id}.tmp.jpg"
        src_kind = item.original.get("source_kind") or ("sequence" if original.is_dir() else "video")
        old_fps = float(item.original.get("fps") or item.options.sequence_fps or (fmt.frame_rate_num / fmt.frame_rate_den))
        seq_fps = float(item.options.sequence_fps or (fmt.frame_rate_num / fmt.frame_rate_den))
        # The sound: a sequence's WAV or the video's audio (its first stream).
        sound: Path | None = None
        sound_channels = 0
        sound_layout = ""
        input_args: list[str]

        if src_kind == "sequence" or original.is_dir():
            info = validate_sequence(original)
            if not info.ok:
                errors = "; ".join(i.message for i in info.issues if i.level == "error")
                return ConversionInfo(status=ConversionStatus.failed, error=errors or "invalid sequence")
            has_alpha = info.has_alpha
            rgb = True
            input_args = [
                "-framerate",
                str(seq_fps),
                "-start_number",
                str(info.start_number),
                "-i",
                str(info.files[0].parent / info.pattern),
            ]
            duration = info.frame_count / seq_fps
            if info.sound is not None:
                astream = next((s for s in self.ffmpeg.probe(info.sound).streams if s.codec_type == "audio"), None)
                if astream is not None:
                    sound, sound_channels, sound_layout = info.sound, int(astream.channels), astream.channel_layout
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
                    "audio_channels": sound_channels,
                }
            )
        else:
            probe = self.ffmpeg.probe(original)
            vstream = next((s for s in probe.streams if s.codec_type == "video"), None)
            astream = next((s for s in probe.streams if s.codec_type == "audio"), None)
            pix_fmt = vstream.pix_fmt if vstream else ""
            # VP9 keeps its alpha in a side stream that only libvpx decodes.
            vp9_alpha = bool(vstream and vstream.codec_name == "vp9" and str(vstream.tags.get("alpha_mode") or "") == "1")
            has_alpha = has_alpha_pix_fmt(pix_fmt) or vp9_alpha
            rgb = is_rgb_pix_fmt(pix_fmt)
            if vstream:
                old_fps = parse_frame_rate(vstream.r_frame_rate or vstream.avg_frame_rate, old_fps)
            input_args = [*(["-c:v", "libvpx-vp9"] if vp9_alpha else []), "-i", str(original)]
            duration = probe.duration
            if astream is not None:
                sound, sound_channels, sound_layout = original, int(astream.channels), astream.channel_layout
            item.original.update(
                {
                    "source_kind": "video",
                    "has_alpha": has_alpha,
                    "fps": old_fps,
                    "audio_channels": sound_channels,
                }
            )

        def video_filter(dst_pix_fmt: str) -> str:
            return build_video_filter(
                width=fmt.width,
                height=fmt.height,
                fps_num=fmt.frame_rate_num,
                fps_den=fmt.frame_rate_den,
                fit=item.options.fit.value,
                fps_mode=item.options.fps_mode.value,
                src_interlaced=False,
                color_space=str(item.original.get("color_space") or ""),
                dst_pix_fmt=dst_pix_fmt,
                rgb=rgb,
                # Bars around a stinger that does not fill the raster are transparent.
                pad="black@0",
            )

        keyint = max(1, round(fmt.frame_rate_num / fmt.frame_rate_den))
        nvenc = self.ffmpeg.has_nvenc()
        use_ch = 1 if item.options.map_channels == 1 else 2
        timeout = conversion_timeout(duration, motion=item.options.fps_mode.value == "motion")
        try:
            job.progress = 0.2
            self._emit(job)
            result = self._run(
                item,
                build_clip_video_command(
                    original=None,
                    input_args=input_args,
                    video_tmp=colour_tmp,
                    vf=video_filter("yuv422p10le"),
                    keyint=keyint,
                    threads=self.threads,
                    nvenc=nvenc,
                ),
                timeout,
                cancel,
            )
            if result.code != 0:
                return ConversionInfo(status=ConversionStatus.failed, error="stinger transcode failed")
            probed = self.ffmpeg.probe(colour_tmp)
            frames = 0
            for stream in probed.streams:
                if stream.codec_type == "video" and stream.nb_frames.isdigit():
                    frames = int(stream.nb_frames)
                    break
            if frames <= 0 and probed.duration > 0:
                frames = int(round(probed.duration * (fmt.frame_rate_num / fmt.frame_rate_den)))
            if frames <= 0:
                return ConversionInfo(status=ConversionStatus.failed, error="stinger mezzanine has no frames")
            if has_alpha:
                job.progress = 0.5
                self._emit(job)
                result = self._run(
                    item,
                    build_stinger_alpha_command(
                        input_args=input_args,
                        alpha_tmp=alpha_tmp,
                        vf=video_filter("yuva444p10le"),
                        width=fmt.width,
                        height=fmt.height,
                        fps=f"{fmt.frame_rate_num}/{fmt.frame_rate_den}",
                        keyint=keyint,
                        threads=self.threads,
                        nvenc=nvenc,
                    ),
                    timeout,
                    cancel,
                )
                if result.code != 0:
                    return ConversionInfo(status=ConversionStatus.failed, error="stinger alpha transcode failed")
            need = samples_until_grain(frames, fmt.frame_rate_num, fmt.frame_rate_den, 48000)
            if sound is not None:
                job.progress = 0.75
                self._emit(job)
                audio = self._run(
                    item,
                    build_clip_audio_command(
                        original=sound,
                        audio_tmp=audio_tmp,
                        af="aresample=48000",
                        src_channels=sound_channels,
                        dst_channels=use_ch,
                        samples=need,
                        src_layout=sound_layout,
                    ),
                    timeout,
                    cancel,
                )
                if audio.code != 0:
                    log.warning("sound of stinger %s cannot be read, the stinger is silent", item.id)
                    sound = None
            self._run(item, build_thumb_command(mezz=colour_tmp, thumb=thumb_tmp), 120.0, cancel)
            self._publish(colour_tmp, mezz)
            for tmp, final, used in ((alpha_tmp, alpha_file, has_alpha), (audio_tmp, audio_file, sound is not None)):
                if used:
                    self._publish(tmp, final)
                else:
                    final.unlink(missing_ok=True)
            self._publish(thumb_tmp, thumb)
            self._remove_prores_mezzanine(item, fmt.id)
        finally:
            for tmp in (colour_tmp, alpha_tmp, audio_tmp, thumb_tmp):
                tmp.unlink(missing_ok=True)

        new_fps = fmt.frame_rate_num / fmt.frame_rate_den
        # The cut frame is set in _run_job from the item as it is when the job ends.
        return ConversionInfo(
            status=ConversionStatus.ready,
            frames=frames,
            audio_samples=need if sound is not None else 0,
            audio_channels=use_ch if sound is not None else 0,
            mezz=mezz.name,
            audio=audio_file.name if sound is not None else None,
            has_alpha=has_alpha,
            duration_s=frames / new_fps,
            # The engine decodes ahead on the GPU (a stinger player).
            playback="decode_ahead",
        )
