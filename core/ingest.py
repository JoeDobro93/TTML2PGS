"""
core/ingest.py

Logic for parsing TTML (IMSC1/Netflix) and WebVTT files into the Unified Data Model.

Key Responsibilities:
1. XML/Text Parsing: Reading the raw file formats.
2. Inheritance Resolution: Recursively applying Initial -> Body -> Div -> P -> Span styles.
3. Attribute Mapping: Converting standard attributes (tts:color) and custom extensions to our Model.
4. Unit Parsing: ensuring '80%' and '40px' are stored distinctly.
5. Region Parsing: Handling both tts:origin (legacy) and tts:position (modern) with correct defaults.
6. Auto-Ruby (VTT Only): Handling flattened "Base(Furigana)" text patterns.
7. Default Management: Utilizing Style.get_system_defaults() as the baseline.
"""

import re
import xml.etree.ElementTree as ET
from typing import Optional, List, Dict, Any, Tuple
from .models import SubtitleProject, SubtitleBody, Cue, Fragment, Style, Region

# Global whitelist of valid language codes to prevent false positives (like "1080p")
VALID_LANG_CODES = {
    # Japanese
    "ja": "ja", "jp": "ja", "jpn": "ja", "ja-jp": "ja",
    # English
    "en": "en", "eng": "en",
    # French
    "fr": "fr", "fre": "fr", "fra": "fr",
    # German
    "de": "de", "deu": "de", "ger": "de",
    # Spanish
    "es": "es", "spa": "es",
    # Italian
    "it": "it", "ita": "it",
    # Portuguese
    "pt": "pt", "por": "pt",
    # Chinese
    "zh": "zh", "chi": "zh", "zho": "zh",
    # Korean
    "ko": "ko", "kor": "ko",
    # Russian
    "ru": "ru", "rus": "ru"
}

# Regex to split text by tags, capturing the delimiters.
# Example: "A<br>B" -> ['A', '<br>', 'B']
VTT_TAG_RE = re.compile(r'(<[^>]+>)')
# Regex for unit splitting (e.g. "10px" -> "10", "px")
UNIT_RE = re.compile(r'([0-9\.]+)([^0-9\.\s]+)?')

class TTMLIngester:
    """
    Parses TTML and IMSC1.1 XML files.
    Implements a recursive descent parser to handle style inheritance.
    Tracks the history of applied styles for debugging.
    """

    def parse(self, path: str) -> SubtitleProject:
        """
        Main entry point. Reads the file and returns a populated Project object.
        """
        tree = ET.parse(path)
        root = tree.getroot()

        project = SubtitleProject()
        self._parse_meta(root, project)

        # 1. Parse <head> definitions
        # This populates project.styles, project.regions, and project.initial_style
        self._parse_head(root, project)

        detected_lang = ""  # Default blank

        # check Filename
        detected_lang = self._detect_lang_from_filename(path)

        # Check Metadata (Fallback)
        if not detected_lang:
            # Check xml:lang attribute on the root <tt> element
            xml_lang = root.attrib.get('{http://www.w3.org/XML/1998/namespace}lang')
            if xml_lang:
                # Handle cases like "en-US" -> just "en"
                clean_lang = xml_lang.split('-')[0].lower()
                if clean_lang in VALID_LANG_CODES:
                    detected_lang = VALID_LANG_CODES[clean_lang]

        # 2. Pass to Defaults (If blank, it defaults to 'en' logic inside the model)
        base_style = Style.get_system_defaults(language=detected_lang)

        project.language = detected_lang if detected_lang else "en"

        # If the file provided <initial> tags, we merge them ON TOP of the system defaults.
        # This ensures that if the file defines color but not outline, we keep the default outline.
        if project.initial_style:
            base_style = base_style.merge_from(project.initial_style)

        # 3. Parse <body>
        body_node = self._find_child(root, "body")
        if body_node:
            # Capture attributes directly on the body tag
            b_style_id = self._get_attr(body_node, "style")
            b_region_id = self._get_attr(body_node, "region")
            project.body.style_id = b_style_id
            project.body.region_id = b_region_id

            current_ids = ["__ROOT__"]
            current_style = base_style

            if b_style_id and b_style_id in project.styles:
                current_style = current_style.merge_from(project.styles[b_style_id])
                current_ids.append(b_style_id)

            inline_body = Style(id="__BODY_INLINE__")
            self._map_attributes(body_node, inline_body)
            current_style = current_style.merge_from(inline_body)

            current_region = None
            if b_region_id and b_region_id in project.regions:
                current_region = project.regions[b_region_id]

            # Begin recursion into divs and paragraphs
            self._recurse_node(body_node, project, current_style, current_ids, current_region)

        return project

    def _detect_lang_from_filename(self, path: str) -> str:
        """
        Parses [filename].[lang].[ext].
        Validates against VALID_LANG_CODES to avoid false positives like '1080p'.
        """
        try:
            # Split by dots
            parts = path.lower().split('.')
            # Must have at least 3 parts: name.lang.ext
            if len(parts) >= 3:
                potential_code = parts[-2]  # Get second to last item
                if potential_code in VALID_LANG_CODES:
                    return VALID_LANG_CODES[potential_code]
        except Exception:
            pass
        return ""

    # --- RECURSION LOGIC ---
    def _recurse_node(self, element, project, parent_style, parent_ids, parent_region):
        # 1. Resolve Style
        node_style_id = self._get_attr(element, "style")
        current_style = parent_style
        current_ids = parent_ids.copy()

        if node_style_id and node_style_id in project.styles:
            current_style = current_style.merge_from(project.styles[node_style_id])
            current_ids.append(node_style_id)

        inline_s = Style(id="inline")
        self._map_attributes(element, inline_s)
        current_style = current_style.merge_from(inline_s)

        # 2. Resolve Region
        node_region_id = self._get_attr(element, "region")
        current_region = parent_region
        if node_region_id and node_region_id in project.regions:
            current_region = project.regions[node_region_id]

        tag = self._strip_ns(element.tag)

        if tag == "p":
            self._create_cue(element, project, current_style, current_ids, current_region)
        elif tag in ["div", "body"]:
            for child in element:
                self._recurse_node(child, project, current_style, current_ids, current_region)

    def _create_cue(self, p_node, project, cue_style, cue_ids, cue_region):
        start = self._get_attr(p_node, "begin")
        end = self._get_attr(p_node, "end")
        if not start or not end: return

        cue = Cue(
            start_ms=self._parse_time(start, project),
            end_ms=self._parse_time(end, project),
            region=cue_region
        )
        self._parse_fragments(p_node, cue.fragments, project, cue_style, cue_ids)
        project.body.cues.append(cue)

    def _parse_fragments(self, element, fragments, project, parent_style, parent_ids):
        # 1. Text
        if element.text:
            fragments.append(Fragment(
                text=element.text,
                calculated_style=parent_style,
                applied_style_ids=parent_ids
            ))

        # 2. Children
        for child in element:
            tag = self._strip_ns(child.tag)

            # Recalculate style for this child
            child_style = parent_style
            child_ids = parent_ids.copy()

            sid = self._get_attr(child, "style")
            if sid and sid in project.styles:
                child_style = child_style.merge_from(project.styles[sid])
                child_ids.append(sid)

            inline = Style(id="inline_child")
            self._map_attributes(child, inline)
            child_style = child_style.merge_from(inline)

            if tag == "br":
                fragments.append(Fragment(text="\n", calculated_style=child_style, applied_style_ids=child_ids))

            # RUBY: Standard XML <span tts:ruby="container"> parsing
            elif self._get_attr(child, "ruby") == "container" or child_style.ruby_role == "container":
                self._handle_ruby(child, fragments, child_style, child_ids, project)

            elif tag == "span":
                self._parse_fragments(child, fragments, project, child_style, child_ids)

            if child.tail:
                fragments.append(Fragment(
                    text=child.tail,
                    calculated_style=parent_style,
                    applied_style_ids=parent_ids
                ))

    def _handle_ruby(self, container, fragments, container_style, container_ids, project):
        base_text = ""
        ruby_text = ""
        for child in container:
            role = self._get_attr(child, "ruby")
            if not role:
                sid = self._get_attr(child, "style")
                if sid and sid in project.styles:
                    role = project.styles[sid].ruby_role
            if role == "base": base_text = child.text or ""
            elif role == "text": ruby_text = child.text or ""

        fragments.append(Fragment(
            text=ruby_text,
            ruby_base=base_text,
            is_ruby=True,
            calculated_style=container_style,
            applied_style_ids=container_ids
        ))

    # --- METADATA & HEADER PARSING ---
    def _parse_head(self, root, project):
        head = self._find_child(root, "head")
        if not head: return
        styling = self._find_child(head, "styling")
        if styling:
            project.initial_style = Style(id="__GLOBAL_INITIAL__")
            for node in styling:
                tag = self._strip_ns(node.tag)
                if tag == "initial": self._map_attributes(node, project.initial_style)
                elif tag == "style":
                    sid = self._get_attr(node, "id")
                    if sid:
                        s = Style(id=sid)
                        self._map_attributes(node, s)
                        project.styles[sid] = s
        layout = self._find_child(head, "layout")
        if layout:
            for node in layout:
                if self._strip_ns(node.tag) == "region": self._parse_region(node, project)

    def _parse_region(self, node, project):
        rid = self._get_attr(node, "id")
        if not rid: return
        r = Region(id=rid)

        wm = self._get_attr(node, "writingMode")
        if wm:
            r.writing_mode = wm
            if any(x in wm for x in ['tblr', 'tbrl', 'tb-rl', 'vertical']): r.is_vertical = True
            elif 'lr' in wm or 'horizontal' in wm: r.is_vertical = False
        sb = self._get_attr(node, "showBackground")
        if sb: r.show_background = sb
        bg = self._get_attr(node, "backgroundColor")
        if bg: r.background_color = bg

        origin = self._get_attr(node, "origin")
        if origin: r.x, r.x_unit, r.y, r.y_unit = self._parse_coords_full(origin)
        pos = self._get_attr(node, "position")
        if pos: self._parse_ttml_position(pos, r)
        extent = self._get_attr(node, "extent")
        if extent: r.width, r.width_unit, r.height, r.height_unit = self._parse_coords_full(extent)

        disp = self._get_attr(node, "displayAlign")
        txt = self._get_attr(node, "textAlign")

        if r.is_vertical:
            r.align_horizontal = "right" # Default Vertical
            r.align_vertical = "top"
            if disp:
                if disp == "before": r.align_horizontal = "right"
                elif disp == "after": r.align_horizontal = "left"
                else: r.align_horizontal = "center"
            if txt:
                if txt == "start": r.align_vertical = "top"
                elif txt == "end": r.align_vertical = "bottom"
                else: r.align_vertical = "center"
        else:
            r.align_vertical = "top" # Default Horizontal
            r.align_horizontal = "center"
            if disp:
                if disp == "before": r.align_vertical = "top"
                elif disp == "after": r.align_vertical = "bottom"
                else: r.align_vertical = "center"
            if txt:
                if txt == "start": r.align_horizontal = "left"
                elif txt == "end": r.align_horizontal = "right"
                else: r.align_horizontal = "center"

        # Apply Safe Title Area logic to prevent edge-hugging text
        self._apply_safe_areas(r)

        project.regions[rid] = r

    def _apply_safe_areas(self, region: Region):
        """
        Enforces 'Safe Title Area' margins (approx 5%).
        Prevents text from touching the absolute edges of the screen.
        """

        # Helper: Normalize units to % for comparison (1vh/vw ~= 1%)
        def to_pct(val, unit):
            if unit == '%': return val
            if unit in ['vh', 'vw', 'rh', 'rw']: return val
            return None

        # 1. Horizontal Safety
        w = to_pct(region.width, region.width_unit)

        # If box is Edge-to-Edge (>= 100% width), shrink and center it.
        # This fixes: extent="100% 15%" -> width="90%", left="50%", transform="-50%"
        if w is not None and w >= 99.0:
            region.width = 90.0
            region.width_unit = "%"
            region.x = 50.0
            region.x_unit = "%"
            region.x_edge = "center"

        # 2. Vertical Safety
        h = to_pct(region.height, region.height_unit)
        y = to_pct(region.y, region.y_unit)

        if h is not None and y is not None:
            # Case A: Positioned from Top
            if region.y_edge == "top":
                # Clamp Top Margin
                if y < 5.0: region.y = 5.0

                # Clamp Bottom Edge (if region extends past 95%)
                if (y + h) > 95.0:
                    overshoot = (y + h) - 95.0
                    region.y = max(5.0, y - overshoot)

            # Case B: Positioned from Bottom (e.g. position="bottom 0px")
            elif region.y_edge == "bottom":
                # Ensure at least 5% margin from bottom
                if y < 5.0: region.y = 5.0

    def _parse_ttml_position(self, pos_str: str, region: Region):
        """
        Parses tts:position="x y".
        Overrides any coordinates set by tts:origin.
        """
        tokens = pos_str.split()
        h_keys = ["left", "right", "center"]
        v_keys = ["top", "bottom", "center"]

        # Track state locally: 0=Need X, 1=Need Y
        vals_found = 0

        for t in tokens:
            is_h = t in h_keys
            is_v = t in v_keys

            # --- Keyword Handling ---
            if is_h and is_v:  # "center" keyword
                # Ambiguous "center": First time is X, second is Y
                if vals_found == 0:
                    region.x_edge = "center"; vals_found += 1
                else:
                    region.y_edge = "center"; vals_found += 1
            elif is_h:
                region.x_edge = t
            elif is_v:
                region.y_edge = t
            else:
                # --- Numeric Value Handling ---
                val, unit = self._parse_unit(t)

                # Heuristic: If coordinate is ~50%, force Center anchor.
                # This prevents 'left: 50%' which pushes the box to the right half.
                is_center = (abs(val - 50.0) < 0.1 and unit == "%")

                if vals_found == 0:
                    # First number is X
                    region.x = val
                    region.x_unit = unit
                    if is_center: region.x_edge = "center"
                    vals_found += 1
                elif vals_found == 1:
                    # Second number is Y
                    region.y = val
                    region.y_unit = unit
                    if is_center: region.y_edge = "center"
                    vals_found += 1

    def _map_attributes(self, node, style: Style):
        c = self._get_attr(node, "color")
        if c: style.color = c
        bg = self._get_attr(node, "backgroundColor")
        if bg: style.background_color = bg
        op = self._get_attr(node, "opacity")
        if op: style.opacity = float(op)
        sb = self._get_attr(node, "showBackground")
        if sb: style.show_background = sb

        fam = self._get_attr(node, "fontFamily")
        if fam:
            raw_list = [x.strip().strip("'") for x in fam.split(',')]
            final_stack = []

            for f in raw_list:
                # Map non-standard TTML keywords to valid CSS-friendly generic names
                if f in ["Japanese", "mincho", "Mincho"]:
                    # We keep "sans-serif" as a safe web standard fallback
                    # The Renderer will inject the specific Noto/Hiragino preference via CSS
                    # TODO: this should be normalized, it's redundant in the models.py font stack
                    for font_name in [
                        "Arial",
                        "Noto Sans CJK JP",
                        "Noto Sans JP",
                        "Hiragino Sans",
                        "Hiragino Kaku Gothic ProN",
                        "Yu Gothic",
                        "Meiryo",
                        "sans-serif"
                    ]:
                        final_stack.append(font_name)

                elif f in ["default", "sansSerif", "proportionalSansSerif"]:
                    final_stack.append("sans-serif")

                elif f in ["serif", "proportionalSerif"]:
                    final_stack.append("serif")

                elif f in ["monospace", "monospaceSansSerif", "monospaceSerif"]:
                    final_stack.append("monospace")

                else:
                    # Pass through specific names (e.g. "Arial")
                    final_stack.append(f)

            style.font_family = final_stack

        fs = self._get_attr(node, "fontSize")
        if fs: val, unit = self._parse_unit(fs); style.font_size = val; style.font_size_unit = unit
        fw = self._get_attr(node, "fontWeight")
        if fw: style.font_weight = fw
        fst = self._get_attr(node, "fontStyle")
        if fst: style.font_style = fst
        origin = self._get_attr(node, "origin")
        if origin: style.origin_x, style.origin_x_unit, style.origin_y, style.origin_y_unit = self._parse_coords_full(origin)
        extent = self._get_attr(node, "extent")
        if extent: style.extent_width, style.extent_width_unit, style.extent_height, style.extent_height_unit = self._parse_coords_full(extent)
        pad = self._get_attr(node, "padding")
        if pad: style.padding = pad
        wm = self._get_attr(node, "writingMode")
        if wm:
            style.writing_mode = wm
            if any(x in wm for x in ['tblr', 'tbrl', 'tb-rl', 'vertical']): style.is_vertical = True
            elif 'lr' in wm or 'horizontal' in wm: style.is_vertical = False
        ta = self._get_attr(node, "textAlign")
        if ta: style.text_align = ta
        da = self._get_attr(node, "displayAlign")
        if da: style.display_align = da
        mra = self._get_attr(node, "multiRowAlign")
        if mra: style.multi_row_align = mra
        lh = self._get_attr(node, "lineHeight")
        if lh: val, unit = self._parse_unit(lh); style.line_height = val; style.line_height_unit = unit
        outline = self._get_attr(node, "textOutline")
        if outline:
            parts = outline.strip().split()
            if len(parts) >= 2:
                p1, p2 = parts[0], parts[1]
                if p1[0].isdigit() or p1.startswith('.'): style.outline_width, style.outline_unit = self._parse_unit(p1); style.outline_color = p2
                else: style.outline_color = p1; style.outline_width, style.outline_unit = self._parse_unit(p2)
        shear = self._get_attr(node, "fontShear") or self._get_attr(node, "shear")
        if shear: val, _ = self._parse_unit(shear); style.skew_angle = val
        r_role = self._get_attr(node, "ruby");
        if r_role: style.ruby_role = r_role
        ra = self._get_attr(node, "rubyAlign");
        if ra: style.ruby_align = ra
        rp = self._get_attr(node, "rubyPosition")
        if rp: style.ruby_position = "under" if rp in ["after", "under"] else "over"
        te = self._get_attr(node, "textEmphasis")
        if te:
            tokens = te.split(); pos_kw = {"before", "after", "outside", "inside"}; pos_found = None; style_parts = []
            for t in tokens:
                if t in pos_kw: pos_found = "under" if t in ["after", "under"] else "over"
                else: style_parts.append(t)
            style.text_emphasis_style = " ".join(style_parts); style.text_emphasis_position = pos_found or "over"

    # --- UTILS ---
    def _parse_coords_full(self, val_str) -> Tuple[float, str, float, str]:
        parts = val_str.split()
        if len(parts) != 2: return 0.0, "", 0.0, ""
        x, xu = self._parse_unit(parts[0]); y, yu = self._parse_unit(parts[1])
        return x, xu, y, yu
    def _parse_unit(self, val_str) -> Tuple[float, str]:
        match = UNIT_RE.match(val_str.strip())
        if match: return float(match.group(1)), (match.group(2) if match.group(2) else "")
        return 0.0, ""
    def _strip_ns(self, tag): return tag.split('}', 1)[1] if '}' in tag else tag
    def _get_attr(self, elem, name):
        if name in elem.attrib: return elem.attrib[name]
        for k, v in elem.attrib.items():
            if self._strip_ns(k) == name: return v
        return None
    def _find_child(self, parent, tag):
        for child in parent:
            if self._strip_ns(child.tag) == tag: return child
        return None
    def _parse_meta(self, root, project):

        # 1. Dimensions (tts:extent)
        # Allows reading "1920px 1080px" from the file instead of hardcoding defaults
        extent = self._get_attr(root, "extent")
        if extent:
            parts = extent.replace('px', '').split()
            if len(parts) == 2:
                try:
                    project.width = int(parts[0])
                    project.height = int(parts[1])
                except ValueError:
                    pass
        # 2. Frame Rate (ttp:frameRate)
        fps = self._get_attr(root, "frameRate")
        if fps:
            try:
                project.fps_num = int(fps)
                project.fps_den = 1
            except:
                pass

        # 3. Frame Rate Multiplier (ttp:frameRateMultiplier)
        # CRITICAL: This turns "24" into "23.976" (24 * 1000 / 1001)
        mult = self._get_attr(root, "frameRateMultiplier")
        if mult:
            parts = mult.split()
            if len(parts) == 2:
                try:
                    project.fps_num *= int(parts[0])
                    project.fps_den = int(parts[1])
                except:
                    pass

        # 4. Netflix Flag (nttm:Smpte24TimingAdjusted)
        # If True, overrides everything to 23.976
        smpte = self._get_attr(root, "Smpte24TimingAdjusted")
        if smpte and smpte.lower() == 'true':
            project.fps_num = 24000
            project.fps_den = 1001

        # 5. Tick Rate (Stored on Self, not Project)
        tick = self._get_attr(root, "tickRate")
        if tick:
            self.tick_rate = int(tick)
        else:
            self.tick_rate = None

    def _parse_time(self, t_str, project):
        if not t_str: return 0

        # 1. Handle Ticks (if applicable)
        if t_str.endswith('t'):
            ticks = int(t_str.strip('t'))
            rate = getattr(self, 'tick_rate', None) or 10000000
            return int(ticks / rate * 1000)

        # 2. Parse HH:MM:SS (and optional frames/ms)
        # We do NOT replace '.' with ':' yet to preserve ms vs frames distinction
        parts = t_str.split(':')
        total_seconds = 0.0

        # Standard SMPTE parsing logic
        if len(parts) == 3:
            # HH:MM:SS.mmm
            h, m, s = map(float, parts)
            total_seconds = (h * 3600.0) + (m * 60.0) + s

        elif len(parts) == 4:
            # HH:MM:SS:FF
            h, m, s, f = map(float, parts)
            fps = project.fps_num / project.fps_den
            total_seconds = (h * 3600.0) + (m * 60.0) + s + (f / fps)

            # 3. Return Raw Real-Time MS (No NTSC Stretch)
            # The timestamp in the file is already Real Time.
        return total_seconds * 1000.0


class WebVTTIngester:
    """
    Parses WebVTT files.

    UPGRADES:
    - **Smart Anchoring:** Switches Y-axis anchor to 'bottom' if line > 50%.
    - **Layout Fix:** Defaults width/height to Auto (None).
    - **Robust Tag Parsing:** Fixes the 'garbage text' issue.
    """

    def parse(self, path: str) -> SubtitleProject:
        project = SubtitleProject()

        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Converts zero width space to nothing
        content = content.replace("\u200b", "")

        # 1. Parse Global Styles
        style_blocks = re.findall(r'STYLE\s+(.*?)(?=\n\n|\Z)', content, re.DOTALL)
        for block in style_blocks:
            self._parse_css(block, project)

        detected_lang = self._detect_lang_from_filename(path)
        base_style = Style.get_system_defaults(language=detected_lang)
        project.language = detected_lang if detected_lang else "en"

        region_cache = {}

        # 2. Block-Based Parsing
        # Split content by double newlines (blank lines) to isolate blocks.
        # r'\n\s*\n' handles multiple blank lines or lines containing only whitespace.
        blocks = re.split(r'\n\s*\n', content) #TODO: check if this is messing up a line that's only space

        for block in blocks:
            # 1. Split into lines (splitlines removes \n automatically)
            raw_lines = block.splitlines()

            # 2. Filter out empty lines, BUT preserve leading spaces on content lines.
            #    We use l.strip() only to check if the line has content.
            lines = [l for l in raw_lines if l.strip()]
            # ---------------------------------

            if not lines:
                continue

            first_line = lines[0]

            # --- BLOCK TYPE IDENTIFICATION ---

            # 1. Header Block (WEBVTT ...)
            # Skips the header AND any metadata lines (X-TIMESTAMP-MAP) inside this block
            if first_line.startswith("WEBVTT"):
                continue

            # 2. Note Block (NOTE ...)
            # Skips comments
            if first_line.startswith("NOTE"):
                continue

            # 3. Style Block (STYLE ...)
            # Already handled by regex above, so we skip here to avoid processing as Cue
            if first_line.startswith("STYLE"):
                continue

            # 4. Region Block (REGION ...)
            # TODO: Implement robust Region parsing. For now, skip to prevent errors.
            if first_line.startswith("REGION"):
                print("REGION PRESENT, CONSIDER IMPLEMENTING")
                continue

            # 5. Cue Block
            # If it's not any of the above, it must be a Cue (or garbage).
            # _process_cue_block verifies if it has a timestamp "-->"
            self._process_cue_block(
                lines,
                project,
                base_style,
                region_cache
            )
        return project

    def _detect_lang_from_filename(self, path: str) -> str:
        parts = path.lower().split('.')
        if len(parts) >= 3:
            potential_code = parts[-2]
            if potential_code in VALID_LANG_CODES:
                return VALID_LANG_CODES[potential_code]
        return ""

    def _parse_css(self, block, project):
        rules = re.findall(r'::cue\(\.([a-zA-Z0-9_\-]+)\)\s*\{(.*?)\}', block, re.DOTALL)
        for cls, content in rules:
            s = Style(id=cls)
            props = [p.strip() for p in content.split(';') if ':' in p]
            for prop in props:
                k, v = [x.strip() for x in prop.split(':', 1)]
                if k == 'color':
                    s.color = v
                elif k == 'background-color':
                    s.background_color = v
                elif k == 'font-family':
                    s.font_family = [v]
                elif k == 'text-emphasis' or k == 'text-emphasis-style':
                    s.text_emphasis_style = v; s.text_emphasis_position = "over"
                elif k == 'text-emphasis-position':
                    s.text_emphasis_position = v
                elif k == 'ruby-position':
                    s.ruby_position = "under" if "under" in v else "over"
                elif k == 'x-ttml-shear':
                    s.skew_angle = float(v.strip('%'))
                elif k == 'text-combine-upright':
                    s.text_combine = v
                elif k == 'text-shadow':
                    s.shadow_color = v
            project.styles[cls] = s

    def _process_cue_block(self, lines, project, base_style, region_cache):
        arrow_idx = -1
        for i, l in enumerate(lines):
            if "-->" in l:
                arrow_idx = i
                break
        if arrow_idx == -1: return

        timing_line = lines[arrow_idx]
        try:
            start_str, end_rest = timing_line.split("-->")
            end_str = end_rest.split()[0]
        except ValueError:
            return

        settings = end_rest.strip().split()[1:]
        cue_region = self._get_or_create_region(settings, project, region_cache)

        cue = Cue(
            start_ms=self._parse_vtt_time(start_str.strip()),
            end_ms=self._parse_vtt_time(end_str.strip()),
            region=cue_region
        )

        payload = "\n".join(lines[arrow_idx + 1:])
        self._parse_payload(payload, cue.fragments, project.styles, base_style)

        # If sanitization removed all text (e.g. it was just metadata),
        # fragments will be empty. We simply return early to drop this cue.
        if not cue.fragments:
            return

        project.body.cues.append(cue)

    def _get_or_create_region(self, settings, project, region_cache):
        r_vertical = False
        r_line_val = None
        r_line_align = None
        r_pos_val = None
        r_pos_align = None
        r_align = "start"
        r_size = None

        for s in settings:
            if ':' not in s: continue
            k, v = s.split(':', 1)

            v_parts = v.split(',')
            val_clean = v_parts[0].replace('%', '')
            val_align = v_parts[1] if len(v_parts) > 1 else None

            if k == 'vertical' and v == 'rl':
                r_vertical = True
            elif k == 'align':
                r_align = v
            elif k == 'line':
                try:
                    val = float(val_clean)
                    # --- CLAMPING FIX: Keep within 5% - 95% ---
                    r_line_val = max(5.0, min(val, 95.0))
                    # r_line_val = val # remove this line to reapply padding
                except:
                    pass
                r_line_align = val_align
            elif k == 'position':
                try:
                    val = float(val_clean)
                    # --- CLAMPING FIX: Keep within 5% - 95% ---
                    r_pos_val = max(5.0, min(val, 95.0))
                    # r_pos_val = val  # remove this line to reapply padding
                except:
                    pass
                r_pos_align = val_align
            elif k == 'size':
                try:
                    r_size = float(val_clean)
                except:
                    pass

        signature = (r_vertical, r_line_val, r_line_align, r_pos_val, r_align, r_size)
        if signature in region_cache:
            return region_cache[signature]

        new_id = f"vtt_region_{len(region_cache)}"
        r = Region(id=new_id)
        r.is_vertical = r_vertical
        r.align_horizontal = r_align

        # KEY: Default to None (Auto)
        r.width = None
        r.height = None

        if r_vertical:
            r.writing_mode = 'tbrl'
            if r_size is not None:
                r.height = r_size
            else:
                r.height = 100

            line_v = r_line_val if r_line_val is not None else 10
            anchor = r_line_align if r_line_align else "start"

            if anchor == "end":
                r.x = 100.0 - line_v
                r.x_edge = "left"
            elif anchor == "start":
                r.x = line_v
                r.x_edge = "right"
            else:
                r.x = line_v
                r.x_edge = "right"
            r.x_unit = "%"

            if r_pos_val is not None:
                r.y = r_pos_val
                r.y_edge = "top"
                if r_pos_align == "center":
                    r.y_edge = "center"
                elif r_pos_align == "end":
                    r.y_edge = "bottom"
            else:
                r.y = 0
                r.y_edge = "top"
            r.y_unit = "%"

        else:
            if r_size is not None:
                r.width = r_size
            else:
                # This allows 'align:start' (Left Text) to exist inside a 'position:50%' (Centered Box).
                r.width = None

            # SMART ANCHORING
            if r_line_val is not None:
                # 1. Check for Explicit Alignment (line:50%,center)
                if r_line_align == "center":
                    r.y = r_line_val
                    r.y_edge = "center"
                elif r_line_align == "end":
                    r.y = 100.0 - r_line_val
                    r.y_edge = "bottom"
                elif r_line_align == "start":
                    r.y = r_line_val
                    r.y_edge = "top"
                else:
                    # 2. Implicit / Smart Defaults
                    # Spec says default is 'start' (top), but 'smart' behavior expects
                    # high values (>50%) to anchor to the bottom.
                    if r_line_val > 50:
                        r.y = 100.0 - r_line_val
                        r.y_edge = "bottom"
                    else:
                        r.y = r_line_val
                        r.y_edge = "top"
            else:
                r.y = 10
                r.y_edge = "bottom"
            r.y_unit = "%"

            if r_pos_val is not None:
                r.x = r_pos_val
                r.x_unit = "%"

                # 1. Determine Anchor (Explicit or Implicit)
                anchor = r_pos_align
                if not anchor:
                    # Implicit: If no anchor is specified, VTT defaults depend on alignment
                    if r_align in ["start", "left"]:
                        anchor = "line-left"
                    elif r_align in ["end", "right"]:
                        anchor = "line-right"
                    else:
                        anchor = "center"

                # 2. Apply Anchor
                # We normalize VTT keywords (line-left) and legacy/simple keywords (start)
                if anchor in ["line-left", "start", "left"]:
                    r.x_edge = "left"
                elif anchor in ["line-right", "end", "right"]:
                    r.x_edge = "right"
                    # CSS 'right' is the distance from the right edge, so we flip the coordinate
                    r.x = 100.0 - r_pos_val
                else:
                    r.x_edge = "center"
            else:
                r.x = 50
                r.x_edge = "center"
                r.x_unit = "%"

        project.regions[new_id] = r
        region_cache[signature] = r
        return r

    def _parse_vtt_time(self, t):
        parts = t.replace('.', ':').split(':')
        if len(parts) == 4: return int(
            (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000 + int(parts[3]))
        if len(parts) == 3: return int((int(parts[0]) * 60 + int(parts[1])) * 1000 + int(parts[2]))
        return 0

    def _parse_payload(self, text, fragments, styles, base_style):
        tokens = VTT_TAG_RE.split(text)

        # Stack always starts with the base style
        style_stack = [base_style]
        current_style = base_style

        in_ruby = False
        ruby_base_buffer = ""
        ruby_text_buffer = ""
        in_rt = False

        i = 0
        while i < len(tokens):
            token = tokens[i]
            if not token:
                i += 1;
                continue

            if token.startswith('<') and token.endswith('>'):
                tag_content = token[1:-1]
                is_close = tag_content.startswith('/')
                tag_raw = tag_content.replace('/', '')
                tag_parts = tag_raw.split('.', 1)
                tag_name = tag_parts[0]

                if tag_name == 'ruby':
                    if is_close:
                        if ruby_base_buffer:
                            fragments.append(Fragment(
                                text=ruby_base_buffer,
                                calculated_style=current_style,
                                applied_style_ids=["VTT_RUBY_ORPHAN"]
                            ))

                        in_ruby = False
                        ruby_base_buffer = ""
                        ruby_text_buffer = ""
                        in_rt = False
                    else:
                        in_ruby = True
                        ruby_base_buffer = ""
                        ruby_text_buffer = ""
                        in_rt = False

                elif tag_name == 'rt':
                    if is_close:
                        # ACTION: Flush the pair immediately upon </rt>
                        # This ensures <ruby>A<rt>a</rt>B<rt>b</rt></ruby> becomes 2 fragments
                        # instead of 1 fragment (base="AB", ruby="ab").
                        fragments.append(Fragment(
                            text=ruby_text_buffer,
                            ruby_base=ruby_base_buffer,
                            is_ruby=True,
                            calculated_style=current_style,
                            applied_style_ids=["VTT_RUBY"]
                        ))
                        # Reset buffers for the next character in the sequence
                        ruby_base_buffer = ""
                        ruby_text_buffer = ""
                        in_rt = False
                    else:
                        in_rt = True

                elif tag_name == 'br':
                    fragments.append(Fragment(text="\n", calculated_style=current_style, applied_style_ids=["VTT_BR"]))

                elif tag_name == 'i':
                    if is_close:
                        # POP Style
                        if len(style_stack) > 1:
                            style_stack.pop()
                            current_style = style_stack[-1]
                        else:
                            current_style = base_style
                    else:
                        # PUSH Style (Apply 16% Skew)
                        # We apply skew_angle instead of font_style="italic" to mimic
                        # the tts:shear geometric slant common in IMSC/TTML workflows.
                        skew_s = Style(id="vtt_i_skew", skew_angle=16.667)

                        new_style = current_style.merge_from(skew_s)
                        style_stack.append(new_style)
                        current_style = new_style

                elif tag_name == 'c':
                    if is_close:
                        # POP Style
                        if len(style_stack) > 1:
                            style_stack.pop()
                            current_style = style_stack[-1]
                        else:
                            # Safety: never pop the base style
                            current_style = base_style
                    elif len(tag_parts) > 1:
                        # PUSH Style (Merge)
                        cls = tag_parts[1]
                        if cls in styles:
                            new_style = current_style.merge_from(styles[cls])
                            style_stack.append(new_style)
                            current_style = new_style
                        else:
                            # Push duplicate of current if class not found (preserves stack depth)
                            style_stack.append(current_style)
                    else:
                        # <c> with no class -> Push duplicate to preserve stack depth
                        style_stack.append(current_style)
                i += 1
            else:
                if in_ruby:
                    if in_rt:
                        ruby_text_buffer += token
                    else:
                        ruby_base_buffer += token
                    i += 1
                else:
                    processed_frags = self._parse_flattened_ruby(token, current_style)
                    fragments.extend(processed_frags)
                    i += 1

    def _is_kanji(self, char):
        return 0x4E00 <= ord(char) <= 0x9FAF or char == '々'

    def _is_hiragana(self, char):
        return 0x3040 <= ord(char) <= 0x309F

    def _is_katakana(self, char):
        return 0x30A0 <= ord(char) <= 0x30FF or char == 'ー'

    def _parse_flattened_ruby(self, text: str, style: Style) -> List[Fragment]:
        results = []
        buffer = ""

        i = 0
        while i < len(text):
            char = text[i]

            if char == '\n':
                if buffer:
                    results.append(Fragment(text=buffer, calculated_style=style, applied_style_ids=["VTT_TEXT"]))
                    buffer = ""
                results.append(Fragment(text="\n", calculated_style=style, applied_style_ids=["VTT_NEWLINE"]))
                i += 1
                continue

            if char == '(':
                close_idx = text.find(')', i)
                if close_idx != -1:
                    furigana = text[i + 1:close_idx]

                    # 1. Look for the last normal space in the buffer.
                    # 2. Everything after that space is the Base.
                    # 3. Everything before that space is Pre-Text.
                    # 4. The space itself is discarded (delimiter).

                    space_idx = buffer.rfind(' ')

                    if space_idx != -1:
                        pre_text = buffer[:space_idx]
                        base_text = buffer[space_idx + 1:]
                    else:
                        # No space found? The whole buffer is the base (e.g. start of line).
                        pre_text = ""
                        base_text = buffer

                    # Only create Ruby if we actually have base text
                    # (This prevents ' (text)' with a leading space from breaking)
                    if base_text:
                        if pre_text:
                            results.append(
                                Fragment(text=pre_text, calculated_style=style, applied_style_ids=["VTT_TEXT"]))

                        results.append(Fragment(
                            text=furigana,
                            ruby_base=base_text,
                            is_ruby=True,
                            calculated_style=style,
                            applied_style_ids=["VTT_AUTO_RUBY"]
                        ))
                        buffer = ""
                        i = close_idx + 1
                        continue
            buffer += char
            i += 1

        if buffer:
            results.append(Fragment(text=buffer, calculated_style=style, applied_style_ids=["VTT_TEXT"]))

        return results