"""
core/render.py

HTML Generator for Subtitle Cues.
Converts the Unified Data Model (Cue -> Region -> Fragments) into
browser-ready HTML/CSS.

FINAL TUNING:
1.  **Font Scale:** Adjusted to 0.91 (up from 0.85) to better match the source video size
    without being "huge" like the raw 1.0 render.
2.  **Ruby Gap:** Added `transform: translateY(15%)` to <rt> tags. This physically moves
    the annotation text closer to the base, overcoming line-height limitations.
"""

import html
import math
import re
from typing import List, Tuple, Optional
from .models import SubtitleProject, Cue, Fragment, Region, Style


class HtmlRenderer:
    # Scale factor tuning
    FONT_SCALE_FACTOR = 1.0

    def __init__(self, project: SubtitleProject, content_resolution=None, debug_mode: bool = False,
                 # Overrides
                 override_font_size: bool = False, global_font_size: float = 4.5, global_font_size_unit: str = "vh",
                 override_color: bool = False, global_color: str = "#FFFFFF",
                 override_outline: bool = False, global_outline_enabled: bool = True,
                 global_outline_color: str = "#000000", global_outline_width: float = 3.0,
                 global_outline_unit: str = "px",
                 override_shadow: bool = False, global_shadow_enabled: bool = True,
                 global_shadow_color: str = "#000000", global_shadow_offset_x: float = 4.0,
                 global_shadow_offset_y: float = 4.0, global_shadow_blur: float = 2.0):

        self.project = project
        self.content_resolution = content_resolution
        self.debug_mode = debug_mode

        # Store Overrides
        self.override_font_size = override_font_size
        self.global_font_size = global_font_size
        self.global_font_size_unit = global_font_size_unit

        self.override_color = override_color
        self.global_color = global_color

        self.override_outline = override_outline
        self.global_outline_enabled = global_outline_enabled
        self.global_outline_color = global_outline_color
        self.global_outline_width = global_outline_width
        self.global_outline_unit = global_outline_unit

        self.override_shadow = override_shadow
        self.global_shadow_enabled = global_shadow_enabled
        self.global_shadow_color = global_shadow_color
        self.global_shadow_offset_x = global_shadow_offset_x
        self.global_shadow_offset_y = global_shadow_offset_y
        self.global_shadow_blur = global_shadow_blur

    def render_cue_to_html(self, cue: Cue, preview_bg: Optional[str] = None) -> str:
        # --- LIVE PREVIEW ---
        # Re-apply current Named Styles from the Project to the fragments.
        # This allows the Settings Pane to update colors/fonts instantly.
        if self.project and self.project.styles:
            for frag in cue.fragments:
                # Iterate over the IDs that created this style (e.g. "Default", "Italic")
                for sid in frag.applied_style_ids:
                    if sid in self.project.styles:
                        # Fetch the CURRENT definition from the Settings Pane/Project
                        current_def = self.project.styles[sid]
                        # Re-merge it onto the fragment's calculated style
                        frag.calculated_style = frag.calculated_style.merge_from(current_def)
        # --------------------------

        region_style = self._generate_region_inline_style(cue.region) if cue.region else self._generate_default_region()

        # Pass the cue so we can check for specific text alignment styles
        p_style = self._generate_paragraph_style(cue, cue.region)
        content_html = self._generate_fragments_html(cue.fragments, cue.region)

        if self.content_resolution:
            W, H = self.content_resolution
            #print(f"[DEBUG] Render: Using Output Resolution {W}x{H}")
        else:
            W = self.project.width
            H = self.project.height

        fps = f"{self.project.fps_num}/{self.project.fps_den}"
        lang = self.project.language or "ja"

        # If preview_bg is passed, use it. Else fall back to debug/transparent.
        if preview_bg:
            print("setting preview color")
            player_bg = preview_bg
        else:
            #print("setting bg transparent")
            player_bg = "#4b0082" if self.debug_mode else "transparent"

        return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="ttml-resolution" content="{self.project.width}x{self.project.height}">
<meta name="ttml-frame-rate" content="{self.project.fps_num}/{self.project.fps_den}">
<style>
  :root {{
    --vw: 1vw; --vh: 1vh; 
    --pvw: 1vw; --pvh: 1vh; 
  }}

  html, body {{ 
    margin: 0; padding: 0; 
    width: 100vw; height: 100vh; 
    overflow: hidden; 
    background: transparent;
    /* FIX: Set Base Font Size to 5% of Player Height.
       - 'em' and '%' units will multiply against this (e.g. 1.5em = 7.5vh).
       - 'vh', 'px', and 'rh' units defined on cues will IGNORE this and set their own size. */
    font-size: calc(var(--pvh) * 4.5);
  }}

  .frame {{ position: relative; width: 100%; height: 100%; }}

  #player-box {{ 
    position: absolute; 
    background: {player_bg}; 
    pointer-events: none; 
  }}

  .region {{
    position: absolute; 
    overflow: visible;
    display: flex;
    flex-direction: column;
  }}

  p {{
    margin: 0;
    white-space: pre-wrap;
    line-break: strict;
    word-break: normal;
    line-height: 1.25; /* Forces tighter, cinematic line spacing */
  }}

  /* --- TEXT STYLING --- */

  .cap {{
    position: relative;
    display: inline-block;
    line-height: normal;
    paint-order: stroke fill;
    stroke-linejoin: round;
    -webkit-stroke-linejoin: round;

    --sw: 0;
    --sc: transparent;
  }}

  .cap:lang(ja), :lang(ja) .cap {{
    font-variant-east-asian: jis04;
  }}

  .char {{ display: inline-block; }}

  /* RUBY CONFIGURATION */
  ruby {{
    ruby-position: over;
    ruby-align: space-between;
    paint-order: stroke fill;
    line-height: 1.0; 
  }}
  
  rb {{
    display: ruby-base;
    position: relative;
    z-index: 1; 
  }}

  rt {{
    font-size: 0.5em;
    /* text-align: center; */
    paint-order: stroke fill;
    position: relative;
    z-index: 0; /* Sit behind the base text */
    top: var(--rt-top, 0);
    
    line-height: var(--rt-lh, 1.0);
    
    /* Boost outline thickness for tiny text to prevent jagged/steppy edges */
    --sw-scale: 1.5;

    /* NOTE: The text-shadow property is generated dynamically in Python 
       and injected inline, so we don't define it here anymore. */
  }}

</style>
</head>
<body>
<div class="frame">
  <div id="player-box">
    <div class="region" style="{region_style}">
      <p style="{p_style}">{content_html}</p>
    </div>
  </div>
</div>

<script>
(function(){{
  const meta = document.querySelector('meta[name="ttml-resolution"]');
  let W = {W}, H = {H};

  function layout(){{
    const ww = window.innerWidth, wh = window.innerHeight;
    const r = W / H; 
    let pw = ww, ph = Math.round(pw / r);
    let left = 0, top = 0;

    if (ph > wh) {{
      ph = wh; 
      pw = Math.round(ph * r);
      left = (ww - pw) / 2; 
    }} else {{
      top = (wh - ph) / 2; 
    }}

    document.documentElement.style.setProperty('--vw', (ww/100)+'px');
    document.documentElement.style.setProperty('--vh', (wh/100)+'px');
    document.documentElement.style.setProperty('--pvw', (pw/100)+'px');
    document.documentElement.style.setProperty('--pvh', (ph/100)+'px');

    const player = document.getElementById('player-box');
    player.style.width = pw + 'px';
    player.style.height = ph + 'px';
    player.style.left = left + 'px';
    player.style.top = top + 'px';
  }}

  window.__ttml_layout = layout;
  window.addEventListener('resize', layout);
  layout();
}})();
</script>
</body>
</html>
"""

    def _generate_region_inline_style(self, region: Region) -> str:
        css = []
        transforms = []

        # 1. Geometry
        # This prevents crashes when width is None (VTT shrink-wrap mode).
        if region.width is not None:
            w = self._convert_unit(region.width, region.width_unit, axis='x')
            css.append(f"width:{w}")

        if region.height is not None:
            h = self._convert_unit(region.height, region.height_unit, axis='y')
            css.append(f"height:{h}")

        # 2. Positioning
        x_val = region.x
        y_val = region.y
        x_css = self._convert_unit(x_val, region.x_unit, axis='x')
        y_css = self._convert_unit(y_val, region.y_unit, axis='y')

        # --- HORIZONTAL ---
        if region.x_edge == "left":
            css.append(f"left:{x_css}")
            # if x_val == 50.0: transforms.append("translateX(-50%)")
        elif region.x_edge == "right":
            css.append(f"right:{x_css}")
            if x_val == 50.0: transforms.append("translateX(50%)")
        elif region.x_edge == "center":
            # LOGIC FIX:
            # If x is 0 (TTML Bug), force 50%.
            # If x is 50 (VTT Standard), use 50%.
            # We treat x_val as the absolute position, not an offset margin.
            val = 50.0 if x_val == 0 else x_val

            # Use specific unit if available, otherwise % for the 50.0 override
            unit = region.x_unit if x_val != 0 else "%"

            css.append(f"left: {val}{unit}")
            transforms.append("translateX(-50%)")

        # --- VERTICAL ---
        if region.y_edge == "top":
            css.append(f"top:{y_css}")
            # if y_val == 50.0: transforms.append("translateY(-50%)")
        elif region.y_edge == "bottom":
            css.append(f"bottom:{y_css}")
            if y_val == 50.0: transforms.append("translateY(50%)")
        elif region.y_edge == "center":
            # LOGIC FIX: Same as horizontal.
            val = 50.0 if y_val == 0 else y_val
            unit = region.y_unit if y_val != 0 else "%"

            css.append(f"top: {val}{unit}")
            transforms.append("translateY(-50%)")

        if transforms:
            css.append(f"transform: {' '.join(transforms)}")

        # 3. Alignment & Writing Mode
        if region.is_vertical:
            css.append("writing-mode: vertical-rl")
            css.append("text-orientation: mixed")

            # --- VERTICAL FLEX MAPPING ---
            # Main Axis (justify-content) -> Horizontal (Right-to-Left)
            # Cross Axis (align-items)    -> Vertical (Top-to-Bottom)

            # Horizontal Alignment (Main Axis)
            if region.align_horizontal in ["left", "start"]:
                css.append("justify-content: flex-end")
            elif region.align_horizontal in ["right", "end"]:
                css.append("justify-content: flex-start")
            else:
                css.append("justify-content: center")

            # Vertical Alignment (Cross Axis)
            if region.align_vertical in ["bottom", "after"]:
                css.append("align-items: flex-end")
            elif region.align_vertical == "center":
                css.append("align-items: center")
            else:
                # Default Top
                css.append("align-items: flex-start")

        else:
            # --- HORIZONTAL FLEX MAPPING ---
            # Main Axis (justify-content) -> Vertical (Top-to-Bottom)
            # Cross Axis (align-items)    -> Horizontal (Left-to-Right)

            # Horizontal Alignment (Cross Axis)
            if region.align_horizontal in ["left", "start"]:
                css.append("align-items: flex-start")
            elif region.align_horizontal in ["right", "end"]:
                css.append("align-items: flex-end")
            else:
                css.append("align-items: center")

            # Vertical Alignment (Main Axis)
            if region.align_vertical == "top":
                css.append("justify-content: flex-start")
            elif region.align_vertical == "center":
                css.append("justify-content: center")
            else:
                css.append("justify-content: flex-end")  # Default Bottom

        if region.show_background == "always" and region.background_color:
            css.append(f"background-color: {region.background_color}")

        return "; ".join(css)

    def _generate_paragraph_style(self, cue: Cue, region: Region) -> str:
        css = []

        # 1. Determine Text Alignment
        # Priority: Specific Cue Style > Region Default
        align = None
        is_multi_row = False

        # Check the first fragment's style for text-align (inherited from P/Style)
        if cue.fragments and cue.fragments[0].calculated_style:
            s = cue.fragments[0].calculated_style

            # Check for Multi-Row Align (Center-Left Logic)
            if s.multi_row_align:
                align = s.multi_row_align
                is_multi_row = True
            else:
                align = s.text_align

        # If no style is set, use the Region's default alignment.
        # - Vertical regions store text alignment in 'align_vertical' (default: top)
        # - Horizontal regions store text alignment in 'align_horizontal' (default: center)
        if not align:
            if region.is_vertical:
                align = region.align_vertical
            else:
                align = region.align_horizontal

        # 2. Apply CSS
        # We normalize 'align' to specific CSS keywords for later logic
        css_align_value = "center"

        # 2. Apply CSS
        # We normalize 'align' to specific CSS keywords for later logic
        css_align_value = "center"

        if region.is_vertical:
            css.append("writing-mode: vertical-rl")
            css.append("text-orientation: mixed")
            if align in ["start", "top"]:
                css_align_value = "start"
            elif align in ["end", "bottom"]:
                css_align_value = "end"
            else:
                css_align_value = "center"

        else:
            css.append("writing-mode: horizontal-tb")
            if align in ["left", "start"]:
                css_align_value = "left"
            elif align in ["right", "end"]:
                css_align_value = "right"
            else:
                css_align_value = "center"

        css.append(f"text-align: {css_align_value}")

        # 3. Sizing
        # For VTT (Auto Regions), P should be auto so region shrinks (handled by width=None in Ingest)
        # This allows the box to be centered by the Region, while text inside is left-aligned.
        if is_multi_row or css_align_value != "center":
            css.append("width: fit-content")
            # Ensure it doesn't overflow the region
            css.append("max-width: 100%")
        elif region.width is not None:
            # TTML Fixed Regions: P fills the region
            css.append("width: 100%")
        else:
            # VTT Auto Regions: P is auto
            pass

        # 4. Spacing
        # Forces tighter, cinematic line spacing
        css.append("margin: 0")
        css.append("white-space: pre-wrap")
        css.append("line-break: strict")
        css.append("word-break: normal")
        css.append("line-height: 1.25")

        return "; ".join(css)

    def _generate_default_region(self) -> str:
        return "width:80%; height:20%; left:50%; top:90%; transform:translate(-50%, -50%); display:flex; flex-direction:column; align-items:center; justify-content:center;"

    def _generate_fragments_html(self, fragments: List[Fragment], region: Optional[Region]) -> str:
        parts = []
        is_vertical = region.is_vertical if region else False


        # Helper to apply Tate-chu-yoko (Upright Digits) in vertical mode, not used any more but might be good
        # in the future
        def fix_vert_digits(s: str) -> str:
            # Old code that kept numbers on one line and rotated them for vertical text.
            # Matches sequences of digits (e.g. "1", "15", "2024")
            # Wraps them in text-combine-upright to make them sit horizontally in the vertical line.
            # -webkit-text-combine is added for broader browser/player compatibility.
            return re.sub(
                r'(\d+)',
                r'<span style="text-combine-upright: all; -webkit-text-combine: horizontal">\1</span>',
                s
            )

        # FIX: Convert ASCII digits to Full-width (Zenkaku) for Vertical text.
        # This treats numbers as standard vertical characters (Upright),
        # eliminating the need for complex 'text-combine-upright' spans and
        # allowing standard vertical skew/italics to work perfectly.
        def to_full_width(s: str) -> str:
            return s.translate(str.maketrans("0123456789", "０１２３４５６７８９"))

        for frag in fragments:
            # 1. Prepare Text (Convert numbers if vertical)
            raw_text = frag.text
            if is_vertical:
                raw_text = to_full_width(raw_text)

            text = html.escape(raw_text)

            # FIX: Rotate numbers to be upright in vertical text. Used in older implementation
            # if is_vertical:
            #    text = fix_vert_digits(text)

            style_css, char_transform = self._style_to_css_and_transform(frag.calculated_style, is_vertical)

            if text == "\n":
                parts.append("<br>")
                continue

            if frag.is_ruby:
                ruby_base = html.escape(frag.ruby_base)

                if is_vertical:
                    ruby_base = fix_vert_digits(ruby_base)

                # Ensure semicolon exists
                base_style = style_css + ";" if style_css and not style_css.endswith(";") else style_css

                # --- CONFIGURATION ---
                # 1. Container Line-Height:
                #    Force the Base text box to be exactly 1.0 (snug).
                #    This removes the "Buffer" above the Kanji.
                container_lh = "line-height: 1.0;"

                # 2. RT Line-Height (--rt-lh):
                #    Force the Ruby text to be exactly 1.0 (snug relative to ITSELF).
                #    This stops it from inheriting the huge 60px height of the parent.
                #    We use "1.0" for Horizontal. For Vertical, we usually keep "1.0" or "normal".
                rt_lh_val = "1.0"

                # 3. RT Offset (--rt-top):
                #    We keep your visual nudge to handle Noto Sans ascenders.
                # NOTE: moving this to 1.0 from 0.0 pushes ruby text *down* however this doesn't work with Noto font
                #       so it doesn't really help since it just breaks fonts like Arial and does nothing for Noto
                rt_top_val = "0.0em"

                # Combine everything:
                # - line-height: 1.0  -> Clamps the base
                # - display: ruby     -> Enforces proper stacking
                # - --rt-lh: 1.0      -> Clamps the ruby text height
                # - --rt-top: 0.75em  -> Pushes ruby text down visually
                ruby_style = (f"{base_style} {container_lh} display: ruby; "
                              f"ruby-position: over; --rt-top: {rt_top_val}; --rt-lh: {rt_lh_val};")

                inner_content = f'<rb>{ruby_base}</rb><rt>{text}</rt>'

                if char_transform:
                    # Wrapper for Skew
                    parts.append(
                        f'<span class="cap" style="{char_transform}">'
                        f'<ruby style="{ruby_style}">{inner_content}</ruby>'
                        f'</span>'
                    )
                else:
                    # Standard
                    parts.append(
                        f'<ruby class="cap" style="{ruby_style}">{inner_content}</ruby>'
                    )

            else:
                if is_vertical and char_transform:
                    # Vertical Skew/Italics
                    # Since numbers are now full-width, we treat everything as standard vertical text.
                    # We iterate the raw unicode characters to ensure we don't break escaped entities (like &amp;)
                    chars_html = ""
                    for char in raw_text:
                        chars_html += f'<span class="char" style="{char_transform}">{html.escape(char)}</span>'
                    parts.append(f'<span class="cap" style="{style_css}">{chars_html}</span>')
                else:
                    # Standard Text
                    #parts.append(f'<span class="cap" style="{style_css}">{text}</span>')
                    sep = ";" if style_css and not style_css.endswith(";") else ""

                    parts.append(f'<span class="cap" style="{style_css}{sep} {char_transform}">{text}</span>')

        return "".join(parts)

    def _style_to_css_and_transform(self, style: Style, is_vertical: bool) -> Tuple[str, str]:
        css = []
        transform = ""

        if style.font_family:
            # TODO: implement font family override logic
            fonts = [f"'{f}'" if " " in f else f for f in style.font_family]
            css.append(f"font-family: {', '.join(fonts)}")

        # --- 1. FONT SIZE OVERRIDE ---
        target_size = self.global_font_size if self.override_font_size else style.font_size
        target_unit = self.global_font_size_unit if self.override_font_size else style.font_size_unit

        if target_size:
            scaled_size = target_size * self.FONT_SCALE_FACTOR
            val = self._convert_unit(scaled_size, target_unit, axis='y')
            css.append(f"font-size: {val}")

        if style.line_height:
            # TODO: implement override to set a minimum/maximum line height
            # If unit is empty (e.g. "1.2"), use raw value.
            if not style.line_height_unit:
                css.append(f"line-height: {style.line_height}")
            else:
                # Convert unit (e.g. "6.00rh" -> calc(...))
                val = self._convert_unit(style.line_height, style.line_height_unit, axis='y')
                css.append(f"line-height: {val}")

        # --- 2. COLOR OVERRIDE ---
        target_color = self.global_color if self.override_color else style.color
        if target_color: css.append(f"color: {target_color}")

        if style.font_weight: css.append(f"font-weight: {style.font_weight}")
        if style.font_style: css.append(f"font-style: {style.font_style}")

        # Skew
        angle = 0
        if style.skew_angle:
            angle = style.skew_angle
        elif style.font_style == 'italic':
            angle = 15

        if angle:
            if is_vertical:
                transform = f"transform: skewY({-angle}deg)"
            else:
                transform = f"transform: skewX({-angle}deg)"

        # --- HIGH-QUALITY SMOOTH OUTLINE (Forced Max Quality) ---
        shadows = []

        # Resolve Settings
        use_outline = self.global_outline_enabled if self.override_outline else style.outline_enabled
        o_width = self.global_outline_width if self.override_outline else style.outline_width
        o_color = self.global_outline_color if self.override_outline else style.outline_color
        o_unit = self.global_outline_unit if self.override_outline else (style.outline_unit or 'px')

        # Force 64 steps for everything.
        # This ensures even thin lines on small text (furigana) are buttery smooth.
        steps = 16
        # print(f"[RENDER] {steps} steps")

        if use_outline and o_width and o_color:
            css.append(f"--sw: {o_width}{o_unit}")
            css.append(f"--sc: {o_color}")

            for i in range(steps):
                angle = (2 * math.pi * i) / steps
                x = math.cos(angle)
                y = math.sin(angle)

                shadows.append(
                    f"calc(var(--sw) * var(--sw-scale, 1) * {x:.3f}) "
                    f"calc(var(--sw) * var(--sw-scale, 1) * {y:.3f}) "
                    f"0 var(--sc)"
                )

        # --- SHADOW ---
        use_shadow = self.global_shadow_enabled if self.override_shadow else style.shadow_enabled
        s_color = self.global_shadow_color if self.override_shadow else style.shadow_color
        s_off_x = self.global_shadow_offset_x if self.override_shadow else style.shadow_offset_x
        s_off_y = self.global_shadow_offset_y if self.override_shadow else style.shadow_offset_y
        s_blur = self.global_shadow_blur if self.override_shadow else style.shadow_blur
        s_unit = 'px' if self.override_shadow else (style.shadow_unit or 'px')

        if use_shadow and s_color:
            def to_css(val, unit):
                return f"{val}{unit}" if isinstance(val, (int, float)) else str(val)

            ox = to_css(s_off_x or 0, s_unit)
            oy = to_css(s_off_y or 0, s_unit)

            # Retrieve injected blur (default to 0 if missing)
            blur = to_css(style.shadow_offset_x or 0, s_unit)

            # Check if it's a raw VTT string (only if NOT overriding)
            if not self.override_shadow and " " in s_color:
                shadows.append(s_color)
            else:
                shadows.append(f"{ox} {oy} {blur} {s_color}")

        if shadows:
            css.append(f"text-shadow: {', '.join(shadows)}")

        if style.text_emphasis_style:
            col = style.text_emphasis_color or "currentcolor"
            css.append(f"text-emphasis: {style.text_emphasis_style} {col}")
            pos = style.text_emphasis_position or ("right" if is_vertical else "over")
            css.append(f"text-emphasis-position: {pos}")

        # TODO: Implement text-combine-upright (Tate-chu-yoko)
        # Currently, the ingest parser reads it into style.text_combine, but we are
        # ignoring it here because we handle vertical numbers via full-width conversion
        # in _generate_fragments_html.
        # if style.text_combine:
        #    css.append(f"text-combine-upright: {style.text_combine}")
        #    css.append("-webkit-text-combine: horizontal")

        return "; ".join(css), transform

    def _convert_unit(self, value: float, unit: str, axis: str = 'y') -> str:
        """
        Converts TTML units to CSS-compatible strings.
        """
        if not unit:
            # Default to px if no unit, but warn if value is suspicious
            return f"{value}px"

        u = unit.lower()

        # 1. Viewport Units (vh, vw, rh, rw)
        # We treat 'rh' (Root Height) as 'vh'
        if u in ['vh', 'rh']:
            return f"calc(var(--pvh) * {value})"
        elif u in ['vw', 'rw']:
            return f"calc(var(--pvw) * {value})"

        # 2. Percentage (%)
        elif u == '%':
            # HEURISTIC: TTML uses % ambiguously.
            # Case A: "100%" usually means 1em (relative to parent/cell).
            # Case B: "10%" usually means 10vh (screen height).
            # Threshold: 25% (A normal subtitle line is rarely > 25% of screen height)
            if value > 25.0:
                # Treat as scaling factor (100% = 1em)
                return f"{value}%"
            else:
                # Treat as viewport height (10% = 10vh)
                if axis == 'x': return f"calc(var(--pvw) * {value})"
                return f"calc(var(--pvh) * {value})"

        # 3. Em / Cell units (Relative)
        elif u == 'em':
            return f"{value}em"
        elif u == 'c':
            # 1c (Cell) is typically 1/15th of the screen height (~6.67%)
            return f"calc(var(--pvh) * {value * 6.67})"

        # 4. Pixels (px)
        elif u == 'px':
            # Convert absolute px to relative scaling based on Project Height.
            if self.project.height:
                pct = (value / self.project.height) * 100
                return f"calc(var(--pvh) * {pct:.4f})"
            return f"{value}px"

        # 5. Fallback / Warning
        else:
            print(f"WARNING: Unhandled unit '{u}' with value {value}. Passing through as px.")
            return f"{value}px"