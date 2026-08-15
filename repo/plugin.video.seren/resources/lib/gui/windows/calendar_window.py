import datetime

import xbmc

from resources.lib.gui.windows.base_window import BaseWindow
from resources.lib.modules.globals import g


class CalendarWindow(BaseWindow):
    def __init__(self, xml_file, location, item_information=None):
        super().__init__(xml_file, location, item_information=item_information)
        self.list_control = None
        self.episodes = []

    def onInit(self):
        self.list_control = self.getControlList(1000)
        self._populate_menu_items()
        self.set_default_focus(self.list_control, 2999, control_list_reset=True)
        super().onInit()

        if not self.episodes:
            g.notification(g.ADDON_NAME, g.get_language_string(31474))
            self.close()

    def _populate_menu_items(self):
        from resources.lib.modules.calendar_data import get_calendar_episodes

        self.episodes = get_calendar_episodes()
        for episode in self.episodes:
            self.list_control.addItem(self._build_list_item(episode))

    @staticmethod
    def _build_list_item(episode):
        from resources.lib.modules.calendar_data import get_air_date

        info = episode.get("info") or {}
        show_title = info.get("tvshowtitle") or info.get("show_title") or ""
        episode_title = info.get("title") or ""
        label = f"{show_title} - {episode_title}" if show_title else episode_title

        list_item = BaseWindow.get_list_item_with_properties(episode, label=label)

        air_date = get_air_date(episode)
        list_item.setProperty("air_date_label", CalendarWindow._format_air_date(air_date))
        list_item.setProperty(
            "is_upcoming", str(bool(air_date and air_date >= g.datetime_to_string(datetime.datetime.utcnow())))
        )

        rating = info.get("rating")
        list_item.setProperty("rating_tmdb", str(rating) if rating else "")

        return list_item

    @staticmethod
    def _format_air_date(air_date):
        if not air_date:
            return ""
        try:
            from resources.lib.common import tools

            parsed = tools.parse_datetime(air_date)
            return parsed.strftime(xbmc.getRegion("dateshort"))
        except (ValueError, TypeError):
            return air_date

    def handle_action(self, action_id, control_id=None):
        if action_id != 7:
            return
        if control_id == 2999:
            self.close()
        elif control_id == 1000:
            self._play_selected()

    def _play_selected(self):
        position = self.list_control.getSelectedPosition()
        if position < 0:
            return
        args = self.episodes[position].get("args")
        if not args:
            return
        self.close()
        url = g.create_url(g.BASE_URL, {"action": "getSources", "action_args": args})
        xbmc.executebuiltin(f'RunPlugin("{url}")')
