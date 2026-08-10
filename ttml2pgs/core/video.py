"""
Video helpers: ffprobe metadata, HDR detection, subtitle↔video filename
matching, frame extraction for previews, and remuxing (mkvmerge
preferred, ffmpeg fallback).
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .timing import normalize_fps

VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.m4v', '.mov', '.ts', '.m2ts', '.avi',
                    '.webm')

_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def _run(cmd, timeout=30, **kw):
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          creationflags=_NO_WINDOW, **kw)


@dataclass
class VideoInfo:
    path: str
    width: int = 1920
    height: int = 1080
    fps: Optional[Fraction] = None
    duration_ms: float = 0.0
    is_hdr: bool = False
    codec: str = ''

    @property
    def resolution(self) -> Tuple[int, int]:
        return (self.width, self.height)


def probe_video(path: str) -> Optional[VideoInfo]:
    """ffprobe the first video stream. Returns None when unavailable."""
    if not path or not os.path.exists(path):
        return None
    try:
        r = _run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                  '-show_entries',
                  'stream=width,height,r_frame_rate,avg_frame_rate,codec_name,'
                  'color_transfer,color_primaries,color_space,'
                  'codec_tag_string,duration',
                  '-show_entries', 'format=duration',
                  '-of', 'json', path], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout.decode('utf-8', errors='replace'))
        stream = data['streams'][0]
    except (ValueError, KeyError, IndexError):
        return None

    info = VideoInfo(path=path)
    info.width = int(stream.get('width', 1920) or 1920)
    info.height = int(stream.get('height', 1080) or 1080)
    info.codec = stream.get('codec_name', '')
    for key in ('avg_frame_rate', 'r_frame_rate'):
        fr = stream.get(key, '')
        m = re.match(r'(\d+)/(\d+)$', fr or '')
        if m and int(m.group(2)) and int(m.group(1)):
            info.fps = normalize_fps(int(m.group(1)), int(m.group(2)))
            break
    dur = stream.get('duration') or data.get('format', {}).get('duration')
    try:
        info.duration_ms = float(dur) * 1000.0
    except (TypeError, ValueError):
        pass
    info.is_hdr = _detect_hdr_from_stream(stream) or _detect_hdr_binary(path)
    return info


def _detect_hdr_from_stream(stream: dict) -> bool:
    transfer = (stream.get('color_transfer') or '').lower()
    primaries = (stream.get('color_primaries') or '').lower()
    for marker in ('smpte2084', 'arib-std-b67', 'bt2020'):
        if marker in transfer or marker in primaries:
            return True
    tag = (stream.get('codec_tag_string') or '').lower()
    return 'dvh1' in tag or 'dvhe' in tag


def _detect_hdr_binary(path: str) -> bool:
    """Dolby Vision configuration atoms in the first 256 KiB."""
    try:
        with open(path, 'rb') as f:
            head = f.read(262144)
        return b'dvcC' in head or b'dvvC' in head
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

#: descriptive tokens streaming rips append after the language
#: (``d1758520-_____.jajp.Dialog.Subtitle.ttml``)
_STEM_DROP = {'forced', 'sdh', 'cc', 'full', 'default', 'dialog',
              'dialogue', 'subtitle', 'subtitles', 'sub', 'subs',
              'signs', 'songs', 'hi'}


def _is_tag_token(tok: str) -> bool:
    """Language / flag suffix token, including 'ja+en' merge combos."""
    from .parsers import LANG_TOKENS
    if tok in LANG_TOKENS or tok in _STEM_DROP:
        return True
    pieces = [p for p in tok.split('+') if p]
    return len(pieces) > 1 and all(
        p in LANG_TOKENS or p in _STEM_DROP for p in pieces)


def subtitle_stem(path: str) -> str:
    """'Show.S01E01.ja.forced.ttml' -> 'Show.S01E01' (merged outputs
    like 'Show.S01E01.ja+en.forced.sup' strip the same way)."""
    name = os.path.basename(path)
    parts = name.split('.')
    if len(parts) <= 1:
        return name
    parts = parts[:-1]  # drop extension
    while len(parts) > 1 and _is_tag_token(parts[-1].lower()):
        parts.pop()
    return '.'.join(parts)


def parse_sup_name(path: str) -> Tuple[str, str, bool]:
    """
    (language, mux track name, forced flag) from a .sup's extension
    chain — the inverse of the app's own output naming:

        'Ep.ja.sup'           → ('ja', '', False)
        'Ep.en.forced.sup'    → ('en', '', True)
        'Ep.ja+en.forced.sup' → ('ja', 'ja+en.forced', False)  merged
        'Ep.en+ja.sup'        → ('en', 'en+ja', False)
        'Ep.sup'              → ('und', '', False)

    A 'x+y' combo speaks the FIRST language and keeps the whole tag
    chain as its track label. Its '.forced' belongs to the merged-in
    SECONDARY (forced signs inside a full dialogue track), so the
    track itself is NOT flagged forced — only a plain
    single-language '.forced' name is.
    """
    from .parsers import LANG_TOKENS
    name = os.path.basename(path)
    parts = name.split('.')[:-1] if '.' in name else [name]
    saw_forced = False
    combo: List[str] = []
    while len(parts) > 1:
        tok = parts[-1].lower()
        if tok in _STEM_DROP:
            saw_forced = saw_forced or tok == 'forced'
            parts.pop()
            continue
        pieces = [p for p in tok.split('+') if p]
        if pieces and all(p in LANG_TOKENS for p in pieces):
            if not combo:                    # nearest-to-extension wins
                combo = [LANG_TOKENS[p] for p in pieces]
            parts.pop()
            continue
        break
    lang = combo[0] if combo else 'und'
    if len(combo) > 1:
        track = '+'.join(combo)
        if saw_forced:
            track += '.forced'
        return lang, track, False
    return lang, '', saw_forced


def find_matching_video(sub_path: str,
                        search_dirs: Optional[List[str]] = None
                        ) -> Optional[str]:
    """
    Find a video next to the subtitle file. Tried in order:
    1. the subtitle stem with language/flag suffix tokens stripped;
    2. v1's rule — everything before the first dot (rescues names whose
       middle tokens aren't recognized, e.g. ``id-x.jajp.Foo.Bar.ttml``).
    """
    name = os.path.basename(sub_path)
    stems = [subtitle_stem(sub_path).lower()]
    first = name.split('.', 1)[0].lower()
    if len(first) >= 3 and first not in stems:
        stems.append(first)
    dirs = search_dirs or [os.path.dirname(os.path.abspath(sub_path))]
    for stem in stems:
        best: Optional[str] = None
        for d in dirs:
            try:
                entries = os.listdir(d)
            except OSError:
                continue
            for fn in entries:
                if not fn.lower().endswith(VIDEO_EXTENSIONS):
                    continue
                vstem = os.path.splitext(fn)[0].lower()
                if vstem == stem or fn.lower().startswith(stem + '.'):
                    cand = os.path.normpath(os.path.join(d, fn))
                    if best is None or len(fn) < len(os.path.basename(best)):
                        best = cand
        if best:
            return best
    return None


def is_forced_name(path: str) -> bool:
    name = os.path.basename(path).lower()
    return '.forced.' in name or name.endswith('.forced')


# --------------------------------------------------------------------------- #
# Frame extraction (preview)
# --------------------------------------------------------------------------- #

_TONEMAP_CHAINS: Optional[List[str]] = None


def _tonemap_chains() -> List[str]:
    global _TONEMAP_CHAINS
    if _TONEMAP_CHAINS is not None:
        return _TONEMAP_CHAINS
    available = ''
    try:
        r = _run(['ffmpeg', '-hide_banner', '-filters'], timeout=15)
        available = r.stdout.decode('ascii', errors='ignore')
    except (OSError, subprocess.TimeoutExpired):
        pass
    chains = []
    if ' libplacebo ' in available:
        chains.append('libplacebo=apply_dolbyvision=true:tonemapping=bt.2390:'
                      'colorspace=bt709:color_primaries=bt709:color_trc=bt709,'
                      'format=yuv420p')
        chains.append('libplacebo=format=yuv420p')
    if ' zscale ' in available and ' tonemap ' in available:
        chains.append(
            'zscale=tin=smpte2084:min=bt2020nc:pin=bt2020:t=linear:npl=100,'
            'format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,'
            'zscale=t=bt709:m=bt709:p=bt709:r=tv,format=yuv420p')
    chains.append('')
    _TONEMAP_CHAINS = chains
    return chains


def extract_frame(video_path: str, at_ms: float, tone_map: bool = False
                  ) -> Optional[bytes]:
    """Extract one frame as JPEG bytes (None on failure)."""
    if not video_path or not os.path.exists(video_path):
        return None
    tmp = os.path.join(tempfile.gettempdir(),
                       f"t2p_frame_{os.getpid()}.jpg")
    chains = _tonemap_chains() if tone_map else ['']
    for vf in chains:
        pre = []
        if vf and 'libplacebo' in vf:
            pre = ['-init_hw_device', 'vulkan=vk', '-filter_hw_device', 'vk']
        cmd = ['ffmpeg', '-nostdin', '-y', *pre,
               '-ss', f"{at_ms / 1000.0:.3f}", '-i', video_path,
               '-frames:v', '1']
        if vf:
            cmd += ['-vf', vf]
        cmd += ['-q:v', '2', tmp]
        try:
            r = _run(cmd, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp):
            try:
                with open(tmp, 'rb') as f:
                    return f.read()
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return None


# --------------------------------------------------------------------------- #
# Remuxing
# --------------------------------------------------------------------------- #

@dataclass
class SubTrack:
    path: str
    lang: str = 'und'
    track_name: str = ''
    forced: bool = False
    default: bool = False


def find_mkvmerge() -> Optional[str]:
    p = shutil.which('mkvmerge')
    if p:
        return p
    if platform.system() == 'Windows':
        for cand in (r'C:\Program Files\MKVToolNix\mkvmerge.exe',
                     r'C:\Program Files (x86)\MKVToolNix\mkvmerge.exe'):
            if os.path.exists(cand):
                return cand
    return None


_ISO639_2 = {
    'ja': 'jpn', 'en': 'eng', 'fr': 'fre', 'de': 'ger', 'es': 'spa',
    'it': 'ita', 'pt': 'por', 'zh': 'chi', 'zh-hans': 'chi', 'zh-hant': 'chi',
    'ko': 'kor', 'ru': 'rus', 'ar': 'ara', 'th': 'tha', 'vi': 'vie',
    'id': 'ind', 'nl': 'dut', 'pl': 'pol', 'sv': 'swe', 'no': 'nor',
    'da': 'dan', 'fi': 'fin', 'tr': 'tur', 'he': 'heb', 'hi': 'hin',
    'pt-br': 'por',
}


def mux_language(lang: str) -> str:
    l = (lang or '').lower()
    if len(l) == 3:
        return l
    return _ISO639_2.get(l, _ISO639_2.get(l.split('-')[0], 'und'))


def remux(video_path: str, subs: List[SubTrack],
          replace_original: bool = True,
          progress: Optional[Callable[[int, int, str], None]] = None,
          cancel: Optional[Callable[[], bool]] = None) -> Tuple[bool, str]:
    """
    Mux .sup tracks into the video. Returns (ok, final_path_or_error).
    mkvmerge preferred; ffmpeg fallback. Output is always Matroska.
    """
    if not os.path.exists(video_path):
        return False, f"video not found: {video_path}"
    for s in subs:
        if not os.path.exists(s.path):
            return False, f"subtitle not found: {s.path}"

    directory = os.path.dirname(video_path)
    name, ext = os.path.splitext(os.path.basename(video_path))
    out_tmp = os.path.join(directory, f"{name}.t2p_mux.mkv")

    # muxing writes a FULL temporary copy of the video next to it —
    # check the headroom up front so a nearly-full drive gives a clear
    # message instead of mkvmerge dying mid-write with 'error 112'
    try:
        need = os.path.getsize(video_path) + sum(
            os.path.getsize(s.path) for s in subs)
        free = shutil.disk_usage(directory).free
        margin = 256 << 20                       # 256 MB safety
        if free < need + margin:
            drive = os.path.splitdrive(directory)[0] or directory
            return False, (
                f"not enough free space on {drive}: muxing writes a "
                f"full temporary copy of the video first — needs "
                f"~{(need + margin) / 1e9:.1f} GB free, only "
                f"{free / 1e9:.1f} GB available. Free up space and "
                f"use Retry mux.")
    except OSError:
        pass                                     # preflight is best-effort
    final = os.path.join(directory, f"{name}.mkv") if replace_original \
        else os.path.join(directory, f"{name}.muxed.mkv")

    mkvmerge = find_mkvmerge()
    if mkvmerge:
        ok, err = _remux_mkvmerge(mkvmerge, video_path, subs, out_tmp,
                                  progress, cancel)
    else:
        ok, err = _remux_ffmpeg(video_path, subs, out_tmp)
    if not ok:
        if os.path.exists(out_tmp):
            try:
                os.remove(out_tmp)
            except OSError:
                pass
        return False, err

    return _finalize_mux(out_tmp, video_path, final, replace_original,
                         progress=progress)


#: retry pacing for the finalize step (injectable for tests)
_FINALIZE_DELAYS = (0.0, 0.5, 1.0, 2.0, 3.0, 3.5)


def _finalize_mux(out_tmp: str, video_path: str, final: str,
                  replace_original: bool,
                  progress: Optional[Callable[[int, int, str], None]] = None,
                  delays: Sequence[float] = _FINALIZE_DELAYS
                  ) -> Tuple[bool, str]:
    """
    Swap the finished mux into place. On Windows the original video is
    often *briefly held open* — the embedded preview player, a media
    scanner, antivirus — which makes os.remove/os.replace fail even
    though the mux itself succeeded. So: retry for a few seconds, and
    if the original stays locked, keep it and deliver `*.muxed.mkv`
    alongside instead of failing the whole mux.
    """
    last: Optional[OSError] = None
    for d in delays:
        if d:
            time.sleep(d)
            if progress:
                progress(100, 100, 'Waiting for the video file to be '
                                   'released…')
        try:
            if replace_original:
                if os.path.exists(video_path):
                    os.remove(video_path)
            elif os.path.exists(final):
                os.remove(final)
            os.replace(out_tmp, final)
            return True, final
        except OSError as e:
            last = e
    if replace_original:
        # original is locked — keep it, deliver alongside (same result
        # the "replace original" checkbox OFF would give)
        base = os.path.splitext(final)[0]
        alt = base + '.muxed.mkv'
        try:
            if os.path.exists(alt):
                os.remove(alt)
            os.replace(out_tmp, alt)
            return True, alt
        except OSError as e:
            last = e
    return False, f"finalize failed: {last}"


def _remux_mkvmerge(exe, video_path, subs, out_path, progress, cancel
                    ) -> Tuple[bool, str]:
    cmd = [exe, '-o', out_path, video_path]
    for s in subs:
        cmd += ['--language', f"0:{mux_language(s.lang)}"]
        if s.track_name:
            cmd += ['--track-name', f"0:{s.track_name}"]
        # forced comes from the CALLER (parse_sup_name semantics): a
        # '.forced' inside a 'ja+en.forced' combo is the merged-in
        # secondary, not this track — a raw filename check would
        # wrongly flag full-dialogue merged tracks as forced-only
        cmd += ['--forced-display-flag',
                f"0:{'yes' if s.forced else 'no'}"]
        cmd += ['--default-track-flag', f"0:{'yes' if s.default else 'no'}"]
        cmd.append(s.path)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                encoding='utf-8', errors='replace',
                                creationflags=_NO_WINDOW, bufsize=1)
    except OSError as e:
        return False, f"mkvmerge failed to start: {e}"
    rgx = re.compile(r'Progress:\s*(\d+)%')
    tail: List[str] = []
    while True:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        if cancel and cancel():
            proc.kill()
            return False, 'canceled'
        line = line.strip()
        if line:
            tail.append(line)
            tail[:] = tail[-15:]
            m = rgx.search(line)
            if m and progress:
                progress(int(m.group(1)), 100,
                         f"Muxing {os.path.basename(video_path)}")
    if proc.returncode is not None and proc.returncode <= 1:
        return True, ''
    return False, 'mkvmerge error: ' + ' | '.join(tail[-4:])


def _remux_ffmpeg(video_path, subs, out_path) -> Tuple[bool, str]:
    cmd = ['ffmpeg', '-nostdin', '-y', '-i', video_path]
    for s in subs:
        cmd += ['-i', s.path]
    cmd += ['-map', '0']
    for i, s in enumerate(subs):
        cmd += ['-map', str(i + 1)]
    cmd += ['-c', 'copy']
    for i, s in enumerate(subs):
        cmd += [f'-metadata:s:s:{i}', f'language={mux_language(s.lang)}']
        if s.track_name:
            cmd += [f'-metadata:s:s:{i}', f'title={s.track_name}']
        cmd += [f'-disposition:s:s:{i}', 'forced' if s.forced else '0']
    cmd.append(out_path)
    try:
        r = _run(cmd, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"ffmpeg failed: {e}"
    if r.returncode != 0:
        return False, 'ffmpeg error: ' + \
            r.stderr.decode('utf-8', errors='replace')[-400:]
    return True, ''
