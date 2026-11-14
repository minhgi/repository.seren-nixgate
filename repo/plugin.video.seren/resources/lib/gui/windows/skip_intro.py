"""Skip overlay dialog — renders one of four button types during playback.

Used for intro / recap / credits / preview segments. The button label is set
dynamically from the segment_type passed by the caller, so a single window XML
serves all four cases. Auto-closes when playback passes skip_end or the file
changes.
"""

import xbmc

from resources.lib.gui.windows.base_window import BaseWindow
from resources.lib.modules.globals import g

# String IDs for button labels — must match strings.po entries
_LABEL_BY_TYPE = {
    "intro": 30765,    # "Skip Intro"
    "recap": 30894,    # "Skip Recap"
    "credits": 30785,  # "Skip Outro" — re-used for credits (same English wording)
    "preview": 30895,  # "Skip Preview"
}

_BUTTON_SKIP = 3001
_BUTTON_CLOSE = 3002


class SkipIntro(BaseWindow):
    """Overlay dialog shown during a skip segment."""

    def __init__(self, xml_file, xml_location, item_information=None, skip_end=0, segment_type="intro", countdown=0):
        super().__init__(xml_file, xml_location, item_information=item_information)
        self.player = xbmc.Player()
        self.skip_end = skip_end
        self.segment_type = segment_type if segment_type in _LABEL_BY_TYPE else "intro"
        self.closed = False
        # Auto-skip countdown: seconds left before the window seeks by itself (0 = plain button, no auto-skip)
        self.remaining = max(0, int(countdown or 0))
        self.label = ""
        self._last_time = None
        try:
            self.playing_file = self.player.getPlayingFile()
        except RuntimeError:
            self.playing_file = None

    def __del__(self):
        self.player = None

    def onInit(self):
        super().onInit()
        # Apply the type-specific button label before showing.
        try:
            label_id = _LABEL_BY_TYPE.get(self.segment_type, 30765)
            label = g.get_language_string(label_id)
            ctrl = self.getControl(_BUTTON_SKIP)
            if ctrl and label:
                ctrl.setLabel(label)
            self.label = label or ""
            if self.remaining:
                self._show_countdown()
                # Close doubles as Cancel while the auto-skip counts down
                self.getControl(_BUTTON_CLOSE).setLabel(xbmc.getLocalizedString(222))
        except Exception:
            # Non-fatal — fall through with whatever the XML default label is.
            g.log_stacktrace()

        self._background_monitor()

    def _show_countdown(self):
        try:
            self.getControl(_BUTTON_SKIP).setLabel(f"{self.label} ({self.remaining})")
        except RuntimeError:
            pass

    def _cancel_countdown(self):
        """Stop the auto-skip but leave the plain Skip / Close buttons on screen."""
        self.remaining = 0
        try:
            self.getControl(_BUTTON_SKIP).setLabel(self.label)
            self.getControl(_BUTTON_CLOSE).setLabel(g.get_language_string(30459))
        except RuntimeError:
            pass
        g.log("Skip: countdown cancelled (playback went back)", "debug")

    def _auto_skip(self):
        """The countdown reached zero: seek to the end of the segment (already includes skip.offset)."""
        try:
            if int(self.player.getTime()) < self.skip_end:
                self.player.seekTime(self.skip_end)
                g.log(f"Skip: countdown finished, skipped {self.segment_type} to {self.skip_end}s", "debug")
        except RuntimeError:
            pass

    def _background_monitor(self):
        """Monitor playback position; auto-close when segment ends or file changes.

        With a countdown it also counts down once a second (held while paused) and skips when it reaches zero."""
        try:
            while (
                not self.closed
                and not g.abort_requested()
                and self.player.isPlaying()
                and self.playing_file == self.player.getPlayingFile()
            ):
                current = int(self.player.getTime())
                if current > self.skip_end:
                    break
                if self.remaining and self._last_time is not None and current < self._last_time - 2:
                    self._cancel_countdown()  # the viewer went back: do not pull them forward again
                self._last_time = current
                xbmc.sleep(1000)
                if self.remaining and not self.closed and not xbmc.getCondVisibility("Player.Paused"):
                    self.remaining -= 1
                    if self.remaining <= 0:
                        self._auto_skip()
                        break
                    self._show_countdown()
        except RuntimeError:
            pass
        self.close()

    def handle_action(self, action_id, control_id=None):
        if action_id == 7:
            if control_id == _BUTTON_SKIP:
                # Skip — seek to end of segment (already includes skip.offset)
                try:
                    self.player.seekTime(self.skip_end)
                except RuntimeError:
                    pass
                self.close()
            elif control_id == _BUTTON_CLOSE:
                # Close — dismiss without skipping
                self.close()

    def close(self):
        self.closed = True
        super().close()
