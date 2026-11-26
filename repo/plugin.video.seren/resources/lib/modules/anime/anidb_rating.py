"""AniDB group rating as a small tie-breaking bonus for the Release Group sort level (anime only).

AniDB tells, per anime, how its users rated each fansub group (rating 0-1000, and the number of votes). The lookup is made in the
background by anidb_udp (since 3.9.185) and kept in the cache; this module only reads that cache, never the network.

The bonus is deliberately tiny: at most +/-MAX_BONUS points. Two releases can carry opposite bonuses, so they can differ by
2 * MAX_BONUS (24), which is below the smallest gap between two TRaSH tiers (25, the anime profile's remux tiers): it can only
order releases that the tiers and SeaDex rank the same. It shrinks with few votes (a group with one vote of 10/10 hardly counts)
and needs MIN_VOTES votes at all. A group TRaSH lists as low quality is never lifted by it.

AniDB rates groups per anime (per season/cour), and Seren holds one show-level AniDB id, so the ratings are used only when that
id is the entry being played (first season, or a film); for any other season there is no bonus.
"""
import re

MAX_BONUS = 12          # 2 * MAX_BONUS must stay below the smallest gap between TRaSH tiers (25)
MIN_VOTES = 3
_NEUTRAL_RATING = 700   # AniDB group ratings run 0-1000; about 7/10 is an ordinary group
_PRIOR_VOTES = 5        # votes needed for a rating to count about half

_LEADING_TAG_RE = re.compile(r"^\s*\[([^\]]+)\]")


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def bonus_for(rating, votes):
    """Whole-number bonus for a group's AniDB rating (0-1000) and vote count; 0 without enough votes."""
    try:
        rating, votes = int(rating), int(votes)
    except (TypeError, ValueError):
        return 0
    if votes < MIN_VOTES or rating <= 0:
        return 0
    spread = max(-1.0, min(1.0, (rating - _NEUTRAL_RATING) / 300.0))
    return int(round(MAX_BONUS * spread * votes / (votes + _PRIOR_VOTES)))


class AniDBRatingSignal:
    """{normalised group name: bonus} for one anime, applied to a source through its release group."""

    def __init__(self, groups):
        self._bonus = {}
        for key, record in (groups or {}).items():
            if not isinstance(record, dict):
                continue
            bonus = bonus_for(record.get("rating"), record.get("votes"))
            if bonus:
                self._bonus[_norm(record.get("name") or key)] = bonus

    def __bool__(self):
        return bool(self._bonus)

    def bonus(self, source):
        """Bonus for a source's release group (leading [Group] or -GROUP suffix); 0 when its group has no usable rating."""
        title = source.get("release_title") or ""
        if not isinstance(title, str) or not title:
            return 0
        from resources.lib.modules.release_groups import extract_groups
        names = list(extract_groups(title))
        match = _LEADING_TAG_RE.match(title)
        if match:
            names.append(match.group(1))
        for name in names:
            value = self._bonus.get(_norm(name))
            if value:
                return value
        return 0


def played_anidb_id(item_information):
    """AniDB id of the entry (season/cour, or film) being played, from the offline Fribb index; None when it cannot be told."""
    info = (item_information or {}).get("info", {})
    if info.get("mediatype") != "episode":
        return info.get("anidb_id")   # a film is its own entry
    from resources.lib.modules.anime import fribb_mapping
    from resources.lib.modules.anime.anilist_mapping import _show_ids_from_info
    imdb_id, tmdb_id, _trakt_id = _show_ids_from_info(info)
    entry, reason = fribb_mapping.lookup_episode(
        imdb_id=imdb_id, tmdb_id=tmdb_id, season=info.get("season"), episode=info.get("episode"))
    if entry:
        return entry.get("anidb_id")
    try:
        season_ok = int(info.get("season")) >= 1
    except (TypeError, ValueError):
        season_ok = False
    if reason == "no_data" and season_ok:
        # no season information in the index for this show: with a single TV entry that entry is the show
        tv_rows = [r for r in fribb_mapping.show_entries(imdb_id=imdb_id, tmdb_id=tmdb_id) if r[1] == "TV"]
        if len(tv_rows) == 1:
            return info.get("anidb_id")
    return None


def signal_for_item(item_information, show_anidb_id):
    """The rating signal, only when the cached AniDB data is for the entry being played (see the module docstring)."""
    if not show_anidb_id:
        return None
    try:
        played = played_anidb_id(item_information)
        if played is None or int(played) != int(show_anidb_id):
            return None
        return signal_for(show_anidb_id)
    except Exception:
        return None


def signal_for(anidb_id):
    """AniDBRatingSignal from the cached group data (no network), or None when nothing usable is cached."""
    if not anidb_id:
        return None
    try:
        from resources.lib.modules.anime.anidb_udp import get_cached_group_data
        data = get_cached_group_data(anidb_id)
        if not data or data.get("_failed"):
            return None
        signal = AniDBRatingSignal(data)
        return signal if signal else None
    except Exception:
        return None
