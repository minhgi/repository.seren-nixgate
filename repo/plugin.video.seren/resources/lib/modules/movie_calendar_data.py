"""Movie calendar data: one calendar month of movies, ordered by release date."""
import calendar
import datetime
import re

import xbmc

from resources.lib.modules.globals import g
from resources.lib.modules.metadataHandler import MetadataHandler

_MOVIE_CALENDAR_ENDPOINT = "calendars/all/movies/{start_date}/{days}"


def get_movie_calendar_month(year, month):
    from resources.lib.modules.syncGateway import configured_providers

    if "trakt" not in configured_providers():
        return []

    from resources.lib.indexers.trakt import TraktAPI

    raw_items = _fetch_raw_trakt_calendar(TraktAPI(), year, month)
    if not raw_items:
        return []  # get_movie_list()'s WHERE trakt_id IN (...) has no empty-list guard

    # NOT i.get("released") - normalization drops that key entirely; a raw calendar
    # item's release date lives at trakt_object.info.aired after get_json()'s
    # normalization pass. Same accessor insert_trakt_movies() itself uses.
    released_by_trakt_id = {
        i.get("trakt_id"): MetadataHandler.get_trakt_info(i, "aired")
        for i in raw_items if i.get("trakt_id") and MetadataHandler.get_trakt_info(i, "aired")
    }

    from resources.lib.database.trakt_sync.movies import TraktSyncDatabase as MoviesDatabase

    movies_db = MoviesDatabase()
    movies_db.insert_trakt_movies(raw_items)          # bootstrap new rows - get_movie_list() alone won't
    hydrated = movies_db.get_movie_list(raw_items, hide_unaired=False, hide_watched=False)

    for movie in hydrated:
        _classify_and_annotate(movie, released_by_trakt_id.get(movie.get("trakt_id")))

    hydrated.sort(key=lambda m: (m.get("_calendar_released") or "", (m.get("info") or {}).get("title") or ""))
    return hydrated


def shift_month(year, month, delta):
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def _fetch_raw_trakt_calendar(trakt_api, year, month):
    days_in_month = calendar.monthrange(year, month)[1]
    start_date = datetime.date(year, month, 1)
    raw = trakt_api.get_json(
        _MOVIE_CALENDAR_ENDPOINT.format(start_date=start_date.strftime("%Y-%m-%d"), days=days_in_month),
        extended="full",
        pull_all=True,
    )
    return raw or []


def _classify_and_annotate(movie, released):
    # Checked on the raw hydrated dict, before get_list_item_with_properties()/add_directory_item()
    # injects a default-poster fallback that would otherwise mask "no poster."
    info = movie.get("info") or {}
    art = movie.get("art") or {}

    movie["_calendar_released"] = released
    movie["_calendar_countdown"] = _format_countdown(released)
    movie["_calendar_date_label"] = _format_release_date(released)

    has_poster = bool(art.get("poster"))
    # Key-presence check, NOT truthiness - a real 0-vote movie has
    # rating.tmdb={"rating":0.0,"votes":0}, which is populated data, not "thin" data.
    has_tmdb_rating = "rating.tmdb" in info

    rating_value = (info.get("rating.tmdb") or {}).get("rating")
    if rating_value is None:
        rating_value = info.get("rating")  # Trakt community rating - display fallback only
    movie["_calendar_rating_display"] = f"{rating_value:.1f}" if isinstance(rating_value, (int, float)) else ""

    if not _is_released(released):
        movie["_calendar_status"] = "upcoming"
    elif has_poster and has_tmdb_rating:
        movie["_calendar_status"] = "released_complete"
    else:
        movie["_calendar_status"] = "released_incomplete"


def _is_released(released):
    if not released:
        return False
    return released < g.datetime_to_string(datetime.datetime.utcnow())


def _format_countdown(released):
    if not released:
        return ""
    try:
        from resources.lib.common import tools

        days = (tools.parse_datetime(released) - datetime.date.today()).days
        return str(max(days, 0))
    except (ValueError, TypeError):
        return ""


def _format_release_date(released):
    if not released:
        return ""
    try:
        from resources.lib.common import tools

        # Drop the year - the calendar's own month/year header already carries it.
        # Regex-strip (not a hardcoded "%m/%d") to keep whatever day/month order
        # the user's Kodi region actually uses.
        date_format = re.sub(r"[/.\-]?%[Yy][/.\-]?", "", xbmc.getRegion("dateshort"), count=1)
        return tools.parse_datetime(released).strftime(date_format)
    except (ValueError, TypeError):
        return released
