"""
Time expressions, frame rates and retiming.

* TTML time expression parsing (clock-time incl. SMPTE frames, offset-time
  with h/m/s/ms/f/t metrics) — TTML1 §10.3.1 / TTML2 §12.3.
* WebVTT / SRT timestamp parsing.
* Rational frame rates and conversion ("conform") between them, covering
  the common mismatch scenarios (23.976<->24, 25 speedup, NTSC pulldown…).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Frame rates
# --------------------------------------------------------------------------- #

COMMON_RATES: List[Tuple[str, Fraction]] = [
    ("23.976 (24000/1001)", Fraction(24000, 1001)),
    ("24", Fraction(24, 1)),
    ("25 (PAL)", Fraction(25, 1)),
    ("29.97 (30000/1001)", Fraction(30000, 1001)),
    ("30", Fraction(30, 1)),
    ("50", Fraction(50, 1)),
    ("59.94 (60000/1001)", Fraction(60000, 1001)),
    ("60", Fraction(60, 1)),
]


def normalize_fps(num: float, den: float = 1.0) -> Fraction:
    """
    Snap a possibly-float fps to the nearest well-known broadcast rational.
    e.g. 23.976 -> 24000/1001;  23.98 -> 24000/1001;  24.0 -> 24/1.
    """
    if den and den != 1.0:
        f = Fraction(int(round(num)), int(round(den)))
    else:
        f = Fraction(num).limit_denominator(1001)
    fl = float(f)
    for _, rate in COMMON_RATES:
        if abs(float(rate) - fl) < 0.005:
            return rate
    return f


def fps_label(fps: Fraction) -> str:
    fl = float(fps)
    return f"{fl:.3f}".rstrip('0').rstrip('.')


# --------------------------------------------------------------------------- #
# Retiming (frame-rate conform + offsets)
# --------------------------------------------------------------------------- #

@dataclass
class RetimePlan:
    """
    A linear retime: t' = t * scale + offset_ms.

    scale for a frame-rate conform src->dst is src/dst: content that was
    timed against a 23.976 master and is muxed with a true-24 video must
    have its timestamps compressed by 24000/1001 / 24 = 1000/1001.
    """
    scale: Fraction = Fraction(1)
    offset_ms: float = 0.0
    description: str = ""

    def apply(self, ms: float) -> float:
        return float(ms * self.scale + self.offset_ms)

    @staticmethod
    def conform(src: Fraction, dst: Fraction, offset_ms: float = 0.0) -> 'RetimePlan':
        if src == dst:
            return RetimePlan(Fraction(1), offset_ms,
                              f"offset {offset_ms:+.0f}ms" if offset_ms else "no change")
        return RetimePlan(src / dst, offset_ms,
                          f"conform {fps_label(src)} -> {fps_label(dst)}")


#: Common conversion scenarios offered in the UI, as (label, src, dst, note).
CONFORM_SCENARIOS = [
    ("23.976 → 24 (sub master NTSC-film, video true 24)",
     Fraction(24000, 1001), Fraction(24, 1),
     "Timestamps shrink by 1000/1001 (~3.6s over 1h)."),
    ("24 → 23.976 (sub master true 24, video NTSC-film)",
     Fraction(24, 1), Fraction(24000, 1001),
     "Timestamps stretch by 1001/1000."),
    ("25 → 23.976 (PAL-speedup source, film-rate video)",
     Fraction(25, 1), Fraction(24000, 1001),
     "Undo PAL 4% speedup: timestamps stretch by ~4.3%."),
    ("23.976 → 25 (film-rate source, PAL-speedup video)",
     Fraction(24000, 1001), Fraction(25, 1),
     "Apply PAL 4% speedup."),
    ("30 → 29.97 (true-30 timing on NTSC video)",
     Fraction(30, 1), Fraction(30000, 1001),
     "Timestamps stretch by 1001/1000."),
    ("29.97 → 23.976 (telecined NTSC timing, IVTC'd video)",
     Fraction(30000, 1001), Fraction(30000, 1001),
     "3:2 pulldown preserves real time — timestamps unchanged. Only frame "
     "*counts* differ; pick this to confirm no conform is needed."),
    ("24 → 25 (true 24 master to PAL)",
     Fraction(24, 1), Fraction(25, 1), "Timestamps shrink by 4%."),
]


def suggest_conform(sub_fps: Optional[Fraction],
                    video_fps: Optional[Fraction]) -> Optional[RetimePlan]:
    """
    Suggest a retime when the subtitle's declared frame rate differs from
    the target video's. Telecine pairs (29.97 subs / 23.976 video) preserve
    real time, so they map to 'no change'.
    """
    if not sub_fps or not video_fps or sub_fps == video_fps:
        return None
    a, b = float(sub_fps), float(video_fps)
    # 29.97<->23.976 (and 59.94<->23.976): telecine — same real-time clock.
    tele = {(30000, 24000), (24000, 30000), (60000, 24000), (24000, 60000)}
    key = (round(a * 1001), round(b * 1001))
    if key in tele:
        return None
    return RetimePlan.conform(sub_fps, video_fps)


# --------------------------------------------------------------------------- #
# TTML time parsing
# --------------------------------------------------------------------------- #

_CLOCK_RE = re.compile(
    r'^(\d{2,}):(\d{2}):(\d{2})(?:(\.\d+)|:(\d{2,})(?:\.(\d+))?)?$')
_OFFSET_RE = re.compile(r'^([0-9]*\.?[0-9]+)(h|m|s|ms|f|t)$')


@dataclass
class TTMLTimeContext:
    """Parameters governing TTML time expression evaluation."""
    frame_rate: Fraction = Fraction(30, 1)          # effective (rate * multiplier)
    sub_frame_rate: int = 1
    tick_rate: int = 1                              # default = frame_rate when frames
    time_base: str = 'media'                        # media | smpte | clock
    drop_mode: str = 'nonDrop'

    def effective_tick_rate(self) -> int:
        return max(1, self.tick_rate)


def parse_ttml_time(text: str, ctx: TTMLTimeContext) -> Optional[float]:
    """Parse a TTML time expression to milliseconds (media time)."""
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None

    m = _OFFSET_RE.match(s)
    if m:
        val = float(m.group(1))
        metric = m.group(2)
        if metric == 'h':
            return val * 3600_000.0
        if metric == 'm':
            return val * 60_000.0
        if metric == 's':
            return val * 1000.0
        if metric == 'ms':
            return val
        if metric == 'f':
            return val / float(ctx.frame_rate) * 1000.0
        if metric == 't':
            return val / ctx.effective_tick_rate() * 1000.0

    m = _CLOCK_RE.match(s)
    if m:
        h, mi, sec = int(m.group(1)), int(m.group(2)), int(m.group(3))
        frac, frames, subframes = m.group(4), m.group(5), m.group(6)
        total = h * 3600.0 + mi * 60.0 + sec
        if frac:
            total += float(frac)
        elif frames is not None:
            fr = int(frames)
            if subframes:
                fr = fr + int(subframes) / max(1, ctx.sub_frame_rate)
            if ctx.time_base == 'smpte':
                # SMPTE NDF: HH:MM:SS:FF counts nominal frames; the wall
                # clock runs at the *effective* rate. Convert via frame count.
                nominal = int(round(float(ctx.frame_rate)))
                total_frames = ((h * 3600 + mi * 60 + sec) * nominal) + fr
                return total_frames / float(ctx.frame_rate) * 1000.0
            total += fr / float(ctx.frame_rate)
        return total * 1000.0
    return None


# --------------------------------------------------------------------------- #
# WebVTT / SRT timestamps
# --------------------------------------------------------------------------- #

_VTT_TS_RE = re.compile(r'^(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{1,3})$')


def parse_vtt_timestamp(text: str) -> Optional[float]:
    """'HH:MM:SS.mmm' or 'MM:SS.mmm' (comma tolerated) -> ms."""
    m = _VTT_TS_RE.match(text.strip())
    if not m:
        return None
    h = int(m.group(1)) if m.group(1) else 0
    mi, s = int(m.group(2)), int(m.group(3))
    ms = int(m.group(4).ljust(3, '0'))
    return ((h * 3600 + mi * 60 + s) * 1000 + ms) * 1.0


def format_vtt_timestamp(ms: float) -> str:
    ms = max(0, int(round(ms)))
    h, rem = divmod(ms, 3600_000)
    mi, rem = divmod(rem, 60_000)
    s, msec = divmod(rem, 1000)
    return f"{h:02d}:{mi:02d}:{s:02d}.{msec:03d}"


def format_srt_timestamp(ms: float) -> str:
    return format_vtt_timestamp(ms).replace('.', ',')


def format_display_time(ms: float) -> str:
    """Editing-friendly HH:MM:SS.mmm."""
    return format_vtt_timestamp(ms)


def parse_display_time(text: str) -> Optional[float]:
    """Accept HH:MM:SS.mmm / MM:SS.mmm / plain seconds / plain ms suffix."""
    s = text.strip()
    if not s:
        return None
    v = parse_vtt_timestamp(s)
    if v is not None:
        return v
    try:
        if s.endswith('ms'):
            return float(s[:-2])
        return float(s) * 1000.0
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# SMPTE helpers (BDN/PGS side)
# --------------------------------------------------------------------------- #

def ms_to_frames(ms: float, fps: Fraction) -> int:
    return int(round(ms / 1000.0 * float(fps)))


def frames_to_ms(frames: int, fps: Fraction) -> float:
    return frames * 1000.0 / float(fps)


def snap_ms_to_frame(ms: float, fps: Fraction) -> float:
    """Snap a millisecond time onto the target frame grid."""
    return frames_to_ms(ms_to_frames(ms, fps), fps)
