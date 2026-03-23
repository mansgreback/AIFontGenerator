#!/usr/bin/env python3
"""
Server Client for AI Font Generation

Handles server communication for font generation and glyph extraction.
"""

import json
import time
import base64
import ssl
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


class ServerClient:
    """Client for the font generation server."""

    BASE_URL = "https://aringtypeface.com/fontgen"

    # Polling settings
    MAX_WAIT_SECONDS = 480
    POLL_INTERVAL = 2

    def __init__(self, base_url=None):
        self.base_url = base_url or self.BASE_URL
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

    def _request(self, method, path, body=None, timeout=60, retries=2):
        """Make a request to the server with automatic retries on connection errors."""
        url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "AIFontGenerator/0.620",
        }
        data = None
        if body:
            data = json.dumps(body, separators=(',', ':')).encode('utf-8')

        last_error = None
        for attempt in range(1 + retries):
            req = Request(url, data=data, headers=headers, method=method)
            try:
                with urlopen(req, context=self.ssl_context, timeout=timeout) as response:
                    return json.loads(response.read().decode('utf-8'))
            except HTTPError as e:
                error_body = e.read().decode('utf-8') if e.fp else str(e)
                raise Exception(f"HTTP {e.code}: {error_body}")
            except URLError as e:
                last_error = e
                if attempt < retries:
                    import time as _time
                    _time.sleep(2 * (attempt + 1))
                    continue
        raise Exception(f"URL Error: {last_error.reason}")

    def generate_template(self, style_image_b64, progress_callback=None,
                          glyphs_user=None):
        """Generate a font template from style reference image via the server.

        Args:
            style_image_b64: Base64 encoded style reference image
            progress_callback: Optional callback(status_str) for progress updates
            glyphs_user: Optional user identification dict

        Returns:
            Tuple of (base64_image_data, log_dir)
        """
        if progress_callback:
            progress_callback("Sending to server...")

        gen_body = {
            "image_data": style_image_b64,
        }
        if glyphs_user:
            gen_body["glyphs_user"] = glyphs_user

        result = self._request("POST", "/generate", gen_body, timeout=30)
        if not result.get("success"):
            raise Exception(f"Generation failed: {result.get('error', 'Unknown error')}")

        job_id = result["job_id"]
        log_dir = result.get("log_dir")

        if progress_callback:
            progress_callback("Generating font template...")

        # Poll for completion
        start_time = time.time()
        while time.time() - start_time < self.MAX_WAIT_SECONDS:
            poll_result = self._request("POST", "/poll-job", {
                "job_id": job_id,
                "log_dir": log_dir
            }, timeout=15)

            if not poll_result.get("success"):
                raise Exception(f"Poll failed: {poll_result.get('error', 'Unknown error')}")

            status = poll_result.get("status")

            if progress_callback:
                elapsed = int(time.time() - start_time)
                progress_callback(f"Generating... ({elapsed}s elapsed)")

            if status == "success":
                image_url = poll_result.get("image_url")
                break
            elif status == "failed":
                raise Exception(f"Generation failed: {poll_result.get('error', 'Unknown error')}")

            time.sleep(self.POLL_INTERVAL)
        else:
            raise Exception("Generation timed out")

        if progress_callback:
            progress_callback("Downloading result...")

        # Fetch the generated image
        proxy_result = self._request("POST", "/proxy-image", {
            "url": image_url,
            "log_dir": log_dir
        }, timeout=30)

        if not proxy_result.get("success"):
            raise Exception(f"Image download failed: {proxy_result.get('error', 'Unknown error')}")

        image_b64 = proxy_result.get("image_data", "")
        if "," in image_b64:
            image_b64 = image_b64.split(",", 1)[1]

        return image_b64, log_dir

    def extract_glyphs(self, template_image_b64, font_metrics=None,
                       log_dir=None, progress_callback=None):
        """Extract glyph data from a template image.

        Args:
            template_image_b64: Base64 encoded template image
            font_metrics: Optional dict with keys: units_per_em, ascender, descender, cap_height, x_height
            log_dir: Optional log directory from generate_template
            progress_callback: Optional callback(status_str)

        Returns:
            Tuple of (glyph_data, bg_glyph_data).
            Each dict maps glyph_name to {paths, width, unicode, anchors, [is_composite, components]}
        """
        if progress_callback:
            progress_callback("Extracting glyphs on server...")

        body = {
            "image_data": template_image_b64,
            "log_dir": log_dir,
        }

        if font_metrics:
            body.update(font_metrics)

        result = self._request("POST", "/extract-glyphs", body, timeout=30)

        if not result.get("success"):
            raise Exception(f"Glyph extraction failed: {result.get('error', 'Unknown error')}")

        job_id = result.get("job_id")
        if not job_id:
            raise Exception("Server did not return a job_id for extraction")

        # Poll for completion
        start_time = time.time()
        while time.time() - start_time < self.MAX_WAIT_SECONDS:
            poll_result = self._request("POST", "/poll-extract", {
                "job_id": job_id
            }, timeout=15)

            if not poll_result.get("success"):
                raise Exception(f"Poll failed: {poll_result.get('error', 'Unknown error')}")

            status = poll_result.get("status")

            if progress_callback:
                elapsed = int(time.time() - start_time)
                progress_callback(f"Extracting glyphs... ({elapsed}s elapsed)")

            if status == "success":
                glyph_data = poll_result.get("glyph_data", {})

                for glyph_name, data in glyph_data.items():
                    if data.get('anchors'):
                        data['anchors'] = [tuple(a) for a in data['anchors']]

                bg_glyph_data = poll_result.get("bg_glyph_data")
                if bg_glyph_data:
                    for glyph_name, data in bg_glyph_data.items():
                        if data.get('anchors'):
                            data['anchors'] = [tuple(a) for a in data['anchors']]

                if progress_callback:
                    progress_callback(f"Extracted {len(glyph_data)} glyphs")

                return glyph_data, bg_glyph_data

            elif status == "failed":
                raise Exception(f"Extraction failed: {poll_result.get('error', 'Unknown error')}")

            time.sleep(self.POLL_INTERVAL)

        raise Exception("Glyph extraction timed out")
