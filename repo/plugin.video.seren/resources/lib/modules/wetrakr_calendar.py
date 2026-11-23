"""WeTrakr as a source of calendar rows (3.9.179).

The Show Calendar (calendar_data.py), the TV Shows Daily Calendar (tv_schedule_data.py) and the Recent / Upcoming Episodes menu of
WeTrakr all read WeTrakr's own calendar of the shows the account follows: GET /calendar/shows?mode=following. One request answers
the episodes of a window with the show's TMDb id, the season and the number, and the UTC moment of the air time, so there is no per-show
walk and no list to page. It lists the shows you follow on WeTrakr: shows on the watching and waiting lists, and a show marked watched that is still airing, were seen in it live; a show on the plan to watch list was NOT tried (the test account's list is empty). It is WeTrakr's counterpart of Trakt's calendars/my/shows.

The shows are resolved through Trakt by TMDb id and the episodes are the Trakt episode rows with the same season and number: every
Seren row (art, cast, play) is built on the shared Trakt tables, the same as for the Simkl and MDBList layers.

WeTrakr is not in configured_providers() (the bridge counts that list), so every caller asks active() itself.
"""

import datetime

from resources.lib.modules.globals import g

CALENDAR_PATH = "/calendar/shows"
MAX_SPAN_DAYS = 31  # the most days one request may span, both ends included (a longer span answers HTTP 400)
UNION_CAP = 25  # shows resolved per open, the cap of the Simkl / MDBList union layers
WATCHED_MARK = "_wetrakr_watched"  # set on a calendar row that WeTrakr holds as watched; calendar_view.is_watched() reads it (3.9.180)


def active():
    return g.wetrakr_active()


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def utc_days(first_day, last_day, tz=None):
    """(first, last) UTC dates that the local days first_day .. last_day (dates) touch. Without a time zone WeTrakr files its rows by whole UTC
    days, and a local day reaches into the UTC day before or after it: asking for these dates (one more day than the window, at most) is
    exact, where one day of slack on each side would make the 30-day Upcoming window 32 days, one request over the 31-day limit.
    tz = a tzinfo (tests); None = this computer's own time zone."""
    begin = datetime.datetime.combine(first_day, datetime.time.min, tzinfo=tz)
    finish = datetime.datetime.combine(last_day, datetime.time.max, tzinfo=tz)
    return begin.astimezone(datetime.timezone.utc).date(), finish.astimezone(datetime.timezone.utc).date()


def _spans(first_day, last_day, tz=None):
    """(start, end) UTC dates of the requests that cover the local days first_day .. last_day, each at most MAX_SPAN_DAYS days"""
    start, end = utc_days(first_day, last_day, tz)
    spans = []
    while start <= end:
        stop = min(end, start + datetime.timedelta(days=MAX_SPAN_DAYS - 1))
        spans.append((start, stop))
        start = stop + datetime.timedelta(days=1)
    return spans


def parse_calendar(data, first_day, last_day):
    """The rows of one /calendar/shows answer whose LOCAL air day lies in first_day .. last_day: dicts tmdb_id (the show's), season, number,
    air_at (UTC moment, text), air_day (local date). A row without a show TMDb id, a season or a number cannot be placed and is left out; an
    episode seen twice counts once."""
    from resources.lib.modules.service_episode_window import local_air_date

    days = data.get("days") if isinstance(data, dict) else None
    if not isinstance(days, dict):  # a window with nothing in it answers {"total": 0, ...} without "days"
        return []
    rows = {}
    for day, entries in days.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            tmdb_id = _int(((entry.get("show") or {}).get("ids") or {}).get("tmdb"))
            season, number = _int(entry.get("season_number")), _int(entry.get("number"))
            if not tmdb_id or season is None or number is None:
                continue
            at = entry.get("air_datetime")
            air_day = local_air_date(at) if at else None
            if air_day is None:  # no air moment: the day it is filed under
                try:
                    air_day = datetime.date.fromisoformat(str(day)[:10])
                except ValueError:
                    continue
            if first_day <= air_day <= last_day:
                rows.setdefault((tmdb_id, season, number), {
                    "tmdb_id": tmdb_id, "season": season, "number": number, "air_at": at or "", "air_day": air_day,
                })
    return sorted(rows.values(), key=lambda row: (row["air_at"] or str(row["air_day"]), row["tmdb_id"], row["season"], row["number"]))


def following_episodes(first_day, last_day, api=None):
    """Rows (parse_calendar) of the episodes of the shows the account follows that air, by local day, in first_day .. last_day (dates),
    oldest first. A request that fails contributes nothing (logged): [] may therefore also mean a failure, and callers keep no empty answer."""
    if api is None:
        from resources.lib.indexers.wetrakr import WeTrakrAPI

        api = WeTrakrAPI()
    found = {}
    for start, end in _spans(first_day, last_day):
        data = api.get_json(CALENDAR_PATH, {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d"), "mode": "following"})
        if data is None:
            g.log(f"WeTrakr calendar: {start} .. {end} could not be read", "warning")
            continue
        for row in parse_calendar(data, first_day, last_day):
            found.setdefault((row["tmdb_id"], row["season"], row["number"]), row)
    return sorted(found.values(), key=lambda row: (row["air_at"] or str(row["air_day"]), row["tmdb_id"], row["season"], row["number"]))


def planning_movie_ids(api=None):
    """TMDb ids of the movies on the account's plan to watch list, newest first ([] when the list could not be read completely)"""
    if api is None:
        from resources.lib.indexers.wetrakr import WeTrakrAPI

        api = WeTrakrAPI()
    ok, rows = api.get_list("/sync/tracking/planning/movies")
    if not ok:
        g.log("WeTrakr calendar: the plan to watch movies could not be read", "warning")
        return []
    from resources.lib.gui.wetrakrMenus import status_ids

    return status_ids(rows)


def episode_key(row):
    """(Trakt show id, season, episode) of a formatted episode row, or None when one is missing"""
    info = row.get("info") or {}
    key = (info.get("trakt_show_id"), info.get("season"), info.get("episode"))
    return None if None in key else key


def episode_items(rows, taken_keys, shows_db):
    """Episode rows (the shape get_mixed_episode_list() returns) for WeTrakr calendar rows: the shows are resolved through Trakt by TMDb id
    (at most UNION_CAP of them, in the order of the rows) and each row picks the Trakt episode with the same season and number. An episode
    whose (Trakt show id, season, number) is in taken_keys is left out (the caller has it already: Trakt's own calendar, or an earlier layer).
    A show or an episode Trakt does not know is skipped (logged by the resolve)."""
    if not rows:
        return []

    from resources.lib.gui.wetrakrMenus import Menus as WeTrakrMenus
    from resources.lib.modules.calendar_data import _format_picks  # pylint: disable=protected-access

    wanted = {}  # show tmdb id -> {(season, number)}, in the order the shows first appear
    for row in rows:
        wanted.setdefault(row["tmdb_id"], set()).add((row["season"], row["number"]))
    show_ids = list(wanted)[:UNION_CAP]
    if len(wanted) > len(show_ids):
        g.log(f"WeTrakr calendar: union cap reached, dropped {len(wanted) - len(show_ids)} show(s) this fetch", "warning")

    taken = set(taken_keys or ())
    seen_shows = set()
    picks = []  # (skeleton row, show tmdb id)
    for show in WeTrakrMenus()._resolve(show_ids, "show"):  # pylint: disable=protected-access
        trakt_show_id, tmdb_id = show.get("trakt_id"), _int(show.get("tmdb_id"))
        if not trakt_show_id or trakt_show_id in seen_shows:
            continue
        if tmdb_id not in wanted:  # Trakt answered with the show it keeps under another TMDb id: the rows cannot be matched to it
            g.log(f"WeTrakr calendar: Trakt answered TMDb id {tmdb_id}, which no row asked for, skipping it", "debug")
            continue
        seen_shows.add(trakt_show_id)
        numbers = wanted[tmdb_id]
        picks.extend(
            (episode, tmdb_id)
            for episode in shows_db.get_episode_skeleton_for_pick(trakt_show_id)
            if (episode.get("db_season"), episode.get("db_number")) in numbers
            and (trakt_show_id, episode.get("db_season"), episode.get("db_number")) not in taken
        )
    items = []
    for item in _format_picks(shows_db, picks):
        item.pop("_source_tmdb_id", None)  # the tag only serves the Simkl / MDBList rating lookups
        key = episode_key(item)
        if key is not None:
            if key in taken:  # the formatted numbering differs from the stored one and Trakt's own row is the same episode
                continue
            taken.add(key)
        items.append(item)
    return items


def _tmdb_ids_of_shows(trakt_show_ids, shows_db=None):
    """{Trakt show id: TMDb id} from the shows Seren keeps (one query per 400 ids); a show without a TMDb id is left out"""
    if shows_db is None:
        from resources.lib.database.trakt_sync.shows import TraktSyncDatabase as ShowsDatabase

        shows_db = ShowsDatabase()
    ids = list(trakt_show_ids)
    found = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        for row in shows_db.fetchall(f"SELECT trakt_id, tmdb_id FROM shows WHERE trakt_id IN ({','.join('?' * len(chunk))})", tuple(chunk)):
            if _int(row["tmdb_id"]):
                found[row["trakt_id"]] = _int(row["tmdb_id"])
    return found


def mark_watched(items, shows_db=None):
    """The watched tick of the calendars for what only WeTrakr knows (3.9.180). Every episode row that no service ticks yet (no play_count,
    no info playcount) and that WeTrakr's local copy holds as watched gets WATCHED_MARK = True. It adds a tick, never removes one, and writes
    nothing but that mark (play_count stays what Trakt, or a sync into it, said, so the context menus offer what Trakt holds). The key is
    (the show's TMDb id, season, episode) as the row carries it; the show's TMDb id comes from the row, else from the shows Seren keeps.
    One query for all the shows of the rows. Returns how many rows were marked; a failure is logged and leaves the rows as they were."""
    try:
        if not items or not active():
            return 0
        pending = []  # (row, season, episode, show TMDb id or None, Trakt show id)
        for row in items:
            if not row or row.get(WATCHED_MARK):
                continue
            info = row.get("info") or {}
            if row.get("play_count") or info.get("playcount"):
                continue
            season, number = _int(info.get("season")), _int(info.get("episode"))
            if season is None or number is None:
                continue
            pending.append((row, season, number, _int(info.get("tmdb_show_id") or row.get("tmdb_show_id")), info.get("trakt_show_id") or row.get("trakt_show_id")))
        missing = {trakt_id for _, _, _, tmdb_id, trakt_id in pending if not tmdb_id and trakt_id}
        known = _tmdb_ids_of_shows(missing, shows_db) if missing else {}
        pending = [(row, season, number, tmdb_id or known.get(trakt_id)) for row, season, number, tmdb_id, trakt_id in pending]
        show_ids = {tmdb_id for _, _, _, tmdb_id in pending if tmdb_id}
        if not show_ids:
            return 0
        from resources.lib.database.wetrakr_sync import WeTrakrSyncDatabase

        watched = WeTrakrSyncDatabase().get_watched_episode_keys(show_ids)
        marked = 0
        for row, season, number, tmdb_id in pending:
            if tmdb_id and (tmdb_id, season, number) in watched:
                row[WATCHED_MARK] = True
                marked += 1
        return marked
    except Exception as exc:  # pylint: disable=broad-except
        g.log(f"WeTrakr calendar: the watched marks could not be added: {exc}", "warning")
        return 0
