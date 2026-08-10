# TTML2PGS

Convert TTML, WebVTT and SRT subtitles to image-based PGS (`.sup`) subtitles
and remux them into your videos.

# What is this for?

Text subtitles render differently in every player — fonts get substituted,
ruby (furigana) is dropped, vertical text falls over, and CJK unified
characters can pick the wrong regional glyphs. PGS subtitles are images, so
they look exactly the same everywhere and play on devices like an Nvidia
Shield/Plex without transcoding.

TTML2PGS renders those images with a real text engine (HarfBuzz + FreeType):
ruby text, vertical layout, tate-chū-yoko, bōten emphasis, outlines, shadows
and per-language font selection all come out the way the subtitle author
intended. English files work great too, but Japanese subtitles are where it
really matters.

# Getting started

```bash
pip install -r requirements.txt
python run_gui.py                # the GUI (or: python -m ttml2pgs)
```

You'll also want on PATH:

* **ffmpeg / ffprobe** — video probing and preview frames
* **mkvmerge** (install MKVToolNix) — remuxing into MKVs

Optional: **libmpv** gives the embedded preview player correct HDR playback
(Linux: `apt install libmpv2`; Windows: drop `libmpv-2.dll` in a folder and
point Preferences → Player at it). Without it the player falls back to Qt
Multimedia. On Linux, install a Japanese font (e.g. Noto Sans CJK JP) for
Japanese output; Windows ships Yu Gothic.

**Desktop shortcut:** `python make_shortcut.py` puts a TTML2PGS shortcut on
your Desktop (add `--start-menu` on Windows for a Start Menu entry too).

**Standalone exe:** `pip install pyinstaller`, then `python make_exe.py` —
the app lands in `dist/TTML2PGS/TTML2PGS.exe` and doesn't need Python.

There's also a CLI for scripting:
`python -m ttml2pgs render sub.ja.ttml --video movie.mkv --mux`
(plus `convert`, `inspect` and `preview` commands).

# Basic overview

## Files panel (bottom left)

**Add subtitles… / Add folder…** loads files and auto-matches a video that
shares the subtitle's name (language tags like `.ja` or `.forced` are
ignored when matching). ffprobe fills in resolution, frame rate and HDR;
double-click the Video cell to bind a video manually. Offset, source/target
fps and the output name are editable per file — a conform indicator shows
when a frame-rate conversion will be applied (e.g. PAL 25 → 23.976).

**Merge selected…** combines two languages per episode into ONE subtitle
(e.g. Japanese dialogue + English forced signs). Highlight any file of each
episode you want merged — files are grouped by episode name, and you pick
the primary + secondary language once for the whole batch (forced tracks
count as their own option). Merged lines keep their source language, so
per-language style overrides still apply to each line, and the output is
named like `Episode01.ja+en.sup` with a `ja-en` track name you can edit in
the queue. An optional timestamp snap aligns the secondary language's cue
edges to the primary's while merging.

**Queue external .sup…** adds an already-rendered `.sup` for mux-only.
**Only checked cues** renders just the cues you've ticked in the cue list.
**Add to queue** (or Render → Add all, `Ctrl+F5`) stages files in the render
queue — nothing starts rendering until you start it from the queue panel.

## Cues panel (top left)

Every cue of the selected file. Click a row to preview it. Times, text,
region and style are edited right in the table — Region and Style are
dropdowns (single click opens them), and edits on a multi-selection apply
to every selected cue, as does right-click → Change region / Change style.
Checkboxes enable/disable individual cues; Add / Duplicate / Delete manage
rows.

Overlapping cues get their timestamps highlighted — click the Start/End
header to filter overlapping cues, the Region/Style headers to filter by
value, or use the text box and (for merged files) the language dropdown.

**Time tools…** shifts timestamps by a `HH:MM:SS.mmm` amount — type digits
to overwrite the timecode Subtitle-Edit-style — earlier or later, for all
cues, the selection, or the selection plus everything after it. The same
dialog does manual frame-rate conforms with the common rates explained.

**Align overlaps…** snaps timestamps for two-language files: secondary cue
edges within a threshold (default 0.5 s) jump to the nearest primary cue
boundary, and same-language collisions are resolved by region position
priority so stacked lines don't fight.

The **Selected cue** pane underneath shows the cue's text with its styling
as draggable tokens (`⟦Style1 … Style1⟧`): highlight text and Add style /
**B** / **I** to wrap it, drag a token to move where styling starts or
ends, delete a token to unwrap. Furigana is plain text here — edit
`漢字(かんじ)` directly, remove the `(reading)` to drop ruby, or select
text and hit **Ruby** to add it.

When you switch between loaded files, each one remembers where you were.

## Preview panel (top right)

Shows the selected cue exactly as it will render. With a video bound,
**Embedded player** plays the video with live subtitle overlays in sync —
the same engine that writes the `.sup` draws the overlay, overlaps
included, and selecting a cue seeks to it. Without a decodable video it
shows extracted stills (HDR tone-mapped) or a plain background.

* **Show regions** outlines every region with its name.
* **Pop out (1:1)** opens a floating window locked to true output pixel
  size — drag to move, right-click to close (it closes itself when any
  other window opens).
* **Open in player** hands the video to your desktop player (MPC-BE /
  MPC-HC / VLC / mpv) seeked to the selected cue.
* Aspect-ratio mattes only visualize letter/pillar boxing — the `.sup`
  canvas itself follows the bound video (any resolution works, not just
  1080p).

## Settings panel (bottom right)

### Global Overrides

Per-language styling applied on top of every file in the batch: the
**Default** tab covers everything, and a tab per language (auto-created
for open files, off until you enable it) lets Japanese run different
settings than English in the same render. Each tab has collapsible Font /
Color / Outline / Spacing sections — font family, size (use `vh` units,
4–5 is a good range), weight boost for heavier strokes without going bold,
outline and shadow, letter and line spacing, and safe-area padding (moves
regions inward without scaling the text).

**Auto-color** (on by default) picks the subtitle color per *target
video*: SDR videos get the SDR color, HDR videos a dimmer HDR-safe one
(detected automatically, Dolby Vision included), with presets — SDR White,
SDR Yellow, HDR Grey, HDR Grey (OLED safe) — selectable per row.

### Layout

Canvas policy: size the `.sup` to the video, **Force 16:9 layout** for
subtitle files that already position within a 16:9 window, or **Override
content aspect ratio** for videos with baked-in black bars so subtitles
stay inside the picture. Note `vh` font sizes scale with the content
height, so aspect overrides affect text size accordingly — the preview
shows the result.

### Styles / Regions / Initials

Live editors for the selected file's named styles, regions and document
defaults — add, rename (references update everywhere) and delete. Regions
list a position hint (bottom / top / vertical right…) and X/Y anchors can
be nudged precisely without affecting font scaling. Edits show up in the
preview immediately.

## Render queue (left dock)

Adding files stages them; **▶ Render all / ▶ Render selected** actually
starts work. Every video and subtitle row has a MakeMKV-style checkbox —
unchecked rows sit out of batch starts. Jobs group per target video, and a
video remuxes as soon as *its* subtitles finish, so one bad episode never
costs you the finished ones. Pause checkpoints mid-job and Resume picks up
without redoing finished cues; a failed mux shows its error and waits for
**Retry mux**; if something has the video file locked, the mux lands next
to it as `*.muxed.mkv` instead of failing. Per-group toggles cover mux,
replace-original and the subtitle track name. Queue state survives
restarts — finished work never re-runs.

## Good to know

* **Undo/redo everywhere:** `Ctrl+Z` / `Ctrl+Shift+Z` reverts cue edits,
  style/region edits, override changes and per-file settings — undoing a
  change made in another file jumps to that file first.
* **Projects:** File → Save project (`.t2p`) keeps the document, your
  global overrides and video bindings losslessly; opening one offers to
  restore the overrides it was saved with.
* **Exports:** File → Export writes TTML / WebVTT / SRT with your current
  overrides baked in, as close as each format allows.
* **Preferences** (`Ctrl+,`): per-language **Default profiles** (styling
  used only where a file specifies nothing, so bare SRTs get your look
  while authored files stay untouched), player engine and external player
  setup, and render worker count (rendering runs on multiple cores).

# Known limitations

* No right-to-left/bidi shaping yet (Arabic, Hebrew).
* Embedded playback depends on platform codecs — undecodable files fall
  back to stills and the external-player hand-off.
* A few TTML niceties (`rubyReserve`, sideways text orientation) use
  close approximations.
