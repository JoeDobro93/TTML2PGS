# What is this for?
This is a Python based program to convert TTML and WebVTT subtitles to image based PGS .sup subtitles and remux them into target video files.

Significantly slower than SubtitleEdit .sup exports as it uses CSS/HTML formatting which allows ruby text, vertical subtitles, and proper scaling that SubtitleEdit's quicker rendering cannot handle. For English subtitles this is usually not a big deal, but for Japanese subtitles or other languages that may use these features more, this will allow more accurate subtitles to be generated.

The PGS subtitles will make the subtitles appear consistently regarless of the player and ensure no issues with CJK unified characters. Ideal for files in Plex on an Nvidia Sheild or other device that suports PGS without transcoding.

# Basic Overview
## Files panel (bottom left)
Adding a subtitle file will automatically try to match a video file that shares the same name as the subtitle.

If a match is found, ffmpeg will populate the program with video metadata.
Similar in Add Folder except it will add all subtitles and matching videos in a folder.

Offset (ms) can be manually added and applied on render.

Run (Current) will only render the selected subtitle file while Run (Batch) starts rendering everything loaded in. More should be able to be added to the cue while a batch render is running, but I haven't tested this much so I recommend waiting for a batch to finish completely before clearing files and running another batch to avoid potential crashes or bugs.

"Render Only Selected Cues" must be selected first if you uncheck any cues and want to only render the selected cues, otherwise all cues will be rendered regardless of selection.

## Cues Panel (top left)
Shows all the cues of the currently selected subtitle file loaded in.

Clicking on any row will send a preview to the preview panel.

Very basical filtering feature as well as a "region" filter to help make sure that all regions are appearing as intended and can be useful for checking targeted adjustments to positioning.

## Preview Panel (top right)
Shows the current cue as it will be rendered on a plain background color. May try to add a video player in the future, but for now this is a quick way to check that subtitle formatting is working as intended.

Aspect ratio changes here do not affect the subtitles themselves, but create pillar/letter boxing to show where the subtitles will appear if the destination is not 16:9. All .sup files are rendered as 1920x1080 images (Blu-Ray standard), but will still work as intended in most players even if the video itself is a different resolution/aspect ratio. I will likely update the program to allow custom resolution .sup files that don't conform the the blu-0ray standard.

"Show Regions" overlays the outline and name of every region on the preview, each in a distinct, readable, randomly-assigned colour (hues are spread evenly so they don't bunch up). Labels are placed at the corner opposite the region's text alignment to reduce overlap, and regions sharing the exact same box are nudged a pixel apart so both outlines stay visible. This is handy for verifying layout and for positioning newly added regions.

## Settings Panel (bottom right)
### Global Overrides
These override any checked attribute for the subtitle file that may be present in the source subtitle file and apply to all subtitles being rendered in the batch.

Auto-Color is to help when batching a combination of HDR and SDR files as pure white subtitles may result in blindingly bright subtitles in HDR while grey in SDR may be dim. This also applies an opacity of .9 alpha to HDR files. This is determined by the target video.

For font size, I recommend using "vh" units (video height) which sets the font size based on the percentage of the video height. Somewhere between 4 and 5 is generally a good value.

"Force 16:9 Layout" will ignore the aspect ratio of the target video file and render as if it were 16:9. This is useful for any subtitle file that already takes into account alternate aspect ratios and positions the subtitles in a 16:9 window (It's Always Sunny .vtt subtitles in the first 5 seasons sourced from Disney+ are an exapmle of this). You can use the preview to check if this is necessary. If it is not, you may end up with subtiltes going into the black bars which may be distracting.

"Override Content Aspect Ratio" will set a custom aspect ratio to position the subtitles within. This is useful for video files where black bars are part of the video frame itself.

Note that changing the aspect ratio will affect scaling of subtitle font size if "vh" is selected and the aspect ratio results in letterboxing.

For "Remux into Video on Completion" I recommend having MKVToolNix installed. remuxer.py points to "mkvmerge.exe" in this install for this program. Without it, it uses ffmpeg to remux, but I have had some issues with this working as intended.

Uncheck Clean-up Temp Files only if you need to check on how images are rendered intividually if there is a bug. These files are generated in the folder the subtitle/video files are contained in.

### Initials/Styles/Regions
Edits in these tabs now apply live to the preview and to the rendered output. The cascade is:

`system defaults -> Initials -> Style(s) -> the cue's own (inline) overrides`

Each attribute row has a checkbox. **Checked** means the attribute is set and overrides whatever it inherits; **unchecked** means it is inherited (left to the level above). Unchecking a row now correctly falls back to inheritance instead of leaving a stale value baked in.

#### Initials
The project-level default style for the subtitle file (the baseline every cue starts from). For VTT files, which have no `<initial>` block, an empty editable Initials set is provided so you can still set a project-wide default. Editing an Initial attribute updates every cue that doesn't override it.

#### Styles
Named styles that override the Initials when applied to a cue (e.g. a default of regular white text plus an "italic" style yields italic white text). Use **Add** to create a new named style: it starts fully unchecked (inheriting everything from the Initials) but each row is pre-filled with a sensible default so checking it gives a usable value. **Rename** and **Delete** keep any cues that referenced the style in sync. (Assigning styles to individual cues from the UI is still planned; adding/naming them now means they're ready for that.)

#### Regions
Useful for adjusting regions in a specific subtitle file. "X" and "Y" adjustments are used to position the region precisely and won't affect font size scaling like aspect ratio changes do. Even when working with the aspect ratio overrides on, this can still be useful if you want a different distance to the edges.

Use **Add** to create a new region (it defaults to a sensible bottom-centred caption box). New, renamed, or deleted regions immediately show up in the Cues pane's region filter and the per-cue region selector, as well as in the "Show Regions" preview overlay.
