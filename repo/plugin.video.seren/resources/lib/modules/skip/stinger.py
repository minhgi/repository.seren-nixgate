"""Films with a scene during or after the credits, from TMDB's movie keywords.

TMDB tags such films with the keywords "duringcreditsstinger" (id 179431) and "aftercreditsstinger" (id 179430).
The answer for a film never really changes, so it is cached for 30 days. A failed lookup is not cached (tried again next time).
"""

import datetime

from resources.lib.database.cache import CacheBase
from resources.lib.modules.globals import g

_DURING = ("duringcreditsstinger", 179431)
_AFTER = ("aftercreditsstinger", 179430)
_CACHE_DAYS = 30


def get_stingers(tmdb_id):
    """Return (scene_during_credits, scene_after_credits) as bools, or None when TMDB could not be asked."""
    if not tmdb_id:
        return None

    cache_key = f"skip.stinger.{tmdb_id}"
    cached = g.CACHE.get(cache_key)
    if cached != CacheBase.NOT_CACHED:
        return bool(cached[0]), bool(cached[1])

    from resources.lib.indexers.tmdb import TMDBAPI

    response = TMDBAPI().get(f"movie/{tmdb_id}/keywords", timeout=10)
    if response is None:
        return None
    rows = (response.json() or {}).get("keywords") or []
    names = {str(row.get("name") or "").lower().replace(" ", "") for row in rows}
    ids = {row.get("id") for row in rows}
    result = (_DURING[0] in names or _DURING[1] in ids, _AFTER[0] in names or _AFTER[1] in ids)

    g.CACHE.set(cache_key, list(result), expiration=datetime.timedelta(days=_CACHE_DAYS))
    return result
