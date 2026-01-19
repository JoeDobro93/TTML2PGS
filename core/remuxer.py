import os
import subprocess
import shutil
import platform
import re


class Remuxer:
    def __init__(self, ffmpeg_exe="ffmpeg"):
        self.ffmpeg_exe = ffmpeg_exe
        self.mkvmerge_exe = self._find_mkvmerge()

    def _find_mkvmerge(self):
        """Attempts to locate mkvmerge executable."""
        # 1. Check PATH
        path = shutil.which("mkvmerge")
        if path: return path

        # 2. Check Common Windows Paths
        if platform.system() == "Windows":
            common_paths = [
                r"C:\Program Files\MKVToolNix\mkvmerge.exe",
                r"C:\Program Files (x86)\MKVToolNix\mkvmerge.exe"
            ]
            for p in common_paths:
                if os.path.exists(p):
                    return p
        return None

    def remux_video(self, video_path: str, subtitles: list, progress_callback=None) -> bool:
        """
        Remuxes one or more .sup files into the target video.
        Uses mkvmerge if available (Preferred for PGS), otherwise falls back to ffmpeg.
        Args:
            video_path: Path to video.
            subtitles: List of dicts [{'path': str, 'lang': str, 'title': str}]
            progress_callback: function(current_pct, total_pct, status_msg)
        """
        if not os.path.exists(video_path):
            print(f"[REMUX] Video not found: {video_path}")
            return False

        if self.mkvmerge_exe:
            return self._remux_with_mkvmerge(video_path, subtitles, progress_callback)
        else:
            print("[REMUX] mkvmerge not found. Falling back to ffmpeg (PGS support may be flaky).")
            return self._remux_with_ffmpeg(video_path, subtitles)

    def _remux_with_mkvmerge(self, video_path: str, subtitles: list, progress_callback=None) -> bool:
        """Robust remuxing using MKVToolNix."""
        directory = os.path.dirname(video_path)
        filename = os.path.basename(video_path)
        name, ext = os.path.splitext(filename)

        # Output is ALWAYS .mkv with mkvmerge
        output_path = os.path.join(directory, f"{name}_muxed.mkv")

        # If we want to replace the original, we output to a temp file first
        if output_path.lower() == video_path.lower():
            output_path = os.path.join(directory, f"{name}_temp_remux.mkv")

        print(f"[REMUX] Using mkvmerge: {self.mkvmerge_exe}")

        # Build Command
        # mkvmerge -o output.mkv video.mp4 --language 0:eng sub1.sup --language 0:jpn sub2.sup
        cmd = [self.mkvmerge_exe, "-o", output_path, video_path]

        for sub in subtitles:
            path = sub['path']
            lang = sub.get('lang', 'und')
            title = sub.get('title', '')

            # Flags apply to the FOLLOWING source file
            cmd.extend(["--language", f"0:{lang}"])
            if title:
                cmd.extend(["--track-name", f"0:{title}"])
            cmd.append(path)

        try:
            # Run mkvmerge (UTF-8 safe)
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding='utf-8',
                errors='replace',
                text=True,
                bufsize=1,  # Line buffered
                universal_newlines=True
            )

            # Regex for "Progress: 10%"
            rgx_progress = re.compile(r"Progress:\s*(\d+)%")

            # Read Output Loop
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break

                if line:
                    line = line.strip()
                    # print(f"[MKV] {line}") # Debug output

                    if progress_callback and line.startswith("Progress:"):
                        match = rgx_progress.search(line)
                        if match:
                            pct = int(match.group(1))
                            progress_callback(pct, 100, f"Remuxing: {pct}%")

            # Wait for exit
            returncode = process.wait()

            # stdout, stderr = process.communicate()

            # mkvmerge returns 0 (success) or 1 (warnings, usually fine)
            if returncode <= 1:
                print("[REMUX] mkvmerge Success.")

                # Swap logic: Delete original, rename new file to original name
                # (Unless source was MP4, then we keep the .mkv extension)
                is_mp4_source = ext.lower() == '.mp4'
                final_target = os.path.join(directory, f"{name}.mkv") if is_mp4_source else video_path

                if os.path.exists(video_path) and video_path != final_target:
                    os.remove(video_path)  # Delete old MP4/MKV
                elif os.path.exists(video_path):
                    os.remove(video_path)  # Delete old MKV to be replaced

                os.rename(output_path, final_target)
                print(f"[REMUX] Final output: {final_target}")
                return True
            else:
                stderr = process.stderr.read()
                print(f"[REMUX] mkvmerge Failed (Code {process.returncode}):\n{stderr}")
                if os.path.exists(output_path): os.remove(output_path)
                return False

        except Exception as e:
            print(f"[REMUX] Exception: {e}")
            return False

    def _remux_with_ffmpeg(self, video_path: str, subtitles: list) -> bool:
        """Fallback remuxing using ffmpeg."""
        directory = os.path.dirname(video_path)
        filename = os.path.basename(video_path)
        name, ext = os.path.splitext(filename)

        temp_source = os.path.join(directory, f"{name}_original_tmp{ext}")

        # Auto-switch to MKV if source is MP4
        target_output = video_path
        if ext.lower() == '.mp4':
            target_output = os.path.join(directory, f"{name}.mkv")

        if os.path.exists(temp_source):
            print(f"[REMUX] Temp source exists, aborting: {temp_source}")
            return False

        try:
            os.rename(video_path, temp_source)
        except OSError as e:
            print(f"[REMUX] Rename failed: {e}")
            return False

        cmd = [self.ffmpeg_exe, "-y", "-i", temp_source]
        for sub in subtitles:
            cmd.extend(["-i", sub['path']])

        cmd.extend(["-map", "0"])
        for i, sub in enumerate(subtitles):
            cmd.extend(["-map", str(i + 1)])
            cmd.extend(["-c:s", "copy"])
            lang = sub.get('lang', 'und')
            cmd.extend([f"-metadata:s:s:{i}", f"language={lang}"])
            if sub.get('title'):
                cmd.extend([f"-metadata:s:s:{i}", f"title={sub['title']}"])

        cmd.extend(["-c:v", "copy", "-c:a", "copy"])
        cmd.append(target_output)

        try:
            result = subprocess.run(cmd, capture_output=True, encoding='utf-8', errors='replace')
            if result.returncode == 0:
                print(f"[REMUX] FFmpeg Success.")
                #os.remove(temp_source)
                return True
            else:
                print(f"[REMUX] FFmpeg Failed:\n{result.stderr}")
                if os.path.exists(target_output): os.remove(target_output)
                os.rename(temp_source, video_path)
                return False
        except Exception as e:
            print(f"[REMUX] Exception: {e}")
            if os.path.exists(temp_source) and not os.path.exists(video_path):
                os.rename(temp_source, video_path)
            return False