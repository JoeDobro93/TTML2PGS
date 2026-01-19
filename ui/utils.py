import subprocess
import json
import os
from core.models import Fragment


def get_video_metadata(video_path):
    """Returns (width, height, fps_num, fps_den) or None."""
    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "json", video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        stream = data['streams'][0]

        width = int(stream['width'])
        height = int(stream['height'])

        fps_str = stream['r_frame_rate']
        num, den = map(int, fps_str.split('/'))

        return width, height, num, den
    except Exception as e:
        print(f"FFprobe Error: {e}")
        return None


def format_cue_text(fragments: list[Fragment]) -> str:
    """Converts fragments to plain text, handling ruby as [base]([text])."""
    text = ""
    for f in fragments:
        if f.is_ruby:
            text += f"{f.ruby_base}({f.text})"
        else:
            text += f.text
    return text