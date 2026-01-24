import traceback
import os
import json
import threading
from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from PyQt6.QtWidgets import QApplication

from core.image_batcher import ImageBatcher
from core.bdn_composer import BdnComposer
from core.exporter import SupExporter
from core.models import SubtitleProject
from core.remuxer import Remuxer


class PipelineWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished_success = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, project: SubtitleProject, output_dir: str, out_filename: str,
                 target_fps, viewport_res, content_res, offset_ms, overrides,
                 bdsup_exe, selected_cues_only, active_cues_ids):
        super().__init__()
        self.project = project
        self.output_dir = output_dir
        self.out_filename = out_filename
        self.target_fps = target_fps
        self.viewport_res = viewport_res
        self.content_res = content_res # sets aspect ratio within the html
        self.offset_ms = offset_ms
        self.overrides = overrides
        self.bdsup_exe = bdsup_exe
        self.selected_cues_only = selected_cues_only
        self.active_cues_ids = active_cues_ids

        # self._is_running = False
        self.cancel_requested = False

    # def isRunning(self):
    #     return self._is_running

    # def start(self):
    #     print(f"[DEBUG] PipelineWorker.start called on thread: {threading.get_ident()}")
    #     #self._is_running = True
    #     self.cancel_requested = False
    #     # Schedule run() on the next event loop iteration (Main Thread)
    #     QTimer.singleShot(0, self.run)

    def cancel(self):
        print("[DEBUG] PipelineWorker.cancel called")
        self.cancel_requested = True

    def run(self):
        print(f"[DEBUG] PipelineWorker.run execution started on thread: {threading.get_ident()}")
        try:
            # 1. SETUP BATCHER
            print("[DEBUG] Initializing ImageBatcher...")
            batcher = ImageBatcher(self.project, self.output_dir)

            batcher.set_resolutions(self.viewport_res, self.content_res)

            batcher.set_target_framerate(self.target_fps[0], self.target_fps[1])
            batcher.set_timing_offset(self.offset_ms)

            print(f"[DEBUG] Applying overrides: {self.overrides.keys()}")
            # We must filter out the auto-color settings to prevent a TypeError
            batcher_args = self.overrides.copy()
            keys_to_remove = [
                'window_bg',
                'auto_color_enabled',
                'auto_sdr_color', 'auto_sdr_alpha',
                'auto_hdr_color', 'auto_hdr_alpha',
                'force_16_9', 'remux_enabled',
                'override_ar_enabled', 'ar_num', 'ar_den',
                'cleanup_enabled', 'move_enabled', 'web_view',
                "use_video_dims",
                "scale_to_hd"
            ]
            for k in keys_to_remove:
                if k in batcher_args:
                    batcher_args.pop(k)

            batcher.set_style_overrides(**batcher_args)

            # Filter Logic
            original_cues = self.project.body.cues
            if self.selected_cues_only:
                filtered = [c for c in original_cues if c.active]
                self.project.body.cues = filtered
                print(f"[DEBUG] Filtered cues: {len(filtered)} / {len(original_cues)}")

            # 2. RUN BATCHER
            self.progress.emit(0, 0, "Starting Render...")
            print("[DEBUG] Starting Batcher.run()...")



            # --- THROTTLE PROCESS EVENTS ---
            # self.last_pct = -1

            def ui_alive_callback(curr, total, msg):
                # Emit signal always
                self.progress.emit(curr, total, msg)

            # FIX: Lambda must accept '_self' argument implicitly passed by Python
            cancel_event_shim = type('EventShim', (object,), {'is_set': lambda _self: self.cancel_requested})()

            batcher.run(debug_mode=False,
                        progress_callback=ui_alive_callback,
                        cancel_event=cancel_event_shim)

            print("[DEBUG] Batcher.run() finished.")
            self.project.body.cues = original_cues

            if self.cancel_requested:
                print("[DEBUG] Cancel detected after Batcher.")
                self.finished.emit()
                return

            # 3. RUN COMPOSER
            print("[DEBUG] Starting Composer...")
            manifest_path = f"{self.output_dir}/manifest.json"
            with open(manifest_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            composer = BdnComposer(self.project, batcher.images_dir, self.output_dir,
                                   target_fps_num=self.target_fps[0],
                                   target_fps_den=self.target_fps[1],
                                   target_resolution=self.viewport_res)

            # FIX: Point to 'cancel_event_shim' instead of 'self.cancel_event'
            composer.compose(data['cues'],
                             progress_callback=ui_alive_callback,
                             cancel_event=cancel_event_shim)

            if self.cancel_requested:
                print("[DEBUG] Cancel detected after Composer.")
                self.finished.emit()
                return

            # 4. EXPORT SUP
            self.progress.emit(0, 0, "Exporting .sup file...")
            print("[DEBUG] Exporting SUP...")

            # QApplication.processEvents()

            bdn_xml = f"{composer.slices_dir}/subtitles.bdn.xml"
            out_sup = os.path.join(self.output_dir, self.out_filename)

            exporter = SupExporter(self.bdsup_exe)
            exporter.export_to_sup(bdn_xml, out_sup, target_resolution=self.viewport_res)

            print(f"[DEBUG] SUCCESS. Output: {out_sup}")
            self.finished_success.emit(out_sup)

        except Exception as e:
            print(f"[ERROR] CRASH inside PipelineWorker.run: {e}")
            traceback.print_exc()
            self.error_occurred.emit(str(e))
        finally:
            # self._is_running = False
            self.finished.emit()
            print("[DEBUG] PipelineWorker.run exiting.")


class RemuxWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal()
    finished_success = pyqtSignal(str)  # Emits message
    error_occurred = pyqtSignal(str)

    def __init__(self, mode='single', data=None):
        """
        mode: 'single' or 'batch'
        data: For 'single': {'video': str, 'subs': list}
              For 'batch': List of dicts [{'video':..., 'sup':..., 'lang':...}]
        """
        super().__init__()
        self.mode = mode
        self.data = data
        self.cancel_requested = False

    def cancel(self):
        self.cancel_requested = True

    def run(self):
        try:
            remuxer = Remuxer()

            # --- CALLBACK HELPER ---
            def on_progress(curr, total, msg):
                self.progress.emit(curr, total, msg)

            if self.mode == 'single':
                vid = self.data.get('video')
                subs = self.data.get('subs')
                if vid and subs:
                    self.progress.emit(0, 100, f"Remuxing {os.path.basename(vid)}...")
                    success = remuxer.remux_video(vid, subs, progress_callback=on_progress)
                    if success:
                        self.finished_success.emit("Remux Complete")
                    else:
                        self.error_occurred.emit("Remux Failed")

            elif self.mode == 'batch':
                # Group by Video
                queue = self.data
                groups = {}
                for item in queue:
                    v = item['video']
                    if v not in groups: groups[v] = []
                    groups[v].append({
                        'path': item['sup'],
                        'lang': item['lang']
                    })

                total_vids = len(groups)
                for i, (vid, subs) in enumerate(groups.items()):
                    if self.cancel_requested: break

                    base_name = os.path.basename(vid)
                    prefix = f"[{i + 1}/{total_vids}] {base_name}"

                    # Custom callback to include file count in message
                    def batch_progress(c, t, m):
                        self.progress.emit(c, t, f"{prefix}: {c}%")

                    self.progress.emit(0, 100, f"Remuxing {prefix}...")
                    remuxer.remux_video(vid, subs, progress_callback=batch_progress)

                self.finished_success.emit("Batch Remux Complete")

        except Exception as e:
            traceback.print_exc()
            self.error_occurred.emit(str(e))
        finally:
            self.finished.emit()