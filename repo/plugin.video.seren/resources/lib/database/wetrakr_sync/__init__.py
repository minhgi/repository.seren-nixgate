import collections

import xbmcgui

from resources.lib.database import Database
from resources.lib.modules.globals import g

# Seren's local copy of what WeTrakr holds for the signed-in account (Seren 3.9.168). WeTrakr resolves every title by TMDb id and numbers
# episodes the TMDb way, so no other id or numbering check is needed (unlike Simkl). The sync stamps live in this database's own
# activities table, so a rebuild of the tables and the stamps always happen together: stamps that outlived emptied tables would say
# "nothing new" for ever.
EPOCH = ""  # no stamp stored yet: every real stamp differs from it, so the first sync reads everything
NO_SEASON = -1  # a movie's season / number in the bookmarks table (the primary key cannot hold NULLs)

schema = {
    "movies": {
        "columns": collections.OrderedDict(
            [
                ("tmdb_id", ["INTEGER", "PRIMARY KEY", "NOT NULL"]),
                ("plays", ["INTEGER", "NOT NULL", "DEFAULT 1"]),
                ("last_watched_at", ["TEXT", "NULL"]),
            ]
        ),
        "table_constraints": [],
        "indices": [],
        "default_seed": [],
    },
    "episodes": {
        "columns": collections.OrderedDict(
            [
                ("tmdb_id", ["INTEGER", "NOT NULL"]),  # the SHOW's TMDb id
                ("season", ["INTEGER", "NOT NULL"]),
                ("number", ["INTEGER", "NOT NULL"]),
                ("plays", ["INTEGER", "NOT NULL", "DEFAULT 1"]),
                ("last_watched_at", ["TEXT", "NULL"]),
                ("wt_show_id", ["INTEGER", "NULL"]),  # WeTrakr's own id of the show (3.9.177): the journal names an episode by it, not by the TMDb id
            ]
        ),
        "table_constraints": ["PRIMARY KEY(tmdb_id, season, number)"],
        "indices": [("idx_wetrakr_episodes_show", ["tmdb_id"]), ("idx_wetrakr_episodes_wt_show", ["wt_show_id"])],
        "default_seed": [],
    },
    "bookmarks": {
        "columns": collections.OrderedDict(
            [
                ("media_type", ["TEXT", "NOT NULL"]),  # 'movie' or 'episode'
                ("tmdb_id", ["INTEGER", "NOT NULL"]),  # the movie's, or the show's
                ("season", ["INTEGER", "NOT NULL", "DEFAULT -1"]),
                ("number", ["INTEGER", "NOT NULL", "DEFAULT -1"]),
                ("percent_played", ["TEXT", "NOT NULL"]),
                ("paused_at", ["TEXT", "NOT NULL"]),
            ]
        ),
        "table_constraints": ["PRIMARY KEY(media_type, tmdb_id, season, number)"],
        "indices": [("idx_wetrakr_bookmarks_paused", ["paused_at"])],
        "default_seed": [],
    },
    "ratings": {
        "columns": collections.OrderedDict(
            [
                ("media_type", ["TEXT", "NOT NULL"]),  # 'movie', 'show', 'season' or 'episode' (3.9.178)
                ("tmdb_id", ["INTEGER", "NOT NULL"]),  # the movie's, or the SHOW's
                ("season", ["INTEGER", "NOT NULL", "DEFAULT -1"]),
                ("number", ["INTEGER", "NOT NULL", "DEFAULT -1"]),
                ("rating", ["REAL", "NOT NULL"]),  # 0 < rating <= 10, one decimal kept (8.5)
                ("rated_at", ["TEXT", "NULL"]),
            ]
        ),
        "table_constraints": ["PRIMARY KEY(media_type, tmdb_id, season, number)"],
        "indices": [],
        "default_seed": [],
    },
    "activities": {
        "columns": collections.OrderedDict(
            [
                ("sync_id", ["INTEGER", "PRIMARY KEY"]),
                ("watched_at", ["TEXT", "NOT NULL", "DEFAULT ''"]),  # newest watched / removed stamp WeTrakr had when the lists were last read
                ("playback_at", ["TEXT", "NOT NULL", "DEFAULT ''"]),  # the same for the resume points
                ("journal_at", ["TEXT", "NOT NULL", "DEFAULT ''"]),  # 3.9.177: how far the watched marks are up to date from the journal ('' = read the lists in full)
                ("ratings_at", ["TEXT", "NOT NULL", "DEFAULT ''"]),  # 3.9.178: the account's ratings stamp when the ratings were last read ('' = read them in full)
            ]
        ),
        "table_constraints": ["UNIQUE(sync_id)"],
        "default_seed": [],
    },
}


class WeTrakrSyncDatabase(Database):
    def __init__(self):
        super().__init__(g.WETRAKR_SYNC_DB_PATH, schema)

        self.activities = {}
        self.refresh_activities()

        if self.activities is None:
            self.set_base_activities()

    def refresh_activities(self):
        self.activities = self.fetchone("SELECT * FROM activities WHERE sync_id=1")

    def set_base_activities(self):
        """Resets the stamps (the journal mark and the ratings stamp fall back to their '' default): the next sync reads the watched marks, the
        resume points and the ratings in full"""
        self.execute_sql("REPLACE INTO activities(sync_id, watched_at, playback_at) VALUES(1, ?, ?)", (EPOCH, EPOCH))
        self.activities = self.fetchone("SELECT * FROM activities WHERE sync_id=1")

    def re_build_database(self, silent=False):
        """Physical drop and recreate of the tables (rebuild_database), then a full resync (force_sync)"""
        if not silent:
            confirm = xbmcgui.Dialog().yesno(g.ADDON_NAME, g.get_language_string(30179))
            if confirm == 0:
                return

        self.rebuild_database()

        from resources.lib.database.wetrakr_sync import activities

        activities.WeTrakrSyncDatabase().force_sync()

    def flush_activities(self):
        """Nothing of one account may survive into the next: empties the tables and resets the stamps (sign-in and logout)"""
        self.execute_sql("DELETE FROM movies")
        self.execute_sql("DELETE FROM episodes")
        self.execute_sql("DELETE FROM bookmarks")
        self.execute_sql("DELETE FROM ratings")
        self.execute_sql("DELETE FROM activities")
        self.set_base_activities()

    # region watched marks
    def is_movie_watched(self, tmdb_id):
        return bool(tmdb_id) and bool(self.fetchone("SELECT 1 FROM movies WHERE tmdb_id=?", (tmdb_id,)))

    def is_episode_watched(self, show_tmdb_id, season, number):
        return bool(show_tmdb_id) and bool(
            self.fetchone(
                "SELECT 1 FROM episodes WHERE tmdb_id=? AND season=? AND number=?", (show_tmdb_id, season, number)
            )
        )

    def mark_movie_watched_locally(self, tmdb_id, watched_at=None):
        if not tmdb_id:
            return
        self.execute_sql(
            "INSERT INTO movies(tmdb_id, plays, last_watched_at) VALUES(?, 1, ?) "
            "ON CONFLICT(tmdb_id) DO UPDATE SET last_watched_at=excluded.last_watched_at",
            (tmdb_id, watched_at),
        )

    def mark_episode_watched_locally(self, show_tmdb_id, season, number, watched_at=None):
        if not show_tmdb_id or season is None or number is None:
            return
        self.execute_sql(
            "INSERT INTO episodes(tmdb_id, season, number, plays, last_watched_at) VALUES(?, ?, ?, 1, ?) "
            "ON CONFLICT(tmdb_id, season, number) DO UPDATE SET last_watched_at=excluded.last_watched_at",
            (show_tmdb_id, season, number, watched_at),
        )

    def mark_movie_unwatched_locally(self, tmdb_id):
        if tmdb_id:
            self.execute_sql("DELETE FROM movies WHERE tmdb_id=?", (tmdb_id,))

    def mark_episode_unwatched_locally(self, show_tmdb_id, season, number):
        if show_tmdb_id and season is not None and number is not None:
            self.execute_sql(
                "DELETE FROM episodes WHERE tmdb_id=? AND season=? AND number=?", (show_tmdb_id, season, number)
            )

    def get_watched_episode_count(self, show_tmdb_id, season=None):
        """Watched episodes of a season, or of the whole show without specials (season 0): the count the context menu compares with the
        item's aired episode total (same convention as MDBListSyncDatabase.get_watched_episode_count)"""
        if not show_tmdb_id:
            return 0
        if season is not None:
            row = self.fetchone("SELECT COUNT(*) AS count FROM episodes WHERE tmdb_id=? AND season=?", (show_tmdb_id, season))
        else:
            row = self.fetchone("SELECT COUNT(*) AS count FROM episodes WHERE tmdb_id=? AND season>0", (show_tmdb_id,))
        return row["count"] if row else 0

    def reset_watched_stamp(self):
        """Forget the saved watched stamp only: the next sync reads the watched lists whatever WeTrakr's own stamp says (after a write of ours).
        The journal mark goes too: the journal shows a change only a few seconds after it was made, so an incremental read right after our own
        write would not see it; the next read is a full one (it saves a new mark)."""
        self.execute_sql("UPDATE activities SET watched_at=?, journal_at=? WHERE sync_id=1", (EPOCH, EPOCH))
        self.refresh_activities()

    def reset_ratings_stamp(self):
        """Forget the saved ratings stamp only: the next sync reads the whole ratings list again (used when WeTrakr answered a rating write with
        'already', which does not say which value it holds, so the local copy is made to match it by a full read)"""
        self.execute_sql("UPDATE activities SET ratings_at=? WHERE sync_id=1", (EPOCH,))
        self.refresh_activities()

    def show_tmdb_of(self, wt_show_id):
        """The TMDb id of the show whose WeTrakr id is wt_show_id, from any stored episode of it; None when no stored row knows it"""
        row = self.fetchone("SELECT tmdb_id FROM episodes WHERE wt_show_id=? LIMIT 1", (wt_show_id,))
        return row["tmdb_id"] if row else None

    # endregion

    # region ratings
    @staticmethod
    def _rating_key(season, number):
        return (NO_SEASON if season is None else season), (NO_SEASON if number is None else number)

    def get_rating(self, media_type, tmdb_id, season=None, number=None):
        """The account's rating of a movie / show / season / episode from the local copy (a float), or None; a show, season or episode is
        addressed by the SHOW's TMDb id"""
        if not tmdb_id or media_type not in ("movie", "show", "season", "episode"):
            return None
        season, number = self._rating_key(season, number)
        row = self.fetchone(
            "SELECT rating FROM ratings WHERE media_type=? AND tmdb_id=? AND season=? AND number=?", (media_type, tmdb_id, season, number)
        )
        return row["rating"] if row else None

    def set_rating_locally(self, media_type, tmdb_id, season, number, rating, rated_at=None):
        """One rating in the local copy at once (after a write of ours); replaces the item's row"""
        if not tmdb_id or media_type not in ("movie", "show", "season", "episode") or not rating:
            return False
        season, number = self._rating_key(season, number)
        self.execute_sql(
            "REPLACE INTO ratings(media_type, tmdb_id, season, number, rating, rated_at) VALUES(?, ?, ?, ?, ?, ?)",
            (media_type, tmdb_id, season, number, float(rating), rated_at),
        )
        return True

    def remove_rating_locally(self, media_type, tmdb_id, season=None, number=None):
        if not tmdb_id or media_type not in ("movie", "show", "season", "episode"):
            return
        season, number = self._rating_key(season, number)
        self.execute_sql(
            "DELETE FROM ratings WHERE media_type=? AND tmdb_id=? AND season=? AND number=?", (media_type, tmdb_id, season, number)
        )

    # endregion

    # region resume points
    @staticmethod
    def _bookmark_key(media_type, season, number):
        if media_type == "movie":
            return NO_SEASON, NO_SEASON
        return (NO_SEASON if season is None else season), (NO_SEASON if number is None else number)

    def get_bookmark(self, media_type, tmdb_id, season=None, number=None):
        season, number = self._bookmark_key(media_type, season, number)
        return self.fetchone(
            "SELECT percent_played, paused_at FROM bookmarks WHERE media_type=? AND tmdb_id=? AND season=? AND number=?",
            (media_type, tmdb_id, season, number),
        )

    def set_bookmark(self, media_type, tmdb_id, season, number, percent, paused_at):
        """One resume point (percentage, UTC paused_at 'YYYY-MM-DDTHH:MM:SS...'); replaces the item's row"""
        if not tmdb_id or media_type not in ("movie", "episode"):
            return False
        season, number = self._bookmark_key(media_type, season, number)
        self.execute_sql(
            "REPLACE INTO bookmarks(media_type, tmdb_id, season, number, percent_played, paused_at) VALUES(?, ?, ?, ?, ?, ?)",
            (media_type, tmdb_id, season, number, str(percent), paused_at),
        )
        return True

    def remove_bookmark(self, media_type, tmdb_id, season=None, number=None):
        season, number = self._bookmark_key(media_type, season, number)
        self.execute_sql(
            "DELETE FROM bookmarks WHERE media_type=? AND tmdb_id=? AND season=? AND number=?",
            (media_type, tmdb_id, season, number),
        )

    def get_bookmarks(self, media_type):
        """Every resume point of one kind, newest pause first: rows with tmdb_id (the movie's, or the show's), season and number (the
        menus resolve an episode row as it is)"""
        if media_type not in ("movie", "episode"):
            return []
        return self.fetchall(
            "SELECT tmdb_id, season, number, percent_played, paused_at FROM bookmarks WHERE media_type=? ORDER BY paused_at DESC",
            (media_type,),
        )

    # endregion

    # region recently watched
    def get_recent_movies(self, limit, offset=0, force_all=False):
        """Watched movies, newest play first (force_all: every row, no limit - the Merge menus, like Simkl's and MDBList's)"""
        query = "SELECT tmdb_id, last_watched_at FROM movies ORDER BY last_watched_at DESC, tmdb_id DESC"
        if force_all:
            return self.fetchall(query)
        return self.fetchall(query + " LIMIT ? OFFSET ?", (limit, offset))

    def get_recent_shows(self, limit, force_all=False):
        """Shows with at least one watched episode, the show whose latest play is newest first (force_all: every show, no limit)"""
        query = (
            "SELECT tmdb_id, MAX(last_watched_at) AS last_watched_at FROM episodes GROUP BY tmdb_id "
            "ORDER BY last_watched_at DESC, tmdb_id DESC"
        )
        if force_all:
            return self.fetchall(query)
        return self.fetchall(query + " LIMIT ?", (limit,))

    def get_watched_episodes(self, limit, offset=0):
        """Watched episodes (tmdb_id is the SHOW's), newest play first: the WeTrakr Watched Episodes menu (3.9.179)"""
        return self.fetchall(
            "SELECT tmdb_id, season, number, last_watched_at FROM episodes "
            "ORDER BY last_watched_at DESC, tmdb_id DESC, season DESC, number DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    def get_watched_episode_keys(self, show_tmdb_ids):
        """{(show tmdb id, season, number)} of the watched episodes of these shows, one query per 400 shows (3.9.180: the WeTrakr tick of the
        calendars)"""
        ids = [i for i in show_tmdb_ids if i]
        keys = set()
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            rows = self.fetchall(
                f"SELECT tmdb_id, season, number FROM episodes WHERE tmdb_id IN ({','.join('?' * len(chunk))})", tuple(chunk)
            )
            keys.update((row["tmdb_id"], row["season"], row["number"]) for row in rows)
        return keys

    # endregion
