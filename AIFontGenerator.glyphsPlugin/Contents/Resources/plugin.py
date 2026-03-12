# encoding: utf-8

"""
AI Font Generator - Glyphs.app General Plugin

Takes selected glyph(s) as style reference, sends them to the server
to generate a complete font, and imports the AI-generated glyphs
into the current font.
"""

from __future__ import division, print_function, unicode_literals
import objc
import base64
import traceback
import os
import time
import json
import ssl
import tempfile
import zipfile
import shutil
from io import BytesIO

PLUGIN_VERSION = "0.600"
VERSION_CHECK_URL = "https://aringtypeface.com/fontgen/plugin_version.json"

from GlyphsApp import Glyphs, GSGlyph, GSPath, GSNode, GSComponent, GSAnchor, GSLINE, GSCURVE, GSOFFCURVE, Message, FILTER_MENU
from GlyphsApp.plugins import GeneralPlugin
from AppKit import NSMenuItem

# Import AppKit for rendering and UI
from AppKit import (
    NSImage, NSBitmapImageRep, NSPNGFileType, NSColor, NSBezierPath,
    NSAffineTransform, NSGraphicsContext, NSCompositingOperationSourceOver,
    NSZeroRect, NSUnionRect,
    NSWindow, NSTextField,
    NSWindowStyleMaskTitled,
    NSBackingStoreBuffered,
    NSFont, NSCenterTextAlignment
)
from Foundation import NSMakeRect, NSMakeSize


class ProgressWindowController:
    """A simple progress window shown during generation."""

    def __init__(self):
        self._window = None
        self._label = None

    def _create_window(self):
        """Create the progress window."""
        frame = NSMakeRect(0, 0, 360, 90)
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskTitled,
            NSBackingStoreBuffered,
            False
        )
        self._window.setTitle_("AI Font Generator")
        self._window.center()

        # Above Glyphs windows, visible when Glyphs loses focus, but not over other apps
        from AppKit import NSModalPanelWindowLevel
        self._window.setLevel_(NSModalPanelWindowLevel)
        self._window.setReleasedWhenClosed_(False)
        self._window.setHidesOnDeactivate_(False)

        content = self._window.contentView()

        # Simple text label
        self._label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 15, 320, 50))
        self._label.setStringValue_("Generating font...\nThis should take around 2-3 minutes.")
        self._label.setBezeled_(False)
        self._label.setDrawsBackground_(False)
        self._label.setEditable_(False)
        self._label.setSelectable_(False)
        self._label.setAlignment_(NSCenterTextAlignment)
        self._label.setFont_(NSFont.systemFontOfSize_(13))
        content.addSubview_(self._label)

        return self._window

    def show(self):
        """Show the progress window and flush the run loop so it actually appears."""
        self._create_window()
        self._window.makeKeyAndOrderFront_(None)
        self._window.display()
        # Flush the run loop to ensure the window renders before heavy work starts
        try:
            from AppKit import NSApplication, NSEventMaskAny, NSDate
            app = NSApplication.sharedApplication()
            # Process pending UI events so the window paints on screen
            deadline = NSDate.dateWithTimeIntervalSinceNow_(0.05)
            while True:
                event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                    NSEventMaskAny, deadline, "NSDefaultRunLoopMode", True
                )
                if event:
                    app.sendEvent_(event)
                else:
                    break
        except:
            pass

    def update(self):
        """Process events to keep UI responsive."""
        if self._window:
            try:
                from AppKit import NSApplication, NSEventMaskAny
                app = NSApplication.sharedApplication()
                for _ in range(3):
                    event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                        NSEventMaskAny, None, "NSDefaultRunLoopMode", True
                    )
                    if event:
                        app.sendEvent_(event)
            except:
                pass

    def complete(self):
        """Mark as complete."""
        pass

    def close(self):
        """Close the progress window."""
        if self._window:
            self._window.orderOut_(None)
            self._window = None
        self._label = None


def show_glyph_selection_dialog():
    """Show a dialog to choose glyph source and creation mode.

    Returns:
        dict with 'glyphs', 'layer', 'existing', 'sidebearings', 'vertical_metrics',
        'create_mode'
        None if user cancelled
    """
    from AppKit import (
        NSAlert, NSAlertFirstButtonReturn,
        NSMatrix, NSButtonCell, NSRadioModeMatrix, NSFont, NSView,
        NSButton, NSSwitchButton, NSControlStateValueOff, NSControlStateValueOn
    )

    alert = NSAlert.alloc().init()
    alert.setMessageText_("AI Font Generator")
    alert.setInformativeText_("")
    alert.addButtonWithTitle_("Generate")
    alert.addButtonWithTitle_("Cancel")

    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 350, 420))
    full_w = 350

    # --- Version label (gray, under title) ---
    versionLabel = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 406, full_w, 16))
    versionLabel.setStringValue_(f"v{PLUGIN_VERSION}")
    versionLabel.setBezeled_(False)
    versionLabel.setDrawsBackground_(False)
    versionLabel.setEditable_(False)
    versionLabel.setSelectable_(True)
    versionLabel.setFont_(NSFont.systemFontOfSize_(11))
    versionLabel.setTextColor_(NSColor.grayColor())
    versionLabel.setAlignment_(NSCenterTextAlignment)
    container.addSubview_(versionLabel)

    # --- Style reference section ---
    y = 365
    label1 = NSTextField.alloc().initWithFrame_(NSMakeRect(0, y, full_w, 18))
    label1.setStringValue_("Style reference:")
    label1.setBezeled_(False)
    label1.setDrawsBackground_(False)
    label1.setEditable_(False)
    label1.setSelectable_(False)
    label1.setFont_(NSFont.boldSystemFontOfSize_(12))
    container.addSubview_(label1)

    cell1 = NSButtonCell.alloc().init()
    cell1.setButtonType_(4)  # NSRadioButton
    cell1.setFont_(NSFont.systemFontOfSize_(13))

    glyphMatrix = NSMatrix.alloc().initWithFrame_mode_prototype_numberOfRows_numberOfColumns_(
        NSMakeRect(10, y - 52, full_w, 50),
        NSRadioModeMatrix,
        cell1,
        2, 1
    )
    glyphMatrix.setCellSize_((full_w, 22))
    glyphMatrix.setIntercellSpacing_((0, 4))
    glyphMatrix.cells()[0].setTitle_("Selected glyphs only")
    glyphMatrix.cells()[1].setTitle_("All glyphs with paths")
    glyphMatrix.selectCellAtRow_column_(0, 0)
    container.addSubview_(glyphMatrix)

    # --- Create as section ---
    y = 290
    label2 = NSTextField.alloc().initWithFrame_(NSMakeRect(0, y, full_w, 18))
    label2.setStringValue_("Create as:")
    label2.setBezeled_(False)
    label2.setDrawsBackground_(False)
    label2.setEditable_(False)
    label2.setSelectable_(False)
    label2.setFont_(NSFont.boldSystemFontOfSize_(12))
    container.addSubview_(label2)

    cellCreate = NSButtonCell.alloc().init()
    cellCreate.setButtonType_(4)  # NSRadioButton
    cellCreate.setFont_(NSFont.systemFontOfSize_(13))

    createMatrix = NSMatrix.alloc().initWithFrame_mode_prototype_numberOfRows_numberOfColumns_(
        NSMakeRect(10, y - 78, full_w, 75),
        NSRadioModeMatrix,
        cellCreate,
        3, 1
    )
    createMatrix.setCellSize_((full_w, 22))
    createMatrix.setIntercellSpacing_((0, 4))
    createMatrix.cells()[0].setTitle_("New master")
    createMatrix.cells()[1].setTitle_("Background in current master")
    createMatrix.cells()[2].setTitle_("Overwrite current master")
    createMatrix.selectCellAtRow_column_(0, 0)
    container.addSubview_(createMatrix)

    # --- Overwrite sub-options (only active when "Overwrite current master" is selected) ---
    cellOverwrite = NSButtonCell.alloc().init()
    cellOverwrite.setButtonType_(4)  # NSRadioButton
    cellOverwrite.setFont_(NSFont.systemFontOfSize_(12))

    overwriteMatrix = NSMatrix.alloc().initWithFrame_mode_prototype_numberOfRows_numberOfColumns_(
        NSMakeRect(30, y - 128, full_w - 20, 48),
        NSRadioModeMatrix,
        cellOverwrite,
        2, 1
    )
    overwriteMatrix.setCellSize_((full_w - 20, 22))
    overwriteMatrix.setIntercellSpacing_((0, 4))
    overwriteMatrix.cells()[0].setTitle_("Keep referenced glyphs")
    overwriteMatrix.cells()[1].setTitle_("Replace all")
    overwriteMatrix.selectCellAtRow_column_(0, 0)
    overwriteMatrix.setEnabled_(False)  # Disabled by default (New master selected)
    container.addSubview_(overwriteMatrix)

    # Callback to enable/disable overwrite sub-options
    def _poll_create_mode(timer):
        is_overwrite = createMatrix.selectedRow() == 2
        overwriteMatrix.setEnabled_(is_overwrite)
        for i in range(2):
            overwriteMatrix.cells()[i].setEnabled_(is_overwrite)

    from AppKit import NSTimer, NSRunLoop, NSRunLoopCommonModes
    poll_timer = NSTimer.timerWithTimeInterval_repeats_block_(0.15, True, _poll_create_mode)
    NSRunLoop.currentRunLoop().addTimer_forMode_(poll_timer, NSRunLoopCommonModes)

    # ========== WARNING TEXT (full width, bottom) ==========
    warningLabel = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 350, 105))
    warningLabel.setStringValue_("Use with caution: Remember to save a copy of your work before running this. All information will be sent to, processed, and stored on the server. Internet access is required. Do not submit any trademark/copyright protected or confidential works. The process will take a few minutes, during which you will not be able to use Glyphs.")
    warningLabel.setBezeled_(False)
    warningLabel.setDrawsBackground_(False)
    warningLabel.setEditable_(False)
    warningLabel.setSelectable_(False)
    warningLabel.setFont_(NSFont.systemFontOfSize_(10))
    warningLabel.setTextColor_(NSColor.secondaryLabelColor())
    warningLabel.setPreferredMaxLayoutWidth_(350)
    try:
        warningLabel.setLineBreakMode_(0)  # NSLineBreakByWordWrapping
    except:
        pass
    container.addSubview_(warningLabel)

    alert.setAccessoryView_(container)

    result = alert.runModal()
    poll_timer.invalidate()

    if result == NSAlertFirstButtonReturn:
        glyph_choice = 'selected' if glyphMatrix.selectedRow() == 0 else 'all'

        create_mode = createMatrix.selectedRow()  # 0=new master, 1=background, 2=overwrite
        if create_mode == 0:
            layer_choice = 'new_master'
            existing_choice = 'replace_all'
        elif create_mode == 1:
            layer_choice = 'background'
            existing_choice = 'replace_all'
        else:
            layer_choice = 'foreground'
            overwrite_row = overwriteMatrix.selectedRow()
            existing_choice = 'replace' if overwrite_row == 0 else 'replace_all'

        return {
            'glyphs': glyph_choice,
            'layer': layer_choice,
            'existing': existing_choice,
            'sidebearings': True,
            'vertical_metrics': True,
            'create_mode': create_mode,
        }
    else:
        return None


def _parse_version(version_str):
    """Parse a version string like '0.5' or '1.2.3' into a tuple of ints."""
    try:
        return tuple(int(x) for x in version_str.strip().split('.'))
    except (ValueError, AttributeError):
        return (0,)


def _check_version():
    """Check the plugin version against the server manifest.

    Returns:
        dict with keys:
            'status': 'ok' | 'update_available' | 'blocked'
            'latest_version': str or None
            'update_url': str or None
            'release_notes': str or None
            'error': str or None (only on network failure)
    """
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        cache_bust = f"?t={int(time.time())}"
        req = Request(VERSION_CHECK_URL + cache_bust, headers={"User-Agent": f"AIFontGenerator/{PLUGIN_VERSION}"})
        with urlopen(req, context=ctx, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))

        latest = data.get("latest_version", PLUGIN_VERSION)
        minimum = data.get("minimum_version", "0.0")
        update_url = data.get("update_url")
        release_notes = data.get("release_notes", "")

        current = _parse_version(PLUGIN_VERSION)
        min_ver = _parse_version(minimum)
        lat_ver = _parse_version(latest)

        if current < min_ver:
            return {
                "status": "blocked",
                "latest_version": latest,
                "update_url": update_url,
                "release_notes": release_notes,
            }
        elif current < lat_ver:
            return {
                "status": "update_available",
                "latest_version": latest,
                "update_url": update_url,
                "release_notes": release_notes,
            }
        else:
            return {"status": "ok"}

    except Exception as e:
        # Fail open — allow plugin to run if server is unreachable
        return {"status": "ok", "error": str(e)}


class AIFontGenerator(GeneralPlugin):
    """Glyphs.app General plugin for AI-based font generation.

    Uses GeneralPlugin instead of FilterWithoutDialog to get a single
    menu callback (no per-layer iteration issues).
    """

    @objc.python_method
    def settings(self):
        """Set up plugin name."""
        self.name = Glyphs.localize({
            'en': 'AI Font Generator',
            'de': 'KI-Schriftgenerator',
            'sv': 'AI-typsnittsgenerator'
        })

    @objc.python_method
    def start(self):
        """Plugin initialization - add menu item."""
        menuName = Glyphs.localize({
            'en': 'AI Generate Full Font',
            'de': 'KI-Schrift generieren',
            'sv': 'AI-generera typsnitt'
        })
        newMenuItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            menuName,
            objc.selector(self.generateFont_, signature=b'v@:@'),
            ""
        )
        newMenuItem.setTarget_(self)
        Glyphs.menu[FILTER_MENU].append(newMenuItem)

    _update_installed = False

    def generateFont_(self, sender):
        """Menu callback - called once when user clicks the menu item."""
        print(f"[AIFontGenerator v{PLUGIN_VERSION}] Starting...")

        if self._update_installed:
            Message(
                title="Restart Required",
                message="An update was installed. Please restart Glyphs to use the new version.",
                OKButton="OK"
            )
            return

        try:
            # --- Version check ---
            version_info = _check_version()

            if version_info["status"] == "blocked":
                self._show_blocked_dialog(version_info)
                return

            if version_info["status"] == "update_available":
                if not self._show_update_dialog(version_info):
                    return  # User chose to update (or cancelled)

            self._run_generation_with_dialog()
        except Exception as e:
            traceback.print_exc()
            Message(
                title="AI Font Generator Error",
                message=str(e),
                OKButton="OK"
            )

    @objc.python_method
    def _show_blocked_dialog(self, version_info):
        """Show dialog when the plugin version is too old to run."""
        from AppKit import NSAlert, NSAlertFirstButtonReturn

        alert = NSAlert.alloc().init()
        alert.setMessageText_("Update Required")
        alert.setInformativeText_(
            f"This version ({PLUGIN_VERSION}) of AI Font Generator is no longer supported.\n\n"
            f"Minimum required version: {version_info.get('latest_version', 'unknown')}\n\n"
            f"Please download the latest version to continue."
        )
        alert.addButtonWithTitle_("Download Update")
        alert.addButtonWithTitle_("Cancel")

        result = alert.runModal()
        if result == NSAlertFirstButtonReturn:
            url = version_info.get("update_url")
            if url:
                import webbrowser
                webbrowser.open(url)

    @objc.python_method
    def _show_update_dialog(self, version_info):
        """Show dialog when a newer version is available.

        Returns True if the user wants to continue with the current version,
        False if they chose to update or cancelled.
        """
        from AppKit import NSAlert, NSAlertFirstButtonReturn

        alert = NSAlert.alloc().init()
        alert.setMessageText_("Update Available")
        notes = version_info.get("release_notes", "")
        msg = (
            f"A new version ({version_info.get('latest_version', '?')}) of AI Font Generator is available.\n"
            f"You are running version {PLUGIN_VERSION}."
        )
        if notes:
            msg += f"\n\n{notes}"
        alert.setInformativeText_(msg)
        alert.addButtonWithTitle_("Update")
        alert.addButtonWithTitle_("Skip")

        result = alert.runModal()
        if result == NSAlertFirstButtonReturn:
            url = version_info.get("update_url")
            if url:
                self._perform_update(url)
            return False  # Don't continue after update attempt
        else:
            return True  # Skip update, continue with current version

    @objc.python_method
    def _perform_update(self, update_url):
        """Download and install a plugin update from the given URL."""
        from urllib.request import Request, urlopen

        progress = ProgressWindowController()
        progress.show()

        try:
            # Step 1: Download the zip
            progress._label.setStringValue_("Downloading update...")
            progress.update()

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            cache_bust = f"?t={int(time.time())}"
            req = Request(update_url + cache_bust, headers={"User-Agent": f"AIFontGenerator/{PLUGIN_VERSION}"})
            with urlopen(req, context=ctx, timeout=30) as resp:
                zip_data = resp.read()

            # Step 2: Save to temp file and extract
            progress._label.setStringValue_("Installing update...")
            progress.update()

            tmp_zip = tempfile.mktemp(suffix='.zip')
            tmp_dir = tempfile.mkdtemp(prefix='aifontgen_update_')

            with open(tmp_zip, 'wb') as f:
                f.write(zip_data)

            with zipfile.ZipFile(tmp_zip, 'r') as zf:
                zf.extractall(tmp_dir)

            # Step 3: Find the .glyphsPlugin bundle in extracted contents
            plugin_bundle = None
            for root, dirs, files in os.walk(tmp_dir):
                for d in dirs:
                    if d.endswith('.glyphsPlugin'):
                        plugin_bundle = os.path.join(root, d)
                        break
                if plugin_bundle:
                    break

            if not plugin_bundle:
                raise Exception("Update package does not contain a .glyphsPlugin bundle.")

            # Step 4: Replace installed plugin
            install_dir = os.path.expanduser(
                "~/Library/Application Support/Glyphs 3/Plugins"
            )
            install_path = os.path.join(install_dir, "AIFontGenerator.glyphsPlugin")

            if os.path.exists(install_path):
                shutil.rmtree(install_path)

            shutil.copytree(plugin_bundle, install_path)

            # Step 5: Clean up temp files
            try:
                os.remove(tmp_zip)
                shutil.rmtree(tmp_dir)
            except:
                pass

            progress.close()
            AIFontGenerator._update_installed = True

            Message(
                title="Update Installed",
                message="AI Font Generator has been updated successfully.\n\nPlease restart Glyphs to use the new version.",
                OKButton="OK"
            )

        except Exception as e:
            progress.close()
            traceback.print_exc()
            Message(
                title="Update Failed",
                message=f"Could not install update:\n{e}",
                OKButton="OK"
            )

    @objc.python_method
    def _run_generation_with_dialog(self):
        """Show options dialog, then show progress window BEFORE starting work."""
        font = Glyphs.font

        if not font:
            Message(title="No Font Open", message="Please open a font file first.", OKButton="OK")
            return

        options = show_glyph_selection_dialog()
        if options is None:
            print("[AIFontGenerator] User cancelled")
            return

        # Store options for the deferred call
        self._pending_options = options

        # Show progress window NOW, before any work
        self._progress = ProgressWindowController()
        self._progress.show()

        # Defer the actual work so the run loop can paint the progress window
        self.performSelector_withObject_afterDelay_(
            objc.selector(self.runDeferred_, signature=b'v@:@'),
            None,
            0.1
        )

    def runDeferred_(self, sender):
        """Called after a short delay so the progress window is visible."""
        try:
            self._run_generation()
        except Exception as e:
            if hasattr(self, '_progress') and self._progress:
                self._progress.close()
            traceback.print_exc()
            Message(title="AI Font Generator Error", message=str(e), OKButton="OK")

    @objc.python_method
    def _run_generation(self):
        """Internal method to run the font generation process."""

        font = Glyphs.font
        options = self._pending_options
        progress = self._progress

        glyph_choice = options['glyphs']
        layer_choice = options['layer']
        existing_choice = options.get('existing', 'replace')
        include_sidebearings = options.get('sidebearings', True)
        include_vertical_metrics = options.get('vertical_metrics', True)
        create_mode = options.get('create_mode', 2)  # 0=new master, 1=background, 2=overwrite

        # Get layers based on choice
        if glyph_choice == 'selected':
            # Use only selected glyphs
            selected_layers = font.selectedLayers
            if not selected_layers:
                Message(
                    title="No Selection",
                    message="Please select at least one glyph to use as style reference.",
                    OKButton="OK"
                )
                return
            layers_with_paths = [l for l in selected_layers if l.paths]
            if not layers_with_paths:
                Message(
                    title="Empty Selection",
                    message="Selected glyphs have no paths. Please select glyphs with visible outlines.",
                    OKButton="OK"
                )
                return
        else:
            # Use all glyphs with paths
            master_id = font.masters[0].id if font.masters else None
            if not master_id:
                Message(
                    title="No Master",
                    message="Font has no masters.",
                    OKButton="OK"
                )
                return
            layers_with_paths = []
            for glyph in font.glyphs:
                layer = glyph.layers[master_id]
                if layer and layer.paths:
                    layers_with_paths.append(layer)
            if not layers_with_paths:
                Message(
                    title="No Glyphs",
                    message="No glyphs with paths found in the font.",
                    OKButton="OK"
                )
                return

        # Collect reference glyph names (used to skip them if needed)
        reference_glyph_names = set(l.parent.name for l in layers_with_paths if l.parent)

        try:
            # Step 1: Rasterize selected glyphs
            progress.update()
            style_image_b64 = self._rasterize_glyphs(layers_with_paths, include_sidebearings=include_sidebearings, include_vertical_metrics=include_vertical_metrics)

            if not style_image_b64:
                progress.close()
                Message(
                    title="Rasterization Failed",
                    message="Could not rasterize the selected glyphs.",
                    OKButton="OK"
                )
                return

            # Step 2: Send to server for generation + glyph extraction
            progress.update()

            try:
                from server_client import ServerClient
            except ImportError:
                try:
                    from .server_client import ServerClient
                except ImportError:
                    import sys
                    plugin_dir = os.path.dirname(__file__)
                    if plugin_dir not in sys.path:
                        sys.path.insert(0, plugin_dir)
                    from server_client import ServerClient

            client = ServerClient()

            def progress_callback(status):
                progress.update()

            # Gather font metrics to pass to server for proper scaling
            master = font.masters[0] if font.masters else None
            font_metrics = {
                'units_per_em': font.upm,
                'ascender': int(master.ascender) if master else 800,
                'descender': int(abs(master.descender)) if master else 200,
                'cap_height': int(master.capHeight) if master else 700,
                'x_height': int(master.xHeight) if master and master.xHeight else None,
            }

            # Step 2a: Generate template
            progress.update()

            template_image_b64, log_dir = client.generate_template(
                style_image_b64,
                progress_callback=progress_callback
            )

            if not template_image_b64:
                progress.close()
                Message(
                    title="Generation Failed",
                    message="Server did not return a template image.",
                    OKButton="OK"
                )
                return

            # Step 3: Extract glyphs
            progress.update()

            glyph_data, bg_glyph_data = client.extract_glyphs(
                template_image_b64,
                font_metrics=font_metrics,
                log_dir=log_dir,
                progress_callback=progress_callback
            )

            # Accept partial results - even 1 glyph is better than none
            if not glyph_data or len(glyph_data) == 0:
                progress.close()
                Message(
                    title="Extraction Failed",
                    message="Could not extract any glyphs from the generated template. Check Macro Panel for details.",
                    OKButton="OK"
                )
                return

            # Step 4: Insert glyphs into font
            progress.update()

            # Handle "New master" mode: create a new master before inserting
            target_master_id = None
            if create_mode == 0:
                # Create a new master as a copy of the first master
                source_master = font.masters[0]
                new_master = source_master.copy()
                new_master.name = f"AI Generated ({time.strftime('%Y-%m-%d %H:%M')})"
                font.masters.append(new_master)
                target_master_id = new_master.id
                # Set layer_choice to 'foreground' — we write into the new master's foreground
                layer_choice = 'foreground'

            replaced_count = self._replace_glyphs(font, glyph_data, layer_choice, existing_choice, reference_glyph_names, target_master_id=target_master_id)

            if bg_glyph_data:
                self._replace_glyphs(font, bg_glyph_data, 'background', 'replace_all', set(), target_master_id=target_master_id)

            progress.update()

            # Complete progress and close
            progress.complete()
            progress.close()

            # Report generated glyph names
            generated_names = sorted(glyph_data.keys())
            print(f"[AIFontGenerator] Generated {replaced_count} glyphs: {', '.join(generated_names)}")

            # Switch to the new master and show Font view with all glyphs
            if create_mode == 0 and target_master_id:
                # Set icon parameter AFTER glyphs are inserted so "n" has paths
                try:
                    for m in font.masters:
                        if m.id == target_master_id:
                            m.customParameters["Master Icon Glyph Name"] = "n"
                            break
                except:
                    pass
                # Switch to font overview first, then select the new master
                try:
                    font.parent.windowController().showTabAtIndex_(0)
                except:
                    pass
                # Now set the master index (after view switch so it sticks)
                try:
                    for i, m in enumerate(font.masters):
                        if m.id == target_master_id:
                            font.masterIndex = i
                            break
                except:
                    pass
                # Force UI refresh
                try:
                    wc = font.parent.windowController()
                    wc.updateFont()
                    tbc = wc.tabBarControl()
                    if tbc:
                        tbc.update()
                        tbc.display()
                except:
                    pass

            # Done!
            output_msg = f"Generated {replaced_count} glyphs."
            if create_mode == 0:
                output_msg += f"\nCreated new master: {new_master.name}"

            Message(
                title="Generation Complete",
                message=output_msg,
                OKButton="OK"
            )

            return

        except Exception as e:
            progress.close()
            raise

    @objc.python_method
    def _distribute_glyphs_to_rows(self, layers, available_width, scale, glyph_spacing=0):
        """Distribute glyphs into rows based on their advance widths.

        Args:
            layers: List of GSLayer objects
            available_width: Available width in pixels
            scale: Current scale factor
            glyph_spacing: Extra spacing between glyphs in pixels

        Returns:
            List of rows, where each row is a list of layers
        """
        rows = []
        current_row = []
        current_width = 0

        for layer in layers:
            glyph_width = layer.width * scale
            spacing = glyph_spacing if current_row else 0

            if current_width + spacing + glyph_width <= available_width:
                current_row.append(layer)
                current_width += spacing + glyph_width
            else:
                if current_row:
                    rows.append(current_row)
                current_row = [layer]
                current_width = glyph_width

        if current_row:
            rows.append(current_row)

        return rows

    @objc.python_method
    def _rasterize_glyphs(self, layers, size=1024, padding=40, include_sidebearings=True, include_vertical_metrics=True):
        """Rasterize selected glyph layers to a PNG image with optional metric guides.

        The image can include horizontal guide lines for font metrics:
        - Orange: baseline
        - Yellow: cap height, x-height, descender

        Letters are drawn with a white outline so guides can pass behind them.
        Multiple glyphs are laid out in rows using their natural advance widths.

        Args:
            layers: List of GSLayer objects to rasterize
            size: Output image size (square)
            padding: Padding around glyphs
            include_sidebearings: If True, draw gray backgrounds and add spacing
            include_vertical_metrics: If True, draw baseline and other metric guides

        Returns:
            Base64 encoded PNG image data
        """
        if not layers:
            return None


        # Get font and master for metrics (used only for guide lines, not layout)
        font = Glyphs.font
        master = font.masters[0] if font and font.masters else None

        if master:
            ascender = master.ascender or 0
            descender = master.descender or 0
            x_height = (master.xHeight if hasattr(master, 'xHeight') else 0) or 0
            cap_height = (master.capHeight if hasattr(master, 'capHeight') else 0) or 0
        else:
            ascender = 0
            descender = 0
            x_height = 0
            cap_height = 0

        upm = (font.upm if font and font.upm else 1000) or 1000

        # Compute actual ink bounds from glyph paths — this drives layout, not metrics
        ink_top = float('-inf')
        ink_bottom = float('inf')
        for l in layers:
            if l.bounds and l.bounds.size.height > 0:
                ink_bottom = min(ink_bottom, l.bounds.origin.y)
                ink_top = max(ink_top, l.bounds.origin.y + l.bounds.size.height)

        if ink_top <= ink_bottom:
            # No ink at all — use UPM as fallback
            ink_top = upm * 0.7
            ink_bottom = -upm * 0.2

        # Row height = actual ink height (what matters for fitting glyphs on screen)
        row_metric_height = ink_top - ink_bottom
        if row_metric_height <= 0:
            row_metric_height = upm

        # For baseline positioning: use ink_bottom as the "descender" equivalent
        layout_ink_top = ink_top
        layout_ink_bottom = ink_bottom

        available_width = size - (padding * 2)
        available_height = size - (padding * 2)

        # Glyph spacing for non-sidebearings mode (fixed spacing)
        # When sidebearings enabled, spacing is calculated dynamically for even distribution
        glyph_spacing_pixels = 0 if include_sidebearings else 0

        # Find best scale and row distribution
        best_scale = 0
        best_rows = []

        for num_rows in range(1, len(layers) + 1):
            row_spacing = row_metric_height * 0.2
            total_row_height = num_rows * row_metric_height + (num_rows - 1) * row_spacing
            scale_for_height = available_height / total_row_height

            # For row distribution, use 0 spacing when sidebearings enabled (even distribution is flexible)
            dist_spacing = 0 if include_sidebearings else glyph_spacing_pixels
            rows = self._distribute_glyphs_to_rows(layers, available_width, scale_for_height, dist_spacing)

            if len(rows) <= num_rows:
                if scale_for_height > best_scale:
                    best_scale = scale_for_height
                    best_rows = rows
                break

        if not best_rows:
            best_rows = [[l] for l in layers]
            row_spacing = row_metric_height * 0.2
            total_row_height = len(best_rows) * row_metric_height + (len(best_rows) - 1) * row_spacing
            best_scale = available_height / total_row_height

        scale = best_scale
        rows = best_rows

        # Safety: ensure scale is valid (not zero, negative, or infinite)
        if not scale or scale <= 0 or scale != scale:  # catches 0, negative, NaN
            scale = available_height / (upm * 1.2)
            pass  # degenerate scale, using fallback


        # Scale-dependent thicknesses
        base_guide_thickness = 5.0
        guide_thickness = max(1.5, base_guide_thickness * scale * 2)
        outline_thickness = 60.0  # Font units - will be scaled naturally

        # Create image
        image = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
        image.lockFocus()
        _vec_transform_applied = False

        try:
            # White background
            NSColor.whiteColor().set()
            NSBezierPath.fillRect_(NSMakeRect(0, 0, size, size))

            row_height_scaled = row_metric_height * scale
            row_spacing_scaled = row_metric_height * 0.2 * scale
            total_content_height = len(rows) * row_height_scaled + (len(rows) - 1) * row_spacing_scaled

            # Position baseline using actual ink top (not cap_height metric)
            start_y = size - padding - (layout_ink_top * scale)
            vertical_offset = (available_height - total_content_height) / 2
            start_y -= vertical_offset

            # Helper to calculate row width including spacing
            def get_row_width(row_layers):
                width = sum(l.width * scale for l in row_layers)
                if not include_sidebearings:
                    width += (len(row_layers) - 1) * glyph_spacing_pixels
                return width

            # Helper to get actual glyph bounds (including overhang beyond sidebearings)
            def get_glyph_overhang(layer):
                """Returns (left_overhang, right_overhang) in font units.
                Left overhang = how much glyph extends left of x=0 (positive = overhang)
                Right overhang = how much glyph extends past advance width (positive = overhang)
                """
                if not layer.bounds:
                    return (0, 0)
                bounds = layer.bounds
                left_overhang = max(0, -bounds.origin.x)
                right_edge = bounds.origin.x + bounds.size.width
                right_overhang = max(0, right_edge - layer.width)
                return (left_overhang, right_overhang)

            # Helper to get glyph x positions for a row (evenly distributed when sidebearings enabled)
            def get_glyph_positions(row_layers):
                """Returns list of x positions for each glyph in the row.
                Positions account for actual glyph bounds, not just advance widths.
                """
                positions = []
                if include_sidebearings and len(row_layers) > 0:
                    # Calculate total width including overhangs
                    total_glyph_width = 0
                    for layer in row_layers:
                        left_oh, right_oh = get_glyph_overhang(layer)
                        # Use max of advance width or actual bounds
                        actual_width = layer.width + left_oh + right_oh
                        total_glyph_width += actual_width * scale

                    remaining_space = available_width - total_glyph_width
                    # Distribute remaining space as gaps (n+1 gaps for n glyphs)
                    gap = max(10, remaining_space / (len(row_layers) + 1))  # Minimum 10px gap

                    # First glyph: account for its left overhang
                    first_left_oh, _ = get_glyph_overhang(row_layers[0])
                    current_x = padding + gap + (first_left_oh * scale)

                    for i, layer in enumerate(row_layers):
                        positions.append(current_x)
                        left_oh, right_oh = get_glyph_overhang(layer)
                        # Move by advance width plus right overhang plus next glyph's left overhang
                        current_x += layer.width * scale + (right_oh * scale) + gap
                        if i + 1 < len(row_layers):
                            next_left_oh, _ = get_glyph_overhang(row_layers[i + 1])
                            current_x += next_left_oh * scale
                else:
                    # Centered layout with fixed spacing
                    row_width = get_row_width(row_layers)
                    current_x = padding + (available_width - row_width) / 2
                    for layer in row_layers:
                        positions.append(current_x)
                        current_x += layer.width * scale + glyph_spacing_pixels
                return positions

            # --- Ensure 30px padding around actual ink (vector padding) ---
            vec_pad = 30
            _ink_min_x = float('inf')
            _ink_max_x = float('-inf')
            _ink_min_y = float('inf')
            _ink_max_y = float('-inf')

            for _ri, _rl in enumerate(rows):
                _by = start_y - _ri * (row_height_scaled + row_spacing_scaled)
                _pos = get_glyph_positions(_rl)
                for _gi, _gl in enumerate(_rl):
                    if _gl.bounds:
                        _b = _gl.bounds
                        _lx = _pos[_gi] + _b.origin.x * scale
                        _rx = _pos[_gi] + (_b.origin.x + _b.size.width) * scale
                        _bby = _by + _b.origin.y * scale
                        _ty = _by + (_b.origin.y + _b.size.height) * scale
                        _ink_min_x = min(_ink_min_x, _lx)
                        _ink_max_x = max(_ink_max_x, _rx)
                        _ink_min_y = min(_ink_min_y, _bby)
                        _ink_max_y = max(_ink_max_y, _ty)

            if _ink_min_x < float('inf'):
                _ink_w = _ink_max_x - _ink_min_x
                _ink_h = _ink_max_y - _ink_min_y
                _min_margin = min(_ink_min_x, _ink_min_y, size - _ink_max_x, size - _ink_max_y)

                if _min_margin < vec_pad and _ink_w > 0 and _ink_h > 0:
                    _cx = (_ink_min_x + _ink_max_x) / 2.0
                    _cy = (_ink_min_y + _ink_max_y) / 2.0
                    _f = min((size - 2.0 * vec_pad) / _ink_w, (size - 2.0 * vec_pad) / _ink_h)

                    NSGraphicsContext.currentContext().saveGraphicsState()
                    _t = NSAffineTransform.transform()
                    _t.translateXBy_yBy_(size / 2.0, size / 2.0)
                    _t.scaleBy_(_f)
                    _t.translateXBy_yBy_(-_cx, -_cy)
                    _t.concat()
                    _vec_transform_applied = True


            # Store vector correction values for guide drawing (need _f, _cx, _cy even if no correction)
            _vec_f = 1.0
            _vec_cx = size / 2.0
            _vec_cy = size / 2.0
            if _vec_transform_applied:
                _vec_f = _f
                _vec_cx = _cx
                _vec_cy = _cy

            # Helper to draw all guides (only draws guides for metrics that are actually set)
            # Guides always span the full image width, even when vector padding transform is active
            def draw_guides():
                # Temporarily undo the vector transform so guides span full image width
                NSGraphicsContext.currentContext().saveGraphicsState()
                if _vec_transform_applied:
                    # Apply inverse: translate to ink center, scale by 1/f, translate to image center
                    inv = NSAffineTransform.transform()
                    inv.translateXBy_yBy_(_vec_cx, _vec_cy)
                    inv.scaleBy_(1.0 / _vec_f)
                    inv.translateXBy_yBy_(-size / 2.0, -size / 2.0)
                    inv.concat()

                for row_idx, row_layers in enumerate(rows):
                    baseline_y = start_y - row_idx * (row_height_scaled + row_spacing_scaled)
                    # Compute guide Y in image coords (applying the vector correction manually)
                    if _vec_transform_applied:
                        by_img = size / 2.0 + _vec_f * (baseline_y - _vec_cy)
                    else:
                        by_img = baseline_y

                    NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.5, 0.0, 1.0).set()
                    NSBezierPath.fillRect_(NSMakeRect(0, by_img - guide_thickness/2, size, guide_thickness))

                    NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 0.0, 1.0).set()
                    if cap_height:
                        y_img = size / 2.0 + _vec_f * (baseline_y + cap_height * scale - _vec_cy) if _vec_transform_applied else baseline_y + cap_height * scale
                        NSBezierPath.fillRect_(NSMakeRect(0, y_img - guide_thickness/2, size, guide_thickness))
                    if x_height:
                        y_img = size / 2.0 + _vec_f * (baseline_y + x_height * scale - _vec_cy) if _vec_transform_applied else baseline_y + x_height * scale
                        NSBezierPath.fillRect_(NSMakeRect(0, y_img - guide_thickness/2, size, guide_thickness))
                    if descender:
                        y_img = size / 2.0 + _vec_f * (baseline_y + descender * scale - _vec_cy) if _vec_transform_applied else baseline_y + descender * scale
                        NSBezierPath.fillRect_(NSMakeRect(0, y_img - guide_thickness/2, size, guide_thickness))

                NSGraphicsContext.currentContext().restoreGraphicsState()

            # Gray color for sidebearing backgrounds
            gray_color = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.85, 0.85, 0.85, 1.0)

            # Collect all gray rectangles for clipping (use ink bounds for rect height)
            all_gray_rects = []
            if include_sidebearings:
                for row_idx, row_layers in enumerate(rows):
                    baseline_y = start_y - row_idx * (row_height_scaled + row_spacing_scaled)
                    rect_top = baseline_y + (layout_ink_top * scale)
                    rect_bot = baseline_y + (layout_ink_bottom * scale)
                    rect_height = rect_top - rect_bot

                    positions = get_glyph_positions(row_layers)
                    for idx, layer in enumerate(row_layers):
                        glyph_width = layer.width * scale
                        all_gray_rects.append((positions[idx], rect_bot, glyph_width, rect_height))

            # Pass 1: Draw gray sidebearing rectangles
            if include_sidebearings:
                gray_color.set()
                for rx, ry, rw, rh in all_gray_rects:
                    NSBezierPath.fillRect_(NSMakeRect(rx, ry, rw, rh))

            # Pass 2: Draw guidelines (on top of white and gray)
            if include_vertical_metrics:
                draw_guides()

            # Pass 3: Draw WHITE letter outlines EVERYWHERE (no clipping)
            # This covers guidelines in both white AND gray areas (including neighboring glyphs' gray)
            if include_vertical_metrics:
                for row_idx, row_layers in enumerate(rows):
                    baseline_y = start_y - row_idx * (row_height_scaled + row_spacing_scaled)
                    positions = get_glyph_positions(row_layers)

                    for idx, layer in enumerate(row_layers):
                        if layer.bezierPath:
                            NSGraphicsContext.currentContext().saveGraphicsState()

                            transform = NSAffineTransform.transform()
                            transform.translateXBy_yBy_(positions[idx], baseline_y)
                            transform.scaleBy_(scale)
                            transform.concat()

                            NSColor.whiteColor().set()
                            layer.bezierPath.fill()
                            path = layer.bezierPath.copy()
                            path.setLineWidth_(outline_thickness)
                            path.stroke()

                            NSGraphicsContext.currentContext().restoreGraphicsState()

            # Pass 4: Draw GRAY letter outlines clipped to each glyph's OWN gray rectangle
            # This restores gray only in the glyph's own sidebearing area (not neighboring glyphs)
            if include_sidebearings:
                rect_idx = 0
                for row_idx, row_layers in enumerate(rows):
                    baseline_y = start_y - row_idx * (row_height_scaled + row_spacing_scaled)
                    positions = get_glyph_positions(row_layers)

                    for idx, layer in enumerate(row_layers):
                        if layer.bezierPath:
                            rx, ry, rw, rh = all_gray_rects[rect_idx]

                            NSGraphicsContext.currentContext().saveGraphicsState()
                            NSBezierPath.clipRect_(NSMakeRect(rx, ry, rw, rh))

                            transform = NSAffineTransform.transform()
                            transform.translateXBy_yBy_(positions[idx], baseline_y)
                            transform.scaleBy_(scale)
                            transform.concat()

                            gray_color.set()
                            layer.bezierPath.fill()
                            path = layer.bezierPath.copy()
                            path.setLineWidth_(outline_thickness)
                            path.stroke()

                            NSGraphicsContext.currentContext().restoreGraphicsState()

                        rect_idx += 1

            # Pass 5: Draw black letter fills on top
            for row_idx, row_layers in enumerate(rows):
                baseline_y = start_y - row_idx * (row_height_scaled + row_spacing_scaled)
                positions = get_glyph_positions(row_layers)

                for idx, layer in enumerate(row_layers):
                    if layer.bezierPath:
                        NSGraphicsContext.currentContext().saveGraphicsState()
                        transform = NSAffineTransform.transform()
                        transform.translateXBy_yBy_(positions[idx], baseline_y)
                        transform.scaleBy_(scale)
                        transform.concat()

                        NSColor.blackColor().set()
                        layer.bezierPath.fill()

                        NSGraphicsContext.currentContext().restoreGraphicsState()

        finally:
            if _vec_transform_applied:
                NSGraphicsContext.currentContext().restoreGraphicsState()
            image.unlockFocus()

        # Log metrics

        # Convert to PNG
        tiff_data = image.TIFFRepresentation()
        if not tiff_data:
            return None

        bitmap = NSBitmapImageRep.imageRepWithData_(tiff_data)
        if not bitmap:
            return None

        png_data = bitmap.representationUsingType_properties_(NSPNGFileType, None)
        if not png_data:
            return None

        return base64.b64encode(bytes(png_data)).decode('utf-8')

    @objc.python_method
    def _replace_glyphs(self, font, glyph_data, layer_choice='foreground', existing_choice='replace', reference_glyph_names=None, target_master_id=None):
        """Replace glyphs in font with generated data.

        Args:
            font: GSFont object
            glyph_data: Dict mapping glyph names to glyph data
            layer_choice: 'foreground' or 'background' - where to place paths
            existing_choice: 'replace' (keep referenced), 'skip' (skip all existing), 'replace_all' (replace everything)
            reference_glyph_names: Set of glyph names used as style reference
            target_master_id: If set, only write to this specific master's layers

        Returns:
            Number of glyphs successfully replaced
        """
        use_background = (layer_choice == 'background')
        replaced_count = 0
        skipped_count = 0
        reference_glyph_names = reference_glyph_names or set()

        # Determine which masters to process
        if target_master_id:
            masters_to_process = [m for m in font.masters if m.id == target_master_id]
        else:
            masters_to_process = list(font.masters)

        # Helper to check if glyph has existing paths
        def glyph_has_paths(glyph_name):
            glyph = font.glyphs[glyph_name]
            if not glyph:
                return False
            mid = masters_to_process[0].id if masters_to_process else None
            if not mid:
                return False
            layer = glyph.layers[mid]
            return layer and len(layer.paths) > 0

        # First pass: create/update all base glyphs (non-composites)
        # This ensures base glyphs exist before we try to reference them in composites
        for glyph_name, data in glyph_data.items():
            if data.get('is_composite'):
                continue  # Handle composites in second pass

            try:
                # Check if we should skip this glyph based on existing_choice
                is_reference = glyph_name in reference_glyph_names
                has_paths = glyph_has_paths(glyph_name)

                skip_foreground = False
                if existing_choice == 'replace' and is_reference:
                    skip_foreground = True
                elif existing_choice == 'skip' and has_paths:
                    skip_foreground = True

                paths = data.get('paths', [])
                width = data.get('width', 500)
                unicode_val = data.get('unicode')
                anchors = data.get('anchors', [])

                # Get or create glyph
                glyph = font.glyphs[glyph_name]
                created_new = False

                if not glyph:
                    glyph = GSGlyph(glyph_name)
                    font.glyphs.append(glyph)
                    created_new = True
                    if unicode_val:
                        glyph.unicode = format(unicode_val, '04X')
                    glyph = font.glyphs[glyph_name]

                if not glyph:
                    continue

                # Process target master layers
                for master in masters_to_process:
                    layer = glyph.layers[master.id]
                    if not layer:
                        continue

                    # Determine target layer
                    if skip_foreground:
                        # Skipped glyph: only add to background layer
                        target_layer = layer.background
                    elif use_background:
                        target_layer = layer.background
                    else:
                        target_layer = layer

                    # Clear target layer
                    try:
                        target_layer.shapes = []
                    except:
                        try:
                            while len(target_layer.paths) > 0:
                                target_layer.removePathAtIndex_(0)
                        except:
                            pass
                        try:
                            while len(target_layer.components) > 0:
                                target_layer.removeComponentAtIndex_(0)
                        except:
                            pass

                    # Clear existing anchors (only when writing to foreground)
                    if not use_background and not skip_foreground:
                        try:
                            layer.anchors = []
                        except:
                            try:
                                while len(layer.anchors) > 0:
                                    layer.removeAnchorAtIndex_(0)
                            except:
                                pass

                    # Set width (only when writing to foreground)
                    if not skip_foreground:
                        layer.width = width

                    # Add paths to target layer
                    for path_nodes in paths:
                        gs_path = self._create_gspath(path_nodes)
                        if gs_path and len(gs_path.nodes) >= 2:
                            target_layer.paths.append(gs_path)

                    # Add anchors (only when writing to foreground)
                    if not use_background and not skip_foreground:
                        for anchor_data in anchors:
                            try:
                                anchor_name, anchor_x, anchor_y = anchor_data
                                anchor = GSAnchor(anchor_name, (anchor_x, anchor_y))
                                layer.anchors.append(anchor)
                            except Exception as ae:
                                pass

                replaced_count += 1
                if skip_foreground:
                    skipped_count += 1

            except Exception as e:
                continue

        # Second pass: create composite glyphs
        composites_created = 0
        for glyph_name, data in glyph_data.items():
            if not data.get('is_composite'):
                continue

            try:
                # Check if we should skip this glyph based on existing_choice
                is_reference = glyph_name in reference_glyph_names
                has_paths = glyph_has_paths(glyph_name)

                skip_foreground = False
                if existing_choice == 'replace' and is_reference:
                    skip_foreground = True
                elif existing_choice == 'skip' and has_paths:
                    skip_foreground = True

                width = data.get('width', 500)
                unicode_val = data.get('unicode')
                components = data.get('components', [])

                # Get or create glyph
                glyph = font.glyphs[glyph_name]
                created_new = False

                if not glyph:
                    glyph = GSGlyph(glyph_name)
                    font.glyphs.append(glyph)
                    created_new = True
                    if unicode_val:
                        glyph.unicode = format(unicode_val, '04X')
                    glyph = font.glyphs[glyph_name]

                if not glyph:
                    continue

                # Process target master layers
                for master in masters_to_process:
                    layer = glyph.layers[master.id]
                    if not layer:
                        continue

                    # Determine target layer
                    if skip_foreground:
                        target_layer = layer.background
                    elif use_background:
                        target_layer = layer.background
                    else:
                        target_layer = layer

                    # Clear target layer shapes
                    try:
                        target_layer.shapes = []
                    except:
                        try:
                            while len(target_layer.paths) > 0:
                                target_layer.removePathAtIndex_(0)
                        except:
                            pass
                        try:
                            while len(target_layer.components) > 0:
                                target_layer.removeComponentAtIndex_(0)
                        except:
                            pass

                    # Set width (only when writing to foreground)
                    if not skip_foreground and not use_background:
                        layer.width = width

                    # Add components to target layer
                    for comp_data in components:
                        try:
                            comp_name = comp_data.get('name')
                            if not comp_name:
                                continue

                            # Check if component glyph exists
                            comp_glyph = font.glyphs[comp_name]
                            if not comp_glyph:
                                continue

                            # Create GSComponent
                            component = GSComponent(comp_name)

                            # Get offset values for positioning
                            offset_x = comp_data.get('offset_x', 0)
                            offset_y = comp_data.get('offset_y', 0)

                            # Set position using offset
                            if offset_x != 0 or offset_y != 0:
                                component.position = (offset_x, offset_y)

                            target_layer.components.append(component)

                        except Exception as ce:
                            pass

                composites_created += 1
                replaced_count += 1

            except Exception as e:
                continue

        return replaced_count

    @objc.python_method
    def _create_gspath(self, path_nodes):
        """Create a GSPath from a list of node strings.

        Args:
            path_nodes: List of node strings like '"100 200 LINE"'

        Returns:
            GSPath object or None
        """
        import re

        if not path_nodes:
            return None

        path = GSPath()

        for node_str in path_nodes:
            try:
                # Parse node string: "x y TYPE [SMOOTH]"
                clean = node_str.strip('"')
                # More flexible regex - handle "CURVE SMOOTH" as one token
                match = re.match(r'(-?\d+)\s+(-?\d+)\s+(.+)', clean)

                if not match:
                    continue

                x = int(match.group(1))
                y = int(match.group(2))
                type_part = match.group(3).upper().strip()

                # Check for SMOOTH suffix
                is_smooth = "SMOOTH" in type_part

                # Determine node type
                if "OFFCURVE" in type_part:
                    node_type = GSOFFCURVE
                elif "CURVE" in type_part:
                    node_type = GSCURVE
                else:
                    node_type = GSLINE

                # Create node using constructor with position
                node = GSNode((x, y), node_type)

                if is_smooth and node_type != GSOFFCURVE:
                    node.smooth = True

                path.nodes.append(node)

            except Exception as e:
                # Skip problematic nodes but continue
                continue

        if len(path.nodes) < 2:
            return None

        path.closed = True
        return path
