"""
core/video_frame.py

Extracts a single still frame from a video file at a given timestamp using
ffmpeg. Used by the Preview pane's "Show video frames" feature so the user can
see the subtitle composited over the actual frame the subtitle sits on.

Frames are scaled down and cached on disk so repeatedly selecting the same cue
does not re-spawn ffmpeg.
"""

import os
import subprocess
import tempfile
import hashlib
from typing import Optional


class VideoFrameExtractor:
    def __init__(self, ffmpeg_exe: str = "ffmpeg"):
        self.ffmpeg_exe = ffmpeg_exe
        self._cache = {}  # key -> output path
        self._dir = os.path.join(tempfile.gettempdir(), "ttml2pgs_frames")
        try:
            os.makedirs(self._dir, exist_ok=True)
        except Exception as e:
            print(f"[FRAME] Could not create temp dir: {e}")

    def extract(self, video_path: str, time_ms: float, max_width: int = 1920) -> Optional[str]:
        """
        Returns a path to a PNG of the frame at `time_ms`, or None on failure.

        Frames are de-duplicated to ~40ms (one 25fps frame) granularity so that
        small timing jitter does not thrash the cache.
        """
        if not video_path or not os.path.exists(video_path):
            return None

        time_ms = max(0.0, float(time_ms))
        bucket = int(time_ms // 40)  # ~1 frame granularity
        key = (os.path.abspath(video_path), bucket, max_width)

        cached = self._cache.get(key)
        if cached and os.path.exists(cached):
            return cached

        sec = time_ms / 1000.0
        h = hashlib.md5(f"{video_path}|{bucket}|{max_width}".encode("utf-8")).hexdigest()[:16]
        out_path = os.path.join(self._dir, f"frame_{h}.png")

        # -ss before -i = fast (keyframe) seek; plenty accurate for a preview.
        cmd = [
            self.ffmpeg_exe, "-y",
            "-ss", f"{sec:.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-vf", f"scale='min({int(max_width)},iw)':-2",
            out_path,
        ]

        try:
            kwargs = {}
            if os.name == "nt":
                # CREATE_NO_WINDOW: don't flash a console window on Windows.
                kwargs["creationflags"] = 0x08000000
            result = subprocess.run(cmd, capture_output=True, **kwargs)

            if result.returncode == 0 and os.path.exists(out_path):
                self._cache[key] = out_path
                return out_path

            err = (result.stderr or b"")[:300]
            print(f"[FRAME] ffmpeg failed (rc={result.returncode}): {err}")
        except FileNotFoundError:
            print(f"[FRAME] ffmpeg executable not found: {self.ffmpeg_exe}")
        except Exception as e:
            print(f"[FRAME] Extraction error: {e}")

        return None
