"""SeaDex "best release" signal for the Release Group sort level (anime only).

SeaDex (seadex.moe, formerly releases.moe) is a curated list of the best torrent release per anime, keyed by AniList id. Two things are used:

    hash match   the source IS one of SeaDex's best torrents (same info hash)        -> HASH_BOOST
    group match  the source comes from a group SeaDex names as best for this entry   -> GROUP_BOOST

A hash match is exact, so it counts for every season of the show. A group name only counts for the entry (cour) that is
being played, found with the offline Fribb index; when that entry cannot be told, only hashes count.

One request per show, fetched in a background thread beside the scrape (it never delays a scrape) and kept for 7 days in
the AniDB group cache table under a negative key (-lowest AniList id of the show: AniDB ids are positive, so no clash and
no schema change). The sorter only reads that cache and never touches the network; no data, a failure or a miss is neutral.
"""
import json
import re
import threading
import time

import requests

from resources.lib.modules.globals import g

HASH_BOOST = 100000
GROUP_BOOST = 50000

_API = "https://seadex.moe/api/collections/entries/records"  # releases.moe only redirects here (one extra hop, ~3 s on a PC whose IPv6 is refused)
_TTL_SECONDS = 7 * 24 * 3600
_FAIL_COOLDOWN_SECONDS = 6 * 3600
_FAILED_KEY = "_failed"
_FIELDS = "alID,expand.trs.infoHash,expand.trs.releaseGroup,expand.trs.isBest,expand.trs.dualAudio"
_HASH_RE = re.compile(r"^[0-9a-f]{40}$")
_LEADING_TAG_RE = re.compile(r"^\s*\[([^\]]+)\]")

_state_lock = threading.Lock()
_inflight = set()


def level_enabled():
    """True when the user has picked the Release Group level (option 10) in any of the eight sort levels.

    Local settings only. Without it nothing is fetched and no SeaDex signal is loaded, so SeaDex is never contacted
    for someone who does not use the level."""
    try:
        return any(g.get_int_setting(f"general.sortmethod.{i}") == 10 for i in range(1, 9))
    except Exception:
        return False


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def resolve(item_information):
    """(all_ids, entry_id): every AniList id of the show (or film), and the one for the episode being played.

    entry_id is None when the index cannot say which entry the episode belongs to. Offline."""
    from resources.lib.modules.anime import fribb_mapping
    from resources.lib.modules.anime.anilist_mapping import _show_ids_from_info
    info = (item_information or {}).get("info", {})
    if info.get("mediatype") == "episode":
        imdb_id, tmdb_id, _trakt = _show_ids_from_info(info)
        rows = fribb_mapping.show_entries(imdb_id=imdb_id, tmdb_id=tmdb_id)
        all_ids = [r[0] for r in rows]
        entry, reason = fribb_mapping.lookup_episode(
            imdb_id=imdb_id, tmdb_id=tmdb_id, season=info.get("season"), episode=info.get("episode"))
        entry_id = entry.get("anilist_id") if entry else None
        if entry_id is None and reason == "no_data":
            # The index carries no season information for this show at all; with a single TV entry that entry is the show.
            # Never for "other_season" / "ambiguous": there the index knows the seasons and this is not one of them.
            tv_ids = [r[0] for r in rows if r[1] == "TV"]
            try:
                season_ok = int(info.get("season")) >= 1
            except (TypeError, ValueError):
                season_ok = False
            if len(tv_ids) == 1 and season_ok:
                entry_id = tv_ids[0]
        return all_ids, entry_id
    imdb_id = info.get("imdb_id")
    rows = fribb_mapping.show_entries(imdb_id=imdb_id) if imdb_id else []
    all_ids = [r[0] for r in rows]
    return all_ids, (all_ids[0] if len(all_ids) == 1 else None)


def _cache_key(all_ids):
    return -min(all_ids)


def _read_cache(all_ids):
    from resources.lib.database.animeCache import AnimeCache
    data = AnimeCache().get_fansub_groups(_cache_key(all_ids))
    return data if isinstance(data, dict) else None


def _write_cache(all_ids, data, ttl):
    from resources.lib.database.animeCache import AnimeCache
    AnimeCache().execute_sql(
        "INSERT OR REPLACE INTO anime_fansub_groups (anidb_id, groups_json, expires) VALUES (?, ?, ?)",
        (_cache_key(all_ids), json.dumps(data), int(time.time()) + ttl),
    )


def parse_entries(payload):
    """SeaDex records -> {"v": 1, "hashes": {info_hash: dual_audio}, "groups": {anilist_id: {group_key: dual_audio}}}.

    Only torrents SeaDex marks as best count. A redacted/empty hash (private trackers) still counts for its group."""
    data = {"v": 1, "hashes": {}, "groups": {}}
    for item in (payload or {}).get("items", []):
        al_id = str(item.get("alID"))
        for torrent in (item.get("expand") or {}).get("trs", []) or []:
            if not torrent.get("isBest"):
                continue
            dual = bool(torrent.get("dualAudio"))
            info_hash = str(torrent.get("infoHash") or "").lower()
            if _HASH_RE.match(info_hash):
                data["hashes"][info_hash] = data["hashes"].get(info_hash, False) or dual
            group = _norm(torrent.get("releaseGroup"))
            if group:
                entry = data["groups"].setdefault(al_id, {})
                entry[group] = entry.get(group, False) or dual
    return data


def fetch(all_ids):
    """Ask SeaDex for the best releases of these AniList ids. Returns the parsed data, or None on any failure."""
    try:
        flt = "(" + "||".join(f"alID={int(i)}" for i in all_ids) + ")"
        # fields= keeps the answer to the few fields used (43 KB -> 3 KB for a six-entry show); the timeout is generous
        # because this runs in a background thread and a failure would be remembered for 6 hours
        response = requests.get(_API, params={"filter": flt, "expand": "trs", "perPage": 100, "fields": _FIELDS},
                                headers={"User-Agent": "Seren"}, timeout=8)
        if not response.ok:
            return None
        return parse_entries(response.json())
    except Exception as e:
        g.log(f"SeaDex lookup failed: {e}", "debug")
        return None


def start_prefetch(item_information):
    """Start the SeaDex fetch for an anime item in a background thread. Returns the thread, or None when nothing started
    (level not in use, no ids, already cached, recently failed, already running)."""
    if not level_enabled():
        return None
    try:
        all_ids, _entry = resolve(item_information)
    except Exception as e:
        g.log(f"SeaDex ids not resolved: {e}", "debug")
        return None
    if not all_ids:
        return None
    try:
        if _read_cache(all_ids) is not None:
            return None
    except Exception:
        return None
    key = _cache_key(all_ids)
    with _state_lock:
        if key in _inflight:
            return None
        _inflight.add(key)

    def _run():
        try:
            data = fetch(all_ids)
            if data is None:
                _write_cache(all_ids, {_FAILED_KEY: True}, _FAIL_COOLDOWN_SECONDS)
            else:
                _write_cache(all_ids, data, _TTL_SECONDS)
        except Exception as e:
            g.log(f"SeaDex background fetch failed: {e}", "debug")
        finally:
            with _state_lock:
                _inflight.discard(key)

    thread = threading.Thread(target=_run, name=f"seadex-{-key}", daemon=True)
    thread.start()
    return thread


class SeaDexSignal:
    """What the sorter needs: the show's best hashes and the best groups of the entry being played."""

    def __init__(self, data, entry_id, dub_preference=0):
        self._hashes = data.get("hashes", {})
        self._groups = data.get("groups", {}).get(str(entry_id), {}) if entry_id is not None else {}
        self._want_dual = dub_preference == 1  # "Prefer Dubbed": only dual-audio best releases count

    def _counts(self, dual):
        return dual or not self._want_dual

    def boost(self, source):
        """HASH_BOOST, GROUP_BOOST or 0 for a source."""
        info_hash = str(source.get("hash") or "").lower()
        if info_hash in self._hashes and self._counts(self._hashes[info_hash]):
            return HASH_BOOST
        if self._groups:
            from resources.lib.modules.release_groups import extract_groups
            title = source.get("release_title") or ""
            names = list(extract_groups(title))
            match = _LEADING_TAG_RE.match(title)
            if match:
                names.append(match.group(1))
            for name in names:
                key = _norm(name)
                if key in self._groups and self._counts(self._groups[key]):
                    return GROUP_BOOST
        return 0


def signal_for(item_information, dub_preference=0):
    """SeaDexSignal for an anime item from the cache (no network), or None when there is nothing usable."""
    if not level_enabled():
        return None
    try:
        all_ids, entry_id = resolve(item_information)
        if not all_ids:
            return None
        data = _read_cache(all_ids)
        if not data or data.get(_FAILED_KEY) or not (data.get("hashes") or data.get("groups")):
            return None
        return SeaDexSignal(data, entry_id, dub_preference)
    except Exception as e:
        g.log(f"SeaDex signal not available: {e}", "debug")
        return None
