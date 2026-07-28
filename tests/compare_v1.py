"""
v1 ↔ v2 positioning comparison harness.

Renders the same cues through BOTH pipelines:
  v1: core.ingest + core.render (HTML/CSS) → headless Chromium screenshot
  v2: ttml2pgs direct rasterizer
…with normalized styling (same font size override, outline on, shadow
off) so ink-bbox differences reflect *positioning*, not font taste.

Outputs, per cue: ink bounding boxes, anchor deltas, and an overlay PNG
(v1 = red channel, v2 = green channel → matched areas read yellow).

Usage:  python tests/compare_v1.py <subtitle files...> [--out DIR]
        (defaults to the bundled samples)
"""

import argparse
import glob as globmod
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --- v1 imports (legacy app at repo root) ---------------------------------- #
from core.ingest import TTMLIngester, WebVTTIngester          # noqa: E402
from core.render import HtmlRenderer                          # noqa: E402

# --- v2 imports ------------------------------------------------------------ #
from ttml2pgs.core.parsers import load_subtitle               # noqa: E402
from ttml2pgs.core.overrides import OverrideSet               # noqa: E402
from ttml2pgs.core.renderer import CueRenderer, compute_canvas  # noqa: E402
from ttml2pgs.core.units import Dim                           # noqa: E402

W, H = 1920, 1080


def find_chromium():
    for pat in ('/opt/pw-browsers/chromium-*/chrome-linux/chrome',
                '/opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell'):
        hits = sorted(globmod.glob(pat))
        if hits:
            return hits[-1]
    return None


class V1Renderer:
    """v1 HTML → Chromium screenshot."""

    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        exe = find_chromium()
        self.browser = self._pw.chromium.launch(
            executable_path=exe, args=['--force-device-scale-factor=1'])
        self.page = self.browser.new_page(
            viewport={'width': W, 'height': H})

    def close(self):
        self.browser.close()
        self._pw.stop()

    def render(self, html: str) -> np.ndarray:
        self.page.set_content(html)
        try:
            self.page.evaluate('document.fonts.ready')
        except Exception:
            pass
        png = self.page.screenshot(omit_background=True)
        import io
        img = Image.open(io.BytesIO(png)).convert('RGBA')
        return np.asarray(img)


def v1_project(path):
    if path.lower().endswith(('.vtt', '.webvtt')):
        return WebVTTIngester().parse(path)
    return TTMLIngester().parse(path)


def v1_html_renderer(project):
    return HtmlRenderer(
        project, content_resolution=(W, H),
        override_font_size=True, global_font_size=4.5,
        global_font_size_unit='vh',
        override_outline=True, global_outline_enabled=True,
        global_outline_color='#000000', global_outline_width=3.0,
        global_outline_unit='px',
        override_shadow=True, global_shadow_enabled=False)


def v2_overrides():
    ov = OverrideSet()
    so = ov.by_lang['']
    so.override_font_size = True
    so.font_size = Dim(4.5, 'vh')
    so.override_outline = True
    so.outline_enabled = True
    so.outline_width = Dim(3.0, 'px')
    so.outline_color = (0, 0, 0, 255)
    so.override_shadow = True
    so.shadow_enabled = False
    return ov


def v2_frame(doc, renderer, cue) -> np.ndarray:
    frame = np.zeros((H, W, 4), np.uint8)
    rc = renderer.render_cue(cue)
    if rc is not None:
        a = rc.bitmap[..., 3:4].astype(np.float32) / 255.0
        sub = frame[rc.y:rc.y + rc.height, rc.x:rc.x + rc.width]
        blended = rc.bitmap[..., :3] * a + sub[..., :3] * (1 - a)
        alpha = rc.bitmap[..., 3:4].astype(np.float32) + \
            sub[..., 3:4].astype(np.float32) * (1 - a)
        frame[rc.y:rc.y + rc.height, rc.x:rc.x + rc.width, :3] = blended
        frame[rc.y:rc.y + rc.height, rc.x:rc.x + rc.width, 3:] = alpha
    return frame


def bbox(arr: np.ndarray):
    a = arr[..., 3]
    ys, xs = np.nonzero(a > 8)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def overlay(v1: np.ndarray, v2: np.ndarray) -> Image.Image:
    out = np.zeros((H, W, 3), np.uint8)
    out[..., 0] = v1[..., 3]                    # v1 = red
    out[..., 1] = v2[..., 3]                    # v2 = green
    bg = np.full((H, W, 3), 24, np.uint8)
    mask = (out.max(axis=2, keepdims=True) > 0)
    return Image.fromarray(np.where(mask, out, bg))


def match_cues(v1_cues, v2_cues):
    """Pair cues by (rounded start, plain text) — robust to ordering."""
    def v1_text(c):
        return ''.join((f.ruby_base or '') + f.text if f.is_ruby else f.text
                       for f in c.fragments).strip()
    unused = list(range(len(v2_cues)))
    pairs = []
    for c1 in v1_cues:
        best = None
        for i in unused:
            c2 = v2_cues[i]
            if abs(c2.begin_ms - c1.start_ms) < 3.0:
                t1 = v1_text(c1).replace(' ', '').replace('\n', '')
                t2 = c2.plain_text().replace(' ', '').replace('\n', '')
                if t1 and (t1 in t2 or t2 in t1 or t1 == t2):
                    best = i
                    break
        if best is not None:
            unused.remove(best)
            pairs.append((c1, v2_cues[best]))
    return pairs


def pick_interesting(doc, limit=4):
    """A bottom cue, a top/upper cue, a vertical cue, a ruby cue."""
    picks = []

    def add(c):
        if c is not None and c not in picks:
            picks.append(c)

    cues = doc.sorted_cues()
    add(next((c for c in cues if not doc.get_region(c).is_vertical()), None))
    add(next((c for c in cues if doc.get_region(c).is_vertical()), None))

    def has_ruby(c):
        chain = [(c.style_refs, c.inline_style)]

        def walk(n, ch):
            ch2 = ch + [(n.style_refs, n.inline_style)]
            comp = doc.resolve_style(ch2, doc.get_region(c))
            if comp.ruby == 'container':
                return True
            return any(walk(k, ch2) for k in n.children if k.kind == 'span')
        return walk(c.root, chain)
    add(next((c for c in cues if has_ruby(c)), None))
    add(next((c for c in cues if '\n' in c.plain_text()), None))
    return picks[:limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='*', default=[])
    ap.add_argument('--out', default='compare_out')
    args = ap.parse_args()
    files = args.files or [
        os.path.join(ROOT, 'tests', 'samples', 'netflix_ja.ttml'),
        os.path.join(ROOT, 'tests', 'samples', 'styled.vtt'),
    ]
    os.makedirs(args.out, exist_ok=True)

    chrome = V1Renderer()
    rows = []
    try:
        for path in files:
            name = os.path.splitext(os.path.basename(path))[0][:24]
            print(f"=== {name} ===")
            p1 = v1_project(path)
            r1 = v1_html_renderer(p1)
            doc = load_subtitle(path)
            canvas = compute_canvas((W, H), v2_overrides().layout)
            r2 = CueRenderer(doc, canvas, v2_overrides())

            pairs = match_cues(p1.body.cues, doc.sorted_cues())
            wanted = pick_interesting(doc)
            pairs = [(a, b) for a, b in pairs if b in wanted]
            for idx, (c1, c2) in enumerate(pairs):
                img1 = chrome.render(r1.render_cue_to_html(c1))
                img2 = v2_frame(doc, r2, c2)
                b1, b2 = bbox(img1), bbox(img2)
                if b1 is None or b2 is None:
                    print(f"  [{idx}] EMPTY render (v1={b1} v2={b2}) "
                          f"{c2.plain_text()[:28]!r}")
                    continue
                cx1, cx2 = (b1[0] + b1[2]) / 2, (b2[0] + b2[2]) / 2
                label = (c2.plain_text().replace('\n', '⏎'))[:26]
                vert = doc.get_region(c2).is_vertical()
                rows.append({
                    'file': name, 'cue': label, 'vertical': vert,
                    'v1': b1, 'v2': b2,
                    'd_cx': round(cx2 - cx1, 1),
                    'd_top': b2[1] - b1[1],
                    'd_bottom': b2[3] - b1[3],
                    'd_left': b2[0] - b1[0],
                    'd_right': b2[2] - b1[2],
                })
                ov = overlay(img1, img2)
                ov.save(os.path.join(
                    args.out, f"{name}_{idx}_{'v' if vert else 'h'}.png"))
                print(f"  [{idx}] {'V' if vert else 'H'} {label!r}")
                print(f"       v1 bbox {b1}  v2 bbox {b2}")
                print(f"       Δcenter_x={cx2 - cx1:+.1f} Δtop={b2[1] - b1[1]:+d} "
                      f"Δbottom={b2[3] - b1[3]:+d} Δleft={b2[0] - b1[0]:+d} "
                      f"Δright={b2[2] - b1[2]:+d}")
    finally:
        chrome.close()

    print("\nSummary (px deltas v2 - v1):")
    for r in rows:
        anchor = ('right/top' if r['vertical'] else 'center/bottom')
        key = (f"Δright={r['d_right']:+d} Δtop={r['d_top']:+d}"
               if r['vertical'] else
               f"Δcx={r['d_cx']:+.1f} Δbottom={r['d_bottom']:+d}")
        print(f"  {r['file'][:22]:22} {'V' if r['vertical'] else 'H'} "
              f"{key:28} ({anchor})  {r['cue']!r}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
