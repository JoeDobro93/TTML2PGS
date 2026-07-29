"""
Render pipeline: document → rendered cues → .sup file.

Used by the CLI, the queue engine and the UI. Supports cooperative
cancel/pause (checked between cues) and keeps per-cue render results so a
paused/resumed job continues instead of restarting.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Dict, List, Optional

from .model import Cue, SubtitleDocument
from .overrides import OverrideSet
from .pgs import TimedRender, TimelineBuilder, SupWriter
from .renderer import CanvasSpec, CueRenderer, RenderedCue, compute_canvas
from .timing import RetimePlan

ProgressFn = Callable[[int, int, str], None]

#: below this many pending cues a worker pool costs more than it saves
#: (each worker re-imports the package and scans system fonts on start)
MIN_PARALLEL_CUES = 16


def auto_workers() -> int:
    """Default worker count: leave one core for the UI/muxing, cap at 8."""
    return max(1, min((os.cpu_count() or 2) - 1, 8))


# --------------------------------------------------------------------------- #
# Worker-process side of the parallel renderer. Cue rendering is pure
# (same inputs -> same bitmap), so cues can render in any process in any
# order; the parent reassembles results in cue order, keeping the output
# .sup byte-identical to a sequential render.
# --------------------------------------------------------------------------- #

_POOL_RENDERER: Optional[CueRenderer] = None
_POOL_CUES: Dict[int, Cue] = {}


def _pool_init(doc: SubtitleDocument, overrides: OverrideSet,
               canvas: CanvasSpec, is_hdr: bool) -> None:
    global _POOL_RENDERER, _POOL_CUES
    _POOL_RENDERER = CueRenderer(doc, canvas, overrides, is_hdr=is_hdr)
    _POOL_CUES = {c.uid: c for c in doc.cues}


def _pool_render(uid: int) -> Optional[RenderedCue]:
    return _POOL_RENDERER.render_cue(_POOL_CUES[uid])


@dataclass
class RenderSettings:
    """Everything needed to render one document to one .sup."""
    out_path: str = ''
    video_res: Optional[tuple] = None          # (w, h) of target video
    target_fps: Fraction = Fraction(24000, 1001)
    retime: Optional[RetimePlan] = None
    offset_ms: float = 0.0
    selected_only: bool = False                # render only enabled cues
    is_hdr: bool = False                       # target video dynamic range
    workers: int = 0                           # render processes: 0 = auto,
                                               # 1 = sequential

    def to_dict(self) -> dict:
        return {
            'out_path': self.out_path,
            'video_res': list(self.video_res) if self.video_res else None,
            'target_fps': [self.target_fps.numerator,
                           self.target_fps.denominator],
            'retime': ([str(self.retime.scale), self.retime.offset_ms,
                        self.retime.description] if self.retime else None),
            'offset_ms': self.offset_ms,
            'selected_only': self.selected_only,
            'is_hdr': self.is_hdr,
            'workers': self.workers,
        }

    @staticmethod
    def from_dict(d: dict) -> 'RenderSettings':
        rs = RenderSettings()
        rs.out_path = d.get('out_path', '')
        vr = d.get('video_res')
        rs.video_res = tuple(vr) if vr else None
        tf = d.get('target_fps') or [24000, 1001]
        rs.target_fps = Fraction(tf[0], tf[1])
        rt = d.get('retime')
        if rt:
            rs.retime = RetimePlan(Fraction(rt[0]), float(rt[1]), rt[2])
        rs.offset_ms = float(d.get('offset_ms', 0.0))
        rs.selected_only = bool(d.get('selected_only', False))
        rs.is_hdr = bool(d.get('is_hdr', False))
        rs.workers = int(d.get('workers', 0))
        return rs


class RenderCancelled(Exception):
    pass


class RenderPipeline:
    """One render job. Re-entrant: pause and call run() again to resume."""

    def __init__(self, doc: SubtitleDocument, settings: RenderSettings,
                 overrides: Optional[OverrideSet] = None):
        self.doc = doc
        self.settings = settings
        self.overrides = overrides or OverrideSet()
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self._render_cache: Dict[int, Optional[RenderedCue]] = {}
        self.canvas: Optional[CanvasSpec] = None

    # ------------------------------------------------------------------ #
    def run(self, progress: Optional[ProgressFn] = None) -> Optional[str]:
        """
        Render + encode. Returns the output path, or None if paused
        (call run() again to resume) — raises RenderCancelled on cancel.
        """
        s = self.settings
        self.canvas = compute_canvas(s.video_res, self.overrides.layout)

        cues = [c for c in self.doc.sorted_cues()
                if (c.enabled or not s.selected_only)]
        total = len(cues)
        todo = [c for c in cues if c.uid not in self._render_cache]
        done = total - len(todo)

        workers = s.workers if s.workers > 0 else auto_workers()
        if len(todo) < MIN_PARALLEL_CUES:
            workers = 1
        if todo:
            if workers > 1:
                finished = self._render_parallel(todo, min(workers,
                                                           len(todo)),
                                                 done, total, progress)
            else:
                finished = self._render_sequential(todo, done, total,
                                                   progress)
            if not finished:
                return None                     # paused

        if self.cancel_event.is_set():
            raise RenderCancelled()

        # assemble in cue order — identical regardless of render order
        renders: List[TimedRender] = []
        for cue in cues:
            rc = self._render_cache.get(cue.uid)
            if rc is not None:
                renders.append(TimedRender(
                    cue.begin_ms + s.offset_ms,
                    cue.end_ms + s.offset_ms, rc))

        out_dir = os.path.dirname(os.path.abspath(s.out_path))
        os.makedirs(out_dir, exist_ok=True)

        tb = TimelineBuilder(self.canvas.width, self.canvas.height)
        events = tb.build(renders, retime=s.retime, snap_fps=s.target_fps)
        writer = SupWriter(self.canvas.width, self.canvas.height,
                           s.target_fps)
        ok = writer.write(events, s.out_path, progress=progress,
                          cancel=self.cancel_event.is_set)
        if not ok:
            raise RenderCancelled()
        return s.out_path

    # ------------------------------------------------------------------ #
    def _render_sequential(self, todo: List[Cue], done: int, total: int,
                           progress: Optional[ProgressFn]) -> bool:
        """Render cues in-process. False = paused; raises on cancel."""
        renderer = CueRenderer(self.doc, self.canvas, self.overrides,
                               is_hdr=self.settings.is_hdr)
        for cue in todo:
            if self.cancel_event.is_set():
                raise RenderCancelled()
            if self.pause_event.is_set():
                return False
            self._render_cache[cue.uid] = renderer.render_cue(cue)
            done += 1
            if progress:
                progress(done, total, f"Rendering cues {done}/{total}")
        return True

    def _render_parallel(self, todo: List[Cue], workers: int, done: int,
                         total: int,
                         progress: Optional[ProgressFn]) -> bool:
        """
        Render cues across a process pool. Uses the 'spawn' start method
        on every platform: run() is called from Qt worker threads, where
        fork() is unsafe. Falls back to sequential rendering if the pool
        cannot be built or dies mid-run.
        """
        import concurrent.futures as cf
        s = self.settings
        try:
            ex = cf.ProcessPoolExecutor(
                max_workers=workers,
                mp_context=multiprocessing.get_context('spawn'),
                initializer=_pool_init,
                initargs=(self.doc, self.overrides, self.canvas, s.is_hdr))
        except Exception:
            return self._render_sequential(todo, done, total, progress)
        try:
            futs = {ex.submit(_pool_render, c.uid): c for c in todo}
            for fut in cf.as_completed(futs):
                if self.cancel_event.is_set():
                    raise RenderCancelled()
                self._render_cache[futs[fut].uid] = fut.result()
                done += 1
                if progress:
                    progress(done, total,
                             f"Rendering cues {done}/{total}")
                if self.pause_event.is_set():
                    return False        # completed renders stay cached
        except RenderCancelled:
            raise
        except Exception:
            # pool broke (worker killed, pickling failure…) — finish the
            # remaining cues in-process so the job still completes; a
            # genuine render bug will re-raise identically here.
            left = [c for c in todo if c.uid not in self._render_cache]
            return self._render_sequential(left, done, total, progress)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        return True

    # ------------------------------------------------------------------ #
    def invalidate(self):
        """Call when the document/overrides changed: drop cached renders."""
        self._render_cache.clear()
