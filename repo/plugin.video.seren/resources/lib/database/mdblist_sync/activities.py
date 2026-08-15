from functools import cached_property

import xbmc

from resources.lib.database import mdblist_sync
from resources.lib.modules.exceptions import ActivitySyncFailure
from resources.lib.modules.global_lock import GlobalLock
from resources.lib.modules.globals import g


class MDBListSyncDatabase(mdblist_sync.MDBListSyncDatabase):
    def __init__(self):
        super().__init__()

    @cached_property
    def mdblist_api(self):
        from resources.lib.indexers.mdblist import MDBListAPI

        return MDBListAPI()

    def _update_activity_record(self, record, value):
        self.execute_sql(f"UPDATE activities SET {record}=? WHERE sync_id=1", (value,))

    def clear_last_sync(self):
        """Clear-button counterpart to force_sync(). Notifies here rather than in
        set_base_activities() itself, since force_sync() also calls that as its
        first step - notifying there would fire a misleading "cleared" toast on
        every Force Sync click too."""
        self.set_base_activities()
        g.notification(g.ADDON_NAME, g.get_language_string(31121))

    def force_sync(self):
        """Resets the sync cursor to base_date and re-runs a full sync. Uses
        set_base_activities() rather than a destructive table wipe - movies and
        episodes are both fully repopulated by the subsequent sync_activities()
        call (see _sync_watched()), so a direct wipe here would be redundant,
        not safer."""
        from resources.lib.database.trakt_sync import clear_list_cache

        self.set_base_activities()
        clear_list_cache()
        self.sync_activities()

    def sync_activities(self):
        with GlobalLock("mdblist.sync"):
            if not g.get_bool_setting("mdblist.enabled") or not (g.get_setting("mdblist.auth") or g.get_setting("mdblist.apikey")):
                g.log("MDBListSync: MDBList disabled or no auth present, no sync will occur", "warning")
                return

            self.refresh_activities()
            remote_activities = self.mdblist_api.get_json("sync/last_activities")
            if not remote_activities:
                g.log("MDBList Activities Sync Failure: unable to connect to MDBList", "error")
                return True

            update_time = self._get_datetime_now()
            watched_count = 0
            playback_count = 0

            watched_stale = self.requires_update(
                remote_activities.get("watched_at") or self.base_date, self.activities["watched_at"]
            )
            episode_watched_stale = self.requires_update(
                remote_activities.get("episode_watched_at") or self.base_date,
                self.activities["episode_watched_at"],
            )
            if watched_stale or episode_watched_stale:
                try:
                    watched_count = self._sync_watched()
                    self._update_activity_record("watched_at", update_time)
                    self._update_activity_record("episode_watched_at", update_time)
                except ActivitySyncFailure as e:
                    g.log(f"Failed to sync MDBList watched status: {e}", "error")

            playback_remote = max(
                remote_activities.get("paused_at") or self.base_date,
                remote_activities.get("episode_paused_at") or self.base_date,
            )
            if self.requires_update(playback_remote, self.activities["playback_at"]):
                try:
                    playback_count = self._sync_playback()
                    self._update_activity_record("playback_at", update_time)
                except ActivitySyncFailure as e:
                    g.log(f"Failed to sync MDBList playback: {e}", "error")

            if watched_count or playback_count:
                g.notification(g.ADDON_NAME, g.get_language_string(30953).format(watched_count, playback_count))
                xbmc.executebuiltin('RunPlugin("plugin://plugin.video.seren/?action=widgetRefresh&playing=False")')

    def _sync_watched(self):
        """Pulls GET /sync/watched (offset/limit-paginated per mdblist.apib - the
        response's pagination object has no cursor/next_cursor field, only
        offset/limit/has_more; a prior version of this method assumed a cursor
        that never existed, so pagination always silently stopped after page 1).

        Every page is collected into memory first; movies and episodes are
        each reset and repopulated as a unit only once pagination has cleanly
        finished (has_more went false). A page fetch failing mid-loop raises
        ActivitySyncFailure before any table is touched, instead of leaving
        either table wiped with only a partial repopulate.

        Movies were previously always empty due to a nesting bug (reading
        m["ids"]["tmdb"] instead of m["movie"]["ids"]["tmdb"], which meant
        every real entry failed its own tmdb-id filter) - now fixed. Live-
        confirmed 2026-07-18 against a real account (417 movies returned,
        correctly nested) that MDBList's API does populate this endpoint's
        movies list, so - like episodes - movies are now reset before
        repopulating: this is a complete paginated fetch of current server
        state, not a delta, so a clean-finish reset+repopulate reflects
        reality, and it makes a movie removed/unmarked on MDBList (or a
        cross_sync push whose local write got skipped, see cross_sync.py)
        self-healing on the next sync instead of a permanent local phantom.
        A locally-authored write (write_watched_locally(), scrobbler/context-
        menu) can be transiently overwritten if a full sync lands in the
        narrow window before MDBList's backend surfaces that same write back
        through this endpoint; that's a bounded, self-correcting risk (next
        sync repopulates it once MDBList catches up), the same risk profile
        already accepted for Simkl's removal reconciliation."""
        movie_rows = []
        episode_rows = []
        offset = 0
        limit = 100
        try:
            for _ in range(500):  # safety cap well above any realistic page count
                page = self.mdblist_api.get_json("sync/watched", offset=offset, limit=limit)
                if page is None:
                    raise ActivitySyncFailure("empty response from MDBList (sync/watched)")

                movies = page.get("movies") or []
                episodes = page.get("episodes") or []

                movie_rows.extend(
                    (m["movie"]["ids"]["tmdb"], m["movie"]["ids"].get("imdb"), m.get("last_watched_at"))
                    for m in movies
                    if m.get("movie", {}).get("ids", {}).get("tmdb")
                )
                episode_rows.extend(
                    (
                        e["episode"]["show"]["ids"]["tmdb"],
                        e["episode"]["season"],
                        e["episode"]["number"],
                        e.get("last_watched_at"),
                    )
                    for e in episodes
                    if e.get("episode", {}).get("show", {}).get("ids", {}).get("tmdb")
                )

                pagination = page.get("pagination") or {}
                if not pagination.get("has_more"):
                    break
                offset += limit

            self.execute_sql("UPDATE episodes SET watched=0")
            if episode_rows:
                self.execute_sql(
                    "REPLACE INTO episodes (show_tmdb_id, season, number, watched, last_watched_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    episode_rows,
                )
            self.execute_sql("UPDATE movies SET watched=0")
            if movie_rows:
                self.execute_sql(
                    "REPLACE INTO movies (tmdb_id, imdb_id, watched, last_watched_at) VALUES (?, ?, 1, ?)",
                    movie_rows,
                )
        except ActivitySyncFailure:
            raise
        except Exception as e:
            raise ActivitySyncFailure(e) from e
        return len(movie_rows) + len(episode_rows)

    def _sync_playback(self):
        """Pulls GET /sync/playback (flat array, not paginated per the
        live-confirmed response for accounts of this size). ids block uses
        key "tmdb" for both movie and show entries - same convention as
        sync/watched, live-confirmed 2026-07-18 (a prior fix here assumed
        "tmdbid" per a spec reading that turned out not to match the live
        API; that would have silently zeroed every bookmark row)."""
        try:
            entries = self.mdblist_api.get_json("sync/playback") or []
            if isinstance(entries, dict):
                entries = entries.get("items") or []

            rows = []
            for entry in entries:
                progress = entry.get("progress")
                runtime = entry.get("runtime")
                if progress is None or runtime is None:
                    continue
                try:
                    percent = float(progress)
                    resume_time = round(percent / 100 * float(runtime) * 60)
                except (TypeError, ValueError):
                    continue
                paused_at = entry.get("paused_at")
                media_type = entry.get("type")

                if media_type == "movie":
                    movie = entry.get("movie") or {}
                    tmdb_id = movie.get("ids", {}).get("tmdb")
                    if not tmdb_id:
                        continue
                    rows.append((tmdb_id, None, None, "movie", str(percent), str(resume_time), paused_at))
                elif media_type == "episode":
                    episode = entry.get("episode") or {}
                    show = entry.get("show") or {}
                    tmdb_id = show.get("ids", {}).get("tmdb")
                    season = episode.get("season")
                    number = episode.get("number")
                    if not tmdb_id or season is None or number is None:
                        continue
                    rows.append((tmdb_id, season, number, "episode", str(percent), str(resume_time), paused_at))

            self.execute_sql("DELETE FROM bookmarks")
            if rows:
                self.execute_sql(
                    "INSERT INTO bookmarks (tmdb_id, season, number, media_type, percent_played, "
                    "resume_time, paused_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    rows,
                )
            return len(rows)
        except Exception as e:
            raise ActivitySyncFailure(e) from e
