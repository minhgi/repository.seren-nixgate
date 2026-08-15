from functools import cached_property

from resources.lib.modules.globals import g


class Menus:
    """Merged Trakt + MDBList + Simkl menus (Merge - In-Progress, Merge - Watched).

    The Merge menu entries themselves are shown whenever at least 2 of
    {Trakt, MDBList, Simkl} are active (movieMenus.py/tvshowMenus.py
    my_movies/my_shows), so any pairing - not just Trakt+MDBList - can reach
    the functions below. Each source's fetch here is correspondingly guarded
    by its own _trakt_active()/_mdblist_active()/_simkl_active() check (same
    gate used everywhere else in this addon - homeMenu.py, movieMenus.py,
    tvshowMenus.py, player.py, syncGateway.py) so a user who disabled one
    service after syncing it once doesn't get its stale local rows folded in
    as phantom items.

    v1 known limitation: if Trakt's scrobbler no-ops on a stop event that
    another source's scrobbler correctly records, an item can be watched=1 on
    that source while still carrying a stale Trakt in-progress bookmark for a
    different episode of the same show. It will appear in both Merge-Watched
    and Merge-In-Progress simultaneously. Now applies pairwise to any two
    active sources, not just Trakt+MDBList. Not solved in v1.
    """

    def __init__(self):
        self.page_limit = g.get_int_setting("item.limit")

    @cached_property
    def movies_database(self):
        from resources.lib.database.trakt_sync.movies import TraktSyncDatabase

        return TraktSyncDatabase()

    @cached_property
    def shows_database(self):
        from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

        return TraktSyncDatabase()

    @cached_property
    def bookmark_database(self):
        from resources.lib.database.trakt_sync.bookmark import TraktSyncDatabase as BookmarkDatabase

        return BookmarkDatabase()

    @cached_property
    def mdblist_menus(self):
        from resources.lib.gui.mdblistMenus import Menus as MDBListMenus

        return MDBListMenus()

    @cached_property
    def simkl_menus(self):
        from resources.lib.gui.simklMenus import Menus as SimklMenus

        return SimklMenus()

    @cached_property
    def hidden_database(self):
        from resources.lib.database.trakt_sync.hidden import TraktSyncDatabase as HiddenDatabase

        return HiddenDatabase()

    @cached_property
    def list_builder(self):
        from resources.lib.modules.list_builder import ListBuilder

        return ListBuilder()

    @staticmethod
    def _trakt_active():
        return bool(g.get_setting('trakt.auth')) and g.get_bool_setting('trakt.enabled', False)

    @staticmethod
    def _mdblist_active():
        return g.get_setting('mdblist.enabled') == "true" and bool(g.get_setting('mdblist.auth') or g.get_setting('mdblist.apikey'))

    @staticmethod
    def _simkl_active():
        return bool(g.get_setting('simkl.auth')) and g.get_bool_setting('simkl.enabled')

    @staticmethod
    def _merge_by_key(trakt_rows, trakt_key_fn, trakt_ts_fn, *other_sources):
        """Folds Trakt's own rows with any number of additional already-resolved
        sources (each an (resolved_rows, key_fn, ts_by_key) tuple), keeping
        whichever row has the most recent timestamp per key. Single pass: each
        source's timestamp is read from its own ts_by_key dict as it is folded
        in, so - unlike calling this method twice - a later source can
        correctly out-rank an earlier non-Trakt winner without losing
        timestamp fidelity. Sources are folded in the order given, and a tie
        keeps whichever source was folded in first (ts > existing[0], not >=)."""
        merged = {}
        for r in trakt_rows:
            key = trakt_key_fn(r)
            if key is None:
                continue
            merged[key] = (trakt_ts_fn(r) or "", r)
        for resolved, key_fn, ts_by_key in other_sources:
            for obj in resolved:
                key = key_fn(obj)
                if key is None:
                    continue
                ts = ts_by_key.get(key) or ""
                existing = merged.get(key)
                if existing is None or ts > existing[0]:
                    merged[key] = (ts, obj)
        return [row for _, row in sorted(merged.values(), key=lambda pair: pair[0], reverse=True)]

    def watched_movies(self):
        trakt_rows = self.movies_database.get_watched_movies(1, force_all=True) if self._trakt_active() else []

        sources = []
        if self._mdblist_active():
            mdb_rows = self.mdblist_menus.mdblist_database.get_recent_movies(self.page_limit, force_all=True)
            mdb_ts_by_tmdb = {row["tmdb_id"]: row["last_watched_at"] for row in mdb_rows}
            sources.append((
                self.mdblist_menus._resolve([row["tmdb_id"] for row in mdb_rows], "movie"),
                lambda obj: obj.get("tmdb_id"),
                mdb_ts_by_tmdb,
            ))

        if self._simkl_active():
            simkl_rows = self.simkl_menus.simkl_database.get_recent_movies(self.page_limit, force_all=True)
            simkl_ts_by_tmdb = {row["tmdb_id"]: row["last_watched_at"] for row in simkl_rows}
            sources.append((
                self.simkl_menus._resolve([row["tmdb_id"] for row in simkl_rows], "movie"),
                lambda obj: obj.get("tmdb_id"),
                simkl_ts_by_tmdb,
            ))

        trakt_list = self._merge_by_key(
            trakt_rows,
            lambda r: r.get("tmdb_id"),
            lambda r: r.get("last_watched_at"),
            *sources,
        )
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.movie_menu_builder(trakt_list, no_paging=True, hide_watched=False)

    def watched_shows(self):
        trakt_rows = self.shows_database.get_recently_watched_shows(force_all=True) if self._trakt_active() else []

        sources = []
        if self._mdblist_active():
            mdb_rows = self.mdblist_menus.mdblist_database.get_recent_shows(self.page_limit, force_all=True)
            mdb_ts_by_tmdb = {row["tmdb_id"]: row["last_watched_at"] for row in mdb_rows}
            sources.append((
                self.mdblist_menus._resolve([row["tmdb_id"] for row in mdb_rows], "show"),
                lambda obj: obj.get("tmdb_id"),
                mdb_ts_by_tmdb,
            ))

        if self._simkl_active():
            simkl_rows = self.simkl_menus.simkl_database.get_recent_shows(self.page_limit, force_all=True)
            simkl_ts_by_tmdb = {row["tmdb_id"]: row["last_watched_at"] for row in simkl_rows}
            sources.append((
                self.simkl_menus._resolve([row["tmdb_id"] for row in simkl_rows], "show"),
                lambda obj: obj.get("tmdb_id"),
                simkl_ts_by_tmdb,
            ))

        trakt_list = self._merge_by_key(
            trakt_rows,
            lambda r: r.get("tmdb_id"),
            lambda r: r.get("lw"),
            *sources,
        )
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.show_list_builder(trakt_list, no_paging=True, hide_watched=False)

    def in_progress_movies(self):
        trakt_rows = self.bookmark_database.get_all_bookmark_items("movie") if self._trakt_active() else []

        sources = []
        if self._mdblist_active():
            mdb_rows = self.mdblist_menus._live_playback_rows("movie")
            mdb_ts_by_tmdb = {row["tmdb_id"]: row.get("paused_at") for row in mdb_rows}
            sources.append((
                self.mdblist_menus._resolve([row["tmdb_id"] for row in mdb_rows], "movie"),
                lambda obj: obj.get("tmdb_id"),
                mdb_ts_by_tmdb,
            ))

        if self._simkl_active():
            simkl_rows = self.simkl_menus.simkl_database.get_bookmarks("movie")
            simkl_ts_by_tmdb = {row["tmdb_id"]: row.get("paused_at") for row in simkl_rows}
            sources.append((
                self.simkl_menus._resolve([row["tmdb_id"] for row in simkl_rows], "movie"),
                lambda obj: obj.get("tmdb_id"),
                simkl_ts_by_tmdb,
            ))

        trakt_list = self._merge_by_key(
            trakt_rows,
            lambda r: r.get("tmdb_id"),
            lambda r: r.get("paused_at"),
            *sources,
        )
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.movie_menu_builder(trakt_list, no_paging=True)

    def in_progress_episodes(self):
        """One row per show (Trakt's own convention). When multiple sources have
        an in-progress episode for the same show, keeps whichever has the most
        recent paused_at, discarding the others entirely (not just deduping
        identical episodes - genuinely different episodes of the same show
        collapse too, per user's confirmed decision). Ties keep whichever source
        was folded in first (Trakt, then MDBList, then Simkl - existing[0] >= ts).
        Shows hidden from progress (Trakt's own on_deck_shows filter) are excluded
        from every source here too. Each source is folded in only when its own
        _trakt_active()/_mdblist_active()/_simkl_active() check passes - see class
        docstring. Hand-rolled per-source rather than routed through
        _merge_by_key, matching this method's existing (pre-Simkl) style."""
        from resources.lib.database.trakt_sync.shows import TraktSyncDatabase as ShowsDatabase

        hidden_shows = self.hidden_database.get_hidden_items("progress_watched", "tvshow")
        merged = {}

        if self._trakt_active():
            trakt_rows = self.bookmark_database.get_all_bookmark_items("episode")
            for r in trakt_rows:
                show_id = r.get("trakt_show_id")
                if show_id is None or show_id in hidden_shows:
                    continue
                merged[show_id] = (r.get("paused_at") or "", r)

        shows_db = ShowsDatabase()
        if self._mdblist_active():
            for row in self.mdblist_menus._live_playback_rows("episode"):
                trakt_show = self.mdblist_menus.trakt_api.search_by_tmdb_id(row["tmdb_id"], "show")
                if not trakt_show or not trakt_show.get("trakt_id"):
                    continue
                show_id = trakt_show["trakt_id"]
                if show_id in hidden_shows:
                    continue
                ts = row.get("paused_at") or ""
                existing = merged.get(show_id)
                if existing is not None and existing[0] >= ts:
                    continue
                episode = shows_db.get_episode_action_args(show_id, row["season"], row["number"])
                if not episode:
                    continue
                merged[show_id] = (ts, episode)

        if self._simkl_active():
            for row in self.simkl_menus.simkl_database.get_bookmarks("episode"):
                trakt_show = self.simkl_menus.trakt_api.search_by_tmdb_id(row["tmdb_id"], "show")
                if not trakt_show or not trakt_show.get("trakt_id"):
                    continue
                show_id = trakt_show["trakt_id"]
                if show_id in hidden_shows:
                    continue
                ts = row.get("paused_at") or ""
                existing = merged.get(show_id)
                if existing is not None and existing[0] >= ts:
                    continue
                episode = shows_db.get_episode_action_args(show_id, row["season"], row["number"])
                if not episode:
                    continue
                merged[show_id] = (ts, episode)

        episodes = [row for _, row in sorted(merged.values(), key=lambda pair: pair[0], reverse=True)]
        if not episodes:
            g.cancel_directory()
            return
        self.list_builder.mixed_episode_builder(episodes, no_paging=True)
