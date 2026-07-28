"""
PGS (.sup) writer with an overlap-aware, jitter-free timeline.

Key improvements over the v1 encoder:

* **Stable objects across overlaps.** Each display set carries up to two
  composition objects/windows (the BD limit). A cue that continues across
  a slice boundary keeps a byte-identical bitmap at an identical window
  position, so overlapping cues never make each other shift or shimmer.
  Only when >2 disjoint groups (or overlapping boxes) exist are bitmaps
  composited — and the composite for a given *set* of cues is cached, so
  it too is byte-identical between slices.
* **Arbitrary canvas sizes.** Any (width, height) is written; no forced
  1080p rounding, no even-dimension padding workarounds.
* **Palette quality.** Vectorized quantization: exact palette when ≤255
  colors, 4-bit posterize fallback, then top-255-by-frequency with
  nearest-neighbour remap (no dropped/transparent pixels). BT.709 matrix
  for HD, BT.601 for SD.
* **Frame-rate conform.** Timestamps can be retimed with a RetimePlan and
  are snapped to the target frame grid before PTS conversion.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

import numpy as np

from .renderer import RenderedCue
from .timing import RetimePlan, snap_ms_to_frame

# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


@dataclass
class SupObject:
    x: int
    y: int
    bitmap: np.ndarray            # HxWx4 uint8 straight RGBA


@dataclass
class SupEvent:
    start_ms: float
    end_ms: float
    objects: List[SupObject]


@dataclass
class TimedRender:
    start_ms: float
    end_ms: float
    render: RenderedCue


def _rects_overlap(a: Tuple[int, int, int, int],
                   b: Tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or
                ay + ah <= by or by + bh <= ay)


def _union_rect(rects: Sequence[Tuple[int, int, int, int]]):
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return (x0, y0, x1 - x0, y1 - y0)


class TimelineBuilder:
    """Slices overlapping cues into display sets with stable objects."""

    def __init__(self, canvas_w: int, canvas_h: int):
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h
        self._composite_cache: Dict[FrozenSet[int], SupObject] = {}

    def build(self, renders: List[TimedRender],
              retime: Optional[RetimePlan] = None,
              snap_fps: Optional[Fraction] = None,
              min_duration_ms: float = 40.0) -> List[SupEvent]:
        entries = []
        for tr in renders:
            s, e = tr.start_ms, tr.end_ms
            if retime is not None:
                s, e = retime.apply(s), retime.apply(e)
            if snap_fps:
                s = snap_ms_to_frame(s, snap_fps)
                e = snap_ms_to_frame(e, snap_fps)
            if e - s < min_duration_ms:
                e = s + min_duration_ms
            if s < 0:
                s, e = 0.0, max(e, min_duration_ms)
            entries.append((s, e, tr.render))
        if not entries:
            return []

        points = sorted({p for s, e, _ in entries for p in (s, e)})
        events: List[SupEvent] = []
        for i in range(len(points) - 1):
            t0, t1 = points[i], points[i + 1]
            if t1 - t0 <= 0.5:
                continue
            mid = (t0 + t1) / 2.0
            active = [(s, e, rc) for s, e, rc in entries if s <= mid < e]
            if not active:
                continue
            # z-order: earlier start first (later cues composite on top)
            active.sort(key=lambda t: (t[0], t[2].cue_uid))
            objects = self._make_objects([rc for _, _, rc in active])
            events.append(SupEvent(t0, t1, objects))
        return events

    # ------------------------------------------------------------------ #
    def _make_objects(self, renders: List[RenderedCue]) -> List[SupObject]:
        groups: List[List[RenderedCue]] = []
        rects: List[Tuple[int, int, int, int]] = []
        for rc in renders:
            rect = (rc.x, rc.y, rc.width, rc.height)
            merged = False
            for gi, grect in enumerate(rects):
                if _rects_overlap(rect, grect):
                    groups[gi].append(rc)
                    rects[gi] = _union_rect([grect, rect])
                    merged = True
                    break
            if not merged:
                groups.append([rc])
                rects.append(rect)
        # collapse transitively-overlapping groups
        changed = True
        while changed and len(groups) > 1:
            changed = False
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    if _rects_overlap(rects[i], rects[j]):
                        groups[i].extend(groups[j])
                        rects[i] = _union_rect([rects[i], rects[j]])
                        del groups[j], rects[j]
                        changed = True
                        break
                if changed:
                    break
        # BD limit: at most 2 composition objects — merge nearest pairs
        while len(groups) > 2:
            best, bi, bj = None, 0, 1
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    u = _union_rect([rects[i], rects[j]])
                    waste = u[2] * u[3] - rects[i][2] * rects[i][3] \
                        - rects[j][2] * rects[j][3]
                    if best is None or waste < best:
                        best, bi, bj = waste, i, j
            groups[bi].extend(groups[bj])
            rects[bi] = _union_rect([rects[bi], rects[bj]])
            del groups[bj], rects[bj]

        objects = []
        for grp in groups:
            objects.append(self._object_for_group(grp))
        # deterministic window order: top-most first
        objects.sort(key=lambda o: (o.y, o.x))
        return objects[:2]

    def _object_for_group(self, group: List[RenderedCue]) -> SupObject:
        if len(group) == 1:
            rc = group[0]
            return SupObject(rc.x, rc.y, rc.bitmap)
        key = frozenset(rc.cue_uid for rc in group)
        cached = self._composite_cache.get(key)
        if cached is not None:
            return cached
        rect = _union_rect([(rc.x, rc.y, rc.width, rc.height) for rc in group])
        x0, y0, w, h = rect
        canvas = np.zeros((h, w, 4), np.float32)
        for rc in sorted(group, key=lambda r: r.cue_uid):
            sub = canvas[rc.y - y0:rc.y - y0 + rc.height,
                         rc.x - x0:rc.x - x0 + rc.width]
            src = rc.bitmap.astype(np.float32) / 255.0
            sa = src[..., 3:4]
            # straight-alpha OVER straight-alpha
            da = sub[..., 3:4]
            oa = sa + da * (1 - sa)
            with np.errstate(divide='ignore', invalid='ignore'):
                rgb = np.where(oa > 0,
                               (src[..., :3] * sa + sub[..., :3] * da * (1 - sa))
                               / np.maximum(oa, 1e-6), 0)
            sub[..., :3] = rgb
            sub[..., 3:4] = oa
        obj = SupObject(x0, y0,
                        (np.clip(canvas, 0, 1) * 255 + 0.5).astype(np.uint8))
        self._composite_cache[key] = obj
        return obj


# --------------------------------------------------------------------------- #
# Quantization
# --------------------------------------------------------------------------- #

def quantize_event(objects: Sequence[SupObject]
                   ) -> Tuple[np.ndarray, List[np.ndarray]]:
    """
    Returns (palette_rgba uint8 [N,4] with index 0 transparent,
             per-object index arrays uint8 HxW).
    """
    pixels = [o.bitmap.reshape(-1, 4) for o in objects]
    allpix = np.concatenate(pixels, axis=0) if pixels else \
        np.zeros((0, 4), np.uint8)
    # transparent -> canonical (0,0,0,0)
    alpha0 = allpix[:, 3] == 0
    keys = allpix.astype(np.uint32)
    packed = (keys[:, 0] << 24) | (keys[:, 1] << 16) | (keys[:, 2] << 8) | keys[:, 3]
    packed[alpha0] = 0

    uniq, counts = np.unique(packed, return_counts=True)
    posterized = False
    if len(uniq) > 256:
        # posterize to 4 bits/channel (keeps AA smooth, collapses shades)
        q = (allpix & 0xF0)
        q = q | (q >> 4)
        q[alpha0] = 0
        keys = q.astype(np.uint32)
        packed = (keys[:, 0] << 24) | (keys[:, 1] << 16) | (keys[:, 2] << 8) | keys[:, 3]
        packed[allpix[:, 3] == 0] = 0
        uniq, counts = np.unique(packed, return_counts=True)
        posterized = True

    # order colors by frequency, transparent excluded
    order = np.argsort(-counts)
    uniq_sorted = uniq[order]
    uniq_sorted = uniq_sorted[uniq_sorted != 0]

    kept = uniq_sorted[:255]
    palette = np.zeros((len(kept) + 1, 4), np.uint8)
    palette[1:, 0] = (kept >> 24) & 0xFF
    palette[1:, 1] = (kept >> 16) & 0xFF
    palette[1:, 2] = (kept >> 8) & 0xFF
    palette[1:, 3] = kept & 0xFF

    # build LUT from packed key -> palette index
    lut = {0: 0}
    for i, k in enumerate(kept, start=1):
        lut[int(k)] = i
    dropped = uniq_sorted[255:]
    if len(dropped):
        # nearest-neighbour remap of rare colors
        drop_rgba = np.stack([(dropped >> 24) & 0xFF, (dropped >> 16) & 0xFF,
                              (dropped >> 8) & 0xFF, dropped & 0xFF],
                             axis=1).astype(np.int32)
        pal = palette[1:].astype(np.int32)
        for row, k in zip(drop_rgba, dropped):
            d = np.abs(pal - row).sum(axis=1)
            lut[int(k)] = int(np.argmin(d)) + 1

    # vectorized key -> index mapping (sorted searchsorted), built once
    keys_arr = np.fromiter(lut.keys(), dtype=np.uint32, count=len(lut))
    vals_arr = np.fromiter(lut.values(), dtype=np.uint8, count=len(lut))
    sortidx = np.argsort(keys_arr)
    ks, vs = keys_arr[sortidx], vals_arr[sortidx]

    out_indices = []
    pos = 0
    for o in objects:
        n = o.bitmap.shape[0] * o.bitmap.shape[1]
        chunk = packed[pos:pos + n]
        pos += n
        idx = np.searchsorted(ks, chunk)
        idx = np.clip(idx, 0, len(ks) - 1)
        mapped = np.where(ks[idx] == chunk, vs[idx], 0).astype(np.uint8)
        out_indices.append(mapped.reshape(o.bitmap.shape[:2]))
    return palette, out_indices


def _rgba_to_ycrcb(palette: np.ndarray, hd: bool) -> List[Tuple[int, int, int, int]]:
    out = []
    for r, g, b, a in palette.astype(np.float32):
        if hd:   # BT.709
            y = 16 + (r * 0.1826 + g * 0.6142 + b * 0.0620)
            cb = 128 + (r * -0.1006 - g * 0.3386 + b * 0.4392)
            cr = 128 + (r * 0.4392 - g * 0.3989 - b * 0.0403)
        else:    # BT.601
            y = 16 + (r * 0.2568 + g * 0.5041 + b * 0.0979)
            cb = 128 - r * 0.1482 - g * 0.2910 + b * 0.4392
            cr = 128 + r * 0.4392 - g * 0.3678 - b * 0.0714
        out.append((int(max(16, min(235, round(y)))),
                    int(max(16, min(240, round(cr)))),
                    int(max(16, min(240, round(cb)))),
                    int(a)))
    return out


# --------------------------------------------------------------------------- #
# RLE
# --------------------------------------------------------------------------- #

def rle_encode(indices: np.ndarray) -> bytes:
    """PGS run-length encoding (per line, 0x00 0x00 line terminator)."""
    out = bytearray()
    h, w = indices.shape
    for row in range(h):
        line = indices[row]
        # run boundaries
        if w == 0:
            out += b'\x00\x00'
            continue
        diff = np.flatnonzero(np.diff(line))
        starts = np.concatenate(([0], diff + 1))
        ends = np.concatenate((diff + 1, [w]))
        for s, e in zip(starts, ends):
            val = int(line[s])
            run = int(e - s)
            while run > 0:
                chunk = min(run, 16383)
                if val == 0:
                    if chunk <= 63:
                        out += bytes((0x00, chunk))
                    else:
                        out += bytes((0x00, 0x40 | (chunk >> 8), chunk & 0xFF))
                else:
                    if chunk <= 2 and chunk * 1 <= 2:
                        # literal pixels (short runs cheaper unencoded)
                        out += bytes((val,)) * chunk
                    elif chunk <= 63:
                        out += bytes((0x00, 0x80 | chunk, val))
                    else:
                        out += bytes((0x00, 0xC0 | (chunk >> 8), chunk & 0xFF, val))
                run -= chunk
        out += b'\x00\x00'
    return bytes(out)


# --------------------------------------------------------------------------- #
# Segment writer
# --------------------------------------------------------------------------- #

_FPS_CODES = [
    (Fraction(24000, 1001), 0x10), (Fraction(24, 1), 0x20),
    (Fraction(25, 1), 0x30), (Fraction(30000, 1001), 0x40),
    (Fraction(50, 1), 0x60), (Fraction(60000, 1001), 0x70),
]


def _fps_code(fps: Fraction) -> int:
    best, bestd = 0x10, 1e9
    for f, code in _FPS_CODES:
        d = abs(float(f) - float(fps))
        if d < bestd:
            best, bestd = code, d
    return best


class SupWriter:
    def __init__(self, canvas_w: int, canvas_h: int, fps: Fraction):
        self.w = canvas_w
        self.h = canvas_h
        self.fps = fps
        self.fps_code = _fps_code(fps)
        self.comp_n = 0

    # -- low level ------------------------------------------------------ #
    @staticmethod
    def _packet(pts: int, seg_type: int, payload: bytes) -> bytes:
        return b'PG' + struct.pack('>IIBH', pts & 0xFFFFFFFF, 0,
                                   seg_type, len(payload)) + payload

    def _pcs(self, pts: int, objects: List[Tuple[int, SupObject]],
             state: int = 0x80) -> bytes:
        d = struct.pack('>HHBHB', self.w, self.h, self.fps_code,
                        self.comp_n & 0xFFFF, state)
        d += struct.pack('>BBB', 0x00, 0, len(objects))
        for win_id, obj in objects:
            d += struct.pack('>HBBHH', win_id, win_id, 0x00,
                             obj.x, obj.y)
        return self._packet(pts, 0x16, d)

    def _wds(self, pts: int, windows: List[Tuple[int, int, int, int, int]]
             ) -> bytes:
        d = struct.pack('>B', len(windows))
        for wid, x, y, w, h in windows:
            d += struct.pack('>BHHHH', wid, x, y, w, h)
        return self._packet(pts, 0x17, d)

    def _pds(self, pts: int, palette_ycrcb) -> bytes:
        d = struct.pack('>BB', 0, 0)
        for i, (y, cr, cb, a) in enumerate(palette_ycrcb):
            if i == 0:
                continue                      # entry 0 = transparent default
            d += struct.pack('>BBBBB', i, y, cr, cb, a)
        return self._packet(pts, 0x14, d)

    def _ods(self, pts: int, obj_id: int, w: int, h: int, rle: bytes
             ) -> List[bytes]:
        packets = []
        total = len(rle)
        max_chunk = 65515 - 11
        offset = 0
        while offset < total or offset == 0:
            chunk = rle[offset:offset + max_chunk]
            first = offset == 0
            last = offset + len(chunk) >= total
            flag = (0x80 if first else 0) | (0x40 if last else 0)
            d = struct.pack('>HBB', obj_id, 0, flag)
            if first:
                full = total + 4
                d += struct.pack('>BH', (full >> 16) & 0xFF, full & 0xFFFF)
                d += struct.pack('>HH', w, h)
            d += chunk
            packets.append(self._packet(pts, 0x15, d))
            offset += len(chunk)
            if last:
                break
        return packets

    def _end(self, pts: int) -> bytes:
        return self._packet(pts, 0x80, b'')

    # -- high level ------------------------------------------------------ #
    def write(self, events: List[SupEvent], path: str,
              progress: Optional[Callable[[int, int, str], None]] = None,
              cancel: Optional[Callable[[], bool]] = None) -> bool:
        hd = self.h >= 600
        with open(path, 'wb') as f:
            prev_windows: List[Tuple[int, int, int, int, int]] = []
            prev_end: Optional[float] = None
            n = len(events)
            for i, ev in enumerate(events):
                if cancel and cancel():
                    return False
                # gap before this event? clear previous display
                if prev_end is not None and ev.start_ms > prev_end + 0.5 \
                        and prev_windows:
                    self._write_clear(f, prev_end, prev_windows)
                    prev_windows = []

                pts = int(round(ev.start_ms * 90))
                palette, indices = quantize_event(ev.objects)
                pal_ycc = _rgba_to_ycrcb(palette, hd)

                objs = list(enumerate(ev.objects))
                windows = [(wid, o.x, o.y,
                            o.bitmap.shape[1], o.bitmap.shape[0])
                           for wid, o in objs]
                self.comp_n += 1
                f.write(self._pcs(pts, objs, state=0x80))
                f.write(self._wds(pts, windows))
                f.write(self._pds(pts, pal_ycc))
                for wid, o in objs:
                    rle = rle_encode(indices[wid])
                    for pkt in self._ods(pts, wid,
                                         o.bitmap.shape[1], o.bitmap.shape[0],
                                         rle):
                        f.write(pkt)
                f.write(self._end(pts))
                prev_windows = windows
                prev_end = ev.end_ms
                if progress and (i % 10 == 0 or i == n - 1):
                    progress(i + 1, n, f"Encoding PGS {i + 1}/{n}")
            # final clear
            if prev_end is not None and prev_windows:
                self._write_clear(f, prev_end, prev_windows)
        return True

    def _write_clear(self, f, at_ms: float,
                     windows: List[Tuple[int, int, int, int, int]]):
        pts = int(round(at_ms * 90))
        self.comp_n += 1
        d = struct.pack('>HHBHB', self.w, self.h, self.fps_code,
                        self.comp_n & 0xFFFF, 0x80)
        d += struct.pack('>BBB', 0x00, 0, 0)
        f.write(self._packet(pts, 0x16, d))
        f.write(self._wds(pts, windows))
        f.write(self._end(pts))


# --------------------------------------------------------------------------- #

def write_sup_file(renders: List[TimedRender], path: str,
                   canvas_w: int, canvas_h: int,
                   fps: Fraction,
                   retime: Optional[RetimePlan] = None,
                   progress: Optional[Callable[[int, int, str], None]] = None,
                   cancel: Optional[Callable[[], bool]] = None) -> bool:
    """Convenience: timeline + write in one call."""
    tb = TimelineBuilder(canvas_w, canvas_h)
    events = tb.build(renders, retime=retime, snap_fps=fps)
    writer = SupWriter(canvas_w, canvas_h, fps)
    return writer.write(events, path, progress=progress, cancel=cancel)
