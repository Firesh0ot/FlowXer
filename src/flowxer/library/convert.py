"""Background conversion job queue."""

from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable

from flowxer.engine.formats import VideoFormat, format_by_id
from flowxer.library.conform import (
    conform_interleaved,
    cut_frame_after_fps_change,
    read_f32le,
    samples_until_grain,
    write_f32le,
)
from flowxer.library.ffmpeg import (
    FFmpeg,
    build_clip_mezz_commands,
    build_stinger_mezz_commands,
    build_video_filter,
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
from flowxer.library.sequence import validate_sequence
from flowxer.library.store import LibraryStore

log = logging.getLogger(__name__)

JobListener = Callable[[ConvertJob], None]


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
        self._cancel: set[str] = set()
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

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._workers:
            thread.join(timeout=2)
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
            if job.state in {JobState.done, JobState.failed, JobState.cancelled}:
                return job
            self._cancel.add(job_id)
            if job.state == JobState.queued:
                job.state = JobState.cancelled
                job.finished_at = time.time()
                try:
                    self._queue.remove(job_id)
                except ValueError:
                    pass
            self._emit(job)
            return job

    def enqueue(self, item_id: str, format_id: str, options: ConvertOptions | None = None) -> ConvertJob:
        format_by_id(format_id)  # validate
        item = self.store.load(item_id)
        if item is None:
            raise KeyError(item_id)
        if options is not None:
            item.options = options
            self.store.save(item)
        with self._lock:
            for existing in self._jobs.values():
                if (
                    existing.item_id == item_id
                    and existing.format_id == format_id
                    and existing.state in {JobState.queued, JobState.running}
                ):
                    return existing
            job = ConvertJob(
                id=uuid.uuid4().hex[:12],
                item_id=item_id,
                format_id=format_id,
                created_at=time.time(),
            )
            self._jobs[job.id] = job
            self._queue.append(job.id)
            item.conversions[format_id] = ConversionInfo(status=ConversionStatus.queued)
            self.store.save(item)
            self._emit(job)
            self._wake.set()
            return job

    def _emit(self, job: ConvertJob) -> None:
        if self.on_update:
            try:
                self.on_update(job)
            except Exception:  # pragma: no cover - listener errors must not kill workers
                log.exception("job listener failed")

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            job_id = None
            with self._lock:
                if self._queue:
                    job_id = self._queue.popleft()
            if job_id is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.state == JobState.cancelled or job_id in self._cancel:
                    continue
                job.state = JobState.running
                job.started_at = time.time()
                job.progress = 0.05
                self._emit(job)
            try:
                self._run_job(job)
            except Exception as exc:
                log.exception("conversion failed for %s", job.item_id)
                job.state = JobState.failed
                job.error = str(exc)
                job.finished_at = time.time()
                item = self.store.load(job.item_id)
                if item:
                    item.conversions[job.format_id] = ConversionInfo(
                        status=ConversionStatus.failed,
                        error=str(exc),
                    )
                    self.store.save(item)
                self._emit(job)

    def _cancelled(self, job: ConvertJob) -> bool:
        return job.id in self._cancel

    def _run_job(self, job: ConvertJob) -> None:
        item = self.store.load(job.item_id)
        if item is None:
            raise RuntimeError("item disappeared")
        if self._cancelled(job):
            job.state = JobState.cancelled
            job.finished_at = time.time()
            self._emit(job)
            return
        self.store.mark_converting(item, job.format_id)
        fmt = format_by_id(job.format_id)
        if item.kind == LibraryKind.stinger:
            info = self._convert_stinger(item, fmt, job)
        else:
            info = self._convert_clip(item, fmt, job)
        if self._cancelled(job):
            job.state = JobState.cancelled
            job.finished_at = time.time()
            self._emit(job)
            return
        item = self.store.load(job.item_id) or item
        item.conversions[job.format_id] = info
        if item.kind == LibraryKind.stinger and info.cut_frame is not None:
            item.cut_frame = info.cut_frame
            item.cut_ms = info.cut_ms
            item.has_alpha = info.has_alpha
        self.store.save(item)
        job.progress = 1.0
        job.state = JobState.done if info.status == ConversionStatus.ready else JobState.failed
        job.error = info.error
        job.finished_at = time.time()
        self._emit(job)

    def _append_log(self, item_id: str, text: str) -> None:
        path = self.store.convert_log_path(item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")

    def _original_path(self, item: LibraryItem) -> Path:
        directory = self.store.item_dir(item.id)
        rel = item.original.get("path")
        if rel:
            candidate = directory / rel
            if candidate.exists():
                return candidate
        for path in sorted(directory.iterdir()):
            if path.name.startswith(("item.", "mezz-", "thumb.", "convert.", "audio", "video-tmp")):
                continue
            if path.is_file() or path.is_dir():
                return path
        raise FileNotFoundError(f"original missing for {item.id}")

    def _convert_clip(self, item: LibraryItem, fmt: VideoFormat, job: ConvertJob) -> ConversionInfo:
        if not self.ffmpeg.which():
            return ConversionInfo(status=ConversionStatus.failed, error="ffmpeg not available")
        original = self._original_path(item)
        directory = self.store.item_dir(item.id)
        video_tmp = directory / "video-tmp.mov"
        audio_raw = directory / "audio.f32le"
        audio_conf = directory / "audio-conformed.f32le"
        mezz = self.store.mezz_path(item.id, fmt.id)
        thumb = self.store.thumb_path(item.id)

        probe = self.ffmpeg.probe(original)
        vstream = next((s for s in probe.streams if s.codec_type == "video"), None)
        astream = next((s for s in probe.streams if s.codec_type == "audio"), None)
        src_i = bool(vstream and "interlace" in (vstream.field_order or "").lower())
        color_space = (vstream.color_space if vstream else "") or item.original.get("color_space", "")
        src_audio_ch = int(astream.channels) if astream else int(item.original.get("audio_channels") or 0)
        map_ch = item.options.map_channels
        use_ch = map_ch if map_ch > 0 else max(2, src_audio_ch or 2)
        use_ch = max(2, min(64, use_ch))

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

        cmds = build_clip_mezz_commands(
            original=original,
            video_tmp=video_tmp,
            audio_raw=audio_raw,
            audio_conf=audio_conf,
            mezz=mezz,
            thumb=thumb,
            vf=vf,
            af=af,
            audio_channels=use_ch,
            src_audio_channels=src_audio_ch,
        )
        # video
        job.progress = 0.2
        self._emit(job)
        result = self.ffmpeg.run(cmds[0])
        self._append_log(item.id, result.output)
        if result.code != 0:
            return ConversionInfo(status=ConversionStatus.failed, error="video transcode failed")

        # audio extract (best-effort)
        job.progress = 0.45
        self._emit(job)
        src_samples = 0
        src = []
        if src_audio_ch > 0:
            ar = self.ffmpeg.run(cmds[1])
            self._append_log(item.id, ar.output)
            if ar.code == 0 and audio_raw.is_file():
                src = read_f32le(audio_raw)
                src_samples = len(src) // max(src_audio_ch, 1)

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

        need = samples_until_grain(frames, fmt.frame_rate_num, fmt.frame_rate_den, 48000)
        xfade = int(item.options.crossfade_ms * 48)
        conformed = conform_interleaved(
            src,
            src_audio_ch if src_audio_ch > 0 else use_ch,
            src_samples,
            use_ch,
            need,
            xfade,
        )
        write_f32le(audio_conf, conformed)

        job.progress = 0.75
        self._emit(job)
        mx = self.ffmpeg.run(cmds[2])
        self._append_log(item.id, mx.output)
        if mx.code != 0:
            return ConversionInfo(status=ConversionStatus.failed, error="mux failed")
        th = self.ffmpeg.run(cmds[3])
        self._append_log(item.id, th.output)

        for tmp in (video_tmp, audio_raw, audio_conf):
            if tmp.exists():
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

    def _convert_stinger(self, item: LibraryItem, fmt: VideoFormat, job: ConvertJob) -> ConversionInfo:
        if not self.ffmpeg.which():
            return ConversionInfo(status=ConversionStatus.failed, error="ffmpeg not available")
        original = self._original_path(item)
        directory = self.store.item_dir(item.id)
        mezz = self.store.mezz_path(item.id, fmt.id)
        thumb = self.store.thumb_path(item.id)
        src_kind = item.original.get("source_kind") or ("sequence" if original.is_dir() else "video")
        old_fps = float(item.original.get("fps") or item.options.sequence_fps or (fmt.frame_rate_num / fmt.frame_rate_den))
        seq_fps = float(item.options.sequence_fps or (fmt.frame_rate_num / fmt.frame_rate_den))
        has_alpha = bool(item.original.get("has_alpha", True))
        src_audio = False
        input_args: list[str]

        if src_kind == "sequence" or original.is_dir():
            info = validate_sequence(original if original.is_dir() else directory / "sequence")
            if not info.ok:
                errors = "; ".join(i.message for i in info.issues if i.level == "error")
                return ConversionInfo(status=ConversionStatus.failed, error=errors or "invalid sequence")
            has_alpha = info.has_alpha
            pattern = info.pattern
            # ffmpeg image2 pattern needs path
            pattern_path = (info.files[0].parent / pattern)
            # Convert printf pattern to something ffmpeg understands — files are already numbered.
            # Use first file's directory with the inferred pattern.
            input_args = [
                "-framerate",
                str(seq_fps),
                "-i",
                str(pattern_path),
            ]
            old_fps = float(item.original.get("fps") or seq_fps)
            item.original.update(
                {
                    "source_kind": "sequence",
                    "frame_count": info.frame_count,
                    "width": info.width,
                    "height": info.height,
                    "has_alpha": has_alpha,
                    "pattern": pattern,
                    "fps": seq_fps,
                }
            )
        else:
            probe = self.ffmpeg.probe(original)
            vstream = next((s for s in probe.streams if s.codec_type == "video"), None)
            astream = next((s for s in probe.streams if s.codec_type == "audio"), None)
            src_audio = astream is not None
            if vstream:
                has_alpha = has_alpha_pix_fmt(vstream.pix_fmt) or has_alpha
                old_fps = parse_frame_rate(vstream.r_frame_rate or vstream.avg_frame_rate, old_fps)
            input_args = ["-i", str(original)]
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
        cmds = build_stinger_mezz_commands(
            input_args=input_args,
            mezz=mezz,
            thumb=thumb,
            vf=vf,
            has_audio=src_audio,
        )
        job.progress = 0.3
        self._emit(job)
        result = self.ffmpeg.run(cmds[0])
        self._append_log(item.id, result.output)
        if result.code != 0:
            return ConversionInfo(status=ConversionStatus.failed, error="stinger transcode failed")
        th = self.ffmpeg.run(cmds[1])
        self._append_log(item.id, th.output)

        probed = self.ffmpeg.probe(mezz)
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

        new_fps = fmt.frame_rate_num / fmt.frame_rate_den
        cut_frame, cut_ms = cut_frame_after_fps_change(
            item.options.cut_ms if item.options.cut_ms is not None else item.cut_ms,
            item.options.cut_frame if item.options.cut_frame is not None else item.cut_frame,
            old_fps,
            new_fps,
            frames,
        )
        decision = decide_ram_playback(
            frames=frames,
            fps=new_fps,
            width=fmt.width,
            height=fmt.height,
            has_alpha=True,
            ram_clip_max_s=self.ram_clip_max_s,
            ram_budget_bytes=self.ram_budget_bytes,
        )
        return ConversionInfo(
            status=ConversionStatus.ready,
            frames=frames,
            audio_samples=0,
            audio_channels=0,
            mezz=mezz.name,
            has_alpha=has_alpha,
            duration_s=frames / new_fps,
            playback="ram" if decision.use_ram else "decode_ahead",
            cut_frame=cut_frame,
            cut_ms=cut_ms,
        )


def copy_original_into_item(store: LibraryStore, item_id: str, source: Path, preferred_name: str | None = None) -> str:
    """Copy or link ``source`` into the item directory; return relative path stored in item.json."""
    directory = store.item_dir(item_id)
    directory.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        dest = directory / "sequence"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
        return "sequence"
    name = preferred_name or source.name
    dest = directory / name
    if source.resolve() != dest.resolve():
        shutil.copy2(source, dest)
    return name
