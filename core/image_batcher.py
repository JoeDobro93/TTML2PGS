"""
core/image_batcher.py

Orchestrates the rendering of all cues in a project to PNG files.
Generates a manifest.json containing source times and project settings.
"""

import os
import json
import time
import io
from typing import Optional, List, Dict
from PIL import Image

from .models import SubtitleProject
from .render import HtmlRenderer
from .image_generator import ImageGenerator

class ImageBatcher:
    def __init__(self, project: SubtitleProject, output_dir: str):
        self.project = project
        self.output_dir = output_dir
        self.images_dir = os.path.join(output_dir, "images")

        # Local Target FPS state
        # Defaults to Source FPS (pass-through) until changed by UI
        self.target_fps_num = project.fps_num
        self.target_fps_den = project.fps_den
        self.viewport_res = (1920, 1080)
        self.content_res = (1920, 1080)

        # Style Overrides (Defaults)
        self.override_font_size = False
        self.global_font_size = 4.5
        self.global_font_size_unit = "vh"
        self.override_color = False
        self.global_color = "#FFFFFF"
        self.override_outline = False
        self.global_outline_enabled = True
        self.global_outline_color = "#000000"
        self.global_outline_width = 3.0
        self.global_outline_unit = "px"
        self.override_shadow = False
        self.global_shadow_enabled = True
        self.global_shadow_color = "#000000"
        self.global_shadow_offset_x = 4.0
        self.global_shadow_offset_y = 4.0
        self.global_shadow_blur = 2.0

        # Global Alpha (1.0 = Opaque, 0.0 = Transparent)
        self.global_alpha = 1.0

        # Ensure directories exist
        os.makedirs(self.images_dir, exist_ok=True)

    #def set_resolution(self, width: int, height: int):
    #    self.target_resolution = (width, height)

    def set_resolutions(self, viewport_res: tuple, content_res: tuple):
        self.viewport_res = viewport_res
        self.content_res = content_res

    def set_style_overrides(self,
                            override_font_size=False, global_font_size=50.0, global_font_size_unit="px",
                            override_color=False, global_color="#FFFFFF",
                            override_outline=False, global_outline_enabled=True, global_outline_color="#000000",
                            global_outline_width=3.0, global_outline_unit="px",
                            override_shadow=False, global_shadow_enabled=True, global_shadow_color="#000000",
                            global_shadow_offset_x=4.0, global_shadow_offset_y=4.0, global_shadow_blur=2.0,
                            global_alpha=1.0):
        """Stores style overrides to pass to the Renderer."""
        self.override_font_size = override_font_size
        self.global_font_size = global_font_size
        self.global_font_size_unit = global_font_size_unit

        self.override_color = override_color
        self.global_color = global_color

        self.override_outline = override_outline
        self.global_outline_enabled = global_outline_enabled
        self.global_outline_color = global_outline_color
        self.global_outline_width = global_outline_width
        self.global_outline_unit = global_outline_unit

        self.override_shadow = override_shadow
        self.global_shadow_enabled = global_shadow_enabled
        self.global_shadow_color = global_shadow_color
        self.global_shadow_offset_x = global_shadow_offset_x
        self.global_shadow_offset_y = global_shadow_offset_y
        self.global_shadow_blur = global_shadow_blur

        self.global_alpha = global_alpha

    def set_target_framerate(self, num: int, den: int):
        """Sets the desired output framerate for the manifest."""
        self.target_fps_num = num
        self.target_fps_den = den

    def run(self, debug_limit: int = 20, debug_mode: bool = True, progress_callback=None, cancel_event=None):
        """
        Renders cues to images and writes manifest.

        :param debug_limit: Max cues to render if debug_mode is True.
        :param debug_mode: If True, restricts output to debug_limit.
        """

        # TIMING LOG BUFFER
        time_logs = []

        def log_step(name, start_t):
            if debug_mode:
                duration = time.time() - start_t
                time_logs.append(f"[{duration:.4f}s] {name}")

        print(f"--- STARTING BATCH RENDER ---")
        print(f"Output: {self.images_dir}")
        print(f"Debug Mode: {debug_mode} (Limit: {debug_limit if debug_mode else 'ALL'})")

        # 1. Filter Cues
        all_cues = self.project.body.cues
        if debug_mode:
            process_cues = all_cues[:debug_limit]
        else:
            process_cues = all_cues

        print(f"Processing {len(process_cues)} / {len(all_cues)} cues...")

        t_renderer_setup = time.time()
        # 2. Setup Renderer (Production Mode = Transparent BG)
        # 2. Setup Renderer (Production Mode = Transparent BG)
        html_renderer = HtmlRenderer(
            self.project,
            debug_mode=False,
            content_resolution=self.content_res,
            override_font_size=self.override_font_size, global_font_size=self.global_font_size,
            global_font_size_unit=self.global_font_size_unit,
            override_color=self.override_color, global_color=self.global_color,
            override_outline=self.override_outline, global_outline_enabled=self.global_outline_enabled,
            global_outline_color=self.global_outline_color, global_outline_width=self.global_outline_width,
            global_outline_unit=self.global_outline_unit,
            override_shadow=self.override_shadow, global_shadow_enabled=self.global_shadow_enabled,
            global_shadow_color=self.global_shadow_color, global_shadow_offset_x=self.global_shadow_offset_x,
            global_shadow_offset_y=self.global_shadow_offset_y, global_shadow_blur=self.global_shadow_blur
        )
        log_step("Renderer Init", t_renderer_setup)

        manifest_cues = []
        t_start = time.time()

        t_browser_launch = time.time()
        with ImageGenerator(self.project,output_resolution=self.viewport_res) as img_gen:
            log_step("Browser Launch", t_browser_launch)

            for i, cue in enumerate(process_cues):
                t_cue_start = time.time()

                # Cancel logic
                if cancel_event and cancel_event.is_set():
                    print("Batch Render Cancelled.")
                    return

                # Naming: cue00001.png
                # We use a sequential counter for filenames (1-based index)
                seq_num = i + 1
                filename = f"cue{seq_num:05d}.png"
                img_path = os.path.join(self.images_dir, filename)

                # Render HTML
                html_content = html_renderer.render_cue_to_html(cue)

                # # Generate PNG
                # img_gen.render_html_to_png(html_content, img_path)
                # # Apply alpha
                # if self.global_alpha < 1.0 and self.global_alpha >= 0.0:
                #     self._apply_global_alpha(img_path, self.global_alpha)

                # Get Raw PNG Bytes (RAM)
                png_bytes = img_gen.get_image_bytes(html_content)

                # Process with PIL in RAM (No Disk I/O yet)
                with Image.open(io.BytesIO(png_bytes)) as img:
                    img = img.convert("RGBA")

                    # Apply Alpha Calculation in RAM
                    if 0.0 <= self.global_alpha < 1.0:
                        r, g, b, a = img.split()
                        # Multiply existing alpha channel by global_alpha
                        new_alpha = a.point(lambda p: int(p * self.global_alpha))
                        img.putalpha(new_alpha)

                    # 4. Save to Disk (Once)
                    img.save(img_path)

                # C. Collect Metadata (Raw Source Times)
                manifest_cues.append({
                    "id": cue.start_ms,
                    "filename": filename,
                    "start_ms": cue.start_ms,
                    "end_ms": cue.end_ms
                })

                log_step(f"Render {filename}", t_cue_start)

                if seq_num % 10 == 0:
                    print(f"  Rendered {seq_num}/{len(process_cues)}")

                # Cancel logic
                if progress_callback:
                    progress_callback(i + 1, len(process_cues), f"Rendering {filename}...")

        t_end = time.time()
        print(f"Batch Render Complete in {t_end - t_start:.2f}s")

        # 3. Write Metadata File
        self._write_manifest(manifest_cues)

        # 4. Print Timing Log (Debug Only)
        if debug_mode:
            print("\n" + "=" * 40)
            print("      PERFORMANCE LOG")
            print("=" * 40)
            for line in time_logs:
                print(line)
            print("=" * 40)

    def _write_manifest(self, cue_data: List[Dict]):
        """
        Writes project metadata and cue list to a JSON file.
        """
        manifest_path = os.path.join(self.output_dir, "manifest.json")

        data = {
            "project_info": {
                "width": self.project.width,
                "height": self.project.height,
                "language": self.project.language,
                "timing_offset_ms": self.project.timing_offset_ms
            },
            "frame_rates": {
                "source": {
                    "num": self.project.fps_num,
                    "den": self.project.fps_den
                },
                "target": {
                    "num": self.target_fps_num,
                    "den": self.target_fps_den
                }
            },
            "cues": cue_data
        }

        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        print(f"Manifest written to: {manifest_path}")

    def set_timing_offset(self, offset_ms: int):
        self.project.timing_offset_ms = offset_ms

    def _apply_global_alpha(self, image_path: str, alpha_factor: float):
        """
        Opens an RGBA image, multiplies the alpha channel by the factor,
        and saves it back to disk.
        """
        try:
            img = Image.open(image_path).convert("RGBA")
            r, g, b, a = img.split()

            # Apply multiplication to every pixel in the alpha channel
            # Note: pixel values are 0-255.
            new_alpha = a.point(lambda p: p * alpha_factor)

            img.putalpha(new_alpha)
            img.save(image_path)
        except Exception as e:
            print(f"[Error] Failed to apply alpha to {image_path}: {e}")