from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import cached_property

from resources.lib.modules.globals import g


class Menus:
    """Simkl-sourced menus (Recently Watched, In Progress).

    Items are local simklSync.db rows (movies/shows/bookmarks, built Phase 3) keyed by
    tmdb_id. Each is resolved to a Trakt object via TraktAPI.search_by_tmdb_id (Trakt's
    ID lookup - no personal OAuth needed) before being handed to the existing,
    unmodified Trakt-native ListBuilder methods - identical reuse path to
    mdblistMenus.py's Menus class. Unlike that file, there is no My Lists/Discover
    section here: Simkl has no named-list concept (its "lists" are watch-status
    buckets - watching/completed/hold/dropped/plantowatch - handled via the context
    menu's status picker, not a browsable list-of-lists), so only the resolve/seed/
    render subset is ported.
    """

    def __init__(self):
        self.page_limit = g.get_int_setting("item.limit")

    @cached_property
    def simkl_database(self):
        from resources.lib.database.simkl_sync import SimklSyncDatabase

        return SimklSyncDatabase()

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
        """Resolves tmdb_ids to Trakt objects in parallel (mirrors
        mdblistMenus.py's Menus._resolve() exactly), dropping (and logging) any id
        Trakt has no match for - or that errors/times out - rather than failing the
        whole list. tmdb_ids is already NULL-filtered by the SimklSyncDatabase query
        methods that feed this (get_recent_movies/get_recent_shows/get_bookmarks all
        filter tmdb_id IS NOT NULL), so no None-guard is needed here."""
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
                g.log(f"Simkl menus: resolve batch timed out, using partial results: {e}", "warning")
                for future in futures:
                    if future.done():
                        resolved_item = self._collect_resolved(future, futures[future], media_type)
                        if resolved_item:
                            resolved.append(resolved_item)
        finally:
            # wait=False enforces the 20s as_completed ceiling rather than blocking on
            # shutdown - see mdblistMenus.py's identical finally block for the full
            # reasoning (already-submitted futures still finish in the background).
            executor.shutdown(wait=False)

        if resolved:
            self._seed_trakt_sync(resolved, media_type)
        return resolved

    @staticmethod
    def _seed_trakt_sync(resolved, media_type):
        """Writes freshly-resolved items into the local Trakt sync DB, identical to
        mdblistMenus.py's Menus._seed_trakt_sync() - see that method's docstring for
        why this is required before movie_menu_builder/show_list_builder can render
        them."""
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
            g.log(f"Simkl menus: error resolving tmdb_id {tmdb_id} ({media_type}): {e}", "warning")
            return None
        if not trakt_object or not trakt_object.get("trakt_id"):
            g.log(f"Simkl menus: no Trakt match for tmdb_id {tmdb_id} ({media_type}), skipping", "debug")
            return None
        return trakt_object

    def recent_movies(self):
        rows = self.simkl_database.get_recent_movies(self.page_limit)
        trakt_list = self._resolve([r["tmdb_id"] for r in rows], "movie")
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.movie_menu_builder(trakt_list, no_paging=True, hide_watched=False)

    def recent_shows(self):
        rows = self.simkl_database.get_recent_shows(self.page_limit)
        trakt_list = self._resolve([r["tmdb_id"] for r in rows], "show")
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.show_list_builder(trakt_list, no_paging=True, hide_watched=False)

    def in_progress_movies(self):
        rows = self.simkl_database.get_bookmarks("movie")
        trakt_list = self._resolve([r["tmdb_id"] for r in rows], "movie")
        if not trakt_list:
            g.cancel_directory()
            return
        self.list_builder.movie_menu_builder(trakt_list, no_paging=True)

    def in_progress_episodes(self):
        from resources.lib.database.trakt_sync.shows import TraktSyncDatabase as ShowsDatabase

        rows = self.simkl_database.get_bookmarks("episode")
        shows_db = ShowsDatabase()
        hidden_shows = self.hidden_database.get_hidden_items("progress_watched", "tvshow")
        episodes = []
        for row in rows:
            trakt_show = self.trakt_api.search_by_tmdb_id(row["tmdb_id"], "show")
            if not trakt_show or not trakt_show.get("trakt_id"):
                g.log(
                    f"Simkl menus: no Trakt match for tmdb_id {row['tmdb_id']} (show), "
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
