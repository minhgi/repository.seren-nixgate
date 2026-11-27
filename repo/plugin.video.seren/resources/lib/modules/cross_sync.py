"""
On-action cross-service mirror (Phase 7, Layer 2). Hooked into the tail end of
TraktContextMenu's existing action methods (gui/trakt_context_menu.py) - never
adds new UI. Every public function here is called AFTER the source service's
own write has already succeeded and the user has already seen a success
notification, so failures in here are logged only, never surfaced or raised -
a mirror failure must not make an already-successful primary action look
broken. Any missed mirror self-heals on the next bridgeSync pass
(modules/bridgeSync.py), which is why this layer never needs its own retry
logic.

Direct API calls only - never instantiates a target service's own sync-
database force/full-sync methods, which would re-trigger that service's own
write-and-notify path and could create a mirror-triggers-mirror echo loop.

Domain coverage mirrors the real shape of the 3 providers' existing UI, which
is asymmetric:
- Watched: all 3 services have native Mark Watched/Unwatched actions, so
  mirror_watched() is genuinely 3-way and takes a `source` argument.
- Watchlist: only Trakt has a generic Add/Remove Watchlist action (gated to
  movie/tvshow only - see TraktContextMenu._handle_watchlist_options). Simkl's
  only entry point is _simkl_set_status's plantowatch option, which is
  add-only by nature (it's a status picker, not a toggle). MDBList has no
  native watchlist UI action at all (its "Add/Remove List" buttons are
  user-named custom lists, a different feature - not mirrored here, matching
  Trakt's own custom lists having no cross-service equivalent either).
- Collection: Trakt-only UI action; Simkl has no Collection endpoint at all
  (hard API constraint). Mirrors to MDBList only.
- Rating: Simkl-only UI action (Trakt/MDBList have no rate button in this
  codebase). Mirrors to Trakt + MDBList, write-only (POST), which is why this
  is safe to ship even though the bridge's own Rating domain is deferred
  pending verification that MDBList/Simkl's ratings-read endpoints actually
  return bulk data. A WeTrakr rating also goes to Simkl (3.9.216), only where it
  cannot change a Simkl status - see mirror_rating_to_simkl.
"""

from resources.lib.modules.globals import g
from resources.lib.modules.syncGateway import configured_providers


def _enabled():
    return g.get_bool_setting("crosssync.enabled", False)


def _targets(source):
    if not _enabled():
        return set()
    return set(configured_providers()) - {source}


def _show_tmdb_id(item_information):
    mediatype = item_information["mediatype"]
    return item_information["tmdb_id"] if mediatype == "tvshow" else item_information["tmdb_show_id"]


def _ids_payload(item_information, extra_fields=None):
    """Nested tmdb-keyed shape shared by Trakt sync/history, MDBList
    sync/watched + sync/collection, and Simkl sync/history + sync/ratings -
    all four accept the identical {"movies"|"shows": [{"ids": {"tmdb": N},
    "seasons": [...]}]} nesting for season/episode granularity (verified
    against simkl.apib and the MDBList apib directly, not just docs prose).
    extra_fields merges into the per-item dict (e.g. {"rating": 8})."""
    mediatype = item_information["mediatype"]
    extra_fields = extra_fields or {}
    if mediatype == "movie":
        return {"movies": [{**extra_fields, "ids": {"tmdb": item_information["tmdb_id"]}}]}
    if mediatype == "tvshow":
        return {"shows": [{**extra_fields, "ids": {"tmdb": item_information["tmdb_id"]}}]}
    if mediatype == "season":
        return {
            "shows": [
                {
                    **extra_fields,
                    "ids": {"tmdb": item_information["tmdb_show_id"]},
                    "seasons": [{"number": item_information["season"]}],
                }
            ]
        }
    if mediatype == "episode":
        return {
            "shows": [
                {
                    **extra_fields,
                    "ids": {"tmdb": item_information["tmdb_show_id"]},
                    "seasons": [
                        {
                            "number": item_information["season"],
                            "episodes": [{"number": item_information["episode"]}],
                        }
                    ],
                }
            ]
        }
    return None


def _mdblist_flat_payload(item_information):
    """Flat shape required by MDBList's POST /watchlist/items/{add|remove} -
    different from _ids_payload's nested shape (verified against the MDBList
    apib directly). Movie/tvshow only - watchlist mirror never receives
    season/episode items, since Trakt's own watchlist UI is gated to
    movie/tvshow (see module docstring)."""
    mediatype = item_information["mediatype"]
    if mediatype == "movie":
        return {"movies": [{"tmdb": item_information["tmdb_id"]}]}
    if mediatype == "tvshow":
        return {"shows": [{"tmdb": item_information["tmdb_id"]}]}
    return None


def _simkl_status_payload(item_information, to_status):
    """Flat shape required by Simkl's POST /sync/add-to-list - matches
    TraktContextMenu._simkl_set_status's own payload exactly. Movie/tvshow
    only, same reasoning as _mdblist_flat_payload."""
    mediatype = item_information["mediatype"]
    if mediatype == "movie":
        return {"movies": [{"to": to_status, "ids": {"tmdb": item_information["tmdb_id"]}}]}
    if mediatype == "tvshow":
        return {"shows": [{"to": to_status, "ids": {"tmdb": item_information["tmdb_id"]}}]}
    return None


def _simkl_movie_watched(item_information):
    """True when Simkl's synced data has this movie as watched. Simkl marks a movie as unwatched when it is
    set to Plan to Watch, so a watchlist mirror must leave such a movie alone (local data, no request)."""
    from resources.lib.database.simkl_sync import SimklSyncDatabase

    return item_information["mediatype"] == "movie" and SimklSyncDatabase().is_movie_watched(
        item_information.get("tmdb_id")
    )


def _resolve_trakt_id(item_information):
    """Resolves this item's tmdb_id to a Trakt object via Trakt's ID lookup
    search (no personal OAuth required - works even when Trakt is only
    configured as a cross-sync target, not used natively). Cached by
    search_by_tmdb_id itself, so repeat lookups for the same item are free.
    Returns None if Trakt has no match."""
    from resources.lib.indexers.trakt import TraktAPI

    mediatype = item_information["mediatype"]
    is_show_level = mediatype in ("tvshow", "season", "episode")
    tmdb_id = _show_tmdb_id(item_information) if mediatype in ("season", "episode") else item_information["tmdb_id"]
    obj = TraktAPI().search_by_tmdb_id(tmdb_id, "show" if is_show_level else "movie")
    return obj.get("trakt_id") if obj else None


# region Watched (3-way, all services have a native Mark Watched/Unwatched action)


def mirror_watched(source, item_information, watched):
    for target in _targets(source):
        try:
            if target == "trakt":
                _push_watched_to_trakt(item_information, watched)
            elif target == "mdblist":
                _push_watched_to_mdblist(item_information, watched)
            elif target == "simkl":
                _push_watched_to_simkl(item_information, watched)
        except Exception as e:
            g.log(f"cross_sync: failed to mirror watched={watched} from {source} to {target}: {e}", "error")
    if source != "wetrakr":
        mirror_watched_to_wetrakr(item_information, watched)


def mirror_bulk_unwatched(source, show_tmdb_id, seasons, episodes, whole=False):
    """One removal request per target for several seasons / episodes of ONE show (the Unwatched Seasons / Unwatched Episodes pickers,
    resources/lib/modules/unwatched_picker.py): looping mirror_watched over the ticked items would be one write each and run into
    Trakt's write rate limit. seasons = season numbers removed whole, episodes = {season: [numbers]}, whole = the show's whole history.
    Local writes follow the same rule as the single-item mirror: only after the target answered."""
    from resources.lib.modules import unwatched_picker

    payload = unwatched_picker.tmdb_payload(show_tmdb_id, seasons, episodes, whole)
    if payload is None:
        return
    for target in _targets(source):
        try:
            if target == "trakt":
                _push_bulk_unwatched_to_trakt(payload, show_tmdb_id, seasons, episodes, whole)
            elif target == "mdblist":
                from resources.lib.indexers.mdblist import MDBListAPI

                if MDBListAPI().post_json("sync/watched/remove", payload):
                    unwatched_picker.mdblist_clear_local(show_tmdb_id, seasons, episodes, whole)
            elif target == "simkl":
                # every pick must be the same on Simkl (a whole season by its episode count, a single episode by title and air date),
                # otherwise nothing is removed there
                differs = None
                if not whole:
                    check = _simkl_check()
                    whole_seasons = sorted({int(s) for s in seasons})
                    differs = next((f"season {s}" for s in whole_seasons if not _simkl_season_agrees(show_tmdb_id, s, check)), None)
                    singles = sorted((int(s), int(n)) for s, numbers in episodes.items() if int(s) not in whole_seasons for n in numbers)
                    differs = differs or next(
                        (f"season {s} episode {n}" for s, n in singles if not _simkl_episode_agrees(show_tmdb_id, s, n, check)), None
                    )
                if differs is not None:
                    g.log(
                        f"cross_sync: unwatched picks of tv {show_tmdb_id} not removed on Simkl - Simkl numbers {differs} differently, "
                        "or it could not be checked",
                        "info",
                    )
                    continue
                from resources.lib.indexers.simkl import SimklAPI

                SimklAPI().post_json("sync/history/remove", payload)
        except Exception as e:
            g.log(f"cross_sync: failed to mirror a bulk unwatched removal from {source} to {target}: {e}", "error")
    if source != "wetrakr" and not whole:
        mirror_bulk_unwatched_to_wetrakr(show_tmdb_id, seasons, episodes)


def _push_bulk_unwatched_to_trakt(payload, show_tmdb_id, seasons, episodes, whole):
    from resources.lib.indexers.trakt import TraktAPI

    response = TraktAPI().post_json("sync/history/remove", payload)
    if not response:
        g.log("cross_sync: trakt bulk unwatched push got no response, skipping local write", "warning")
        return
    trakt_id = _resolve_trakt_id({"mediatype": "season", "tmdb_show_id": show_tmdb_id})
    if not trakt_id:
        return
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

    db = TraktSyncDatabase()
    if whole:
        db.mark_show_watched(trakt_id, 0)
        return
    for season in seasons:
        db.mark_season_watched(trakt_id, int(season), 0)
    for season, numbers in episodes.items():
        if int(season) in {int(s) for s in seasons}:
            continue
        for number in numbers:
            db.mark_episode_unwatched(trakt_id, int(season), int(number))


def _push_watched_to_trakt(item_information, watched):
    from resources.lib.indexers.trakt import TraktAPI

    payload = _ids_payload(item_information)
    if payload is None:
        return
    response = TraktAPI().post_json("sync/history" if watched else "sync/history/remove", payload)
    if not response:
        g.log("cross_sync: trakt watched push got no response, skipping local write", "warning")
        return
    _write_trakt_watched_local(item_information, watched)


def _write_trakt_watched_local(item_information, watched):
    mediatype = item_information["mediatype"]
    trakt_id = _resolve_trakt_id(item_information)
    if not trakt_id:
        return
    if mediatype == "movie":
        from resources.lib.database.trakt_sync.movies import TraktSyncDatabase

        db = TraktSyncDatabase()
        db.mark_movie_watched(trakt_id) if watched else db.mark_movie_unwatched(trakt_id)
        return

    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

    db = TraktSyncDatabase()
    if mediatype == "episode":
        if watched:
            db.mark_episode_watched(trakt_id, item_information["season"], item_information["episode"])
        else:
            db.mark_episode_unwatched(trakt_id, item_information["season"], item_information["episode"])
    elif mediatype == "season":
        db.mark_season_watched(trakt_id, item_information["season"], 1 if watched else 0)
    elif mediatype == "tvshow":
        db.mark_show_watched(trakt_id, 1 if watched else 0)


def _push_watched_to_mdblist(item_information, watched):
    from resources.lib.indexers.mdblist import MDBListAPI

    payload = _ids_payload(item_information)
    if payload is None:
        return
    response = MDBListAPI().post_json("sync/watched" if watched else "sync/watched/remove", payload)
    if not response:
        g.log("cross_sync: mdblist watched push got no response, skipping local write", "warning")
        return
    if item_information.get("mediatype") == "season":
        from resources.lib.database.mdblist_sync.activities import MDBListSyncDatabase as MDBListWatchedSync

        MDBListWatchedSync().resync_after_season_mark(item_information)
    if watched:
        from resources.lib.database.mdblist_sync import MDBListSyncDatabase

        MDBListSyncDatabase().write_watched_locally(item_information["mediatype"], item_information)
        clear_resume_mdblist(item_information)


def _push_watched_to_simkl(item_information, watched):
    from resources.lib.indexers.simkl import SimklAPI

    payload = _ids_payload(item_information)
    if payload is None:
        return
    mediatype = item_information["mediatype"]
    if mediatype == "episode" or (mediatype == "season" and not watched):
        # Simkl numbers some shows' seasons differently: never mark or remove another episode there, or remove another season
        show_id, season = item_information["tmdb_show_id"], item_information["season"]
        episode = item_information["episode"] if mediatype == "episode" else None
        agrees = _simkl_season_agrees(show_id, season) if episode is None else _simkl_episode_agrees(show_id, season, episode)
        if not agrees:
            what = f"season {season}"
            if mediatype == "episode":
                what += f" episode {item_information['episode']}"
            action = "marked watched" if watched else "removed"
            g.log(f"cross_sync: {what} of tv {show_id} not {action} on Simkl - Simkl numbers it differently, or it could not be checked", "info")
            return
    response = SimklAPI().post_json("sync/history" if watched else "sync/history/remove", payload)
    if watched and response:
        clear_resume_simkl(item_information)


def _simkl_check(check=None):
    from resources.lib.modules.bridgeSync import _Bridge

    if check is None:
        check = _Bridge((), None)
        check._meta = {}  # no stored memo: the answer is read fresh for this one write
    return check


def _simkl_season_agrees(tmdb_id, season, check=None):
    """For a WHOLE season: it must exist on both sides with about the same episode count (_Bridge._simkl_season_counts_agree);
    anything that cannot be checked is False. Nothing is remembered or saved from here."""
    return _simkl_check(check)._simkl_season_counts_agree(tmdb_id, season)


def _simkl_episode_agrees(tmdb_id, season, episode, check=None):
    """For ONE episode: the number must be the same episode on both sides, by title and air date (_Bridge._simkl_numbering_ok);
    anything that cannot be checked is False. `check` shared between calls reads each season once."""
    return _simkl_check(check)._simkl_numbering_ok(tmdb_id, season, episode)


def _resume_key(item_information):
    """(media_type, tmdb_id, season, number) of a movie or an episode (the tmdb id of an episode is its show's), else None."""
    mediatype = item_information.get("mediatype")
    if mediatype == "movie":
        key = ("movie", item_information.get("tmdb_id"), None, None)
    elif mediatype == "episode":
        key = ("episode", item_information.get("tmdb_show_id"), item_information.get("season"), item_information.get("episode"))
    else:
        return None
    return key if key[1] and (key[0] == "movie" or (key[2] is not None and key[3] is not None)) else None


def clear_resume_mdblist(item_information):
    """A finished movie / episode must not keep a resume point on the target it was mirrored to as watched: MDBList's sync/watched
    leaves the resume entry in place (only scrobble/stop clears it), so the item stayed in its In Progress list with its old
    percentage (TMDb Helper 7.0.91). Clears the entry on the server and the local bookmark. Failures are logged only."""
    key = _resume_key(item_information)
    if key is None:
        return
    try:
        from resources.lib.database.mdblist_sync import MDBListSyncDatabase
        from resources.lib.indexers.mdblist import MDBListAPI

        api = MDBListAPI()
        playback = api.get_json("sync/playback")
        if isinstance(playback, dict):
            playback = playback.get("items")
        if isinstance(playback, list):
            for entry in playback:
                if not isinstance(entry, dict) or entry.get("id") is None:
                    continue
                if key[0] == "movie":
                    if str(((entry.get("movie") or {}).get("ids") or {}).get("tmdb")) != str(key[1]):
                        continue
                else:
                    episode = entry.get("episode") or {}
                    if (
                        str(((entry.get("show") or {}).get("ids") or {}).get("tmdb")) != str(key[1])
                        or episode.get("season") != key[2]
                        or episode.get("number") != key[3]
                    ):
                        continue
                api.post_json("scrobble/clear", {"id": entry["id"]})
        MDBListSyncDatabase().remove_bookmark(key[1], key[0], key[2], key[3])
    except Exception as e:
        g.log(f"cross_sync: could not clear the MDBList resume point of a mirrored watched mark: {e}", "warning")


def clear_resume_simkl(item_information):
    """Simkl hides the playback entry of an item marked watched by itself, but Seren's local bookmark row stayed until the next
    sync (up to 15 minutes): the movie / episode kept its old percentage in the In Progress list. Local row only: a DELETE right
    after the history write would run into Simkl's 20 s write lock."""
    key = _resume_key(item_information)
    if key is None:
        return
    try:
        from resources.lib.database.simkl_sync import SimklSyncDatabase

        SimklSyncDatabase().remove_bookmark(key[1], key[0], key[2], key[3])
    except Exception as e:
        g.log(f"cross_sync: could not clear the Simkl resume point of a mirrored watched mark: {e}", "warning")


# endregion

# region Watchlist (Trakt <-> MDBList full; Trakt/Simkl add-only per Simkl's destructive remove)


def mirror_watchlist_add_from_trakt(item_information):
    for target in _targets("trakt"):
        try:
            if target == "mdblist":
                from resources.lib.indexers.mdblist import MDBListAPI

                payload = _mdblist_flat_payload(item_information)
                if payload is not None:
                    MDBListAPI().post_json("watchlist/items/add", payload)
            elif target == "simkl":
                from resources.lib.indexers.simkl import SimklAPI

                if _simkl_movie_watched(item_information):
                    g.log(
                        f"cross_sync: movie {item_information.get('tmdb_id')} is watched on Simkl - "
                        "not moved to Plan to Watch",
                        "info",
                    )
                    continue
                payload = _simkl_status_payload(item_information, "plantowatch")
                if payload is not None:
                    SimklAPI().post_json("sync/add-to-list", payload)
        except Exception as e:
            g.log(f"cross_sync: failed to mirror watchlist-add from trakt to {target}: {e}", "error")
    mirror_watchlist_to_wetrakr(item_information, False)


def mirror_watchlist_remove_from_trakt(item_information):
    """Never mirrors to Simkl - its only removal endpoint (sync/history/remove)
    clears ALL list status for an item, not just watchlist/plantowatch, so a
    remove mirror there would be destructive rather than corrective."""
    mirror_watchlist_to_wetrakr(item_information, True)
    if "mdblist" not in _targets("trakt"):
        return
    try:
        from resources.lib.indexers.mdblist import MDBListAPI

        payload = _mdblist_flat_payload(item_information)
        if payload is not None:
            MDBListAPI().post_json("watchlist/items/remove", payload)
    except Exception as e:
        g.log(f"cross_sync: failed to mirror watchlist-remove from trakt to mdblist: {e}", "error")


def mirror_watchlist_add_from_simkl(item_information):
    """Called only when _simkl_set_status's to_status == "plantowatch" -
    mirrors unconditionally to both other services, matching the reference
    implementation's ItemSimklPlanToWatch._cross_sync() precedent."""
    for target in _targets("simkl"):
        try:
            if target == "trakt":
                from resources.lib.indexers.trakt import TraktAPI

                payload = _ids_payload(item_information)
                if payload is not None:
                    TraktAPI().post_json("sync/watchlist", payload)
            elif target == "mdblist":
                from resources.lib.indexers.mdblist import MDBListAPI

                payload = _mdblist_flat_payload(item_information)
                if payload is not None:
                    MDBListAPI().post_json("watchlist/items/add", payload)
        except Exception as e:
            g.log(f"cross_sync: failed to mirror watchlist-add from simkl to {target}: {e}", "error")
    mirror_watchlist_to_wetrakr(item_information, False)


# endregion

# region Collection (Trakt <-> MDBList only; Simkl has no Collection endpoint)


def mirror_collection_add_from_trakt(item_information):
    mirror_collection_to_wetrakr(item_information, False)
    if "mdblist" not in _targets("trakt"):
        return
    try:
        from resources.lib.indexers.mdblist import MDBListAPI

        payload = _ids_payload(item_information)
        if payload is not None:
            MDBListAPI().post_json("sync/collection", payload)
    except Exception as e:
        g.log(f"cross_sync: failed to mirror collection-add from trakt to mdblist: {e}", "error")


def mirror_collection_remove_from_trakt(item_information):
    mirror_collection_to_wetrakr(item_information, True)
    if "mdblist" not in _targets("trakt"):
        return
    try:
        from resources.lib.indexers.mdblist import MDBListAPI

        payload = _ids_payload(item_information)
        if payload is not None:
            MDBListAPI().post_json("sync/collection/remove", payload)
    except Exception as e:
        g.log(f"cross_sync: failed to mirror collection-remove from trakt to mdblist: {e}", "error")


# endregion

# region Rating (Simkl-originated only; Trakt/MDBList have no rate action in this UI)


def mirror_rating_from_simkl(item_information, rating):
    for target in _targets("simkl"):
        try:
            payload = _ids_payload(item_information, {"rating": rating})
            if payload is None:
                continue
            if target == "trakt":
                from resources.lib.indexers.trakt import TraktAPI

                TraktAPI().post_json("sync/ratings", payload)
            elif target == "mdblist":
                from resources.lib.indexers.mdblist import MDBListAPI

                MDBListAPI().post_json("sync/ratings", payload)
        except Exception as e:
            g.log(f"cross_sync: failed to mirror rating from simkl to {target}: {e}", "error")
    mirror_rating_to_wetrakr(item_information, rating)


def mirror_rating_clear_from_simkl(item_information):
    for target in _targets("simkl"):
        try:
            payload = _ids_payload(item_information)
            if payload is None:
                continue
            if target == "trakt":
                from resources.lib.indexers.trakt import TraktAPI

                TraktAPI().post_json("sync/ratings/remove", payload)
            elif target == "mdblist":
                from resources.lib.indexers.mdblist import MDBListAPI

                MDBListAPI().post_json("sync/ratings/remove", payload)
        except Exception as e:
            g.log(f"cross_sync: failed to mirror rating-clear from simkl to {target}: {e}", "error")
    mirror_rating_to_wetrakr(item_information, 0)


# endregion

# region WeTrakr ratings (3.9.214)


def _wetrakr_ratings_mode():
    """crosssync.wetrakrRatings: 0 off, 1 log only, 2 on. Only while the sync is on and watched marks with WeTrakr are On."""
    if not _enabled():
        return 0
    from resources.lib.modules.bridgeSync import _wetrakr_setting_mode

    if _wetrakr_setting_mode() != 2:
        return 0
    try:
        mode = int(g.get_int_setting("crosssync.wetrakrRatings", 1))
    except Exception:
        return 1
    return mode if 0 <= mode <= 2 else 1


def mirror_rating_to_wetrakr(item_information, rating):
    """A rating given (or, rating 0, removed) on Simkl goes to WeTrakr. Direct API call only: no echo."""
    try:
        mode = _wetrakr_ratings_mode()
        if not mode or rating is None:
            return
        from resources.lib.gui.wetrakr_manager import rating_payload, read_outcome, target_of

        target = target_of(item_information)
        if target is None:
            return
        what = f"rating {rating}" if rating else "rating removal"
        if mode == 1:
            g.log(f"cross_sync (WeTrakr ratings, log only): On would send {what} for {target} to WeTrakr", "info")
            return
        from resources.lib.indexers.wetrakr import WeTrakrAPI

        # 0 is a real rating to WeTrakr, so a removal goes to the remove endpoint without a value
        path = "/sync/ratings" if rating else "/sync/ratings/remove"
        response = WeTrakrAPI().user_request("POST", path, body=rating_payload(target, rating or None))
        status, detail = read_outcome(response)
        if status == "failed":
            g.log(f"cross_sync: WeTrakr did not take the {what} for {target} - {detail}", "warning")
            return
        try:
            from resources.lib.database.wetrakr_sync import WeTrakrSyncDatabase

            WeTrakrSyncDatabase().reset_ratings_stamp()
        except Exception:
            g.log_stacktrace()
    except Exception as e:
        g.log(f"cross_sync: failed to mirror rating to wetrakr: {e}", "error")


def mirror_rating_from_wetrakr(target, rating):
    """A rating given (or, rating 0, removed) on WeTrakr goes to Trakt and MDBList, and to Simkl under that mirror's own setting and checks.
    target: (kind, tmdb id, season, episode) as the WeTrakr manager uses it."""
    try:
        mode = _wetrakr_ratings_mode()
        if not mode or rating is None:
            return
        if target[0] == "season":
            g.log(f"cross_sync: WeTrakr season rating for {target} not sent on (season ratings stay on WeTrakr)", "info")
            return
        what = f"rating {rating}" if rating else "rating removal"
        if mode == 1:
            g.log(f"cross_sync (WeTrakr ratings, log only): On would send {what} for {target} to Trakt and MDBList", "info")
            return
        from resources.lib.gui.wetrakr_manager import rating_payload

        payload = rating_payload(target, rating or None)  # the nested ids shape Trakt and MDBList take too
        path = "sync/ratings" if rating else "sync/ratings/remove"
    except Exception as e:
        g.log(f"cross_sync: failed to mirror rating from wetrakr: {e}", "error")
        return
    for service in ("trakt", "mdblist"):
        if service not in _targets("wetrakr"):
            continue
        try:
            if service == "trakt":
                from resources.lib.indexers.trakt import TraktAPI

                TraktAPI().post_json(path, payload)
            else:
                from resources.lib.indexers.mdblist import MDBListAPI

                MDBListAPI().post_json(path, payload)
        except Exception as e:
            g.log(f"cross_sync: failed to mirror rating from wetrakr to {service}: {e}", "error")
    if "simkl" in _targets("wetrakr"):
        mirror_rating_to_simkl(target, rating)


# endregion


# region Ratings into Simkl (3.9.216)
# POST sync/ratings is not a rating only: Simkl makes any movie that is not "completed" completed, watched at that moment, and files an
# unlisted show as "watching" (live-probed 2026-11-27). So a rating is sent only for a movie Simkl itself lists as completed, or a show
# that is on one of its lists, asked right before sending and written to the entry that was asked about. Anything unclear: nothing is sent.
# No local pre-check: the local Simkl show table holds completed and watching shows only.

_SIMKL_LISTS = ("watching", "plantowatch", "completed", "hold", "dropped", "notinteresting")  # either name of "dropped", by Simkl app


def _simkl_ratings_mode():
    """crosssync.simklRatings: 0 off, 1 log only, 2 on."""
    try:
        mode = int(g.get_int_setting("crosssync.simklRatings", 1))
    except Exception:
        return 1
    return mode if 0 <= mode <= 2 else 1


def _simkl_listing(api, kind, tmdb_id):
    """(Simkl id, list) as Simkl answers now, list None when the title is on none. (None, None) when it cannot be told."""
    try:
        row = api.post_json("sync/watched", [{"tmdb": int(tmdb_id), "type": "movie" if kind == "movie" else "tv"}])[0]
        if row.get("result") not in (True, False):
            return None, None
        return int(row["simkl"]), row.get("list")
    except Exception:
        return None, None


def _simkl_list_name(value):
    return "dropped" if value == "notinteresting" else value


def mirror_rating_to_simkl(target, rating):
    """A movie or show rating given (or, rating 0, removed) on WeTrakr goes to Simkl. Never a season or an episode (a season rating
    overwrites the show's on Simkl). target: (kind, tmdb id, season, episode). The caller has checked that Simkl takes part."""
    try:
        mode = _simkl_ratings_mode()
        kind, tmdb_id = target[0], target[1]
        if not mode or rating is None or kind not in ("movie", "show"):
            return
        rating = int(rating)
        if not 0 <= rating <= 10:
            return
        from resources.lib.indexers.simkl import SimklAPI

        api = SimklAPI()
        label = f"{kind} {tmdb_id}"
        what = f"rating {rating}" if rating else "rating removal"
        key = "movies" if kind == "movie" else "shows"
        listed = None
        if rating:
            simkl_id, listed = _simkl_listing(api, kind, tmdb_id)
            if simkl_id is None:
                g.log(f"cross_sync (Simkl ratings): {what} for {label} not sent - Simkl could not be asked what list it is on", "info")
                return
            if not (listed == "completed" if kind == "movie" else listed in _SIMKL_LISTS):
                g.log(f"cross_sync (Simkl ratings): {what} for {label} not sent - Simkl list: {listed or 'none'} (a rating would change it)", "info")
                return
            body = {key: [{"ids": {"simkl": simkl_id}, "rating": rating}]}
            path = "sync/ratings"
        else:
            # removing a rating changes no list, also for a title on none (live-probed 2026-11-27): nothing to ask first
            body = {key: [{"ids": {"tmdb": int(tmdb_id)}}]}
            path = "sync/ratings/remove"
        if mode == 1:
            where = f" (Simkl list: {listed})" if listed else ""
            g.log(f"cross_sync (Simkl ratings, log only): On would send {what} for {label} to Simkl{where}", "info")
            return
        response = api.post_json(path, body)
        if not isinstance(response, dict) or any((response.get("not_found") or {}).values()):
            g.log(f"cross_sync: Simkl did not take the {what} for {label}", "warning")
            return
        if rating:
            try:
                now = response["added"]["statuses"][0]["response"]["status"]
            except (KeyError, IndexError, TypeError):
                now = None
            if now and _simkl_list_name(now) != _simkl_list_name(listed):
                g.log(f"cross_sync: Simkl moved {label} from {listed} to {now} when it took the rating", "warning")
        g.log(f"cross_sync (Simkl ratings): sent {what} for {label} to Simkl", "info")
    except Exception as e:
        g.log(f"cross_sync: failed to mirror rating to simkl: {e}", "error")


# endregion


# region WeTrakr menu actions (3.9.215)
# Into WeTrakr: watched / unwatched, watchlist (the planning list) and collection (the library). Out of WeTrakr: watched / unwatched only
# (WeTrakr's manager has no watchlist or collection action). Direct API calls only. When anything cannot be checked the change is left to
# the background sync. The sync's own files (snapshot, journal) are never touched here.


def _wetrakr_menu_mode():
    """crosssync.wetrakrMenu: 0 off, 1 log only, 2 on. Only while the sync is on and watched marks with WeTrakr are On."""
    if not _enabled():
        return 0
    from resources.lib.modules.bridgeSync import _wetrakr_setting_mode

    if _wetrakr_setting_mode() != 2:
        return 0
    try:
        mode = int(g.get_int_setting("crosssync.wetrakrMenu", 1))
    except Exception:
        return 1
    return mode if 0 <= mode <= 2 else 1


def _wetrakr_menu_list_mode():
    """Watchlist / collection into WeTrakr: the weaker of the menu setting and the WeTrakr watchlist and collection setting."""
    mode = _wetrakr_menu_mode()
    if not mode:
        return 0
    from resources.lib.modules.bridgeSync import _wetrakr_lists_mode

    return min(mode, _wetrakr_lists_mode())


def _wetrakr_client():
    if not g.wetrakr_active():
        return None
    from resources.lib.indexers.wetrakr import WeTrakrAPI

    return WeTrakrAPI()


def _wetrakr_menu_log(message, level="info"):
    g.log(f"cross_sync (WeTrakr menu): {message}", level)


def _wetrakr_write(api, path, body, what):
    """One write. Returns 'ok' (something changed), 'unchanged' or 'failed'. A 2xx answer is not enough, see read_outcome."""
    from resources.lib.gui.wetrakr_manager import read_outcome

    response = api.user_request("POST", path, body=body)
    if getattr(response, "status_code", None) == 420:
        _wetrakr_menu_log(f"{what}: the WeTrakr library is full for this account plan", "warning")
        return "failed"
    status, detail = read_outcome(response)
    if status == "ok":
        return "ok"
    if status == "already":
        return "unchanged"
    _wetrakr_menu_log(f"WeTrakr did not take the {what} - {detail}. Left to the sync", "warning")
    return "failed"


def _wetrakr_reset_watched_stamp():
    try:
        from resources.lib.database.wetrakr_sync.activities import WeTrakrSyncDatabase

        WeTrakrSyncDatabase().reset_watched_stamp()
    except Exception:
        g.log_stacktrace()


def mirror_watched_to_wetrakr(item_information, watched):
    """Mark watched / unwatched made on Trakt, MDBList or Simkl. Unwatched removes every play (remove/all), as the other services do."""
    try:
        mode = _wetrakr_menu_mode()
        if not mode:
            return
        from resources.lib.gui.wetrakr_manager import WeTrakrManager, target_of, tracking_payload

        target = target_of(item_information)
        api = _wetrakr_client() if target else None
        if not api:
            return
        what = f"{'watched' if watched else 'unwatched'} mark for {target}"
        if mode == 1:
            _wetrakr_menu_log(f"log only: On would send the {what} to WeTrakr")
            return
        payload = tracking_payload(target, "watched")
        if watched:
            payload["allow_rewatch"] = False  # never a second play for a title WeTrakr already has
        status = _wetrakr_write(api, "/sync/tracking" if watched else "/sync/tracking/remove/all", payload, what)
        if status == "failed":
            return
        if status == "ok":
            WeTrakrManager("")._write_through_watched(target, watched)  # local rows only, no notification, no mirror
        _wetrakr_reset_watched_stamp()
    except Exception as e:
        g.log(f"cross_sync: failed to mirror watched={watched} to wetrakr: {e}", "error")


def mirror_bulk_unwatched_to_wetrakr(show_tmdb_id, seasons, episodes):
    """The ticked seasons / episodes of the Unwatched Seasons / Unwatched Episodes pickers, in one request. Never a whole show: without
    a nested season the request would wipe every play of the show, so nothing is sent then."""
    try:
        mode = _wetrakr_menu_mode()
        if not mode:
            return
        whole_seasons = sorted({int(s) for s in seasons})
        nested = [{"number": s} for s in whole_seasons]
        for season, numbers in sorted(episodes.items(), key=lambda pair: int(pair[0])):
            if int(season) not in whole_seasons and numbers:
                nested.append({"number": int(season), "episodes": [{"number": int(n)} for n in sorted(numbers, key=int)]})
        api = _wetrakr_client() if nested else None
        if not api:
            return
        what = f"unwatched marks for picked seasons / episodes of show {show_tmdb_id}"
        if mode == 1:
            _wetrakr_menu_log(f"log only: On would send the {what} to WeTrakr: {nested}")
            return
        body = {"shows": [{"ids": {"tmdb": int(show_tmdb_id)}, "status": "watched", "seasons": nested}]}
        if _wetrakr_write(api, "/sync/tracking/remove/all", body, what) != "failed":
            _wetrakr_reset_watched_stamp()
    except Exception as e:
        g.log(f"cross_sync: failed to mirror a bulk unwatched removal to wetrakr: {e}", "error")


def _wetrakr_list_holding(api, kind, tmdb_id):
    """(checked, list name or None): which WeTrakr tracking list has this movie / show, read fresh. checked False = a list could not be read."""
    from resources.lib.modules.bridgeSync import _WETRAKR_LISTS

    for status in _WETRAKR_LISTS[kind]:
        ok, rows = api.get_list(f"/sync/tracking/{status}/{kind}s")
        if not ok:
            return False, None
        if any(str(((row or {}).get("ids") or {}).get("tmdb")) == str(tmdb_id) for row in rows):
            return True, status
    return True, None


def _wetrakr_list_target(item_information):
    """('movie' / 'show', tmdb id) of a whole movie or show, else None (the lists take whole titles only)."""
    from resources.lib.gui.wetrakr_manager import target_of

    target = target_of(item_information)
    return (target[0], target[1]) if target and target[0] in ("movie", "show") else None


def mirror_watchlist_to_wetrakr(item_information, remove):
    """Watchlist add / remove = WeTrakr's planning list. Adding moves a title off any other list there, so it is sent only when a fresh
    read finds the title on no list. A removal names the planning list, so it can touch nothing else."""
    try:
        mode = _wetrakr_menu_list_mode()
        if not mode:
            return
        target = _wetrakr_list_target(item_information)
        api = _wetrakr_client() if target else None
        if not api:
            return
        kind, tmdb_id = target
        what = f"watchlist {'removal' if remove else 'add'} for {kind} {tmdb_id}"
        if not remove:
            checked, holding = _wetrakr_list_holding(api, kind, tmdb_id)
            if not checked:
                _wetrakr_menu_log(f"{what}: the WeTrakr lists could not be read. Left to the sync")
                return
            if holding:
                _wetrakr_menu_log(f"{what}: already on the WeTrakr {holding} list - left alone")
                return
        if mode == 1:
            _wetrakr_menu_log(f"log only: On would send the {what} to WeTrakr")
            return
        body = {f"{kind}s": [{"ids": {"tmdb": tmdb_id}, "status": "planning"}]}
        _wetrakr_write(api, "/sync/tracking/remove" if remove else "/sync/tracking", body, what)
    except Exception as e:
        g.log(f"cross_sync: failed to mirror a watchlist change to wetrakr: {e}", "error")


def _wetrakr_in_library(api, kind, tmdb_id):
    """True / False whether the WeTrakr library holds the title; None when that cannot be told (a page failed or rows are missing)."""
    page, pages, rows_read, total = 1, 1, 0, None
    while page <= pages:
        if page > 100:
            return None
        response = api.user_request("GET", "/sync/library/all", params={"page": page, "limit": 100})
        if response is None or response.status_code != 200:
            return None
        try:
            data = response.json()
            pages = int(response.headers.get("X-Pagination-Page-Count") or 0)
            total = int(response.headers.get("X-Pagination-Item-Count"))
        except (ValueError, TypeError):
            return None
        if not isinstance(data, list):
            return None
        for row in data:
            if row.get("type") not in ("movie", "show"):
                return None
            if row.get("type") == kind and str((row.get("ids") or {}).get("tmdb")) == str(tmdb_id):
                return True
        rows_read += len(data)
        page += 1
    return False if rows_read == total else None


def mirror_collection_to_wetrakr(item_information, remove):
    """Collection add / remove = WeTrakr's library, whole titles only (as the background sync). The add makes a copy with the sync's own
    copy_key, and only when the library does not hold the title yet. A removal takes only copies carrying that prefix: a copy the user
    made on WeTrakr stays."""
    try:
        mode = _wetrakr_menu_list_mode()
        if not mode:
            return
        target = _wetrakr_list_target(item_information)
        api = _wetrakr_client() if target else None
        if not api:
            return
        from resources.lib.modules.bridgeSync import _WETRAKR_COPY_PREFIX

        kind, tmdb_id = target
        what = f"collection {'removal' if remove else 'add'} for {kind} {tmdb_id}"
        if not remove:
            held = _wetrakr_in_library(api, kind, tmdb_id)
            if held is None:
                _wetrakr_menu_log(f"{what}: the WeTrakr library could not be read. Left to the sync")
                return
            if held:
                _wetrakr_menu_log(f"{what}: already in the WeTrakr library - left alone")
                return
        if mode == 1:
            _wetrakr_menu_log(f"log only: On would send the {what} to WeTrakr")
            return
        entry = {"ids": {"tmdb": tmdb_id}}
        if remove:
            path, body = "/sync/library/remove", {f"{kind}s": [entry], "copy_key_prefix": _WETRAKR_COPY_PREFIX}
        else:
            import datetime

            entry["copy_key"] = f"{_WETRAKR_COPY_PREFIX}{kind}:{tmdb_id}"
            entry["collected_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            path, body = "/sync/library", {f"{kind}s": [entry]}
        _wetrakr_write(api, path, body, what)
    except Exception as e:
        g.log(f"cross_sync: failed to mirror a collection change to wetrakr: {e}", "error")


def mirror_watched_from_wetrakr(item_information, watched):
    """Set as watched / unwatched made in the WeTrakr manager goes to Trakt, MDBList and Simkl (mirror_watched never sends anything back
    to the source it is told)."""
    try:
        mode = _wetrakr_menu_mode()
        if not mode:
            return
        if mode == 1:
            from resources.lib.gui.wetrakr_manager import target_of

            what = f"{'watched' if watched else 'unwatched'} mark for {target_of(item_information)}"
            _wetrakr_menu_log(f"log only: On would send the {what} to Trakt, MDBList and Simkl")
            return
    except Exception as e:
        g.log(f"cross_sync: failed to mirror a watched change from wetrakr: {e}", "error")
        return
    mirror_watched("wetrakr", item_information, watched)


# endregion
