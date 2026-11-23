from functools import cached_property

from resources.lib.database import wetrakr_sync
from resources.lib.modules.exceptions import ActivitySyncFailure
from resources.lib.modules.global_lock import GlobalLock
from resources.lib.modules.globals import g

NO_ACTIVITY = "2000-01-01T00:00:00.000Z"  # the stamp of an account with nothing to sync
WATCHED_KEYS = ("last_tracking_watched_at", "last_tracking_removed_at")  # a play added / a play removed (the only stamp an unwatch moves)
PLAYBACK_KEYS = ("last_playback_at",)  # a resume point started, moved or finished
RATINGS_KEYS = ("last_updated_at", "last_removed_at")  # a rating added / changed, a rating removed: the account-level `ratings` object
KINDS = ("movies", "shows", "episodes")
PLAY_TIME_TIMEOUT = 4  # seconds for the one request made at play start (like Simkl's)
OVERLAP = 1  # seconds an incremental read starts before the saved mark: an entry read twice is harmless (every write is idempotent), a gap is not
MOVIES_PATH = "/sync/tracking/watched/movies"
EPISODES_PATH = "/sync/tracking/watched/episodes"
RATINGS_PATH = "/sync/ratings/all"


def shift_iso(value, seconds):
    """An ISO 8601 stamp of WeTrakr ("2026-11-23T00:54:53.173Z") moved by some seconds, in the same form; None when it cannot be read"""
    import datetime

    try:
        text = f"{value}".strip()
        millis = int((text[20:23] if text[19:20] == "." else "0").ljust(3, "0"))
        moment = datetime.datetime(
            int(text[0:4]), int(text[5:7]), int(text[8:10]), int(text[11:13]), int(text[14:16]), int(text[17:19]), millis * 1000
        ) + datetime.timedelta(seconds=seconds)
    except (ValueError, TypeError):
        return None
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


def journal_plan(entries):
    """What the journal entries say: {"movies": bool, "episodes": bool, "removed_movies": {tmdb}, "removed_episodes": [(show wt id, season, number)]},
    or None when something cannot be placed (a removal with no ids, a removed season / show no episode entry explains): the lists are then read
    in full"""
    plan = {"movies": False, "episodes": False, "removed_movies": set(), "removed_episodes": []}
    bulk = []
    for entry in entries:
        kind, removed = entry.get("type"), entry.get("status") == "removed"
        if kind == "movie":
            plan["movies"] = True
            if removed:
                tmdb_id = _int((entry.get("ids") or {}).get("tmdb"))
                if not tmdb_id:
                    return None
                plan["removed_movies"].add(tmdb_id)
        elif kind == "episode":
            plan["episodes"] = True
            if removed:
                media, season, number = (_int(entry.get(k)) for k in ("media_id", "season_number", "number"))
                if media is None or season is None or number is None:
                    return None
                plan["removed_episodes"].append((media, season, number))
        elif kind in ("season", "show"):
            plan["episodes"] = True
            if removed:
                bulk.append(_int(entry.get("media_id") if kind == "season" else entry.get("id")))
    covered = {media for media, _, _ in plan["removed_episodes"]}
    if any(media is None or media not in covered for media in bulk):
        return None  # a season / show was unwatched and its episodes are not in the journal: do not guess
    return plan


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def newest_stamp(activities, keys):
    """The newest of these stamps across movies, shows and episodes: NO_ACTIVITY for an account with none, None when the answer is not
    readable at all (nothing can then be said about what changed)"""
    if not isinstance(activities, dict) or not activities:
        return None
    stamps = [(activities.get(kind) or {}).get(key) for kind in KINDS for key in keys]
    return max((s for s in stamps if s), default=NO_ACTIVITY)


def ratings_stamp(activities):
    """The newest stamp of the account-level `ratings` object of last_activities (3.9.178): NO_ACTIVITY for an account that never rated, None
    when the object is absent or unreadable (the ratings are then left alone this run and the rest of the sync goes on). The per-kind stamps
    are not used: a season-only rating moves none of them."""
    block = activities.get("ratings") if isinstance(activities, dict) else None
    if not isinstance(block, dict):
        return None
    return max((s for s in (block.get(key) for key in RATINGS_KEYS) if s), default=NO_ACTIVITY)


def rating_value(value):
    """A rating is 0-10 with one decimal (8.5); anything else is left out (None)"""
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    return rating if 0 < rating <= 10 else None


def rating_target(row):
    """(media_type, tmdb id, season, number) of the stored row a ratings row is about, or None when it cannot be told. The rows differ by type
    (live-probed in TMDb Helper 7.5.7): a movie or show row has the title's own TMDb id in ids; a season row has the season number in `number` and
    the show in show_ids (its own ids.tmdb is the SEASON's); an episode row has season_number and number and the show in show_ids (its own ids.tmdb
    is the episode's). So a season or an episode is keyed by the SHOW's id, never by its own. A removed row (incremental read) keeps the same ids."""
    kind = row.get("type")
    if kind in ("movie", "show"):
        tmdb_id = _int((row.get("ids") or {}).get("tmdb"))
        return (kind, tmdb_id, -1, -1) if tmdb_id else None
    show = _int((row.get("show_ids") or {}).get("tmdb"))
    if kind == "season":
        season = _int(row.get("number"))
        return ("season", show, season, -1) if show and season is not None else None
    if kind == "episode":
        season, number = _int(row.get("season_number")), _int(row.get("number"))
        return ("episode", show, season, number) if show and season is not None and number is not None else None
    return None


def parse_ratings(rows):
    """(rows to write {key: (media_type, tmdb, season, number, rating, rated_at)}, keys whose rating was removed) from a ratings list, in the
    order WeTrakr gave it: the LATER row of a key wins, so a rating removed and set again in one window is a rating, not a removal. A removed
    row (status 'removed', no rating) only appears in an incremental read (from_date)."""
    changed, removed = {}, set()
    for row in rows:
        target = rating_target(row)
        if not target:
            continue
        if row.get("status") == "removed":
            changed.pop(target, None)
            removed.add(target)
            continue
        rating = rating_value(row.get("rating"))
        if rating:
            changed[target] = (*target, rating, row.get("rated_at"))
            removed.discard(target)
    return changed, removed


def paused_progress(row):
    """The resume percentage of a row of the playing list, or None. Only a PAUSED row is a resume point: the percentage of a 'playing' row
    is moved along by WeTrakr's clock. Kept as WeTrakr gives it (a decimal): Seren turns it into seconds, and 1 % of a film is over a minute."""
    if row.get("status") != "paused":
        return None
    try:
        progress = float(row.get("progress_percent"))
    except (TypeError, ValueError):
        return None
    return round(progress, 2) if 0 < progress < 100 else None


class WeTrakrSyncDatabase(wetrakr_sync.WeTrakrSyncDatabase):
    def __init__(self):
        super().__init__()

    @cached_property
    def wetrakr_api(self):
        from resources.lib.indexers.wetrakr import WeTrakrAPI

        return WeTrakrAPI()

    def _update_activity_record(self, record, value):
        self.execute_sql(f"UPDATE activities SET {record}=? WHERE sync_id=1", (value,))

    def clear_last_sync(self):
        """Clear-button counterpart to force_sync(): resets the stamps and tells the user"""
        self.set_base_activities()
        g.notification(g.ADDON_NAME, g.get_language_string(31121))

    def force_sync(self):
        """Resets the stamps and reads everything again"""
        self.set_base_activities()
        self.sync_activities()

    def sync_activities(self):
        """Periodic sync. One request (/sync/last_activities) while nothing changed; a changed watched stamp reads only what changed (the journal
        names the titles, the lists' from_date gives their new state; 3.9.177) or, when that cannot be done safely, the two watched lists in full;
        a changed resume-point stamp costs the two playing lists (every list is read in full or not at all)"""
        with GlobalLock("wetrakr.sync"):
            if not g.get_bool_setting("wetrakr.enabled", True) or not g.get_setting("wetrakr.auth"):
                g.log("WeTrakrSync: WeTrakr disabled or no auth present, no sync will occur", "warning")
                return

            try:
                self.wetrakr_api.ensure_username()  # 3.9.181: a login without a name (Account Manager's push, older sign-in) gets it here
            except Exception:
                g.log_stacktrace()
            self.refresh_activities()
            remote = self.wetrakr_api.get_json("/sync/last_activities")
            watched_stamp, playback_stamp = newest_stamp(remote, WATCHED_KEYS), newest_stamp(remote, PLAYBACK_KEYS)
            if watched_stamp is None or playback_stamp is None:
                g.log("WeTrakr Activities Sync Failure: unable to read last_activities from WeTrakr", "error")
                return True

            watched_count = playback_count = ratings_count = 0
            ratings_now = ratings_stamp(remote)  # None: this answer says nothing about the ratings, they are left alone (not an error)
            if watched_stamp != self.activities["watched_at"]:
                try:
                    watched_count, watched_stamp = self._sync_watched(watched_stamp)
                    self._update_activity_record("watched_at", watched_stamp)  # the stamp read BEFORE the lists were read (never newer than the journal showed)
                except ActivitySyncFailure as e:
                    g.log(f"Failed to sync WeTrakr watched marks: {e}", "error")
            if playback_stamp != self.activities["playback_at"]:
                try:
                    playback_count = self._sync_playback()
                    self._update_activity_record("playback_at", playback_stamp)
                except ActivitySyncFailure as e:
                    g.log(f"Failed to sync WeTrakr resume points: {e}", "error")
            if ratings_now is not None and ratings_now != self.activities.get("ratings_at"):
                try:
                    ratings_count = self._sync_ratings(self.activities.get("ratings_at"))
                    self._update_activity_record("ratings_at", ratings_now)  # the stamp read BEFORE the list was read
                except ActivitySyncFailure as e:
                    g.log(f"Failed to sync WeTrakr ratings: {e}", "error")
            if watched_count or playback_count or ratings_count:
                g.log(f"WeTrakrSync: {watched_count} watched marks, {playback_count} resume points and {ratings_count} ratings stored", "info")
            if watched_count or playback_count:
                g.notification_unless_playing(g.ADDON_NAME, g.get_language_string(31748).format(watched_count, playback_count))

    # region lists
    def _read(self, path):
        ok, rows = self.wetrakr_api.get_list(path)
        if not ok:
            raise ActivitySyncFailure(f"{path} could not be read completely")
        return rows

    def _sync_watched(self, stamp):
        """Brings the watched marks up to date and returns (rows touched, the stamp to save). Incremental when a journal mark is saved (see
        _sync_watched_incremental); otherwise, or when that could not be done safely, the two lists are read in full (_sync_watched_full) and the
        stamp that read started from becomes the journal mark.

        The journal shows a change only a few seconds after it was made, so its `visible_until` (never the clock or the activity stamp) is the
        next mark, and the stamp saved is never newer than it: a change still held back keeps the stamp behind WeTrakr's, so the next sync reads it."""
        done = self._sync_watched_incremental()
        if done is not None:
            count, until = done
            self._update_activity_record("journal_at", until)
            return count, min(stamp, until)
        count = self._sync_watched_full()
        self._update_activity_record("journal_at", stamp)
        return count, stamp

    def _sync_watched_incremental(self):
        """(rows touched, the journal's visible_until) once the stored data was brought up to date from the journal; None when the lists must be read
        in full (nothing is changed then). The journal says WHICH titles changed since the mark and the lists' own from_date returns their NEW
        state: a title still watched comes back with its new play count, one that lost its last play does not come back, so a title the journal
        reports as removed and the list does not return is deleted. One lower bound (mark - 1 s) for the journal and both lists, the journal FIRST:
        its upper bound is then never newer than what the lists show."""
        mark = self.activities["journal_at"] if self.activities else ""
        since = shift_iso(mark, -OVERLAP) if mark else None
        if not since:
            g.log("WeTrakrSync: watched - no journal mark yet, reading the lists in full", "info")
            return None
        state, entries, until = self.wetrakr_api.get_journal(since, "watched")
        if state != "ok":
            g.log(f"WeTrakrSync: watched - journal not usable ({state}), reading the lists in full", "info")
            return None
        plan = journal_plan(entries)
        if plan is None:
            g.log("WeTrakrSync: watched - the journal has a change that cannot be placed, reading the lists in full", "info")
            return None
        movie_rows, episode_rows = [], []
        if plan["movies"]:
            ok, movie_rows = self.wetrakr_api.get_list(MOVIES_PATH, {"from_date": since})
            if not ok:
                return None
        if plan["episodes"]:
            ok, episode_rows = self.wetrakr_api.get_list(EPISODES_PATH, {"from_date": since})
            if not ok:
                return None
        try:
            count = self._apply_delta(plan, movie_rows, episode_rows)
        except Exception as e:  # pylint: disable=broad-except
            g.log(f"WeTrakrSync: watched - the changes could not be applied ({e}), reading the lists in full", "info")
            return None
        if count is None:
            g.log("WeTrakrSync: watched - a removed episode could not be placed, reading the lists in full", "info")
            return None
        g.log(
            f"WeTrakrSync: watched - incremental, {len(entries)} journal entries, {len(movie_rows)} movie rows, {len(episode_rows)} episode rows",
            "info",
        )
        return count, until

    def _apply_delta(self, plan, movie_rows, episode_rows):
        """Write the changed rows and delete the titles that lost their last play. Returns the number of rows touched, or None when a removed
        episode cannot be placed (nothing is written then)."""
        movies = {}
        for row in movie_rows:
            tmdb_id = _int((row.get("ids") or {}).get("tmdb"))
            if tmdb_id:
                movies[tmdb_id] = (tmdb_id, _int(row.get("plays")) or 1, row.get("last_watched_at"))
        episodes, shows = {}, {}  # shows: WeTrakr show id -> TMDb id, from the rows just read, then from the stored ones
        for row in episode_rows:
            show = _int((row.get("show_ids") or {}).get("tmdb"))
            season, number, wt_show = _int(row.get("season_number")), _int(row.get("number")), _int(row.get("show_id"))
            if show and season is not None and number is not None:
                episodes[(show, season, number)] = (show, season, number, _int(row.get("plays")) or 1, row.get("last_watched_at"), wt_show)
                if wt_show:
                    shows[wt_show] = show
        gone_movies = [(tmdb_id,) for tmdb_id in plan["removed_movies"] if tmdb_id not in movies]
        gone_episodes = []
        for media, season, number in plan["removed_episodes"]:
            show = shows.get(media) or self.show_tmdb_of(media)
            if not show:
                return None
            shows[media] = show
            if (show, season, number) not in episodes:
                gone_episodes.append((show, season, number))
        if movies:
            self.execute_sql("INSERT OR REPLACE INTO movies(tmdb_id, plays, last_watched_at) VALUES(?, ?, ?)", list(movies.values()))
        if episodes:
            self.execute_sql(
                "INSERT OR REPLACE INTO episodes(tmdb_id, season, number, plays, last_watched_at, wt_show_id) VALUES(?, ?, ?, ?, ?, ?)",
                list(episodes.values()),
            )
        if gone_movies:
            self.execute_sql("DELETE FROM movies WHERE tmdb_id=?", gone_movies)
        if gone_episodes:
            self.execute_sql("DELETE FROM episodes WHERE tmdb_id=? AND season=? AND number=?", gone_episodes)
        return len(movies) + len(episodes) + len(gone_movies) + len(gone_episodes)

    def _sync_watched_full(self):
        """GET /sync/tracking/watched/movies and /episodes (compact, every page): a full replace, only once BOTH lists were read completely"""
        try:
            movies = self._read(MOVIES_PATH)
            episodes = self._read(EPISODES_PATH)

            movie_rows = {}
            for row in movies:
                tmdb_id = _int((row.get("ids") or {}).get("tmdb"))
                if tmdb_id:
                    movie_rows[tmdb_id] = (tmdb_id, _int(row.get("plays")) or 1, row.get("last_watched_at"))
            episode_rows = {}
            for row in episodes:
                show = _int((row.get("show_ids") or {}).get("tmdb"))
                season, number = _int(row.get("season_number")), _int(row.get("number"))
                if show and season is not None and number is not None:
                    episode_rows[(show, season, number)] = (
                        show, season, number, _int(row.get("plays")) or 1, row.get("last_watched_at"), _int(row.get("show_id")),
                    )

            self.execute_sql("DELETE FROM movies")
            self.execute_sql("DELETE FROM episodes")
            if movie_rows:
                self.execute_sql(
                    "INSERT OR REPLACE INTO movies(tmdb_id, plays, last_watched_at) VALUES(?, ?, ?)", list(movie_rows.values())
                )
            if episode_rows:
                self.execute_sql(
                    "INSERT OR REPLACE INTO episodes(tmdb_id, season, number, plays, last_watched_at, wt_show_id) VALUES(?, ?, ?, ?, ?, ?)",
                    list(episode_rows.values()),
                )
            return len(movie_rows) + len(episode_rows)
        except ActivitySyncFailure:
            raise
        except Exception as e:
            raise ActivitySyncFailure(e) from e

    def _sync_ratings(self, stored):
        """GET /sync/ratings/all (compact, every page): movies, shows, seasons and episodes in one list. With a stored stamp only the ratings
        changed since it are read (from_date = the stored stamp - 1 s; a removed one comes back with status 'removed' and no rating); with none
        (first read, force sync, clear, rebuild) the whole list replaces the table, only once every page was read. A failed read keeps the rows
        and the stamp. Returns the number of rows touched."""
        try:
            since = shift_iso(stored, -OVERLAP) if stored else None
            if since:
                ok, rows = self.wetrakr_api.get_list(RATINGS_PATH, {"from_date": since})
                if not ok:
                    raise ActivitySyncFailure(f"{RATINGS_PATH} could not be read completely")
                changed, removed = parse_ratings(rows)
                if changed:
                    self.execute_sql(
                        "INSERT OR REPLACE INTO ratings(media_type, tmdb_id, season, number, rating, rated_at) VALUES(?, ?, ?, ?, ?, ?)",
                        list(changed.values()),
                    )
                if removed:
                    self.execute_sql(
                        "DELETE FROM ratings WHERE media_type=? AND tmdb_id=? AND season=? AND number=?", list(removed)
                    )
                g.log(f"WeTrakrSync: ratings - incremental, {len(changed)} changed, {len(removed)} removed", "info")
                return len(changed) + len(removed)
            changed, _ = parse_ratings(self._read(RATINGS_PATH))
            self.execute_sql("DELETE FROM ratings")
            if changed:
                self.execute_sql(
                    "INSERT OR REPLACE INTO ratings(media_type, tmdb_id, season, number, rating, rated_at) VALUES(?, ?, ?, ?, ?, ?)",
                    list(changed.values()),
                )
            g.log(f"WeTrakrSync: ratings - {len(changed)} rated, read in full", "info")
            return len(changed)
        except ActivitySyncFailure:
            raise
        except Exception as e:
            raise ActivitySyncFailure(e) from e

    def _sync_playback(self):
        """GET /sync/tracking/playing/movies and /episodes (compact, every page): the PAUSED rows are the resume points; a full replace, so a
        pause that was finished or cancelled on WeTrakr disappears"""
        try:
            movies = self._read("/sync/tracking/playing/movies")
            episodes = self._read("/sync/tracking/playing/episodes")

            rows = {}
            for row in movies:
                tmdb_id, progress, paused_at = _int((row.get("ids") or {}).get("tmdb")), paused_progress(row), row.get("tracked_at")
                if tmdb_id and progress and paused_at:
                    rows[("movie", tmdb_id, -1, -1)] = ("movie", tmdb_id, -1, -1, str(progress), paused_at)
            for row in episodes:
                show = _int((row.get("show_ids") or {}).get("tmdb"))
                season, number = _int(row.get("season_number")), _int(row.get("number"))
                progress, paused_at = paused_progress(row), row.get("tracked_at")
                if show and season is not None and number is not None and progress and paused_at:
                    rows[("episode", show, season, number)] = ("episode", show, season, number, str(progress), paused_at)

            self.execute_sql("DELETE FROM bookmarks")
            if rows:
                self.execute_sql(
                    "INSERT OR REPLACE INTO bookmarks(media_type, tmdb_id, season, number, percent_played, paused_at) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    list(rows.values()),
                )
            return len(rows)
        except ActivitySyncFailure:
            raise
        except Exception as e:
            raise ActivitySyncFailure(e) from e

    # endregion

    def refresh_play_bookmark(self, tmdb_id, mediatype, season=None, number=None):
        """Pull ONE item's resume point from WeTrakr into the local bookmarks table at play start (see modules/resumeRefresh.py): one
        request (the playing list of that kind, compact) with a short timeout; replaces the item's local row when WeTrakr's is newer.
        Holds no lock and writes one row. Returns True when the local row was added or replaced. WeTrakr keeps a percentage only; the
        caller turns it into seconds with the item's runtime."""
        if not tmdb_id or mediatype not in ("movie", "episode"):
            return False
        response = self.wetrakr_api.user_request(
            "GET",
            "/sync/tracking/playing/movies" if mediatype == "movie" else "/sync/tracking/playing/episodes",
            params={"compact": "true", "limit": 5000},
            timeout=PLAY_TIME_TIMEOUT,
        )
        if response is None or response.status_code != 200:
            return False  # the request failed: the local copy is left as it is
        try:
            entries = response.json()
        except ValueError:
            return False
        if not isinstance(entries, list):
            return False
        complete = not response.headers.get("X-Pagination-Next")  # a longer list than one page: absence proves nothing

        found = None
        for entry in entries:
            if mediatype == "movie":
                if str(_int((entry.get("ids") or {}).get("tmdb"))) != str(tmdb_id):
                    continue
            else:
                if (
                    str(_int((entry.get("show_ids") or {}).get("tmdb"))) != str(tmdb_id)
                    or str(_int(entry.get("season_number"))) != str(season)
                    or str(_int(entry.get("number"))) != str(number)
                ):
                    continue
            found = entry
            break

        local = self.get_bookmark(mediatype, tmdb_id, season, number)
        if found is not None:
            if found.get("status") != "paused":
                return False  # playing right now (on another device): not a resume point, and not proof that the old one is gone
            progress, paused_at = paused_progress(found), found.get("tracked_at")
            if not progress or not paused_at:
                return False
            if local and local["paused_at"] and g.validate_date(local["paused_at"]) >= (g.validate_date(paused_at) or ""):
                return False  # the local row is as new or newer
            self.set_bookmark(mediatype, tmdb_id, season, number, progress, paused_at)
            g.log(f"Resume: took the newer WeTrakr resume point for tmdb {tmdb_id} ({progress:.1f}%)", "info")
            return True
        # the playing list holds every paused item of this kind: no row for this item = no resume point there any more
        if local and complete:
            from resources.lib.modules.resumeRefresh import written_just_now

            if not written_just_now(local["paused_at"]):  # a row written under 2 min ago may not have reached WeTrakr yet
                self.remove_bookmark(mediatype, tmdb_id, season, number)
                g.log(f"Resume: WeTrakr no longer has a resume point for tmdb {tmdb_id}, dropped the local copy", "info")
        return False
