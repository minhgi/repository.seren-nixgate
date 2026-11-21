"""Unwatched Seasons / Unwatched Episodes: tick the seasons or episodes of one show to mark as unwatched.

Trakt and MDBList entries of the item context menu. What can be ticked comes from that service's LOCAL sync data (nothing is asked of the
service while the menu is built or while the picker opens). The removal goes out as ONE request per service (a loop of single-item
requests would run into Trakt's write rate limit part-way): to the service whose menu it is first, then, when cross-sync is on, as one
request to each other connected service (cross_sync.mirror_bulk_unwatched). Simkl has no local season / episode rows, so it has no
entry of its own and only receives the mirrored removal.

A removal is written to the local database only after the service has confirmed it removed something.
"""
import xbmcgui

from resources.lib.modules.globals import g

STR_SEASONS = 31664
STR_EPISODES = 31665
STR_PICK_SEASONS = 31666
STR_PICK_EPISODES = 31667
STR_SEASON_WATCHED = 31668
STR_SPECIALS_WATCHED = 31669
STR_SEASON_WHOLE = 31670
STR_SPECIALS_WHOLE = 31671
STR_WHOLE_SHOW = 31672

STR_MARKED_UNWATCHED = 30289
STR_FAILED = 30287

KIND_SEASONS = "seasons"
KIND_EPISODES = "episodes"


# region helpers shared with cross_sync


def season_entries(seasons, episodes):
    """Season / episode part of a removal payload. seasons = season numbers removed whole; episodes = {season: [numbers]}
    (the seasons already removed whole are left out of it)."""
    whole = {int(s) for s in seasons}
    entries = [{"number": int(s)} for s in sorted(whole)]
    entries += [
        {"number": int(s), "episodes": [{"number": int(e)} for e in sorted(eps)]}
        for s, eps in sorted(episodes.items())
        if int(s) not in whole and eps
    ]
    return entries


def tmdb_payload(show_tmdb_id, seasons, episodes, whole=False):
    """The tmdb-keyed {"shows": [...]} body Trakt sync/history/remove, MDBList sync/watched/remove and Simkl sync/history/remove all take."""
    show = {"ids": {"tmdb": int(show_tmdb_id)}}
    if not whole:
        entries = season_entries(seasons, episodes)
        if not entries:
            return None
        show["seasons"] = entries
    return {"shows": [show]}


def mdblist_clear_local(show_tmdb_id, seasons, episodes, whole=False):
    """Writes a confirmed MDBList removal to the local MDBList sync tables (no schema change: only rows that exist are updated or deleted)."""
    from resources.lib.database.mdblist_sync import MDBListSyncDatabase

    db = MDBListSyncDatabase()
    show_tmdb_id = int(show_tmdb_id)
    try:
        if whole:
            db.execute_sql("UPDATE episodes SET watched=0, last_watched_at=NULL WHERE show_tmdb_id=?", (show_tmdb_id,))
            db.execute_sql("DELETE FROM bulk_watched_shows WHERE show_tmdb_id=?", (show_tmdb_id,))
            db.execute_sql("DELETE FROM bulk_watched_seasons WHERE show_tmdb_id=?", (show_tmdb_id,))
            return
        for season in seasons:
            db.execute_sql(
                "UPDATE episodes SET watched=0, last_watched_at=NULL WHERE show_tmdb_id=? AND season=?", (show_tmdb_id, int(season))
            )
            db.execute_sql("DELETE FROM bulk_watched_seasons WHERE show_tmdb_id=? AND season=?", (show_tmdb_id, int(season)))
        for season, numbers in episodes.items():
            for number in numbers:
                db.execute_sql(
                    "UPDATE episodes SET watched=0, last_watched_at=NULL WHERE show_tmdb_id=? AND season=? AND number=?",
                    (show_tmdb_id, int(season), int(number)),
                )
    except Exception:
        g.log_stacktrace()


# endregion

# region local data (what can be ticked)


def trakt_watched(show_trakt_id):
    """({season: watched count}, [(season, number, title or None)]) of the show's watched episodes in the local Trakt sync data."""
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

    rows = TraktSyncDatabase().fetchall(
        "SELECT season, number, info FROM episodes WHERE trakt_show_id=? AND watched=1 ORDER BY season, number", (show_trakt_id,)
    )
    counts, episodes = {}, []
    for row in rows or []:
        season, number = row["season"], row["number"]
        if season is None or number is None:
            continue
        info = row["info"]
        title = info.get("title") if isinstance(info, dict) else None
        counts[int(season)] = counts.get(int(season), 0) + 1
        episodes.append((int(season), int(number), title))
    return counts, episodes


def trakt_has_watched(show_trakt_id):
    """Menu gate: does the local Trakt data hold any watched episode of this show? (one local query, no request)"""
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

    row = TraktSyncDatabase().fetchone(
        "SELECT COUNT(*) AS count FROM episodes WHERE trakt_show_id=? AND watched=1", (show_trakt_id,)
    )
    return bool(row and row["count"])


def mdblist_watched(show_tmdb_id):
    """(counts, episodes, bulk_seasons, whole_show) from the local MDBList sync data. A season or the whole show that was marked
    watched as a whole on MDBList has no episode rows: bulk_seasons = those season numbers, whole_show = True for a whole-show mark."""
    from resources.lib.database.mdblist_sync import MDBListSyncDatabase

    db = MDBListSyncDatabase()
    rows = db.fetchall(
        "SELECT season, number FROM episodes WHERE show_tmdb_id=? AND watched=1 ORDER BY season, number", (show_tmdb_id,)
    )
    counts, episodes = {}, []
    for row in rows or []:
        season, number = row["season"], row["number"]
        if season is None or number is None:
            continue
        counts[int(season)] = counts.get(int(season), 0) + 1
        episodes.append((int(season), int(number), None))
    bulk_seasons = {int(s) for s in db.get_bulk_watched_seasons().get(show_tmdb_id, set())}
    whole_show = show_tmdb_id in db.get_bulk_watched_shows()
    return counts, episodes, bulk_seasons, whole_show


def mdblist_has_watched(show_tmdb_id):
    """Menu gate: any watched episode, whole-season mark or whole-show mark of this show in the local MDBList data?"""
    if not show_tmdb_id:
        return False
    counts, episodes, bulk_seasons, whole_show = mdblist_watched(show_tmdb_id)
    return bool(episodes or bulk_seasons or whole_show)


# endregion

# region dialogs


def _season_label(season, count=None):
    if count is None:
        return g.get_language_string(STR_SPECIALS_WHOLE if season == 0 else STR_SEASON_WHOLE).format(season)
    if season == 0:  # the Specials string has ONE placeholder (the count): formatting it with (season, count) printed the season number
        return g.get_language_string(STR_SPECIALS_WATCHED).format(count)
    return g.get_language_string(STR_SEASON_WATCHED).format(season, count)


def pick_seasons(counts, bulk_seasons=(), whole_show=False):
    """Multi-select of the watched seasons. Returns (seasons, whole_show) or None when nothing was ticked."""
    rows = []  # (label, season or None for the whole-show row)
    if whole_show:
        rows.append((g.get_language_string(STR_WHOLE_SHOW), None))
    for season in sorted(set(counts) | set(bulk_seasons)):
        rows.append((_season_label(season, counts[season]) if season in counts else _season_label(season), season))
    if not rows:
        return None
    chosen = xbmcgui.Dialog().multiselect(g.get_language_string(STR_PICK_SEASONS), [label for label, _ in rows])
    if not chosen:
        return None
    picked = [rows[i][1] for i in chosen]
    if None in picked:  # the whole-show row: nothing else needs listing
        return [], True
    return picked, False


def pick_episodes(episodes):
    """Multi-select of the watched episodes. Returns {season: [numbers]} or None when nothing was ticked."""
    if not episodes:
        return None
    labels = []
    for season, number, title in episodes:
        code = f"{season}x{number:02d}"
        labels.append(f"{code}. {title}" if title else code)
    chosen = xbmcgui.Dialog().multiselect(g.get_language_string(STR_PICK_EPISODES), labels)
    if not chosen:
        return None
    result = {}
    for i in chosen:
        season, number, _ = episodes[i]
        result.setdefault(season, []).append(number)
    return result


# endregion

# region removal


def _failed(header, response, service):
    g.notification(header, g.get_language_string(STR_FAILED))
    g.log(f"Failed to mark seasons / episodes as unwatched on {service}\nResponse: {response}")
    return False


def _removed_count(response, key):
    """Number the service says it removed (Trakt answers {"deleted": {...}}, MDBList {"removed": {...}}), 0 for anything else."""
    if not isinstance(response, dict):
        return 0
    counts = response.get(key)
    if not isinstance(counts, dict):
        return 0
    try:
        if isinstance(counts.get("episodes"), (int, float)):
            return int(counts["episodes"])
        return sum(int(v) for v in counts.values() if isinstance(v, (int, float)))
    except (TypeError, ValueError):
        return 0


def remove_from_trakt(api, header, show_trakt_id, seasons, episodes):
    entries = season_entries(seasons, episodes)
    if not entries:
        return False
    response = api.post_json("sync/history/remove", {"shows": [{"ids": {"trakt": show_trakt_id}, "seasons": entries}]})
    if _removed_count(response, "deleted") <= 0:
        return _failed(header, response, "Trakt")
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

    db = TraktSyncDatabase()
    for season in seasons:
        db.mark_season_watched(show_trakt_id, int(season), 0)
    for season, numbers in episodes.items():
        if int(season) in {int(s) for s in seasons}:
            continue
        for number in numbers:
            db.mark_episode_unwatched(show_trakt_id, int(season), int(number))
    return True


def remove_from_mdblist(api, header, show_tmdb_id, seasons, episodes, whole=False):
    payload = tmdb_payload(show_tmdb_id, seasons, episodes, whole)
    if payload is None:
        return False
    response = api.post_json("sync/watched/remove", payload)
    if _removed_count(response, "removed") <= 0:
        return _failed(header, response, "MDBList")
    mdblist_clear_local(show_tmdb_id, seasons, episodes, whole)
    return True


def run(service, info, kind, api, header):
    """Entry point of the context menu. service = "trakt" / "mdblist"; info = the item's info dict; kind = KIND_SEASONS / KIND_EPISODES;
    api = that service's API object; header = the notification heading."""
    mediatype = info["mediatype"]
    show_tmdb_id = info.get("tmdb_id") if mediatype == "tvshow" else info.get("tmdb_show_id")
    whole = False
    if service == "trakt":
        show_trakt_id = info["trakt_id"] if mediatype == "tvshow" else info["trakt_show_id"]
        counts, episode_rows = trakt_watched(show_trakt_id)
        bulk_seasons, whole_show = (), False
    else:
        counts, episode_rows, bulk_seasons, whole_show = mdblist_watched(show_tmdb_id)

    seasons, episodes = [], {}
    if kind == KIND_SEASONS:
        picked = pick_seasons(counts, bulk_seasons, whole_show)
        if picked is None:
            return
        seasons, whole = picked
    else:
        episodes = pick_episodes(episode_rows)
        if episodes is None:
            return

    if service == "trakt":
        done = remove_from_trakt(api, header, show_trakt_id, seasons, episodes)
    else:
        done = remove_from_mdblist(api, header, show_tmdb_id, seasons, episodes, whole)
    if not done:
        return

    g.notification(header, g.get_language_string(STR_MARKED_UNWATCHED))
    if show_tmdb_id:
        from resources.lib.modules import cross_sync

        cross_sync.mirror_bulk_unwatched(service, show_tmdb_id, seasons, episodes, whole)
    g.container_refresh()
    g.trigger_widget_refresh()


# endregion
