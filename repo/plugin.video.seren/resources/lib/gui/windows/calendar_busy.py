import threading

import xbmc

SHOW_AFTER = 0.5  # seconds a load may take before the busy dialog appears


def close_busy_now():
    """Close a busy dialog at once. A plain Dialog.Close leaves a dialog that was on screen for about a second after the close."""
    xbmc.executebuiltin("Dialog.Close(busydialognocancel,true)")
    xbmc.executebuiltin("Dialog.Close(busydialog,true)")


class DelayedBusyDialog:
    """The busy dialog of a calendar window's load: it appears only when the load takes longer than SHOW_AFTER seconds, so a load that is
    answered at once shows nothing and a load with a delay always shows the dialog (no guessing beforehand which one it will be).

        busy = DelayedBusyDialog().start()
        try:
            ...load...
        finally:
            busy.stop()

    It is also a context manager. The dialog is opened from a timer thread and closed with the forced close: a plain Dialog.Close leaves a
    dialog that was on screen for about a second after the load is done."""

    def __init__(self, delay=SHOW_AFTER):
        self._delay = delay
        self._lock = threading.Lock()
        self._timer = None
        self._shown = False
        self._stopped = False

    def start(self):
        timer = threading.Timer(self._delay, self._show)
        timer.daemon = True
        with self._lock:
            self._timer = timer
        timer.start()
        return self

    def _show(self):
        with self._lock:
            if self._stopped:
                return
            self._shown = True
            xbmc.executebuiltin("ActivateWindow(busydialognocancel)")

    def stop(self):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            shown = self._shown
            timer = self._timer
        if timer is not None:
            timer.cancel()
        if shown:
            close_busy_now()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.stop()
