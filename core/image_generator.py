"""
core/image_generator.py

Headless Browser Wrapper for creating .png images from HTML cues.
Uses Selenium + Chrome DevTools Protocol (CDP) to ensure:
1. Exact Viewport Sizing (1920x1080, bypassing window borders)
2. True Alpha Transparency (bypassing default white canvas)
"""

import os
import tempfile
import time
import shutil
import urllib.parse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager

from .models import SubtitleProject

class ImageGenerator:
    def __init__(self, project: SubtitleProject, output_resolution=None):
        self.project = project
        self.output_resolution = output_resolution
        self._user_data_dir = None
        self.driver = self._setup_driver()
        self._configure_viewport()

    def _setup_driver(self) -> webdriver.Chrome:
        """Configures Headless Chrome."""
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--hide-scrollbars")

        # Each instance gets its own isolated temp profile directory so that
        # parallel Chrome processes don't share state or lock each other out.
        options.add_argument("--disable-extensions")
        self._user_data_dir = tempfile.mkdtemp(prefix="subbles_chrome_")
        options.add_argument(f"--user-data-dir={self._user_data_dir}")

        # --- 1. CHECK FOR BUNDLED (OFFLINE) CHROME ---
        # Look for a 'bin' folder in the current working directory
        cwd = os.getcwd()
        bundled_chrome = os.path.join(cwd, "bin", "chrome-win64", "chrome.exe")
        bundled_driver = os.path.join(cwd, "bin", "chromedriver-win64", "chromedriver.exe")

        service = None

        if os.path.exists(bundled_chrome) and os.path.exists(bundled_driver):
            print(f"[DEBUG] Using Bundled Offline Chrome: {bundled_chrome}")
            options.binary_location = bundled_chrome
            service = ChromeService(executable_path=bundled_driver)

        else:
            # --- 2. FALLBACK TO SYSTEM CHROME ---
            print("[DEBUG] Bundled Chrome not found. Attempting System Chrome...")

            try:
                # Attempt to use ChromeDriverManager to find/update the driver (requires internet)
                driver_path = ChromeDriverManager().install()
                service = ChromeService(driver_path)
            except Exception:
                # Fallback for offline mode: Use the default service
                # This relies on a driver being in your PATH or Selenium's internal cache
                print("[WARNING] Internet unreachable: Skipping driver update check and using default/system driver.")
                service = ChromeService()

        try:
            driver = webdriver.Chrome(service=service, options=options)
            return driver
        except Exception as e:
            print("[CRITICAL] Failed to initialize Chrome Driver.")
            print(f"Path used: {bundled_chrome if os.path.exists(bundled_chrome) else 'System Default'}")
            raise e

    def _configure_viewport(self):
        """
        Uses Chrome DevTools Protocol (CDP) to force exact render settings.
        This overrides the "window size" logic which often creates 1898x930 artifacts.
        """
        if self.output_resolution:
            width, height = self.output_resolution
            print(f"[DEBUG] ImageGenerator: Enforcing Target Resolution: {width}x{height}")
        else:
            width = self.project.width
            height = self.project.height

        # 1. Force Exact Viewport Resolution
        # This tells Chrome: "The screen is exactly this big, ignore window borders."
        self.driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "width": width,
            "height": height,
            "deviceScaleFactor": 1,
            "mobile": False,
        })

        # 2. Force Transparent Background
        # This tells Chrome: "The default canvas color is 00000000 (Transparent), not White."
        self.driver.execute_cdp_cmd("Emulation.setDefaultBackgroundColorOverride", {
            "color": {"r": 0, "g": 0, "b": 0, "a": 0}
        })

    def get_image_bytes(self, html_content: str) -> bytes:
        """
        Loads HTML via a base64 data URI to avoid Chrome's Trusted Types restriction
        (enforced from Chrome 143+) which blocks document.write() with arbitrary HTML.
        """
        import base64
        encoded = base64.b64encode(html_content.encode('utf-8')).decode('ascii')
        self.driver.get(f"data:text/html;base64,{encoded}")

        try:
            self.driver.execute_async_script("""
                var callback = arguments[arguments.length - 1];
                document.fonts.ready.then(callback);
            """)
        except Exception:
            time.sleep(0.02)

        return self.driver.get_screenshot_as_png()

    def render_html_to_png(self, html_content: str, output_path: str):
        """
        OPTIMIZED: Uses Data URIs to avoid disk I/O and checks document.fonts.ready
        instead of sleeping.
        """
        # 1. Use Data URI to avoid file I/O overhead
        # This encodes the HTML into the URL string itself.
        encoded_html = urllib.parse.quote(html_content)
        url = f"data:text/html;charset=utf-8,{encoded_html}"

        self.driver.get(url)

        # 2. Smart Wait for Fonts
        # Instead of sleeping blindly, we wait for the browser to report that fonts are parsed.
        # This returns instantly if they are already ready.
        try:
            self.driver.execute_async_script("""
                var callback = arguments[arguments.length - 1];
                document.fonts.ready.then(callback);
            """)
        except Exception:
            # Fallback for safety if the script fails (rare)
            print("[DEBUG IMG GENERATOR] SLEEP TIMER USED]")
            time.sleep(0.02)

        # 3. Take screenshot
        self.driver.save_screenshot(output_path)

    def close(self):
        if self.driver:
            self.driver.quit()
        if self._user_data_dir and os.path.exists(self._user_data_dir):
            shutil.rmtree(self._user_data_dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()