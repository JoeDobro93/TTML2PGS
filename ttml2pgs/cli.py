"""
Command-line interface.

    python -m ttml2pgs render input.ttml [-o out.sup] [--video movie.mkv] ...
    python -m ttml2pgs convert input.ttml -o out.vtt
    python -m ttml2pgs inspect input.vtt
    python -m ttml2pgs preview input.ttml -n 3 -o preview_dir/

Also proves the whole pipeline headless (no GUI required).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from fractions import Fraction


def _fps(text: str) -> Fraction:
    from .core.timing import normalize_fps
    if '/' in text:
        n, d = text.split('/')
        return Fraction(int(n), int(d))
    return normalize_fps(float(text))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='ttml2pgs',
                                 description='Fast TTML/VTT/SRT → PGS renderer')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_r = sub.add_parser('render', help='render subtitle to .sup')
    p_r.add_argument('input')
    p_r.add_argument('-o', '--output', help='output .sup path')
    p_r.add_argument('--video', help='target video (canvas/fps/mux target)')
    p_r.add_argument('--fps', type=_fps, default=None,
                     help='target fps (e.g. 23.976 or 24000/1001)')
    p_r.add_argument('--src-fps', type=_fps, default=None,
                     help='override source fps for conform')
    p_r.add_argument('--offset', type=float, default=0.0,
                     help='global offset in ms')
    p_r.add_argument('--canvas', default=None,
                     help='canvas size WxH (default 1920x1080)')
    p_r.add_argument('--font-size', default=None,
                     help='override font size (e.g. 4.5vh)')
    p_r.add_argument('--color', default=None, help='override text color')
    p_r.add_argument('--lang', default=None, help='force language code')
    p_r.add_argument('--mux', action='store_true',
                     help='remux into --video when done')
    p_r.add_argument('--workers', type=int, default=0,
                     help='render processes (0 = auto, 1 = sequential)')

    p_c = sub.add_parser('convert', help='convert between text formats')
    p_c.add_argument('input')
    p_c.add_argument('-o', '--output', required=True,
                     help='output path (.ttml/.vtt/.srt/.t2p)')

    p_i = sub.add_parser('inspect', help='dump parsed document summary')
    p_i.add_argument('input')

    p_p = sub.add_parser('preview', help='render cue PNGs')
    p_p.add_argument('input')
    p_p.add_argument('-o', '--outdir', default='preview')
    p_p.add_argument('-n', '--count', type=int, default=5)

    args = ap.parse_args(argv)
    if args.cmd == 'render':
        return cmd_render(args)
    if args.cmd == 'convert':
        return cmd_convert(args)
    if args.cmd == 'inspect':
        return cmd_inspect(args)
    if args.cmd == 'preview':
        return cmd_preview(args)
    return 1


def cmd_render(args) -> int:
    from .core.parsers import load_subtitle
    from .core.overrides import OverrideSet
    from .core.pipeline import RenderPipeline, RenderSettings
    from .core.timing import suggest_conform
    from .core.units import Dim
    from .core.colors import parse_color
    from .core.video import probe_video, remux, SubTrack

    doc = load_subtitle(args.input)
    if args.lang:
        doc.language = args.lang
        for c in doc.cues:
            c.lang = args.lang
    print(f"Parsed {len(doc.cues)} cues "
          f"[{doc.source_format}, lang={doc.language}, fps={doc.fps}]")

    video_res = None
    target_fps = args.fps
    vinfo = None
    if args.video:
        vinfo = probe_video(args.video)
        if vinfo:
            video_res = vinfo.resolution
            if target_fps is None:
                target_fps = vinfo.fps
            print(f"Video: {vinfo.width}x{vinfo.height} @ {vinfo.fps} "
                  f"{'HDR' if vinfo.is_hdr else 'SDR'}")
    if target_fps is None:
        target_fps = doc.fps or Fraction(24000, 1001)

    src_fps = args.src_fps or doc.fps
    retime = suggest_conform(src_fps, target_fps)
    if retime:
        print(f"Retime: {retime.description}")
    if args.offset:
        from .core.timing import RetimePlan
        retime = retime or RetimePlan()
        retime.offset_ms += args.offset

    overrides = OverrideSet()
    if args.canvas:
        w, h = args.canvas.lower().split('x')
        overrides.layout.use_video_dims = True
        video_res = (int(w), int(h))
        overrides.layout.scale_to_hd = False
    so = overrides.by_lang['']
    if args.font_size:
        so.override_font_size = True
        so.font_size = Dim.parse(args.font_size) or so.font_size
    if args.color:
        so.override_color = True
        so.color = parse_color(args.color) or so.color

    out = args.output
    if not out:
        base = os.path.splitext(args.input)[0]
        out = f"{base}.sup"

    settings = RenderSettings(out_path=out, video_res=video_res,
                              target_fps=target_fps, retime=retime,
                              workers=args.workers)
    pipe = RenderPipeline(doc, settings, overrides)

    t0 = time.time()
    last = ['']

    def progress(cur, total, msg):
        if msg != last[0]:
            sys.stdout.write(f"\r{msg}    ")
            sys.stdout.flush()
            last[0] = msg

    result = pipe.run(progress=progress)
    print(f"\nWrote {result} in {time.time() - t0:.1f}s")

    if args.mux and args.video:
        print("Muxing...")
        ok, res = remux(args.video,
                        [SubTrack(path=result, lang=doc.language)],
                        progress=lambda c, t, m: None)
        print(f"Mux: {'OK → ' + res if ok else 'FAILED: ' + res}")
        return 0 if ok else 2
    return 0


def cmd_convert(args) -> int:
    from .core.parsers import load_subtitle
    from .core.exporters import export_srt, export_ttml, export_vtt
    from .core.project import save_project

    doc = load_subtitle(args.input)
    ext = os.path.splitext(args.output)[1].lower()
    if ext in ('.ttml', '.dfxp', '.xml'):
        text = export_ttml(doc)
    elif ext in ('.vtt', '.webvtt'):
        text = export_vtt(doc)
    elif ext == '.srt':
        text = export_srt(doc)
    elif ext == '.t2p':
        save_project(args.output, doc)
        print(f"Wrote {args.output}")
        return 0
    else:
        print(f"Unsupported output format: {ext}", file=sys.stderr)
        return 1
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f"Wrote {args.output} ({len(doc.cues)} cues)")
    return 0


def cmd_inspect(args) -> int:
    from .core.parsers import load_subtitle
    doc = load_subtitle(args.input)
    print(f"format:   {doc.source_format}")
    print(f"language: {doc.language}")
    print(f"px space: {doc.px_width}x{doc.px_height}")
    print(f"fps:      {doc.fps}")
    print(f"styles:   {len(doc.styles)} → {', '.join(doc.styles) or '-'}")
    print(f"regions:  {len(doc.regions)}")
    for rid, r in doc.regions.items():
        wm = r.style.writing_mode or 'lrtb'
        print(f"  {rid}: x={r.x}/{r.x_edge} y={r.y}/{r.y_edge} "
              f"w={r.width} h={r.height} wm={wm}"
              f"{' (derived)' if r.derived else ''}")
    print(f"cues:     {len(doc.cues)}")
    for c in doc.sorted_cues()[:12]:
        text = c.plain_text().replace('\n', '⏎')
        if len(text) > 60:
            text = text[:57] + '…'
        print(f"  [{c.begin_ms / 1000:8.3f}-{c.end_ms / 1000:8.3f}] "
              f"{c.region_id or '-':12} {text}")
    if len(doc.cues) > 12:
        print(f"  … {len(doc.cues) - 12} more")
    return 0


def cmd_preview(args) -> int:
    import numpy as np
    from PIL import Image
    from .core.parsers import load_subtitle
    from .core.overrides import OverrideSet
    from .core.renderer import CueRenderer, compute_canvas

    doc = load_subtitle(args.input)
    canvas = compute_canvas(None, OverrideSet().layout)
    r = CueRenderer(doc, canvas)
    os.makedirs(args.outdir, exist_ok=True)
    for i, cue in enumerate(doc.sorted_cues()[:args.count]):
        rc = r.render_cue(cue)
        if rc is None:
            continue
        bg = np.full((canvas.height, canvas.width, 4), (40, 40, 40, 255),
                     np.uint8)
        a = rc.bitmap[..., 3:4].astype(np.float32) / 255
        sub = bg[rc.y:rc.y + rc.height, rc.x:rc.x + rc.width].astype(np.float32)
        sub[..., :3] = rc.bitmap[..., :3] * a + sub[..., :3] * (1 - a)
        bg[rc.y:rc.y + rc.height, rc.x:rc.x + rc.width] = sub.astype(np.uint8)
        out = os.path.join(args.outdir, f"cue{i:04d}.png")
        Image.fromarray(bg).save(out)
        print(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
