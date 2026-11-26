import threading

import xbmc

from resources.lib.modules.globals import g

""" 3.9.193: a video started from a calendar (Play in the episode / movie information dialog) opens behind the window, which is modal, so the
video was out of sight until the calendar was closed by hand. This watches for a video that starts after the window opened and closes the window,
which shows the video. A video that was already playing when the window opened does not count; a start that never comes (the source pick is
cancelled, no source plays) leaves the calendar open. Shared by the Shows Calendars and the Movie Calendar (the same watcher exists in TMDb Helper
7.5.26). """


# The information dialog a calendar opened (Dialog().info, which blocks the window) is parked under the fullscreen video and comes back when the video
# stops (seen on Bingie: "Video info" left on screen, Estuary ends on Home). Dialog.Close does nothing while it is parked, so this waits for the stop and
# closes it then. It runs in its own thread: the window's close handler joins the watcher thread (stop_watching) and must not wait for a whole movie.
INFO_ACTIVE = "Window.IsActive(movieinformation) | Window.IsActive(12003)"


def close_info_after_stop(log, name):
    def _run():
        monitor = xbmc.Monitor()
        while xbmc.getCondVisibility("Player.HasVideo"):  # the video plays (started from the calendar), wait for its end
            if monitor.waitForAbort(0.5):
                return
        for _ in range(10):  # the dialog comes back a moment after the stop
            if xbmc.getCondVisibility(INFO_ACTIVE):
                xbmc.executebuiltin("Dialog.Close(movieinformation,true)")
                log(f"{name}: the video ended, the information dialog that came back is closed")
                return
            if monitor.waitForAbort(0.5):
                return
    threading.Thread(target=_run, name=f"{name} close info after stop", daemon=True).start()


class CloseOnPlay(threading.Thread):
    def __init__(self, window, name):
        super().__init__(name=f"{name} close on play", daemon=True)
        self._window = window
        self._log_name = name
        self._stop_event = threading.Event()
        self._monitor = xbmc.Monitor()
        self._player = xbmc.Player()
        self._first = self._playing_file()  # whatever already plays when the window opens

    def _playing_file(self):
        """ Path of the file that plays now, '' when no video plays (or it ended while this asked) """
        if not xbmc.getCondVisibility("Player.HasVideo"):
            return ""
        try:
            return self._player.getPlayingFile() or ""
        except RuntimeError:  # Kodi is not playing any file
            return ""

    def stop_watching(self):
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=1)

    def run(self):
        while not self._stop_event.is_set() and not self._monitor.waitForAbort(0.5):
            path = self._playing_file()
            if not path or path == self._first:
                continue
            window, self._window = self._window, None
            if window is not None and not self._stop_event.is_set():
                g.log(f"{self._log_name}: a video started, the window is closed so it shows", "info")
                try:
                    window.close()
                except Exception as exc:
                    g.log(f"{self._log_name}: could not close the window - {exc}", "warning")
                close_info_after_stop(lambda text: g.log(text, "info"), self._log_name)  # 3.9.196
            return
