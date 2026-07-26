import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import cached_property
from urllib.parse import quote

import xbmcgui

from resources.lib.modules.globals import g

# MDBList's /lists/{listid}/items `sort` enum - key, display-string-id pairs, in the
# order they're offered in the sort picker. Covers all 25 keys the API documents.
_LIST_ITEMS_SORT_OPTIONS = [
    ("rank", 31015),
    ("title", 31036),
    ("score", 31016),
    ("added", 31038),
    ("released", 31019),
    ("releasedigital", 31020),
    ("imdbrating", 31021),
    ("imdbvotes", 31022),
    ("imdbpopular", 31024),
    ("tmdbpopular", 31025),
    ("metacritic", 31029),
    ("rtomatoes", 31027),
    ("rtaudience", 31028),
    ("myanimelist", 31030),
    ("letterrating", 31031),
    ("lettervotes", 31032),
    ("rogerebert", 31026),
    ("last_air_date", 31023),
    ("runtime", 31035),
    ("budget", 31033),
    ("revenue", 31034),
    ("sort_title", 31037),
    ("usort", 31017),
    ("score_average", 31018),
    ("random", 31039),
]

# Supporter-only rating filters: API param prefix -> display-string-id. Prefixes are
# MDBList's filter_<prefix>_min/_max names, which don't all match the sort keys above
# (e.g. sort's "rtomatoes" is filter's "tomatorating" for the same underlying source).
_RATING_FILTER_SOURCES = [
    ("score", 31016),
    ("imdbrating", 31021),
    ("tmdbrating", 31040),
    ("metarating", 31029),
    ("tomatorating", 31027),
    ("popcornrating", 31028),
    ("letterrating", 31031),
    ("malrating", 31030),
]

# g.REQUEST_PARAMS keys that carry list-items sort/filter state across requests (Next
# Page and re-entering the picker both need to preserve whatever's currently active).
_FILTER_KEYS = (
    "sort", "order", "filter_title", "filter_genre", "released_from", "released_to",
    "filter_score_min", "filter_score_max",
    "filter_imdbrating_min", "filter_imdbrating_max",
    "filter_tmdbrating_min", "filter_tmdbrating_max",
    "filter_metarating_min", "filter_metarating_max",
    "filter_tomatorating_min", "filter_tomatorating_max",
    "filter_popcornrating_min", "filter_popcornrating_max",
    "filter_letterrating_min", "filter_letterrating_max",
    "filter_malrating_min", "filter_malrating_max",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Menus:
    """MDBList-sourced menus (Recently Watched, In-Progress).

    Items are local mdblistSync.db (or, for in-progress, live sync/playback) rows
    keyed by tmdb_id. Each is resolved to a Trakt object via TraktAPI.search_by_tmdb_id
    (Trakt's ID lookup - no personal OAuth needed) before being handed to the existing,
    unmodified Trakt-native ListBuilder methods. This lets MDBList-only items render
    and play through the exact same proven pipeline as any normally-browsed Trakt item,
    rather than a parallel MDBList-native pipeline that would still need to separately
    solve source-search's own trakt_id requirement.
    """

    def __init__(self):
        self.page_limit = g.get_int_setting("item.limit")

    @cached_property
    def mdblist_database(self):
        from resources.lib.database.mdblist_sync import MDBListSyncDatabase

        return MDBListSyncDatabase()

    @cached_property
    def mdblist_api(self):
        from resources.lib.indexers.mdblist import MDBListAPI

        return MDBListAPI()

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
        """Resolves tmdb_ids to Trakt objects in parallel (mirrors debrid/external_cache.py's
        ThreadPoolExecutor + as_completed pattern), dropping (and logging) any id Trakt has
        no match for - or that errors/times out - rather than failing the whole list."""
        if not tmdb_ids:
            return []

        resolved = []
        executor = ThreadPoolExecutor(max_workers=min(10, len(tmdb_ids)))
        try:
            futures = {
                executor.submit(self.trakt_api.search_by_tmdb_id, tmdb_id, media_type): tmdb_id
                for tmdb_id in tmdb_ids
            }
            try:
                for future in as_completed(futures, timeout=20):
                    resolved_item = self._collect_resolved(future, futures[future], media_type)
                    if resolved_item:
                        resolved.append(resolved_item)
            except Exception as e:
                g.log(f"MDBList menus: resolve batch timed out, using partial results: {e}", "warning")
                for future in futures:
                    if future.done():
                        resolved_item = self._collect_resolved(future, futures[future], media_type)
                        if resolved_item:
                            resolved.append(resolved_item)
        finally:
            # `with ThreadPoolExecutor(...)` would block here on shutdown(wait=True) regardless
            # of the as_completed timeout above, silently turning the 20s ceiling into "however
            # long the slowest straggler takes." wait=False actually enforces the ceiling.
            # (cancel_futures needs Python 3.9+; Kodi 21.3 embeds 3.8, so it's omitted here -
            # already-submitted futures still run to completion in the background, but we no
            # longer wait on them.)
            executor.shutdown(wait=False)

        if resolved:
            self._seed_trakt_sync(resolved, media_type)
        return resolved

    @staticmethod
    def _seed_trakt_sync(resolved, media_type):
        """Writes freshly-resolved items into the local Trakt sync DB via the same
        insert_trakt_movies/insert_trakt_shows path every native Trakt-list menu (Watchlist,
        Collection, etc.) goes through before rendering. Without this, movie_menu_builder/
        show_list_builder's get_movie_list/get_show_list crash on any trakt_id that's never
        been locally synced before - _update_movies/_update_shows only refresh existing rows,
        they don't create them from scratch."""
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
            g.log(f"MDBList menus: error resolving tmdb_id {tmdb_id} ({media_type}): {e}", "warning")
            return None
        if not trakt_object or not trakt_object.get("trakt_id"):
            g.log(f"MDBList menus: no Trakt match for tmdb_id {tmdb_id} ({media_type}), skipping", "debug")
            return None
        return trakt_object

    def _live_playback_rows(self, media_type):
        """Live GET /sync/playback - fresher than the local bookmarks cache, which
        only updates on scrobble events and can go stale between syncs."""
        entries = self.mdblist_api.get_json("sync/playback") or []
        if isinstance(entries, dict):
            entries = entries.get("items") or []

        rows = []
        for entry in entries:
            if entry.get("type") != media_type:
                continue
            paused_at = entry.get("paused_at")
            if media_type == "movie":
                tmdb_id = (entry.get("movie") or {}).get("ids", {}).get("tmdb")
                if tmdb_id:
                    rows.append({"tmdb_id": tmdb_id, "paused_at": paused_at})
            elif media_type == "episode":
                show = entry.get("show") or {}
                episode = entry.get("episode") or {}
                tmdb_id = show.get("ids", {}).get("tmdb")
                season = episode.get("season")
                number = episode.get("number")
                if tmdb_id and season is not None and number is not None:
                    rows.append(
                        {"tmdb_id": tmdb_id, "season": season, "number": number, "paused_at": paused_at}
                    )
        return rows

    def recent_movies(self):
        rows = self.mdblist_database.get_recent_movies(self.page_limit)
        trakt_list = self._resolve([r["tmdb_id"] for r in rows], "movie")
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.movie_menu_builder(trakt_list, no_paging=True, hide_watched=False)

    def recent_shows(self):
        rows = self.mdblist_database.get_recent_shows(self.page_limit)
        trakt_list = self._resolve([r["tmdb_id"] for r in rows], "show")
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.show_list_builder(trakt_list, no_paging=True, hide_watched=False)

    def in_progress_movies(self):
        rows = self._live_playback_rows("movie")
        trakt_list = self._resolve([r["tmdb_id"] for r in rows], "movie")
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.movie_menu_builder(trakt_list, no_paging=True)

    def in_progress_episodes(self):
        from resources.lib.database.trakt_sync.shows import TraktSyncDatabase as ShowsDatabase

        rows = self._live_playback_rows("episode")
        shows_db = ShowsDatabase()
        hidden_shows = self.hidden_database.get_hidden_items("progress_watched", "tvshow")
        episodes = []
        for row in rows:
            trakt_show = self.trakt_api.search_by_tmdb_id(row["tmdb_id"], "show")
            if not trakt_show or not trakt_show.get("trakt_id"):
                g.log(
                    f"MDBList menus: no Trakt match for tmdb_id {row['tmdb_id']} (show), "
                    "skipping in-progress episode",
                    "debug",
                )
                continue
            if trakt_show["trakt_id"] in hidden_shows:
                continue
            episode = shows_db.get_episode_action_args(trakt_show["trakt_id"], row["season"], row["number"])
            if not episode:
                continue
            episodes.append(episode)
        if not episodes:
            g.cancel_directory()
            return
        self.list_builder.mixed_episode_builder(episodes, no_paging=True)

    @staticmethod
    def _list_context_menu(lst):
        """Right-click actions for a My Lists row. Only Rename/Privacy - Delete is API-
        restricted to static lists, which never reach this menu (they report mediatype=None,
        so my_lists()'s own movie/show filter above always excludes them - see
        _mdblist_static_lists()'s docstring in trakt_context_menu.py for the same finding).
        Static lists are managed from there instead, where they're already reachable.
        """
        list_id = lst.get("id")
        rename_url = g.create_url(
            g.BASE_URL,
            {
                "action": "mdblistListManager",
                "action_args": {"list_id": list_id, "op": "rename", "name": lst.get("name", "")},
            },
        )
        make_private = not lst.get("private")
        privacy_label = g.get_language_string(31055) if make_private else g.get_language_string(31056)
        privacy_url = g.create_url(
            g.BASE_URL,
            {
                "action": "mdblistListManager",
                "action_args": {"list_id": list_id, "op": "privacy", "private": make_private},
            },
        )
        return [
            (g.get_language_string(31054), f'RunPlugin("{rename_url}")'),
            (privacy_label, f'RunPlugin("{privacy_url}")'),
        ]

    def my_lists(self, media_type):
        """Mirrors listsHelper.py's _create_list_menu (Trakt's My Lists) - live API call
        rather than a DB cache, since MDBList list membership isn't synced locally.

        No `sort` kwarg is passed to lists_menu_builder - that would register Kodi's
        client-side SORT_METHOD_LABEL, which silently re-alphabetizes on screen and
        would mask the server-driven sort (ranked/name/created) chosen below. Leaving
        it unset keeps SORT_METHOD_NONE, so the order MDBList returns is authoritative.
        """
        # Carries the active sort forward the same way Next Page does (list_builder.py
        # copies g.REQUEST_PARAMS wholesale) - without this, a second visit to "Sort
        # by..." starts from a request with no `sort` param, and cancelling that dialog
        # would silently revert to the default instead of keeping the current choice.
        current_sort = {"sort": g.REQUEST_PARAMS["sort"]} if g.REQUEST_PARAMS.get("sort") else {}
        g.add_directory_item(
            g.get_language_string(30998),
            action="mdblistMyListsSort",
            mediatype=media_type,
            special_sort="top",
            **current_sort,
        )

        mdb_media_type = "movie" if media_type == "movies" else "show"
        sort = g.REQUEST_PARAMS.get("sort") or "ranked"
        lists = self.mdblist_api.get_json("lists/user", sort=sort) or []
        trakt_list = [
            {
                "info": {"title": self._list_display_name(lst), "mediatype": "list"},
                "args": {"list_id": lst.get("id")},
                "cm": self._list_context_menu(lst),
            }
            for lst in lists
            if lst.get("mediatype") == mdb_media_type
        ]
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.lists_menu_builder(trakt_list, action="mdblistListItems", media_type=media_type)

    def my_lists_sort_picker(self, media_type):
        """Entry point for the "Sort by..." control item on My Lists. Prompts, then
        renders through my_lists() directly (mirrors movieMenus.py's movies_search ->
        movies_search_results split) rather than a Container.Update redirect."""
        heading = f"{g.ADDON_NAME}: {g.get_language_string(30998)}"
        options = [
            (g.get_language_string(30999), "ranked"),
            (g.get_language_string(31000), "name"),
            (g.get_language_string(31001), "created"),
        ]
        selection = xbmcgui.Dialog().select(heading, [label for label, _ in options])
        if selection != -1:
            g.REQUEST_PARAMS["sort"] = options[selection][1]
        self.my_lists(media_type)

    @staticmethod
    def _list_display_name(lst):
        subtitle = g.get_language_string(31041).format(lst.get("items", 0))
        tags = []
        if lst.get("private"):
            tags.append(g.get_language_string(31042))
        if lst.get("dynamic"):
            tags.append(g.get_language_string(31043))
        if tags:
            subtitle = f"{subtitle} · {' · '.join(tags)}"
        return f"{lst.get('name', '')}  {g.color_string(f'[{subtitle}]')}"

    def list_manager(self, action_args):
        """Dispatched from a My Lists row's context menu (see _list_context_menu). Only
        rename/privacy reach here - dynamic lists (the only ones this menu ever shows) can't
        be deleted per the API, so there's no "op" value for it."""
        list_id = action_args["list_id"]
        op = action_args["op"]
        if op == "rename":
            self._rename_list(list_id, action_args.get("name", ""))
        elif op == "privacy":
            self._set_list_privacy(list_id, action_args.get("private", False))

    def _rename_list(self, list_id, current_name):
        name = g.get_keyboard_input(heading=g.get_language_string(31054), default=current_name)
        if not name or name == current_name:
            return
        response = self.mdblist_api.put(f"lists/{list_id}", {"name": name})
        if response is None or not response.ok:
            g.notification(
                f"{g.ADDON_NAME}: {g.get_language_string(30961)}",
                g.get_language_string(30287),
            )
            g.log(f"Failed to rename MDBList list {list_id}\nResponse: {response}")
            return
        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30961)}",
            g.get_language_string(31058).format(name),
        )
        g.container_refresh()
        g.trigger_widget_refresh()

    def _set_list_privacy(self, list_id, private):
        response = self.mdblist_api.put(f"lists/{list_id}", {"private": private})
        if response is None or not response.ok:
            g.notification(
                f"{g.ADDON_NAME}: {g.get_language_string(30961)}",
                g.get_language_string(30287),
            )
            g.log(f"Failed to update MDBList list {list_id} privacy\nResponse: {response}")
            return
        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30961)}",
            g.get_language_string(31059),
        )
        g.container_refresh()
        g.trigger_widget_refresh()

    def list_items(self):
        """Unlike recent_movies/recent_shows, list content can be arbitrarily long, so this
        fetches and resolves a single bounded page (<=page_limit Trakt lookups, well under
        the _resolve timeout) instead of the whole list. force_next_page passes MDBList's own
        has_more through directly - the default length-based heuristic would misfire here,
        since _resolve can drop unmatched items and shrink a full page below page_limit.

        Uses get() rather than get_json() because MDBList signals pagination via the
        X-Has-More response header, not a body field - get_json() only returns the parsed
        body, so the header would otherwise be unreachable here.

        sort/order/filter_* are read from g.REQUEST_PARAMS rather than function args, same
        as list_id/media_type above - this lets them ride the existing Next Page rebuild
        (list_builder.py copies g.REQUEST_PARAMS forward wholesale) with no extra plumbing.
        """
        arguments = g.REQUEST_PARAMS['action_args']
        list_id = arguments['list_id']
        media_type = g.REQUEST_PARAMS.get('media_type')
        mdb_media_type = "movie" if media_type == "movies" else "show"

        # Same forwarding as my_lists()'s sort control - without carrying the active
        # sort/filter_* along, a second visit to "Sort / Filter..." would start from a
        # request with none of them set, silently dropping whatever's currently applied
        # (list_items_menu()'s "Clear All Filters" option would also never appear).
        active_filters = {key: g.REQUEST_PARAMS[key] for key in _FILTER_KEYS if g.REQUEST_PARAMS.get(key)}
        g.add_directory_item(
            g.get_language_string(31002),
            action="mdblistListItemsMenu",
            action_args={"list_id": list_id},
            media_type=media_type,
            special_sort="top",
            **active_filters,
        )

        offset = (g.PAGE - 1) * self.page_limit
        query_params = {"limit": self.page_limit, "offset": offset}
        for key in _FILTER_KEYS:
            value = g.REQUEST_PARAMS.get(key)
            if value:
                query_params[key] = value

        try:
            api_response = self.mdblist_api.get(f"lists/{list_id}/items", **query_params)
        except Exception as e:
            g.log(f"MDBList menus: failed to fetch list items for list {list_id}: {e}", "error")
            api_response = None

        if api_response is not None and api_response.ok:
            try:
                response = api_response.json()
            except ValueError:
                response = {}
            has_more = api_response.headers.get("X-Has-More", "").lower() == "true"
        else:
            response = {}
            has_more = False

        raw_items = response.get("movies" if mdb_media_type == "movie" else "shows") or []
        tmdb_ids = [item["id"] for item in raw_items if item.get("id")]
        trakt_list = self._resolve(tmdb_ids, mdb_media_type)
        if not trakt_list:
            g.cancel_directory()
            return
        if mdb_media_type == "movie":
            self.list_builder.movie_menu_builder(trakt_list, hide_watched=False, force_next_page=has_more)
        else:
            self.list_builder.show_list_builder(trakt_list, hide_watched=False, force_next_page=has_more)

    def list_items_menu(self):
        """Hub for list-items' "Sort / Filter..." control item: prompts via a nested
        Dialog().select()/input(), applies the choice to g.REQUEST_PARAMS (resetting to
        page 1 - a new sort/filter on page 3 of the old query would otherwise land on
        page 3 of the new one), then renders through the unchanged list_items()."""
        heading = f"{g.ADDON_NAME}: {g.get_language_string(31002)}"
        options = [(g.get_language_string(30998), "sort")]
        options.append((g.get_language_string(31003), "filter_title"))
        options.append((g.get_language_string(31004), "filter_genre"))
        options.append((g.get_language_string(31005), "released"))
        if self.mdblist_api.is_supporter():
            options.append((g.get_language_string(31006), "rating"))
        if any(g.REQUEST_PARAMS.get(key) for key in _FILTER_KEYS):
            options.append((g.get_language_string(31007), "clear"))

        selection = xbmcgui.Dialog().select(heading, [label for label, _ in options])
        if selection != -1:
            choice = options[selection][1]
            {
                "sort": self._pick_sort,
                "filter_title": self._pick_filter_title,
                "filter_genre": self._pick_filter_genre,
                "released": self._pick_release_range,
                "rating": self._pick_rating_filter,
                "clear": self._clear_filters,
            }[choice]()
        self.list_items()

    @staticmethod
    def _pick_sort():
        heading = f"{g.ADDON_NAME}: {g.get_language_string(30998)}"
        labels = [g.get_language_string(sid) for _, sid in _LIST_ITEMS_SORT_OPTIONS]
        selection = xbmcgui.Dialog().select(heading, labels)
        if selection == -1:
            return
        sort_key = _LIST_ITEMS_SORT_OPTIONS[selection][0]

        order_selection = xbmcgui.Dialog().select(
            heading, [g.get_language_string(31008), g.get_language_string(31009)]
        )
        if order_selection == -1:
            return

        g.REQUEST_PARAMS["sort"] = sort_key
        g.REQUEST_PARAMS["order"] = "asc" if order_selection == 0 else "desc"
        g.REQUEST_PARAMS["page"] = 1
        g.PAGE = 1

    @staticmethod
    def _pick_filter_title():
        current = g.REQUEST_PARAMS.get("filter_title", "")
        value = g.get_keyboard_input(heading=g.get_language_string(31003), default=current)
        if value is None:
            return
        if value:
            g.REQUEST_PARAMS["filter_title"] = value
        else:
            g.REQUEST_PARAMS.pop("filter_title", None)
        g.REQUEST_PARAMS["page"] = 1
        g.PAGE = 1

    def _pick_filter_genre(self):
        # anime=0 (regular genres) - the spec doesn't document a default when omitted,
        # and the endpoint's own example response is all-anime, so this is passed
        # explicitly rather than relied on.
        genres = self.mdblist_api.get_json("genres", anime=0) or []
        heading = f"{g.ADDON_NAME}: {g.get_language_string(31004)}"
        labels = [g.get_language_string(31010)] + [genre.get("title", genre.get("slug", "")) for genre in genres]
        selection = xbmcgui.Dialog().select(heading, labels)
        if selection == -1:
            return
        if selection == 0:
            g.REQUEST_PARAMS.pop("filter_genre", None)
        else:
            g.REQUEST_PARAMS["filter_genre"] = genres[selection - 1].get("slug")
        g.REQUEST_PARAMS["page"] = 1
        g.PAGE = 1

    @staticmethod
    def _pick_release_range():
        from_value = g.get_keyboard_input(
            heading=g.get_language_string(31011), default=g.REQUEST_PARAMS.get("released_from", "")
        )
        if from_value is None:
            return
        to_value = g.get_keyboard_input(
            heading=g.get_language_string(31012), default=g.REQUEST_PARAMS.get("released_to", "")
        )
        if to_value is None:
            return

        if from_value and _DATE_RE.match(from_value):
            g.REQUEST_PARAMS["released_from"] = from_value
        elif not from_value:
            g.REQUEST_PARAMS.pop("released_from", None)
        if to_value and _DATE_RE.match(to_value):
            g.REQUEST_PARAMS["released_to"] = to_value
        elif not to_value:
            g.REQUEST_PARAMS.pop("released_to", None)
        g.REQUEST_PARAMS["page"] = 1
        g.PAGE = 1

    @staticmethod
    def _pick_rating_filter():
        heading = f"{g.ADDON_NAME}: {g.get_language_string(31006)}"
        labels = [g.get_language_string(sid) for _, sid in _RATING_FILTER_SOURCES]
        selection = xbmcgui.Dialog().select(heading, labels)
        if selection == -1:
            return
        prefix, label_id = _RATING_FILTER_SOURCES[selection]
        source_label = g.get_language_string(label_id)

        min_key, max_key = f"filter_{prefix}_min", f"filter_{prefix}_max"
        min_value = xbmcgui.Dialog().input(
            f"{source_label}: {g.get_language_string(31013)}",
            str(g.REQUEST_PARAMS.get(min_key, "")),
            xbmcgui.INPUT_NUMERIC,
        )
        max_value = xbmcgui.Dialog().input(
            f"{source_label}: {g.get_language_string(31014)}",
            str(g.REQUEST_PARAMS.get(max_key, "")),
            xbmcgui.INPUT_NUMERIC,
        )

        if min_value:
            g.REQUEST_PARAMS[min_key] = min_value
        else:
            g.REQUEST_PARAMS.pop(min_key, None)
        if max_value:
            g.REQUEST_PARAMS[max_key] = max_value
        else:
            g.REQUEST_PARAMS.pop(max_key, None)
        g.REQUEST_PARAMS["page"] = 1
        g.PAGE = 1

    @staticmethod
    def _clear_filters():
        for key in _FILTER_KEYS:
            g.REQUEST_PARAMS.pop(key, None)
        g.REQUEST_PARAMS["page"] = 1
        g.PAGE = 1

    # region Discover (Phase 4) - browsing other users'/public lists, read-only.
    # Always media_type-paired at the entry point (movieMenus.py/tvshowMenus.py, same as
    # In Progress/Recently Watched/My Lists above) rather than one unified mixed screen -
    # Top/Search/User-lists all return movie and show lists together with no server-side
    # mediatype filter, but list_items() (reused as-is below) only ever renders one
    # mediatype per call, and a single mixed directory would also surface show-lists under
    # the Movies menu, fighting this addon's existing navigation model. Splitting client-side
    # here - the same `if lst.get("mediatype") == mdb_media_type` idiom my_lists() already
    # uses - needs zero changes to list_items()/list_builder.py, so the already-live-verified
    # My Lists path is untouched. Public lists with an unset mediatype (mixed-content or
    # unset-mediatype static lists, same class as Phase 1/3's own static-list finding) are
    # silently excluded from both the movie and show views - a known, deferred gap, not fixed
    # here, matching the user's Phase 3 choice not to unify browsing across list shapes.

    def discover_hub(self, media_type):
        g.add_directory_item(
            g.get_language_string(31065),
            action="mdblistTopLists",
            mediatype=media_type,
            menu_item=g.create_icon_dict("list", g.ICONS_PATH),
        )
        g.add_directory_item(
            g.get_language_string(31066),
            action="mdblistListSearch",
            action_args={"media_type": media_type},
            menu_item=g.create_icon_dict("movies_search", g.ICONS_PATH),
        )
        g.add_directory_item(
            g.get_language_string(31067),
            action="mdblistUserLists",
            action_args={"media_type": media_type},
            menu_item=g.create_icon_dict("list", g.ICONS_PATH),
        )
        g.close_directory(g.CONTENT_MENU)

    def _discover_results(self, lists, media_type):
        """Shared render step for Top/Search/User-lists. no_paging=True: none of the
        3 source endpoints document limit/offset params (confirmed against mdblist.apib -
        unlike /lists/{listid}/items, they always return their full result set in one
        response), so Kodi's own length-based Next Page heuristic would otherwise offer a
        button that just re-fetches the identical unpaginated list, not a new page."""
        mdb_media_type = "movie" if media_type == "movies" else "show"
        trakt_list = [
            {
                "info": {"title": self._list_display_name(lst), "mediatype": "list"},
                "args": {"list_id": lst.get("id")},
            }
            for lst in lists
            if lst.get("mediatype") == mdb_media_type
        ]
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.lists_menu_builder(
            trakt_list, action="mdblistListItems", media_type=media_type, no_paging=True
        )

    def top_lists(self, media_type):
        lists = self.mdblist_api.get_json("lists/top") or []
        self._discover_results(lists, media_type)

    def list_search(self, action_args):
        media_type = action_args.get("media_type")
        query = action_args.get("query")
        if not query:
            query = g.get_keyboard_input(heading=g.get_language_string(31066))
            if not query:
                g.cancel_directory(silent=True)
                return
        lists = self.mdblist_api.get_json("lists/search", query=query) or []
        self._discover_results(lists, media_type)

    def user_lists(self, action_args):
        media_type = action_args.get("media_type")
        username = action_args.get("username")
        if not username:
            username = g.get_keyboard_input(heading=g.get_language_string(31067))
            if not username:
                g.cancel_directory(silent=True)
                return
        lists = self.mdblist_api.get_json(f"lists/user/{quote(username, safe='')}") or []
        self._discover_results(lists, media_type)

    # endregion
