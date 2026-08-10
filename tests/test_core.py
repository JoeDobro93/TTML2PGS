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
        keep their size. Now PER LANGUAGE: the ja set's padding applies
        to ja cues; a different language's padding must not."""
        cue = next(c for c in self.doc.sorted_cues()
                   if not self.doc.get_region(c).is_vertical())
        plain = self.renderer.render_cue(cue)

        ov = OverrideSet()
        so = ov.ensure_language('ja')
        so.use_padding = True
        so.padding_v = 10.0                 # 5% inset per edge
        so.padding_h = 10.0
        canvas = compute_canvas((1920, 1080), ov.layout)
        self.assertEqual(canvas.content_h, 1080.0)          # unshrunk
        rp = CueRenderer(self.doc, canvas, ov)
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

        # padding on a DIFFERENT language must not move this ja cue
        ov2 = OverrideSet()
        en = ov2.ensure_language('en')
        en.use_padding = True
        en.padding_v = en.padding_h = 10.0
        other = CueRenderer(self.doc, canvas, ov2).render_cue(cue)
        self.assertEqual((plain.x, plain.y), (other.x, other.y))

    def test_line_spacing_multiplier(self):
        """Per-language line spacing tightens/widens multi-line cues;
        it floors at glyph height and NEVER eats the furigana reserve."""
        with tempfile.TemporaryDirectory() as td:
            def make(name, body):
                p = os.path.join(td, name)
                with open(p, 'w', encoding='utf-8') as f:
                    f.write('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n'
                            + body + '\n')
                d = load_subtitle(p)
                d.language = 'ja'
                for c in d.cues:
                    c.lang = 'ja'
                return d

            ruby_doc = make('r.ja.vtt', 'こんにちは世界\n東京(とうきょう)へ行く')
            plain_doc = make('p.ja.vtt', 'こんにちは世界\n東京へ行く')
            canvas = compute_canvas((1920, 1080), OverrideSet().layout)

            def height(doc, spacing):
                ov = OverrideSet()
                ov.by_lang[''].line_spacing = spacing
                rc = CueRenderer(doc, canvas, ov).render_cue(doc.cues[0])
                return rc.height

            # multiplier works both ways
            self.assertLess(height(plain_doc, 0.8),
                            height(plain_doc, 1.0))
            self.assertLess(height(plain_doc, 1.0),
                            height(plain_doc, 1.4))
            # floor: below glyph height nothing shrinks further
            self.assertEqual(height(plain_doc, 0.05),
                             height(plain_doc, 0.2))
            # furigana reserve survives maximum tightening: the ruby
            # version stays taller than the identical plain text
            self.assertGreater(height(ruby_doc, 0.5),
                               height(plain_doc, 0.5) + 8)

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

    def test_overlap_slicing_keeps_earlier_cue_visible(self):
        """Time-overlapping cues are sliced into intervals showing ALL
        active cues — a later cue must never cancel an earlier one
        (the classic PGS pitfall). Canonical scenario:
        cue1 2–15s, cue2 3.5–25s, cue3 20.6–24.5s."""
        c1 = self._mkrender(1, 860, 950, 200, 40)
        c2 = self._mkrender(2, 860, 880, 200, 40)
        c3 = self._mkrender(3, 860, 100, 200, 40)
        events = TimelineBuilder(1920, 1080).build([
            TimedRender(2000, 15000, c1),
            TimedRender(3500, 25000, c2),
            TimedRender(20600, 24500, c3),
        ], snap_fps=None)

        def visible(ev):
            out = set()
            for o in ev.objects:
                h = o.bitmap.shape[0]
                for y, uid in ((950, 1), (880, 2), (100, 3)):
                    if o.y <= y < o.y + h:
                        out.add(uid)
            return out

        got = [(ev.start_ms, ev.end_ms, visible(ev)) for ev in events]
        self.assertEqual(got, [
            (2000.0, 3500.0, {1}),
            (3500.0, 15000.0, {1, 2}),
            (15000.0, 20600.0, {2}),
            (20600.0, 24500.0, {2, 3}),
            (24500.0, 25000.0, {2}),
        ])
        # the long cue keeps one byte-identical bitmap at one position
        # through all five slices (no shimmer across boundaries)
        c2_objs = [o for ev in events[1:] for o in ev.objects
                   if o.y <= 880 < o.y + o.bitmap.shape[0]]
        self.assertEqual(len(c2_objs), 4)
        for o in c2_objs[1:]:
            self.assertEqual((o.x, o.y), (c2_objs[0].x, c2_objs[0].y))
            self.assertTrue(np.array_equal(o.bitmap, c2_objs[0].bitmap))

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
        j1 = q.add_render(None, sample('basic.srt'),
                          RenderSettings(out_path='x.sup'), OverrideSet())
        j2 = q.add_render(None, sample('basic.srt'),
                          RenderSettings(out_path='y.sup'), OverrideSet())
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

            # an edited mux track name persists with the queue
            q.set_track_name(job2.id, 'ja-en')
            q2 = QueueManager(state_path=state)
            n = q2.load_state()
            # the finished no-video group clears itself on reload; the
            # unstarted one comes back still unstarted
            self.assertEqual(n, 1)
            self.assertEqual(len(q2.groups), 1)
            self.assertEqual(q2.groups[0].render_jobs[0].state,
                             JobState.PENDING)
            self.assertFalse(q2.groups[0].render_jobs[0].started)
            self.assertEqual(q2.groups[0].render_jobs[0].track_name,
                             'ja-en')

    def test_failed_mux_runs_once_and_others_proceed(self):
        """Batch regression: a failing mux must go FAILED after ONE
        attempt (no instant re-pick loop) and must not starve the other
        groups' muxes. retry_mux re-arms it explicitly; the before_mux
        hook fires so the UI can release player file handles."""
        from ttml2pgs.core import jobqueue
        from ttml2pgs.core.jobqueue import QueueManager, JobState

        calls: list = []
        fail_ep1 = {'on': True}

        def fake_remux(video, subs, replace_original=True,
                       progress=None, cancel=None):
            calls.append(video)
            if 'ep1' in os.path.basename(video) and fail_ep1['on']:
                return False, 'finalize failed: locked'
            return True, video

        orig = jobqueue.remux
        jobqueue.remux = fake_remux
        try:
            with tempfile.TemporaryDirectory() as td:
                v1 = os.path.join(td, 'ep1.mkv')
                v2 = os.path.join(td, 'ep2.mkv')
                for v in (v1, v2):
                    open(v, 'wb').write(b'x')
                q = QueueManager(state_path=os.path.join(td, 'q.json'))
                released: list = []
                q.before_mux = released.append
                s1 = RenderSettings(out_path=os.path.join(td, 'ep1.en.sup'))
                s2 = RenderSettings(out_path=os.path.join(td, 'ep2.en.sup'))
                q.add_render(load_subtitle(sample('basic.srt')), 'a.srt',
                             s1, OverrideSet(), video_path=v1, lang='en',
                             start=True)
                q.add_render(load_subtitle(sample('basic.srt')), 'b.srt',
                             s2, OverrideSet(), video_path=v2, lang='en',
                             start=True)
                q.start()
                g1, g2 = q.snapshot()
                deadline = time.time() + 90
                while time.time() < deadline and not (
                        g1.mux_state == JobState.FAILED and
                        g2.mux_state == JobState.DONE):
                    time.sleep(0.05)
                self.assertEqual(g1.mux_state, JobState.FAILED)
                self.assertEqual(g2.mux_state, JobState.DONE,
                                 'other groups must still mux')
                # give a broken re-pick loop time to expose itself
                time.sleep(0.6)
                n_ep1 = sum(1 for c in calls
                            if 'ep1' in os.path.basename(c))
                self.assertEqual(n_ep1, 1, 'failed mux must not loop')
                self.assertIn(v1, released)
                self.assertIn(v2, released)

                fail_ep1['on'] = False
                q.retry_mux(g1.id)
                deadline = time.time() + 30
                while time.time() < deadline and \
                        g1.mux_state != JobState.DONE:
                    time.sleep(0.05)
                self.assertEqual(g1.mux_state, JobState.DONE)
                q.shutdown(wait=True)
        finally:
            jobqueue.remux = orig

    def test_mux_finalize_fallback_when_video_locked(self):
        """Windows-style lock on the original video (player holds it
        open): finalize must fall back to *.muxed.mkv and report
        success instead of failing — and never lose the mux output."""
        import ttml2pgs.core.video as vid
        with tempfile.TemporaryDirectory() as td:
            video = os.path.join(td, 'ep.mkv')
            open(video, 'wb').write(b'ORIG')
            tmp = os.path.join(td, 'ep.t2p_mux.mkv')
            open(tmp, 'wb').write(b'MUXED')

            real_remove, real_replace = os.remove, os.replace

            def locked(path):
                return os.path.abspath(str(path)) == os.path.abspath(video)

            def rm(path, *a, **k):
                if locked(path):
                    raise PermissionError(13, 'held by player', path)
                return real_remove(path, *a, **k)

            def rp(src, dst, *a, **k):
                if locked(dst):
                    raise PermissionError(13, 'held by player', dst)
                return real_replace(src, dst, *a, **k)

            vid.os.remove, vid.os.replace = rm, rp
            try:
                ok, res = vid._finalize_mux(tmp, video, video, True,
                                            delays=(0.0, 0.0))
            finally:
                vid.os.remove, vid.os.replace = real_remove, real_replace
            self.assertTrue(ok, f'fallback should succeed, got: {res}')
            self.assertTrue(res.endswith('.muxed.mkv'))
            self.assertEqual(open(res, 'rb').read(), b'MUXED')
            self.assertEqual(open(video, 'rb').read(), b'ORIG')

            # unlocked: replace-original works normally
            tmp2 = os.path.join(td, 'ep2.t2p_mux.mkv')
            v2 = os.path.join(td, 'ep2.mkv')
            open(tmp2, 'wb').write(b'M2')
            open(v2, 'wb').write(b'O2')
            ok, res = vid._finalize_mux(tmp2, v2, v2, True, delays=(0.0,))
            self.assertTrue(ok)
            self.assertEqual(res, v2)
            self.assertEqual(open(v2, 'rb').read(), b'M2')

    def test_queue_pane_inplace_refresh_and_aggregates(self):
        """The queue tree must refresh IN PLACE: selection (and the
        items themselves) survive progress refreshes. Group rows carry
        aggregate State/Progress so a collapsed group stays readable."""
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            self.skipTest('PyQt6 not installed')
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])  # noqa: F841
        from ttml2pgs.ui.widgets.queue_view import QueuePane
        from ttml2pgs.core.jobqueue import QueueManager, JobState

        with tempfile.TemporaryDirectory() as td:
            v1 = os.path.join(td, 'ep1.mkv')
            open(v1, 'wb').write(b'x')
            q = QueueManager()                 # never started: no threads
            doc = load_subtitle(sample('basic.srt'))
            s1 = RenderSettings(out_path=os.path.join(td, 'ep1.a.sup'))
            s2 = RenderSettings(out_path=os.path.join(td, 'ep1.b.sup'))
            j1 = q.add_render(doc, 'a.srt', s1, OverrideSet(),
                              video_path=v1, lang='ja')
            j2 = q.add_render(doc, 'b.srt', s2, OverrideSet(),
                              video_path=v1, lang='en')

            pane = QueuePane(q, app_settings={})
            pane.refresh()
            gi = pane.tree.topLevelItem(0)
            self.assertEqual(pane.tree.topLevelItemCount(), 1)
            self.assertEqual(gi.childCount(), 2)
            self.assertEqual(gi.text(1), '0/2 · 2 added')
            self.assertEqual(gi.text(2), '0%')

            # select both jobs, then simulate progress refreshes
            c1, c2 = gi.child(0), gi.child(1)
            c1.setSelected(True)
            c2.setSelected(True)
            j1.progress = 0.5
            pane.refresh()
            pane.refresh()
            self.assertIs(gi, pane.tree.topLevelItem(0),
                          'items must be updated in place, not rebuilt')
            self.assertIs(c1, gi.child(0))
            self.assertTrue(c1.isSelected() and c2.isSelected(),
                            'selection must survive refresh')
            self.assertEqual(gi.text(2), '25%')     # (0.5 + 0) / 2

            # finished group: aggregate flips to done/100%
            for j in (j1, j2):
                j.state = JobState.DONE
                j.progress = 1.0
            g = q.snapshot()[0]
            g.mux_state = JobState.DONE
            pane.refresh()
            self.assertEqual(gi.text(1), '2/2 · done')
            self.assertEqual(gi.text(2), '100%')

            # replace-original toggle shows the delivery hint
            q.set_group_replace(g.id, False)
            pane.refresh()
            self.assertIn('*.muxed.mkv', gi.text(3))
            self.assertFalse(g.replace_original)

            # group + one job selected: job ids dedup to the group's set
            gi.setSelected(True)
            self.assertEqual(sorted(pane._selected_job_ids()),
                             sorted([j1.id, j2.id]))

            # Del removes the selection (group selected → whole group)
            pane._remove_selected()
            pane.refresh()
            self.assertEqual(pane.tree.topLevelItemCount(), 0)
            self.assertEqual(len(q.snapshot()), 0)

    def test_queue_pane_checkboxes_and_selection_rules(self):
        """Checkbox column reflects/drives engine arming; selection is
        kind-constrained (parents with parents, children within one
        group)."""
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            self.skipTest('PyQt6 not installed')
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])  # noqa: F841
        from ttml2pgs.ui.widgets.queue_view import QueuePane
        from ttml2pgs.core.jobqueue import QueueManager

        with tempfile.TemporaryDirectory() as td:
            v1, v2 = (os.path.join(td, n) for n in ('e1.mkv', 'e2.mkv'))
            q = QueueManager()
            doc = load_subtitle(sample('basic.srt'))

            def add(video, name):
                return q.add_render(
                    doc, name, RenderSettings(
                        out_path=os.path.join(td, name + '.sup')),
                    OverrideSet(), video_path=video, lang='en')
            j1, j2 = add(v1, 'a'), add(v1, 'b')
            j3 = add(v2, 'c')
            pane = QueuePane(q, app_settings={})
            pane.refresh()
            g1i, g2i = (pane.tree.topLevelItem(i) for i in (0, 1))

            # rows start checked; unchecking a row updates the engine
            self.assertEqual(g1i.checkState(0), Qt.CheckState.Checked)
            g1i.child(1).setCheckState(0, Qt.CheckState.Unchecked)
            self.assertFalse(j2.checked)
            g2i.setCheckState(0, Qt.CheckState.Unchecked)
            self.assertFalse(q.snapshot()[1].checked)

            # child selection is constrained to ONE group (the prune is
            # deferred a tick — see _constrain_selection)
            pane.tree.clearSelection()
            pane.tree.setCurrentItem(g1i.child(0))
            g1i.child(0).setSelected(True)
            g2i.child(0).setSelected(True)      # other video's child
            app.processEvents()
            self.assertNotIn(g2i.child(0), pane.tree.selectedItems(),
                             'cross-group child selection must prune')
            self.assertIn(g1i.child(0), pane.tree.selectedItems())
            # parent selection never mixes with children
            pane.tree.clearSelection()
            pane.tree.setCurrentItem(g1i)
            g1i.setSelected(True)
            g1i.child(0).setSelected(True)
            app.processEvents()
            self.assertNotIn(g1i.child(0), pane.tree.selectedItems(),
                             'parent+child selection must prune')
            q.shutdown()

    def test_queue_reload_drops_completed_keeps_statuses(self):
        """Reopening the app: fully-finished groups disappear; the rest
        come back in their LAST state (failed stays failed with its
        error — never a phantom re-run)."""
        from ttml2pgs.core.jobqueue import QueueManager, JobState
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, 'q.json')
            done_sup = os.path.join(td, 'ep1.done.sup')
            open(done_sup, 'wb').write(b'x')
            v1 = os.path.join(td, 'ep1.mkv')
            v2 = os.path.join(td, 'ep2.mkv')

            q = QueueManager(state_path=state)
            doc = load_subtitle(sample('basic.srt'))
            # group 1: everything done + muxed → must vanish on reload
            jd = q.add_render(doc, 'a.srt',
                              RenderSettings(out_path=done_sup),
                              OverrideSet(), video_path=v1, lang='en')
            jd.state = JobState.DONE
            g1 = q.snapshot()[0]
            g1.mux_state = JobState.DONE
            # group 2: one failed, one never started
            jf = q.add_render(doc, 'b.srt',
                              RenderSettings(
                                  out_path=os.path.join(td, 'ep2.a.sup')),
                              OverrideSet(), video_path=v2, lang='ja')
            jf.state = JobState.FAILED
            jf.error = 'font exploded'
            jf.started = True
            jp = q.add_render(doc, 'c.srt',
                              RenderSettings(
                                  out_path=os.path.join(td, 'ep2.b.sup')),
                              OverrideSet(), video_path=v2, lang='en')
            jp.checked = False
            q._save_state()

            q2 = QueueManager(state_path=state)
            n = q2.load_state()
            self.assertEqual(n, 2)
            groups = q2.snapshot()
            self.assertEqual(len(groups), 1, 'completed group must clear')
            g = groups[0]
            self.assertEqual(g.video_path, v2)
            self.assertEqual(g.render_jobs[0].state, JobState.FAILED)
            self.assertEqual(g.render_jobs[0].error, 'font exploded')
            self.assertEqual(g.render_jobs[1].state, JobState.PENDING)
            self.assertFalse(g.render_jobs[1].started)
            self.assertFalse(g.render_jobs[1].checked)

    def test_queue_checkbox_gating(self):
        """MakeMKV model: Render all arms only checked jobs in checked
        groups; group start skips unchecked jobs; check_all flips all."""
        from ttml2pgs.core.jobqueue import QueueManager
        with tempfile.TemporaryDirectory() as td:
            v1 = os.path.join(td, 'ep1.mkv')
            v2 = os.path.join(td, 'ep2.mkv')
            q = QueueManager()
            doc = load_subtitle(sample('basic.srt'))

            def add(video, name):
                return q.add_render(
                    doc, name, RenderSettings(
                        out_path=os.path.join(td, name + '.sup')),
                    OverrideSet(), video_path=video, lang='en')
            a1, a2 = add(v1, 'a1'), add(v1, 'a2')
            b1 = add(v2, 'b1')
            g1, g2 = q.snapshot()

            q.set_job_checked(a2.id, False)     # a2 sits out
            q.set_group_checked(g2.id, False)   # whole ep2 sits out
            q.start_all()
            self.assertTrue(a1.started)
            self.assertFalse(a2.started, 'unchecked job must sit out')
            self.assertFalse(b1.started, 'unchecked group must sit out')

            # explicit group start honors job checkboxes only
            q.set_group_checked(g2.id, True)
            q.set_job_checked(b1.id, False)
            q.start_group(g2.id)
            self.assertFalse(b1.started)
            q.check_all_jobs(g2.id, True)
            self.assertTrue(b1.checked)
            q.start_group(g2.id)
            self.assertTrue(b1.started)
            q.shutdown()

    def test_settings_migration_v4_padding_per_language(self):
        """v3 settings with layout padding migrate it into the Default
        language set."""
        with tempfile.TemporaryDirectory() as td:
            old_cfg = os.environ.get('XDG_CONFIG_HOME')
            os.environ['XDG_CONFIG_HOME'] = td
            try:
                from ttml2pgs.ui.state import AppState, config_dir
                os.makedirs(config_dir(), exist_ok=True)
                import json as _json
                ov = OverrideSet()
                ov.layout.use_padding = True
                ov.layout.padding_v = 12.0
                ov.layout.padding_h = 6.0
                with open(os.path.join(config_dir(), 'settings.json'),
                          'w', encoding='utf-8') as f:
                    _json.dump({'version': 3, 'settings': {},
                                'overrides': ov.to_dict()}, f)
                st = AppState()
                st.load_settings()
                base = st.overrides.by_lang['']
                self.assertTrue(base.use_padding)
                self.assertEqual(base.padding_v, 12.0)
                self.assertEqual(base.padding_h, 6.0)
                self.assertFalse(st.overrides.layout.use_padding)
            finally:
                if old_cfg is None:
                    os.environ.pop('XDG_CONFIG_HOME', None)
                else:
                    os.environ['XDG_CONFIG_HOME'] = old_cfg

    def test_requeue_same_output_replaces(self):
        """Queuing a subtitle whose output is already queued REPLACES
        the old job (settings may have changed) instead of duplicating."""
        from ttml2pgs.core.jobqueue import QueueManager
        with tempfile.TemporaryDirectory() as td:
            v = os.path.join(td, 'ep.mkv')
            q = QueueManager()
            doc = load_subtitle(sample('basic.srt'))
            out = os.path.join(td, 'ep.en.sup')
            q.add_render(doc, 'a.srt', RenderSettings(out_path=out),
                         OverrideSet(), video_path=v, lang='en')
            s2 = RenderSettings(out_path=out, offset_ms=250.0)
            j2 = q.add_render(doc, 'a.srt', s2, OverrideSet(),
                              video_path=v, lang='en')
            groups = q.snapshot()
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(groups[0].render_jobs), 1)
            self.assertIs(groups[0].render_jobs[0], j2)
            self.assertEqual(groups[0].render_jobs[0].settings.offset_ms,
                             250.0)
            q.shutdown()

    def test_launcher_assets(self):
        """Standalone-launch support: the app icon exists and loads,
        make_shortcut points at real files, the PyInstaller spec and
        entry point are wired for frozen multiprocessing."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        icon = os.path.join(root, 'resources', 'icon.ico')
        self.assertTrue(os.path.exists(icon))
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            self.skipTest('PyQt6 not installed')
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtGui import QImage
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])  # noqa: F841
        self.assertFalse(QImage(icon).isNull(), 'icon must be loadable')

        import make_shortcut
        self.assertTrue(os.path.exists(make_shortcut.SCRIPT))
        self.assertTrue(os.path.exists(make_shortcut.ICON))

        spec = os.path.join(root, 'ttml2pgs.spec')
        self.assertTrue(os.path.exists(spec))
        # the spawn-based worker pool needs freeze_support in the entry
        src = open(os.path.join(root, 'ttml2pgs', '__main__.py'),
                   encoding='utf-8').read()
        self.assertIn('freeze_support', src)
        # IDE launches refresh the exe; the frozen exe must not
        gui_src = open(os.path.join(root, 'run_gui.py'),
                       encoding='utf-8').read()
        self.assertIn('make_exe', gui_src)
        self.assertIn('frozen', gui_src)

    def test_make_exe_fingerprint(self):
        """The auto-build stamp: stable across calls, changes when a
        source file changes, restores when reverted."""
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            'make_exe', os.path.join(root, 'make_exe.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fp1 = mod.fingerprint()
        self.assertEqual(fp1, mod.fingerprint())
        target = os.path.join(root, 'ttml2pgs', '__init__.py')
        st = os.stat(target)
        try:
            os.utime(target, ns=(st.st_atime_ns,
                                 st.st_mtime_ns + 1_000_000))
            self.assertNotEqual(fp1, mod.fingerprint())
        finally:
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(fp1, mod.fingerprint())

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
        # vertical flow renders the INVERTED shear sign (authored 15°
        # leans the same way it does horizontally)
        expect = -t * (sheared.left + sheared.alpha.shape[1] / 2.0)
        self.assertAlmostEqual(sheared.dy, expect, places=3)
        self.assertLess(sheared.dy, -1.0)       # meaningful correction
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

    def test_main_window_constructs_offscreen(self):
        """GUI smoke test: the whole MainWindow builds headless and the
        queue dock helper doesn't recurse (regression: 2.0.x sed bug made
        _show_queue call itself)."""
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            self.skipTest('PyQt6 not installed')
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        with tempfile.TemporaryDirectory() as td:
            old_cfg = os.environ.get('XDG_CONFIG_HOME')
            os.environ['XDG_CONFIG_HOME'] = td
            try:
                from PyQt6.QtWidgets import QApplication
                app = QApplication.instance() or QApplication([])
                from ttml2pgs.ui.main_window import MainWindow
                win = MainWindow()
                try:
                    win._show_queue()
                    self.assertFalse(win.queue_dock.isHidden())
                    win._show_queue()          # must be idempotent
                finally:
                    win.queue.shutdown()
                    win.preview_pane.shutdown_players()
                del app
            except Exception:
                raise
            finally:
                if old_cfg is None:
                    os.environ.pop('XDG_CONFIG_HOME', None)
                else:
                    os.environ['XDG_CONFIG_HOME'] = old_cfg

    def test_cue_markup_roundtrip(self):
        """Token markup: cue → text → tree must be canonical (idempotent)
        and render-identical, ruby atoms included."""
        try:
            from ttml2pgs.ui.widgets.cue_editor import CueMarkup
        except ImportError:
            self.skipTest('PyQt6 not installed')
        for name in ('netflix_ja.ttml', 'styled.vtt', 'basic.srt'):
            doc = load_subtitle(sample(name))
            for cue in doc.cues:
                mk = CueMarkup.from_cue(doc, cue)
                tree, reason = mk.to_tree(mk.text)
                self.assertIsNotNone(tree, f'{name}: {reason}')
                rebuilt = cue.copy()
                rebuilt.root = tree
                mk2 = CueMarkup.from_cue(doc, rebuilt)
                self.assertEqual(mk2.text, mk.text,
                                 f'{name}: markup not canonical')
        # render equivalence on a ruby cue
        doc = load_subtitle(sample('netflix_ja.ttml'))
        canvas = compute_canvas((1920, 1080), OverrideSet().layout)
        r = CueRenderer(doc, canvas, OverrideSet())
        cue = doc.cues[0]
        before = r.render_cue(cue)
        mk = CueMarkup.from_cue(doc, cue)
        cue.root = mk.to_tree(mk.text)[0]
        after = r.render_cue(cue)
        self.assertEqual((before.x, before.y), (after.x, after.y))
        self.assertTrue(np.array_equal(before.bitmap, after.bitmap),
                        'render changed after markup round-trip')

    def test_cue_markup_overlap_and_validation(self):
        try:
            from ttml2pgs.ui.widgets.cue_editor import CueMarkup
        except ImportError:
            self.skipTest('PyQt6 not installed')
        from ttml2pgs.core.model import (Cue as MCue, SpanNode, Style,
                                         SubtitleDocument)
        doc = SubtitleDocument()
        doc.styles['a'] = Style(id='a', color=(255, 0, 0, 255))
        doc.styles['b'] = Style(id='b', font_weight='bold')
        mk = CueMarkup(doc=doc)

        # overlap normalizes into nested spans (HTML semantics)
        tree, reason = mk.to_tree('⟦axx ⟦byy a⟧zz b⟧')
        self.assertIsNotNone(tree, reason)
        cue = MCue(begin_ms=0, end_ms=1000)
        cue.root = tree
        canonical = CueMarkup.from_cue(doc, cue).text
        self.assertEqual(canonical, '⟦axx ⟦byy b⟧a⟧⟦bzz b⟧')
        self.assertEqual(cue.plain_text(), 'xx yy zz ')

        # b/i pseudo styles map to inline bold/italic spans
        tree, _ = mk.to_tree('⟦bhi b⟧ and ⟦ithere i⟧')
        spans = [n for n in tree.children if n.kind == 'span']
        self.assertEqual(spans[0].inline_style.font_weight, 'bold')
        self.assertEqual(spans[1].inline_style.font_style, 'italic')

        # invalid states are rejected with reasons
        for bad in ('⟦a x',                 # never closed
                    'a⟧ x ⟦a',              # end before start
                    '⟦a ⟦a x a⟧ a⟧',        # self-nesting
                    '⟦zz x zz⟧',            # unknown style
                    'stray ⟧ here'):
            tree, reason = mk.to_tree(bad)
            self.assertIsNone(tree, f'{bad!r} should be invalid')
            self.assertTrue(reason)

        # literal ⟦ in subtitle text is escaped and survives
        cue2 = MCue(begin_ms=0, end_ms=1000)
        cue2.root = SpanNode(kind='root')
        cue2.root.children.append(SpanNode.text_node('x⟦y⟧z'))
        mk2 = CueMarkup.from_cue(doc, cue2)
        tree2, reason2 = mk2.to_tree(mk2.text)
        self.assertIsNotNone(tree2, reason2)
        rebuilt = MCue(begin_ms=0, end_ms=1000)
        rebuilt.root = tree2
        self.assertEqual(rebuilt.plain_text(), 'x⟦y⟧z')

    def test_cjk_normal_targets_medium_weight(self):
        """CJK 'normal' picks a 500-weight face when available (Chrome's
        Yu Gothic Medium behavior); Latin keeps 400."""
        from ttml2pgs.core.fonts import FaceRecord, FontManager
        fm = FontManager()                       # fresh, no scan
        recs = [FaceRecord(path='/fake/yu-r.ttf', index=0,
                           families=['Yu Gothic'], weight=400),
                FaceRecord(path='/fake/yu-m.ttf', index=0,
                           families=['Yu Gothic'], weight=500),
                FaceRecord(path='/fake/yu-b.ttf', index=0,
                           families=['Yu Gothic'], weight=700)]
        fm.records = recs
        for r in recs:
            for fam in r.families:
                fm.by_family.setdefault('yugothic', []).append(r)
        ja = fm.resolve_stack(['Yu Gothic'], lang='ja')
        self.assertEqual(ja[0].weight, 500)
        en = fm.resolve_stack(['Yu Gothic'], lang='en')
        self.assertEqual(en[0].weight, 400)
        bold = fm.resolve_stack(['Yu Gothic'], lang='ja', weight='bold')
        self.assertEqual(bold[0].weight, 700)

    def test_cjk_medium_wins_across_families(self):
        """The common Windows setup: Noto JP Regular installed (heads the
        old stack) + Yu Gothic Medium — generic ja text must use the
        Medium face, not Noto Regular; bold must use the true Bold."""
        from ttml2pgs.core.fonts import FaceRecord, FontManager, _norm_family
        fm = FontManager()
        recs = [
            FaceRecord(path='/fake/noto-r.otf', index=0,
                       families=['Noto Sans JP'], weight=400),
            FaceRecord(path='/fake/yu-m.ttc', index=0,
                       families=['Yu Gothic', 'Yu Gothic Medium'],
                       weight=500),
            FaceRecord(path='/fake/yu-b.ttc', index=0,
                       families=['Yu Gothic', 'Yu Gothic Bold'],
                       weight=700),
        ]
        fm.records = recs
        for r in recs:
            for fam in r.families:
                fm.by_family.setdefault(_norm_family(fam), []).append(r)
        normal = fm.resolve_stack(['sans-serif'], lang='ja')
        self.assertEqual(normal[0].weight, 500,
                         'Medium must lead the ja stack across families')
        # bold: family order still wins (CSS semantics — synth bold on a
        # regular-only family), but Medium families move BEHIND the base
        # families and within a family the true Bold outranks Medium
        bold = fm.resolve_stack(['sans-serif'], lang='ja', weight='bold')
        self.assertEqual(bold[0].weight, 400)        # Noto + synth bold
        first_yu = next(r for r in bold
                        if 'Yu Gothic' in r.families)
        self.assertEqual(first_yu.weight, 700)

    def test_vertical_dash_rotates_without_vert_alt(self):
        """A ー/— style bar in vertical flow must not lie horizontally
        across the column when the font has no vert alternate."""
        from ttml2pgs.core.layout import (LayoutEngine, TextItem, RunStyle,
                                          _ROTATE_IF_NO_VERT_ALT)
        from ttml2pgs.core.fonts import FontManager
        self.assertIn('ー', _ROTATE_IF_NO_VERT_ALT)
        fm = FontManager.instance()
        rs = RunStyle(font_px=48.0, lang='ja')
        rs.faces = fm.resolve_stack(['sans-serif'], lang='ja')
        eng = LayoutEngine()
        atoms_lines = eng._items_to_atom_lines(
            [TextItem('テー', rs)], vertical=True)
        atoms = atoms_lines[0]
        dash = atoms[-1]
        # whichever way it resolved, the ink must be TALLER than wide
        # (rotated bar or a proper vertical alternate — never horizontal)
        res = eng.layout([TextItem('テー', rs)], 500, vertical=True)
        self.assertGreater(res.height, res.width * 1.5)

    def test_no_auto_language_sets_and_migration(self):
        """Opening a file must not clone Default into a language set;
        saved identical clones from older builds are dropped on load."""
        from ttml2pgs.ui.state import AppState
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get('XDG_CONFIG_HOME')
            os.environ['XDG_CONFIG_HOME'] = td
            try:
                st = AppState()
                st.open_subtitle(sample('netflix_ja.ttml'))
                self.assertEqual(set(st.overrides.by_lang), {''},
                                 'opening a file must not create a '
                                 'language override set')
                # migration: identical 'ja' clone dropped, modified kept
                st.overrides.ensure_language('ja')
                st.overrides.ensure_language('en')
                st.overrides.by_lang['en'].weight_boost = 7.0
                st.save_settings()
                st2 = AppState()
                st2.load_settings()
                self.assertNotIn('ja', st2.overrides.by_lang)
                self.assertIn('en', st2.overrides.by_lang)
                self.assertEqual(st2.overrides.by_lang['en'].weight_boost,
                                 7.0)
            finally:
                if old is None:
                    os.environ.pop('XDG_CONFIG_HOME', None)
                else:
                    os.environ['XDG_CONFIG_HOME'] = old

    def test_emphasis_mark_outline_ring(self):
        """Emphasis dots get a matching round outline UNDER the mark."""
        ttml = '''<?xml version="1.0"?>
<tt xmlns="http://www.w3.org/ns/ttml"
    xmlns:tts="http://www.w3.org/ns/ttml#styling" xml:lang="ja">
 <body><div>
  <p begin="0s" end="2s"><span tts:textEmphasis="filled dot before">
点々</span></p>
 </div></body></tt>'''
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'e.ttml')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(ttml)
            doc = load_subtitle(path)
        ov = OverrideSet()
        so = ov.by_lang['']
        so.override_outline = True
        so.outline_enabled = True
        so.outline_width = Dim(5, 'px')
        so.outline_color = (0, 0, 0, 255)
        canvas = compute_canvas((1920, 1080), ov.layout)
        rc = CueRenderer(doc, canvas, ov).render_cue(doc.cues[0])
        self.assertIsNotNone(rc)
        a = rc.bitmap[..., 3]
        rgb = rc.bitmap[..., :3].astype(int)
        white = (a > 200) & (rgb.sum(axis=2) > 500)
        black = (a > 200) & (rgb.sum(axis=2) < 150)
        self.assertTrue(white.any() and black.any())
        top_white = int(np.nonzero(white.any(axis=1))[0].min())
        top_black = int(np.nonzero(black.any(axis=1))[0].min())
        # the ring sits ABOVE the mark's white fill (outline under mark)
        self.assertLess(top_black, top_white)

    def test_layout_options_conflict_greying(self):
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            self.skipTest('PyQt6 not installed')
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        _app = QApplication.instance() or QApplication([])
        from ttml2pgs.core.overrides import LayoutOptions
        from ttml2pgs.ui.widgets.settings_panel import LayoutOptionsEditor
        ed = LayoutOptionsEditor(LayoutOptions())
        self.assertFalse(ed.chk_hd.isEnabled())      # needs video dims
        ed.chk_vidims.setChecked(True)
        self.assertTrue(ed.chk_hd.isEnabled())
        self.assertFalse(ed.spin_arw.isEnabled())    # needs AR override
        ed.chk_ar.setChecked(True)
        self.assertTrue(ed.spin_arw.isEnabled())
        self.assertFalse(ed.chk_169.isEnabled())     # AR override wins
        ed.chk_ar.setChecked(False)
        self.assertTrue(ed.chk_169.isEnabled())
        # padding moved to the per-language sets (Spacing & opacity)
        self.assertFalse(hasattr(ed, 'chk_pad'))
        from ttml2pgs.core.overrides import StyleOverrides
        from ttml2pgs.ui.widgets.settings_panel import OverrideEditor
        so = StyleOverrides()
        oe = OverrideEditor(so)
        self.assertEqual(
            list(oe.sections),
            ['Font', 'Color', 'Outline & shadow', 'Spacing & opacity'])
        oe.chk_pad.setChecked(True)
        oe.spin_ph.setValue(8.0)
        self.assertTrue(so.use_padding)
        self.assertEqual(so.padding_h, 8.0)

    def test_ruby_baked_inline_and_styles_pruned(self):
        """Ruby roles are baked onto spans at load; role-only styles are
        pruned — rendering must be identical to the raw parse."""
        from ttml2pgs.core.parsers.ttml import TTMLParser
        doc = load_subtitle(sample('netflix_ja.ttml'))
        # no surviving named style carries a ruby role
        for sid, st in doc.styles.items():
            self.assertIsNone(st.ruby, f'{sid} still has a ruby role')
        # raw parse (no bake/prune) renders identically
        raw = TTMLParser().parse_file(sample('netflix_ja.ttml'))
        canvas = compute_canvas((1920, 1080), OverrideSet().layout)
        r_norm = CueRenderer(doc, canvas, OverrideSet())
        r_raw = CueRenderer(raw, canvas, OverrideSet())
        matched = 0
        for cue_n, cue_r in zip(doc.sorted_cues(), raw.sorted_cues()):
            a = r_norm.render_cue(cue_n)
            b = r_raw.render_cue(cue_r)
            if a is None or b is None:
                continue
            self.assertEqual((a.x, a.y), (b.x, b.y))
            self.assertTrue(np.array_equal(a.bitmap, b.bitmap),
                            'bake/prune changed rendering')
            matched += 1
        self.assertGreater(matched, 0)

    def test_prune_noop_styles_edges(self):
        from ttml2pgs.core.model import Style, SubtitleDocument, Cue
        from ttml2pgs.core.parsers import prune_noop_styles
        from ttml2pgs.core.units import Dim

        def make_doc(extra=None):
            d = SubtitleDocument()
            d.styles['noop'] = Style(id='noop', font_size=Dim(100, '%'),
                                     font_weight='normal',
                                     font_style='normal')
            d.styles['role'] = Style(id='role', ruby='base')
            d.styles['keep'] = Style(id='keep', ruby='container',
                                     color=(255, 0, 0, 255))
            if extra:
                d.styles['boldy'] = Style(id='boldy', font_weight='bold')
            c = Cue(begin_ms=0, end_ms=1000)
            c.style_refs = ['noop', 'role', 'keep']
            d.cues.append(c)
            return d

        d = make_doc()
        n = prune_noop_styles(d)
        self.assertEqual(n, 2)                       # noop + role
        self.assertEqual(set(d.styles), {'keep'})    # ruby+color stays
        self.assertEqual(d.cues[0].style_refs, ['keep'])

        # an explicit weight:normal is NOT pruned when something in the
        # document sets bold (it may be cancelling it)
        d2 = make_doc(extra=True)
        prune_noop_styles(d2)
        self.assertIn('noop', d2.styles)

    def test_ruby_markup_editing(self):
        try:
            from ttml2pgs.ui.widgets.cue_editor import CueMarkup
        except ImportError:
            self.skipTest('PyQt6 not installed')
        import re as _re
        doc = load_subtitle(sample('netflix_ja.ttml'))
        cue = next(c for c in doc.cues
                   if 'ル1' in CueMarkup.from_cue(doc, c).text)
        mk = CueMarkup.from_cue(doc, cue)

        def ann_texts(root):
            out = []

            def walk(n, chain):
                for ch in n.children:
                    if ch.kind == 'span':
                        sub = chain + [(ch.style_refs, ch.inline_style)]
                        role = doc.resolve_style(sub).ruby or ''
                        if role in ('text', 'textContainer'):
                            out.append(ch.plain_text())
                        walk(ch, sub)
            walk(root, [(cue.style_refs, cue.inline_style)])
            return out

        # change the reading inside the () — annotation must follow
        edited = _re.sub(r'\(([^)]*)\)', '(テスト)', mk.text, count=1)
        tree, reason = mk.to_tree(edited)
        self.assertIsNotNone(tree, reason)
        self.assertIn('テスト', ann_texts(tree))

        # deleting the (reading) dissolves the ruby into plain text
        dissolved = _re.sub(r'\(([^)]*)\)', '', mk.text, count=1)
        tree2, reason2 = mk.to_tree(dissolved)
        self.assertIsNotNone(tree2, reason2)
        self.assertLess(len(ann_texts(tree2)), len(ann_texts(tree)))

        # a style chip may not open inside a ruby block
        sid = sorted(doc.styles.keys())[0] if doc.styles else None
        if sid:
            bad = mk.text.replace('(', f'⟦{sid}(', 1)
            t3, r3 = mk.to_tree(bad)
            self.assertIsNone(t3)

    def test_wrap_ruby_offscreen(self):
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            self.skipTest('PyQt6 not installed')
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        _app = QApplication.instance() or QApplication([])
        from ttml2pgs.ui.widgets.cue_editor import CueMarkup, TokenStyleEdit
        doc = load_subtitle(sample('basic.srt'))
        cue = doc.cues[0]
        mk = CueMarkup.from_cue(doc, cue)
        ed = TokenStyleEdit()
        ed.load(mk)
        c = ed.textCursor()
        c.setPosition(0)
        c.setPosition(2, c.MoveMode.KeepAnchor)   # first two chars = base
        ed.setTextCursor(c)
        ed.wrap_ruby()
        ed.textCursor().insertText('よみ')         # type the reading
        text = ed._serialize()
        self.assertIn('⟦ル1', text)
        self.assertIn('(よみ)', text)
        tree, reason = mk.to_tree(text)
        self.assertIsNotNone(tree, reason)
        roles = []

        def walk(n):
            for ch in n.children:
                if ch.kind == 'span':
                    if ch.inline_style is not None and \
                            ch.inline_style.ruby:
                        roles.append(ch.inline_style.ruby)
                    walk(ch)
        walk(tree)
        self.assertIn('container', roles)

    def test_style_hints(self):
        from ttml2pgs.core.model import Style, style_hints
        from ttml2pgs.core.units import Dim
        st = Style(color=(255, 0, 0, 255), text_align='center',
                   display_align='after', shear=15.0,
                   font_size=Dim(5, 'vh'))
        h = style_hints(st)
        self.assertEqual(h.count('align'), 1)      # grouped
        self.assertIn('italics', h)                # shear counts
        self.assertIn('color', h)
        self.assertIn('size', h)
        self.assertEqual(style_hints(Style()), '')
        self.assertIn('bold', style_hints(Style(font_weight='bold')))
        self.assertIn('vertical', style_hints(Style(writing_mode='tbrl')))
        # explicit 'normal' weight/style adds no hint; nor does a lone
        # ruby_position
        self.assertEqual(style_hints(Style(font_weight='normal')), '')
        self.assertEqual(style_hints(Style(font_style='normal')), '')
        self.assertEqual(style_hints(Style(ruby_position='after')), '')
        self.assertIn('italics', style_hints(Style(font_style='italic')))

    def test_used_styles_spans(self):
        try:
            from ttml2pgs.ui.widgets.cue_table import used_styles
        except ImportError:
            self.skipTest('PyQt6 not installed')
        from ttml2pgs.core.model import Cue, SpanNode
        cue = Cue(begin_ms=0, end_ms=1000)
        cue.style_refs = ['Style0']
        sp = SpanNode(kind='span')
        sp.style_refs = ['Style2']
        sp.children.append(SpanNode.text_node('inner'))
        cue.root.children.append(SpanNode.text_node('outer '))
        cue.root.children.append(sp)
        self.assertEqual(used_styles(cue), {'Style0', 'Style2'})

    def test_token_edit_offscreen(self):
        """Chip editor: load/serialize round-trip, wrap, partner-delete."""
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            self.skipTest('PyQt6 not installed')
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        _app = QApplication.instance() or QApplication([])
        from ttml2pgs.ui.widgets.cue_editor import CueMarkup, TokenStyleEdit
        doc = load_subtitle(sample('styled.vtt'))
        cue = next(c for c in doc.cues
                   if CueMarkup.from_cue(doc, c).text.count('⟦') >= 1)
        mk = CueMarkup.from_cue(doc, cue)
        ed = TokenStyleEdit()
        committed = []
        ed.committed.connect(committed.append)
        ed.load(mk)
        self.assertEqual(ed._serialize(), mk.text,
                         'document/chip round-trip broke the markup')

        # wrap the whole content in another existing style
        sid = sorted(doc.styles.keys())[0]
        c = ed.textCursor()
        c.select(c.SelectionType.Document)
        ed.setTextCursor(c)
        ed.wrap_selection(sid)
        after = ed._serialize()
        self.assertTrue(after.startswith('⟦' + sid))
        self.assertTrue(after.endswith(sid + '⟧'))
        self.assertTrue(committed, 'wrap must commit a rebuilt tree')

        # copying a range that spans chips exports markup literals via
        # the piece map (regression: per-char cursor walk crashed)
        c2 = ed.textCursor()
        c2.select(c2.SelectionType.Document)
        ed.setTextCursor(c2)
        for _ in range(3):
            mime = ed.createMimeDataFromSelection()
            self.assertEqual(mime.text(), ed._serialize())

        # deleting a chip removes its partner but keeps the text
        tok = ed._doc_tokens[0]
        sel = ed.textCursor()
        sel.setPosition(tok[0])
        sel.setPosition(tok[0] + 1, sel.MoveMode.KeepAnchor)
        ed.setTextCursor(sel)
        n_before = len(ed._doc_tokens)
        ed._delete_smart(forward=True)
        self.assertEqual(len(ed._doc_tokens), n_before - 2,
                         'partner chip must go too')
        tree, reason = mk.to_tree(ed._serialize())
        self.assertIsNotNone(tree, reason)

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
        self.assertEqual(so.weight_boost, 1.0)
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


class TestDefaultProfiles(unittest.TestCase):
    """Round 12: fallback profiles — 'the initials if no initials are
    set'. They fill only the gaps the file leaves open."""

    def _plain_doc(self, lang='ja'):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'p.vtt')
            with open(p, 'w', encoding='utf-8') as f:
                f.write('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n'
                        'こんにちは世界\n')
            doc = load_subtitle(p)
        doc.language = lang
        for c in doc.cues:
            c.lang = lang
        return doc

    def test_profile_fills_only_gaps(self):
        doc = self._plain_doc()
        canvas = compute_canvas((1920, 1080), OverrideSet().layout)
        base = CueRenderer(doc, canvas).render_cue(doc.cues[0])

        ov = OverrideSet()
        ov.profiles[''] = Style(id='__profile__', font_size=Dim(9.0, 'vh'))
        big = CueRenderer(doc, canvas, ov).render_cue(doc.cues[0])
        self.assertGreater(big.height, base.height * 1.5,
                           'profile font size must apply to a bare file')

        # a document that DOES set an initial font size wins over the
        # profile: with-profile and without-profile renders are identical
        doc.initial.font_size = Dim(4.5, 'vh')
        a = CueRenderer(doc, canvas).render_cue(doc.cues[0])
        b = CueRenderer(doc, canvas, ov).render_cue(doc.cues[0])
        self.assertEqual((a.width, a.height), (b.width, b.height))
        self.assertTrue(np.array_equal(a.bitmap, b.bitmap))

    def test_profile_color_below_file_styles(self):
        doc = self._plain_doc()
        ov = OverrideSet()
        ov.profiles[''] = Style(id='__profile__',
                                color=(255, 255, 0, 255))
        computed = doc.resolve_style([([], None)], None,
                                     fallback=ov.profile_for('ja'))
        self.assertEqual(computed.color, (255, 255, 0, 255))
        # an explicit initial color beats the profile
        doc.initial.color = (0, 255, 0, 255)
        computed = doc.resolve_style([([], None)], None,
                                     fallback=ov.profile_for('ja'))
        self.assertEqual(computed.color, (0, 255, 0, 255))

    def test_language_profile_replaces_default(self):
        doc = self._plain_doc('ja')
        canvas = compute_canvas((1920, 1080), OverrideSet().layout)
        both = OverrideSet()
        both.profiles[''] = Style(id='__profile__', font_size=Dim(2, 'vh'))
        both.profiles['ja'] = Style(id='__profile__',
                                    font_size=Dim(9, 'vh'))
        only_ja = OverrideSet()
        only_ja.profiles['ja'] = Style(id='__profile__',
                                       font_size=Dim(9, 'vh'))
        a = CueRenderer(doc, canvas, both).render_cue(doc.cues[0])
        b = CueRenderer(doc, canvas, only_ja).render_cue(doc.cues[0])
        self.assertTrue(np.array_equal(a.bitmap, b.bitmap),
                        'ja profile must be used INSTEAD of Default')
        # region-tag match: ja-JP falls back to the ja profile
        self.assertIs(both.profile_for('ja-JP'), both.profiles['ja'])

    def test_empty_profile_neither_shadows_nor_persists(self):
        ov = OverrideSet()
        ov.profiles[''] = Style(id='__profile__',
                                font_size=Dim(6, 'vh'))
        ov.profiles['ja'] = Style(id='__profile__')   # nothing set
        self.assertIs(ov.profile_for('ja'), ov.profiles[''],
                      'empty language profile must not shadow Default')
        d = ov.to_dict()
        self.assertNotIn('ja', d['profiles'])
        self.assertIn('', d['profiles'])

    def test_profiles_serialization_roundtrip(self):
        ov = OverrideSet()
        ov.profiles['ja'] = Style(id='__profile__',
                                  font_family=['Yu Gothic Medium'],
                                  font_size=Dim(5, 'vh'),
                                  color=(200, 200, 200, 255))
        ov2 = OverrideSet.from_dict(ov.to_dict())
        p = ov2.profiles['ja']
        self.assertEqual(p.font_family, ['Yu Gothic Medium'])
        self.assertEqual((p.font_size.value, p.font_size.unit), (5, 'vh'))
        self.assertEqual(p.color, (200, 200, 200, 255))


class TestMediumSibling(unittest.TestCase):
    def test_author_named_family_gets_medium_sibling(self):
        """Files often name the Regular family ('Noto Sans JP') while the
        heavier face is installed under '… Medium'. At CJK normal weight
        the sibling must be found; bold and Latin must not hunt for it."""
        from ttml2pgs.core.fonts import FaceRecord, FontManager, _norm_family
        fm = FontManager()
        recs = [
            FaceRecord(path='/fake/noto-r.otf', index=0,
                       families=['Noto Sans JP'], weight=400),
            FaceRecord(path='/fake/noto-m.otf', index=0,
                       families=['Noto Sans JP Medium'], weight=500),
        ]
        fm.records = recs
        for r in recs:
            for fam in r.families:
                fm.by_family.setdefault(_norm_family(fam), []).append(r)
        ja = fm.resolve_stack(['Noto Sans JP'], lang='ja')
        self.assertEqual(ja[0].weight, 500,
                         'Medium sibling must lead for author-named CJK')
        en = fm.resolve_stack(['Noto Sans JP'], lang='en')
        self.assertEqual(en[0].weight, 400)
        bold = fm.resolve_stack(['Noto Sans JP'], lang='ja', weight='bold')
        self.assertEqual(bold[0].weight, 400,
                         'bold keeps the named family (synth bold)')


class TestParallelRender(unittest.TestCase):
    def _many_cue_doc(self, td, n=20):
        lines = ['WEBVTT', '']
        for i in range(n):
            t0, t1 = i * 2, i * 2 + 1
            lines.append(f'00:00:{t0:02d}.000 --> 00:00:{t1:02d}.500')
            lines.append(f'Cue number {i} — some text to raster')
            lines.append('')
        p = os.path.join(td, 'many.vtt')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        return load_subtitle(p)

    def test_parallel_matches_sequential_bytes(self):
        """workers=2 must produce a byte-identical .sup to workers=1
        (cue rendering is pure; assembly is in cue order)."""
        from ttml2pgs.core.pipeline import MIN_PARALLEL_CUES
        with tempfile.TemporaryDirectory() as td:
            doc = self._many_cue_doc(td)
            self.assertGreaterEqual(len(doc.cues), MIN_PARALLEL_CUES)
            out_seq = os.path.join(td, 'seq.sup')
            out_par = os.path.join(td, 'par.sup')
            RenderPipeline(doc, RenderSettings(out_path=out_seq,
                                               workers=1)).run()
            RenderPipeline(doc, RenderSettings(out_path=out_par,
                                               workers=2)).run()
            with open(out_seq, 'rb') as f:
                seq = f.read()
            with open(out_par, 'rb') as f:
                par = f.read()
            self.assertGreater(len(seq), 500)
            self.assertEqual(seq, par)

    def test_small_jobs_stay_sequential(self):
        """Below MIN_PARALLEL_CUES the pool is skipped (worker startup
        would dominate) — the job must still complete."""
        with tempfile.TemporaryDirectory() as td:
            doc = self._many_cue_doc(td, n=3)
            out = os.path.join(td, 'o.sup')
            pipe = RenderPipeline(doc, RenderSettings(out_path=out,
                                                      workers=8))
            self.assertEqual(pipe.run(), out)
            self.assertGreater(os.path.getsize(out), 500)

    def test_workers_serialization(self):
        rs = RenderSettings(out_path='x.sup', workers=4)
        rs2 = RenderSettings.from_dict(rs.to_dict())
        self.assertEqual(rs2.workers, 4)
        self.assertEqual(RenderSettings.from_dict({'out_path': 'y'}).workers,
                         0)


class TestPreferencesDialog(unittest.TestCase):
    def test_preferences_dialog_offscreen(self):
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            self.skipTest('PyQt6 not installed')
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])  # noqa: F841
        from ttml2pgs.ui.preferences import PreferencesDialog

        ov = OverrideSet()
        settings = {'render_workers': 0, 'player_engine': 'auto'}
        dlg = PreferencesDialog(ov, settings)

        # Default profile row exists, selected, not removable
        self.assertEqual(dlg.profile_list.count(), 1)
        self.assertEqual(dlg.profile_list.item(0).text(), 'Default')
        self.assertFalse(dlg.btn_del_profile.isEnabled())

        # editing the editor writes into overrides.profiles['']
        changed = []
        dlg.profiles_changed.connect(lambda: changed.append(1))
        ed = dlg.profile_editor
        ed.c_size.setChecked(True)
        self.assertIsNotNone(ov.profiles[''].font_size)
        self.assertTrue(changed)

        # language profile add (programmatic path) + removable
        ov.profiles.setdefault('ja', Style(id='__profile__'))
        dlg._reload_profiles(select='ja')
        item = dlg.profile_list.currentItem()
        self.assertEqual(item.text(), 'ja')
        self.assertTrue(dlg.btn_del_profile.isEnabled())
        dlg._del_profile()
        self.assertNotIn('ja', ov.profiles)

        # performance tab writes render_workers
        dlg.spin_workers.setValue(3)
        self.assertEqual(settings['render_workers'], 3)

        # player tab writes engine choice
        dlg.cmb_engine.setCurrentIndex(1)
        self.assertEqual(settings['player_engine'], 'qt')

        # close prunes empty profiles (Default stays only if non-empty)
        ov.profiles.setdefault('zh', Style(id='__profile__'))
        dlg.close()
        self.assertNotIn('zh', ov.profiles)
        self.assertIn('', ov.profiles)          # has font_size set


class TestMergeMode(unittest.TestCase):
    """Round 16: merge two languages into one renderable document."""

    def _vtt(self, td, name, lines):
        p = os.path.join(td, name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write('WEBVTT\n\n' + '\n\n'.join(lines))
        return p

    def _fixture(self, td):
        mk = self._vtt
        return {
            'ja1': mk(td, 'Episode01.jp.vtt',
                      ['00:00:02.000 --> 00:00:05.000\nこんにちは',
                       '00:00:06.000 --> 00:00:08.000\n世界']),
            'en1': mk(td, 'Episode01.en.vtt',
                      ['00:00:02.000 --> 00:00:04.000\nHello']),
            'enf1': mk(td, 'Episode01.en.forced.vtt',
                       ['00:00:01.700 --> 00:00:03.500\nSign',
                        '00:00:03.500 --> 00:00:05.200\nSign 2']),
            'ja2': mk(td, 'Episode02.jp.vtt',
                      ['00:00:02.000 --> 00:00:05.000\nテスト']),
            'enf2': mk(td, 'Episode02.en.forced.vtt',
                       ['00:00:02.100 --> 00:00:04.000\nS']),
        }

    def test_grouping_and_common_variants(self):
        """Selection covers episodes; ALL open files of those episodes
        are considered; only options present everywhere are common —
        forced distinct from regular."""
        from ttml2pgs.core.merge import (plan_merge, common_variants,
                                         all_variants, variant_label)
        with tempfile.TemporaryDirectory() as td:
            f = self._fixture(td)
            pool = [(p, load_subtitle(p).language) for p in f.values()]
            groups = plan_merge(pool, [f['ja1'], f['ja2']])
            self.assertEqual(set(groups), {'episode01', 'episode02'})
            self.assertEqual(set(groups['episode01']),
                             {'ja', 'en', 'en+forced'})
            common = common_variants(groups)
            self.assertEqual(set(common), {'ja', 'en+forced'},
                             'en missing from ep2 → not common')
            self.assertIn('en', all_variants(groups))
            self.assertEqual(variant_label('en+forced'), 'en (forced)')

    def test_merge_documents_structure(self):
        """Merged doc: primary language, per-cue source langs kept,
        secondary regions/styles suffixed, both render."""
        from ttml2pgs.core.merge import merge_documents, merged_out_path
        with tempfile.TemporaryDirectory() as td:
            f = self._fixture(td)
            p, s = load_subtitle(f['ja1']), load_subtitle(f['enf1'])
            m = merge_documents(p, s, 'ja', 'en+forced')
            self.assertEqual(m.language, 'ja')
            self.assertEqual(len(m.cues), 4)
            self.assertEqual(sorted({c.lang for c in m.cues}),
                             ['en', 'ja'])
            # secondary region renamed with a language suffix
            self.assertTrue(any(r.endswith('.en') for r in m.regions),
                            m.regions.keys())
            # uids unique (queue/preview caching relies on it)
            uids = [c.uid for c in m.cues]
            self.assertEqual(len(uids), len(set(uids)))
            out = merged_out_path(f['ja1'], None, 'ja', 'en+forced')
            self.assertEqual(os.path.basename(out),
                             'Episode01.ja+en.forced.sup')
            from ttml2pgs.core.merge import merged_track_name
            self.assertEqual(merged_track_name('ja', 'en+forced'),
                             'ja-en.forced')
            self.assertEqual(merged_track_name('ja', 'en'), 'ja-en')
            # every merged cue renders
            canvas = compute_canvas((1920, 1080), OverrideSet().layout)
            r = CueRenderer(m, canvas, OverrideSet())
            for cue in m.cues:
                self.assertIsNotNone(r.render_cue(cue))

    def test_merge_language_specific_overrides_apply(self):
        """Per-language override sets act on the merged doc's cues by
        SOURCE language (the point of keeping cue.lang)."""
        from ttml2pgs.core.merge import merge_documents
        with tempfile.TemporaryDirectory() as td:
            f = self._fixture(td)
            m = merge_documents(load_subtitle(f['ja1']),
                                load_subtitle(f['enf1']),
                                'ja', 'en+forced')
            ov = OverrideSet()
            en = ov.ensure_language('en')
            en.override_font_size = True
            en.font_size = Dim(9.0, 'vh')
            canvas = compute_canvas((1920, 1080), OverrideSet().layout)
            plain = CueRenderer(m, canvas, OverrideSet())
            sized = CueRenderer(m, canvas, ov)
            ja_cue = next(c for c in m.cues if c.lang == 'ja')
            en_cue = next(c for c in m.cues if c.lang == 'en')
            self.assertEqual(plain.render_cue(ja_cue).height,
                             sized.render_cue(ja_cue).height,
                             'ja cues must not take the en override')
            self.assertGreater(sized.render_cue(en_cue).height,
                               plain.render_cue(en_cue).height * 1.5)

    def test_secondary_initials_preserved(self):
        """Secondary TTML initials survive as a style on its cues while
        the doc initial stays the primary's."""
        from ttml2pgs.core.merge import merge_documents
        p = SubtitleDocument()
        p.language = 'ja'
        p.initial.color = (255, 255, 255, 255)
        s = SubtitleDocument()
        s.language = 'en'
        s.initial.color = (255, 255, 0, 255)
        cue = Cue(begin_ms=0, end_ms=1000)
        node = SpanNode(kind='text', text='hi')
        cue.root.children.append(node)
        s.cues.append(cue)
        m = merge_documents(p, s, 'ja', 'en')
        self.assertEqual(m.initial.color, (255, 255, 255, 255))
        mc = m.cues[-1]
        self.assertTrue(mc.style_refs and
                        mc.style_refs[0].startswith('__init.'))
        computed = m.resolve_style(
            [(mc.style_refs, mc.inline_style)], None, language='en')
        self.assertEqual(computed.color, (255, 255, 0, 255))

    def test_snap_secondary_timestamps_example(self):
        """The user's canonical scenario: en starts 0.3s early → snaps
        to the ja start; the following en cue ends 0.2s late → snaps to
        the ja end; mid-cue edges out of range stay put."""
        from ttml2pgs.core.merge import (merge_documents,
                                         snap_secondary_timestamps)
        with tempfile.TemporaryDirectory() as td:
            f = self._fixture(td)
            m = merge_documents(load_subtitle(f['ja1']),
                                load_subtitle(f['enf1']),
                                'ja', 'en+forced')
            n = snap_secondary_timestamps(m, 'ja', 500.0)
            self.assertEqual(n, 2)
            sec = sorted((c for c in m.cues if c.lang == 'en'),
                         key=lambda c: c.begin_ms)
            self.assertEqual(sec[0].begin_ms, 2000.0)   # 1700 → ja start
            self.assertEqual(sec[0].end_ms, 3500.0)     # no bound near
            self.assertEqual(sec[1].begin_ms, 3500.0)   # untouched
            self.assertEqual(sec[1].end_ms, 5000.0)     # 5200 → ja end
            # far-away cues untouched
            m2 = merge_documents(load_subtitle(f['ja1']),
                                 load_subtitle(f['en1']), 'ja', 'en')
            for c in m2.cues:
                if c.lang == 'en':
                    c.begin_ms, c.end_ms = 20000.0, 22000.0
            self.assertEqual(
                snap_secondary_timestamps(m2, 'ja', 500.0), 0)

    def test_reopen_by_name(self):
        """Same FILE NAME (any folder) can't be open twice — the state
        finds it and reload replaces in place."""
        from ttml2pgs.ui.state import AppState
        with tempfile.TemporaryDirectory() as td:
            d1 = os.path.join(td, 'a')
            d2 = os.path.join(td, 'b')
            os.makedirs(d1)
            os.makedirs(d2)
            p1 = self._vtt(d1, 'ep.vtt',
                           ['00:00:01.000 --> 00:00:02.000\none'])
            p2 = self._vtt(d2, 'ep.vtt',
                           ['00:00:01.000 --> 00:00:02.000\ntwo'])
            st = AppState()
            st.open_subtitle(p1, auto_match=False)
            self.assertEqual(st.find_session_by_name(p2), 0)
            st.reload_session(0, p2)
            self.assertEqual(len(st.sessions), 1)
            self.assertEqual(st.sessions[0].sub_path,
                             os.path.abspath(p2))
            self.assertIn('two', st.sessions[0].doc.cues[0].plain_text())

    def test_overlap_highlight_and_filter(self):
        """Timestamp cells of time-overlapping cues are tinted (not the
        whole row) and the Start/End header filter can isolate
        overlapping / non-overlapping cues."""
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            self.skipTest('PyQt6 not installed')
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])  # noqa: F841
        from ttml2pgs.ui.widgets.cue_table import (
            CueModel, CueFilterProxy, COL_START, COL_END, COL_TEXT)

        doc = SubtitleDocument()
        times = [(2000, 15000), (3500, 25000), (20600, 24500),
                 (30000, 31000),          # lone cue
                 (31000, 32000)]          # touches previous — NOT overlap
        for b, e in times:
            c = Cue(begin_ms=b, end_ms=e)
            c.root.children.append(SpanNode.text_node('x'))
            doc.cues.append(c)
        model = CueModel()
        model.set_document(doc)
        over = [c.uid in model._overlaps for c in model.cues]
        self.assertEqual(over, [True, True, True, False, False])

        # tint on the timestamp cells only
        bg_start = model.data(model.index(0, COL_START),
                              Qt.ItemDataRole.BackgroundRole)
        bg_text = model.data(model.index(0, COL_TEXT),
                             Qt.ItemDataRole.BackgroundRole)
        bg_lone = model.data(model.index(3, COL_END),
                             Qt.ItemDataRole.BackgroundRole)
        self.assertIsNotNone(bg_start)
        self.assertIsNone(bg_text)
        self.assertIsNone(bg_lone)
        tip = model.data(model.index(0, COL_START),
                         Qt.ItemDataRole.ToolTipRole)
        self.assertIn('#2', tip)

        proxy = CueFilterProxy()
        proxy.setSourceModel(model)
        proxy.set_overlap_value(True)
        self.assertEqual(proxy.rowCount(), 3)
        proxy.set_overlap_value(False)
        self.assertEqual(proxy.rowCount(), 2)
        proxy.set_overlap_value(None)
        self.assertEqual(proxy.rowCount(), 5)

        # a timing edit re-evaluates overlap state
        model.setData(model.index(3, COL_START), '00:00:24.000')
        self.assertIn(model.cues[3].uid, model._overlaps)

    def test_merge_bakes_differing_source_timing(self):
        """Offsets/conform applied to a source before merging are baked
        into its cues so both languages stay in sync in one job."""
        from ttml2pgs.core.merge import bake_timing, merge_documents
        from fractions import Fraction as F
        with tempfile.TemporaryDirectory() as td:
            f = self._fixture(td)
            p = load_subtitle(f['ja1'])
            s = load_subtitle(f['enf1'])
            # secondary nudged +500ms and PAL-conformed
            plan = RetimePlan(scale=F(25, 24), offset_ms=0.0,
                              description='24→25')
            bake_timing(s, plan, 500.0)
            first = s.cues[0]
            self.assertAlmostEqual(first.begin_ms,
                                   1700 * 25 / 24 + 500, places=3)
            self.assertIsNone(s.fps)
            m = merge_documents(p, s, 'ja', 'en+forced')
            en = [c for c in m.cues if c.lang == 'en']
            self.assertAlmostEqual(min(c.begin_ms for c in en),
                                   1700 * 25 / 24 + 500, places=3)
            # primary untouched
            ja = [c for c in m.cues if c.lang == 'ja']
            self.assertEqual(min(c.begin_ms for c in ja), 2000.0)

    def test_classify_region_positions(self):
        """Region → screen position: bottom band → 'bottom', top band →
        'top', tbrl column on the right → 'vertical right'."""
        from ttml2pgs.core.renderer import (classify_region_position,
                                            REGION_POSITION_PRIORITY)
        doc = load_subtitle(sample('netflix_ja.ttml'))
        pos = {rid: classify_region_position(doc, r)
               for rid, r in doc.regions.items()}
        # region0: y=70% h=20% (text bottom-aligned) → bottom
        self.assertEqual(pos['region0'], 'bottom')
        # region1: y=10% h=20% → top
        self.assertEqual(pos['region1'], 'top')
        # region2: tbrl at x=75% w=20% → vertical right
        self.assertEqual(pos['region2'], 'vertical right')
        self.assertLess(REGION_POSITION_PRIORITY['bottom'],
                        REGION_POSITION_PRIORITY['vertical right'])
        self.assertLess(REGION_POSITION_PRIORITY['top'],
                        REGION_POSITION_PRIORITY['center'])

    def test_align_same_language_and_closest_boundary(self):
        """Within one language a top-region cue aligns to the
        bottom-region cue; among several boundaries in threshold the
        CLOSEST wins."""
        from ttml2pgs.core.merge import (align_overlaps,
                                         align_same_language_overlaps,
                                         _nearest_bound)
        from ttml2pgs.core.model import Region
        from ttml2pgs.core.units import Dim

        # closest-boundary preference (both within threshold)
        self.assertEqual(_nearest_bound(5000.0, [4800.0, 5300.0], 500.0),
                         4800.0)
        self.assertEqual(_nearest_bound(5200.0, [4800.0, 5300.0], 500.0),
                         5300.0)

        doc = SubtitleDocument()
        doc.language = 'ja'
        doc.regions['bot'] = Region(id='bot', x=Dim(10, '%'),
                                    y=Dim(70, '%'), width=Dim(80, '%'),
                                    height=Dim(20, '%'))
        doc.regions['top'] = Region(id='top', x=Dim(10, '%'),
                                    y=Dim(10, '%'), width=Dim(80, '%'),
                                    height=Dim(20, '%'))

        def cue(b, e, rid):
            c = Cue(begin_ms=b, end_ms=e, region_id=rid)
            c.root.children.append(SpanNode.text_node('x'))
            c.lang = 'ja'
            doc.cues.append(c)
            return c

        dialog = cue(2000, 5000, 'bot')          # reference (bottom)
        note = cue(1700, 5200, 'top')            # top note, both edges off
        far = cue(9000, 10000, 'top')            # no overlap — untouched
        n = align_same_language_overlaps(doc, 500.0)
        self.assertEqual(n, 1)
        self.assertEqual((note.begin_ms, note.end_ms), (2000.0, 5000.0))
        self.assertEqual((dialog.begin_ms, dialog.end_ms),
                         (2000.0, 5000.0))       # reference unchanged
        self.assertEqual((far.begin_ms, far.end_ms), (9000.0, 10000.0))
        # align_overlaps on a single-language doc = same-language pass
        self.assertEqual(align_overlaps(doc, 'ja', 500.0), 0)

    def test_language_set_enable_toggle(self):
        """A disabled language set follows Default entirely; enabling
        it activates its own values (auto-created tabs start off)."""
        ov = OverrideSet()
        ov.by_lang[''].override_font_size = True
        ov.by_lang[''].font_size = Dim(5, 'vh')
        so = ov.ensure_language('ja', enabled=False)
        so.font_size = Dim(9, 'vh')
        self.assertIs(ov.for_language('ja'), ov.by_lang[''])
        self.assertIs(ov.for_language('ja-JP'), ov.by_lang[''])
        so.enabled = True
        self.assertIs(ov.for_language('ja'), so)
        # round-trips through serialization
        ov2 = OverrideSet.from_dict(ov.to_dict())
        self.assertTrue(ov2.by_lang['ja'].enabled)
        ov2.by_lang['ja'].enabled = False
        ov3 = OverrideSet.from_dict(ov2.to_dict())
        self.assertFalse(ov3.by_lang['ja'].enabled)

    def test_cross_format_merge_and_t2p_roundtrip(self):
        """Merge is format-agnostic (everything parses into the same
        model): TTML + VTT merge, and the result survives a .t2p
        save/load with per-cue languages intact."""
        from ttml2pgs.core.merge import merge_documents
        with tempfile.TemporaryDirectory() as td:
            p = load_subtitle(sample('netflix_ja.ttml'))   # TTML
            f = self._fixture(td)
            s = load_subtitle(f['enf1'])                   # VTT
            m = merge_documents(p, s, 'ja', 'en+forced')
            self.assertEqual(m.language, 'ja')
            self.assertEqual(sorted({c.lang for c in m.cues}),
                             ['en', 'ja'])
            # TTML styles/regions + suffixed VTT regions coexist
            self.assertTrue(any(r.endswith('.en') for r in m.regions))
            self.assertIn('region0', m.regions)
            canvas = compute_canvas((1920, 1080), OverrideSet().layout)
            r = CueRenderer(m, canvas, OverrideSet())
            for cue in m.cues:
                self.assertIsNotNone(r.render_cue(cue))
            # .t2p round trip keeps the merge fully editable
            proj = os.path.join(td, 'merged.t2p')
            save_project(proj, m)
            m2 = load_subtitle(proj)
            self.assertEqual(sorted({c.lang for c in m2.cues}),
                             ['en', 'ja'])
            self.assertEqual(len(m2.cues), len(m.cues))
            self.assertTrue(any(r.endswith('.en') for r in m2.regions))

    def test_region_outlines_follow_per_language_padding(self):
        """Show-regions outlines must move with each REGION's language
        padding (by the cues using it), matching where text renders —
        merged docs mix languages."""
        try:
            from ttml2pgs.ui.widgets.preview import compute_region_boxes
        except ImportError:
            self.skipTest('PyQt6 not installed')
        from ttml2pgs.core.merge import merge_documents
        with tempfile.TemporaryDirectory() as td:
            f = self._fixture(td)
            m = merge_documents(load_subtitle(f['ja1']),
                                load_subtitle(f['enf1']),
                                'ja', 'en+forced')
            canvas = compute_canvas((1920, 1080), OverrideSet().layout)

            def boxes(ov):
                r = CueRenderer(m, canvas, ov)
                return {rid: (x, y, w, h) for rid, _c, x, y, w, h, _corner
                        in compute_region_boxes(m, r)}

            plain = boxes(OverrideSet())
            ov = OverrideSet()
            en = ov.ensure_language('en')
            en.use_padding = True
            en.padding_v = en.padding_h = 10.0
            padded = boxes(ov)
            ja_rid = next(r for r in m.regions if not r.endswith('.en'))
            en_rid = next(r for r in m.regions if r.endswith('.en'))
            self.assertEqual(plain[ja_rid], padded[ja_rid],
                             'ja region must ignore the en padding')
            self.assertNotEqual(plain[en_rid], padded[en_rid],
                                'en region outline must move inward')

    def test_merge_dialog_offscreen(self):
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            self.skipTest('PyQt6 not installed')
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])  # noqa: F841
        from ttml2pgs.ui.widgets.sources import _MergeDialog

        settings = {}
        dlg = _MergeDialog(['ja', 'en', 'en+forced'], ['ja', 'en+forced'],
                           settings, None)
        # the non-common option is disabled in both lists
        for lst in (dlg.lst_primary, dlg.lst_secondary):
            texts = {lst.item(i).text(): lst.item(i)
                     for i in range(lst.count())}
            self.assertFalse(texts['en'].flags() &
                             Qt.ItemFlag.ItemIsEnabled)
            self.assertTrue(texts['ja'].flags() &
                            Qt.ItemFlag.ItemIsEnabled)
        # OK disabled until two DIFFERENT variants picked
        ok = dlg.btns.button(dlg.btns.StandardButton.Ok)
        self.assertFalse(ok.isEnabled())
        dlg.lst_primary.item(0).setSelected(True)      # ja
        dlg.lst_secondary.item(0).setSelected(True)    # ja again
        self.assertFalse(ok.isEnabled())
        dlg.lst_secondary.item(0).setSelected(False)
        dlg.lst_secondary.item(2).setSelected(True)    # en (forced)
        self.assertTrue(ok.isEnabled())
        dlg.chk_close.setChecked(False)
        dlg.chk_snap.setChecked(True)
        dlg.spin_snap.setValue(0.75)
        dlg.accept()
        self.assertEqual(dlg.choice(), ('ja', 'en+forced', False))
        self.assertEqual(dlg.snap_choice(), (True, 0.75))
        self.assertFalse(settings['merge_close_unused'])
        self.assertTrue(settings['merge_snap'])
        self.assertEqual(settings['merge_snap_threshold'], 0.75)


if __name__ == '__main__':
    unittest.main(verbosity=2)
