"""Recent / Upcoming Episodes for the MDBList and Simkl menus (3.9.136).

Trakt's "Recent Episodes" and "Upcoming Episodes" use calendars/my/shows, an account-scoped calendar that MDBList and
Simkl do not have. Here the candidate shows come from the service itself (recently watched, watchlist / watching and
plan to watch), are resolved to Trakt shows by the menu, and every episode of each show whose air date falls inside the
window is listed - the same windows as Trakt's menus (recent: today minus 13 days through today; upcoming: tomorrow for
30 days). Each show costs one get_episode_skeleton() (cold shows bootstrap their episode list once), so the walk is bounded
by CANDIDATE_CAP shows and TIME_BUDGET seconds, checked before each show."""

import datetime
import time

from resources.lib.modules.globals import g

CANDIDATE_CAP = 25
TIME_BUDGET = 25.0


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def local_air_date(air_date):
    """Local calendar date of an episode air date (stored as naive UTC, e.g. 2026-11-15T02:00:00; Trakt's own format ends in Z;
    a bare date stays as it is), or None."""
    if not air_date:
        return None
    text = str(air_date)
    try:
        moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None and "T" in text:
            # Seren stores episodes.air_date as naive UTC (2026-11-15T02:00:00): read it as UTC, not as local time
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        if moment.tzinfo is not None:
            moment = moment.astimezone().replace(tzinfo=None)
        return moment.date()
    except ValueError:
        try:
            return datetime.date.fromisoformat(text[:10])
        except ValueError:
            return None


def candidate_ids(*groups):
    """Concatenates tmdb id groups (most relevant first), keeps the first occurrence of each id, caps at CANDIDATE_CAP."""
    seen = {}
    for group in groups:
        for value in group or []:
            tmdb_id = _to_int(value)
            if tmdb_id is not None:
                seen.setdefault(tmdb_id, None)
    ids = list(seen)
    if len(ids) > CANDIDATE_CAP:
        g.log(f"service episode lists: {len(ids) - CANDIDATE_CAP} candidate show(s) beyond the {CANDIDATE_CAP} cap not used", "debug")
    return ids[:CANDIDATE_CAP]


def order_like(resolved, tmdb_ids):
    """Trakt objects in the order of tmdb_ids: _resolve() returns them in arrival order. An object whose Trakt tmdb_id
    differs from the queried one (legitimate, see mdblistMenus.next_up) goes last."""
    rank = {}
    for index, tmdb_id in enumerate(tmdb_ids):
        rank.setdefault(_to_int(tmdb_id), index)
    return sorted(resolved, key=lambda item: rank.get(_to_int(item.get("tmdb_id")), len(rank)))


def watched_episode_items(trakt_api, rows):
    """(show_tmdb_id, season, number) rows, already in display order -> episode objects for mixed_episode_builder
    (shows resolved through Trakt's tmdb lookup once each). Rows Trakt cannot resolve are skipped."""
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase as ShowsDatabase

    shows_db = ShowsDatabase()
    trakt_shows = {}
    episodes = []
    for tmdb_id, season, number in rows:
        if tmdb_id not in trakt_shows:
            trakt_shows[tmdb_id] = trakt_api.search_by_tmdb_id(tmdb_id, "show")
        trakt_show = trakt_shows[tmdb_id]
        if not trakt_show or not trakt_show.get("trakt_id"):
            g.log(f"service episode lists: no Trakt match for tmdb_id {tmdb_id} (show), skipping watched episode", "debug")
            continue
        episode = shows_db.get_episode_action_args(trakt_show["trakt_id"], season, number)
        if episode:
            episodes.append(episode)
    return episodes


def mdblist_watchlist_show_ids():
    """Fallback when mdblistMembership.db has no watchlist yet (first sync not run): bridgeSync's paginated reader."""
    try:
        from resources.lib.modules.bridgeSync import _read_mdblist_watchlist  # pylint: disable=protected-access

        return [value for key, value in _read_mdblist_watchlist().items() if key.startswith("show:")]
    except Exception as e:  # best effort, never break the menu
        g.log(f"service episode lists: MDBList watchlist read failed: {e}", "warning")
        return []


def episodes_in_window(shows, start, end, newest_first, hidden_show_ids=()):
    """Every episode of the given Trakt shows with a local air date from start through end (dates), sorted by air date.
    Returns clean {trakt_id, trakt_show_id} dicts for ListBuilder.mixed_episode_builder."""
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase as ShowsDatabase
    from resources.lib.modules.calendar_data import get_air_date

    shows_db = ShowsDatabase()
    found = []
    seen = set()
    started = time.monotonic()
    examined = 0
    for show in shows:
        trakt_show_id = show.get("trakt_id")
        if not trakt_show_id or trakt_show_id in seen or trakt_show_id in hidden_show_ids:
            continue
        seen.add(trakt_show_id)
        if examined and time.monotonic() - started > TIME_BUDGET:
            g.log(f"service episode lists: stopped after {examined} of {len(shows)} shows, over the {TIME_BUDGET:.0f}s budget", "debug")
            break
        examined += 1
        for episode in shows_db.get_episode_skeleton(trakt_show_id) or []:
            if shows_db.hide_specials and episode.get("db_season") == 0:
                continue
            air_date = get_air_date(episode)
            day = local_air_date(air_date)
            if day is None or day < start or day > end:
                continue
            found.append((str(air_date), {"trakt_id": episode["trakt_id"], "trakt_show_id": episode["trakt_show_id"]}))
    found.sort(key=lambda pair: pair[0], reverse=newest_first)
    g.log(f"service episode lists: {examined} shows examined, {len(found)} episodes between {start} and {end}", "debug")
    return [pick for _, pick in found]
