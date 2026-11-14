"""Prefetch: keep the stored torrent results (Tier 0) of the episodes the user will probably play next warm.

Three triggers, all queueing the existing silent torrent re-scrape (router action refreshTorrentCache, the one the stale
refresh uses) in its own interpreter, so a scrape never holds up the caller:
  * idle  - prefetch_next_up(), one step of cycleHousekeeping (every 13-17 min): the next-up episode of the most recently
            watched shows (Trakt; MDBList and Simkl when Trakt has nothing to queue). At most one show per cycle.
  * watching - prefetch_following_episode(), called by the player a minute into an episode: the episode after the playing
            one (into the next season at a season end).
Nothing here resolves or plays anything.
"""
import json
import time

import xbmc

from resources.lib.common import tools
from resources.lib.modules.globals import g

PREFETCH_SETTING = "scraping.prefetchNextUp"
PLAYBACK_SETTING = "scraping.prefetchWhilePlaying"
# How many of the most recently watched shows are looked at per cycle (the first one without stored results is queued).
PREFETCH_CANDIDATES = 3
# A show that was queued is left alone for this long, whether or not the scrape found anything.
PREFETCH_GUARD_SECONDS = 6 * 3600
# AlarmClock delays (mm:ss). Idle: short, the scrape re-checks "is a video playing / are results stored" itself when it
# starts. Watching: the player queues this a minute into the episode; the stale refresh runs at 01:30 after play start.
PREFETCH_DELAY = "00:30"
PLAYBACK_DELAY = "01:10"
# MDBList / Simkl candidates are only looked up when Trakt had nothing to queue, and at most this often when they
# found nothing either (each lookup is Trakt id searches, cached, plus MDBList's dropped list).
SERVICE_SCAN_INTERVAL = 3600
SERVICE_SHOW_CAP = 3
_MDBLIST_SCAN_CAP = 10
_SERVICE_TIME_BUDGET = 20.0
_DROPPED_CACHE_SECONDS = 6 * 3600
_DROPPED_KEY = "prefetch.mdblistDropped"
_SCAN_KEY = "prefetch.serviceScan"


def skip_reason(reason, playing, has_rows):
    """Decision made when a queued prefetch scrape starts: None = go ahead, else why it is skipped.
    reason 'prefetch' (idle) must not run while a video plays; 'prefetch_next' (queued by the player) is meant to."""
    if reason == "prefetch" and playing:
        return "playing"
    if has_rows:
        return "stored"
    return None


def _guarded(trakt_id, now):
    last = g.get_runtime_setting(f"prefetch.{trakt_id}")
    try:
        return bool(last) and now - float(last) < PREFETCH_GUARD_SECONDS
    except (TypeError, ValueError):
        return False


def _queue(action_args, delay, reason, alarm_prefix, now):
    trakt_id = action_args["trakt_id"]
    g.set_runtime_setting(f"prefetch.{trakt_id}", str(now))
    url = (
        f"plugin://{g.ADDON_ID}/?action=refreshTorrentCache&reason={reason}"
        f"&actionArgs={tools.construct_action_args(action_args)}"
    )
    g.log(f"Prefetch: queued trakt id {trakt_id} ({reason}), scraping in {delay}", "info")
    xbmc.executebuiltin(f'AlarmClock({alarm_prefix}.{trakt_id},RunPlugin("{url}"),{delay},silent)')


def _queue_first_cold(candidates, torrent_cache, now, seen):
    """candidates: iterable of episode action_args dicts (a generator is only advanced as far as needed). Queues the
    first one that is not guarded and has no stored results; returns its Trakt id or None."""
    for action_args in candidates:
        trakt_id = action_args.get("trakt_id")
        if not trakt_id or trakt_id in seen:
            continue
        seen.add(trakt_id)
        if _guarded(trakt_id, now):
            continue
        item_information = tools.get_item_information(action_args)
        if not item_information:
            continue
        if torrent_cache.get_torrents(item_information):
            g.log(f"Prefetch: trakt id {trakt_id} already has stored results - skipping", "debug")
            continue
        _queue(action_args, PREFETCH_DELAY, "prefetch", "seren.prefetch", now)
        return trakt_id
    return None


def _trakt_candidates():
    from resources.lib.database.trakt_sync import shows

    next_up = shows.TraktSyncDatabase().get_nextup_episodes(sort_by_last_watched=True)
    for row in next_up[:PREFETCH_CANDIDATES]:
        episode = row.get("episode")
        trakt_id = row.get("trakt_id")
        show_id = row.get("trakt_show_id")
        if episode is None or not trakt_id or not show_id:
            continue
        action_args = {"mediatype": "episode", "trakt_id": trakt_id, "trakt_show_id": show_id}
        season_id = ((episode.get("trakt_object") or {}).get("info") or {}).get("trakt_season_id")
        if season_id:
            action_args["trakt_season_id"] = season_id
        yield action_args


def _simkl_candidates():
    if not g.simkl_active():
        return
    from resources.lib.gui.simklMenus import _NEXT_TO_WATCH_REGEX, Menus

    menus = Menus()
    targets = []
    for row in menus.simkl_database.get_next_to_watch_shows():
        match = _NEXT_TO_WATCH_REGEX.match((row["next_to_watch"] or "").strip())
        if not match or int(match.group(1)) < 1:
            continue
        targets.append({"tmdb_id": row["tmdb_id"], "season": int(match.group(1)), "number": int(match.group(2))})
        if len(targets) >= SERVICE_SHOW_CAP:
            break
    if not targets:
        return
    for episode in menus._resolve_episode_rows(targets):  # Trakt-id lookups (cached) + local ids; hidden shows dropped
        yield {"mediatype": "episode", "trakt_id": episode["trakt_id"], "trakt_show_id": episode["trakt_show_id"]}


def _mdblist_dropped(menus, now):
    """MDBList's dropped show tmdb ids; the live call is repeated at most every 6 h (cached as a runtime string)."""
    try:
        cached = json.loads(g.get_runtime_setting(_DROPPED_KEY) or "null")
        if cached and now - cached["t"] < _DROPPED_CACHE_SECONDS:
            return set(cached["ids"])
    except (TypeError, ValueError, KeyError):
        pass
    ids = menus._get_dropped_tmdb_ids()
    g.set_runtime_setting(_DROPPED_KEY, json.dumps({"t": now, "ids": sorted(ids)}))
    return ids


def _mdblist_candidates(now):
    if not g.mdblist_active():
        return
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase
    from resources.lib.gui.mdblistMenus import Menus

    menus = Menus()
    recent_shows = menus.mdblist_database.get_recent_shows(_MDBLIST_SCAN_CAP)
    if not recent_shows:
        return
    watched_by_show = {}
    for row in menus.mdblist_database.get_all_watched_episode_tmdb_keys():
        watched_by_show.setdefault(row["show_tmdb_id"], set()).add((row["season"], row["number"]))
    dropped = _mdblist_dropped(menus, now)
    hidden_shows = menus.hidden_database.get_hidden_items("progress_watched", "tvshow")
    shows_db = TraktSyncDatabase()
    started = time.monotonic()
    picks = 0
    for row in recent_shows:
        if picks >= SERVICE_SHOW_CAP or time.monotonic() - started > _SERVICE_TIME_BUDGET:
            break
        tmdb_id = row["tmdb_id"]
        watched_set = watched_by_show.get(tmdb_id)
        if tmdb_id in dropped or not watched_set:
            continue
        trakt_show = menus.trakt_api.search_by_tmdb_id(tmdb_id, "show")
        if not trakt_show or not trakt_show.get("trakt_id") or trakt_show["trakt_id"] in hidden_shows:
            continue
        pick = menus._pick_next_unwatched(shows_db.get_episode_skeleton(trakt_show["trakt_id"]), watched_set)
        if pick:
            picks += 1
            yield {"mediatype": "episode", "trakt_id": pick["trakt_id"], "trakt_show_id": pick["trakt_show_id"]}


def _service_candidates(now):
    for source in (_simkl_candidates, _mdblist_candidates):
        try:
            yield from (source() if source is _simkl_candidates else source(now))
        except Exception as e:
            g.log(f"Prefetch: {source.__name__.strip('_')} failed: {e}", "warning")


def prefetch_next_up():
    """Idle prefetch: queue a silent scrape for the next-up episode of a recently watched show that has no stored
    results. Returns the queued episode's Trakt id or None."""
    if not g.get_bool_setting(PREFETCH_SETTING, True) or not g.get_bool_setting("general.torrentCache"):
        return None
    if xbmc.Player().isPlayingVideo():
        # Playing already covers its own next episode (prefetch_following_episode / pre-scrape); keep the connection for the stream.
        return None

    from resources.lib.database.torrentCache import TorrentCache

    torrent_cache = TorrentCache()
    now = time.time()
    seen = set()
    queued = _queue_first_cold(_trakt_candidates(), torrent_cache, now, seen)
    if queued or not (g.simkl_active() or g.mdblist_active()):
        return queued
    last_scan = g.get_runtime_setting(_SCAN_KEY)
    try:
        if last_scan and now - float(last_scan) < SERVICE_SCAN_INTERVAL:
            return None
    except (TypeError, ValueError):
        pass
    queued = _queue_first_cold(_service_candidates(now), torrent_cache, now, seen)
    if queued is None:
        g.set_runtime_setting(_SCAN_KEY, str(now))
    return queued


def prefetch_following_episode(trakt_id):
    """Called by the player a minute into an episode: queue a silent scrape for the episode after it (the first
    of the next season at a season end). Local data only; the stored-results check happens when the scrape starts."""
    if not g.get_bool_setting(PLAYBACK_SETTING, True) or not g.get_bool_setting("general.torrentCache"):
        return None
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

    following = TraktSyncDatabase().get_following_episode(trakt_id)
    if not following or not following.get("trakt_id") or not following.get("trakt_show_id"):
        return None
    now = time.time()
    next_id = following["trakt_id"]
    if _guarded(next_id, now):
        return None
    action_args = {"mediatype": "episode", "trakt_id": next_id, "trakt_show_id": following["trakt_show_id"]}
    if following.get("trakt_season_id"):
        action_args["trakt_season_id"] = following["trakt_season_id"]
    _queue(action_args, PLAYBACK_DELAY, "prefetch_next", "seren.prefetchnext", now)
    return next_id
