"""
Periodic cross-service reconciliation (Phase 7, Layer 1). Catches changes
made directly on a service's own website/app, outside Kodi - the on-action
mirror (modules/cross_sync.py, Layer 2) only ever fires from a Kodi-
originated action.

Checked on Seren's existing maintenance-loop cadence (service.py) but only
actually executes once per crosssync.intervalMinutes (default 180) - the
Sync Tools "Force a sync" button bypasses the interval via run(force=True).
Also gated on at least 2 of {trakt, mdblist, simkl} being configured - a
genuinely N-way design, not Trakt-hub-centric, so a Simkl+MDBList-only setup
with no Trakt still gets a full bridge between the two. If crosssync.enabled
is left on from before a service dropped below that threshold (disabled,
revoked, or never re-authed), _enabled() self-heals it back to off as a side
effect - see its own comment. Only ever forces off, never back on.

Snapshot-delta reconciliation, not a raw current-state diff. Each
(domain, service, media_type) keeps a persisted snapshot of its last fully-
accounted-for item set (database/bridge_sync). Every run:
  newly_added = current - snapshot
and only that gets pushed outward, to whichever other active services are
missing it. An item only enters the next snapshot once it's confirmed
present everywhere (already there, or the push just succeeded) - a failed
push leaves it out, so it's retried next run instead of silently dropped.
Items that vanish from current are dropped from the snapshot too, so a later
genuine re-add is still recognised as new.

This is what avoids the reversal bug a raw current-state diff has: if an
item is removed from service A directly (outside Kodi) while service B still
has it, a naive `current_B - current_A` diff would treat B's copy as
"missing from A, push it" and resurrect the very thing the user just
removed. Snapshot-gating means B's copy is old news (it was already in
snapshot_B last run), not new evidence - it only gets pushed if it
genuinely disappeared from B too and then came back.

Domain coverage, and why it stops where it does (evidence-based exclusions,
not policy shortcuts - see Simkl_Implementation_Plan.md's Phase 7 section
for the full write-up):
- Watched/movie: full 3-way (trakt, mdblist, simkl) - all three have a local
  cache with per-movie tmdb_id + a real watched flag.
- Watched/episode: trakt + mdblist only. Simkl's local cache only stores an
  aggregate watched_episodes_count per show, not per-episode identity (see
  database/simkl_sync/activities.py's _sync_shows docstring) - a hard
  data-model constraint. Episode marks made in Kodi via Simkl still mirror
  out fine through cross_sync.py; only detecting a change made directly on
  Simkl's own site, at episode granularity, is out of reach without extra
  live calls this module deliberately doesn't make (see next point).
- Watchlist/movie+tvshow: trakt + mdblist only, both read live (neither has
  a local watchlist cache). Simkl's plantowatch status is fully covered by
  cross_sync.py's Layer 2 (add-only, on Kodi actions) but NOT bridged here:
  a bulk read means Simkl's sync/all-items family, which
  simkl_sync/activities.py's own docstring already documents a client_id
  suspension risk for polling ungated, and there's no confirmed cursor field
  for plantowatch specifically the way completed/watching already have.
  Deferred pending that verification - same bucket as Rating below, not
  silently dropped.
- Collection/movie+tvshow: trakt + mdblist only. Simkl has no Collection
  endpoint at all (confirmed absent from its API).
- Progress: one-directional Trakt -> mdblist/simkl, an idempotent re-push of
  Trakt's current playback rows every run. No snapshot: a resume position
  changes on every playback anyway, so there's nothing meaningful to delta.
- Rating: deferred entirely (advisor-reviewed). It's a value+timestamp, not
  set-membership, so it needs a different mechanism than everything above,
  and depends on two unverified reads - whether MDBList's GET /sync/ratings
  actually returns bulk data (unverified; a similar doubt about GET
  /sync/watched turned out to be a code nesting bug, not an API gap, once
  live-checked - see mdblist_sync/activities.py's _sync_watched() - so that
  precedent isn't evidence either way for ratings and this needs its own
  live check), and whether Simkl exposes a bulk ratings read at all (its
  documented read is path-keyed by rating value, up to ~20 calls to
  enumerate). cross_sync.py's on-action Rating mirror is
  unaffected by this deferral - it only needs the already-confirmed-working
  write endpoints.
"""

import time

from resources.lib.modules.globals import g
from resources.lib.modules.syncGateway import configured_providers

_WATCHED = "watched"
_WATCHLIST = "watchlist"
_COLLECTION = "collection"


def _enabled():
    if not g.get_bool_setting("crosssync.enabled", False):
        return False
    if len(configured_providers()) < 2:
        # Stale "on" toggle from before a service dropped below the 2-provider minimum
        # (disabled, revoked, or never re-authed) - correct it here, on Seren's existing
        # maintenance-loop cadence (service.py) and on-demand via Force a sync, rather than
        # only reactively on the next settings-dialog close. Never auto-enables back on.
        g.set_setting("crosssync.enabled", False)
        return False
    return True


def _interval_elapsed():
    # crosssync.lastRunAt is an undeclared hidden setting, not in settings.xml (same pattern as simkl.anime_completed_at).
    interval_seconds = g.get_int_setting("crosssync.intervalMinutes", 180) * 60
    last_run = g.get_float_setting("crosssync.lastRunAt", 0.0)
    return (time.time() - last_run) >= interval_seconds


def run(force=False):
    if not _enabled():
        return
    if not force and not _interval_elapsed():
        return

    services = configured_providers()
    from resources.lib.database.bridge_sync import BridgeSyncDatabase

    bridge_db = BridgeSyncDatabase()

    _reconcile_watched_movies(services, bridge_db)
    _reconcile_watched_episodes(services, bridge_db)
    _reconcile_watchlist(services, bridge_db)
    _reconcile_collection(services, bridge_db)
    _push_progress(services)

    g.set_setting("crosssync.lastRunAt", time.time())


def _safe_read(label, func):
    try:
        return func()
    except Exception as e:
        g.log(f"bridgeSync: read failed ({label}): {e}", "error")
        return {}


def _reconcile(domain, media_type, services_current, bridge_db, batch_push_fn):
    """services_current: {service: {item_key: tmdb_id}}. batch_push_fn(items,
    source, target) -> (succeeded, given_up): both subsets of items.keys().
    items is {item_key: tmdb_id} for every item this source has that target
    is missing, sent as one or more batched API calls per (source, target)
    pair, not one call per item - these libraries run into the hundreds, and
    a per-item loop would fire that many sequential POSTs on the very first
    run.

    succeeded = confirmed present on target now (pushed and accepted, or
    already there). given_up = permanently unresolvable on target (e.g. a
    dead tmdb_id the target's catalog doesn't recognise) - counted as
    reconciled so it stops being retried forever, but NOT merged into
    services_current[target] since it genuinely isn't present there.
    Anything in neither set is a real (possibly transient) failure and
    stays in `missing` next run, retried as before.

    This distinction exists because a strict all-or-nothing gate (the whole
    batch counted as failed unless every single item confirmed) silently and
    permanently stalled reconciliation in production: a ~409-item first-run
    push to MDBList failed because ONE tmdb_id came back in its response's
    not_found list, so the response's updated-count fell one short of count
    and the entire batch - including the ~408 items MDBList had genuinely
    accepted - was discarded and retried identically forever (see
    Project_Report_Full.md's live-test session for the full diagnosis,
    confirmed against real API responses, not inferred). Individual push
    functions that can positively identify which specific items failed
    (currently: MDBList's watched-movie push, via its not_found field)
    report that precisely; ones that can't still return all-or-nothing
    (empty succeeded/given_up on failure) exactly as before - this is an
    additive capability, not a required behavior change for every target.

    This retry is NOT safe because these endpoints are idempotent - Trakt's
    sync/history specifically isn't: it's an additive play-log
    (mark_movie_watched does watched = play_count + 1), so re-sending an
    already-recorded item there logs another play, not a no-op (MDBList and
    Simkl's watched/watchlist/collection endpoints ARE genuine set-membership
    and report a repeat under "existing", but Trakt history doesn't get that
    same free pass). What actually keeps a retried batch from re-pushing
    items that already succeeded is the missing-guard above
    (`k not in services_current[target]`) combined with running after this
    cycle's provider syncs (service.py sleeps 15s before calling this) - by
    the next bridge run, a successfully-pushed item is visible in the
    target's own freshly-synced local read and drops out of `missing` on its
    own, without needing this module to have recorded the batch as
    successful. This self-heal is what the original design leaned on to
    bound a partial-batch failure to one extra retry. It used to not hold
    for MDBList's movie table specifically, which was permanently write-only
    at the time this mechanism was added (GET /sync/watched didn't return
    movies due to a since-fixed nesting bug) - a push there could never
    become visible in a later cycle's local read no matter how long this
    module kept retrying, which is why the succeeded/given_up split was
    added for that path. MDBList movies are now reconciled from remote by
    _sync_watched() the same as every other domain (live-confirmed
    2026-07-18), so self-heal does eventually reach this path too - but that
    reconciliation only fires when MDBList's own activity timestamp
    indicates staleness, which can lag a push by more than one cycle. The
    not_found-based split is kept regardless: it confirms success from the
    push response immediately instead of waiting on a reconciliation cycle
    of uncertain timing - the same rationale that would justify adding it
    for any other target capable of the same precision.

    Mutates services_current in place so a push earlier in this run is
    visible to later sources in the same run (lets a single run fully
    converge a 3-way propagation instead of taking multiple cycles)."""
    services = list(services_current)
    for source in services:
        snapshot_source = bridge_db.get_snapshot(domain, source, media_type)
        current_source = services_current[source]
        newly = {k: v for k, v in current_source.items() if k not in snapshot_source}
        carried = {k: v for k, v in snapshot_source.items() if k in current_source}

        unresolved_keys = set()
        for target in services:
            if target == source:
                continue
            missing = {k: v for k, v in newly.items() if k not in services_current[target]}
            if not missing:
                continue
            try:
                succeeded, given_up = batch_push_fn(missing, source, target)
            except Exception as e:
                succeeded, given_up = set(), set()
                g.log(
                    f"bridgeSync: {domain}/{media_type} batch push {source}->{target} "
                    f"failed for {len(missing)} item(s): {e}",
                    "error",
                )
            if succeeded:
                services_current[target].update({k: missing[k] for k in succeeded})
            for item_key in given_up:
                g.log(
                    f"bridgeSync: {domain}/{media_type} item {item_key} not found "
                    f"on {target}, giving up (won't retry)",
                    "warning",
                )
            unresolved_keys.update(set(missing) - succeeded - given_up)

        fully_synced = {k: v for k, v in newly.items() if k not in unresolved_keys}
        bridge_db.replace_snapshot(domain, source, media_type, {**carried, **fully_synced})


def _parse_item_key(item_key):
    parts = item_key.split(":")
    if len(parts) == 1:
        return {"tmdb_id": int(parts[0])}
    tmdb_id, season, number = parts
    return {"tmdb_id": int(tmdb_id), "season": int(season), "number": int(number)}


def _synced_count(response, key):
    """added+existing both count as "confirmed present on target" - matches
    how the pre-existing per-item context-menu actions already treat these
    same APIs (gui/trakt_context_menu.py's _confirm_marked_watched,
    _confirm_simkl_write). existing is 0/absent for endpoints that don't
    report it (e.g. Trakt's sync/history, which is an event log, not a set),
    so this degrades safely to added-only there."""
    if not response:
        return 0
    return (response.get("added") or {}).get(key, 0) + (response.get("existing") or {}).get(key, 0)


def _updated_count(response, key):
    """MDBList's watched/collection sync report a single 'updated' count,
    not an added/existing split (verified in mdblist.apib)."""
    if not response:
        return 0
    return (response.get("updated") or {}).get(key, 0)


# region Watched


def _reconcile_watched_movies(services, bridge_db):
    services_current = {}
    for service in services:
        if service == "trakt":
            services_current[service] = _safe_read("trakt watched movies", _read_trakt_watched_movies)
        elif service == "mdblist":
            services_current[service] = _safe_read("mdblist watched movies", _read_mdblist_watched_movies)
        elif service == "simkl":
            services_current[service] = _safe_read("simkl watched movies", _read_simkl_watched_movies)

    _reconcile(_WATCHED, "movie", services_current, bridge_db, _push_watched_movies_batch)


def _read_trakt_watched_movies():
    from resources.lib.database.trakt_sync.movies import TraktSyncDatabase

    rows = TraktSyncDatabase().get_all_watched_movie_tmdb_ids()
    return {str(r["tmdb_id"]): r["tmdb_id"] for r in rows}


def _read_mdblist_watched_movies():
    from resources.lib.database.mdblist_sync import MDBListSyncDatabase

    rows = MDBListSyncDatabase().get_all_watched_movie_tmdb_ids()
    return {str(r["tmdb_id"]): r["tmdb_id"] for r in rows}


def _read_simkl_watched_movies():
    from resources.lib.database.simkl_sync import SimklSyncDatabase

    rows = SimklSyncDatabase().get_recent_movies(None, force_all=True)
    return {str(r["tmdb_id"]): r["tmdb_id"] for r in rows}


_MDBLIST_WATCHED_MOVIES_CHUNK_SIZE = 50


def _chunked(items, size):
    """Splits {item_key: tmdb_id} into consecutive sub-dicts of at most
    `size` items each, preserving iteration order."""
    keys = list(items)
    for i in range(0, len(keys), size):
        yield {k: items[k] for k in keys[i : i + size]}


def _push_watched_movies_batch(items, source, target):
    """Returns (succeeded, given_up) - see _reconcile's docstring for the
    contract and the production incident that motivated it. Trakt stays
    all-or-nothing (unchanged from before, do not add per-item retry here):
    sync/history is an additive event log with no itemized not_found field
    documented, so a retried item that already succeeded logs a second real
    play - live-confirmed 2026-07-18 (a mixed-result batch's 4 good items
    were already present with plays=1 and a single clean timestamp before
    any retry was attempted; a per-item retry fallback would have double-
    played every one of them). The pre-existing missing-guard already
    self-heals a single bad item within one extra retry cycle, also live-
    confirmed that session. MDBList and Simkl are both not_found-aware
    (simkl.apib confirms sync/history's response includes an itemized
    not_found.movies[] and explicitly warns against inferring success from
    the 201 status alone) and both are genuine set-membership endpoints, so
    retrying an already-succeeded item there just reports it under
    "existing" - safe, unlike Trakt. Not_found-awareness alone doesn't prove
    that though (an additive log can report not_found too) - the actual
    idempotency guarantee for Simkl is spec-confirmed separately: simkl.apib's
    Add to History section states "Already-completed items don't bump on
    subsequent POST /sync/history calls" unless the caller opts in with
    ?allow_rewatch=yes, which this call never sets (plain payload, no query
    params - see the target == "simkl" branch below). Only MDBList is chunked - live-tested
    up to 200 real items in one call with no size-related failure (this
    chunk size leaves a comfortable margin below that, not a discovered
    hard limit); no equivalent evidence Simkl needs chunking, so its call
    stays a single request.

    When a chunk/batch's updated+not_found counts don't fully add up (an
    "ambiguous shortfall" - some item neither confirmed updated nor
    reported not_found), MDBList and Simkl isolate it by recursing into
    this same function once per item still in that chunk, reusing the
    classification logic below instead of duplicating it - a single-item
    call either lands cleanly (updated or not_found) or hits this same
    ambiguous branch again with count==1, the base case: it gives up on
    just that one item for this cycle (stays unresolved, retried next
    cycle) rather than recursing further. This replaced an earlier all-or-
    nothing fallback (leave the whole chunk/batch unresolved) that was the
    original production bug for MDBList specifically: a single dead
    tmdb_id made the updated-count gate fail 100% of a ~409-item first-run
    batch, forever, silently. That specific failure mode (a clean
    not_found hit blocking its batch-mates) was already fixed by the
    not_found-based classification below, live-confirmed 2026-07-18 to
    isolate correctly within the same cycle with no change needed here.
    This recursive fallback instead covers the narrower ambiguous case
    above it, which had no confirmed live occurrence as of that same
    session - added as defense-in-depth at explicit user request, not in
    response to an observed failure."""
    count = len(items)
    if target == "trakt":
        from resources.lib.indexers.trakt import TraktAPI

        payload = {"movies": [{"ids": {"tmdb": tmdb_id}} for tmdb_id in items.values()]}
        response = TraktAPI().post_json("sync/history", payload)
        if _synced_count(response, "movies") >= count:
            return set(items), set()
        return set(), set()
    elif target == "mdblist":
        from resources.lib.indexers.mdblist import MDBListAPI

        api = MDBListAPI()
        succeeded, given_up = set(), set()
        for chunk in _chunked(items, _MDBLIST_WATCHED_MOVIES_CHUNK_SIZE):
            payload = {"movies": [{"ids": {"tmdb": tmdb_id}} for tmdb_id in chunk.values()]}
            response = api.post_json("sync/watched", payload)
            if not response:
                continue
            not_found_ids = {
                entry.get("ids", {}).get("tmdb")
                for entry in (response.get("not_found") or {}).get("movies") or []
            }
            if _updated_count(response, "movies") < len(chunk) - len(not_found_ids):
                if len(chunk) == 1:
                    # base case: still ambiguous alone - stays unresolved,
                    # retried next cycle same as any other real failure.
                    continue
                # updated+not_found doesn't fully account for this chunk -
                # isolate item-by-item so a single bad/ambiguous id can't
                # keep the rest of the chunk from being classified.
                for item_key, tmdb_id in chunk.items():
                    s, g = _push_watched_movies_batch({item_key: tmdb_id}, source, target)
                    succeeded |= s
                    given_up |= g
                continue
            for item_key, tmdb_id in chunk.items():
                (given_up if tmdb_id in not_found_ids else succeeded).add(item_key)
        return succeeded, given_up
    elif target == "simkl":
        from resources.lib.indexers.simkl import SimklAPI

        payload = {"movies": [{"ids": {"tmdb": tmdb_id}} for tmdb_id in items.values()]}
        response = SimklAPI().post_json("sync/history", payload)
        if not response:
            return set(), set()
        not_found_ids = {
            entry.get("ids", {}).get("tmdb") for entry in (response.get("not_found") or {}).get("movies") or []
        }
        if _synced_count(response, "movies") < count - len(not_found_ids):
            if count == 1:
                # base case: still ambiguous alone - stays unresolved,
                # retried next cycle same as any other real failure.
                return set(), set()
            # Same ambiguous-shortfall isolation as MDBList's branch above.
            succeeded, given_up = set(), set()
            for item_key, tmdb_id in items.items():
                s, g = _push_watched_movies_batch({item_key: tmdb_id}, source, target)
                succeeded |= s
                given_up |= g
            return succeeded, given_up
        succeeded, given_up = set(), set()
        for item_key, tmdb_id in items.items():
            (given_up if tmdb_id in not_found_ids else succeeded).add(item_key)
        return succeeded, given_up
    return set(), set()


def _reconcile_watched_episodes(services, bridge_db):
    services_current = {}
    if "trakt" in services:
        services_current["trakt"] = _safe_read("trakt watched episodes", _read_trakt_watched_episodes)
    if "mdblist" in services:
        services_current["mdblist"] = _safe_read("mdblist watched episodes", _read_mdblist_watched_episodes)

    _reconcile(_WATCHED, "episode", services_current, bridge_db, _push_watched_episodes_batch)


def _read_trakt_watched_episodes():
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

    rows = TraktSyncDatabase().get_all_watched_episode_tmdb_keys()
    return {
        f"{r['show_tmdb_id']}:{r['season']}:{r['number']}": r["show_tmdb_id"]
        for r in rows
    }


def _read_mdblist_watched_episodes():
    from resources.lib.database.mdblist_sync import MDBListSyncDatabase

    rows = MDBListSyncDatabase().get_all_watched_episode_tmdb_keys()
    return {
        f"{r['show_tmdb_id']}:{r['season']}:{r['number']}": r["show_tmdb_id"]
        for r in rows
    }


def _episodes_payload_shows(items):
    """One shows[] entry per episode, even when several episodes share a
    show_tmdb_id - both Trakt and MDBList process each array entry as its
    own directive, so repeating the show id across entries is fine and
    avoids needing to group episodes by show client-side."""
    shows = []
    for item_key, show_tmdb_id in items.items():
        parsed = _parse_item_key(item_key)
        shows.append(
            {
                "ids": {"tmdb": show_tmdb_id},
                "seasons": [{"number": parsed["season"], "episodes": [{"number": parsed["number"]}]}],
            }
        )
    return shows


_MDBLIST_WATCHED_EPISODES_CHUNK_SIZE = 50


def _push_watched_episodes_batch(items, source, target):
    count = len(items)
    if target == "trakt":
        from resources.lib.indexers.trakt import TraktAPI

        response = TraktAPI().post_json("sync/history", {"shows": _episodes_payload_shows(items)})
        if _synced_count(response, "episodes") >= count:
            return set(items), set()
        return set(), set()
    elif target == "mdblist":
        # Chunked (same size precedent as the watched-movies branch) but not
        # not_found-aware: mdblist.apib's POST /sync/watched response is only
        # {"updated": {...}} - no itemized not_found field to attribute a
        # shortfall to specific episodes, unlike the movies push (confirmed
        # live per that branch's own docstring, not spec-documented). A chunk
        # that doesn't fully account for itself is left entirely unresolved
        # for retry - chunking here only bounds a single bad item to costing
        # its own chunk instead of the whole batch, same as
        # _push_collection_batch's mdblist branch.
        from resources.lib.indexers.mdblist import MDBListAPI

        api = MDBListAPI()
        succeeded = set()
        for chunk in _chunked(items, _MDBLIST_WATCHED_EPISODES_CHUNK_SIZE):
            response = api.post_json("sync/watched", {"shows": _episodes_payload_shows(chunk)})
            if _updated_count(response, "episodes") >= len(chunk):
                succeeded.update(chunk)
        return succeeded, set()
    return set(), set()


# endregion

# region Watchlist (trakt <-> mdblist only - see module docstring)


def _reconcile_watchlist(services, bridge_db):
    services_current = {}
    if "trakt" in services:
        services_current["trakt"] = _safe_read("trakt watchlist", _read_trakt_watchlist)
    if "mdblist" in services:
        services_current["mdblist"] = _safe_read("mdblist watchlist", _read_mdblist_watchlist)

    if len(services_current) < 2:
        return
    _reconcile(_WATCHLIST, "movie_or_show", services_current, bridge_db, _push_watchlist_batch)


def _read_trakt_watchlist():
    """Paginates explicitly (page/limit) because Trakt applies this
    endpoint's default pagination even with no params passed at all
    (confirmed live: an unparameterized call already comes back capped at
    limit:100), and silently caps limit at 250 when a larger value is
    requested - unlike sync/playback (see _push_progress), which stays
    fully unpaginated unless page/limit are explicitly passed, this
    endpoint is paginated whether you ask for it or not. Stops on a
    short/empty page rather than trusting X-Pagination-Item-Count/
    Page-Count, which report the combined movies+shows watchlist size on
    both type-filtered endpoints (confirmed live: movies-only and
    shows-only calls both reported item-count 8 for a true 4-movie/
    4-show split), not the type-specific count."""
    from resources.lib.indexers.trakt import TraktAPI

    api = TraktAPI()
    current = {}
    limit = 250  # Trakt's actual max page size for this endpoint (server-capped, confirmed live)
    for endpoint, media_key in (("movies", "movie"), ("shows", "show")):
        page = 1
        for _ in range(50):  # safety cap well above any realistic page count
            data = api.get_json(f"users/me/watchlist/{endpoint}", page=page, limit=limit)
            if data is None:
                g.log(f"bridgeSync: trakt watchlist {endpoint} read failed mid-pagination, returning partial result", "warning")
                break
            if page == 1 and data:
                g.log(f"bridgeSync: trakt watchlist {endpoint} entry keys: {sorted(data[0].keys())}", "debug")
            for entry in data:
                # TraktAPI.get_json() normalizes responses (_handle_response ->
                # _create_trakt_object), which flattens ids.tmdb to a top-level
                # tmdb_id and removes the movie/show wrapper key entirely -
                # entry.get(media_key) is always None post-normalization.
                tmdb_id = entry.get("tmdb_id")
                if tmdb_id:
                    current[f"{media_key}:{tmdb_id}"] = tmdb_id
            if len(data) < limit:
                break
            page += 1
    return current


def _read_mdblist_watchlist():
    """Paginates explicitly (offset/limit, no cursor - same shape as
    sync/collection) because the server silently caps limit at 1000
    regardless of what's requested (confirmed live: requesting 5000 still
    echoes back limit:1000), so a watchlist past 1000 items would otherwise
    only ever see its first page. Feeds both _reconcile_watchlist's
    current-state read and _push_watchlist_batch's given_up read-back."""
    from resources.lib.indexers.mdblist import MDBListAPI

    api = MDBListAPI()
    current = {}
    offset = 0
    limit = 1000  # mdblist's actual max page size for watchlist/items (server-capped, confirmed live)
    for _ in range(50):  # safety cap well above any realistic page count
        data = api.get_json("watchlist/items", offset=offset, limit=limit)
        if data is None:
            g.log("bridgeSync: mdblist watchlist read failed mid-pagination, returning partial result", "warning")
            break
        for entry in data.get("movies") or []:
            tmdb_id = entry.get("id")
            if tmdb_id:
                current[f"movie:{tmdb_id}"] = tmdb_id
        for entry in data.get("shows") or []:
            tmdb_id = entry.get("id")
            if tmdb_id:
                current[f"show:{tmdb_id}"] = tmdb_id
        if not (data.get("pagination") or {}).get("has_more"):
            break
        offset += limit
    return current


def _split_movie_show(items):
    """item_key is 'movie:<tmdb_id>' or 'show:<tmdb_id>' - splits a mixed
    batch into two plain tmdb_id lists."""
    movies, shows = [], []
    for item_key, tmdb_id in items.items():
        media_kind, _, _ = item_key.partition(":")
        (movies if media_kind == "movie" else shows).append(tmdb_id)
    return movies, shows


_MDBLIST_WATCHLIST_CHUNK_SIZE = 50


def _push_watchlist_batch(items, source, target):
    """mdblist branch is chunked; watchlist/items/add's own not_found field is
    only a count ({"movies": N, "shows": N}, verified in mdblist.apib), not an
    itemized id list like the watched-movies endpoint, so it can gate that
    something in a chunk was rejected but can't say which. A chunk that falls
    short of its own count AND reports a nonzero not_found triggers one
    read-back of the live watchlist (_read_mdblist_watchlist, already relied
    on elsewhere in this module as unpaginated/reliable) to identify exactly
    which item(s) didn't land - those are given_up, the rest succeeded. A
    shortfall with not_found == 0 (unexplained by mdblist's own accounting)
    is left entirely unresolved and retried whole next cycle, same as before
    this fix - only an explicit rejection is trusted as permanent. The
    read-back only fires on a shortfall, so the common all-succeeded case
    costs no extra API call."""
    movies, shows = _split_movie_show(items)
    count = len(items)
    if target == "trakt":
        from resources.lib.indexers.trakt import TraktAPI

        payload = {}
        if movies:
            payload["movies"] = [{"ids": {"tmdb": t}} for t in movies]
        if shows:
            payload["shows"] = [{"ids": {"tmdb": t}} for t in shows]
        response = TraktAPI().post_json("sync/watchlist", payload)
        if _synced_count(response, "movies") + _synced_count(response, "shows") >= count:
            return set(items), set()
        return set(), set()
    elif target == "mdblist":
        from resources.lib.indexers.mdblist import MDBListAPI

        api = MDBListAPI()
        succeeded = set()
        given_up = set()
        for chunk in _chunked(items, _MDBLIST_WATCHLIST_CHUNK_SIZE):
            chunk_movies, chunk_shows = _split_movie_show(chunk)
            payload = {}
            if chunk_movies:
                payload["movies"] = [{"tmdb": t} for t in chunk_movies]
            if chunk_shows:
                payload["shows"] = [{"tmdb": t} for t in chunk_shows]
            response = api.post_json("watchlist/items/add", payload)
            if _synced_count(response, "movies") + _synced_count(response, "shows") >= len(chunk):
                succeeded.update(chunk)
                continue
            not_found = (response or {}).get("not_found") or {}
            if not (not_found.get("movies") or not_found.get("shows")):
                continue
            current_readback = _read_mdblist_watchlist()
            for item_key in chunk:
                if item_key in current_readback:
                    succeeded.add(item_key)
                else:
                    given_up.add(item_key)
        return succeeded, given_up
    return set(), set()


# endregion

# region Collection (trakt <-> mdblist only - simkl has no collection endpoint)


def _reconcile_collection(services, bridge_db):
    services_current = {}
    if "trakt" in services:
        services_current["trakt"] = _safe_read("trakt collection", _read_trakt_collection)
    if "mdblist" in services:
        services_current["mdblist"] = _safe_read("mdblist collection", _read_mdblist_collection)

    if len(services_current) < 2:
        return
    _reconcile(_COLLECTION, "movie_or_show", services_current, bridge_db, _push_collection_batch)


def _read_trakt_collection():
    from resources.lib.database.trakt_sync.movies import TraktSyncDatabase as MoviesDB
    from resources.lib.database.trakt_sync.shows import TraktSyncDatabase as ShowsDB

    current = {f"movie:{r['tmdb_id']}": r["tmdb_id"] for r in MoviesDB().get_all_collected_movie_tmdb_ids()}
    current.update({f"show:{r['tmdb_id']}": r["tmdb_id"] for r in ShowsDB().get_all_collected_show_tmdb_ids()})
    return current


def _read_mdblist_collection():
    """Paginates explicitly (offset/limit, no cursor - confirmed against
    mdblist.apib and a live call) because an unspecified limit silently
    defaults to ~100 despite the spec documenting 1000, and mdblist
    auto-cascades every episode of a show-level collection add into the same
    response - real movies/shows entries can land past page 1, pushed there
    by hundreds of cascaded episode records, and get silently missed."""
    from resources.lib.indexers.mdblist import MDBListAPI

    api = MDBListAPI()
    current = {}
    offset = 0
    limit = 5000  # mdblist's documented max page size for sync/collection
    for _ in range(50):  # safety cap well above any realistic page count
        data = api.get_json("sync/collection", offset=offset, limit=limit)
        if data is None:
            g.log("bridgeSync: mdblist collection read failed mid-pagination, returning partial result", "warning")
            break
        for entry in data.get("movies") or []:
            tmdb_id = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
            if tmdb_id:
                current[f"movie:{tmdb_id}"] = tmdb_id
        for entry in data.get("shows") or []:
            tmdb_id = ((entry.get("show") or {}).get("ids") or {}).get("tmdb")
            if tmdb_id:
                current[f"show:{tmdb_id}"] = tmdb_id
        if not (data.get("pagination") or {}).get("has_more"):
            break
        offset += limit
    return current


_MDBLIST_COLLECTION_CHUNK_SIZE = 50


def _push_collection_batch(items, source, target):
    """mdblist branch is chunked but not not_found-aware: sync/collection's
    response is only {"updated": {...}} (verified in mdblist.apib) - no
    not_found field at all, unlike the watched-movies endpoint. So a chunk
    that doesn't fully account for itself is left entirely unresolved (empty
    given_up, same as the pre-chunking fallback) rather than picking out
    individual bad ids - chunking here only bounds a single bad item to
    costing its own chunk instead of the whole batch, it can't identify which
    item was bad."""
    movies, shows = _split_movie_show(items)
    count = len(items)
    if target == "trakt":
        from resources.lib.indexers.trakt import TraktAPI

        payload = {}
        if movies:
            payload["movies"] = [{"ids": {"tmdb": t}} for t in movies]
        if shows:
            payload["shows"] = [{"ids": {"tmdb": t}} for t in shows]
        response = TraktAPI().post_json("sync/collection", payload)
        if _synced_count(response, "movies") + _synced_count(response, "shows") >= count:
            return set(items), set()
        return set(), set()
    elif target == "mdblist":
        from resources.lib.indexers.mdblist import MDBListAPI

        api = MDBListAPI()
        succeeded = set()
        for chunk in _chunked(items, _MDBLIST_COLLECTION_CHUNK_SIZE):
            chunk_movies, chunk_shows = _split_movie_show(chunk)
            payload = {}
            if chunk_movies:
                payload["movies"] = [{"ids": {"tmdb": t}} for t in chunk_movies]
            if chunk_shows:
                payload["shows"] = [{"ids": {"tmdb": t}} for t in chunk_shows]
            response = api.post_json("sync/collection", payload)
            if _updated_count(response, "movies") + _updated_count(response, "shows") >= len(chunk):
                succeeded.update(chunk)
        return succeeded, set()
    return set(), set()


# endregion

# region Progress (one-directional Trakt -> others, idempotent, no snapshot)


def _get_raw_json(api, url):
    """TraktAPI.get_json() normalizes responses (_handle_response), which
    flattens/renames fields (see _read_trakt_watchlist(), 3.3.240) -
    _push_progress_entry() below is built against Trakt's raw un-normalized
    movie/show/episode.ids.tmdb + episode.season/number shape (verified
    against modules/player.py's own request builders, per its docstring),
    so this bypasses normalization instead of adapting the extraction."""
    response = api.get(url)
    if response is None:
        return None
    try:
        return response.json()
    except (ValueError, AttributeError) as e:
        g.log(f"bridgeSync: failed to parse raw Trakt JSON for {url}: {e}", "error")
        return None


def _push_progress(services):
    if "trakt" not in services or not (g.get_setting("trakt.auth") and g.get_bool_setting("trakt.enabled", False)):
        return
    try:
        from resources.lib.indexers.trakt import TraktAPI

        api = TraktAPI()
        movies = _get_raw_json(api, "sync/playback/movies") or []
        episodes = _get_raw_json(api, "sync/playback/episodes") or []
        if movies:
            g.log(f"bridgeSync: trakt playback movie entry keys: {sorted(movies[0].keys())}", "debug")
        if episodes:
            g.log(f"bridgeSync: trakt playback episode entry keys: {sorted(episodes[0].keys())}", "debug")
    except Exception as e:
        g.log(f"bridgeSync: progress read failed: {e}", "error")
        return

    for entry in movies:
        _push_progress_entry(services, "movie", entry)
    for entry in episodes:
        _push_progress_entry(services, "episode", entry)


def _push_progress_entry(services, media_type, entry):
    """Payload shapes verified against modules/player.py's own
    _build_mdblist_object/_build_simkl_object (the scrobbler's already-live-
    tested request builders), not assumed from docs - MDBList and Simkl
    nest the episode identity differently (show.season.episode vs a sibling
    top-level episode key) and it would be easy to get this wrong by
    treating them as interchangeable."""
    progress = entry.get("progress")
    if progress is None:
        return
    # MDBList rejects progress values with more than 5 total digits
    # (e.g. Trakt's raw 37.7697 -> 400 "no more than 5 digits in total") -
    # round once, upfront, for both payloads. Guarded because round() is
    # the first numeric use of progress here - a non-numeric value would
    # otherwise raise uncaught and abort the rest of this cycle's loop.
    try:
        progress = round(progress, 2)
    except TypeError:
        return

    if media_type == "movie":
        tmdb_id = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
        if not tmdb_id:
            return
        mdblist_payload = {"movie": {"ids": {"tmdb": tmdb_id}}, "progress": progress}
        simkl_payload = {"progress": progress, "movie": {"ids": {"tmdb": tmdb_id}}}
    else:
        episode = entry.get("episode") or {}
        show_tmdb_id = ((entry.get("show") or {}).get("ids") or {}).get("tmdb")
        season = episode.get("season")
        number = episode.get("number")
        if not show_tmdb_id or season is None or number is None:
            return
        mdblist_payload = {
            "show": {
                "ids": {"tmdb": show_tmdb_id},
                "season": {"number": season, "episode": {"number": number}},
            },
            "progress": progress,
        }
        simkl_payload = {
            "progress": progress,
            "show": {"ids": {"tmdb": show_tmdb_id}},
            "episode": {"season": season, "number": number},
        }

    if "mdblist" in services:
        try:
            from resources.lib.indexers.mdblist import MDBListAPI

            if MDBListAPI().post_json("scrobble/pause", mdblist_payload) is None:
                g.log(f"bridgeSync: progress push to mdblist rejected (media_type={media_type})", "warning")
        except Exception as e:
            g.log(f"bridgeSync: progress push to mdblist failed: {e}", "error")

    if "simkl" in services:
        try:
            from resources.lib.indexers.simkl import SimklAPI

            if SimklAPI().post_json("scrobble/pause", simkl_payload) is None:
                g.log(f"bridgeSync: progress push to simkl rejected (media_type={media_type})", "warning")
        except Exception as e:
            g.log(f"bridgeSync: progress push to simkl failed: {e}", "error")


# endregion
