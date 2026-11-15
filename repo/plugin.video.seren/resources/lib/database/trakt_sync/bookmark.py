from resources.lib.database import trakt_sync


class TraktSyncDatabase(trakt_sync.TraktSyncDatabase):
    def get_bookmark(self, trakt_id):
        return self.fetchone("SELECT * FROM bookmarks WHERE trakt_id=?", (trakt_id,))

    def set_bookmark(self, trakt_id, time_in_seconds, media_type, percent_played):
        paused_at = self._get_datetime_now()
        self.execute_sql(
            "REPLACE INTO bookmarks Values (?, ?, ?, ?, ?)",
            (trakt_id, time_in_seconds, percent_played, media_type, paused_at),
        )

    def remove_bookmark(self, trakt_id):
        self.execute_sql("DELETE FROM bookmarks WHERE trakt_id=?", (trakt_id,))

    def get_all_bookmark_items(self, mediatype):
        if mediatype == "episode":
            query = """
                SELECT ep.trakt_show_id   AS trakt_show_id,
                       bm.trakt_id        AS trakt_id,
                       ep.trakt_season_id AS trakt_season_id,
                       bm.resume_time     AS progress,
                       bm.paused_at       AS paused_at,
                       em.value           AS episode,
                       sm.value           AS show
                FROM bookmarks AS bm
                         INNER JOIN episodes AS ep
                                    ON bm.trakt_id = ep.trakt_id
                         INNER JOIN episodes_meta AS em
                                    ON ep.trakt_id = em.id AND em.type = 'trakt'
                         LEFT JOIN shows_meta AS sm
                                   ON ep.trakt_show_id = sm.id AND sm.type = 'trakt'
                WHERE bm.type = 'episode'
                GROUP BY ep.trakt_show_id
                ORDER BY Datetime(bm.paused_at) DESC
                """
        else:
            query = """
                SELECT bm.trakt_id,
                       m.tmdb_id      AS tmdb_id,
                       bm.resume_time AS progress,
                       bm.paused_at   AS paused_at,
                       mm.value       AS trakt_object
                FROM bookmarks AS bm
                         LEFT JOIN movies_meta AS mm
                                   ON bm.trakt_id = mm.id
                         LEFT JOIN movies AS m
                                   ON bm.trakt_id = m.trakt_id
                WHERE bm.type = 'movie'
                ORDER BY bm.paused_at DESC
                """

        return self.wrap_in_trakt_object(self.fetchall(query))


    def refresh_bookmark_from_trakt(self, trakt_id, mediatype):
        """Pull ONE item's resume point from Trakt into the local bookmarks table (Seren 3.9.129/3.9.130, see modules/resumeRefresh.py).

        The periodic activity sync can be up to ~15 min behind a pause made on another device. Reads sync/playback (Trakt lists
        the most recent pauses first; the first page of 50 holds anything paused recently, later pages are read, up to
        _PLAYBACK_MAX_PAGES, only to prove an item is NOT listed), stores this item's row when it is newer than the local row and,
        when Trakt's complete answer has no usable resume point for it (finished or cleared on another device), drops the stale
        local row, as the periodic sync would. Holds no lock and changes one row only, so it is safe to run beside a sync and to
        outlive the caller's deadline. Returns True when the local row was added or replaced; any failure leaves the table as it is.
        """
        from resources.lib.modules.globals import g
        from resources.lib.modules.metadataHandler import MetadataHandler

        if not trakt_id or not g.get_setting("trakt.auth") or not g.get_bool_setting("trakt.enabled", False):
            return False
        bookmark_type = "movies" if mediatype == "movie" else "episodes"
        url = f"sync/playback/{bookmark_type}"
        get = MetadataHandler.get_trakt_info
        row = None
        complete = False
        try:
            page, page_count = self.trakt_api.get_page_json(url, extended="full", timeout=4, limit=50, page=1)
            if page is None:
                g.log("Resume: Trakt resume point not refreshed (no answer)", "info")
                return False
            number = 1
            while True:
                row = next((i for i in page if str(i.get("trakt_id")) == str(trakt_id)), None)
                if row is not None or number >= page_count:
                    break
                if number >= self._PLAYBACK_MAX_PAGES:
                    break  # too long a list to prove the item is missing: leave the local row as it is
                number += 1
                page, _ = self.trakt_api.get_page_json(url, extended="full", timeout=4, limit=50, page=number)
                if page is None:
                    g.log("Resume: Trakt resume point not refreshed (a later page failed)", "info")
                    return False
            complete = number >= page_count
        except Exception as e:
            g.log(f"Resume: Trakt resume point not refreshed ({e})", "info")
            return False

        usable = False
        if row is not None:
            if mediatype == "movie":
                percent, duration, paused_at = get(row, "percentplayed"), get(row, "duration"), get(row, "paused_at")
            else:
                percent, duration, paused_at = row.get("percentplayed"), get(row["episode"], "duration"), row.get("paused_at")
            usable = bool(percent and 0 < percent < 100 and duration and paused_at)

        local = self.get_bookmark(trakt_id)
        if not usable:
            # Trakt has no resume point for this item any more (or lists it unusable) = the periodic sync would not keep the local row.
            # Only a COMPLETE answer proves "not listed", and a row written here in the last two minutes may simply not have
            # reached Trakt yet (scrobble in flight), so that one stays.
            if local and (row is not None or complete) and not self._written_just_now(local["paused_at"]):
                self.remove_bookmark(trakt_id)
                g.log(f"Resume: Trakt no longer has a resume point for {trakt_id}, dropped the local copy", "info")
            return False

        if local and local["paused_at"] and len(local["paused_at"]) == len(paused_at) and local["paused_at"] >= paused_at:
            return False  # the local row is as new or newer (a stop made here that Trakt has not shown yet)
        self.execute_sql(
            "REPLACE INTO bookmarks VALUES (?, ?, ?, ?, ?)",
            (trakt_id, int(float(percent / 100) * duration), percent, mediatype, paused_at),
        )
        g.log(f"Resume: took the newer Trakt resume point for {trakt_id} ({percent:.1f}%)", "info")
        return True

    _PLAYBACK_MAX_PAGES = 6  # 300 paused items: more than that and an item missing from the pages read is not proven gone

    @staticmethod
    def _written_just_now(paused_at):
        """True when the local row's UTC paused_at is under two minutes old (written by a stop on this device)."""
        import datetime

        try:
            written = datetime.datetime.strptime(str(paused_at)[:19], "%Y-%m-%dT%H:%M:%S")
            age = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - written
            return age < datetime.timedelta(minutes=2)
        except (TypeError, ValueError):
            return False
