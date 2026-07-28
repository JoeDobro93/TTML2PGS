"""
Session state: open documents, their video bindings and render targets,
the app-wide override set, and persistence between launches.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional

from ..core.model import SubtitleDocument
from ..core.overrides import OverrideSet
from ..core.parsers import load_subtitle
from ..core.timing import RetimePlan, suggest_conform
from ..core.video import VideoInfo, find_matching_video, probe_video


def config_dir() -> str:
    base = os.environ.get('APPDATA') or os.environ.get('XDG_CONFIG_HOME') \
        or os.path.join(os.path.expanduser('~'), '.config')
    d = os.path.join(base, 'ttml2pgs')
    os.makedirs(d, exist_ok=True)
    return d


@dataclass
class DocumentSession:
    """One open subtitle + its render target configuration."""
    doc: SubtitleDocument
    sub_path: str
    video_path: Optional[str] = None
    video_info: Optional[VideoInfo] = None
    offset_ms: float = 0.0
    #: manual conform override (None = auto-suggest from doc vs video fps)
    manual_src_fps: Optional[Fraction] = None
    manual_dst_fps: Optional[Fraction] = None
    use_manual_conform: bool = False
    out_path: str = ''
    dirty: bool = False

    # ------------------------------------------------------------------ #
    @property
    def display_name(self) -> str:
        return os.path.basename(self.sub_path)

    @property
    def language(self) -> str:
        return self.doc.language

    def target_fps(self) -> Fraction:
        if self.use_manual_conform and self.manual_dst_fps:
            return self.manual_dst_fps
        if self.video_info and self.video_info.fps:
            return self.video_info.fps
        return self.doc.fps or Fraction(24000, 1001)

    def retime_plan(self) -> Optional[RetimePlan]:
        if self.use_manual_conform:
            src = self.manual_src_fps or self.doc.fps
            dst = self.manual_dst_fps
            if src and dst and src != dst:
                return RetimePlan.conform(src, dst)
            return None
        video_fps = self.video_info.fps if self.video_info else None
        return suggest_conform(self.manual_src_fps or self.doc.fps,
                               video_fps)

    def default_out_path(self) -> str:
        base_dir = os.path.dirname(self.video_path or self.sub_path)
        stem = os.path.basename(self.sub_path)
        stem = os.path.splitext(stem)[0]
        lang = self.doc.language or 'und'
        if not stem.endswith(f'.{lang}') and f'.{lang}.' not in stem:
            stem = f"{stem}.{lang}"
        return os.path.join(base_dir, f"{stem}.sup")

    def bind_video(self, path: Optional[str]):
        self.video_path = path
        self.video_info = probe_video(path) if path else None
        if not self.out_path:
            self.out_path = self.default_out_path()

    def auto_match_video(self):
        match = find_matching_video(self.sub_path)
        if match:
            self.bind_video(match)


class AppState:
    """Application-level state + settings persistence."""

    def __init__(self):
        self.sessions: List[DocumentSession] = []
        self.active_index: int = -1
        self.overrides = OverrideSet()
        self.settings: Dict = {
            'remux_after_render': True,
            'replace_original': True,
            'move_to_subs_folder': False,
            'external_player': '',
            'external_player_args': '"{file}" /start {ms}',
            'preview_bg': '#B0C4DE',       # v1's LightSteelBlue matte
            'restore_session': True,
        }
        self._settings_path = os.path.join(config_dir(), 'settings.json')
        self._session_path = os.path.join(config_dir(), 'session.json')
        self.queue_state_path = os.path.join(config_dir(), 'queue.json')

    # ------------------------------------------------------------------ #
    @property
    def active(self) -> Optional[DocumentSession]:
        if 0 <= self.active_index < len(self.sessions):
            return self.sessions[self.active_index]
        return None

    def open_subtitle(self, path: str, auto_match: bool = True
                      ) -> DocumentSession:
        path = os.path.abspath(path)
        for i, s in enumerate(self.sessions):
            if os.path.normcase(s.sub_path) == os.path.normcase(path):
                self.active_index = i
                return s
        doc = load_subtitle(path)
        sess = DocumentSession(doc=doc, sub_path=path)
        if auto_match:
            sess.auto_match_video()
        sess.out_path = sess.default_out_path()
        self.sessions.append(sess)
        self.active_index = len(self.sessions) - 1
        # make sure the language has an override tab available
        self.overrides.ensure_language(doc.language)
        return sess

    def close_session(self, index: int):
        if 0 <= index < len(self.sessions):
            del self.sessions[index]
            self.active_index = min(self.active_index,
                                    len(self.sessions) - 1)

    def languages_open(self) -> List[str]:
        langs = []
        for s in self.sessions:
            l = s.doc.language
            if l and l not in langs:
                langs.append(l)
        return langs

    # ------------------------------------------------------------------ #
    def load_settings(self):
        try:
            with open(self._settings_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.settings.update(data.get('settings', {}))
            # migrate the pre-2.0.1 grey default (the picker never saved
            # user choices back then, so a stored #606060 was never picked)
            if str(self.settings.get('preview_bg', '')).lower() == '#606060':
                self.settings['preview_bg'] = '#B0C4DE'
            ov = data.get('overrides')
            if ov:
                self.overrides = OverrideSet.from_dict(ov)
            if int(data.get('version', 1)) < 2:
                # 2.0.1 defaults changed: stem darkening calibrated to 3.0
                # and auto-color enabled. Migrate configs saved before the
                # bump unless the user had set their own values.
                for so in self.overrides.by_lang.values():
                    if so.weight_boost == 1.0:
                        so.weight_boost = 3.0
                    if not so.override_color:
                        so.auto_color = True
        except (OSError, ValueError):
            pass

    def save_settings(self):
        try:
            with open(self._settings_path, 'w', encoding='utf-8') as f:
                json.dump({'version': 2,
                           'settings': self.settings,
                           'overrides': self.overrides.to_dict()},
                          f, indent=1)
        except OSError:
            pass

    def save_session(self):
        data = {'files': [{
            'sub_path': s.sub_path,
            'video_path': s.video_path,
            'offset_ms': s.offset_ms,
            'out_path': s.out_path,
        } for s in self.sessions], 'active': self.active_index}
        try:
            with open(self._session_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=1)
        except OSError:
            pass

    def restore_session(self) -> int:
        if not self.settings.get('restore_session', True):
            return 0
        try:
            with open(self._session_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return 0
        n = 0
        for ent in data.get('files', []):
            path = ent.get('sub_path')
            if not path or not os.path.exists(path):
                continue
            try:
                sess = self.open_subtitle(path, auto_match=False)
            except Exception:
                continue
            vp = ent.get('video_path')
            if vp and os.path.exists(vp):
                sess.bind_video(vp)
            else:
                sess.auto_match_video()
            sess.offset_ms = float(ent.get('offset_ms', 0.0))
            if ent.get('out_path'):
                sess.out_path = ent['out_path']
            n += 1
        self.active_index = min(int(data.get('active', 0)),
                                len(self.sessions) - 1)
        return n
