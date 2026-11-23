"""Play-time refresh of the resume point from Trakt, MDBList and Simkl (Seren 3.9.129).

Seren decides where to resume from its LOCAL copy of each service's resume points. That copy is refreshed by the
periodic activity sync: about 45 s after Kodi starts, then every 13-17 minutes, never when play is pressed. After a
switch to another device (pause on the PC, press play on the Shield) the play therefore started from 0:00 or from an
older point (PC log 2026-11-15: play 48 s after Kodi started, first sync 24 s later; Shield: first sync 85 s after start).

newest_resume() runs at play start for every movie / episode play that can resume (3.9.130: the TMDb Helper route with
forceresumecheck, plays from Seren's own lists, playlist and auto-next plays), unless resuming is switched off. It waits (read only) for a periodic
sync that is already running, pulls the item's resume point from every enabled service side by side, and returns the NEWEST
resume point of the three in seconds, or None, plus whether the answer can be trusted. All of it is bounded by DEADLINE.
The worker threads hold no lock and change one row each, so one that is still running when the deadline passes is harmless.
A service that cannot be reached leaves its local row as it is. When the answer cannot be trusted (nothing to ask, a periodic
sync still rewriting the tables at the deadline, an error) the caller keeps what it had: the list's resume param or the old
Trakt-only local lookup, so playback is never held up for long or broken.
"""

import threading
import time

from resources.lib.modules.global_lock import GlobalLock
from resources.lib.modules.globals import g

DEADLINE = 8.0  # seconds for the whole refresh: the wait for a running sync plus the lookups, which run side by side

_SYNC_LOCKS = {"trakt": "trakt.sync", "mdblist": "mdblist.sync", "simkl": "simkl.sync", "wetrakr": "wetrakr.sync"}


def best_resume_seconds(item_information):
    """The newest resume point in seconds, None = none found (or not asked). See newest_resume() for whether to trust a None."""
    return newest_resume(item_information)[0]


def newest_resume(item_information):
    """Refresh the item's resume point from every enabled service.

    Returns (seconds of the newest resume point or None, answered). answered is True when the services were asked and their local
    tables could be read in a settled state: a None then means the item has no resume point on any service, so a stale copy
    (a list's resume param) must not be used. answered is False when there was nothing to ask, a service had not answered at the
    deadline or a periodic sync was still rewriting the tables, so a None is not trustworthy.
    """
    try:
        info = item_information["info"]
        mediatype = info.get("mediatype")
        trakt_id = (item_information.get("action_args") or {}).get("trakt_id") or info.get("trakt_id")
        services = _enabled_services()
        if not services or mediatype not in (g.MEDIA_MOVIE, g.MEDIA_EPISODE):
            return None, False

        started = time.monotonic()
        # A periodic sync that is mid-way rewrites a whole table (the local copy is empty or half full meanwhile): let it
        # finish first. Nothing is locked here, so a sync that outlasts the deadline is simply read as it stands.
        sync_still_running = _wait_for_running_syncs(services, started)

        workers = []
        for name in services:
            worker = threading.Thread(
                target=_refresh_service, args=(name, info, mediatype, trakt_id), name=f"resume-{name}", daemon=True
            )
            worker.start()
            workers.append((name, worker))
        slow = []
        for name, worker in workers:
            worker.join(max(0.0, DEADLINE - (time.monotonic() - started)))
            if worker.is_alive():
                slow.append(name)
                g.log(f"Resume: {name} did not answer within {DEADLINE:.0f}s, using its local resume point", "info")

        candidates = _candidates(services, info, mediatype, trakt_id)
        if not candidates:
            if sync_still_running:
                g.log("Resume: a sync was still rewriting the resume points, not trusting the empty answer", "info")
            # nothing found: only a real answer from every service means "no resume point anywhere"
            return None, not (sync_still_running or slow)
        best = max(candidates, key=lambda c: c[2])  # newest paused_at; max() keeps the first of equals (Trakt first)
        g.log(
            "Resume: "
            + ", ".join(f"{n} {s}s at {p or 'unknown time'}" for n, s, p in candidates)
            + f" -> using {best[0]} ({best[1]}s) after {time.monotonic() - started:.1f}s",
            "info",
        )
        return best[1], True
    except Exception as e:  # never let the refresh stop a play
        g.log(f"Resume: refresh failed ({e}), using the local resume point", "warning")
        return None, False


def _enabled_services():
    services = []
    if g.get_setting("trakt.auth") and g.get_bool_setting("trakt.enabled", False):
        services.append("trakt")
    if g.get_bool_setting("mdblist.enabled") and (g.get_setting("mdblist.auth") or g.get_setting("mdblist.apikey")):
        services.append("mdblist")
    if g.get_setting("simkl.auth", "") and g.get_bool_setting("simkl.enabled", True):
        services.append("simkl")
    if g.get_setting("wetrakr.auth", "") and g.get_bool_setting("wetrakr.enabled", True):
        services.append("wetrakr")
    return services


def _wait_for_running_syncs(services, started):
    """Poll the 'sync is running' flags (set by GlobalLock while a periodic sync runs) until clear or the deadline.
    Returns True when a sync was still running at the end (the local tables may be half rewritten)."""
    names = [_SYNC_LOCKS[s] for s in services]
    waited_for = None
    while time.monotonic() - started < DEADLINE:
        running = [n for n in names if GlobalLock(n)._running()]  # pylint: disable=protected-access
        if not running:
            return False if not waited_for else _log_wait(waited_for, started, False)
        waited_for = running
        if g.wait_for_abort(0.1):
            return True
    if waited_for:
        return _log_wait(waited_for, started, True)
    return False


def _log_wait(names, started, still_running):
    g.log(f"Resume: waited {time.monotonic() - started:.1f}s for a running sync ({', '.join(names)})", "info")
    return still_running


def _refresh_service(name, info, mediatype, trakt_id):
    """Worker thread: pull this item's resume point from one service into its local table. Never raises."""
    try:
        season = info.get("season") if mediatype == g.MEDIA_EPISODE else None
        number = info.get("episode") if mediatype == g.MEDIA_EPISODE else None
        tmdb_id = info.get("tmdb_id") if mediatype == g.MEDIA_MOVIE else info.get("tmdb_show_id")
        if name == "trakt":
            from resources.lib.database.trakt_sync.bookmark import TraktSyncDatabase

            TraktSyncDatabase().refresh_bookmark_from_trakt(trakt_id, mediatype)
        elif name == "mdblist":
            from resources.lib.database.mdblist_sync.activities import MDBListSyncDatabase

            MDBListSyncDatabase().refresh_play_bookmark(tmdb_id, mediatype, season, number)
        elif name == "simkl":
            from resources.lib.database.simkl_sync.activities import SimklSyncDatabase

            SimklSyncDatabase().refresh_play_bookmark(tmdb_id, mediatype, season, number)
        elif name == "wetrakr":
            from resources.lib.database.wetrakr_sync.activities import WeTrakrSyncDatabase

            WeTrakrSyncDatabase().refresh_play_bookmark(tmdb_id, mediatype, season, number)
    except Exception as e:
        g.log(f"Resume: could not refresh the {name} resume point ({e}), using the local copy", "warning")


def _normalise(paused_at):
    """Service timestamps (Z, offsets, fractions) as one comparable 'YYYY-MM-DDTHH:MM:SS' string; '' when unreadable."""
    try:
        return g.validate_date(paused_at) or ""
    except Exception:
        return ""


def _candidates(services, info, mediatype, trakt_id):
    """[(service, seconds, normalised paused_at)] from each enabled service's local table, after the refresh."""
    found = []
    duration = _number(info.get("duration"))
    season = info.get("season") if mediatype == g.MEDIA_EPISODE else None
    number = info.get("episode") if mediatype == g.MEDIA_EPISODE else None
    tmdb_id = info.get("tmdb_id") if mediatype == g.MEDIA_MOVIE else info.get("tmdb_show_id")

    if "trakt" in services and trakt_id:
        from resources.lib.database.trakt_sync.bookmark import TraktSyncDatabase

        row = TraktSyncDatabase().get_bookmark(trakt_id)
        seconds = _number(row["resume_time"]) if row else None
        if seconds and seconds > 0:
            found.append(("trakt", int(seconds), _normalise(row["paused_at"])))

    if "mdblist" in services and tmdb_id:
        from resources.lib.database.mdblist_sync.activities import MDBListSyncDatabase

        row = MDBListSyncDatabase().get_bookmark(tmdb_id, mediatype, season, number)
        seconds = _number(row["resume_time"]) if row else None
        if seconds and seconds > 0:
            found.append(("mdblist", int(seconds), _normalise(row["paused_at"])))

    if "simkl" in services and tmdb_id and duration:
        from resources.lib.database.simkl_sync.activities import SimklSyncDatabase

        db = SimklSyncDatabase()
        if mediatype == g.MEDIA_MOVIE:
            row = db.fetchone(
                "SELECT percent_played, paused_at FROM bookmarks WHERE tmdb_id=? AND media_type='movie'", (tmdb_id,)
            )
        else:
            row = db.fetchone(
                "SELECT percent_played, paused_at FROM bookmarks "
                "WHERE tmdb_id=? AND season=? AND number=? AND media_type='episode'",
                (tmdb_id, season, number),
            )
        percent = _number(row["percent_played"]) if row else None
        if percent and 0 < percent < 100:  # Simkl keeps a percentage only: the item's runtime turns it into seconds
            found.append(("simkl", int(percent / 100 * duration), _normalise(row["paused_at"])))

    if "wetrakr" in services and tmdb_id and duration:
        from resources.lib.database.wetrakr_sync import WeTrakrSyncDatabase

        row = WeTrakrSyncDatabase().get_bookmark(mediatype, tmdb_id, season, number)
        percent = _number(row["percent_played"]) if row else None
        if percent and 0 < percent < 100:  # WeTrakr keeps a percentage only: the item's runtime turns it into seconds
            found.append(("wetrakr", int(percent / 100 * duration), _normalise(row["paused_at"])))

    # An item that is (almost) finished is not a resume point worth jumping to
    return [c for c in found if not duration or c[1] < duration]


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def written_just_now(paused_at):
    """True when a local row's UTC paused_at is under two minutes old: a stop made on this device whose scrobble may not have
    reached the service yet, so an absence in the service's answer proves nothing (3.9.132, same rule as Trakt's 3.9.130 guard)."""
    import datetime

    try:
        written = datetime.datetime.strptime(str(paused_at)[:19], "%Y-%m-%dT%H:%M:%S")
        age = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - written
        return age < datetime.timedelta(minutes=2)
    except (TypeError, ValueError):
        return False


def write_local_bookmark(service, mediatype, info, current_time, percent, ignore_seconds, min_percent):
    """Local resume point at a pause / stop for MDBList and Simkl (Seren 3.9.132): the counterpart of Trakt's
    Player._handle_bookmark, so list progress, the Simkl In Progress menu and the next play see the point at once instead of after
    the next sync (up to ~15 min). Uses the service's own thresholds, never deletes at 0 s (a failed stream must not erase a good
    point), and writes nothing below the ignore-seconds mark. Returns 'set', 'removed' or None. Database errors are left to the
    caller, which logs them."""
    if service not in ("mdblist", "simkl") or mediatype not in (g.MEDIA_MOVIE, g.MEDIA_EPISODE):
        return None
    current_time, percent = _number(current_time), _number(percent)
    if not current_time or current_time <= 0 or percent is None:
        return None
    episode = mediatype == g.MEDIA_EPISODE
    tmdb_id = info.get("tmdb_show_id") if episode else info.get("tmdb_id")
    season, number = (info.get("season"), info.get("episode")) if episode else (None, None)
    if not tmdb_id or (episode and (season is None or number is None)):
        return None
    if service == "mdblist":
        from resources.lib.database.mdblist_sync import MDBListSyncDatabase

        db = MDBListSyncDatabase()
    else:
        from resources.lib.database.simkl_sync import SimklSyncDatabase

        db = SimklSyncDatabase()
    if percent >= min_percent:
        db.remove_bookmark(tmdb_id, mediatype, season, number)
        return "removed"
    if current_time < ignore_seconds:
        return None
    if service == "mdblist":
        db.set_bookmark(tmdb_id, mediatype, season, number, percent, current_time)
    elif not db.set_bookmark(tmdb_id, mediatype, season, number, percent):
        return None  # Simkl: no local show / movie row to take the simkl id from yet; the next sync writes the point
    return "set"
