"""
core/models.py

The Unified Data Model.
Stores subtitle data in a normalized format, independent of the input file type (TTML vs VTT).
Supports hierarchical inheritance (Style chains) and precise layout units.

UPDATED:
- Added `get_system_defaults` to Style class to centralize fallback styling.
"""

from dataclasses import dataclass, field, replace
from typing import List, Optional, Dict, Any, Tuple

# --- STYLE MODEL ---
@dataclass
class Style:
    """
    Represents a set of visual attributes.
    Attributes are Optional; 'None' means "inherit from parent".
    """
    id: str

    # -- Fonts --
    font_family: Optional[List[str]] = None
    font_size: Optional[float] = None
    font_size_unit: Optional[str] = None # 'vh', 'px', 'em', '%', 'rh'
    font_weight: Optional[str] = None    # 'bold', 'normal'
    font_style: Optional[str] = None     # 'italic', 'normal'

    # -- Colors & Opacity --
    color: Optional[str] = None           # Hex or rgba string
    background_color: Optional[str] = None
    opacity: Optional[float] = None       # 0.0 to 1.0 (tts:opacity)
    show_background: Optional[str] = None # 'always', 'whenActive'

    # -- Global Layout (Safe Area / Root Container) --
    # These often come from <initial> tags and define the "Canvas" constraints.
    origin_x: Optional[float] = None
    origin_x_unit: Optional[str] = None

    origin_y: Optional[float] = None
    origin_y_unit: Optional[str] = None

    extent_width: Optional[float] = None
    extent_width_unit: Optional[str] = None

    extent_height: Optional[float] = None
    extent_height_unit: Optional[str] = None

    # -- Positioning / Writing Mode --
    is_vertical: Optional[bool] = None    # Helper flag for vertical text
    writing_mode: Optional[str] = None    # Exact string: 'tblr', 'lrtb', 'vertical-rl'

    text_align: Optional[str] = None      # 'start', 'center', 'end', 'left', 'right'
    display_align: Optional[str] = None   # 'before' (top), 'center', 'after' (bottom)
    multi_row_align: Optional[str] = None # ebutts:multiRowAlign ('start', 'center', 'auto')

    # -- Outline --
    outline_enabled: Optional[bool] = None
    outline_color: Optional[str] = None
    outline_width: Optional[float] = None
    outline_unit: Optional[str] = None

    # -- Shadow --
    shadow_enabled: Optional[bool] = None
    shadow_color: Optional[str] = None
    shadow_offset_x: Optional[float] = None
    shadow_offset_y: Optional[float] = None
    shadow_blur: Optional[float] = None
    shadow_unit: Optional[str] = None

    skew_angle: Optional[float] = None    # tts:shear / fontShear

    # -- Spacing --
    line_height: Optional[float] = None
    line_height_unit: Optional[str] = None
    padding: Optional[str] = None         # Raw string, e.g. "2px 5px"

    # -- Ruby (Furigana) --
    ruby_role: Optional[str] = None       # 'container', 'base', 'text', 'delimiter'
    ruby_align: Optional[str] = None      # 'center', 'space-around'
    ruby_position: Optional[str] = None   # 'over', 'under'

    text_combine: Optional[str] = None  # 'all', 'digits', etc. (for tate-chu-yoko)

    # -- Text Emphasis (Bouten / Dots) --
    text_emphasis_style: Optional[str] = None    # e.g. "dot", "circle", "filled"
    text_emphasis_position: Optional[str] = None # e.g. "over", "under"
    text_emphasis_color: Optional[str] = None

    def merge_from(self, other: 'Style') -> 'Style':
        """
        Returns a NEW Style object that is a copy of 'self',
        overwritten by any non-None attributes found in 'other'.

        This mimics CSS cascading: Child properties override Parent properties.
        """
        new_style = replace(self)
        for k, v in other.__dict__.items():
            if k == "id": continue
            if v is not None:
                setattr(new_style, k, v)
        return new_style

    @staticmethod
    def get_system_defaults(language: str = "en") -> 'Style':
        """
        Returns the absolute baseline style for the entire project.
        Accepts a language code ('en', 'ja') to determine appropriate font stacks.
        """

        # Normalize
        lang = language.lower().strip()

        # Determine Font Stack based on language
        if language.lower() in ["ja", "jp", "jpn", "ja-jp"]:
            # Japanese Stack: Noto (User Pref) -> Mac Standards -> Windows Standards -> Fallback
            font_stack = [
                "Arial",
                "Noto Sans CJK JP",
                "Noto Sans JP",
                "Hiragino Sans",
                "Hiragino Kaku Gothic ProN",
                "Yu Gothic",
                "Meiryo",
                "sans-serif"
            ]
        else:
            # Default / English Stack
            font_stack = ["Arial", "Helvetica", "sans-serif"]

        return Style(
            id="__SYSTEM_DEFAULT__",

            # Dynamic Font Family
            font_family=font_stack,

            font_size=4.5,
            font_size_unit="vh",
            color="#FFFFFF", #white
            opacity=1.0,

            outline_enabled=True,
            outline_color="#000000", #black outline
            outline_width=3.0,
            outline_unit="px",

            shadow_enabled=True,
            shadow_color="#000000",
            shadow_offset_x=2.0,
            shadow_offset_y=2.0,
            shadow_blur=0.1,
            shadow_unit="px",

            line_height=1.2,
            line_height_unit="em"
        )

# --- REGION MODEL ---
@dataclass
class Region:
    """
    Defines a layout box.
    Updated to handle IMSC/TTML2 tts:position (edge offsets).
    """
    id: str

    # -- Positioning --
    # tts:origin implies x_edge="left", y_edge="top"
    # tts:position implies flexible edges (e.g. x_edge="right", x=10%)
    x: float = 0.0
    x_unit: str = "%"
    x_edge: str = "left" # 'left', 'right', 'center'

    y: float = 0.0
    y_unit: str = "%"
    y_edge: str = "top"  # 'top', 'bottom', 'center'

    # -- Sizing --
    width: float = 100.0
    width_unit: str = "%"

    height: float = 100.0
    height_unit: str = "%"

    # -- Alignment --
    # Defaults changed to 'top' and 'left' to match TTML 'before'/'start' defaults
    # (unless writingMode changes the physical meaning)
    align_vertical: str = "top"
    align_horizontal: str = "left"

    # -- Visuals --
    z_index: int = 0
    background_color: Optional[str] = None
    opacity: Optional[float] = None
    show_background: Optional[str] = None # 'always', 'whenActive'

    # -- Writing Mode --
    # In IMSC, regions can define the text flow for their children
    writing_mode: Optional[str] = None
    is_vertical: bool = False

# --- CONTENT MODELS ---
@dataclass
class Fragment:
    """
    The smallest atomic unit of text.
    'calculated_style' is the final result of the inheritance chain for this specific text.
    """
    text: str
    calculated_style: Style
    is_ruby: bool = False
    ruby_base: Optional[str] = None

    # Debugging: List of IDs that contributed to this style (e.g. ['s1', 'inline'])
    applied_style_ids: List[str] = field(default_factory=list)

@dataclass
class Cue:
    """A specific event in time containing a list of text fragments."""
    start_ms: float
    end_ms: float
    region: Optional[Region] = None
    fragments: List[Fragment] = field(default_factory=list)

    active: bool = True

    @property
    def duration_ms(self):
        return self.end_ms - self.start_ms

@dataclass
class SubtitleBody:
    """The root container for cues."""
    style_id: Optional[str] = None
    region_id: Optional[str] = None
    cues: List[Cue] = field(default_factory=list)

@dataclass
class SubtitleProject:
    """The Master Object holding all data."""
    width: int = 1920
    height: int = 1080
    fps_num: int = 24000
    fps_den: int = 1001

    # A value (positive or negative) in milliseconds to shift the final output.
    # Defaults to 0. Can be set by UI.
    timing_offset_ms: float = 0

    # Stores 'ja', 'en', etc. so the Renderer can set <html lang="...">
    language: str = "en"

    styles: Dict[str, Style] = field(default_factory=dict)
    regions: Dict[str, Region] = field(default_factory=dict)

    # The Global Defaults (merged result of all <initial> tags)
    initial_style: Optional[Style] = None

    body: SubtitleBody = field(default_factory=SubtitleBody)