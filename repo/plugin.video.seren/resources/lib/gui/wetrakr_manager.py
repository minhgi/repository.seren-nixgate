"""The WeTrakr manager of the item context menu (Seren 3.9.169): Set as watched / unwatched, Rate, Reset Resume Position.

The same dialog the Trakt / MDBList / Simkl managers use (gui/trakt_context_menu.py, scope "wetrakr"); this module holds the parts that are plain
functions so they can be tested without Kodi: the request bodies, the reading of WeTrakr's answers and the menu gates.

WeTrakr resolves every title by TMDb id and numbers seasons and episodes the TMDb way, so everything is sent by TMDb id (a season or an episode
nested under its show by number). The bodies and the answer shapes were live-probed in TMDb Helper 7.5.3 (2026-11-21): a write answers 2xx with
counts under `added` / `removed` and names what it did not do under `notFound`, `existing` or `errored`.
WeTrakr is not a bridge service: nothing here is mirrored to Trakt, MDBList or Simkl (modules/cross_sync.py is not touched).
"""

import datetime
from functools import cached_property

import xbmcgui

from resources.lib.modules.globals import g

STR_HEADER = 31699
STR_SET_WATCHED = 31700
STR_SET_UNWATCHED = 31701
STR_RESET_RESUME = 31702
STR_RATE = 31703
STR_DONE_WATCHED = 31704
STR_DONE_UNWATCHED = 31705
STR_DONE_RESET = 31706
STR_DONE_RATED = 31707
STR_DONE_RATING_REMOVED = 31708
STR_ALREADY = 31709
STR_FAILED = 31710
STR_RATE_INPUT = 31711
STR_RATE_CHANGE = 31736  # "Change rating (WeTrakr: {})": the Rate entry while the local copy holds a rating (3.9.178)


# region plain functions
def rate_label(db, target):
    """The text of the Rate entry: 'Change rating (WeTrakr: 8.5)' while the local copy holds a rating for the item, else 'Rate (WeTrakr)'.
    Local data only (no request); a copy that cannot be read gives the plain text."""
    try:
        current = db.get_rating(*target)
    except Exception:  # pylint: disable=broad-except
        current = None
    if current:
        return g.get_language_string(STR_RATE_CHANGE).format(f"{current:g}")
    return g.get_language_string(STR_RATE)


def has_entries(value):
    """True when a WeTrakr answer part (an array, or an object of arrays per target) holds anything"""
    if isinstance(value, dict):
        return any(has_entries(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(has_entries(v) for v in value)
    return bool(value)


def read_outcome(response):
    """What a write answered, as (status, detail): 'ok', 'already' (WeTrakr says the title is in that state) or 'failed'.

    A 2xx answer is not enough: WeTrakr answers 200 with counts of what changed under `added` / `removed` and names what it did not do under
    `notFound` (id not resolved), `existing` or `errored` (e.g. "Already watched!", "Nothing to remove"). A season or a show can be partly done
    (some episodes changed, the rest listed as errored), so anything that really changed counts as done first; only an answer that changed
    nothing is 'already' or 'failed'."""
    if response is None:
        return "failed", "no answer from WeTrakr"
    code = response.status_code
    try:
        data = response.json()
    except ValueError:
        data = None
    if code == 404 and isinstance(data, dict) and data.get("error") == "NO_SESSION":
        return "already", "no resume point left on WeTrakr"  # DELETE /scrobble/playing with nothing paused
    if not 200 <= code < 300:
        return "failed", f"HTTP {code}"
    if not isinstance(data, dict):
        return "ok", ""
    if has_entries(data.get("added")) or has_entries(data.get("removed")):
        return "ok", ""
    if has_entries(data.get("notFound")) or has_entries(data.get("not_found")):
        return "failed", "WeTrakr could not find the title"
    if has_entries(data.get("existing")):
        return "already", "already in that state on WeTrakr"
    errored = data.get("errored") or data.get("errors")
    if has_entries(errored):
        if "already" in f"{errored}".lower() or "nothing to remove" in f"{errored}".lower():
            return "already", "already in that state on WeTrakr"
        return "failed", "WeTrakr refused the change"
    return "ok", ""


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def target_of(info):
    """(kind, tmdb id, season, episode) of the item behind a manager call, or None when it cannot be addressed. kind: movie / show / season /
    episode. A show, season or episode is addressed by the SHOW's TMDb id."""
    mediatype = info.get("mediatype")
    if mediatype == "movie":
        tmdb_id = _int(info.get("tmdb_id"))
        return ("movie", tmdb_id, None, None) if tmdb_id else None
    if mediatype == "tvshow":
        tmdb_id = _int(info.get("tmdb_id"))
        return ("show", tmdb_id, None, None) if tmdb_id else None
    if mediatype in ("season", "episode"):
        tmdb_id, season = _int(info.get("tmdb_show_id")), _int(info.get("season"))
        if not tmdb_id or season is None:
            return None
        if mediatype == "season":
            return "season", tmdb_id, season, None
        episode = _int(info.get("episode"))
        return ("episode", tmdb_id, season, episode) if episode is not None else None
    return None


def tracking_payload(target, status="watched"):
    """Body of POST /sync/tracking and /sync/tracking/remove/all (the same shape)"""
    kind, tmdb_id, season, episode = target
    ids = {"tmdb": tmdb_id}
    if kind == "movie":
        return {"movies": [{"ids": ids, "status": status}]}
    if kind == "show":
        return {"shows": [{"ids": ids, "status": status}]}
    if kind == "season":
        return {"shows": [{"ids": ids, "status": status, "seasons": [{"number": season}]}]}
    return {"shows": [{"ids": ids, "status": status, "seasons": [{"number": season, "episodes": [{"number": episode}]}]}]}


def playing_payload(target):
    """Body of DELETE /scrobble/playing: the movie, or the show and its episode (a resume point exists for those only)"""
    kind, tmdb_id, season, episode = target
    if kind == "movie":
        return {"movie": {"ids": {"tmdb": tmdb_id}}}
    if kind == "episode":
        return {"show": {"ids": {"tmdb": tmdb_id}}, "episode": {"season": season, "number": episode}}
    return None


def rating_payload(target, rating=None):
    """Body of POST /sync/ratings (with a rating) and /sync/ratings/remove (without)"""
    kind, tmdb_id, season, episode = target
    ids = {"tmdb": tmdb_id}
    extra = {} if rating is None else {"rating": rating}
    if kind == "movie":
        return {"movies": [{"ids": ids, **extra}]}
    if kind == "show":
        return {"shows": [{"ids": ids, **extra}]}
    if kind == "season":
        return {"shows": [{"ids": ids, "seasons": [{"number": season, **extra}]}]}
    return {"shows": [{"ids": ids, "seasons": [{"number": season, "episodes": [{"number": episode, **extra}]}]}]}


def watched_gate(db, target, episode_total=0):
    """(offer Set as watched, offer Set as unwatched), from the LOCAL copy only (no request).

    A movie / an episode: the one that applies. A show / a season: Set as watched unless every aired episode is watched, Set as unwatched once
    one episode is (the same two-option rule TMDb Helper's WeTrakr menu uses: a half-watched show can go either way). Before the first sync
    of the watched marks the copy is empty and says nothing: both are offered."""
    db.refresh_activities()
    if not (db.activities or {}).get("watched_at"):
        return True, True
    kind, tmdb_id, season, episode = target
    if kind == "movie":
        watched = db.is_movie_watched(tmdb_id)
        return not watched, watched
    if kind == "episode":
        watched = db.is_episode_watched(tmdb_id, season, episode)
        return not watched, watched
    count = db.get_watched_episode_count(tmdb_id, season if kind == "season" else None)
    complete = episode_total > 0 and count >= episode_total
    return not complete, count > 0


def has_resume_point(db, target):
    kind, tmdb_id, season, episode = target
    if kind not in ("movie", "episode"):
        return False
    return db.get_bookmark(kind, tmdb_id, season, episode) is not None


# endregion


class WeTrakrManager:
    """The actions of the WeTrakr manager. header is the dialog title the notifications carry."""

    def __init__(self, header):
        self.header = header

    @cached_property
    def api(self):
        from resources.lib.indexers.wetrakr import WeTrakrAPI

        return WeTrakrAPI()

    @staticmethod
    def database():
        from resources.lib.database.wetrakr_sync import WeTrakrSyncDatabase

        return WeTrakrSyncDatabase()

    def _notify(self, string_id):
        g.notification(f"{g.ADDON_NAME}: {self.header}", g.get_language_string(string_id))

    def _write(self, method, path, payload):
        """Sends the write and reads the answer: (status, detail)"""
        response = self.api.user_request(method, path, body=payload)
        status, detail = read_outcome(response)
        if status == "failed":
            g.log(f"WeTrakr {method} {path} failed: {detail}", "warning")
        return status, detail

    def _resync_watched(self):
        """Read the watched marks again right after a write: the saved watched stamp is dropped first (WeTrakr's own stamp may not have moved
        yet) so the next read looks regardless. Waits for a periodic sync that is already running (it holds the same lock); never raises."""
        try:
            from resources.lib.database.wetrakr_sync.activities import WeTrakrSyncDatabase

            db = WeTrakrSyncDatabase()
            db.reset_watched_stamp()
            db.sync_activities()
        except Exception:  # pylint: disable=broad-except
            g.log_stacktrace()

    @staticmethod
    def _refresh():
        g.container_refresh()
        g.trigger_widget_refresh()

    def set_watched(self, info, watched):
        target = target_of(info)
        if target is None:
            return
        payload = tracking_payload(target, "watched")
        if watched:
            payload["allow_rewatch"] = False
            status, _ = self._write("POST", "/sync/tracking", payload)
        else:
            status, _ = self._write("POST", "/sync/tracking/remove/all", payload)
        if status == "failed":
            self._notify(STR_FAILED)
            return
        if status == "ok":
            self._write_through_watched(target, watched)
        self._notify(STR_ALREADY if status == "already" else (STR_DONE_WATCHED if watched else STR_DONE_UNWATCHED))
        self._resync_watched()
        self._refresh()

    def _write_through_watched(self, target, watched):
        """A movie / an episode in the local copy at once (a show / a season is left to the read that follows). A title set as watched must
        not keep a resume point: WeTrakr drops its paused row itself when a play is logged, so only the local row is cleared."""
        kind, tmdb_id, season, episode = target
        if kind not in ("movie", "episode"):
            return
        try:
            db = self.database()
            if watched:
                now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if kind == "movie":
                    db.mark_movie_watched_locally(tmdb_id, now)
                else:
                    db.mark_episode_watched_locally(tmdb_id, season, episode, now)
                db.remove_bookmark(kind, tmdb_id, season, episode)
            elif kind == "movie":
                db.mark_movie_unwatched_locally(tmdb_id)
            else:
                db.mark_episode_unwatched_locally(tmdb_id, season, episode)
        except Exception:  # pylint: disable=broad-except
            g.log_stacktrace()

    def mark_watched(self, info):
        self.set_watched(info, True)

    def mark_unwatched(self, info):
        self.set_watched(info, False)

    def reset_resume_point(self, info):
        target = target_of(info)
        payload = playing_payload(target) if target else None
        if not payload:
            return
        status, _ = self._write("DELETE", "/scrobble/playing", payload)
        if status == "failed":
            self._notify(STR_FAILED)
            return
        try:  # done, or already gone on WeTrakr: either way the local row has no business staying
            kind, tmdb_id, season, episode = target
            self.database().remove_bookmark(kind, tmdb_id, season, episode)
        except Exception:  # pylint: disable=broad-except
            g.log_stacktrace()
        self._notify(STR_ALREADY if status == "already" else STR_DONE_RESET)
        self._refresh()

    def rate(self, info):
        target = target_of(info)
        if target is None:
            return
        entered = xbmcgui.Dialog().input(g.get_language_string(STR_RATE_INPUT), "", xbmcgui.INPUT_NUMERIC)
        if not entered:
            return
        rating = _int(entered)
        if rating is None or not 0 <= rating <= 10:
            self._notify(STR_FAILED)
            return
        if rating == 0:
            status, _ = self._write("POST", "/sync/ratings/remove", rating_payload(target))
        else:
            status, _ = self._write("POST", "/sync/ratings", rating_payload(target, rating))
        if status == "failed":
            self._notify(STR_FAILED)
            return
        self._write_through_rating(target, rating)  # done, or already so on WeTrakr: the local copy holds what WeTrakr holds
        if status == "already":
            self._reset_ratings_stamp()  # 'already' does not say which value WeTrakr keeps: the next sync reads the ratings in full
            self._notify(STR_ALREADY)
        else:
            self._notify(STR_DONE_RATING_REMOVED if rating == 0 else STR_DONE_RATED)
        # a rating changes no list: nothing to refresh; the watched stamp and the journal mark are left alone (a rating is not a watched mark)

    def _reset_ratings_stamp(self):
        try:
            self.database().reset_ratings_stamp()
        except Exception:  # pylint: disable=broad-except
            g.log_stacktrace()

    def _write_through_rating(self, target, rating):
        """The rating in the local copy at once, so the Rate entry says 'Change rating (X)' on the next open; the ratings stamp is left alone,
        the next incremental read of the ratings brings the same row again"""
        kind, tmdb_id, season, episode = target
        try:
            db = self.database()
            if rating == 0:
                db.remove_rating_locally(kind, tmdb_id, season, episode)
            else:
                now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                db.set_rating_locally(kind, tmdb_id, season, episode, rating, now)
        except Exception:  # pylint: disable=broad-except
            g.log_stacktrace()
