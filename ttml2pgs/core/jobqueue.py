"""
The render/mux queue engine.

Design goals (the v1 pain points this fixes):

* **Video-grouped hierarchy.** Jobs are organized into
  :class:`VideoGroup`s keyed by target video. A group's mux runs as soon
  as *that group's* renders are done — not after the whole batch — so a
  crash on episode 12 never leaves episodes 1-11 unmuxed.
* **Late additions.** Adding another subtitle for a video that is already
  queued (even mid-render) joins its group, and the group's mux waits for
  it.
* **External .sup entries** can be queued for mux-only (no render).
* **Pause / resume / cancel / reorder** at queue, group and job level.
  Pausing a running render checkpoints between cues and resumes without
  re-rendering finished cues.
* **Crash recovery.** Queue state persists to JSON; on reload, finished
  .sup files are detected and only missing work re-runs.

The engine is UI-agnostic (callbacks) and fully testable headless.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

from .model import SubtitleDocument
from .overrides import OverrideSet
from .parsers import load_subtitle
from .pipeline import RenderCancelled, RenderPipeline, RenderSettings
from .video import SubTrack, remux


class JobState(str, Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    PAUSED = 'paused'
    DONE = 'done'
    FAILED = 'failed'
    CANCELED = 'canceled'
    WAITING = 'waiting'      # mux waiting for renders

    def is_terminal(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELED)


_ids = itertools.count(1)


@dataclass
class RenderJob:
    doc: Optional[SubtitleDocument]
    sub_path: str
    settings: RenderSettings
    overrides: OverrideSet
    lang: str = ''
    track_name: str = ''
    state: JobState = JobState.PENDING
    #: jobs are *added* to the queue unstarted; only started jobs render.
    #: Resume also only affects started jobs, so what you queued but never
    #: launched stays put.
    started: bool = False
    progress: float = 0.0
    message: str = ''
    error: str = ''
    id: int = field(default_factory=lambda: next(_ids))
    pipeline: Optional[RenderPipeline] = None

    def label(self) -> str:
        return os.path.basename(self.settings.out_path or self.sub_path)


@dataclass
class ExternalSup:
    sup_path: str
    lang: str = 'und'
    track_name: str = ''
    id: int = field(default_factory=lambda: next(_ids))


@dataclass
class VideoGroup:
    video_path: Optional[str]              # None = standalone (no mux)
    render_jobs: List[RenderJob] = field(default_factory=list)
    external_sups: List[ExternalSup] = field(default_factory=list)
    mux_enabled: bool = True
    mux_state: JobState = JobState.WAITING
    mux_progress: float = 0.0
    mux_message: str = ''
    mux_error: str = ''
    replace_original: bool = True
    id: int = field(default_factory=lambda: next(_ids))

    def label(self) -> str:
        if self.video_path:
            return os.path.basename(self.video_path)
        return '(no video)'

    def renders_finished(self) -> bool:
        return all(j.state in (JobState.DONE,)
                   for j in self.render_jobs) and bool(self.render_jobs
                                                       or self.external_sups)

    def renders_settled(self) -> bool:
        """True when nothing renderable is pending/running/paused."""
        return all(j.state.is_terminal() for j in self.render_jobs)

    def unstarted_count(self) -> int:
        return sum(1 for j in self.render_jobs
                   if not j.started and not j.state.is_terminal())

    def wants_mux(self) -> bool:
        if not self.mux_enabled or self.video_path is None:
            return False
        if self.mux_state in (JobState.DONE, JobState.RUNNING,
                              JobState.CANCELED):
            return False
        if not self.renders_settled():
            return False
        done_subs = [j for j in self.render_jobs if j.state == JobState.DONE]
        return bool(done_subs or self.external_sups)


class QueueManager:
    """
    Thread-based queue processor. One render worker + one mux worker so a
    long mux never blocks rendering (and vice versa).
    """

    def __init__(self, state_path: Optional[str] = None):
        self.groups: List[VideoGroup] = []
        self.state_path = state_path
        #: after a successful mux, move sources+sups into a 'subs' subfolder
        self.move_to_subs = False
        self.on_change: Optional[Callable[[], None]] = None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._queue_paused = False
        self._stop = False
        self._render_thread: Optional[threading.Thread] = None
        self._mux_thread: Optional[threading.Thread] = None
        self._active_render: Optional[RenderJob] = None

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def snapshot(self) -> List[VideoGroup]:
        with self._lock:
            return list(self.groups)

    def is_paused(self) -> bool:
        return self._queue_paused

    def is_idle(self) -> bool:
        """No started work outstanding (unstarted queued jobs don't count)."""
        with self._lock:
            for g in self.groups:
                if any(j.started and not j.state.is_terminal()
                       for j in g.render_jobs):
                    return False
                if g.wants_mux() or g.mux_state == JobState.RUNNING:
                    return False
            return True

    def _notify(self):
        cb = self.on_change
        if cb:
            try:
                cb()
            except Exception:
                pass
        self._save_state()

    # ------------------------------------------------------------------ #
    # Adding work
    # ------------------------------------------------------------------ #
    def _group_for_video(self, video_path: Optional[str]) -> VideoGroup:
        """Same video (path-normalized) → same group. No video → own group."""
        if video_path:
            norm = os.path.normcase(os.path.abspath(video_path))
            with self._lock:
                for g in self.groups:
                    if g.video_path and \
                            os.path.normcase(os.path.abspath(g.video_path)) == norm:
                        return g
        g = VideoGroup(video_path=video_path)
        with self._lock:
            self.groups.append(g)
        return g

    def add_render(self, doc: SubtitleDocument, sub_path: str,
                   settings: RenderSettings, overrides: OverrideSet,
                   video_path: Optional[str] = None, lang: str = '',
                   track_name: str = '', start: bool = False) -> RenderJob:
        job = RenderJob(doc=doc, sub_path=sub_path, settings=settings,
                        overrides=overrides,
                        lang=lang or (doc.language if doc else ''),
                        track_name=track_name, started=start)
        with self._lock:
            group = self._group_for_video(video_path)
            group.render_jobs.append(job)
            if group.mux_state.is_terminal():
                # new work re-arms the group's mux
                group.mux_state = JobState.WAITING
        self._wake.set()
        self._notify()
        return job

    def add_external_sup(self, video_path: str, sup_path: str,
                         lang: str = 'und', track_name: str = ''
                         ) -> ExternalSup:
        ent = ExternalSup(sup_path=sup_path, lang=lang, track_name=track_name)
        with self._lock:
            group = self._group_for_video(video_path)
            group.external_sups.append(ent)
            if group.mux_state.is_terminal():
                group.mux_state = JobState.WAITING
        self._wake.set()
        self._notify()
        return ent

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    def start(self):
        self._stop = False
        if self._render_thread is None or not self._render_thread.is_alive():
            self._render_thread = threading.Thread(
                target=self._render_loop, name='t2p-render', daemon=True)
            self._render_thread.start()
        if self._mux_thread is None or not self._mux_thread.is_alive():
            self._mux_thread = threading.Thread(
                target=self._mux_loop, name='t2p-mux', daemon=True)
            self._mux_thread.start()

    def shutdown(self, wait: bool = False):
        self._stop = True
        self._wake.set()
        with self._lock:
            if self._active_render and self._active_render.pipeline:
                self._active_render.pipeline.pause_event.set()
        if wait:
            for t in (self._render_thread, self._mux_thread):
                if t and t.is_alive():
                    t.join(timeout=10)

    # -- starting (added ≠ started) ------------------------------------- #
    def start_job(self, job_id: int):
        with self._lock:
            j = self._find_job(job_id)
            if j is None:
                return
            j.started = True
            if j.state == JobState.PAUSED:
                j.state = JobState.PENDING
                if j.pipeline:
                    j.pipeline.pause_event.clear()
        self._wake.set()
        self._notify()

    def start_group(self, group_id: int):
        with self._lock:
            for g in self.groups:
                if g.id == group_id:
                    for j in g.render_jobs:
                        if not j.state.is_terminal():
                            j.started = True
                            if j.state == JobState.PAUSED:
                                j.state = JobState.PENDING
                                if j.pipeline:
                                    j.pipeline.pause_event.clear()
        self._wake.set()
        self._notify()

    def start_all(self):
        with self._lock:
            self._queue_paused = False
            for g in self.groups:
                for j in g.render_jobs:
                    if not j.state.is_terminal():
                        j.started = True
                        if j.state == JobState.PAUSED:
                            j.state = JobState.PENDING
                            if j.pipeline:
                                j.pipeline.pause_event.clear()
        self._wake.set()
        self._notify()

    def pause_all(self):
        """Pause rendering: checkpoints the running job and stops picking
        up further *started* jobs. Unstarted jobs are unaffected."""
        self._queue_paused = True
        with self._lock:
            job = self._active_render
            if job and job.pipeline:
                job.pipeline.pause_event.set()
        self._notify()

    def resume_all(self):
        """Resume paused *started* work. Jobs that were only added to the
        queue (never started) stay waiting for an explicit start."""
        self._queue_paused = False
        with self._lock:
            for g in self.groups:
                for j in g.render_jobs:
                    if j.started and j.state == JobState.PAUSED:
                        j.state = JobState.PENDING
                        if j.pipeline:
                            j.pipeline.pause_event.clear()
        self._wake.set()
        self._notify()

    def pause_job(self, job_id: int):
        with self._lock:
            j = self._find_job(job_id)
            if j is None:
                return
            if j.state == JobState.RUNNING and j.pipeline:
                j.pipeline.pause_event.set()
            elif j.state == JobState.PENDING and j.started:
                j.state = JobState.PAUSED
        self._notify()

    def resume_job(self, job_id: int):
        with self._lock:
            j = self._find_job(job_id)
            if j and j.state == JobState.PAUSED:
                j.state = JobState.PENDING
                if j.pipeline:
                    j.pipeline.pause_event.clear()
        self._wake.set()
        self._notify()

    def cancel_job(self, job_id: int):
        with self._lock:
            j = self._find_job(job_id)
            if j is None:
                return
            if j.state == JobState.RUNNING and j.pipeline:
                j.pipeline.cancel_event.set()
                j.pipeline.pause_event.set()
            elif not j.state.is_terminal():
                j.state = JobState.CANCELED
        self._wake.set()
        self._notify()

    def remove_job(self, job_id: int):
        with self._lock:
            for g in self.groups:
                for j in list(g.render_jobs):
                    if j.id == job_id and j.state != JobState.RUNNING:
                        g.render_jobs.remove(j)
                for e in list(g.external_sups):
                    if e.id == job_id:
                        g.external_sups.remove(e)
            self._prune_empty_groups()
        self._notify()

    def remove_group(self, group_id: int):
        with self._lock:
            for g in list(self.groups):
                if g.id == group_id:
                    for j in g.render_jobs:
                        if j.state == JobState.RUNNING and j.pipeline:
                            j.pipeline.cancel_event.set()
                            j.pipeline.pause_event.set()
                    self.groups.remove(g)
        self._notify()

    def set_group_mux(self, group_id: int, enabled: bool):
        with self._lock:
            for g in self.groups:
                if g.id == group_id:
                    g.mux_enabled = enabled
                    if enabled and g.mux_state.is_terminal():
                        g.mux_state = JobState.WAITING
        self._wake.set()
        self._notify()

    def move_group(self, group_id: int, delta: int):
        with self._lock:
            idx = next((i for i, g in enumerate(self.groups)
                        if g.id == group_id), None)
            if idx is None:
                return
            new = max(0, min(len(self.groups) - 1, idx + delta))
            g = self.groups.pop(idx)
            self.groups.insert(new, g)
        self._notify()

    def move_job(self, job_id: int, delta: int):
        with self._lock:
            for g in self.groups:
                ids = [j.id for j in g.render_jobs]
                if job_id in ids:
                    idx = ids.index(job_id)
                    new = max(0, min(len(ids) - 1, idx + delta))
                    j = g.render_jobs.pop(idx)
                    g.render_jobs.insert(new, j)
                    break
        self._notify()

    def retry_job(self, job_id: int):
        with self._lock:
            j = self._find_job(job_id)
            if j and j.state in (JobState.FAILED, JobState.CANCELED):
                j.state = JobState.PENDING
                j.started = True          # retrying implies starting
                j.error = ''
                j.progress = 0.0
                j.pipeline = None
                grp = self._group_of(j)
                if grp and grp.mux_state.is_terminal():
                    grp.mux_state = JobState.WAITING
        self._wake.set()
        self._notify()

    # ------------------------------------------------------------------ #
    def find_job(self, job_id: int) -> Optional[RenderJob]:
        with self._lock:
            return self._find_job(job_id)

    def _find_job(self, job_id: int) -> Optional[RenderJob]:
        for g in self.groups:
            for j in g.render_jobs:
                if j.id == job_id:
                    return j
        return None

    def _group_of(self, job: RenderJob) -> Optional[VideoGroup]:
        for g in self.groups:
            if job in g.render_jobs:
                return g
        return None

    def _prune_empty_groups(self):
        self.groups = [g for g in self.groups
                       if g.render_jobs or g.external_sups]

    # ------------------------------------------------------------------ #
    # Workers
    # ------------------------------------------------------------------ #
    def _next_render(self) -> Optional[RenderJob]:
        with self._lock:
            if self._queue_paused:
                return None
            for g in self.groups:
                for j in g.render_jobs:
                    if j.state == JobState.PENDING and j.started:
                        return j
        return None

    def _render_loop(self):
        while not self._stop:
            job = self._next_render()
            if job is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            self._run_render(job)

    def _run_render(self, job: RenderJob):
        with self._lock:
            if job.doc is None:
                try:
                    job.doc = load_subtitle(job.sub_path)
                except Exception as e:
                    job.state = JobState.FAILED
                    job.error = f"load failed: {e}"
                    self._notify()
                    return
            if job.pipeline is None:
                job.pipeline = RenderPipeline(job.doc, job.settings,
                                              job.overrides)
            job.pipeline.pause_event.clear()
            job.state = JobState.RUNNING
            self._active_render = job
        self._notify()

        def progress(cur, total, msg):
            job.progress = cur / max(1, total)
            job.message = msg
            cb = self.on_change
            if cb:
                try:
                    cb()
                except Exception:
                    pass

        try:
            result = job.pipeline.run(progress=progress)
            with self._lock:
                if result is None:
                    job.state = JobState.PAUSED
                    job.message = 'paused'
                else:
                    job.state = JobState.DONE
                    job.progress = 1.0
                    job.message = 'done'
        except RenderCancelled:
            with self._lock:
                job.state = JobState.CANCELED
                job.message = 'canceled'
        except Exception as e:
            traceback.print_exc()
            with self._lock:
                job.state = JobState.FAILED
                job.error = str(e)
        finally:
            with self._lock:
                self._active_render = None
        self._wake.set()
        self._notify()

    # -- mux ------------------------------------------------------------ #
    def _next_mux(self) -> Optional[VideoGroup]:
        with self._lock:
            if self._queue_paused:
                return None
            for g in self.groups:
                if g.wants_mux():
                    g.mux_state = JobState.RUNNING
                    return g
        return None

    def _mux_loop(self):
        while not self._stop:
            group = self._next_mux()
            if group is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            self._run_mux(group)

    def _run_mux(self, group: VideoGroup):
        self._notify()
        subs: List[SubTrack] = []
        for j in group.render_jobs:
            if j.state == JobState.DONE and os.path.exists(j.settings.out_path):
                subs.append(SubTrack(path=j.settings.out_path, lang=j.lang,
                                     track_name=j.track_name))
        for e in group.external_sups:
            if os.path.exists(e.sup_path):
                subs.append(SubTrack(path=e.sup_path, lang=e.lang,
                                     track_name=e.track_name))
        if not subs:
            with self._lock:
                group.mux_state = JobState.FAILED
                group.mux_error = 'no subtitle tracks produced'
            self._notify()
            return

        def progress(cur, total, msg):
            group.mux_progress = cur / max(1, total)
            group.mux_message = msg
            cb = self.on_change
            if cb:
                try:
                    cb()
                except Exception:
                    pass

        ok, res = remux(group.video_path, subs,
                        replace_original=group.replace_original,
                        progress=progress,
                        cancel=lambda: self._stop)
        with self._lock:
            if ok:
                group.mux_state = JobState.DONE
                group.mux_message = f"muxed → {os.path.basename(res)}"
            else:
                group.mux_state = JobState.FAILED
                group.mux_error = res
        if ok and self.move_to_subs:
            self._move_to_subs_folder(group)
        self._notify()

    def _move_to_subs_folder(self, group: VideoGroup):
        """Tidy sources + rendered sups into a 'subs' subfolder."""
        import shutil
        base = os.path.dirname(group.video_path or '') or '.'
        subs_dir = os.path.join(base, 'subs')
        try:
            os.makedirs(subs_dir, exist_ok=True)
        except OSError:
            return
        for j in group.render_jobs:
            for path in (j.settings.out_path, j.sub_path):
                try:
                    if path and os.path.exists(path) and \
                            os.path.dirname(os.path.abspath(path)) != \
                            os.path.abspath(subs_dir):
                        shutil.move(path,
                                    os.path.join(subs_dir,
                                                 os.path.basename(path)))
                except OSError:
                    pass

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _save_state(self):
        if not self.state_path:
            return
        try:
            with self._lock:
                data = {
                    'groups': [{
                        'video_path': g.video_path,
                        'mux_enabled': g.mux_enabled,
                        'mux_state': g.mux_state.value,
                        'replace_original': g.replace_original,
                        'renders': [{
                            'sub_path': j.sub_path,
                            'settings': j.settings.to_dict(),
                            'overrides': j.overrides.to_dict(),
                            'lang': j.lang,
                            'track_name': j.track_name,
                            'state': j.state.value,
                            'started': j.started,
                        } for j in g.render_jobs],
                        'external': [{
                            'sup_path': e.sup_path, 'lang': e.lang,
                            'track_name': e.track_name,
                        } for e in g.external_sups],
                    } for g in self.groups],
                }
            tmp = self.state_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def load_state(self) -> int:
        """Restore a previously saved queue. Returns #jobs restored."""
        if not self.state_path or not os.path.exists(self.state_path):
            return 0
        try:
            with open(self.state_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return 0
        count = 0
        with self._lock:
            for gd in data.get('groups', []):
                g = VideoGroup(video_path=gd.get('video_path'))
                g.mux_enabled = bool(gd.get('mux_enabled', True))
                g.replace_original = bool(gd.get('replace_original', True))
                prev_mux = gd.get('mux_state', 'waiting')
                for jd in gd.get('renders', []):
                    settings = RenderSettings.from_dict(jd.get('settings', {}))
                    job = RenderJob(
                        doc=None, sub_path=jd.get('sub_path', ''),
                        settings=settings,
                        overrides=OverrideSet.from_dict(
                            jd.get('overrides', {})),
                        lang=jd.get('lang', ''),
                        track_name=jd.get('track_name', ''))
                    prev = jd.get('state')
                    job.started = bool(jd.get('started', prev != 'pending'))
                    # crash recovery: done + file exists → keep done;
                    # otherwise re-queue.
                    if prev == 'done' and os.path.exists(settings.out_path):
                        job.state = JobState.DONE
                        job.progress = 1.0
                    else:
                        job.state = JobState.PENDING
                    g.render_jobs.append(job)
                    count += 1
                for ed in gd.get('external', []):
                    g.external_sups.append(ExternalSup(
                        sup_path=ed.get('sup_path', ''),
                        lang=ed.get('lang', 'und'),
                        track_name=ed.get('track_name', '')))
                    count += 1
                if prev_mux == 'done':
                    g.mux_state = JobState.DONE
                if g.render_jobs or g.external_sups:
                    self.groups.append(g)
        self._notify()
        return count
