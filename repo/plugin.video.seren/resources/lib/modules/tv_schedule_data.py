"""TV Schedule data: one calendar week of episodes airing globally (region/
language filtered via the shared calendar.region/calendar.language settings
Movie Calendar also uses), unioned with the user's watchlisted/currently-
watching shows for that week - the watchlist layer is always included
regardless of the region/language filter, per design.

Both layers converge on the same normalized episode-row shape Seren already
uses everywhere (info/art/args/cast, trakt_id) via
ShowsDatabase.get_mixed_episode_list()/get_episode_list() - never a separate,
self-contained cache. Playback (tools.get_item_information()) hard-requires
an episode to exist in the shared trakt_sync episodes table by trakt_id, so
the global layer bootstraps into that shared table exactly like
movie_calendar_data.py already does for movies. TvScheduleDatabase (see
resources/lib/database/tvSchedule) only tracks which (episode, week) pairs
were already fetched, so eviction there never touches data other features
(My Shows, sync, tracking) depend on.

The "core window" (last/current/next real week) needs no special refresh
logic - it's an emergent property of every read going through
get_mixed_episode_list(), which re-validates staleness against Trakt's own
metadata-cache TTL on every call, same as calendar_data.py's existing
Trakt-calendar path.

The month/week-independent part of the watchlist layer - which shows the user's Simkl plan-to-watch and MDBList watchlist hold,
those shows resolved on Trakt, and their Simkl/MDBList ratings - is kept in memory too (3.9.161, _get_union_layer()): it cost about
0.4 s of every week-boundary press. The Trakt "my calendar" answer of a week is kept too once the rolling prefetch has asked for it
(3.9.165, _fetch_my_calendar()); its hydration and the pick of the episodes that air in the week are still done for each week.

Dedupe key is (trakt_show_id, season, episode) - NOT show id alone. The
global layer can carry every episode of a show airing that week; the
watchlist layer's Simkl/MDBList-only resolution (_pick_episodes_in_week)
does the same for consistency, unlike calendar_data.py's own
_pick_relevant_episode() (single nearest-to-today episode), which answers a
different question and would silently drop a real second airing here.
"""
import copy
import datetime
from functools import lru_cache
import threading
import time

import xbmc

from resources.lib.modules.globals import g

_GLOBAL_ENDPOINT = "calendars/all/shows/{start_date}/{days}"
_WATCHLIST_ENDPOINT = "calendars/my/shows/{start_date}/{days}"
_UNION_CAP = 25  # matches calendar_data.py's own cap - same per-show live-resolve cost

_fetch_locks_guard = threading.Lock()
_fetch_locks = {}  # week_start (YYYY-MM-DD) -> threading.Lock, created on first use, never evicted

# The hydrated global layer of a week, kept in memory so that paging into a week the rolling prefetch already built does not hydrate it
# again on the main thread (3.9.151). Valid only while sync_stamp() (calendar_stamp.py) is the value read before it was built and for
# _MEMO_SECONDS; the window throws it all away with clear_memo() wherever it throws its own weeks away (an action in the context menu
# changes watched data without moving that stamp). _memo_generation makes a prefetch that started before a clear_memo() unable to store its
# older rows afterwards. A stamp of None (unreadable) never stores or serves anything: that is the behaviour before 3.9.151.
_MEMO_SECONDS = 600
_MEMO_MAX = 4
_memo_lock = threading.Lock()
_memo = {}  # week_start (YYYY-MM-DD) -> (time stored, stamp, rows)
_memo_generation = 0


def _copy_rows(rows):
    """Rows and their nested dicts copied one level: get_schedule_week() annotates rows in place and a caller may keep them"""
    return [{k: (dict(v) if isinstance(v, dict) else v) for k, v in r.items()} for r in rows]


def _memo_fresh(week_start_str, stamp):
    if stamp is None:
        return False
    with _memo_lock:
        entry = _memo.get(week_start_str)
        return entry is not None and time.time() - entry[0] < _MEMO_SECONDS and entry[1] == stamp


def _memo_get(week_start_str, stamp):
    if stamp is None:
        return None
    with _memo_lock:
        entry = _memo.get(week_start_str)
        if entry is not None and (time.time() - entry[0] >= _MEMO_SECONDS or entry[1] != stamp):
            _memo.pop(week_start_str, None)
            entry = None
        rows = entry[2] if entry is not None else None
    if rows is None:
        return None
    g.log(f"tv_schedule_data: global layer of week {week_start_str} served from memory ({len(rows)} rows)", "debug")
    return _copy_rows(rows)


def _memo_put(week_start_str, stamp, generation, rows):
    if stamp is None:
        return
    copied = _copy_rows(rows)
    with _memo_lock:
        if generation != _memo_generation:  # clear_memo() ran while this was being built: these rows may predate what it was called for
            return
        _memo[week_start_str] = (time.time(), stamp, copied)
        while len(_memo) > _MEMO_MAX:
            _memo.pop(min(_memo, key=lambda k: _memo[k][0]))
    g.log(f"tv_schedule_data: global layer of week {week_start_str} kept in memory ({len(rows)} rows)", "debug")


# The week-independent part of the watchlist layer (3.9.161): [time stored, sync stamp, providers key, capped tmdb ids, simkl id by tmdb id,
# resolved shows, ratings or None]. Read and written on the main thread only (SimklMenus()._resolve() is never run off it); the rolling
# prefetch never touches it. The ratings are fetched the first time a week needs them (only a week with a Simkl/MDBList-only show does).
_union = None


# The Trakt "my calendar" answer of a week (3.9.165): the one request still left in a week-boundary press. The rolling prefetch asks for it
# in the background for the adjacent weeks, so that the press finds it here. Same rules as the kept global layer: valid for _MEMO_SECONDS
# under the sync stamp it was fetched with, dropped by clear_memo(), never stored for a stamp of None, never stored when empty (an empty
# answer may be a failed request: fetch_my_calendar_days() turns a failure into no rows) and handed out as a copy.
_my_calendar = {}  # week_start (YYYY-MM-DD) -> (time stored, stamp, rows)


def clear_memo():
    """Forget every kept week, the kept watchlist-union layer and the kept Trakt calendar answers (the window calls this when it drops its own weeks)"""
    global _memo_generation, _union
    with _memo_lock:
        _memo_generation += 1
        _memo.clear()
        _my_calendar.clear()
        _union = None


def _my_calendar_fresh(week_start_str, stamp):
    if stamp is None:
        return False
    with _memo_lock:
        entry = _my_calendar.get(week_start_str)
        return entry is not None and time.time() - entry[0] < _MEMO_SECONDS and entry[1] == stamp


def _my_calendar_get(week_start_str, stamp):
    if stamp is None:
        return None
    with _memo_lock:
        entry = _my_calendar.get(week_start_str)
        if entry is not None and (time.time() - entry[0] >= _MEMO_SECONDS or entry[1] != stamp):
            _my_calendar.pop(week_start_str, None)
            entry = None
        rows = entry[2] if entry is not None else None
    if rows is None:
        return None
    g.log(f"tv_schedule_data: Trakt calendar of week {week_start_str} served from memory ({len(rows)} rows)", "debug")
    return copy.deepcopy(rows)


def _fetch_my_calendar(week_start_str, stamp):
    """Trakt's calendars/my/shows rows that air in the week (calendar_data.fetch_my_calendar_days), from memory when the prefetch (or an earlier
    visit) kept them. A per-week lock of its own, so that a press and the prefetch asking for the same week do not both go to Trakt."""
    from resources.lib.modules.calendar_data import fetch_my_calendar_days
    from resources.lib.indexers.trakt import TraktAPI

    with _fetch_lock("my:" + week_start_str):
        kept = _my_calendar_get(week_start_str, stamp)
        if kept is not None:
            return kept
        with _memo_lock:
            generation = _memo_generation
        week_start = datetime.datetime.strptime(week_start_str, "%Y-%m-%d").date()
        raw = fetch_my_calendar_days(TraktAPI(), week_start, 7)
        if raw and stamp is not None:
            copied = copy.deepcopy(raw)
            with _memo_lock:
                if generation == _memo_generation:  # clear_memo() ran during the request: these rows may predate what it was called for
                    _my_calendar[week_start_str] = (time.time(), stamp, copied)
                    while len(_my_calendar) > _MEMO_MAX:
                        _my_calendar.pop(min(_my_calendar, key=lambda k: _my_calendar[k][0]))
            g.log(f"tv_schedule_data: Trakt calendar of week {week_start_str} kept in memory ({len(raw)} rows)", "debug")
    return raw


def _warm_my_calendar(week_start_str, stamp):
    """Prefetch side of _fetch_my_calendar(): only for a Trakt user, only when it is not kept already. Trakt only: the Simkl/MDBList part of the
    watchlist layer is never touched off the main thread."""
    from resources.lib.modules.syncGateway import configured_providers

    if "trakt" not in configured_providers() or _my_calendar_fresh(week_start_str, stamp):
        return
    _fetch_my_calendar(week_start_str, stamp)


def _fetch_lock(week_start_str):
    """Per-week lock so a background rolling-prefetch timer (see
    show_calendar_window.py) and a foreground navigation to the same week can't
    both see a cache miss and both fetch Trakt live. The second caller blocks
    here, then re-checks TvScheduleDatabase after acquiring - the existing
    `cached is None` branches in _get_global_layer() already do the right
    thing once that re-check happens inside the lock. Mirrors
    movie_calendar_data.py's own _fetch_lock()."""
    with _fetch_locks_guard:
        lock = _fetch_locks.get(week_start_str)
        if lock is None:
            lock = threading.Lock()
            _fetch_locks[week_start_str] = lock
        return lock


def prefetch_schedule_week(week_start, stamp=None):
    """Background-safe entry point for the rolling-prefetch timer (see
    show_calendar_window.py) - warms TvScheduleDatabase's bookkeeping for
    week_start via the same self-contained _get_global_layer()
    get_schedule_week() itself uses, without ever touching
    _get_watchlist_layer()'s SimklMenus()._resolve() call. That call is never
    exercised off the main thread anywhere in this codebase, and the
    watchlist layer it resolves is only kept for the foreground (see module
    docstring and _get_union_layer()), so skipping it here loses nothing. Return
    value discarded - callers only care about the
    TvScheduleDatabase.mark_week_fetched() side effect inside
    _get_global_layer().

    Pre-checks the cache before calling _get_global_layer() at all - on a hit,
    that call would still run the full get_mixed_episode_list() hydration pass
    (discarded here anyway) while holding _fetch_lock(week_start_str), the same
    lock a foreground get_schedule_week() to this week would block on. Steady-
    state rolling prefetch hits this branch often: one of the two adjacent
    weeks is usually the one just left. This check is outside the lock, so a
    near-simultaneous write can narrowly miss it - harmless, since
    _get_global_layer()'s own in-lock cache re-check still prevents a
    duplicate live Trakt call either way; this is purely a fast-path skip of
    the redundant hydration pass, mirrors movie_calendar_data.py's own
    `if prefetch and cached is not None: return []` guard."""
    from resources.lib.database.tvSchedule import TvScheduleDatabase

    week_start_str = week_start.strftime("%Y-%m-%d")
    week_end_str = (week_start + datetime.timedelta(days=6)).strftime("%Y-%m-%d")
    if stamp is not None:
        # 3.9.151: also build a week whose bookkeeping is cached - the rows are kept in memory for the window (stamp = the window's
        # sync_stamp(), a plain tuple handed over by the timer: this thread does not read the sync databases itself)
        if not _memo_fresh(week_start_str, stamp):
            _get_global_layer(week_start_str, week_end_str, stamp)
        _warm_my_calendar(week_start_str, stamp)  # 3.9.165: the week's Trakt "my calendar" answer too
        return
    if TvScheduleDatabase().get_week(week_start_str) is not None:
        return
    _get_global_layer(week_start_str, week_end_str)


def get_schedule_week(week_start):
    """week_start: datetime.date, the Sunday starting the target week.
    Returns one chronologically-sorted list of normalized episode rows."""
    from resources.lib.modules.syncGateway import configured_providers

    if not configured_providers():
        return []

    week_start_str = week_start.strftime("%Y-%m-%d")
    week_end_str = (week_start + datetime.timedelta(days=6)).strftime("%Y-%m-%d")

    try:
        from resources.lib.modules.calendar_stamp import sync_stamp

        stamp = sync_stamp()
    except Exception:
        stamp = None
    global_rows = _get_global_layer(week_start_str, week_end_str, stamp)
    watchlist_rows = _get_watchlist_layer(week_start_str, week_end_str, stamp)

    # watchlist-sourced rows win on conflict - both layers hydrate through the
    # same get_mixed_episode_list()/get_episode_list() calls, so the row shape
    # is identical either way; this only decides which layer resolved a given
    # episode first when both cover it.
    merged = {}
    for row in global_rows:
        merged[_episode_key(row)] = row
    for row in watchlist_rows:
        merged[_episode_key(row)] = row
    merged.pop(None, None)  # a row with no usable key can't be deduped - drop it

    # A row's air date can drift from the week it was fetched/bucketed under -
    # a rescheduled episode re-hydrated from a stale global-layer bookkeeping row,
    # for instance. get_schedule_week()'s contract is "episodes airing this week",
    # so enforce it here rather than let a caller's day-of-week bucketing (7 fixed
    # columns) index out of range on a row that doesn't actually belong.
    result = [r for r in merged.values() if week_start_str <= (get_air_date(r) or "")[:10] <= week_end_str]
    for row in result:
        _annotate(row)
    result.sort(key=lambda r: (get_air_date(r) or "", (r.get("info") or {}).get("tvshowtitle") or ""))
    return result


def shift_week(week_start, delta):
    return week_start + datetime.timedelta(weeks=delta)


def retention_cutoff():
    """Oldest week_start still worth keeping fetched: cleanup() drops anything
    older than this.

    Widened from -2 to -3 weeks alongside the rolling-prefetch timer (see
    show_calendar_window.py's _schedule_prefetch()): once landing on any week W
    triggers a background prefetch of W-1, a user just 2 real weeks back from
    today (still inside the old -2 retention edge) would have that backward
    prefetch reach 3 weeks back - one week past the old cutoff, and
    immediately eligible for the next cleanup() pass to delete before it's
    ever read from cache. The extra week of margin protects exactly that
    prefetch reach; matches movie_calendar_data.py's own retention_cutoff(),
    which was already sized this way in Step 1 (see its docstring)."""
    return week_start_for(datetime.date.today() - datetime.timedelta(weeks=3)).strftime("%Y-%m-%d")


def week_start_for(date_):
    """Sunday-anchored week start containing date_ - matches the approved
    mockup's paging (left lands on Saturday = end of week, right lands on
    Sunday = start of week)."""
    days_since_sunday = (date_.weekday() + 1) % 7  # Python weekday(): Monday=0..Sunday=6
    return date_ - datetime.timedelta(days=days_since_sunday)


def get_air_date(episode):
    """Same accessor calendar_data.py uses - top-level air_date, then
    info.aired, then info.premiered."""
    info = episode.get("info") or {}
    return episode.get("air_date") or info.get("aired") or info.get("premiered")


def _episode_key(row):
    info = row.get("info") or {}
    show_id, season, number = info.get("trakt_show_id"), info.get("season"), info.get("episode")
    if show_id is None or season is None or number is None:
        return None
    return show_id, season, number


def _get_global_layer(week_start_str, week_end_str, stamp=None):
    from resources.lib.database.tvSchedule import TvScheduleDatabase
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase as ShowsDatabase

    db = TvScheduleDatabase()
    shows_db = ShowsDatabase()

    with _fetch_lock(week_start_str):
        # inside the lock: a caller that waited for a prefetch of this week now finds what that prefetch kept
        kept = _memo_get(week_start_str, stamp)
        if kept is not None:
            return kept
        with _memo_lock:
            generation = _memo_generation
        cached = db.get_week(week_start_str)

        if cached is None:
            from resources.lib.indexers.trakt import TraktAPI

            # Server-side filter, same params confirmed live against Movie Calendar's
            # own endpoint - blank setting omits the param, matching an unfiltered fetch.
            filters = {}
            region = g.get_setting("calendar.region")
            if region:
                filters["countries"] = region
            language = g.get_setting("calendar.language")
            if language:
                filters["languages"] = language
            raw = TraktAPI().get_json(
                _GLOBAL_ENDPOINT.format(start_date=week_start_str, days=7),
                extended="full",
                pull_all=True,
                **filters,
            ) or []
            trakt_items = raw
        else:
            trakt_items = [{"trakt_id": r["trakt_episode_id"], "trakt_show_id": r["trakt_show_id"]} for r in cached]

        hydrated = shows_db.get_mixed_episode_list(trakt_items, hide_unaired=False, hide_watched=False) if trakt_items else []
        hydrated = [i for i in hydrated if i is not None]

        if cached is None:
            # Trakt's calendar endpoint can return episodes whose real air date falls
            # outside the requested [start_date, start_date+days) window (confirmed live:
            # calendars/all/shows/2026-08-09/7 included episodes dated 2026-08-16/17).
            # schedule_episodes' primary key is trakt_episode_id alone, so bookkeeping a
            # boundary episode under this week here would let a later fetch of its real
            # week get INSERT OR REPLACE'd away the moment this adjacent week is fetched
            # again. Filter against the same hydrated air_date the render path trusts,
            # not the pre-hydration raw item (which normalizes down to bare id fields
            # with no usable date - see get_schedule_week()'s own module docstring).
            in_week = [r for r in hydrated if week_start_str <= (get_air_date(r) or "")[:10] <= week_end_str]
            db.mark_week_fetched(
                week_start_str,
                [
                    (r["trakt_id"], (r.get("info") or {}).get("trakt_show_id"))
                    for r in in_week
                    if r.get("trakt_id") and (r.get("info") or {}).get("trakt_show_id")
                ],
            )

        _memo_put(week_start_str, stamp, generation, hydrated)

    return hydrated


def _get_union_layer(providers, stamp):
    """The week-independent part of the watchlist layer: [time, stamp, providers key, capped tmdb ids, simkl id by tmdb id, resolved shows,
    ratings or None]. Kept for _MEMO_SECONDS under the sync stamp it was read with (a stamp that cannot be read keeps nothing), main thread
    only. Nothing is kept when there is no candidate (an empty answer may be a failed lookup). The returned entry is shared: callers read it and
    only _get_union_ratings() adds the ratings. Never raises because of the memo."""
    global _union
    from resources.lib.modules.calendar_data import _simkl_plantowatch_tmdb_ids, _mdblist_watchlist_tmdb_ids

    key = tuple(sorted(providers))
    with _memo_lock:
        generation = _memo_generation
        kept = _union
        if stamp is not None and kept is not None and kept[1] == stamp and kept[2] == key and time.time() - kept[0] < _MEMO_SECONDS:
            g.log("tv_schedule_data: watchlist-union layer served from memory", "debug")
            return kept

    candidate_tmdb_ids = {}  # dict (not set) purely to preserve first-seen order for the cap
    simkl_id_by_tmdb = {}
    if "simkl" in providers:
        for tmdb_id, simkl_id in _simkl_plantowatch_tmdb_ids():
            candidate_tmdb_ids.setdefault(tmdb_id, None)
            if simkl_id is not None:
                simkl_id_by_tmdb.setdefault(tmdb_id, simkl_id)
    if "mdblist" in providers:
        for tmdb_id in _mdblist_watchlist_tmdb_ids():
            candidate_tmdb_ids.setdefault(tmdb_id, None)

    capped_ids = list(candidate_tmdb_ids)[:_UNION_CAP]
    dropped = len(candidate_tmdb_ids) - len(capped_ids)
    if dropped > 0:
        g.log(f"tv_schedule_data: union cap reached, dropped {dropped} Simkl/MDBList-only show(s) this fetch", "warning")

    resolved_shows = _resolve_union_shows(capped_ids)
    layer = [time.time(), stamp, key, capped_ids, simkl_id_by_tmdb, resolved_shows, None]
    if stamp is not None and capped_ids:
        with _memo_lock:
            if generation == _memo_generation:  # clear_memo() ran during the load: these may predate what it was called for
                _union = layer
    return layer


def _resolve_union_shows(capped_ids):
    if not capped_ids:
        return []

    from resources.lib.gui.simklMenus import Menus as SimklMenus

    return SimklMenus()._resolve(capped_ids, "show")


def _get_union_ratings(layer, providers):
    """(MDBList ratings, Simkl ratings) of the layer's candidates: fetched the first time a week needs them, then kept with the layer"""
    if layer[6] is None:
        from resources.lib.modules.calendar_ratings import fetch_union_ratings

        layer[6] = fetch_union_ratings(layer[3], layer[4], "show", providers)
    return layer[6]


def _get_watchlist_layer(week_start_str, week_end_str, stamp=None):
    from resources.lib.modules.syncGateway import configured_providers
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase as ShowsDatabase
    from resources.lib.database.trakt_sync.hidden import TraktSyncDatabase as HiddenDatabase

    providers = configured_providers()

    if "trakt" in providers:
        # By each episode's own air day (3.9.144): Trakt's calendar index can file an episode a week away from the
        # day it airs, so the plain 7-day request missed episodes that belong to this week. Kept in memory when the
        # prefetch already asked for it (3.9.165, _fetch_my_calendar()).
        raw = _fetch_my_calendar(week_start_str, stamp)
    else:
        raw = []

    hidden_shows = HiddenDatabase().get_hidden_items("calendar", "tvshow")
    raw = [i for i in raw if i.get("trakt_show_id") not in hidden_shows]
    covered_show_ids = {i["trakt_show_id"] for i in raw if i.get("trakt_show_id")}

    shows_db = ShowsDatabase()
    trakt_items = shows_db.get_mixed_episode_list(raw, hide_unaired=False, hide_watched=False) if raw else []
    trakt_items = [i for i in trakt_items if i is not None]

    layer = _get_union_layer(providers, stamp)
    extra_items = _pick_extra_shows_in_week(layer[5], covered_show_ids, shows_db, week_start_str, week_end_str)
    _annotate_union_ratings(extra_items, layer, providers)
    return trakt_items + extra_items


def _annotate_union_ratings(extra_items, layer, providers):
    """Attaches _schedule_rating_override onto each Simkl/MDBList-union
    episode (never onto the global/watchlist Trakt-sourced rows - those never
    go through _pick_extra_shows_in_week(), so they never carry a
    _source_tmdb_id tag to match against). _annotate() prefers this over
    info["rating"] when present; mirrors calendar_data.py's own
    _annotate_union_ratings(), adapted for this module's per-week candidate
    set instead of a fixed recent/upcoming window."""
    if not extra_items:
        return

    from resources.lib.modules.calendar_ratings import pick_best_rating

    mdblist_ratings, simkl_ratings = _get_union_ratings(layer, providers)
    if not mdblist_ratings and not simkl_ratings:
        return

    for item in extra_items:
        tmdb_id = item.pop("_source_tmdb_id", None)
        rating = pick_best_rating(simkl_ratings.get(tmdb_id), mdblist_ratings.get(tmdb_id), None)
        if rating is not None:
            item["_schedule_rating_override"] = rating


def _pick_extra_shows_in_week(resolved_shows, covered_show_ids, shows_db, week_start_str, week_end_str):
    """The episodes of the resolved Simkl/MDBList-only shows that air in the week (per week: reads the local episode table)"""
    if not resolved_shows:
        return []

    from resources.lib.modules.calendar_data import _format_picks

    seen_show_ids = set(covered_show_ids)
    picks = []  # (skeleton row, source tmdb_id)
    for show in resolved_shows:
        trakt_show_id = show.get("trakt_id")
        if not trakt_show_id or trakt_show_id in seen_show_ids:
            continue
        seen_show_ids.add(trakt_show_id)
        # Pick from the bare episode rows and format only the ones in the week - get_episode_list() would
        # format and re-save every episode of the show on each call just to keep a handful.
        skeleton = shows_db.get_episode_skeleton_for_pick(trakt_show_id)
        picks.extend((e, show.get("tmdb_id")) for e in _pick_episodes_in_week(skeleton, week_start_str, week_end_str))
    return _format_picks(shows_db, picks)


def _pick_episodes_in_week(episodes, week_start_str, week_end_str):
    """All of a show's episodes (formatted or bare skeleton rows) whose air date falls within [week_start, week_end] -
    NOT calendar_data.py's _pick_relevant_episode() (single nearest-to-today episode),
    which answers "what's this show's one relevant row right now," not "what airs
    this specific week." """
    result = []
    for e in episodes:
        d = get_air_date(e)
        if d and week_start_str <= d[:10] <= week_end_str:
            result.append(e)
    return result


def _annotate(episode):
    info = episode.get("info") or {}
    released = get_air_date(episode)

    episode["_schedule_released"] = released
    episode["_schedule_date_label"] = _format_release_date(released)
    episode["_schedule_time_label"] = _format_release_time(released)

    rating_value = episode.get("_schedule_rating_override")
    if rating_value is None:
        rating_value = info.get("rating")
    episode["_schedule_rating_display"] = f"{rating_value:.1f}" if isinstance(rating_value, (int, float)) else ""

    episode["_schedule_status"] = "aired" if _is_released(released) else "upcoming"


def _is_released(released):
    if not released:
        return False
    return released < g.datetime_to_string(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))


@lru_cache(maxsize=1)
def _schedule_date_region():
    # Cached for the process lifetime - Kodi's system region format does not
    # change without a restart, same assumption movie_calendar_data.py's
    # sibling cache and locale_playback._stream_language_names() make.
    return xbmc.getRegion("dateshort")


@lru_cache(maxsize=1)
def _schedule_time_region():
    return xbmc.getRegion("time")


def _format_release_date(released):
    if not released:
        return ""
    try:
        from resources.lib.common import tools

        return tools.parse_datetime(released).strftime(_schedule_date_region())
    except (ValueError, TypeError):
        return released


def _format_release_time(released):
    if not released:
        return ""
    try:
        from resources.lib.common import tools

        return tools.parse_datetime(released, False).strftime(_schedule_time_region())
    except (ValueError, TypeError):
        return ""
