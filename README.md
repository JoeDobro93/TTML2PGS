# TTML2PGS 2

Convert TTML / WebVTT / SRT subtitles to image-based PGS (`.sup`) tracks and
remux them into your videos — with browser-quality CJK typography (ruby,
vertical text, proper Japanese-vs-Chinese glyphs) at native-code speed.

**Version 2 is a ground-up rewrite.** The v1 pipeline rendered every cue as
HTML in headless Chrome and screenshotted it; v2 rasterizes glyphs directly
with HarfBuzz + FreeType + NumPy. Same visual results (paint-order outlines,
soft shadows, justified ruby), several times faster, no browser dependency,
and far less RAM. The legacy app is still in `core/`, `ui/`, `main.py` for
reference; the new app lives entirely in `ttml2pgs/`.

## Running

```bash
pip install -r requirements.txt

python -m ttml2pgs                      # GUI
python -m ttml2pgs render sub.ja.ttml --video movie.mkv --mux
python -m ttml2pgs convert sub.vtt -o sub.srt
python -m ttml2pgs inspect sub.ttml     # dump styles/regions/cues
python -m ttml2pgs preview sub.ttml -n 5 -o out/   # cue PNGs
```

`ffmpeg`/`ffprobe` (probing, preview frames) and `mkvmerge` (remuxing)
should be on PATH.

## What v2 does

### Parsing — to one editable model
* **TTML1 / TTML2 / IMSC** per the 2018 W3C recommendations: referential
  styling chains, `<initial>`, region styling (including nested `<style>`),
  `tts:origin/extent/position`, SMPTE/media/offset/tick time expressions
  with `frameRate(Multiplier)`/`tickRate`/`subFrameRate`, cellResolution
  units, ruby (all roles), `textCombine` (tate-chū-yoko), `shear`,
  `textEmphasis` (bōten), `textOutline`, `textShadow`, `writingMode`,
  `multiRowAlign`, xml:space handling, the Netflix `Smpte24TimingAdjusted`
  quirk.
* **WebVTT**: `REGION` blocks, `STYLE ::cue` selectors (classes, tags,
  voices, lang), all cue settings (`vertical/line/position/size/align`
  with alignment suffixes), payload tags (`c i b u v lang ruby rt`,
  timestamps), and **region derivation** — cues sharing positional
  signatures collapse into shared regions (one region for a normal file,
  a second for vertical cues, etc.).
* **SRT**: tags, `<font color>`, `{\anX}` anchors → derived regions.
* Language detection from filename/metadata (nonstandard tags like
  ``xml:lang="jp"`` normalized); **auto-ruby** for Japanese VTT *and*
  SRT: an ASCII ``(`` introducing a kana reading marks ruby — the base
  runs back to the nearest space (removed as a marker) or the line
  start; full-width ``（）`` parentheticals are left as real text.
* Validated against real masters: Netflix (Annihilation, Django
  Unchained incl. the head-metadata ``Smpte24TimingAdjusted`` variant),
  Amazon ``.ttml2`` (Civil War, The Holdovers — ``vh`` units,
  ``fontShear``, CSS-style ``tts:position`` percentage-point offsets),
  Disney+ WebVTT (The French Dispatch — ``::cue`` classes, shear,
  tate-chū-yoko, explicit ruby).

Styles are kept as **references + inline styles** and resolved through the
full cascade at render time — so editing a named style updates every cue
that uses it, live (the v1 "baked at parse" flaw is gone).

### Rendering — direct to pixels
* Per-run font selection with **language-aware CJK fallback**: a `ja`
  document will never borrow unified-Han glyphs from a Chinese font when a
  Japanese font exists (and vice versa); localized family names
  (`游ゴシック` …) match; bold/italic pick real faces with synthetic
  fallback.
* HarfBuzz shaping (kerning, ligatures, `vert` features), line breaking
  with Japanese kinsoku rules, `multiRowAlign`, letter spacing.
* **Ruby**: over/under, 1-2-1 justification when the annotation is
  narrower than its base, centered with widening when longer; annotation
  size follows the author's explicit size or a 50 % default.
* **Vertical text** (`tbrl`/`tblr`): upright kana/kanji, rotated Latin
  runs, full-width digit conversion, tate-chū-yoko groups, ruby on the
  correct side, per-column ruby reserve.
* Effects: stroked outlines (round joins, painted under all fills),
  multi-shadow with blur/alpha, background boxes, shear/italics (slanting
  along the correct axis in vertical), emphasis dots/circles/sesame,
  underline/strike.
* Typographic substitutions (⸺ → ——, 〝〞 → curly quotes …) whenever the
  only font covering a rare character is a pan-unicode bitmap fallback.
* **Everything scales.** All lengths live as units (`%`, `vh/vw`, `c`,
  `em`, authored `px` rescaled from the document's declared pixel space),
  resolved against the output canvas — a 3px outline authored for 1080p
  renders as 6px on a 2160p canvas.

### PGS output
* Native `.sup` writer, **any canvas size** (odd sizes included), BT.709
  palettes for HD, lossless quantization when ≤255 colors with graceful
  posterize + nearest-remap fallback, numpy RLE.
* **Jitter-free overlaps**: each display set carries up to two composition
  objects/windows, so a cue that continues across an overlap boundary
  keeps a byte-identical bitmap at an identical position. Composites (>2
  overlapping boxes) are cached per cue-set so they're stable too.
* Frame-rate conform (23.976↔24↔25, PAL speedup, 30→29.97…) with
  automatic suggestion when the subtitle's declared fps mismatches the
  probed video fps — telecine pairs (29.97i from 23.976) correctly map to
  "no change". Manual conform lives in Cue pane → Time tools.

### The queue (rebuilt)
* Jobs are grouped **per target video**; a group's mux starts as soon as
  *its* renders finish — a crash on episode 12 no longer costs you the
  eleven finished episodes.
* Add a second subtitle for an already-queued video (even mid-render) and
  the group's mux waits for it.
* Queue **external `.sup` files** for mux-only.
* Pause / resume / cancel / retry / reorder at queue, group and job
  level; pausing checkpoints between cues and resumes without redoing
  finished cues.
* Queue state persists to disk; after a crash, finished `.sup`s are
  detected and only missing work re-runs.

### The UI (rebuilt)
* **Sources pane** — open many subtitles, auto-matched videos (stem
  matching, language/flag tokens ignored), probed resolution/fps/HDR,
  conform indicator, per-file offset and output name. Per-session edits
  persist until you close the app; restore-on-launch included.
* **Cue pane** — filter by text/region, edit times/region/text inline,
  add/duplicate/delete cues, enable checkboxes, and Time tools (shift
  all/selected/after; manual fps conform with explained presets).
* **Preview** — an **embedded video player** (SubtitleEdit-style,
  QtMultimedia): plays the bound video with live subtitle overlays kept
  in sync — overlapping cues included, rendered by the same engine that
  feeds the .sup. Selecting a cue seeks to its first frame, paused.
  Falls back automatically to stills mode (matte AR guides + ffmpeg
  frame extraction with HDR tone-map) when the platform lacks a codec.
  Pop-out window locked 1:1 to the output pixel size (opening it pauses
  the embedded player); "open in external player at this cue" with
  MPC-BE / MPC-HC / VLC / mpv presets.
* **Settings pane** — **per-language global override tabs** (Japanese can
  run 5.2vh while English runs 4.5vh in the same batch) including
  **auto-color**: per *target video*, HDR episodes get the HDR
  color/alpha and SDR episodes the SDR one — detected automatically for
  every queued video (metadata + Dolby Vision binary scan), so series
  batches need no per-episode fiddling. Plus layout/canvas policy
  (video dims, force 16:9, content-AR override, safe-area padding),
  post-processing toggles, and live **Styles / Regions / Initial**
  editors for the active document.
* **Save/Load** native `.t2p` projects (lossless document + overrides +
  bindings); export TTML / WebVTT / SRT (lossy where the target format
  can't express a feature).

## Architecture

```
ttml2pgs/
  cli.py                render/convert/inspect/preview commands
  core/
    model.py            document model: styles (referenced), regions, cue span trees
    units.py, colors.py, timing.py
    parsers/            ttml.py, vtt.py, srt.py (+ detection)
    fonts.py            discovery, localized names, CJK-aware fallback
    shaping.py          HarfBuzz wrapper
    layout.py           lines, kinsoku, ruby, vertical, TCY, emphasis
    raster.py           FreeType glyphs, stroker outlines, shadows, compositing
    renderer.py         canvas/region resolution, cascade → pixels
    pgs.py              overlap timeline + SUP writer
    overrides.py        per-language override sets + layout options
    pipeline.py         doc → .sup orchestration (pause/cancel/resume)
    jobqueue.py         video-grouped queue engine
    video.py            ffprobe, HDR detect, matching, remux
    exporters.py        TTML/VTT/SRT writers
    project.py          .t2p project format
  ui/                   PyQt6 app (panes described above)
tests/
  test_core.py          35 tests: parsers, layout, PGS bytes, queue, round-trips
  samples/              TTML (ruby/vertical/emphasis), VTT (regions/styles), SRT
```

## Known limitations
* No bidi/RTL shaping yet (Arabic/Hebrew subtitles).
* `rubyReserve`, `textOrientation: sideways/upright` overrides, and
  `line`-number (non-percent) positioning use sensible approximations.
* Embedded playback depends on platform codecs (Windows Media
  Foundation / GStreamer); files it can't decode automatically fall
  back to stills + external-player hand-off.
* Rendering is single-process; a real 1372-cue Japanese feature (ruby,
  vertical, shear) renders + encodes in under two minutes on a modest
  CPU — several times the v1 Chrome pipeline; further parallelism is a
  straightforward future optimization.
