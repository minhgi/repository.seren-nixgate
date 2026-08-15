"""Calendar data aggregation for the windowed airing calendar (Phase 6,
Implementation_Plan.md).

Merges Trakt's own calendar (recent + upcoming - the same two windows already
used by tvshowMenus.py's my_recent_episodes()/my_upcoming_episodes(), just
unified into one list) with shows that are only on Simkl's plan-to-watch list
or MDBList's watchlist. Those two need real per-show episode lookups, not
just an extra API call: Trakt's calendars/my/shows endpoint is scoped to
shows watched/collected/watchlisted on Trakt itself (confirmed via Trakt's
own API documentation), so a Simkl/MDBList-only show never appears there,
even once its tmdb_id is resolved to a Trakt object.

Both paths converge on the same normalized episode-row shape Seren already
uses everywhere (trakt_id, info, cast, art, args, ...) - the Trakt-calendar
path via TraktSyncDatabase.get_mixed_episode_list() (the same DB call
mixed_episode_builder() already makes), the union path via
TraktSyncDatabase.get_episode_list() per resolved show. Both read the same
episodes.info column, so a rating comes along for free on either path - no
separate enrichment step. Corrected after live-testing: info["rating"] is
Trakt's own community rating as a plain float (trakt.py's EpisodeNormalization
maps raw "rating" straight to output key "rating", a number - not a nested
{rating, votes} dict; that nested shape exists separately under the
namespaced "rating.trakt" key, which this module doesn't use). TMDB's own
rating is separately namespaced under "rating.tmdb". episodes.user_rating is
the signed-in user's own personal rating, not a community score.
"""
import datetime

from resources.lib.modules.globals import g

_UNION_CAP = 25  # hard cap on Simkl/MDBList-only shows resolved per open - each
                 # costs a live search_by_tmdb_id + get_episode_list call, not a
                 # cache hit (get_episode_list always runs _try_update_episodes()
                 # first). Past this cap, stop and log the dropped count instead
                 # of letting a large plan-to-watch/watchlist hang the window.


def get_air_date(episode):
    """Episode air date, matching list_builder.py's is_aired() fallback chain
    exactly (top-level air_date, then info.aired, then info.premiered) - Trakt's
    own normalization (trakt.py EpisodeNormalization/MixedEpisodeNormalization)
    writes the date to info.aired/info.premiered, never to a bare "air_date"
    key, confirmed against the real schema during live-testing (this function
    replaces an earlier, wrong assumption that "air_date" itself was the key,
    which silently zeroed out every date-based comparison in this module)."""
    info = episode.get("info") or {}
    return episode.get("air_date") or info.get("aired") or info.get("premiered")


def get_calendar_episodes():
    """Returns one chronologically-sorted list of normalized episode rows
    spanning recent (13 days back) through upcoming (30 days forward)."""
    from resources.lib.modules.syncGateway import configured_providers
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase as ShowsDatabase

    providers = configured_providers()

    if "trakt" in providers:
        from resources.lib.indexers.trakt import TraktAPI

        raw_calendar_items = _fetch_raw_trakt_calendar(TraktAPI())
    else:
        raw_calendar_items = []
    covered_show_ids = {i["trakt_show_id"] for i in raw_calendar_items if i.get("trakt_show_id")}

    shows_db = ShowsDatabase()
    trakt_items = shows_db.get_mixed_episode_list(raw_calendar_items, hide_unaired=False) if raw_calendar_items else []

    candidate_tmdb_ids = {}  # dict (not set) purely to preserve first-seen order for the cap
    # Simkl/MDBList are enrichment on top of Trakt's own calendar, not a hard dependency -
    # both helpers already degrade to [] on a transient fetch failure (get_json returns
    # None on failure, never raises), so no try/except is needed at this call site.
    if "simkl" in providers:
        for tmdb_id in _simkl_plantowatch_tmdb_ids():
            candidate_tmdb_ids.setdefault(tmdb_id, None)
    if "mdblist" in providers:
        for tmdb_id in _mdblist_watchlist_tmdb_ids():
            candidate_tmdb_ids.setdefault(tmdb_id, None)

    capped_ids = list(candidate_tmdb_ids)[:_UNION_CAP]
    dropped = len(candidate_tmdb_ids) - len(capped_ids)
    if dropped > 0:
        g.log(f"calendar_data: union cap reached, dropped {dropped} Simkl/MDBList-only show(s) this open", "warning")

    extra_items = _resolve_extra_shows(capped_ids, covered_show_ids, shows_db)

    merged = trakt_items + extra_items
    merged.sort(key=lambda i: get_air_date(i) or "")
    return merged


def _fetch_raw_trakt_calendar(trakt_api):
    """Same two windows tvshowMenus.py already fetches - unchanged values,
    just returned as raw Trakt objects instead of being handed straight to
    mixed_episode_builder(). Hidden-shows filter matches my_recent_episodes()'s
    own (tvshowMenus.py ~L525-536); my_upcoming_episodes() doesn't apply it
    today, but a merged calendar view is one list, one filter, applied once."""
    from resources.lib.database.trakt_sync.hidden import TraktSyncDatabase as HiddenDatabase

    recent_start = datetime.datetime.now() - datetime.timedelta(days=13)
    recent = trakt_api.get_json(
        f"calendars/my/shows/{recent_start.strftime('%Y-%m-%d')}/14", extended="full", pull_all=True
    ) or []

    tomorrow = g.datetime_to_string(datetime.date.today() + datetime.timedelta(days=1))
    upcoming = trakt_api.get_json(f"calendars/my/shows/{tomorrow}/30", extended="full", pull_all=True) or []

    hidden_shows = HiddenDatabase().get_hidden_items("calendar", "tvshow")
    return [i for i in (recent + upcoming) if i.get("trakt_show_id") not in hidden_shows]


def _simkl_plantowatch_tmdb_ids():
    """Fetch+extract half of simklMenus.py's _status_shows("plantowatch") -
    deliberately not the resolve/render half, since this module needs raw
    tmdb_ids to dedupe against the Trakt set before resolving, not a rendered
    directory (_status_shows() itself calls g.cancel_directory() on empty,
    which doesn't apply here)."""
    from resources.lib.indexers.simkl import SimklAPI

    page = SimklAPI().get_json("sync/all-items/shows/plantowatch")
    if page is None:
        return []
    tmdb_ids = []
    for entry in page.get("shows") or []:
        tmdb_id = ((entry.get("show") or {}).get("ids") or {}).get("tmdb")
        if tmdb_id is not None:
            tmdb_ids.append(tmdb_id)
    return tmdb_ids


def _mdblist_watchlist_tmdb_ids():
    """Reuses bridgeSync's own paginated watchlist reader as-is (same
    server-side 1000-item-per-page cap applies, same as bridgeSync's own
    use) - "show:" prefixed keys only, movies aren't in scope for an airing
    calendar."""
    from resources.lib.modules.bridgeSync import _read_mdblist_watchlist

    watchlist = _read_mdblist_watchlist()
    return [tmdb_id for key, tmdb_id in watchlist.items() if key.startswith("show:")]


def _resolve_extra_shows(tmdb_ids, covered_show_ids, shows_db):
    """Resolves capped tmdb_ids to Trakt shows, reusing simklMenus.py's tested
    _resolve() (deliberately not a third copy of that logic - mdblistMenus.py
    already carries a second). Skips anything that turns out to already be
    covered by Trakt's own calendar (possible if a show is on both Trakt's
    list and Simkl's plantowatch) before paying for an episode lookup.

    seen_show_ids also catches a second case found live: Simkl and MDBList can
    reference the same real-world show under two different tmdb_ids (observed
    with an anthology series), which both survive candidate_tmdb_ids' dedup
    upstream (different dict keys) but resolve to the same trakt_show_id here
    - without this, that show's episode was added twice."""
    if not tmdb_ids:
        return []

    from resources.lib.gui.simklMenus import Menus as SimklMenus

    resolved_shows = SimklMenus()._resolve(tmdb_ids, "show")
    seen_show_ids = set(covered_show_ids)
    items = []
    for show in resolved_shows:
        trakt_show_id = show.get("trakt_id")
        if not trakt_show_id or trakt_show_id in seen_show_ids:
            continue
        seen_show_ids.add(trakt_show_id)
        episodes = shows_db.get_episode_list(trakt_show_id, hide_unaired=False) or []
        episode = _pick_relevant_episode(episodes)
        if episode:
            items.append(episode)
    return items


def _pick_relevant_episode(episodes):
    """Picks whichever of 'next unaired' or 'most recently aired' falls
    nearest today from a show's full episode list - matching what a single
    calendar row for this show should show."""
    now = g.datetime_to_string(datetime.datetime.utcnow())
    dated = [(e, get_air_date(e)) for e in episodes]
    dated = [(e, d) for e, d in dated if d]
    upcoming = [(e, d) for e, d in dated if d >= now]
    if upcoming:
        return min(upcoming, key=lambda pair: pair[1])[0]
    if dated:
        return max(dated, key=lambda pair: pair[1])[0]
    return None
