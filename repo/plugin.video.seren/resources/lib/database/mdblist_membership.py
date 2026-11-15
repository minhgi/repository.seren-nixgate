"""MDBList watchlist / collection membership, kept in its OWN small file (mdblistMembership.db, Seren 3.9.134) so the large
mdblistSync.db keeps its schema and is not rebuilt. Used only to show the single applicable context-menu option (Add or Remove)
without a live request, per the single-option gating rule. Filled by MDBListSyncDatabase.refresh_membership() on every periodic
sync and written through when the context menu adds or removes an item. is_member() answers None until a kind was synced once."""

import collections
import datetime
import os

from resources.lib.common import tools
from resources.lib.database import Database
from resources.lib.modules.globals import g

schema = {
    "membership": {
        "columns": collections.OrderedDict(
            [
                ("kind", ["TEXT", "NOT NULL"]),
                ("media_type", ["TEXT", "NOT NULL"]),
                ("tmdb_id", ["INTEGER", "NOT NULL"]),
            ]
        ),
        "table_constraints": ["PRIMARY KEY(kind, media_type, tmdb_id)"],
        "indices": [],
        "default_seed": [],
    },
    "synced": {
        "columns": collections.OrderedDict([("kind", ["TEXT", "PRIMARY KEY", "NOT NULL"]), ("synced_at", ["TEXT", "NULL"])]),
        "table_constraints": [],
        "indices": [],
        "default_seed": [],
    },
}


class MDBListMembershipDatabase(Database):
    def __init__(self):
        super().__init__(tools.translate_path(os.path.join(g.ADDON_USERDATA_PATH, "mdblistMembership.db")), schema)

    def is_member(self, kind, media_type, tmdb_id):
        """True / False once this kind has been synced, None before (the menu then offers nothing rather than guessing)."""
        if not tmdb_id or not self.fetchone("SELECT 1 FROM synced WHERE kind=?", (kind,)):
            return None
        return bool(
            self.fetchone(
                "SELECT 1 FROM membership WHERE kind=? AND media_type=? AND tmdb_id=?", (kind, media_type, tmdb_id)
            )
        )

    def member_ids(self, kind, media_type):
        """tmdb ids of one kind and media type, or None before that kind was synced once (3.9.136)."""
        if not self.fetchone("SELECT 1 FROM synced WHERE kind=?", (kind,)):
            return None
        rows = self.fetchall("SELECT tmdb_id FROM membership WHERE kind=? AND media_type=?", (kind, media_type))
        return [row["tmdb_id"] for row in rows]

    def replace_kind(self, kind, items):
        """items: iterable of (media_type, tmdb_id) - the complete current membership of one kind."""
        rows = [(kind, media_type, tmdb_id) for media_type, tmdb_id in set(items)]
        self.execute_sql("DELETE FROM membership WHERE kind=?", (kind,))
        if rows:
            self.execute_sql("INSERT OR IGNORE INTO membership (kind, media_type, tmdb_id) VALUES (?, ?, ?)", rows)
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        self.execute_sql("REPLACE INTO synced (kind, synced_at) VALUES (?, ?)", (kind, now))

    def add(self, kind, media_type, tmdb_id):
        self.execute_sql(
            "INSERT OR IGNORE INTO membership (kind, media_type, tmdb_id) VALUES (?, ?, ?)", (kind, media_type, tmdb_id)
        )

    def remove(self, kind, media_type, tmdb_id):
        self.execute_sql(
            "DELETE FROM membership WHERE kind=? AND media_type=? AND tmdb_id=?", (kind, media_type, tmdb_id)
        )
