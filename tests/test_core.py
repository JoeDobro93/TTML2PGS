"""
Core test suite: parsers, units, retiming, layout/render, PGS output,
exporters round-trip, project format, and the queue engine.

Run:  python -m pytest tests/ -v   (or python tests/test_core.py)
"""

import os
import struct
import sys
import tempfile
import time
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from ttml2pgs.core.colors import parse_color, to_hex
from ttml2pgs.core.exporters import export_srt, export_ttml, export_vtt
from ttml2pgs.core.model import Cue, SpanNode, Style, SubtitleDocument
from ttml2pgs.core.overrides import OverrideSet, StyleOverrides
from ttml2pgs.core.parsers import detect_format, load_subtitle
from ttml2pgs.core.parsers.ttml import TTMLParser
from ttml2pgs.core.parsers.vtt import VTTParser
from ttml2pgs.core.parsers.srt import SRTParser
from ttml2pgs.core.pgs import (TimedRender, TimelineBuilder, quantize_event,
                               rle_encode, write_sup_file, SupObject)
from ttml2pgs.core.pipeline import RenderPipeline, RenderSettings
from ttml2pgs.core.project import load_project, save_project
from ttml2pgs.core.renderer import CueRenderer, compute_canvas
from ttml2pgs.core.timing import (RetimePlan, parse_ttml_time,
                                  parse_vtt_timestamp, suggest_conform,
                                  TTMLTimeContext, normalize_fps)
from ttml2pgs.core.units import Dim, UnitContext

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'samples')


def sample(name):
    return os.path.join(SAMPLES, name)


class TestUnits(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(Dim.parse('80%'), Dim(80, '%'))
        self.assertEqual(Dim.parse('12.5px'), Dim(12.5, 'px'))
        self.assertEqual(Dim.parse('1.2', default_unit=''), Dim(1.2, ''))
        self.assertEqual(Dim.parse('4.5vh'), Dim(4.5, 'vh'))
        self.assertIsNone(Dim.parse('garbage'))

    def test_resolution_scales_px(self):
        # authored px scale with canvas: 3px @1080 → 6px @2160
        ctx = UnitContext(canvas_w=3840, canvas_h=2160,
                          doc_w=1920, doc_h=1080)
        self.assertAlmostEqual(ctx.resolve(Dim(3, 'px'), 'y'), 6.0)
        self.assertAlmostEqual(ctx.resolve(Dim(10, 'vh'), 'y'), 216.0)
        self.assertAlmostEqual(ctx.resolve(Dim(50, '%'), 'x'), 1920.0)

    def test_cell_units(self):
        ctx = UnitContext(canvas_w=1920, canvas_h=1080, cell_rows=15)
        self.assertAlmostEqual(ctx.resolve(Dim(1, 'c'), 'y'), 72.0)


class TestColors(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_color('#FFFFFF'), (255, 255, 255, 255))
        self.assertEqual(parse_color('#ffffff80'), (255, 255, 255, 128))
        self.assertEqual(parse_color('rgba(255,0,0,0.5)'), (255, 0, 0, 128))
        self.assertEqual(parse_color('rgb(0, 128, 0)'), (0, 128, 0, 255))
        self.assertEqual(parse_color('transparent'), (0, 0, 0, 0))
        self.assertEqual(parse_color('white'), (255, 255, 255, 255))
        self.assertEqual(to_hex((255, 0, 0, 255)), '#ff0000')
        self.assertEqual(to_hex((255, 0, 0, 128)), '#ff000080')


class TestTiming(unittest.TestCase):
    def test_ttml_times(self):
        ctx = TTMLTimeContext(frame_rate=Fraction(24000, 1001),
                              tick_rate=10_000_000)
        self.assertAlmostEqual(parse_ttml_time('1.5s', ctx), 1500.0)
        self.assertAlmostEqual(parse_ttml_time('500ms', ctx), 500.0)
        self.assertAlmostEqual(parse_ttml_time('10000000t', ctx), 1000.0)
        self.assertAlmostEqual(parse_ttml_time('00:00:05.250', ctx), 5250.0)
        self.assertAlmostEqual(parse_ttml_time('24f', ctx),
                               24 / (24000 / 1001) * 1000)
        # media clock-time frames: seconds + frames/effective_rate
        self.assertAlmostEqual(parse_ttml_time('00:00:01:12', ctx),
                               1000.0 + 12 / (24000 / 1001) * 1000.0, places=2)

    def test_smpte(self):
        ctx = TTMLTimeContext(frame_rate=Fraction(24000, 1001),
                              time_base='smpte')
        # 1 hour NDF @23.976: 86400 frames → real time 3603.6s
        v = parse_ttml_time('01:00:00:00', ctx)
        self.assertAlmostEqual(v, 86400 / (24000 / 1001) * 1000, places=1)

    def test_vtt_time(self):
        self.assertEqual(parse_vtt_timestamp('01:02:03.456'), 3723456.0)
        self.assertEqual(parse_vtt_timestamp('02:03.456'), 123456.0)
        self.assertEqual(parse_vtt_timestamp('02:03,456'), 123456.0)

    def test_conform(self):
        plan = RetimePlan.conform(Fraction(24000, 1001), Fraction(24))
        # one hour shrinks by ~3.6 s
        self.assertAlmostEqual(plan.apply(3_600_000), 3_600_000 * 1000 / 1001)
        # telecine pairs suggest no conform
        self.assertIsNone(suggest_conform(Fraction(30000, 1001),
                                          Fraction(24000, 1001)))
        self.assertIsNotNone(suggest_conform(Fraction(25), Fraction(24000, 1001)))
        self.assertIsNone(suggest_conform(Fraction(24), Fraction(24)))

    def test_normalize(self):
        self.assertEqual(normalize_fps(23.976), Fraction(24000, 1001))
        self.assertEqual(normalize_fps(24000, 1001), Fraction(24000, 1001))
        self.assertEqual(normalize_fps(25.0), Fraction(25))


class TestParsers(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(detect_format(sample('netflix_ja.ttml')), 'ttml')
        self.assertEqual(detect_format(sample('styled.vtt')), 'vtt')
        self.assertEqual(detect_format(sample('basic.srt')), 'srt')

    def test_ttml(self):
        doc = load_subtitle(sample('netflix_ja.ttml'))
        self.assertEqual(doc.language, 'ja')
        self.assertEqual(doc.fps, Fraction(24000, 1001))
        self.assertEqual(len(doc.cues), 5)
        self.assertIn('style0', doc.styles)
        self.assertIsNotNone(doc.styles['style0'].shear)
        self.assertTrue(doc.regions['region2'].is_vertical())
        # ruby structure preserved as spans
        ruby_cue = doc.cues[1]
        spans = [n for n in ruby_cue.root.children if n.kind == 'span']
        self.assertTrue(any(
            (s.inline_style and s.inline_style.ruby == 'container')
            for s in spans))

    def test_ttml_style_chain(self):
        xml = '''<tt xmlns="http://www.w3.org/ns/ttml"
            xmlns:tts="http://www.w3.org/ns/ttml#styling">
          <head><styling>
            <style xml:id="a" tts:color="red" tts:fontSize="10px"/>
            <style xml:id="b" style="a" tts:color="lime"/>
          </styling></head>
          <body><div>
            <p begin="0s" end="1s" style="b">x</p>
          </div></body></tt>'''
        doc = TTMLParser().parse_string(xml)
        computed = doc.resolve_style([(doc.cues[0].style_refs, None)], None)
        self.assertEqual(computed.color, (0, 255, 0, 255))     # b wins
        self.assertEqual(computed.font_size, Dim(10, 'px'))    # from a

    def test_vtt(self):
        doc = load_subtitle(sample('styled.vtt'))
        self.assertEqual(len(doc.cues), 6)
        self.assertIn('bottomband', doc.regions)
        self.assertIn('yellow', doc.styles)
        self.assertEqual(doc.styles['yellow'].color, (255, 255, 0, 255))
        # derived regions share signature
        v_regions = [r for r in doc.regions.values() if r.is_vertical()]
        self.assertEqual(len(v_regions), 1)
        # class span carries ref
        c0 = doc.cues[0]
        found = [n for n in c0.root.children
                 if n.kind == 'span' and 'yellow' in n.style_refs]
        self.assertTrue(found)

    def test_vtt_region_reuse(self):
        text = ('WEBVTT\n\n'
                '00:00.000 --> 00:01.000 line:90%\nA\n\n'
                '00:02.000 --> 00:03.000 line:90%\nB\n\n'
                '00:04.000 --> 00:05.000 line:10%\nC\n')
        doc = VTTParser().parse_string(text)
        rids = {c.region_id for c in doc.cues}
        self.assertEqual(len(rids), 2)     # 90% shared, 10% separate

    def test_srt(self):
        doc = load_subtitle(sample('basic.srt'))
        self.assertEqual(len(doc.cues), 4)
        top = doc.cues[2]
        self.assertTrue(top.region_id and 'an8' in top.region_id)
        green = doc.cues[3]
        spans = [n for n in green.root.children if n.kind == 'span']
        self.assertTrue(any(s.inline_style and s.inline_style.color ==
                            (0, 255, 0, 255) for s in spans))


class TestRealWorldQuirks(unittest.TestCase):
    """Regressions found against real Netflix/Amazon/Disney+ files."""

    def test_position_percent_is_point_semantics(self):
        # Netflix Django: position="right 10.0rw top 50.0%" must center
        # vertically ((H - h) * 50%), not offset the top edge by 50%.
        xml = '''<tt xmlns="http://www.w3.org/ns/ttml"
            xmlns:tts="http://www.w3.org/ns/ttml#styling">
          <head><layout>
            <region xml:id="r" tts:extent="30% 80%"
                    tts:position="right 10.0rw top 50.0%"
                    tts:writingMode="tbrl"/>
          </layout></head>
          <body><div><p begin="0s" end="1s" region="r">縦</p></div></body>
        </tt>'''
        doc = TTMLParser().parse_string(xml)
        r = doc.regions['r']
        self.assertEqual(r.y_edge, 'point')
        self.assertAlmostEqual(r.y.value, 50.0)
        self.assertEqual(r.x_edge, 'right')
        canvas = compute_canvas((1920, 1080), OverrideSet().layout)
        rend = CueRenderer(doc, canvas)
        rect = rend._region_rect(r)
        x, y = rend._anchor_pos(rect, rect['w'], rect['h'])
        self.assertAlmostEqual(y, (1080 - 864) / 2, delta=1)   # 108
        self.assertAlmostEqual(x, 1920 - 192 - 576, delta=1)   # 1152

    def test_smpte24_flag_on_head_metadata(self):
        xml = '''<tt xmlns="http://www.w3.org/ns/ttml"
            xmlns:nttm="http://www.netflix.com/ns/ttml#metadata"
            xmlns:ttp="http://www.w3.org/ns/ttml#parameter"
            ttp:tickRate="10000000">
          <head><metadata nttm:Smpte24TimingAdjusted="true"/></head>
          <body><div><p begin="0s" end="1s">x</p></div></body></tt>'''
        doc = TTMLParser().parse_string(xml)
        self.assertEqual(doc.fps, Fraction(24000, 1001))

    def test_language_normalization(self):
        from ttml2pgs.core.parsers import normalize_language
        self.assertEqual(normalize_language('jp'), 'ja')
        self.assertEqual(normalize_language('jpn'), 'ja')
        self.assertEqual(normalize_language('en-US'), 'en')
        self.assertEqual(normalize_language('zh-TW'), 'zh-Hant')

    def test_ttml2_extension_detected(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'x.ttml2')
            with open(p, 'w') as f:
                f.write('<tt xmlns="http://www.w3.org/ns/ttml"/>')
            self.assertEqual(detect_format(p), 'ttml')

    def test_char_substitution_fallback(self):
        # ⸺ (two-em dash) is missing from most fonts; it must substitute
        # to em dashes instead of falling back to the bitmap font.
        from ttml2pgs.core.layout import LayoutEngine, TextItem
        from ttml2pgs.core.fonts import FontManager
        fm = FontManager.instance()
        eng = LayoutEngine()
        from ttml2pgs.core.layout import RunStyle
        rs = RunStyle(font_px=40,
                      faces=fm.resolve_stack(['sans-serif'], lang='ja'))
        result = eng.layout([TextItem('あ⸺い', rs)], measure=None)
        self.assertGreaterEqual(len(result.glyphs), 4)  # ⸺ became 2 glyphs


class TestAutoRuby(unittest.TestCase):
    def _parse_srt(self, payload):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 't.ja.srt')
            with open(p, 'w', encoding='utf-8') as f:
                f.write(f"1\n00:00:01,000 --> 00:00:02,000\n{payload}\n")
            return load_subtitle(p)

    @staticmethod
    def _rubies(cue):
        out = []

        def w(n):
            if n.kind == 'span' and n.inline_style and \
                    n.inline_style.ruby == 'container':
                base = ann = ''
                for c in n.children:
                    role = c.inline_style.ruby if c.inline_style else ''
                    if role == 'base':
                        base = c.plain_text()
                    elif role == 'text':
                        ann = c.plain_text()
                out.append((base, ann))
            for c in n.children:
                w(c)
        w(cue.root)
        return out

    def test_ascii_parens_make_ruby_space_removed(self):
        doc = self._parse_srt('これは 東京(とうきょう)です')
        self.assertEqual(self._rubies(doc.cues[0]), [('東京', 'とうきょう')])
        # the delimiter space is removed from the render text
        self.assertNotIn(' ', doc.cues[0].plain_text())

    def test_fullwidth_parens_stay_text(self):
        doc = self._parse_srt('楽しい（わらい）時間')
        self.assertEqual(self._rubies(doc.cues[0]), [])
        self.assertIn('（わらい）', doc.cues[0].plain_text())

    def test_non_kana_annotation_stays_text(self):
        doc = self._parse_srt('組織(FBI)の捜査')
        self.assertEqual(self._rubies(doc.cues[0]), [])

    def test_line_start_base(self):
        doc = self._parse_srt('相談(パーレイ)する')
        self.assertEqual(self._rubies(doc.cues[0]), [('相談', 'パーレイ')])

    def test_vtt_gets_auto_ruby_too(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 't.ja.vtt')
            with open(p, 'w', encoding='utf-8') as f:
                f.write("WEBVTT\n\n00:01.000 --> 00:02.000\n"
                        "痙攣(けいれん)あり\n")
            doc = load_subtitle(p)
        self.assertEqual(self._rubies(doc.cues[0]), [('痙攣', 'けいれん')])

    def test_non_japanese_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 't.en.srt')
            with open(p, 'w', encoding='utf-8') as f:
                f.write("1\n00:00:01,000 --> 00:00:02,000\n"
                        "hello (world)\n")
            doc = load_subtitle(p)
        self.assertEqual(self._rubies(doc.cues[0]), [])


class TestDedup(unittest.TestCase):
    """HLS-chunked VTTs duplicate the boundary cue — must be condensed."""

    def _vtt(self, body):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 't.en.vtt')
            with open(p, 'w', encoding='utf-8') as f:
                f.write("WEBVTT\n\n" + body)
            return load_subtitle(p)

    def test_identical_overlap_condensed(self):
        doc = self._vtt(
            "00:10.000 --> 00:12.000 line:90%\nSame line\n\n"
            "00:10.000 --> 00:12.000 line:90%\nSame line\n")
        self.assertEqual(len(doc.cues), 1)

    def test_partial_overlap_merges_to_union(self):
        doc = self._vtt(
            "00:10.000 --> 00:12.000\nSame line\n\n"
            "00:11.500 --> 00:14.000\nSame line\n")
        self.assertEqual(len(doc.cues), 1)
        self.assertEqual(doc.cues[0].begin_ms, 10000)
        self.assertEqual(doc.cues[0].end_ms, 14000)

    def test_adjacent_identical_not_merged(self):
        doc = self._vtt(
            "00:10.000 --> 00:12.000\nSame line\n\n"
            "00:12.000 --> 00:14.000\nSame line\n")
        self.assertEqual(len(doc.cues), 2)

    def test_different_text_or_position_kept(self):
        doc = self._vtt(
            "00:10.000 --> 00:12.000 line:90%\nLine A\n\n"
            "00:10.000 --> 00:12.000 line:10%\nLine A\n\n"
            "00:10.000 --> 00:12.000 line:90%\nLine B\n")
        self.assertEqual(len(doc.cues), 3)

    def test_chain_of_chunk_duplicates(self):
        doc = self._vtt(
            "00:10.000 --> 00:12.000\nSame\n\n"
            "00:10.000 --> 00:12.000\nSame\n\n"
            "00:10.000 --> 00:12.000\nSame\n")
        self.assertEqual(len(doc.cues), 1)


class TestAutoColor(unittest.TestCase):
    def test_auto_color_picks_by_dynamic_range(self):
        so = StyleOverrides()
        so.auto_color = True
        so.auto_sdr_color = (229, 229, 229, 255)
        so.auto_hdr_color = (128, 128, 128, 255)
        so.auto_sdr_alpha = 1.0
        so.auto_hdr_alpha = 0.9
        sdr = so.to_style(is_hdr=False)
        hdr = so.to_style(is_hdr=True)
        self.assertEqual(sdr.color, (229, 229, 229, 255))
        self.assertEqual(hdr.color, (128, 128, 128, 255))
        self.assertAlmostEqual(hdr.opacity_mult, 0.9)
        # auto wins over manual color override
        so.override_color = True
        so.color = (255, 0, 0, 255)
        self.assertEqual(so.to_style(is_hdr=True).color,
                         (128, 128, 128, 255))

    def test_auto_color_changes_render(self):
        doc = load_subtitle(sample('netflix_ja.ttml'))
        canvas = compute_canvas((1920, 1080), OverrideSet().layout)
        ov = OverrideSet()
        so = ov.by_lang['']
        so.auto_color = True
        so.auto_hdr_color = (120, 120, 120, 255)
        sdr = CueRenderer(doc, canvas, ov, is_hdr=False)
        hdr = CueRenderer(doc, canvas, ov, is_hdr=True)
        a = sdr.render_cue(doc.cues[0])
        b = hdr.render_cue(doc.cues[0])
        self.assertFalse(np.array_equal(a.bitmap, b.bitmap))


class TestStyleResolution(unittest.TestCase):
    def test_nested_innermost_wins(self):
        doc = SubtitleDocument()
        doc.styles['blue'] = Style(id='blue', color=(0, 0, 255, 255))
        doc.styles['green'] = Style(id='green', color=(0, 255, 0, 255))
        computed = doc.resolve_style(
            [(['blue'], None), (['green'], None)], None)
        self.assertEqual(computed.color, (0, 255, 0, 255))

    def test_override_beats_everything(self):
        doc = SubtitleDocument()
        doc.styles['blue'] = Style(id='blue', color=(0, 0, 255, 255))
        ov = Style(color=(255, 0, 0, 255))
        computed = doc.resolve_style([(['blue'], None)], None, overrides=ov)
        self.assertEqual(computed.color, (255, 0, 0, 255))


class TestRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = load_subtitle(sample('netflix_ja.ttml'))
        cls.canvas = compute_canvas((1920, 1080), OverrideSet().layout)
        cls.renderer = CueRenderer(cls.doc, cls.canvas)

    def test_all_cues_render(self):
        for cue in self.doc.cues:
            rc = self.renderer.render_cue(cue)
            self.assertIsNotNone(rc, f"cue {cue.plain_text()!r} empty")
            self.assertGreater(rc.width, 4)
            self.assertGreater(rc.height, 4)

    def test_determinism(self):
        cue = self.doc.cues[0]
        a = self.renderer.render_cue(cue)
        b = self.renderer.render_cue(cue)
        self.assertEqual((a.x, a.y), (b.x, b.y))
        self.assertTrue(np.array_equal(a.bitmap, b.bitmap))

    def test_vertical_shape(self):
        vert = self.doc.cues[3]
        rc = self.renderer.render_cue(vert)
        self.assertGreater(rc.height, rc.width * 3,
                           "vertical cue should be tall")
        self.assertGreater(rc.x, 1300, "region2 sits on the right side")

    def test_font_override_scales(self):
        ov = OverrideSet()
        so = ov.ensure_language('ja')
        so.override_font_size = True
        so.font_size = Dim(12.0, 'vh')      # ~130px vs 72px default
        r2 = CueRenderer(self.doc, self.canvas, ov)
        big = r2.render_cue(self.doc.cues[0])
        normal = self.renderer.render_cue(self.doc.cues[0])
        self.assertGreater(big.height, normal.height * 1.5)

    def test_language_specific_override(self):
        ov = OverrideSet()
        en = ov.ensure_language('en')
        en.override_font_size = True
        en.font_size = Dim(9.0, 'vh')
        # ja cues must NOT pick up the en override
        r2 = CueRenderer(self.doc, self.canvas, ov)
        a = r2.render_cue(self.doc.cues[0])
        b = self.renderer.render_cue(self.doc.cues[0])
        self.assertEqual(a.height, b.height)

    def test_ar_letterbox(self):
        opts = OverrideSet().layout
        opts.override_ar = True
        opts.ar_w, opts.ar_h = 2.39, 1.0
        canvas = compute_canvas((1920, 1080), opts)
        self.assertEqual(canvas.width, 1920)
        self.assertLess(canvas.content_h, 830)
        self.assertGreater(canvas.content_y, 100)

    def test_padding_moves_regions_without_scaling_text(self):
        """Safe-area padding = v1 #pad-box: regions move inward, fonts
        keep their size (only AR letterboxing may scale text)."""
        cue = next(c for c in self.doc.sorted_cues()
                   if not self.doc.get_region(c).is_vertical())
        plain = self.renderer.render_cue(cue)

        ov = OverrideSet()
        ov.layout.use_padding = True
        ov.layout.padding_v = 10.0          # 5% inset per edge
        ov.layout.padding_h = 10.0
        padded_canvas = compute_canvas((1920, 1080), ov.layout)
        self.assertEqual(padded_canvas.content_h, 1080.0)   # unshrunk
        self.assertEqual(padded_canvas.pad_y, 54.0)
        rp = CueRenderer(self.doc, padded_canvas, ov)
        padded = rp.render_cue(cue)

        # same glyphs, same size: bitmap dims unchanged
        self.assertEqual((plain.width, plain.height),
                         (padded.width, padded.height))
        # the cue moves inward, away from its anchoring edge
        if plain.y + plain.height > 540:      # bottom-half cue
            self.assertLess(padded.y + padded.height,
                            plain.y + plain.height)
        else:                                 # top-half cue
            self.assertGreater(padded.y, plain.y)

    def test_region_overlay_boxes(self):
        try:
            from ttml2pgs.ui.widgets.preview import compute_region_boxes
        except ImportError:
            self.skipTest('PyQt6 not installed')
        for name in ('netflix_ja.ttml', 'styled.vtt'):
            doc = load_subtitle(sample(name))
            canvas = compute_canvas((1920, 1080), OverrideSet().layout)
            r = CueRenderer(doc, canvas, OverrideSet())
            boxes = compute_region_boxes(doc, r)
            self.assertEqual(len(boxes), len(doc.regions))
            colors = set()
            for rid, hexc, x, y, w, h, corner in boxes:
                self.assertGreater(w, 4, f'{name}:{rid} collapsed (w)')
                self.assertGreater(h, 4, f'{name}:{rid} collapsed (h)')
                self.assertTrue(0 <= x and x + w <= 1921 and
                                0 <= y and y + h <= 1081,
                                f'{name}:{rid} outside canvas')
                colors.add(hexc)
            self.assertEqual(len(colors), len(boxes),
                             'region colors must be distinct')


class TestPGS(unittest.TestCase):
    def _mkrender(self, uid, x, y, w, h, color=(255, 255, 255, 255)):
        from ttml2pgs.core.renderer import RenderedCue
        bmp = np.zeros((h, w, 4), np.uint8)
        bmp[...] = color
        return RenderedCue(uid, x, y, bmp, 1920, 1080)

    def test_overlap_stable_objects(self):
        a = self._mkrender(1, 100, 900, 200, 60)
        b = self._mkrender(2, 100, 100, 200, 60)
        tb = TimelineBuilder(1920, 1080)
        events = tb.build([
            TimedRender(0, 4000, a),
            TimedRender(2000, 6000, b),
        ])
        self.assertEqual(len(events), 3)
        # middle slice has 2 objects; cue A's object bitmap identical to
        # its solo slice (no jitter)
        mid = events[1]
        self.assertEqual(len(mid.objects), 2)
        solo_a = events[0].objects[0]
        both_a = [o for o in mid.objects if o.y == 900][0]
        self.assertTrue(np.array_equal(solo_a.bitmap, both_a.bitmap))
        self.assertEqual((solo_a.x, solo_a.y), (both_a.x, both_a.y))

    def test_overlapping_boxes_merge(self):
        a = self._mkrender(1, 100, 900, 300, 80)
        b = self._mkrender(2, 200, 940, 300, 80)   # overlaps a
        tb = TimelineBuilder(1920, 1080)
        events = tb.build([TimedRender(0, 2000, a), TimedRender(0, 2000, b)])
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0].objects), 1)
        o = events[0].objects[0]
        self.assertEqual((o.x, o.y), (100, 900))
        self.assertEqual(o.bitmap.shape[:2], (120, 400))

    def test_sup_bytes(self):
        doc = load_subtitle(sample('netflix_ja.ttml'))
        canvas = compute_canvas((1920, 1080), OverrideSet().layout)
        r = CueRenderer(doc, canvas)
        renders = []
        for cue in doc.cues:
            rc = r.render_cue(cue)
            if rc:
                renders.append(TimedRender(cue.begin_ms, cue.end_ms, rc))
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, 'out.sup')
            ok = write_sup_file(renders, out, 1920, 1080,
                                Fraction(24000, 1001))
            self.assertTrue(ok)
            data = open(out, 'rb').read()
        self.assertGreater(len(data), 1000)
        # walk all segments, verify structure and monotonic PTS per DS
        pos, seg_types, last_pts = 0, [], -1
        while pos < len(data):
            self.assertEqual(data[pos:pos + 2], b'PG')
            pts, _dts, st, size = struct.unpack('>IIBH', data[pos + 2:pos + 13])
            seg_types.append(st)
            if st == 0x16:
                self.assertGreaterEqual(pts, last_pts)
                last_pts = pts
            pos += 13 + size
        self.assertEqual(pos, len(data))
        for t in (0x14, 0x15, 0x16, 0x17, 0x80):
            self.assertIn(t, seg_types)

    def test_nonstandard_size(self):
        rc = self._mkrender(1, 10, 10, 100, 40)
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, 'odd.sup')
            ok = write_sup_file([TimedRender(0, 1000, rc)], out,
                                1440, 603, Fraction(25))
            self.assertTrue(ok)
            data = open(out, 'rb').read()
        # PCS carries the odd size verbatim
        w, h = struct.unpack('>HH', data[13:17])
        self.assertEqual((w, h), (1440, 603))

    def test_rle_roundtrip_regression(self):
        rng = np.random.default_rng(7)
        idx = rng.integers(0, 5, size=(33, 517)).astype(np.uint8)
        idx[:, 100:400] = 0
        rle = rle_encode(idx)
        # decode
        out = np.zeros_like(idx)
        row, col, i = 0, 0, 0
        while i < len(rle) and row < idx.shape[0]:
            b = rle[i]; i += 1
            if b != 0:
                out[row, col] = b; col += 1
                continue
            b2 = rle[i]; i += 1
            if b2 == 0:
                row += 1; col = 0
                continue
            if b2 < 0x40:
                run, val = b2, 0
            elif b2 < 0x80:
                run = ((b2 & 0x3F) << 8) | rle[i]; i += 1; val = 0
            elif b2 < 0xC0:
                run = b2 & 0x3F; val = rle[i]; i += 1
            else:
                run = ((b2 & 0x3F) << 8) | rle[i]; i += 1
                val = rle[i]; i += 1
            out[row, col:col + run] = val
            col += run
        self.assertTrue(np.array_equal(out, idx))


class TestExporters(unittest.TestCase):
    def test_ttml_roundtrip(self):
        doc = load_subtitle(sample('netflix_ja.ttml'))
        text = export_ttml(doc)
        doc2 = TTMLParser().parse_string(text)
        self.assertEqual(len(doc2.cues), len(doc.cues))
        self.assertEqual(set(doc2.styles) & set(doc.styles), set(doc.styles))
        self.assertAlmostEqual(doc2.cues[0].begin_ms, doc.cues[0].begin_ms,
                               delta=1.0)
        self.assertEqual(doc2.cues[0].plain_text(), doc.cues[0].plain_text())
        # vertical region survives
        self.assertTrue(any(r.is_vertical() for r in doc2.regions.values()))

    def test_vtt_export(self):
        doc = load_subtitle(sample('netflix_ja.ttml'))
        text = export_vtt(doc)
        self.assertTrue(text.startswith('WEBVTT'))
        doc2 = VTTParser().parse_string(text)
        self.assertEqual(len(doc2.cues), len(doc.cues))
        self.assertIn('<ruby>', text)
        self.assertIn('vertical:rl', text)

    def test_srt_export(self):
        doc = load_subtitle(sample('netflix_ja.ttml'))
        text = export_srt(doc)
        doc2 = SRTParser().parse_string(text)
        self.assertEqual(len(doc2.cues), len(doc.cues))
        self.assertIn('とうきょう', text)   # ruby flattened to base(reading)

    def test_project_roundtrip(self):
        doc = load_subtitle(sample('styled.vtt'))
        ov = OverrideSet()
        ov.ensure_language('ja').override_font_size = True
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'p.t2p')
            save_project(path, doc, ov, {'video_path': '/x/y.mkv'})
            doc2, ov2, extras = load_project(path)
        self.assertEqual(len(doc2.cues), len(doc.cues))
        self.assertEqual(doc2.cues[0].plain_text(), doc.cues[0].plain_text())
        self.assertEqual(set(doc2.regions), set(doc.regions))
        self.assertTrue(ov2.by_lang['ja'].override_font_size)
        self.assertEqual(extras['video_path'], '/x/y.mkv')


class TestPipelineAndQueue(unittest.TestCase):
    def test_pipeline_pause_resume(self):
        doc = load_subtitle(sample('netflix_ja.ttml'))
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, 'o.sup')
            pipe = RenderPipeline(doc, RenderSettings(out_path=out))
            pipe.pause_event.set()
            self.assertIsNone(pipe.run())          # paused immediately
            pipe.pause_event.clear()
            self.assertEqual(pipe.run(), out)      # resumes and finishes
            self.assertTrue(os.path.getsize(out) > 500)

    def test_queue_grouping_and_mux_gating(self):
        from ttml2pgs.core import jobqueue
        from ttml2pgs.core.jobqueue import QueueManager, JobState

        # stub remux so no mkvmerge/ffmpeg needed
        calls = []

        def fake_remux(video, subs, replace_original=True,
                       progress=None, cancel=None):
            calls.append((video, [s.path for s in subs]))
            return True, video

        orig = jobqueue.remux
        jobqueue.remux = fake_remux
        try:
            with tempfile.TemporaryDirectory() as td:
                video = os.path.join(td, 'ep1.mkv')
                open(video, 'wb').write(b'x')
                doc1 = load_subtitle(sample('netflix_ja.ttml'))
                doc2 = load_subtitle(sample('basic.srt'))
                q = QueueManager(state_path=os.path.join(td, 'q.json'))
                s1 = RenderSettings(out_path=os.path.join(td, 'ep1.ja.sup'))
                s2 = RenderSettings(out_path=os.path.join(td, 'ep1.en.sup'))
                q.add_render(doc1, 'a.ttml', s1, OverrideSet(),
                             video_path=video, lang='ja')
                q.add_render(doc2, 'b.srt', s2, OverrideSet(),
                             video_path=video, lang='en')
                # same video → one group with 2 jobs
                self.assertEqual(len(q.groups), 1)
                self.assertEqual(len(q.groups[0].render_jobs), 2)

                # jobs are added unstarted: the render worker must skip them
                self.assertIsNone(q._next_render())
                q.start_all()
                q.start()
                deadline = time.time() + 120
                while not q.is_idle() and time.time() < deadline:
                    time.sleep(0.1)
                q.shutdown(wait=True)

                g = q.groups[0]
                self.assertTrue(all(j.state == JobState.DONE
                                    for j in g.render_jobs))
                self.assertEqual(g.mux_state, JobState.DONE)
                # mux ran once, with both sups, only after both renders
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(calls[0][1]), 2)
                self.assertTrue(os.path.exists(s1.out_path))
                self.assertTrue(os.path.exists(s2.out_path))
        finally:
            jobqueue.remux = orig

    def test_unstarted_job_holds_group_mux(self):
        """Starting one of two jobs must not mux until the other resolves."""
        from ttml2pgs.core import jobqueue
        from ttml2pgs.core.jobqueue import QueueManager, JobState

        calls = []

        def fake_remux(video, subs, replace_original=True,
                       progress=None, cancel=None):
            calls.append([s.path for s in subs])
            return True, video

        orig = jobqueue.remux
        jobqueue.remux = fake_remux
        try:
            with tempfile.TemporaryDirectory() as td:
                video = os.path.join(td, 'ep1.mkv')
                open(video, 'wb').write(b'x')
                doc1 = load_subtitle(sample('basic.srt'))
                doc2 = load_subtitle(sample('basic.srt'))
                q = QueueManager(state_path=None)
                s1 = RenderSettings(out_path=os.path.join(td, 'a.sup'))
                s2 = RenderSettings(out_path=os.path.join(td, 'b.sup'))
                j1 = q.add_render(doc1, 'a.srt', s1, OverrideSet(),
                                  video_path=video, lang='en')
                j2 = q.add_render(doc2, 'b.srt', s2, OverrideSet(),
                                  video_path=video, lang='ja')
                q.start_job(j1.id)
                q.start()
                deadline = time.time() + 60
                while j1.state != JobState.DONE and time.time() < deadline:
                    time.sleep(0.05)
                self.assertEqual(j1.state, JobState.DONE)
                time.sleep(0.8)     # give a would-be premature mux a chance
                g = q.groups[0]
                self.assertEqual(g.mux_state, JobState.WAITING)
                self.assertEqual(g.unstarted_count(), 1)
                self.assertEqual(calls, [])
                # resolving the unstarted job (cancel) releases the mux
                q.cancel_job(j2.id)
                deadline = time.time() + 60
                while not q.is_idle() and time.time() < deadline:
                    time.sleep(0.05)
                q.shutdown(wait=True)
                self.assertEqual(g.mux_state, JobState.DONE)
                self.assertEqual(calls, [[s1.out_path]])
        finally:
            jobqueue.remux = orig

    def test_pause_resume_leave_unstarted_alone(self):
        from ttml2pgs.core.jobqueue import QueueManager
        q = QueueManager(state_path=None)     # workers never started
        s = RenderSettings(out_path='x.sup')
        j1 = q.add_render(None, sample('basic.srt'), s, OverrideSet())
        j2 = q.add_render(None, sample('basic.srt'), s, OverrideSet())
        q.start_job(j1.id)
        q.pause_all()
        q.resume_all()
        self.assertTrue(j1.started)
        self.assertFalse(j2.started)          # resume must not start it
        self.assertIsNotNone(q._next_render())
        self.assertIs(q._next_render(), j1)

    def test_queue_state_persistence(self):
        from ttml2pgs.core.jobqueue import QueueManager
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, 'q.json')
            q = QueueManager(state_path=state)
            sup = os.path.join(td, 'done.sup')
            open(sup, 'wb').write(b'PG')
            s = RenderSettings(out_path=sup)
            job = q.add_render(None, sample('basic.srt'), s, OverrideSet(),
                               video_path=None, lang='en')
            job2 = q.add_render(None, sample('basic.srt'),
                                RenderSettings(out_path=sup + '2'),
                                OverrideSet(), video_path=None, lang='ja')
            from ttml2pgs.core.jobqueue import JobState
            job.state = JobState.DONE
            job.started = True
            q._save_state()

            q2 = QueueManager(state_path=state)
            n = q2.load_state()
            self.assertEqual(n, 2)
            self.assertEqual(q2.groups[0].render_jobs[0].state, JobState.DONE)
            # started/unstarted survives the round-trip
            self.assertTrue(q2.groups[0].render_jobs[0].started)
            self.assertFalse(q2.groups[1].render_jobs[0].started)

    def test_video_match_streaming_names(self):
        """v1 matched on the first dot-token; names like
        id.jajp.Dialog.Subtitle.ttml must find id.mkv again."""
        from ttml2pgs.core.video import find_matching_video, subtitle_stem
        with tempfile.TemporaryDirectory() as td:
            sub = os.path.join(td, 'd1758520-_____.jajp.Dialog.Subtitle.ttml')
            vid = os.path.join(td, 'd1758520-_____.mkv')
            open(sub, 'w').close()
            open(vid, 'wb').close()
            self.assertEqual(find_matching_video(sub),
                             os.path.normpath(vid))
        # ordinary names still match on the full stripped stem
        with tempfile.TemporaryDirectory() as td:
            sub = os.path.join(td, 'Show.S01E01.ja.forced.ttml')
            vid = os.path.join(td, 'Show.S01E01.mkv')
            other = os.path.join(td, 'Show.S01E02.mkv')
            open(sub, 'w').close()
            open(vid, 'wb').close()
            open(other, 'wb').close()
            self.assertEqual(subtitle_stem(sub), 'Show.S01E01')
            self.assertEqual(find_matching_video(sub),
                             os.path.normpath(vid))

    def test_compact_language_tokens(self):
        from ttml2pgs.core.parsers import (detect_language_from_filename,
                                           normalize_language)
        self.assertEqual(
            detect_language_from_filename('x.jajp.Dialog.Subtitle.ttml'),
            'ja')
        self.assertEqual(detect_language_from_filename('x.enus.srt'), 'en')
        self.assertEqual(normalize_language('jajp'), 'ja')

    def test_vertical_shear_center_origin(self):
        """Sheared upright-vertical glyphs must shear about their center:
        GlyphBitmap.dy cancels the width-dependent column displacement."""
        import math
        from ttml2pgs.core.fonts import FontManager
        from ttml2pgs.core.layout import RunStyle, PlacedGlyph
        from ttml2pgs.core.raster import _glyph_cache
        fm = FontManager.instance()
        faces = fm.resolve_stack(['sans-serif'], lang='ja')
        rec = fm.face_covering(faces, '国')
        self.assertIsNotNone(rec, 'no ja font with 国 available')
        gid = fm.ft_face(rec.path, rec.index).get_char_index(ord('国'))

        def render(shear, axis='y', rot90=False):
            st = RunStyle(shear_deg=shear, shear_axis=axis)
            g = PlacedGlyph(face=rec, gid=gid, x=0, y=0, font_px=48.0,
                            style=st, rot90=rot90)
            return _glyph_cache.get(g, 0.0)

        sheared = render(15.0)
        self.assertIsNotNone(sheared)
        t = math.tan(math.radians(15.0))
        expect = t * (sheared.left + sheared.alpha.shape[1] / 2.0)
        self.assertAlmostEqual(sheared.dy, expect, places=3)
        self.assertGreater(sheared.dy, 1.0)     # meaningful correction
        # no correction without shear, and none for horizontal italics
        self.assertEqual(render(0.0).dy, 0.0)
        self.assertEqual(render(15.0, axis='x').dy, 0.0)

    def test_preferred_default_font(self):
        from ttml2pgs.core.fonts import FontManager
        fm = FontManager.instance()
        stack = fm.resolve_stack(['sans-serif'], lang='ja',
                                 preferred='WenQuanYi Zen Hei')
        if not stack:
            self.skipTest('no fonts installed')
        names = ' '.join(stack[0].families).lower()
        if not any('wenquanyi' in ' '.join(r.families).lower()
                   for r in stack):
            self.skipTest('WenQuanYi not installed')
        self.assertIn('wenquanyi', names,
                      'preferred font must head the generic resolution')

    def test_parse_style_refs(self):
        try:
            from ttml2pgs.ui.widgets.cue_table import parse_style_refs
        except ImportError:
            self.skipTest('PyQt6 not installed')
        doc = load_subtitle(sample('netflix_ja.ttml'))
        sids = sorted(doc.styles.keys())
        self.assertGreaterEqual(len(sids), 2)
        self.assertEqual(parse_style_refs(doc, f'{sids[0]} {sids[1]}'),
                         [sids[0], sids[1]])
        self.assertEqual(parse_style_refs(doc, 'default'), [])
        self.assertEqual(parse_style_refs(doc, ''), [])
        self.assertIsNone(parse_style_refs(doc, 'no_such_style'))

    def test_body_level_styles_reach_cues(self):
        """<body style=…> definitions cascade into every cue's refs."""
        ttml = '''<?xml version="1.0"?>
<tt xmlns="http://www.w3.org/ns/ttml"
    xmlns:tts="http://www.w3.org/ns/ttml#styling" xml:lang="en">
 <head><styling>
   <style xml:id="bodyStyle" tts:color="#00ff00"/>
 </styling></head>
 <body style="bodyStyle"><div>
   <p begin="0s" end="2s">hello</p>
 </div></body></tt>'''
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'b.ttml')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(ttml)
            doc = load_subtitle(path)
        cue = doc.cues[0]
        self.assertIn('bodyStyle', cue.style_refs)
        comp = doc.resolve_style([(cue.style_refs, cue.inline_style)],
                                 doc.get_region(cue))
        self.assertEqual(comp.color[:3], (0, 255, 0))

    def test_rename_style_cascades(self):
        doc = load_subtitle(sample('netflix_ja.ttml'))
        # find a style actually referenced by a cue
        used = next(sid for c in doc.cues for sid in c.style_refs)
        cue = next(c for c in doc.cues if used in c.style_refs)
        region = doc.get_region(cue)
        before = doc.resolve_style([(cue.style_refs, cue.inline_style)],
                                   region)
        self.assertTrue(doc.rename_style(used, 'renamed_style'))
        self.assertNotIn(used, doc.styles)
        self.assertIn('renamed_style', doc.styles)
        self.assertIn('renamed_style', cue.style_refs)
        self.assertNotIn(used, cue.style_refs)
        after = doc.resolve_style([(cue.style_refs, cue.inline_style)],
                                  region)
        self.assertEqual(before.color, after.color)
        self.assertEqual(before.font_size, after.font_size)
        # collision refused
        self.assertFalse(doc.rename_style('renamed_style',
                                          list(doc.styles.keys())[0]))

    def test_rename_region_cascades(self):
        doc = load_subtitle(sample('netflix_ja.ttml'))
        rid = next(c.region_id for c in doc.cues if c.region_id)
        n_refs = sum(1 for c in doc.cues if c.region_id == rid)
        self.assertTrue(doc.rename_region(rid, 'renamed_region'))
        self.assertNotIn(rid, doc.regions)
        self.assertEqual(
            sum(1 for c in doc.cues if c.region_id == 'renamed_region'),
            n_refs)

    def test_new_defaults(self):
        from ttml2pgs.core.overrides import StyleOverrides
        so = StyleOverrides()
        self.assertEqual(so.weight_boost, 3.0)
        self.assertTrue(so.auto_color)
        # auto-color engages by default: SDR videos get the SDR preset
        st = so.to_style(is_hdr=False)
        self.assertEqual(st.color, (229, 229, 229, 255))
        st = so.to_style(is_hdr=True)
        self.assertEqual(st.color, (161, 161, 161, 255))

    def test_player_command_builder(self):
        try:
            from ttml2pgs.ui.widgets.preview import build_player_command
        except ImportError:
            self.skipTest('PyQt6 not installed')
        cmd = build_player_command(
            r'C:\Program Files\MPC-BE\mpc-be64.exe',
            '"{file}" /start {ms}',
            r'D:\My Videos\Episode 01.mkv', 61500)
        # path with spaces stays ONE argv entry, quotes stripped
        self.assertEqual(cmd, [r'C:\Program Files\MPC-BE\mpc-be64.exe',
                               r'D:\My Videos\Episode 01.mkv',
                               '/start', '61500'])
        cmd = build_player_command('vlc', '--start-time={sec} "{file}"',
                                   '/tmp/a b.mkv', 61500)
        self.assertEqual(cmd, ['vlc', '--start-time=61.500', '/tmp/a b.mkv'])

    def test_mpv_overlay_conversion(self):
        try:
            from ttml2pgs.ui.widgets.mpv_player import _to_bgra_scaled
        except ImportError:
            self.skipTest('PyQt6 not installed')
        import numpy as np
        rgba = np.zeros((20, 40, 4), np.uint8)
        rgba[..., 0] = 200                      # R
        rgba[..., 3] = 128                      # 50% alpha
        out = _to_bgra_scaled(rgba, 80, 40)
        self.assertEqual(out.shape, (40, 80, 4))
        self.assertTrue(out.flags['C_CONTIGUOUS'])
        # premultiplied: R lands in BGRA[2] at ~200*0.5
        self.assertLessEqual(abs(int(out[20, 40, 2]) - 100), 2)
        self.assertLessEqual(abs(int(out[20, 40, 3]) - 128), 1)

    def test_mpv_engine_commands(self):
        """Exact command forms the embedded mpv widget issues (headless
        vo=null): load, exact seek, overlay-add/remove."""
        try:
            from ttml2pgs.ui.widgets.mpv_player import mpv_available
        except ImportError:
            self.skipTest('PyQt6 not installed')
        if not mpv_available():
            self.skipTest('libmpv not installed')
        import shutil
        if not shutil.which('ffmpeg'):
            self.skipTest('ffmpeg not available')
        import subprocess
        import numpy as np
        import mpv as mpvmod
        with tempfile.TemporaryDirectory() as td:
            vid = os.path.join(td, 't.mp4')
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
                            '-i', 'testsrc2=size=160x90:rate=24:duration=2',
                            '-pix_fmt', 'yuv420p', vid], check=True)
            m = mpvmod.MPV(vo='null', ao='null', pause=True,
                           keep_open='yes')
            try:
                m.loadfile(vid)
                deadline = time.time() + 20
                while not m.duration and time.time() < deadline:
                    time.sleep(0.05)
                self.assertAlmostEqual(float(m.duration), 2.0, delta=0.2)
                m.command('seek', 1.0, 'absolute+exact')
                time.sleep(0.4)
                self.assertAlmostEqual(float(m.time_pos), 1.0, delta=0.3)
                arr = np.ascontiguousarray(
                    np.full((30, 60, 4), 128, np.uint8))
                m.overlay_add(1, 10, 20, '&' + str(arr.ctypes.data), 0,
                              'bgra', 60, 30, 240)
                m.overlay_remove(1)
            finally:
                m.terminate()

    def test_ruby_preview_text_has_parens(self):
        try:
            from ttml2pgs.ui.widgets.cue_table import preview_text
        except ImportError:
            self.skipTest('PyQt6 not installed')
        doc = load_subtitle(sample('netflix_ja.ttml'))
        previews = [preview_text(doc, c) for c in doc.cues]
        joined = ''.join(previews)
        self.assertIn('(', joined)            # ruby readings visible again
        # parenthesised text must be the annotation, right after its base
        self.assertTrue(any('(' in p and ')' in p and
                            p.index('(') < p.index(')') for p in previews))
        # plain (non-ruby) cues keep their text intact
        srt_doc = load_subtitle(sample('basic.srt'))
        for cue in srt_doc.cues:
            self.assertEqual(preview_text(srt_doc, cue), cue.plain_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
