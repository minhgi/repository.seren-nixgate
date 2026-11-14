"""Stay seated card - shown as the credits of a film begin when TMDB says a scene follows during or after them.

Two buttons: Keep watching (default) and Stop. Closes by itself after SHOW_FOR seconds, when playback stops or the file
changes. Back/Close keeps watching; the Stop key on the remote stops playback.
"""

import xbmc

from resources.lib.gui.windows.base_window import ACTION_NAV_BACK
from resources.lib.gui.windows.base_window import ACTION_PLAYER_STOP
from resources.lib.gui.windows.base_window import ACTION_PREVIOUS_MENU
from resources.lib.gui.windows.base_window import BaseWindow
from resources.lib.modules.globals import g

_BUTTON_KEEP = 3001
_BUTTON_STOP = 3002
_LABEL_KICKER = 3003
_LABEL_HEAD = 3004
_TEXT_SUB = 3005
SHOW_FOR = 15  # seconds the card stays


class StaySeated(BaseWindow):
    """Card shown at the start of the credits of a film that has a scene during and/or after them."""

    def __init__(self, xml_file, xml_location, during=False, after=False):
        super().__init__(xml_file, xml_location)
        self.player = xbmc.Player()
        self.during = during
        self.after = after
        self.closed = False
        try:
            self.playing_file = self.player.getPlayingFile()
        except RuntimeError:
            self.playing_file = None

    def __del__(self):
        self.player = None

    def onInit(self):
        super().onInit()
        try:
            if self.during and self.after:
                head, sub = 31638, 31641
            elif self.during:
                head, sub = 31637, 31639
            else:
                head, sub = 31637, 31640
            self.getControl(_LABEL_KICKER).setLabel(g.get_language_string(31636))
            self.getControl(_LABEL_HEAD).setLabel(g.get_language_string(head))
            self.getControl(_TEXT_SUB).setText(g.get_language_string(sub))
            self.getControl(_BUTTON_KEEP).setLabel(g.get_language_string(31642))
            self.getControl(_BUTTON_STOP).setLabel(g.get_language_string(31643))
        except Exception:
            # Non-fatal - the XML has no default text, but the buttons still work
            g.log_stacktrace()

        self._background_monitor()

    def _background_monitor(self):
        """Close after SHOW_FOR seconds, or earlier when playback stops or the file changes."""
        try:
            waited = 0
            while (
                not self.closed
                and waited < SHOW_FOR
                and not g.abort_requested()
                and self.player.isPlaying()
                and self.playing_file == self.player.getPlayingFile()
            ):
                xbmc.sleep(1000)
                waited += 1
        except RuntimeError:
            pass
        self.close()

    def onAction(self, action):
        # Not BaseWindow.onAction: backing out of this card is not "the user cancelled a play attempt"
        action_id = action.getId()
        if action_id == ACTION_PLAYER_STOP:
            self._stop_playback()
        elif action_id in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self.close()
        elif action_id != 7:  # Enter(7) also fires an onClick event
            self.handle_action(action_id, self.getFocusId())

    def handle_action(self, action_id, control_id=None):
        if action_id != 7:
            return
        if control_id == _BUTTON_KEEP:
            self.close()
        elif control_id == _BUTTON_STOP:
            self._stop_playback()

    def _stop_playback(self):
        try:
            self.player.stop()
        except RuntimeError:
            pass
        self.close()

    def close(self):
        self.closed = True
        super().close()
