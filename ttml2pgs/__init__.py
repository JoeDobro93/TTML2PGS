"""
TTML2PGS — fast TTML/WebVTT/SRT to PGS (.sup) subtitle renderer.

Ground-up rewrite of the original HTML/Chrome-screenshot pipeline with a
direct pixel rasterizer (HarfBuzz shaping + FreeType rendering), an
editable document model, per-language global overrides, and a robust
video-grouped render/remux queue.
"""

__version__ = "2.0.0"
