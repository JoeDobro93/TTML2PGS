"""
core/exporter.py

Wrapper for bdsup2sub++ execution.
Converts BDN XML (and its slice images) into a binary .sup file.
"""

import os
import subprocess
import shutil
import sys
from .pgs_encoder import PgsEncoder

"""
core/exporter.py

Wrapper for the Native PGS Encoder.
Converts BDN XML (and its slice images) into a binary .sup file using
the pure Python implementation (core.pgs_encoder), replacing bdsup2sub++.
"""

import os
import shutil
import traceback
from .pgs_encoder import PgsEncoder

class SupExporter:
    def __init__(self, exe_path: str = None):
        # exe_path is kept for interface compatibility but ignored.
        self.encoder = PgsEncoder()

    def export_to_sup(self, input_xml_path: str, output_sup_path: str, target_resolution=None):
        """
        Runs the native Python encoder.
        """
        input_abs = os.path.abspath(input_xml_path)
        output_abs = os.path.abspath(output_sup_path)

        print(f"--- STARTING SUP EXPORT (NATIVE) ---")
        print(f"Input: {input_abs}")
        print(f"Output: {output_abs}")

        try:
            # Call the Python Native Encoder
            self.encoder.export(input_abs, output_abs)

            if os.path.exists(output_abs):
                print(f"[SUCCESS] Generated: {output_abs}")
            else:
                raise FileNotFoundError("Native encoder finished but file missing.")

        except Exception as e:
            print(f"\n[EXPORTER ERROR] {e}")
            traceback.print_exc()
            raise

class SupExporterSup2Sub:
    def __init__(self, exe_path: str):
        self.exe_path = os.path.abspath(exe_path)

        # Verify executable exists
        if not os.path.exists(self.exe_path):
            print("HEREERE")
            #raise FileNotFoundError(f"bdsup2sub++ executable not found at: {self.exe_path}")

    def export_to_sup(self, input_xml_path: str, output_sup_path: str, target_resolution=None):
        """
        Runs bdsup2sub++ to convert XML -> SUP.
        """
        input_abs = os.path.abspath(input_xml_path)
        output_abs = os.path.abspath(output_sup_path)

        # Working Directory (Where the XML lives)
        work_dir = os.path.dirname(input_abs)
        input_filename = os.path.basename(input_abs)

        # SAFE TEMP NAME: We use this to avoid passing complex chars to the C++ tool
        temp_filename = "temp_export.sup"
        temp_full_path = os.path.join(work_dir, temp_filename)

        res_flag = "1080"  # Default
        if target_resolution:
            h = target_resolution[1]
            if h <= 480:
                res_flag = "480"
            elif h <= 576:
                res_flag = "576"
            elif h <= 720:
                res_flag = "720"
            elif h <= 1080:
                res_flag = "1080"

        print(f"--- STARTING SUP EXPORT ---")
        print(f"Resolution: {res_flag}p")
        print(f"Exe: {self.exe_path}")
        print(f"Input: {input_abs}")
        print(f"Output: {output_abs}")

        # Construct Command
        # Syntax: bdsup2sub++ -o <outfile> <infile>
        # We explicitly enforce 1080p resolution to ensure standard compliance
        cmd = [
            self.exe_path,
            "--resolution", res_flag,
            "-o", temp_filename,
            input_filename
        ]

        try:
            # Run process and capture output
            result = subprocess.run(
                cmd,
                cwd=work_dir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding='utf-8',
                errors='replace'
            )

            # Print tool output for debugging
            print("\n[BDSup2Sub++ Output]")
            print(result.stdout)

            if os.path.exists(temp_full_path):
                # Delete existing destination if present (shutil.move fails otherwise on some OS)
                if os.path.exists(output_abs):
                    os.remove(output_abs)

                shutil.move(temp_full_path, output_abs)
                print(f"[SUCCESS] Generated: {output_abs}")
            else:
                # If tool exit code was 0 but file missing
                raise FileNotFoundError(f"Tool finished but '{temp_filename}' was not created.")

        except subprocess.CalledProcessError as e:
            print(f"\n[CRITICAL ERROR] bdsup2sub++ failed with code {e.returncode}")
            print(e.stderr)
            raise
        except Exception as e:
            print(f"\n[EXPORTER ERROR] {e}")
            raise
        finally:
            # Cleanup temp file if something went wrong and it was left behind
            if os.path.exists(temp_full_path):
                try:
                    os.remove(temp_full_path)
                except:
                    pass