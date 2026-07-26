import collections
import datetime

from resources.lib.database import Database
from resources.lib.modules.globals import g

schema = {
    "movies": {
        "columns": collections.OrderedDict(
            [
                ("simkl_id", ["INTEGER", "PRIMARY KEY", "NOT NULL"]),
                ("tmdb_id", ["INTEGER", "NULL"]),
                ("imdb_id", ["TEXT", "NULL"]),
                ("watched", ["INTEGER", "NOT NULL", "DEFAULT 0"]),
                ("last_watched_at", ["TEXT", "NULL"]),
            ]
        ),
        "table_constraints": [],
        "indices": [
            ("idx_simkl_movies_tmdb", ["tmdb_id"]),
            ("idx_simkl_movies_watched", ["watched"]),
        ],
        "default_seed": [],
    },
    "shows": {
        "columns": collections.OrderedDict(
            [
                ("simkl_id", ["INTEGER", "PRIMARY KEY", "NOT NULL"]),
                ("tmdb_id", ["INTEGER", "NULL"]),
                ("tvdb_id", ["TEXT", "NULL"]),
                ("imdb_id", ["TEXT", "NULL"]),
                ("status", ["TEXT", "NULL"]),
                ("watched_episodes_count", ["INTEGER", "NOT NULL", "DEFAULT 0"]),
                ("total_episodes_count", ["INTEGER", "NOT NULL", "DEFAULT 0"]),
                ("next_to_watch", ["TEXT", "NULL"]),
                ("last_watched_at", ["TEXT", "NULL"]),
            ]
        ),
        "table_constraints": [],
        "indices": [
            ("idx_simkl_shows_tmdb", ["tmdb_id"]),
            ("idx_simkl_shows_last_watched", ["last_watched_at"]),
        ],
        "default_seed": [],
    },
    "bookmarks": {
        "columns": collections.OrderedDict(
            [
                ("simkl_id", ["INTEGER", "NOT NULL"]),
                ("season", ["INTEGER", "NULL"]),
                ("number", ["INTEGER", "NULL"]),
                ("media_type", ["TEXT", "NOT NULL"]),
                ("tmdb_id", ["INTEGER", "NULL"]),
                ("playback_id", ["INTEGER", "NULL"]),
                ("percent_played", ["TEXT", "NOT NULL"]),
                ("paused_at", ["TEXT", "NOT NULL"]),
            ]
        ),
        "table_constraints": ["PRIMARY KEY(simkl_id, season, number, media_type)"],
        "indices": [
            ("idx_simkl_bookmarks_paused", ["paused_at"]),
            ("idx_simkl_bookmarks_tmdb", ["tmdb_id"]),
        ],
        "default_seed": [],
    },
    "activities": {
        "columns": collections.OrderedDict(
            [
                ("sync_id", ["INTEGER", "PRIMARY KEY"]),
                ("all_at", ["TEXT", "NOT NULL", "DEFAULT '1970-01-01T00:00:00+00:00'"]),
                ("movies_completed_at", ["TEXT", "NOT NULL", "DEFAULT '1970-01-01T00:00:00+00:00'"]),
                ("tv_shows_completed_at", ["TEXT", "NOT NULL", "DEFAULT '1970-01-01T00:00:00+00:00'"]),
                ("tv_shows_watching_at", ["TEXT", "NOT NULL", "DEFAULT '1970-01-01T00:00:00+00:00'"]),
                ("playback_at", ["TEXT", "NOT NULL", "DEFAULT '1970-01-01T00:00:00+00:00'"]),
                ("simkl_username", ["TEXT", "NULL"]),
            ]
        ),
        "table_constraints": ["UNIQUE(sync_id)"],
        "default_seed": [],
    },
}


def _parse_simkl_datetime(string_date):
    """Simkl's cursor/item timestamps come back as either Z-suffixed or numeric-offset
    ISO8601 (both forms appear across simkl.apib) - normalized to tz-aware here so every
    comparison this module makes is apples-to-apples. Deliberately not
    tools.parse_datetime(), which rstrip("Z")'s the offset away entirely and returns a
    naive datetime - fine for Trakt/MDBList's bare timestamps, but comparing that against
    an aware datetime raises TypeError."""
    if not string_date:
        return None
    return datetime.datetime.fromisoformat(string_date.replace("Z", "+00:00"))


class SimklSyncDatabase(Database):
    def __init__(self):
        super().__init__(g.SIMKL_SYNC_DB_PATH, schema)

        self.activities = {}
        self.base_date = "1970-01-01T00:00:00+00:00"
        self.refresh_activities()

        if self.activities is None:
            self.set_base_activities()

    def refresh_activities(self):
        self.activities = self.fetchone("SELECT * FROM activities WHERE sync_id=1")

    def set_base_activities(self):
        self.execute_sql(
            "REPLACE INTO activities(sync_id, simkl_username) VALUES(1, ?)",
            (g.get_setting("simkl.username"),),
        )
        self.activities = self.fetchone("SELECT * FROM activities WHERE sync_id=1")
        # Removal-reconciliation cursors live in addon settings, not this table (see
        # _reconcile_removed_movies/_reconcile_removed_shows in activities.py) - adding
        # columns here would change this schema dict's checksum and trigger
        # Database._integrity_check_db's rebuild_database() on every existing install,
        # which wipes movies/shows/bookmarks for everyone, not just migrates activities.
        g.set_setting("simkl.movies_removed_at", self.base_date)
        g.set_setting("simkl.tv_shows_removed_at", self.base_date)
        # Anime has no dedicated activities-table columns at all (unlike movies/shows,
        # which get completed_at/watching_at columns plus a settings-based removed_at) -
        # all three of its cursors live in settings for the same schema-checksum reason.
        g.set_setting("simkl.anime_completed_at", self.base_date)
        g.set_setting("simkl.anime_watching_at", self.base_date)
        g.set_setting("simkl.anime_removed_at", self.base_date)

    def flush_activities(self):
        """Wipes local watch-state and resets sync cursors to base_date - called on a
        Simkl account switch (mirrors Trakt's flush_activities/clear_user_information,
        collapsed into one method since Simkl's tables hold only watch-state, not a
        browsable catalog cache like Trakt's - there's nothing worth preserving across
        a user switch)."""
        self.execute_sql("DELETE FROM movies")
        self.execute_sql("DELETE FROM shows")
        self.execute_sql("DELETE FROM bookmarks")
        self.execute_sql("DELETE FROM activities")
        self.set_base_activities()

    def set_simkl_user(self, simkl_username):
        g.log(f"Setting Simkl Username: {simkl_username}")
        self.execute_sql("UPDATE activities SET simkl_username=?", (simkl_username,))

    @staticmethod
    def _get_datetime_now():
        # Kept tz-aware (unlike g.datetime_to_string()'s naive output) so a fallback
        # stamp here never collides with a _parse_simkl_datetime() comparison elsewhere.
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    @staticmethod
    def requires_update(new_date, old_date):
        new_dt = _parse_simkl_datetime(new_date)
        old_dt = _parse_simkl_datetime(old_date)
        if new_dt is None:
            return False
        if old_dt is None:
            return True
        return new_dt > old_dt

    def get_recent_movies(self, limit, offset=0, force_all=False):
        """Locally-synced watched movies, newest first. tmdb_id IS NOT NULL: unlike
        MDBList's tmdb-keyed tables, Simkl rows aren't guaranteed a tmdb_id (Phase 3
        confirmed it's nullable and sometimes absent) - excluded here rather than
        reaching Menus._resolve() and wasting a search/tmdb/None call."""
        query = """
            SELECT simkl_id, tmdb_id, last_watched_at
            FROM movies
            WHERE watched = 1 AND tmdb_id IS NOT NULL
            ORDER BY last_watched_at DESC
            """
        if not force_all:
            query += f" LIMIT {limit} OFFSET {offset}"
        return self.fetchall(query)

    def get_recent_shows(self, limit, offset=0, force_all=False):
        """Locally-synced shows with watched progress, newest first - ordered by
        Simkl's own per-show last_watched_at aggregate (no per-episode rows to MAX()
        over; see Simkl_Implementation_Plan.md Phase 3 notes on aggregate-only
        tracking). tmdb_id IS NOT NULL: see get_recent_movies."""
        query = """
            SELECT simkl_id, tmdb_id, last_watched_at
            FROM shows
            WHERE watched_episodes_count > 0 AND tmdb_id IS NOT NULL
            ORDER BY last_watched_at DESC
            """
        if not force_all:
            query += f" LIMIT {limit} OFFSET {offset}"
        return self.fetchall(query)

    def get_bookmarks(self, media_type):
        """Locally-synced paused/in-progress sessions for Phase 4's In Progress menus.
        Reads Phase 3's bookmarks table directly (kept current via _sync_playback's
        full-snapshot replace on every sync_activities() cycle) rather than a live API
        call - unlike MDBList's _live_playback_rows(), which fetches live because its
        own local cache only updates on scrobble events and can go stale between syncs.
        Simkl has no such gap here, so the already-synced local table is authoritative.
        tmdb_id IS NOT NULL: see get_recent_movies."""
        query = """
            SELECT tmdb_id, season, number, playback_id, paused_at
            FROM bookmarks
            WHERE media_type = ? AND tmdb_id IS NOT NULL
            ORDER BY paused_at DESC
            """
        return self.fetchall(query, (media_type,))

    def remove_bookmark(self, tmdb_id, media_type, season=None, number=None):
        """Deletes a local bookmark row by tmdb_id rather than the table's own
        simkl_id-based PK - context-menu callers only have a tmdb_id (items reach the
        context menu as Trakt objects). Not strictly required for correctness - the
        next sync_activities() cycle's _sync_playback() full-snapshot replace would
        eventually reconcile this anyway - but deleting immediately avoids a
        just-cleared item lingering in the In Progress menu until the next sync."""
        if media_type == "movie":
            self.execute_sql("DELETE FROM bookmarks WHERE tmdb_id=? AND media_type='movie'", (tmdb_id,))
        else:
            self.execute_sql(
                "DELETE FROM bookmarks WHERE tmdb_id=? AND season=? AND number=? AND media_type='episode'",
                (tmdb_id, season, number),
            )
