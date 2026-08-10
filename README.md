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
python run_gui.py                       # same GUI, plain-script launcher
                                        # (point IDE run configs here)
python -m ttml2pgs render sub.ja.ttml --video movie.mkv --mux
python -m ttml2pgs convert sub.vtt -o sub.srt
python -m ttml2pgs inspect sub.ttml     # dump styles/regions/cues
python -m ttml2pgs preview sub.ttml -n 5 -o out/   # cue PNGs
```

`ffmpeg`/`ffprobe` (probing, preview frames) and `mkvmerge` (remuxing)
should be on PATH. For the embedded preview player, install **libmpv**
(`libmpv2`/`mpv` package on Linux; `libmpv-2.dll` on Windows, folder
configurable in Settings) — it gives HDR-correct playback; without it
the preview falls back to Qt Multimedia.

## Launching it like an installed app

Two options, by how often you're still updating the code:

**A. Shortcut into your existing environment (recommended while
iterating).** One command, no build step, and every `git pull` /
PyCharm edit is live the next time you double-click:

```bash
python make_shortcut.py               # Desktop shortcut
python make_shortcut.py --start-menu  # + Start Menu entry (Windows)
```

Run it from the interpreter/venv PyCharm uses (PyCharm's Terminal tab
is already there). On Windows the shortcut targets `pythonw.exe`, so
no console window appears; it carries the app icon and the right
working directory. On Linux it installs a `.desktop` launcher.

**B. Fully standalone build (no Python needed to run).** For when the
app is in a good state and you want a self-contained folder you can
keep outside the repo:

```bash
pip install pyinstaller
pyinstaller ttml2pgs.spec
```

→ `dist/TTML2PGS/TTML2PGS.exe` (windowed, icon included) — pin a
shortcut to it anywhere. `ffmpeg`/`mkvmerge` are still found on PATH
(or drop them next to the exe), and libmpv stays optional via
Preferences → Player. Remember this snapshot doesn't update with the
repo: rebuild after pulling changes.

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
* **v1-matched typography defaults.** Base font size is 4.5 % of the
  content height (what v1's HTML body used — `em`/`%` sizes chain from
  it), the Japanese fallback stack is v1's Chrome order with Yu Gothic
  *Medium* preferred (Chromium's Windows default; plain Regular is
  wire-thin), JIS2004 glyph forms are requested, and a light **stem
  darkening** reproduces Chrome's heavier rasterization — tunable per
  language via "Stroke weight boost" (0 = off; default 3, calibrated
  against v1; up to 10 for heavier text without going bold).

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

### Merge mode
* **Merge selected…** in the Files pane combines two languages per
  episode into ONE subtitle (e.g. Japanese dialogue + English forced
  signs). Highlight any file of each episode; every open file of those
  episodes is grouped by filename stem, and you pick the **primary**
  and **secondary** language once for the whole batch (forced tracks
  are distinct options; choices missing from any episode are greyed
  out; fewer than two common options aborts with a warning).
* The merged row shows `Episode01.jp.vtt | Episode01.en.forced.vtt`,
  speaks the primary language (initials, mux tag, Default profile) and
  writes `Episode01.ja+en.forced.sup` by default. "Close unused"
  (remembered) closes the leftovers.
* Lines keep their SOURCE language, so per-language Text style
  overrides still apply per line; secondary styles/regions get a
  `.lang` suffix; secondary initials survive as a style on its cues.
* The cue pane grows a **language filter** and **Snap timestamps…**
  for merged files: secondary cue edges snap to the nearest primary
  cue boundary within a threshold (default 0.5 s) — edges with no
  boundary in range stay put, inverted/zero-length results are
  prevented.

### The queue (rebuilt)
* **Added ≠ started.** The Sources pane *adds* files to the queue; the
  queue panel's **Render all / Render selected** (or per-item context
  menu) actually starts them. Pause checkpoints the running job between
  cues; Resume continues only work you started — jobs sitting in
  "added" never render behind your back.
* Jobs are grouped **per target video**; a group's mux starts as soon as
  *its* renders finish — a crash on episode 12 no longer costs you the
  eleven finished episodes. Unstarted jobs visibly hold their group's
  mux ("waiting — N not started"); cancel or start them to release it.
* Add a second subtitle for an already-queued video (even mid-render) and
  the group's mux waits for it.
* Queue **external `.sup` files** for mux-only.
* A failed mux stays failed (with the error shown) instead of silently
  retrying — re-run it via **Retry mux** on the group. Before muxing,
  the embedded player releases the video file, and if something else
  still holds it locked, the mux lands as `*.muxed.mkv` next to the
  original rather than failing the batch.
* **MakeMKV-style checkboxes**: every video and every subtitle row has
  one. "Render all" (and group start) arm only checked jobs whose
  video is checked too — uncheck a row and it sits out of batch
  starts. Right-click a video → Select/Unselect all subtitles.
* Selection is kind-constrained: shift-selecting video rows never
  grabs their subtitles, and shift-selecting subtitles stays inside
  one video's group — no more erratic mixed selections.
* The tree updates **in place** — multi-selection, the shift-click
  anchor and scroll position survive live progress updates. **Del**
  removes the selection; the right-click menu adapts to it (bulk
  Start/Pause/Resume-Retry/Cancel/Remove on multi-select), right-click
  on empty space gets queue-wide options (start all, expand/collapse,
  move-to-subs). Columns resize individually. Group rows aggregate
  their children — `2/3 · rendering`, live render %, then `mux 45%` —
  so collapsed groups stay readable. Per-group **mux** and **replace
  original** toggles live in the group's context menu.
* Closing the app clears fully-finished groups from the queue;
  everything else comes back in its last state (done stays done,
  failed stays failed with its error) — finished work never re-runs.
* Pause / resume / cancel / retry / reorder at queue, group and job
  level; pausing checkpoints between cues and resumes without redoing
  finished cues.
* The queue lives in a **left-side dock** by default (drag it to any
  edge); opening it widens the window instead of crushing the panes,
  screen space permitting.
* Queue state persists to disk; after a crash, finished `.sup`s are
  detected and only missing work re-runs.

### The UI (rebuilt)
* **Sources pane** — open many subtitles, auto-matched videos (stem
  matching, language/flag tokens ignored), probed resolution/fps/HDR,
  conform indicator, per-file offset and output name. Per-session edits
  persist until you close the app; restore-on-launch included.
* **Cue pane** — filter by text/region, edit times/region/text inline,
  add/duplicate/delete cues, enable checkboxes, and Time tools (shift
  all/selected/after; manual fps conform with explained presets). A
  **Style column** shows each cue's named styles (italic *default* =
  defers to Initials, ✎ = has inline overrides) and is editable —
  region/style edits on a multi-selection apply to every selected cue.
  The collapsible **Selected cue** pane beneath shows the cue's text
  with visible **style tokens** — `⟦Style1 … Style1⟧`, like TTML spans
  made draggable. Highlight text and *Add style* (or **B**/**I**) to
  wrap it; drag tokens to move where styling starts/ends (overlaps
  auto-normalize into nested spans); deleting a token removes its
  partner and keeps the text. Only existing styles can be applied, and
  any edit that would corrupt the structure reverts with an
  explanation. **Furigana is directly editable**: ruby shows as
  `ルビ▸ 漢字(かんじ) ◂ルビ` with the base and reading as plain text —
  edit either, delete the `(reading)` to drop the ruby, or select text
  and hit ルビ to add new furigana (identical for TTML/VTT/SRT since
  ruby structure is normalized at load). Tate-chū-yoko and other
  complex blocks stay as solid chips. Cue-level (`<p>`) style refs are
  editable on the line above.
* **Preview** — an **embedded video player** (SubtitleEdit-style):
  plays the bound video with live subtitle overlays kept in sync —
  overlapping cues included, rendered by the same engine that feeds the
  .sup. Uses **mpv (libmpv)** when installed — correct HDR→SDR tone
  mapping, wide codec support, hardware decode — with QtMultimedia as
  the automatic fallback (Linux: `apt install libmpv2`; Windows: drop
  `libmpv-2.dll` in a folder and set it under Preferences → Player).
  Selecting a cue seeks to its first frame, paused.
  Falls back automatically to stills mode (matte AR guides + ffmpeg
  frame extraction with HDR tone-map) when the platform lacks a codec.
  **Show regions** outlines every region with its name in distinct
  stable colors (stills and player mode). Pop-out window locked 1:1 to
  the output pixel size — works from player mode too (pauses playback,
  shows the extracted frame behind the cue). "Open in player" hands the
  bound video to your desktop player (MPC-BE / MPC-HC / VLC / mpv
  presets, PATH + install-folder detection, or pick the exe) seeked to
  the selected cue for real-playback sync checking.
* **Settings pane** — **per-language global override tabs** (Japanese can
  run 5.2vh while English runs 4.5vh in the same batch) including
  **auto-color** (on by default): per *target video*, HDR episodes get
  the HDR color/alpha and SDR episodes the SDR one — detected
  automatically for every queued video (metadata + Dolby Vision binary
  scan), with v1's preset palette (SDR White / SDR Yellow / HDR Grey /
  OLED-safe) selectable per row. Plus layout/canvas policy (video dims,
  force 16:9, content-AR override, safe-area padding — padding insets
  region positions only and **never scales text**), post-processing
  toggles, and live **Styles / Regions / Initial** editors with
  add/rename/delete (renames cascade through every cue reference).
* **Preferences window** (menu bar → Preferences, `Ctrl+,`) —
  * **Default profiles**: fallback "initials" per language — applied
    only where the subtitle file specifies nothing at all (no inline
    styling, no named styles, no initials), so a bare SRT gets your
    chosen look while authored files stay untouched. The **Default**
    profile covers every subtitle; add a **language profile** (`ja`,
    `zh-Hant`…) and it is used *instead of* Default for files in that
    language.
  * **Player**: embedded engine (mpv / Qt Multimedia), libmpv folder,
    external player exe + args.
  * **Performance**: render worker processes (0 = auto).
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
    pipeline.py         doc → .sup orchestration (parallel, pause/cancel/resume)
    jobqueue.py         video-grouped queue engine
    video.py            ffprobe, HDR detect, matching, remux
    exporters.py        TTML/VTT/SRT writers
    project.py          .t2p project format
  ui/                   PyQt6 app (panes described above)
tests/
  test_core.py          96 tests: parsers, layout, PGS bytes, queue, round-trips
  samples/              TTML (ruby/vertical/emphasis), VTT (regions/styles), SRT
```

## Known limitations
* No bidi/RTL shaping yet (Arabic/Hebrew subtitles).
* `rubyReserve`, `textOrientation: sideways/upright` overrides, and
  `line`-number (non-percent) positioning use sensible approximations.
* Embedded playback depends on platform codecs (Windows Media
  Foundation / GStreamer); files it can't decode automatically fall
  back to stills + external-player hand-off.
* Cue rendering runs across a **process pool** by default (cores − 1,
  cap 8; configurable under Preferences → Performance, `--workers` on
  the CLI). Output is byte-identical to a single-process render; jobs
  under 16 cues stay in-process since worker startup would dominate.
  The remaining serial parts are PGS encode + mux.
