"""
Render pipeline: document → rendered cues → .sup file.

Used by the CLI, the queue engine and the UI. Supports cooperative
cancel/pause (checked between cues) and keeps per-cue render results so a
paused/resumed job continues instead of restarting.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Dict, List, Optional

from .model import SubtitleDocument
from .overrides import OverrideSet
from .pgs import TimedRender, TimelineBuilder, SupWriter
from .renderer import CanvasSpec, CueRenderer, RenderedCue, compute_canvas
from .timing import RetimePlan

ProgressFn = Callable[[int, int, str], None]


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
        renderer = CueRenderer(self.doc, self.canvas, self.overrides,
                               is_hdr=s.is_hdr)

        cues = [c for c in self.doc.sorted_cues()
                if (c.enabled or not s.selected_only)]
        total = len(cues)
        renders: List[TimedRender] = []
        for i, cue in enumerate(cues):
            if self.cancel_event.is_set():
                raise RenderCancelled()
            if self.pause_event.is_set():
                return None
            rc = self._render_cache.get(cue.uid)
            if cue.uid not in self._render_cache:
                rc = renderer.render_cue(cue)
                self._render_cache[cue.uid] = rc
            if rc is not None:
                renders.append(TimedRender(
                    cue.begin_ms + s.offset_ms,
                    cue.end_ms + s.offset_ms, rc))
            if progress:
                progress(i + 1, total,
                         f"Rendering cues {i + 1}/{total}")

        if self.cancel_event.is_set():
            raise RenderCancelled()

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
    def invalidate(self):
        """Call when the document/overrides changed: drop cached renders."""
        self._render_cache.clear()
