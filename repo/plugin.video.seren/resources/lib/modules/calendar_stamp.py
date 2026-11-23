"""Tells the calendar windows that a background sync brought in new watched data while a window was open.

The Show Calendar and the TV Shows Calendar keep the weeks / rows they loaded for as long as the window is open (loading them is a
database pass per week, the watchlist layer costs live lookups). Every provider's sync writes the activity timestamp of what it synced AFTER
the data itself (trakt_sync/activities.py, mdblist_sync/activities.py, simkl_sync/activities.py), so a change of these values means rows were
changed behind the window's back, and a window that read them BEFORE it loaded is never left with half-synced rows under a newer stamp.

Not the databases' file times: building a calendar page inserts the shows / episodes it shows into traktSync.db ("Inserting Shows into sync
database"), which would change such a stamp on every load. Measured: opening both calendars, paging three days and the prefetch changed none
of the values below, and neither did five Kodi restarts with their start-up syncs.
"""

from resources.lib.modules.globals import g

# only the columns of what a calendar row shows (watched ticks, hidden shows), never SELECT * (that would include last_activities_call,
# which moves on every poll)
_TRAKT = "SELECT all_activities, shows_watched, hidden_sync FROM activities WHERE sync_id=1"
_MDBLIST = "SELECT watched_at, episode_watched_at FROM activities WHERE sync_id=1"
_SIMKL = "SELECT tv_shows_completed_at, tv_shows_watching_at FROM activities WHERE sync_id=1"
# 3.9.180: watched_at only (written after the marks on both sync paths, moves only when WeTrakr's watched stamp moved); journal_at moves on every sync
_WETRAKR = "SELECT watched_at FROM activities WHERE sync_id=1"


def _values(row):
    return tuple(row[key] for key in row.keys()) if row else None


def sync_stamp():
    """The synced-activity values of the configured providers, as one comparable value; None when they cannot be read (then it never
    changes: a failing read must not make a window reload over and over). Goes through the Database classes (SQLite or MySQL) and only for
    providers that are set up: constructing the class of an unconfigured provider would create its database."""
    try:
        from resources.lib.modules.syncGateway import configured_providers

        providers = configured_providers()
        stamp = []
        if "trakt" in providers:
            from resources.lib.database.trakt_sync import TraktSyncDatabase

            stamp.append(_values(TraktSyncDatabase().fetchone(_TRAKT)))
        if "mdblist" in providers:
            from resources.lib.database.mdblist_sync import MDBListSyncDatabase

            stamp.append(_values(MDBListSyncDatabase().fetchone(_MDBLIST)))
        if "simkl" in providers:
            from resources.lib.database.simkl_sync import SimklSyncDatabase

            stamp.append(_values(SimklSyncDatabase().fetchone(_SIMKL)))
        if g.wetrakr_active():  # not in configured_providers(); the database only for a signed-in WeTrakr
            from resources.lib.database.wetrakr_sync import WeTrakrSyncDatabase

            stamp.append(_values(WeTrakrSyncDatabase().fetchone(_WETRAKR)))
        return tuple(stamp)
    except Exception as exc:  # pylint: disable=broad-except
        g.log(f"calendar_stamp: the sync activities could not be read: {exc}", "debug")
        return None
