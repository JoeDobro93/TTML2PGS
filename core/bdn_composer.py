"""
core/bdn_composer.py

Timeline Slicer & BDN XML Generator.
Responsible for:
1. Identifying overlapping cues and creating distinct "Time Slices".
2. Merging overlapping cue images into a single composite.
3. Cropping transparent areas to minimize BDN file size.
4. Generating the BDN XML with drift-corrected timestamps (Frame Rate Conforming).
"""

import os
import math
from typing import List, Dict, Tuple
from xml.dom import minidom
import xml.etree.ElementTree as ET

from PIL import Image

from .models import SubtitleProject


class BdnComposer:
    def __init__(self, project: SubtitleProject, input_dir: str, output_dir: str,
                 target_fps_num: int = None, target_fps_den: int = None,
                 target_resolution=None):
        self.project = project
        self.target_resolution = target_resolution
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.slices_dir = os.path.join(output_dir, "slices_for_bdn")

        # Store Source FPS as fraction components for logic if needed
        self.src_fps_num = project.fps_num
        self.src_fps_den = project.fps_den
        self.src_fps_float = project.fps_num / project.fps_den

        # Determine Target FPS
        if target_fps_num and target_fps_den:
            self.tgt_fps_num = target_fps_num
            self.tgt_fps_den = target_fps_den
        else:
            self.tgt_fps_num = self.src_fps_num
            self.tgt_fps_den = self.src_fps_den

        self.tgt_fps_float = self.tgt_fps_num / self.tgt_fps_den

        os.makedirs(self.slices_dir, exist_ok=True)

    def compose(self, cues_metadata: List[Dict], progress_callback=None, cancel_event=None):
        """
        Main entry point.
        metadata format: [{'id':..., 'filename':..., 'start_ms':..., 'end_ms':...}]
        """
        print(f"--- STARTING BDN COMPOSITION ---")
        print(f"Source FPS: {self.src_fps_float:.3f} | Target FPS: {self.tgt_fps_float:.3f}")
        print(f"Offset: {self.project.timing_offset_ms}ms")

        # 1. GENERATE TIME SLICES
        # We need every start and end point to define the intervals
        points = set()
        for c in cues_metadata:
            # Apply offset HERE to ensure internal logic uses "Corrected Source Time"
            s = c['start_ms'] + self.project.timing_offset_ms
            e = c['end_ms'] + self.project.timing_offset_ms
            points.add(s)
            points.add(e)

        sorted_points = sorted(list(points))

        bdn_events = []
        seq_num = 0

        # 2. ITERATE INTERVALS
        for i in range(len(sorted_points) - 1):
            # cancel logic
            if cancel_event and cancel_event.is_set():
                return

            t_start = sorted_points[i]
            t_end = sorted_points[i + 1]
            duration = t_end - t_start

            if duration <= 0: continue

            # Find active cues in this interval
            # Logic: A cue is active if it starts before the interval ends AND ends after the interval starts.
            # (Standard AABB collision)
            mid_point = (t_start + t_end) / 2

            active_cues = []
            for c in cues_metadata:
                # Check using offset times
                c_start = c['start_ms'] + self.project.timing_offset_ms
                c_end = c['end_ms'] + self.project.timing_offset_ms

                # Check overlap
                if c_start <= mid_point < c_end:
                    active_cues.append(c)

            if not active_cues:
                continue

            # 3. COMPOSITE & CROP
            seq_num += 1
            slice_filename = f"slice_{seq_num:05d}.png"
            output_path = os.path.join(self.slices_dir, slice_filename)

            # Sort by ID (or z-index if we had it) to ensure consistent layering
            active_cues.sort(key=lambda x: x['id'])

            x, y, w, h = self._create_composite_image(active_cues, output_path)

            # If image is fully transparent (empty), skip it
            if w == 0 or h == 0:
                continue

            # 4. CONVERT TIME (Drift Correction / Conform)
            # Formula: Target_Time = Source_Time * (Source_FPS / Target_FPS)
            # This handles the speed-up or slow-down (e.g. 23.976 -> 24)
            rate_ratio = self.src_fps_float / self.tgt_fps_float

            final_start_ms = t_start * rate_ratio
            final_end_ms = t_end * rate_ratio

            # PASS INT COMPONENTS FOR PRECISION
            in_tc = self._ms_to_tc(final_start_ms, self.tgt_fps_num, self.tgt_fps_den)
            out_tc = self._ms_to_tc(final_end_ms, self.tgt_fps_num, self.tgt_fps_den)

            bdn_events.append({
                "InTC": in_tc,
                "OutTC": out_tc,
                "Width": w, "Height": h,
                "X": x, "Y": y,
                "Filename": slice_filename
            })

            if seq_num % 10 == 0:
                print(f"  Composed Slice {seq_num} (Active Cues: {len(active_cues)})")

            # Cancel logic
            if progress_callback:
                progress_callback(i + 1, len(sorted_points) - 1, f"Composing Slice {seq_num}")

        # 5. WRITE XML
        self._write_bdn_xml(bdn_events)
        print(f"BDN Composition Complete: {len(bdn_events)} events.")

    def _create_composite_image(self, cues: List[Dict], output_path: str) -> Tuple[int, int, int, int]:
        """
        Merges images and returns the bounding box (x, y, w, h) of the content.
        """
        # Create base canvas (Transparent)
        # Using RGBA for Alpha support
        if self.target_resolution:
            canvas_w, canvas_h = self.target_resolution
        else:
            canvas_w, canvas_h = self.project.width, self.project.height

        canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

        for c in cues:
            src_path = os.path.join(self.input_dir, c['filename'])
            if os.path.exists(src_path):
                layer = Image.open(src_path).convert("RGBA")
                # Alpha composite puts the layer ON TOP of the canvas
                canvas.alpha_composite(layer)

        # Get Bounding Box of non-transparent pixels
        # bbox is (left, upper, right, lower)
        bbox = canvas.getbbox()

        if bbox:
            left, upper, right, lower = bbox

            # --- PIXEL PERFECT ALIGNMENT FIX ---
            # 1. Force X/Y to be EVEN (Floor)
            if left % 2 != 0: left -= 1
            if upper % 2 != 0: upper -= 1

            # 2. Force Right/Bottom to be EVEN (Ceil)
            # This ensures that Width (Right-Left) and Height (Bottom-Upper) are EVEN.
            if right % 2 != 0: right += 1
            if lower % 2 != 0: lower += 1

            # 3. Safety Clamp (Don't go out of canvas bounds)
            left = max(0, left)
            upper = max(0, upper)
            right = min(canvas_w, right)
            lower = min(canvas_h, lower)

            # Re-calculate Dimensions
            w = right - left
            h = lower - upper

            # 4. Crop & Save
            # We use the aligned box
            cropped = canvas.crop((left, upper, right, lower))

            # --- ANTI-CROP GUARD ---
            # bdsup2sub++ tries to be smart and re-crop transparent edges.
            # This undoes our "Even" padding, causing odd dimensions and triggering
            # the blurry resize/shift behavior you observed.
            # We fix this by placing nearly-invisible pixels (Alpha=1) in the corners.

            pixels = cropped.load()
            w_c, h_c = cropped.size

            # Guard Color: Black with 1/255 opacity (0.4%) - Invisible to eye, opaque to tool.
            guard = (0, 0, 0, 0)

            # Check and dirty the 4 corners if they are empty
            # Top-Left
            if pixels[0, 0][3] == 0: pixels[0, 0] = guard
            # Top-Right
            if pixels[w_c - 1, 0][3] == 0: pixels[w_c - 1, 0] = guard
            # Bottom-Left
            if pixels[0, h_c - 1][3] == 0: pixels[0, h_c - 1] = guard
            # Bottom-Right
            if pixels[w_c - 1, h_c - 1][3] == 0: pixels[w_c - 1, h_c - 1] = guard

            cropped.save(output_path, optimize=True)

            return left, upper, w, h
        else:
            return 0, 0, 0, 0

    def _ms_to_tc(self, ms: float, fps_num: int, fps_den: int) -> str:
        """
        Converts Milliseconds -> Total Frames -> HH:MM:SS:FF (SMPTE NDF).
        This handles the NTSC drift correctly by using Frame Count as the truth.
        """
        # 1. Calculate Total Frames from Real Time
        # Formula: Real_Seconds * Real_FPS
        total_seconds = ms / 1000.0

        # We use round() because timestamps should align to the nearest frame grid
        total_frames = int(round(total_seconds * (fps_num / fps_den)))

        # 2. Determine Timecode Base (Nominal FPS)
        # For 23.976 (24000/1001), Base is 24.
        # For 29.97 (30000/1001), Base is 30.
        fps_base = int(round(fps_num / fps_den))

        # 3. Convert Total Frames to HH:MM:SS:FF
        # Frames per Hour/Minute/Second of TIMECODE (not real time)
        frames_per_sec = fps_base
        frames_per_min = fps_base * 60
        frames_per_hr  = fps_base * 3600

        h = total_frames // frames_per_hr
        rem = total_frames % frames_per_hr

        m = rem // frames_per_min
        rem = rem % frames_per_min

        s = rem // frames_per_sec
        f = rem % frames_per_sec

        return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"

    def _write_bdn_xml(self, events: List[Dict]):
        root = ET.Element("BDN", Version="1.0", xmlns_xsi="http://www.w3.org/2001/XMLSchema-instance")

        # Header
        desc = ET.SubElement(root, "Description")
        ET.SubElement(desc, "Name").text = "TTML2PGS Export"
        ET.SubElement(desc, "Language", Code=self.project.language or "und")

        std_w = self.target_resolution[0]
        std_h = self.target_resolution[1]

        # In the new logic, there are no offsets.
        # The image is the full frame (or exactly the size of the video).
        offset_x = 0
        offset_y = 0

        # Construct Description
        desc = ET.SubElement(root, "Description")
        name = self.project.initial_style.id if self.project.initial_style else "Default"
        ET.SubElement(desc, "Name", Title=name, Content="")

        fps_str = f"{self.tgt_fps_float:.3f}".rstrip('0').rstrip('.')

        # CHANGED: Write 'Resolution' instead of 'VideoFormat'
        # This allows non-standard resolutions (e.g. 1920x803)
        res_str = f"{std_w}x{std_h}"
        ET.SubElement(desc, "Format", Resolution=res_str, FrameRate=fps_str, DropFrame="False")

        events_elem = ET.SubElement(root, "Events")

        # --- LOOP EVENTS WITH OFFSET ---
        for ev_data in events:
            event = ET.SubElement(events_elem, "Event",
                                  InTC=ev_data['InTC'],
                                  OutTC=ev_data['OutTC'],
                                  Forced="False")

            # Apply offsets to the coordinates here
            final_x = ev_data['X'] + offset_x
            final_y = ev_data['Y'] + offset_y

            # Ensure we don't go out of bounds (optional safety)
            final_x = max(0, final_x)
            final_y = max(0, final_y)

            g = ET.SubElement(event, "Graphic",
                              Width=str(ev_data['Width']),
                              Height=str(ev_data['Height']),
                              X=str(final_x),
                              Y=str(final_y))
            g.text = os.path.basename(ev_data['Filename'])

        # Pretty Print
        xml_str = minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")

        with open(os.path.join(self.slices_dir, "subtitles.bdn.xml"), "w", encoding="utf-8") as f:
            f.write(xml_str)