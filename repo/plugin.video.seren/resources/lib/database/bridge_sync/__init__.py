import collections

from resources.lib.database import Database
from resources.lib.modules.globals import g

schema = {
    "snapshot": {
        "columns": collections.OrderedDict(
            [
                ("domain", ["TEXT", "NOT NULL"]),
                ("service", ["TEXT", "NOT NULL"]),
                ("media_type", ["TEXT", "NOT NULL"]),
                ("tmdb_id", ["INTEGER", "NOT NULL"]),
                ("item_key", ["TEXT", "NOT NULL"]),
            ]
        ),
        "table_constraints": ["PRIMARY KEY(domain, service, media_type, item_key)"],
        "indices": [
            ("idx_bridge_snapshot_lookup", ["domain", "service", "media_type"]),
        ],
        "default_seed": [],
    },
}


class BridgeSyncDatabase(Database):
    """Persists each domain+service+media_type's full item-set as of the last
    successful bridgeSync run. bridgeSync.py diffs the freshly-read current set
    against this snapshot to find only what's genuinely new since last run
    (newly_added = current - snapshot), pushes that outward, then overwrites the
    snapshot with the current set. This is deliberately NOT a monotonic
    "ever-seen" record - it must reset when an item disappears from a service
    (e.g. unwatched externally), or a later legitimate re-add would be silently
    blocked. See modules/bridgeSync.py's module docstring for the full rationale
    (advisor-reviewed: this is what avoids the reversal-bug risk of a naive
    full-current-state diff against another service's own state)."""

    def __init__(self):
        super().__init__(g.BRIDGE_SYNC_DB_PATH, schema)

    def get_snapshot(self, domain, service, media_type):
        """Returns {item_key: tmdb_id} for every item this (domain, service,
        media_type) held as of the last successful run."""
        rows = self.fetchall(
            "SELECT item_key, tmdb_id FROM snapshot WHERE domain=? AND service=? AND media_type=?",
            (domain, service, media_type),
        )
        return {r["item_key"]: r["tmdb_id"] for r in rows}

    def replace_snapshot(self, domain, service, media_type, current_items):
        """Overwrites the stored set for this (domain, service, media_type) with
        current_items ({item_key: tmdb_id}) - always the full set, never a partial
        update, so a missing item is correctly treated as "gone" next run."""
        self.execute_sql(
            "DELETE FROM snapshot WHERE domain=? AND service=? AND media_type=?",
            (domain, service, media_type),
        )
        if current_items:
            rows = [
                (domain, service, media_type, tmdb_id, item_key)
                for item_key, tmdb_id in current_items.items()
            ]
            self.execute_sql(
                "INSERT INTO snapshot (domain, service, media_type, tmdb_id, item_key) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
