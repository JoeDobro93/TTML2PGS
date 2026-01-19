"""
core/pgs_encoder.py

Native Python encoder for Blu-ray Presentation Graphic Stream (PGS/SUP).
Encodes PNG images + XML Metadata directly to .sup files.

FIXES:
1. Smart Quantization: Handles images with >256 colors (gradients/shadows)
   by reducing bit-depth and prioritizing frequent colors instead of blanking out.
2. ID Management: Uses fixed IDs (0) for Epoch Start events to prevent overflow.
3. RLE: Ensures strict End-of-Line markers.
"""

import os
import struct
import math
import xml.etree.ElementTree as ET
from PIL import Image

class PgsEncoder:
    def __init__(self):
        self.sequence_index = 0

    def export(self, bdn_xml_path: str, output_sup_path: str):
        """
        Main entry point. Reads BDN XML, loads images, encodes to SUP.
        """
        print(f"[PGS] Starting Native Export: {output_sup_path}")
        self.sequence_index = 0

        # 1. Parse XML
        events, video_format = self._parse_bdn(bdn_xml_path)
        fps = video_format['fps']

        fps_num, fps_den = 0, 0

        if not fps or fps <= 0:
            print("[PGS] Warning: Invalid FPS. Defaulting to 23.976.")
            fps_num, fps_den = 24000, 1001
        elif abs(fps - 23.976) < 0.01:
            fps_num, fps_den = 24000, 1001
        elif abs(fps - 29.97) < 0.01:
            fps_num, fps_den = 30000, 1001
        elif abs(fps - 59.94) < 0.01:
            fps_num, fps_den = 60000, 1001
        elif abs(fps - 24.0) < 0.001:
            fps_num, fps_den = 24, 1
        elif abs(fps - 25.0) < 0.001:
            fps_num, fps_den = 25, 1
        elif abs(fps - 30.0) < 0.001:
            fps_num, fps_den = 30, 1
        elif abs(fps - 50.0) < 0.001:
            fps_num, fps_den = 50, 1
        elif abs(fps - 60.0) < 0.001:
            fps_num, fps_den = 60, 1
        else:
            # Fallback for custom FPS (preserve 3 decimal precision)
            fps_num = int(fps * 1000)
            fps_den = 1000
        print(f"[PGS] Framerate found to be {fps_num} / {fps_den}")

        # 2. Open Output File
        with open(output_sup_path, 'wb') as out_file:
            for idx, ev in enumerate(events):
                # Calculate Timestamps
                start_pts = self._tc_to_pts(ev['in_tc'], fps_num, fps_den)
                end_pts = self._tc_to_pts(ev['out_tc'], fps_num, fps_den)

                # Load Image
                img_path = os.path.join(os.path.dirname(bdn_xml_path), ev['filename'])
                if not os.path.exists(img_path):
                    print(f"[PGS] Warning: Image missing {img_path}")
                    continue

                img = Image.open(img_path).convert("RGBA")

                # Ensure Even Dimensions to prevent bottom-row glitches
                if img.width % 2 != 0 or img.height % 2 != 0:
                    new_w = img.width + (img.width % 2)
                    new_h = img.height + (img.height % 2)
                    new_img = Image.new("RGBA", (new_w, new_h), (0, 0, 0, 0))
                    new_img.paste(img, (0, 0))
                    img = new_img

                # IDs: We use 0 for everything since each subtitle is a fresh "Epoch Start"
                # This avoids running out of IDs (limit 255) on long files.
                obj_id = 0
                win_id = 0
                pal_id = 0

                # A. Generate Palette & Indexed Bitmap (Smart Quantize)
                palette_yuv, indexed_bitmap = self._quantize_image(img)

                # B. Compress Bitmap (RLE)
                rle_data = self._rle_compress(indexed_bitmap, img.width, img.height)

                # --- DISPLAY SET (SHOW) ---

                # 1. PCS (Presentation Composition Segment)
                # State 0x80 = Epoch Start (Resets Player State)
                self.sequence_index += 1
                pcs = self._create_pcs(
                    w=video_format['w'], h=video_format['h'], fps=fps, pts=start_pts,
                    state=0x80, comp_num=self.sequence_index,
                    pal_id=pal_id, obj_id=obj_id, win_id=win_id,
                    img_x=ev['x'], img_y=ev['y'],
                    img_w=img.width, img_h=img.height,
                    palette_update=False
                )
                out_file.write(pcs)

                # 2. WDS (Window Definition)
                wds = self._create_wds(
                    pts=start_pts, win_id=win_id,
                    x=ev['x'], y=ev['y'], w=img.width, h=img.height
                )
                out_file.write(wds)

                # 3. PDS (Palette Definition)
                pds = self._create_pds(pts=start_pts, pal_id=pal_id, palette=palette_yuv)
                out_file.write(pds)

                # 4. ODS (Object Definition)
                ods_packets = self._create_ods_packets(
                    pts=start_pts, obj_id=obj_id,
                    w=img.width, h=img.height, rle_data=rle_data,
                    debug_name=ev['filename']
                )
                for pkt in ods_packets:
                    out_file.write(pkt)

                # 5. END
                out_file.write(self._create_end(pts=start_pts))

                # --- DISPLAY SET (CLEAR) ---
                # Wipe screen at end_pts
                self.sequence_index += 1
                pcs_clear = self._create_pcs_clear(
                    w=video_format['w'], h=video_format['h'], fps=fps, pts=end_pts,
                    comp_num=self.sequence_index, state=0x80
                )
                out_file.write(pcs_clear)

                wds_clear = self._create_wds(pts=end_pts, win_id=win_id, x=ev['x'], y=ev['y'], w=img.width, h=img.height)
                out_file.write(wds_clear)

                out_file.write(self._create_end(pts=end_pts))

        print(f"[PGS] Export Complete.")

    # --- IMAGE QUANTIZATION (FIXED) ---
    def _quantize_image(self, img: Image.Image):
        """
        Converts RGBA to 8-bit indexed color using a smart fallback strategy
        to preserve transparency and details.
        """
        # Strategy 1: Check if it naturally fits in 256 colors
        colors = img.getcolors(maxcolors=256)

        if not colors:
            # Strategy 2: Reduce bit-depth (Posterize) to merge similar shadow pixels.
            # Mask 0xF0 keeps top 4 bits (16 levels per channel).
            # This is usually invisible for text but drastically lowers color count.
            img = img.point(lambda p: (p & 0xF0))
            colors = img.getcolors(maxcolors=256)

        if not colors:
            # Strategy 3: Truncate Rare Colors.
            # If still > 256, we count ALL colors, sort by frequency, and keep top 255.
            # Rare edge pixels will become transparent. This prevents the "Blank" bug.
            # Note: 65536 maxcolors covers 16-bit space, sufficient for posterized image.
            colors = img.getcolors(maxcolors=16777216)
            if colors:
                # Sort by count (descending)
                colors.sort(key=lambda x: x[0], reverse=True)
                # Keep top 255
                colors = colors[:255]
            else:
                # Absolute worst case fallback (should be impossible with posterization)
                print("[PGS] Critical: Image too complex. Output may have artifacts.")
                colors = []

        # Extract raw (r,g,b,a) tuples
        raw_colors = [c[1] for c in colors]

        # Ensure (0,0,0,0) is present and at Index 0
        if (0,0,0,0) in raw_colors:
            raw_colors.remove((0,0,0,0))

        palette_rgba = [(0,0,0,0)] + raw_colors

        # Trim if we somehow exceeded 256 (e.g. if we added transparent back)
        palette_rgba = palette_rgba[:256]

        # Create Mapping Dictionary
        color_map = {c: i for i, c in enumerate(palette_rgba)}

        # Convert Image Data to Indices
        pixels = list(img.getdata())

        # Map pixels. If a pixel isn't in the map (Strategy 3 dropped it),
        # map it to 0 (Transparent).
        indexed_pixels = [color_map.get(p, 0) for p in pixels]

        # Convert Palette to YUV
        palette_yuv = []
        for (r, g, b, a) in palette_rgba:
            # BT.601 Conversion
            y  = 16 + (0.257 * r) + (0.504 * g) + (0.098 * b)
            cb = 128 - (0.148 * r) - (0.291 * g) + (0.439 * b)
            cr = 128 + (0.439 * r) - (0.368 * g) - (0.071 * b)

            y = max(16, min(235, int(y)))
            cb = max(16, min(240, int(cb)))
            cr = max(16, min(240, int(cr)))

            palette_yuv.append((y, cr, cb, a))

        return palette_yuv, indexed_pixels

    # --- SEGMENT HELPERS (Unchanged structure, ensuring IDs) ---
    def _create_packet(self, pts, dts, segment_type, payload):
        header = b'PG'
        header += struct.pack('>I', int(pts))
        header += struct.pack('>I', int(dts))
        header += struct.pack('>B', segment_type)
        header += struct.pack('>H', len(payload))
        return header + payload

    def _create_pcs(self, w, h, fps, pts, state, comp_num, pal_id, obj_id, win_id, img_x, img_y, img_w, img_h, palette_update=False):
        # Determine Frame Rate Code
        code = 0x00
        if 23 < fps < 24: code = 0x01
        elif fps == 24: code = 0x02
        elif fps == 25: code = 0x03
        elif 29 < fps < 30: code = 0x04
        elif fps == 30: code = 0x05
        elif fps == 50: code = 0x06
        elif 59 < fps < 60: code = 0x07

        data = struct.pack('>HHB', w, h, code)
        data += struct.pack('>HB', comp_num, state)
        data += struct.pack('>BBB', 0x80 if palette_update else 0x00, pal_id, 1)
        data += struct.pack('>HBB', obj_id, win_id, 0)
        data += struct.pack('>HH', img_x, img_y)

        return self._create_packet(pts, pts, 0x16, data)

    def _create_pcs_clear(self, w, h, fps, pts, comp_num, state):
        code = 0x01 # Default or match source
        data = struct.pack('>HHB', w, h, code)
        data += struct.pack('>HB', comp_num, state)
        data += struct.pack('>BBB', 0x00, 0, 0) # 0 objects
        return self._create_packet(pts, pts, 0x16, data)

    def _create_wds(self, pts, win_id, x, y, w, h):
        data = struct.pack('>B', 1)
        data += struct.pack('>BHHHH', win_id, x, y, w, h)
        return self._create_packet(pts, pts, 0x17, data)

    def _create_pds(self, pts, pal_id, palette):
        data = struct.pack('>BB', pal_id, 0)
        for i, (y, cr, cb, a) in enumerate(palette):
            data += struct.pack('>BBBBB', i, y, cr, cb, a)
        return self._create_packet(pts, pts, 0x14, data)

    def _create_ods(self, pts, obj_id, w, h, rle_data):
        full_len = len(rle_data) + 4
        data = struct.pack('>HBB', obj_id, 0, 0xC0)
        data += struct.pack('>B', (full_len >> 16) & 0xFF)
        data += struct.pack('>H', full_len & 0xFFFF)
        data += struct.pack('>HH', w, h)
        data += rle_data
        return self._create_packet(pts, pts, 0x15, data)

    def _create_ods_packets(self, pts, obj_id, w, h, rle_data, debug_name=""):
        """
        Creates ODS packets. Splits large data into multiple segments if needed.
        (Max packet size ~65KB).
        """
        packets = []
        total_size = len(rle_data)

        # Max payload for ODS inside a PG packet.
        # PG Header (10) + ODS Header (11) + Payload <= 65535
        # 60000 is a safe chunk size.
        max_chunk = 60000

        if total_size > max_chunk:
            print(f"[PGS DEBUG] SPLIT PACKET: {debug_name} | Size: {total_size:,} bytes | Dims: {w}x{h}")

        offset = 0
        while offset < total_size:
            chunk_len = min(max_chunk, total_size - offset)
            chunk = rle_data[offset: offset + chunk_len]

            # Sequence Flag: 0x80=First, 0x40=Last, 0xC0=Both, 0x00=Middle
            seq_flag = 0x00
            is_first = (offset == 0)

            if is_first:
                seq_flag |= 0x80
            if offset + chunk_len >= total_size:
                seq_flag |= 0x40

            # ODS Header: ObjID(2), Ver(1), SeqFlag(1), Len(3)
            data = struct.pack('>HBB', obj_id, 0, seq_flag)

            # Determine Fragment Length & Content
            # Width(2) and Height(2) are ONLY present in the First segment (or Both)
            if is_first:
                full_obj_len = total_size + 4  # +4 for W/H
                data += struct.pack('>B', (full_obj_len >> 16) & 0xFF)
                data += struct.pack('>H', full_obj_len & 0xFFFF)
                data += struct.pack('>HH', w, h)
            else:
                # Subsequent fragments do not need a length field or W/H.
                # The data simply continues immediately after the Sequence Flag.
                pass

            # Append RLE Data
            data += chunk

            packets.append(self._create_packet(pts, pts, 0x15, data))

            offset += chunk_len

        return packets

    def _create_end(self, pts):
        return self._create_packet(pts, pts, 0x80, b'')

    # --- XML PARSING ---
    def _parse_bdn(self, xml_path):
        tree = ET.parse(xml_path)
        root = tree.getroot()
        fmt = root.find("Description/Format")
        fps_str = fmt.get("FrameRate", "23.976")
        fps = float(fps_str)
        vid_fmt = fmt.get("VideoFormat", "1080p")
        if "720" in vid_fmt: w, h = 1280, 720
        elif "480" in vid_fmt: w, h = 720, 480
        elif "576" in vid_fmt: w, h = 720, 576
        else: w, h = 1920, 1080

        video_format = {'fps': fps, 'w': w, 'h': h}
        events = []
        for ev in root.findall("Events/Event"):
            g = ev.find("Graphic")
            events.append({
                'in_tc': ev.get("InTC"),
                'out_tc': ev.get("OutTC"),
                'x': int(g.get("X")),
                'y': int(g.get("Y")),
                'filename': g.text
            })
        return events, video_format

    def _tc_to_pts(self, tc: str, fps_num: int, fps_den: int) -> int:
        """
        Converts SMPTE Timecode (NDF) -> Total Frames -> PTS (90kHz).

        OPTIMIZATION:
        Uses exact integer ratios (e.g. 24000/1001) for common framerates
        to avoid float drift over long durations.
        """
        # 1. Parse Timecode
        try:
            hh, mm, ss, ff = map(int, tc.split(':'))
        except ValueError:
            print(f"[PGS] Error: Invalid timecode format '{tc}'. Defaulting to 0.")
            return 0

        # 2. Determine Base FPS (Integer) for Frame Counting
        # NDF Timecode counts 0..23 even if real speed is 23.976.
        base_fps = int(round(fps_num / fps_den))

        # 3. Calculate Total Frames (NDF)
        total_frames = (hh * 3600 * base_fps) + (mm * 60 * base_fps) + (ss * base_fps) + ff

        # 4. Calculate Exact PTS
        # Formula: PTS = (TotalFrames / RealFPS) * 90000
        # Integer Math: (TotalFrames * 90000 * Den) // Num
        pts = (total_frames * 90000 * fps_den) // fps_num

        return pts

    # --- RLE COMPRESSION ---
    def _rle_compress(self, pixels, width, height):
        rle = bytearray()
        for row in range(height):
            start = row * width
            end = start + width
            line = pixels[start:end]
            i = 0
            while i < width:
                val = line[i]
                run = 1
                while i + run < width and line[i + run] == val and run < 16383:
                    run += 1

                if val == 0:
                    if run <= 63:
                        rle.extend(struct.pack('>BB', 0x00, run))
                    else:
                        rle.extend(struct.pack('>BB', 0x00, 0x40 | (run >> 8)))
                        rle.append(run & 0xFF)
                else:
                    if run == 1:
                        rle.append(val)
                    else:
                        if run <= 63:
                            rle.extend(struct.pack('>BB', 0x00, 0x80 | run))
                            rle.append(val)
                        else:
                            rle.extend(struct.pack('>BB', 0x00, 0xC0 | (run >> 8)))
                            rle.append(run & 0xFF)
                            rle.append(val)
                i += run
            rle.extend(b'\x00\x00') # End of Line
        return bytes(rle)