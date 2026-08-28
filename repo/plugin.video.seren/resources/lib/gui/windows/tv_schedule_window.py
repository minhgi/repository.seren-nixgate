import datetime
import threading

import xbmc

from resources.lib.gui.windows.base_window import BaseWindow
from resources.lib.modules.globals import g

PREV_BUTTON_ID = 3001
NEXT_BUTTON_ID = 3002
CLOSE_BUTTON_ID = 2999
DAY_CONTROL_IDS = [1000, 1001, 1002, 1003, 1004, 1005, 1006]  # Sunday..Saturday, matches week_start_for()'s anchor

# Kodi core localized-string ids for weekday names (xbmc.getLocalizedString, NOT this
# addon's own string pool) - confirmed 11=Monday..17=Sunday against Kodi's own
# resource.language.en_gb/strings.po. Indexed 0=Sunday..6=Saturday to match DAY_CONTROL_IDS.
_WEEKDAY_STRING_IDS = [17, 11, 12, 13, 14, 15, 16]

ACTION_MOVE_LEFT = 1
ACTION_MOVE_RIGHT = 2

_PREFETCH_FORWARD_DELAY = 3.0   # land on a week, fetch the next one after this
_PREFETCH_BACKWARD_DELAY = 8.0  # ...then the previous one 5s after that (3s + 5s)


def _prefetch_week(week_start):
    """threading.Timer callback - module-level and takes only a plain
    datetime.date, never a bound window method or `self`. Mirrors
    movie_calendar_window.py's own _prefetch_month() for the same reason: a
    Timer holding the window instance would keep it alive past
    open_tv_schedule()'s `del window`, and a stray callback firing after
    close() would risk touching torn-down controls."""
    if g.abort_requested():
        return
    try:
        from resources.lib.modules.tv_schedule_data import prefetch_schedule_week

        prefetch_schedule_week(week_start)
        g.log(f"tv_schedule_window: prefetched week of {week_start}", "debug")
    except Exception as e:
        g.log(f"tv_schedule_window: prefetch of week {week_start} failed: {e}", "warning")


class TvScheduleWindow(BaseWindow):
    def __init__(self, xml_file, location, item_information=None):
        super().__init__(xml_file, location, item_information=item_information)
        from resources.lib.modules.tv_schedule_data import week_start_for

        self.day_controls = []
        self.week_start = week_start_for(datetime.date.today())
        self.days = [[] for _ in range(7)]  # index 0=Sunday..6=Saturday
        self._prefetch_timers = []

    def onInit(self):
        self.day_controls = [self.getControlList(cid) for cid in DAY_CONTROL_IDS]
        try:
            self._populate_grid()
            self.set_default_focus(self.day_controls[0], CLOSE_BUTTON_ID, control_list_reset=True)
        finally:
            # closes any busy dialog - must run even if population raises, matching CalendarWindow/MovieCalendarWindow
            super().onInit()

        if not any(self.days):
            if g.get_setting("calendar.region") and g.get_setting("calendar.language"):
                g.notification(g.ADDON_NAME, g.get_language_string(31543))
            else:
                g.notification(g.ADDON_NAME, g.get_language_string(31540))
            self.close()
            return

        self._schedule_prefetch()

    def _schedule_prefetch(self):
        """Rolling background prefetch: land on a week, warm the adjacent
        weeks' TvScheduleDatabase bookkeeping so paging there is a cache hit
        instead of a live Trakt call. Re-triggered on every navigation -
        _cancel_prefetch() drops any still-pending timers for the position
        just left, so rapid paging only ever prefetches around the week the
        user actually lands on. Mirrors MovieCalendarWindow's own
        _schedule_prefetch()."""
        from resources.lib.modules.tv_schedule_data import shift_week

        self._cancel_prefetch()

        fwd_timer = threading.Timer(_PREFETCH_FORWARD_DELAY, _prefetch_week, args=(shift_week(self.week_start, 1),))
        back_timer = threading.Timer(_PREFETCH_BACKWARD_DELAY, _prefetch_week, args=(shift_week(self.week_start, -1),))
        fwd_timer.daemon = True
        back_timer.daemon = True
        self._prefetch_timers = [fwd_timer, back_timer]
        fwd_timer.start()
        back_timer.start()

    def _cancel_prefetch(self):
        for timer in self._prefetch_timers:
            timer.cancel()
        self._prefetch_timers = []

    def close(self):
        self._cancel_prefetch()
        super().close()

    def _populate_grid(self):
        from resources.lib.common import tools
        from resources.lib.modules.tv_schedule_data import get_schedule_week, get_air_date

        self.setProperty("week_label", self._week_label())

        episodes = get_schedule_week(self.week_start)
        self.days = [[] for _ in range(7)]
        for episode in episodes:
            aired = get_air_date(episode)
            try:
                day_index = (tools.parse_datetime(aired) - self.week_start).days
            except (ValueError, TypeError):
                continue
            if 0 <= day_index <= 6:  # get_schedule_week() already drops out-of-week rows; this is belt-and-braces
                self.days[day_index].append(episode)

        for i, control in enumerate(self.day_controls):
            control.reset()
            column_date = self.week_start + datetime.timedelta(days=i)
            self.setProperty(f"day_name_{i}", xbmc.getLocalizedString(_WEEKDAY_STRING_IDS[i]))
            self.setProperty(f"day_date_{i}", column_date.strftime(xbmc.getRegion("dateshort")))
            if self.days[i]:
                for episode in self.days[i]:
                    control.addItem(self._build_list_item(episode))
            else:
                control.addItem(self._build_empty_item())

    def _week_label(self):
        week_end = self.week_start + datetime.timedelta(days=6)
        date_format = xbmc.getRegion("dateshort")
        return f"{self.week_start.strftime(date_format)} - {week_end.strftime(date_format)}"

    @staticmethod
    def _build_list_item(episode):
        info = episode.get("info") or {}
        show_title = info.get("tvshowtitle") or info.get("show_title") or ""
        season, number = info.get("season"), info.get("episode")
        code = f"S{season:02d}E{number:02d}" if isinstance(season, int) and isinstance(number, int) else ""
        episode_title = info.get("title") or ""

        list_item = BaseWindow.get_list_item_with_properties(episode, label=show_title or episode_title)
        list_item.setProperty("show_title", show_title)
        list_item.setProperty("episode_code", code)
        list_item.setProperty("episode_title", episode_title)
        list_item.setProperty("air_time_label", episode.get("_schedule_time_label") or "")
        list_item.setProperty("rating_display", episode.get("_schedule_rating_display") or "")
        list_item.setProperty("air_status", episode.get("_schedule_status") or "upcoming")
        return list_item

    @staticmethod
    def _build_empty_item():
        import xbmcgui

        list_item = xbmcgui.ListItem(label=g.get_language_string(31541))
        list_item.setProperty("is_placeholder", "true")
        return list_item

    def _navigate_week(self, delta):
        from resources.lib.modules.tv_schedule_data import shift_week

        g.show_busy_dialog()
        try:
            self.week_start = shift_week(self.week_start, delta)
            self._populate_grid()
            # Paging left lands on that week's Saturday (rightmost column); paging
            # right lands on Sunday (leftmost) - matches the approved mockup.
            landing_index = 6 if delta < 0 else 0
            self.set_default_focus(self.day_controls[landing_index], CLOSE_BUTTON_ID, control_list_reset=True)
        finally:
            g.close_busy_dialog()
        # After the busy dialog closes, not before - the 3s/8s clock should start
        # when the user is actually looking at the new week.
        self._schedule_prefetch()

    def handle_action(self, action_id, control_id=None):
        # Kodi's native onleft/onright still fires alongside this callback - the XML
        # self-loops DAY_CONTROL_IDS[0]'s onleft and DAY_CONTROL_IDS[6]'s onright so
        # that native move is a no-op, and paging here is what actually happens.
        if action_id == ACTION_MOVE_LEFT and control_id == DAY_CONTROL_IDS[0]:
            self._navigate_week(-1)
            return
        if action_id == ACTION_MOVE_RIGHT and control_id == DAY_CONTROL_IDS[6]:
            self._navigate_week(1)
            return

        if action_id != 7:
            return
        if control_id == CLOSE_BUTTON_ID:
            self.close()
        elif control_id == PREV_BUTTON_ID:
            self._navigate_week(-1)
        elif control_id == NEXT_BUTTON_ID:
            self._navigate_week(1)
        elif control_id in DAY_CONTROL_IDS:
            self._play_selected(DAY_CONTROL_IDS.index(control_id))

    def _play_selected(self, day_index):
        control = self.day_controls[day_index]
        position = control.getSelectedPosition()
        if position < 0 or position >= len(self.days[day_index]):
            return  # placeholder item ("no episodes") or a stale position after repopulation
        args = self.days[day_index][position].get("args")
        if not args:
            return
        self.close()
        url = g.create_url(g.BASE_URL, {"action": "getSources", "action_args": args})
        xbmc.executebuiltin(f'RunPlugin("{url}")')


def open_tv_schedule():
    from resources.lib.database.skinManager import SkinManager

    g.show_busy_dialog()
    window = TvScheduleWindow(*SkinManager().confirm_skin_path("tv_schedule.xml"))
    try:
        window.doModal()
    finally:
        g.close_busy_dialog()
        del window
