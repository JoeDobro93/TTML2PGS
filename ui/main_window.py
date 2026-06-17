import os
import shutil
import sys
import traceback
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QSplitter,
                             QProgressBar, QLabel, QStatusBar, QToolBar, QSpinBox,
                             QPushButton, QMessageBox, QApplication, QButtonGroup,
                             QRadioButton, QLineEdit, QFileDialog, QHBoxLayout,
                             QSizePolicy)
from PyQt6.QtCore import Qt, QThread

from .cues_pane import CuesPane
from .files_pane import FilesPane
from .preview_pane import PreviewPane
from .settings_pane import SettingsPane
from .workers import PipelineWorker, RemuxWorker
from .queue_window import QueueWindow
from . import app_settings
from core.remuxer import Remuxer


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TTML2PGS Ultimate")
        self.resize(1600, 1000)

        # --- LOAD PERSISTENT SETTINGS ---
        self._app_settings = app_settings.load()

        # --- TOOLBAR ---
        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)

        self.spin_src_fps_num = QSpinBox();
        self.spin_src_fps_num.setRange(1, 100000)
        self.spin_src_fps_den = QSpinBox();
        self.spin_src_fps_den.setRange(1, 100000)
        toolbar.addWidget(QLabel(" Src FPS: "))
        toolbar.addWidget(self.spin_src_fps_num)
        toolbar.addWidget(QLabel("/"))
        toolbar.addWidget(self.spin_src_fps_den)

        toolbar.addSeparator()

        # --- TEMP FILES SECTION ---
        self._build_temp_files_toolbar(toolbar)

        self.spin_tgt_fps_num = QSpinBox();
        self.spin_tgt_fps_num.setRange(1, 100000)
        self.spin_tgt_fps_den = QSpinBox();
        self.spin_tgt_fps_den.setRange(1, 100000)
        toolbar.addWidget(QLabel(" Target FPS: "))
        toolbar.addWidget(self.spin_tgt_fps_num)
        toolbar.addWidget(QLabel("/"))
        toolbar.addWidget(self.spin_tgt_fps_den)

        # --- CENTRAL WIDGET ---
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # Splitters
        h_split = QSplitter(Qt.Orientation.Horizontal)
        left_split = QSplitter(Qt.Orientation.Vertical)
        right_split = QSplitter(Qt.Orientation.Vertical)

        # Panes
        self.cues_pane = CuesPane()
        self.files_pane = FilesPane()
        self.preview_pane = PreviewPane()
        self.settings_pane = SettingsPane()

        # Layout Assembly
        # Left Side: Cues (Top), Files (Bottom)
        left_split.addWidget(self.cues_pane)
        left_split.addWidget(self.files_pane)

        # Right Side: Preview (Top), Settings (Bottom)
        right_split.addWidget(self.preview_pane)
        right_split.addWidget(self.settings_pane)

        # Main Horizontal Split
        h_split.addWidget(left_split)
        h_split.addWidget(right_split)

        main_layout.addWidget(h_split)

        # --- FORCE SPLIT SIZES (50/50) ---
        # We set large equal numbers to force the ratio
        left_split.setSizes([1000, 1000])
        right_split.setSizes([1000, 1000])
        h_split.setSizes([2200, 800])

        # --- STATUS BAR ---
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.status.addPermanentWidget(self.progress_bar)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self.cancel_worker)
        self.status.addPermanentWidget(self.btn_cancel)

        # --- CONNECTIONS ---
        self.files_pane.project_loaded.connect(self.on_project_loaded)
        self.files_pane.run_current.connect(self.run_current_job)
        self.files_pane.run_batch.connect(self.run_batch_jobs)

        self.cues_pane.cue_selected.connect(lambda c: self.preview_pane.render_cue(c))
        self.settings_pane.settings_changed.connect(self.on_settings_changed)
        self.settings_pane.regions_changed.connect(self.on_regions_changed)

        self.current_project = None
        self.current_is_hdr = False
        self.current_video_path = ""
        self.worker = None
        self.job_queue = []
        self.completed_jobs = []
        self.is_batch_mode = False

        self.queue_window = QueueWindow(self)
        self.current_job_config = None

        self.pending_single_remux = None
        self.batch_remux_queue = []  # Stores {'video': path, 'sup': path, 'lang': code}

    def _build_temp_files_toolbar(self, toolbar):
        """Add the 'Temp Files' controls to the toolbar."""
        toolbar.addWidget(QLabel(" Temp Files: "))

        self._radio_grp = QButtonGroup(self)
        self._rb_match = QRadioButton("Match Source")
        self._rb_custom = QRadioButton("Custom:")
        self._rb_match.setToolTip(
            "Temp image files are written to a subfolder inside the source subtitle/video directory (original behaviour).")
        self._rb_custom.setToolTip("Temp image files are written to a fixed folder of your choice.")

        toolbar.addWidget(self._rb_match)
        toolbar.addWidget(self._rb_custom)
        self._radio_grp.addButton(self._rb_match)
        self._radio_grp.addButton(self._rb_custom)

        self._lbl_temp_path = QLineEdit()
        self._lbl_temp_path.setReadOnly(True)
        self._lbl_temp_path.setPlaceholderText("(same as source)")
        self._lbl_temp_path.setFixedWidth(240)
        self._lbl_temp_path.setStyleSheet("color: #aaaaaa;")
        toolbar.addWidget(self._lbl_temp_path)

        self._btn_browse_temp = QPushButton("Browse…")
        self._btn_browse_temp.setFixedWidth(70)
        self._btn_browse_temp.clicked.connect(self._on_browse_temp_dir)
        toolbar.addWidget(self._btn_browse_temp)

        self._apply_temp_settings_to_ui()

        self._rb_match.toggled.connect(self._on_temp_mode_changed)
        self._rb_custom.toggled.connect(self._on_temp_mode_changed)

    def _apply_temp_settings_to_ui(self):
        """Sync toolbar widgets to the current _app_settings state."""
        mode = self._app_settings.get("temp_dir_mode", "match_source")
        custom_path = self._app_settings.get("temp_dir_custom", "")

        if mode == "custom":
            self._rb_custom.setChecked(True)
            self._lbl_temp_path.setText(custom_path)
            self._lbl_temp_path.setEnabled(True)
            self._btn_browse_temp.setEnabled(True)
        else:
            self._rb_match.setChecked(True)
            self._lbl_temp_path.setText("")
            self._lbl_temp_path.setEnabled(False)
            self._btn_browse_temp.setEnabled(False)

    def _on_temp_mode_changed(self):
        """Called when either radio button is toggled."""
        if self._rb_custom.isChecked():
            self._app_settings["temp_dir_mode"] = "custom"
            self._lbl_temp_path.setEnabled(True)
            self._btn_browse_temp.setEnabled(True)
        else:
            self._app_settings["temp_dir_mode"] = "match_source"
            self._lbl_temp_path.setText("")
            self._lbl_temp_path.setEnabled(False)
            self._btn_browse_temp.setEnabled(False)
        app_settings.save(self._app_settings)

    def _on_browse_temp_dir(self):
        """Open a folder picker and update the custom temp dir."""
        start = self._app_settings.get("temp_dir_custom", "") or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Select Temp Files Folder", start)
        if folder:
            self._app_settings["temp_dir_custom"] = folder
            self._lbl_temp_path.setText(folder)
            app_settings.save(self._app_settings)

    def _resolve_temp_dir(self, source_path: str) -> str:
        """Return the directory to use for temp files for the given job."""
        return app_settings.resolve_temp_dir(self._app_settings, source_path)

    def _apply_auto_color(self, overrides, is_hdr):
        """Resolves Auto-Color logic into specific Global Color/Alpha overrides."""
        if overrides.get('auto_color_enabled', False):
            if is_hdr:
                overrides['global_color'] = overrides['auto_hdr_color']
                overrides['global_alpha'] = overrides['auto_hdr_alpha']
                print(f"[PREVIEW] Auto-HDR Applied: {overrides['global_color']}")
            else:
                overrides['global_color'] = overrides['auto_sdr_color']
                overrides['global_alpha'] = overrides['auto_sdr_alpha']
                print(f"[PREVIEW] Auto-SDR Applied: {overrides['global_color']}")

            # Force the Renderer to treat this as an active override
            overrides['override_color'] = True
        return overrides

    def _calculate_viewport_res(self, overrides, video_res):
        """
        Resolve the OUTPUT pixel size that gets fed to the Chrome windows
        (and therefore the pop-out preview). Mirrors the logic used by
        process_next_job so the preview matches the final render exactly.
        """
        use_video_dims = overrides.get('use_video_dims', False)
        scale_to_hd = overrides.get('scale_to_hd', False)

        if use_video_dims and video_res:
            vw, vh = video_res
            if scale_to_hd:
                scale = min(1920 / vw, 1080 / vh)
                return (int(round(vw * scale)), int(round(vh * scale)))
            return (vw, vh)
        return (1920, 1080)

    # Helper method to calculate content_res based on priority
    def _resolve_content_res(self, overrides, original_res):
        if overrides.get('override_ar_enabled', False):
            # Priority 1: Manual AR Override
            num = overrides.get('ar_num', 1920.0)
            den = overrides.get('ar_den', 1080.0)
            print(f"[DEBUG] Layout: Manual AR Override {num}:{den}")
            return (num, den)
        elif overrides.get('force_16_9', False):
            # Priority 2: Force 16:9
            print("[DEBUG] Layout: Force 16:9 enabled.")
            return (1920, 1080)
        else:
            # Priority 3: Native Resolution
            return original_res or (1920, 1080)

    def on_project_loaded(self, project, target_fps, is_hdr=False, content_res=None, video_path=""):
        try:
            self.current_project = project
            self.current_is_hdr = is_hdr  # <--- Store State

            self.current_content_res = content_res
            self.current_video_path = video_path or ""

            self.spin_src_fps_num.setValue(project.fps_num)
            self.spin_src_fps_den.setValue(project.fps_den)

            self.spin_tgt_fps_num.setValue(target_fps[0])
            self.spin_tgt_fps_den.setValue(target_fps[1])

            overrides = self.settings_pane.get_overrides()
            overrides = self._apply_auto_color(overrides, is_hdr)

            final_res = self._resolve_content_res(overrides, content_res)
            viewport_res = self._calculate_viewport_res(overrides, content_res)

            self.preview_pane.set_project(project, overrides, final_res,
                                          viewport_res=viewport_res,
                                          video_res=content_res,
                                          video_path=self.current_video_path)
            self.cues_pane.load_project(project)

            self.settings_pane.load_project(project)

        except Exception as e:
            print(f"[ERROR] MainWindow.on_project_loaded crash: {e}")
            traceback.print_exc()

    def on_settings_changed(self, overrides):
        if self.current_project:
            overrides = self._apply_auto_color(overrides, self.current_is_hdr)
            # Use the stored resolution if available, else default to 1080p
            base_res = getattr(self, 'current_content_res', (1920, 1080))
            final_res = self._resolve_content_res(overrides, base_res)
            viewport_res = self._calculate_viewport_res(overrides, base_res)

            self.preview_pane.set_project(self.current_project, overrides, final_res,
                                          viewport_res=viewport_res,
                                          video_res=base_res,
                                          video_path=getattr(self, 'current_video_path', ""))

    def on_regions_changed(self):
        """Regions were added/renamed/removed in the Settings pane: refresh the
        Cues pane's region filter and in-table selector so they stay in sync."""
        try:
            self.cues_pane.refresh_regions()
        except Exception as e:
            print(f"[ERROR] on_regions_changed: {e}")

    def _refresh_queue_window(self):
        # Helper to ensure we always pass all 3 lists correctly
        self.queue_window.update_queue(self.completed_jobs, self.current_job_config, self.job_queue)

    def run_current_job(self, config):
        print("[DEBUG] run_current_job called")
        self.job_queue.append(config)
        self._refresh_queue_window()
        self.queue_window.show()

        # if not self.worker or not self.worker.isRunning():
        #     self.is_batch_mode = False
        #     self.process_next_job()

        if self.worker is None:
            self.is_batch_mode = False
            self.process_next_job()

    def run_batch_jobs(self, configs):
        print(f"[DEBUG] run_batch_jobs called with {len(configs)} configs")

        #if self.worker and self.worker.isRunning():
        if self.worker is not None:
            pass

        self.job_queue.extend(configs)
        print("[DEBUG] Updating queue window...")

        # --- FIX: Use the helper to pass ALL arguments correctly ---
        self._refresh_queue_window()
        # -----------------------------------------------------------

        self.queue_window.show()

        # if not self.worker or not self.worker.isRunning():
        #     print("[DEBUG] Starting batch processing...")
        #     self.is_batch_mode = True
        #     self.process_next_job()
        if self.worker is None:
            print("[DEBUG] Starting batch processing...")
            self.is_batch_mode = True
            self.process_next_job()

    def process_next_job(self):
        # 1. PRIORITY: Pending Single Remux (Run Immediately)
        # This triggers after the render thread has cleaned up.
        if self.pending_single_remux:
            print("[DEBUG] Starting Single Remux Worker...")
            data = self.pending_single_remux
            self.pending_single_remux = None  # Clear it

            self._start_remux_worker('single', data)
            return

        # 2. STANDARD QUEUE CHECK
        print(f"[DEBUG] process_next_job. Queue size: {len(self.job_queue)}")
        if not self.job_queue:
            self.current_job_config = None
            self._refresh_queue_window()

            # 3. BATCH REMUX CHECK (Run at end of batch)
            if self.is_batch_mode and self.batch_remux_queue:
                print("[DEBUG] Starting Batch Remux Worker...")
                data = list(self.batch_remux_queue)  # Copy
                self.batch_remux_queue = []  # Clear

                self._start_remux_worker('batch', data)
                return

            if self.is_batch_mode:
                QMessageBox.information(self, "Batch Complete", "All batch jobs finished successfully.")

            self.toggle_ui_lock(False)
            return

        self.current_job_config = self.job_queue.pop(0)
        self._refresh_queue_window()
        self.queue_window.update_progress(0)

        config = self.current_job_config

        # --- OVERRIDE out_dir WITH TEMP DIR SETTING ---
        source_for_temp = config.get('sub_path') or config.get('video_path', '')
        resolved_temp = self._resolve_temp_dir(source_for_temp)
        job_name = os.path.splitext(os.path.basename(source_for_temp))[0]
        config['out_dir'] = os.path.join(resolved_temp, f"_subbles_tmp_{job_name}")
        print(f"[TEMP] Temp dir for this job: {config['out_dir']}")

        # --- AUTO-COLOR LOGIC START ---
        # Get base settings
        overrides = self.settings_pane.get_overrides()
        is_hdr = config.get('is_hdr', False)

        # DEBUG PRINT: Verify what the system sees
        print(
            f"[DEBUG] Job: {config['out_filename']} | HDR Detected: {is_hdr} | Auto-Color: {overrides.get('auto_color_enabled')}")

        # Apply Auto-Color
        overrides = self._apply_auto_color(overrides, is_hdr)

        if overrides.get('auto_color_enabled'):
            print(f"[DEBUG] Applied Color: {overrides['global_color']} | Alpha: {overrides['global_alpha']}")
        # --- AUTO-COLOR LOGIC END ---

        tgt_fps = config['target_fps']

        tgt_res = config.get('target_res', None)
        print(f"[DEBUG] Job Target Res: {tgt_res}")

        offset_ms = config.get('offset_ms', 0)
        active_ids = None

        # 1. Output Resolution (shared logic with the live preview / pop-out)
        # Get Video Dimensions from Job Config (detected in FilesPane)
        video_res = config.get('target_res')  # e.g. (3840, 1606)
        viewport_res = self._calculate_viewport_res(overrides, video_res)
        print(f"[RES] Viewport (output) resolution: {viewport_res}")

        # 2. Content Resolution: Determines where text sits (Layout)
        original_res = config.get('target_res', (1920, 1080))
        content_res = self._resolve_content_res(overrides, original_res)

        bdsup = "resources/bdsup2sub++1.0.2_Win32.exe"
        # if not os.path.exists(bdsup):
        #     QMessageBox.critical(self, "Error", "bdsup2sub++ not found in resources/")
        #     return

        self.current_sub_name = os.path.basename(config['sub_path'])
        self.status.showMessage(f"{self.current_sub_name}: Starting...")

        self.worker = PipelineWorker(
            project=config['project'],
            output_dir=config['out_dir'],
            out_filename=config['out_filename'],
            target_fps=tgt_fps,
            viewport_res=viewport_res,  # The Canvas Size (ImageGenerator)
            content_res=content_res,  # The Layout Size (HtmlRenderer)
            offset_ms=offset_ms,
            overrides=overrides,
            bdsup_exe=bdsup,
            selected_cues_only=config['selected_only'],
            active_cues_ids=active_ids
        )

        # Create a Thread
        self.thread = QThread()

        # Move the Worker to the Thread
        self.worker.moveToThread(self.thread)

        # Connect Signals
        # Start the worker when the thread starts
        self.thread.started.connect(self.worker.run)

        self.worker.progress.connect(self.update_progress)
        self.worker.finished_success.connect(self.job_finished)
        self.worker.error_occurred.connect(lambda e: self.status.showMessage(f"Error: {e}"))

        # Cleanup: When worker finishes, quit the thread
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.worker_cleanup)

        self.toggle_ui_lock(True)
        self.thread.start()

    def _start_remux_worker(self, mode, data):
        """Helper to launch RemuxWorker on a background thread."""
        self.worker = RemuxWorker(mode=mode, data=data)
        self.thread = QThread()
        self.worker.moveToThread(self.thread)

        # Connect signals
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.update_progress)

        # RemuxWorker emits 'finished_success' with a status string (e.g. "Remux Complete")
        self.worker.finished_success.connect(lambda msg: self.status.showMessage(msg))
        self.worker.error_occurred.connect(lambda e: self.status.showMessage(f"Error: {e}"))

        # Cleanup when done
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.worker_cleanup)

        self.toggle_ui_lock(True)
        self.thread.start()

    def perform_batch_remux(self):
        """Groups finished batch jobs by video target and remuxes them together."""
        print("[BATCH] Starting Batch Remux...")

        # Group by Video Path
        # Structure: { 'video_path': [ {'path': sup, 'lang': lang}, ... ] }
        groups = {}

        for item in self.batch_remux_queue:
            v_path = item['video']
            if not os.path.exists(v_path):
                continue

            if v_path not in groups:
                groups[v_path] = []

            groups[v_path].append({
                'path': item['sup'],
                'lang': item['lang'] #,
                # 'title': f"TTML2PGS {item['lang']}"
            })

        # Execute Remux for each Video
        remuxer = Remuxer()
        count = 0
        total = len(groups)

        for vid, subs in groups.items():
            count += 1
            self.status.showMessage(f"Batch Remux ({count}/{total}): {os.path.basename(vid)}")
            QApplication.processEvents()

            print(f"[BATCH] Remuxing {len(subs)} tracks into {vid}")
            remuxer.remux_video(vid, subs)

        self.status.showMessage("Batch Remux Complete.")

    def update_progress(self, curr, total, msg):
        display_msg = f"{self.current_sub_name}: {msg}"
        self.status.showMessage(display_msg)

        if total > 0:
            pct = int((curr / total) * 100)
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(pct)
            try:
                self.queue_window.update_progress(pct)
            except Exception as e:
                print(f"[ERROR] MW update_progress queue update failed: {e}")

    def cancel_worker(self):
        if self.worker:
            self.status.showMessage("Cancelling...")
            self.worker.cancel()

        self.job_queue = []
        self.current_job_config = None
        self.completed_jobs = []  # Optional: clear completed on cancel?
        self._refresh_queue_window()
        self.queue_window.hide()

        # self.toggle_ui_lock(False)
        self.status.showMessage("Cancelled.")

    def worker_cleanup(self):
        self.progress_bar.setVisible(False)
        self.worker = None
        self.thread = None

        self.process_next_job()

    def job_finished(self, path):
        # 1. Gather Job Metadata
        config = self.current_job_config
        vid_path = config.get('video_path')
        sub_path = config.get('sub_path')
        project = config.get('project')

        # Fallback language code if not set
        lang_code = project.language if project and project.language else 'und'

        overrides = self.settings_pane.get_overrides()
        remux_enabled = overrides.get('remux_enabled', False)
        move_enabled = overrides.get('move_enabled', False)
        cleanup = overrides.get('cleanup_enabled', True)

        if cleanup:
            out_dir = config.get('out_dir')
            if out_dir:
                # Paths to clean
                p_images = os.path.join(out_dir, "images")
                p_slices = os.path.join(out_dir, "slices_for_bdn")
                p_manifest = os.path.join(out_dir, "manifest.json")

                try:
                    if os.path.exists(p_images):
                        shutil.rmtree(p_images)
                    if os.path.exists(p_slices):
                        shutil.rmtree(p_slices)
                    if os.path.exists(p_manifest):
                        os.remove(p_manifest)
                    # Remove the job temp folder itself if now empty
                    if os.path.exists(out_dir) and not os.listdir(out_dir):
                        os.rmdir(out_dir)
                    print(f"[CLEANUP] Temp files removed for {self.current_sub_name}")
                except Exception as e:
                    print(f"[CLEANUP] Failed to remove temp files: {e}")

        print(f"[DEBUG] Job Finished.")

        final_sup_path = path  # Default to current location

        if move_enabled and sub_path:
            try:
                # Determine target folder
                base_dir = os.path.dirname(sub_path)
                subs_dir = os.path.join(base_dir, "subbles")

                if not os.path.exists(subs_dir):
                    os.makedirs(subs_dir)

                # A. Move Source Subtitle (.ttml/.vtt)
                src_name = os.path.basename(sub_path)
                new_src_path = os.path.join(subs_dir, src_name)

                # Only move if not already there
                if sub_path != new_src_path:
                    shutil.move(sub_path, new_src_path)
                    print(f"[MOVE] Source moved to: {new_src_path}")

                # B. Move Generated .sup
                sup_name = os.path.basename(path)
                new_sup_path = os.path.join(subs_dir, sup_name)

                if path != new_sup_path:
                    shutil.move(path, new_sup_path)
                    final_sup_path = new_sup_path  # Update path for Remuxer!
                    print(f"[MOVE] SUP moved to: {new_sup_path}")

            except Exception as e:
                print(f"[MOVE] Error moving files: {e}")


        print(f" - Remux Enabled: {remux_enabled}")
        print(f" - Video Path:    {vid_path}")
        print(f" - File Exists:   {os.path.exists(vid_path) if vid_path else 'N/A'}")

        if not self.is_batch_mode:
            self.status.showMessage(f"Finished: {os.path.basename(final_sup_path)}")

            # --- SINGLE MODE REMUX ---
            if remux_enabled and vid_path and os.path.exists(vid_path):
                print(f"[DEBUG] Queuing Single Remux for: {vid_path}")
                # We store this in a variable. 'worker_cleanup' will call 'process_next_job',
                # which will see this variable and start the RemuxWorker.
                self.pending_single_remux = {
                    'video': vid_path,
                    'subs': [{'path': final_sup_path, 'lang': lang_code}]
                } #TODO: can include 'title': for more labeling (HDR etc)

                # QApplication.processEvents()  # Force UI update
                #
                # remuxer = Remuxer()
                # subs = [{'path': final_sup_path, 'lang': lang_code}]
                #
                # success = remuxer.remux_video(vid_path, subs)
                # if success:
                #     self.status.showMessage("Remux Complete!")
                # else:
                #     self.status.showMessage("Remux Failed! Check console.")

        else:
            print(f"[BATCH] Finished: {final_sup_path}")
            # --- BATCH MODE: QUEUE FOR LATER ---
            if remux_enabled and vid_path:
                self.batch_remux_queue.append({
                    'video': vid_path,
                    'sup': final_sup_path,
                    'lang': lang_code
                })

        # Move current job to completed list
        if self.current_job_config:
            self.completed_jobs.append(self.current_job_config)

        # self.process_next_job()

    def toggle_ui_lock(self, locked):
        self.files_pane.btn_run_curr.setEnabled(True)

        # Keep Batch run disabled while running
        self.files_pane.btn_run_batch.setEnabled(not locked)

        self.btn_cancel.setVisible(locked)