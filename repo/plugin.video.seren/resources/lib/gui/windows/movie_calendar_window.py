import calendar
import datetime

import xbmc

from resources.lib.gui.windows.base_window import BaseWindow
from resources.lib.modules.globals import g

PREV_BUTTON_ID = 3001
NEXT_BUTTON_ID = 3002
GRID_ID = 1000
CLOSE_BUTTON_ID = 2999


class MovieCalendarWindow(BaseWindow):
    def __init__(self, xml_file, location, item_information=None):
        super().__init__(xml_file, location, item_information=item_information)
        self.grid_control = None
        self.movies = []
        today = datetime.date.today()
        self.year, self.month = today.year, today.month

    def onInit(self):
        self.grid_control = self.getControlList(GRID_ID)
        self._populate_grid()
        self.set_default_focus(self.grid_control, CLOSE_BUTTON_ID, control_list_reset=True)
        super().onInit()  # closes any busy dialog - must run after population, matching CalendarWindow

        if not self.movies:
            g.notification(g.ADDON_NAME, g.get_language_string(31518))
            self.close()

    def _populate_grid(self):
        from resources.lib.modules.movie_calendar_data import get_movie_calendar_month

        self.grid_control.reset()
        self.setProperty("month_year_label", f"{calendar.month_name[self.month]} {self.year}")
        self.movies = get_movie_calendar_month(self.year, self.month)
        for movie in self.movies:
            self.grid_control.addItem(self._build_tile_item(movie))

    @staticmethod
    def _build_tile_item(movie):
        info = movie.get("info") or {}
        list_item = BaseWindow.get_list_item_with_properties(movie, label=info.get("title") or "")
        list_item.setProperty("movie_title", info.get("title") or "")
        list_item.setProperty("release_date_label", movie.get("_calendar_date_label") or "")
        list_item.setProperty("release_countdown", movie.get("_calendar_countdown") or "")
        list_item.setProperty("rating_display", movie.get("_calendar_rating_display") or "")
        list_item.setProperty("release_status", movie.get("_calendar_status") or "upcoming")
        return list_item

    def _navigate_month(self, delta):
        from resources.lib.modules.movie_calendar_data import shift_month

        g.show_busy_dialog()
        try:
            self.year, self.month = shift_month(self.year, self.month, delta)
            self._populate_grid()
            self.set_default_focus(self.grid_control, CLOSE_BUTTON_ID, control_list_reset=True)
        finally:
            g.close_busy_dialog()

    def handle_action(self, action_id, control_id=None):
        if action_id != 7:
            return
        if control_id == CLOSE_BUTTON_ID:
            self.close()
        elif control_id == PREV_BUTTON_ID:
            self._navigate_month(-1)
        elif control_id == NEXT_BUTTON_ID:
            self._navigate_month(1)
        elif control_id == GRID_ID:
            self._play_selected()

    def _play_selected(self):
        position = self.grid_control.getSelectedPosition()
        if position < 0:
            return
        args = self.movies[position].get("args")
        if not args:
            return
        self.close()
        url = g.create_url(g.BASE_URL, {"action": "getSources", "action_args": args})
        xbmc.executebuiltin(f'RunPlugin("{url}")')


def open_movie_calendar():
    from resources.lib.database.skinManager import SkinManager

    window = MovieCalendarWindow(*SkinManager().confirm_skin_path("movie_calendar.xml"))
    try:
        window.doModal()
    finally:
        del window
