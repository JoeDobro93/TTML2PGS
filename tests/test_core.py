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
            from ttml2pgs.core.jobqueue import JobState
            job.state = JobState.DONE
            q._save_state()

            q2 = QueueManager(state_path=state)
            n = q2.load_state()
            self.assertEqual(n, 1)
            self.assertEqual(q2.groups[0].render_jobs[0].state, JobState.DONE)


if __name__ == '__main__':
    unittest.main(verbosity=2)
