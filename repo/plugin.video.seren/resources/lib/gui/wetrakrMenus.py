import datetime
from concurrent.futures import as_completed
from functools import cached_property

from resources.lib.common.thread_pool import ThreadPoolExecutor
from resources.lib.modules.globals import g

# Same bound and reason as simklMenus._NEXT_UP_SHOW_CAP: every show reaching the episode lookup can trigger a one-time, synchronous cold
# bootstrap of that show's season list.
_NEXT_UP_SHOW_CAP = 10
# Next Up reads the top of the watching list only (it is sorted newest watched first): 3 pages of 100 shows are far more than the cap needs.
_NEXT_UP_PAGE_LIMIT = 100
_NEXT_UP_MAX_PAGES = 3


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stamp(row):
    return row.get("tracked_at") or row.get("last_watched_at") or row.get("watched_at") or ""


def status_ids(rows):
    """TMDb ids of a compact status list (ids.tmdb + tracked_at), newest first, each once. A row with no TMDb id cannot be opened and is left out."""
    stamped = []
    for row in rows or []:
        tmdb_id = _int((row.get("ids") or {}).get("tmdb"))
        if tmdb_id:
            stamped.append((_stamp(row), tmdb_id))
    ids, seen = [], set()
    for _, tmdb_id in sorted(stamped, key=lambda item: item[0], reverse=True):
        if tmdb_id not in seen:
            seen.add(tmdb_id)
            ids.append(tmdb_id)
    return ids


def next_up_targets(shows, today, cap=_NEXT_UP_SHOW_CAP):
    """Rows of GET /sync/tracking/watching/shows?extended=episode_level_1 (already newest watched first) -> [{tmdb_id, season, number}], at most
    `cap`. A show with no next_watching_episode is caught up; an episode whose air date is after today ('YYYY-MM-DD') is nothing to watch yet."""
    targets = []
    for row in shows or []:
        tmdb_id = _int((row.get("ids") or {}).get("tmdb"))
        following = row.get("next_watching_episode") or {}
        season, number = _int(following.get("season_number")), _int(following.get("number"))
        if not tmdb_id or season is None or number is None:
            continue
        aired = f'{following.get("air_date") or ""}'[:10]
        if aired and aired > today:
            continue
        targets.append({"tmdb_id": tmdb_id, "season": season, "number": number})
        if len(targets) >= cap:
            break
    return targets


class Menus:
    """WeTrakr-sourced menus, the sibling of simklMenus.Menus (same resolve-through-Trakt reuse of the Trakt-native ListBuilder methods).

    recent_movies/recent_shows/in_progress_* read the local wetrakrSync.db (Seren 3.9.168), keyed by TMDb id. The status lists (Plan to Watch,
    Dropped, Watching, On Hold) and Next Up are read from WeTrakr live, per menu open, like Simkl's status lists: one request for a compact
    list, read in full or not at all. WeTrakr resolves every title by TMDb id and numbers episodes the TMDb way, so no numbering check is needed.
    """

    def __init__(self):
        self.page_limit = g.get_int_setting("item.limit")
        self.page_start = (g.PAGE - 1) * self.page_limit
        self.page_end = g.PAGE * self.page_limit

    @cached_property
    def wetrakr_database(self):
        from resources.lib.database.wetrakr_sync import WeTrakrSyncDatabase

        return WeTrakrSyncDatabase()

    @cached_property
    def wetrakr_api(self):
        from resources.lib.indexers.wetrakr import WeTrakrAPI

        return WeTrakrAPI()

    @cached_property
    def trakt_api(self):
        from resources.lib.indexers.trakt import TraktAPI

        return TraktAPI()

    @cached_property
    def hidden_database(self):
        from resources.lib.database.trakt_sync.hidden import TraktSyncDatabase as HiddenDatabase

        return HiddenDatabase()

    @cached_property
    def list_builder(self):
        from resources.lib.modules.list_builder import ListBuilder

        return ListBuilder()

    def _resolve(self, tmdb_ids, media_type):
        """Resolves tmdb_ids to Trakt objects in parallel (mirrors simklMenus.Menus._resolve), dropping (and logging) any id Trakt has no
        match for, or that errors or times out, rather than failing the whole list. The result comes back in the order the lookups
        finished: callers put it back in list order with service_episode_window.order_like."""
        if not tmdb_ids:
            return []

        resolved = []
        executor = ThreadPoolExecutor(max_workers=min(10, len(tmdb_ids)))
        try:
            futures = {
                executor.submit(self.trakt_api.search_by_tmdb_id, tmdb_id, media_type): tmdb_id for tmdb_id in tmdb_ids
            }
            try:
                for future in as_completed(futures, timeout=20):
                    resolved_item = self._collect_resolved(future, futures[future], media_type)
                    if resolved_item:
                        resolved.append(resolved_item)
            except Exception as e:
                g.log(f"WeTrakr menus: resolve batch timed out, using partial results: {e}", "warning")
                for future in futures:
                    if future.done():
                        resolved_item = self._collect_resolved(future, futures[future], media_type)
                        if resolved_item:
                            resolved.append(resolved_item)
        finally:
            # wait=False enforces the 20 s as_completed ceiling instead of blocking on shutdown (see simklMenus._resolve)
            executor.shutdown(wait=False, cancel_futures=True)

        if resolved:
            self._seed_trakt_sync(resolved, media_type)
        return resolved

    @staticmethod
    def _seed_trakt_sync(resolved, media_type):
        """Writes freshly resolved items into the local Trakt sync DB: movie_menu_builder/show_list_builder cannot render them before that
        (identical to simklMenus.Menus._seed_trakt_sync)"""
        if media_type == "movie":
            from resources.lib.database.trakt_sync.movies import TraktSyncDatabase as MoviesDatabase

            MoviesDatabase().insert_trakt_movies(resolved)
        else:
            from resources.lib.database.trakt_sync.shows import TraktSyncDatabase as ShowsDatabase

            ShowsDatabase().insert_trakt_shows(resolved)

    @staticmethod
    def _collect_resolved(future, tmdb_id, media_type):
        try:
            trakt_object = future.result()
        except Exception as e:
            g.log(f"WeTrakr menus: error resolving tmdb_id {tmdb_id} ({media_type}): {e}", "warning")
            return None
        if not trakt_object or not trakt_object.get("trakt_id"):
            g.log(f"WeTrakr menus: no Trakt match for tmdb_id {tmdb_id} ({media_type}), skipping", "debug")
            return None
        return trakt_object

    # region local lists (wetrakrSync.db)
    def recent_movies(self):
        """Watched movies, newest first, paged like Simkl's Recently Watched Movies"""
        from resources.lib.modules import service_episode_window

        rows = self.wetrakr_database.get_recent_movies(self.page_limit + 1, self.page_start)
        has_more = len(rows) > self.page_limit
        tmdb_ids = [r["tmdb_id"] for r in rows[: self.page_limit]]
        trakt_list = service_episode_window.order_like(self._resolve(tmdb_ids, "movie"), tmdb_ids)
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.movie_menu_builder(trakt_list, force_next_page=has_more, hide_watched=False)

    def recent_shows(self):
        """Shows you watched episodes of, the show with the newest play first"""
        from resources.lib.modules import service_episode_window

        tmdb_ids = [r["tmdb_id"] for r in self.wetrakr_database.get_recent_shows(self.page_limit)]
        trakt_list = service_episode_window.order_like(self._resolve(tmdb_ids, "show"), tmdb_ids)
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.show_list_builder(trakt_list, no_paging=True, hide_watched=False)

    def in_progress_movies(self):
        from resources.lib.modules import service_episode_window

        tmdb_ids = [r["tmdb_id"] for r in self.wetrakr_database.get_bookmarks("movie")]
        trakt_list = service_episode_window.order_like(self._resolve(tmdb_ids, "movie"), tmdb_ids)
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.movie_menu_builder(trakt_list, no_paging=True)

    def in_progress_episodes(self):
        rows = self.wetrakr_database.get_bookmarks("episode")
        if not rows:
            g.cancel_directory()
            return
        episodes = self._resolve_episode_rows(rows)
        if not episodes:
            g.cancel_directory()
            return
        self.list_builder.mixed_episode_builder(episodes, no_paging=True)

    def watched_episodes(self):
        """Every episode you watched on WeTrakr, newest first, paged (Trakt: My Watched Episodes; 3.9.179). Read from the local wetrakrSync.db."""
        from resources.lib.modules import service_episode_window as window

        offset = (g.PAGE - 1) * self.page_limit
        rows = self.wetrakr_database.get_watched_episodes(self.page_limit + 1, offset)
        has_more = len(rows) > self.page_limit
        episodes = window.watched_episode_items(
            self.trakt_api, [(r["tmdb_id"], r["season"], r["number"]) for r in rows[: self.page_limit]]
        )
        g.log(f"WeTrakr menus: watched episodes page {g.PAGE} {len(rows[: self.page_limit])} rows, {len(episodes)} resolved", "debug")
        if not episodes:
            g.cancel_directory()
            return
        self.list_builder.mixed_episode_builder(episodes, hide_watched=False, force_next_page=has_more)

    # endregion

    # region calendar lists (3.9.179, WeTrakr's own calendar of the shows you follow: modules/wetrakr_calendar.py)
    def recent_episodes(self):
        """Episodes aired in the last 14 days from the shows you follow on WeTrakr, newest first, paged (Trakt: Recent Episodes)"""
        from resources.lib.modules import wetrakr_calendar

        today = datetime.date.today()
        rows = wetrakr_calendar.following_episodes(today - datetime.timedelta(days=13), today, self.wetrakr_api)
        rows.reverse()
        self._calendar_menu(rows, "recent episodes", upcoming=False)

    def upcoming_episodes(self):
        """Episodes airing in the next 30 days from the shows you follow on WeTrakr, soonest first, paged (Trakt: Upcoming Episodes)"""
        from resources.lib.modules import wetrakr_calendar

        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        rows = wetrakr_calendar.following_episodes(tomorrow, tomorrow + datetime.timedelta(days=29), self.wetrakr_api)
        self._calendar_menu(rows, "upcoming episodes", upcoming=True)

    def _calendar_menu(self, rows, label, upcoming):
        """One page of calendar rows (already in display order): the page is cut BEFORE the Trakt resolve, and force_next_page says whether rows
        remain past it whatever of this page's rows resolve"""
        start = (g.PAGE - 1) * self.page_limit
        page_rows = rows[start : start + self.page_limit]
        episodes = self._resolve_episode_rows(
            [{"tmdb_id": r["tmdb_id"], "season": r["season"], "number": r["number"]} for r in page_rows], hidden_section="calendar"
        )
        g.log(f"WeTrakr menus: {label} {len(rows)} in the window, page {g.PAGE} {len(page_rows)} rows, {len(episodes)} resolved", "debug")
        if not episodes:
            g.cancel_directory()
            return
        params = {"hide_watched": False, "force_next_page": len(rows) > start + self.page_limit}
        if upcoming:  # a date in front of each title, and the episodes that have not aired are what this list is for (Recent keeps the builder's default, like Simkl's)
            params.update(prepend_date=True, hide_unaired=False)
        self.list_builder.mixed_episode_builder(episodes, **params)

    # endregion

    # region live lists
    def _status_ids(self, status, target):
        """TMDb ids of one status list of WeTrakr (GET /sync/tracking/{status}/{target}, compact, every page), newest first; None when the list
        could not be read completely"""
        ok, rows = self.wetrakr_api.get_list(f"/sync/tracking/{status}/{target}")
        if not ok:
            g.log(f"WeTrakr menus: the {status} {target} list could not be read", "warning")
            return None
        return status_ids(rows)

    def _status_menu(self, status, media_type):
        """One status list: the TMDb id list is cut to the current page BEFORE the Trakt resolve (the list has no server-side paging of its
        own here), and force_next_page says whether ids remain past this page whatever of this page's ids resolve"""
        from resources.lib.modules import service_episode_window

        tmdb_ids = self._status_ids(status, "movies" if media_type == "movie" else "shows")
        if tmdb_ids is None:
            g.cancel_directory()
            return
        page_ids = tmdb_ids[self.page_start : self.page_end]
        has_more = len(tmdb_ids) > self.page_end
        trakt_list = service_episode_window.order_like(self._resolve(page_ids, media_type), page_ids)
        if not trakt_list:
            g.cancel_directory()
            return
        if media_type == "movie":
            self.list_builder.movie_menu_builder(trakt_list, hide_watched=False, force_next_page=has_more)
        else:
            self.list_builder.show_list_builder(trakt_list, hide_watched=False, force_next_page=has_more)

    def plan_to_watch_movies(self):
        self._status_menu("planning", "movie")

    def dropped_movies(self):
        self._status_menu("dropped", "movie")

    def watching_shows(self):
        self._status_menu("watching", "show")

    def plan_to_watch_shows(self):
        self._status_menu("planning", "show")

    def hold_shows(self):
        self._restore_header("wetrakrHoldShowsRestore", 31760, 31761, "shows_collected")
        self._status_menu("hold", "show")

    def dropped_shows(self):
        self._restore_header("wetrakrDroppedShowsRestore", 31759, 31548, "shows_watched")
        self._status_menu("dropped", "show")

    @staticmethod
    def _restore_header(action, label_id, description_id, icon):
        """3.9.194: the Restore entry at the top of page 1 of the On Hold / Dropped Shows list (as MDBList's Dropped Shows has)"""
        if g.PAGE == 1:
            g.add_directory_item(
                g.get_language_string(label_id),
                action=action,
                is_folder=False,  # an action, not a folder: a Container.Refresh on the list must not run the picker again (MDBList's Restore header, a folder item, does)
                description=g.get_language_string(description_id),
                menu_item=g.create_icon_dict(icon, g.ICONS_PATH),
            )

    def _restore_status(self, status, heading_id, empty_id):
        """3.9.194: take the shows you pick off WeTrakr's `status` list (hold / dropped): POST /sync/tracking/remove puts a show back in the list
        its plays give it (watching or waiting; live-probed 2026-11-25). The list is read live, every page, never from a cached copy."""
        import xbmcgui
        from resources.lib.gui.wetrakr_manager import read_outcome

        tmdb_ids = self._status_ids(status, "shows")
        if tmdb_ids is None:
            g.notification(g.ADDON_NAME, g.get_language_string(30287))
            return
        if not tmdb_ids:
            g.notification(g.ADDON_NAME, g.get_language_string(empty_id))
            return
        titles = {}
        for item in self._resolve(tmdb_ids, "show"):  # the compact rows carry no title
            # a resolved Trakt search result keeps its show data in trakt_object > info (the *_id keys are also on the object itself)
            info = (
                (item.get("trakt_object") or {}).get("info")
                or ((item.get("show") or {}).get("trakt_object") or {}).get("info")
                or item.get("info")
                or {}
            )
            tmdb = _int(item.get("tmdb_id") or info.get("tmdb_id"))
            title = item.get("title") or info.get("title") or ""
            year = item.get("year") or info.get("year")
            titles[tmdb] = f"{title} ({year})" if year else title
        entries = [(i, titles.get(i) or f"TMDb {i}") for i in tmdb_ids]
        selected = xbmcgui.Dialog().multiselect(
            f"{g.ADDON_NAME}: {g.get_language_string(heading_id)}", [e[1] for e in entries]
        )
        if not selected:
            return
        picked = [entries[i][0] for i in selected]
        response = self.wetrakr_api.user_request(
            "POST", "/sync/tracking/remove", body={"shows": [{"ids": {"tmdb": i}, "status": status} for i in picked]}
        )
        if read_outcome(response)[0] == "failed":
            g.notification(g.ADDON_NAME, g.get_language_string(30287))
            return
        g.notification(g.ADDON_NAME, g.get_language_string(31549).format(len(picked)))
        g.container_refresh()  # the list the header sits on reads WeTrakr again
        g.trigger_widget_refresh()

    def dropped_shows_bulk_restore(self):
        self._restore_status("dropped", 31548, 31550)

    def hold_shows_bulk_restore(self):
        self._restore_status("hold", 31761, 31762)

    def next_up(self):
        """One row per show WeTrakr lists as watching: WeTrakr's own next_watching_episode, the most recently watched show first. The
        watching list is read with extended=episode_level_1 (the next episode is only in the full rows), newest watched first; caught-up shows
        and episodes that have not aired are left out. Each pick is resolved like in_progress_episodes() (Trakt id lookup, then one SELECT for
        that episode's ids) and formatted by get_mixed_episode_list(), never get_episode_list(). hide_watched=False is load-bearing for the same
        reason as in simklMenus.next_up(): Trakt's own watched flag means nothing for a Trakt-disabled user."""
        ok, shows = self.wetrakr_api.get_pages(
            "/sync/tracking/watching/shows",
            {"extended": "episode_level_1", "sort_by": "my_last_watched"},
            limit=_NEXT_UP_PAGE_LIMIT,
            max_pages=_NEXT_UP_MAX_PAGES,
            partial_ok=True,
        )
        if not ok:
            g.log("WeTrakr menus: the watching list could not be read for Next Up", "warning")
            g.cancel_directory()
            return
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        targets = next_up_targets(shows, today)
        episodes = self._resolve_episode_rows(targets) if targets else []
        if not episodes:
            g.cancel_directory()
            return
        self.list_builder.mixed_episode_builder(episodes, no_paging=True, hide_watched=False)

    # endregion

    def _resolve_episode_rows(self, rows, hidden_section="progress_watched"):
        """rows: dicts with tmdb_id/season/number. Returns episode action-args dicts in row order, dropping rows Trakt cannot place, shows
        hidden under hidden_section (progress_watched; calendar for the calendar lists), and anything that errors or misses the 20 s ceiling
        (mirrors simklMenus._resolve_episode_rows)."""
        from resources.lib.database.trakt_sync.shows import TraktSyncDatabase as ShowsDatabase

        if not rows:  # a calendar page past the end of its list: ThreadPoolExecutor(max_workers=0) would raise
            return []
        shows_db = ShowsDatabase()
        hidden_shows = self.hidden_database.get_hidden_items(hidden_section, "tvshow")
        results = [None] * len(rows)
        executor = ThreadPoolExecutor(max_workers=min(10, len(rows)))
        try:
            futures = {
                executor.submit(self.trakt_api.search_by_tmdb_id, row["tmdb_id"], "show"): i for i, row in enumerate(rows)
            }
            seen = set()
            try:
                for future in as_completed(futures, timeout=20):
                    i = futures[future]
                    results[i] = self._collect_episode(future, rows[i], shows_db, hidden_shows)
                    seen.add(i)
            except Exception as e:
                g.log(f"WeTrakr menus: episode resolve batch timed out, using partial results: {e}", "warning")
                for future in futures:
                    i = futures[future]
                    if i not in seen and future.done():
                        results[i] = self._collect_episode(future, rows[i], shows_db, hidden_shows)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return [ep for ep in results if ep]

    @staticmethod
    def _collect_episode(future, row, shows_db, hidden_shows):
        try:
            trakt_show = future.result()
        except Exception as e:
            g.log(f"WeTrakr menus: error resolving tmdb_id {row['tmdb_id']} (show): {e}", "warning")
            return None
        if not trakt_show or not trakt_show.get("trakt_id"):
            g.log(f"WeTrakr menus: no Trakt match for tmdb_id {row['tmdb_id']} (show), skipping episode", "debug")
            return None
        if trakt_show["trakt_id"] in hidden_shows:
            return None
        try:
            return shows_db.get_episode_action_args(trakt_show["trakt_id"], row["season"], row["number"])
        except Exception as e:
            g.log(f"WeTrakr menus: error building episode args for tmdb_id {row['tmdb_id']} (show): {e}", "warning")
            return None
