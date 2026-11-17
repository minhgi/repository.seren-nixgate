"""
Trakt "reset watching" (rewatch) dates, kept for Next Up.

A show on Trakt can be reset: its earlier plays stay in the history (and in sync/watched/shows) but the progress starts again.
Next Up has to continue after the furthest episode watched SINCE the reset, so each show's reset date is kept in a small
file next to the databases (no database layout change). The Trakt watched sync writes it, Next Up reads it. A show that
was never reset is not in the file, and with an empty file Next Up reads exactly as it did before.
"""
import json
import os
import re

from resources.lib.modules.globals import g

_FILE = "traktResetWatching.json"
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")  # g.validate_date format, the one episodes' last_watched_at has


def _path():
    return os.path.join(g.ADDON_USERDATA_PATH, _FILE)


def save(resets):
    """Replaces the stored dates with resets = {trakt_show_id: reset date in g.validate_date format}."""
    clean = {str(int(k)): v for k, v in (resets or {}).items() if isinstance(v, str) and _STAMP_RE.match(v)}
    path = _path()
    try:
        if not clean:
            if os.path.exists(path):
                os.remove(path)
            return
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f)
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError) as e:
        g.log(f"Trakt reset watching dates not saved ({type(e).__name__})", "warning")


def load():
    """{trakt_show_id (int): reset date}; empty when there is none or the file is unreadable."""
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        items = list(data.items())
    except (OSError, ValueError, AttributeError, TypeError):
        return {}
    result = {}
    for key, value in items:
        if isinstance(value, str) and _STAMP_RE.match(value) and str(key).isdigit():
            result[int(key)] = value
    return result


def watched_sql(resets, prefix=""):
    """SQL condition for 'this episode counts as watched': watched, and (for a reset show) watched after the reset.
    An episode without a watch date keeps counting (Trakt can list a completed episode with no date). prefix = table alias + '.'."""
    if not resets:
        return f"{prefix}watched >= 1"
    return f"({prefix}watched >= 1 AND {_since_reset_sql(resets, prefix)})"


def unwatched_sql(resets, prefix=""):
    """SQL condition for 'this episode still has to be watched': not watched, or watched only before the show's reset."""
    if not resets:
        return f"{prefix}watched = 0"
    return f"({prefix}watched = 0 OR ({prefix}watched >= 1 AND NOT {_since_reset_sql(resets, prefix)}))"


def _since_reset_sql(resets, prefix):
    cases = " ".join(f"WHEN {int(show_id)} THEN '{stamp}'" for show_id, stamp in resets.items())
    return (
        f"({prefix}last_watched_at IS NULL OR {prefix}last_watched_at > "
        f"CASE {prefix}trakt_show_id {cases} ELSE '' END)"
    )
