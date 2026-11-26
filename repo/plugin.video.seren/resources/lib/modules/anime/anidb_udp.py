"""AniDB UDP API client — fansub group data for per-anime source ranking.

GROUPSTATUS command returns all fansub groups that released a given anime,
with completion percentage and file counts. This enables dynamic per-anime
fansub ranking rather than the static hardcoded group lists in source_filter.py.

REQUIREMENTS:
- AniDB username + password configured in settings (anidb.username, anidb.password)
- Falls back completely to hardcoded lists if credentials absent or connection fails
- Rate limit: 1 command per 4 seconds (same as HTTP API — conservative)
- UDP to api.anidb.net:9000

USAGE:
    get_cached_group_data(anidb_id) -> dict|None
    Called by source_filter.get_fansub_group() as first-tier lookup.
"""

import json
import socket
import threading
import time

from resources.lib.modules.globals import g

_UDP_HOST = "api.anidb.net"
_UDP_PORT = 9000
_CLIENT = "serenkodi"  # registered at anidb.net/software/24813 (UDP API, version 1); the old name "otakukodi" is banned by AniDB (504)
_CLIENT_VER = "1"
_PROTO_VER = "3"
_TIMEOUT = 8  # seconds
_RATE_LIMIT = 4.0  # seconds between commands (conservative)
_MAX_RETRIES = 2

# Module-level state (one session per Kodi run)
_session_key = None
_last_command_time = 0
_connected = False

# Background-prefetch bookkeeping: ids being fetched right now by this process.
_state_lock = threading.Lock()
_session_lock = threading.Lock()  # one AniDB login at a time per process, whichever show asked
_inflight = set()

# A failed lookup (bad login, ban, outage, no answer) is remembered in the cache table itself for a few hours, as a
# marker row, so the password is not sent to AniDB again on every play. It is stored, not kept in memory, because
# every plugin run is a fresh Python process (addon.xml has reuselanguageinvoker=false).
_FAIL_COOLDOWN_SECONDS = 6 * 3600
_FAILED_KEY = "_failed"

# The sorter asks for the same show's group data once per source (hundreds of times in a row), so the answer
# (hit or miss) is remembered for a few seconds instead of opening the cache database for every source.
_MEMO_SECONDS = 30
_memo = {}


def get_cached_group_data(anidb_id):
    """Return fansub group data for an anime from SQLite cache.

    Does NOT trigger a UDP fetch — that happens in prefetch_group_data().
    Returns dict of {group_name_lower: {'category': str, 'completion': int}}
    or None if not cached.
    """
    if not anidb_id:
        return None
    now = time.time()
    hit = _memo.get(anidb_id)
    if hit is not None and now - hit[0] < _MEMO_SECONDS:
        return hit[1]
    try:
        from resources.lib.database.animeCache import AnimeCache
        data = AnimeCache().get_fansub_groups(anidb_id)
    except Exception:
        return None
    _memo[anidb_id] = (now, data)
    return data


def prefetch_group_data(anidb_id):
    """Fetch and cache fansub group data for an anime via UDP.

    Called from getSources.py after anime IDs are resolved, before sorting.
    Silent no-op if credentials not configured or connection fails.
    Returns True if data was fetched and cached, False otherwise.
    """
    if not anidb_id:
        return False

    # Skip if already cached
    if get_cached_group_data(anidb_id) is not None:
        return True

    # Skip if credentials not configured
    username = g.get_setting("anidb.username", "").strip()
    password = g.get_setting("anidb.password", "").strip()
    if not username or not password:
        return False

    try:
        client = _AniDBUDPClient()
        groups = client.get_fansub_groups(anidb_id, username, password)
        if groups is not None:
            _store_group_data(anidb_id, groups)
            return True
    except Exception as e:
        g.log(f"AniDB UDP prefetch failed (anidb_id={anidb_id}): {e}", "debug")
    return False


def start_group_prefetch(anidb_id):
    """Start prefetch_group_data() in a background thread so playback never waits on AniDB.

    Called from getSources once the show's AniDB id is known. A first lookup needs three UDP commands
    4 s apart (about 8 s), which is why it runs beside the scrape and the result is simply used if it is
    in the cache by the time the sources are sorted; otherwise the next play of the show has it.

    Returns the started thread, or None when nothing was started (no/invalid id, no AniDB login, already
    cached, already running, or it failed within the last few hours).
    """
    try:
        anidb_id = int(anidb_id)
    except (TypeError, ValueError):
        return None
    if not g.get_setting("anidb.username", "").strip() or not g.get_setting("anidb.password", "").strip():
        return None
    if get_cached_group_data(anidb_id) is not None:
        return None
    with _state_lock:
        if anidb_id in _inflight:
            return None
        _inflight.add(anidb_id)

    def _run():
        try:
            with _session_lock:
                fetched = prefetch_group_data(anidb_id)
            if not fetched:
                _store_failure(anidb_id)
        except Exception as e:
            _store_failure(anidb_id)
            g.log(f"AniDB UDP background prefetch failed (anidb_id={anidb_id}): {e}", "debug")
        finally:
            with _state_lock:
                _inflight.discard(anidb_id)

    thread = threading.Thread(target=_run, name=f"anidb-groups-{anidb_id}", daemon=True)
    thread.start()
    return thread


def _store_failure(anidb_id):
    """Remember a failed lookup for _FAIL_COOLDOWN_SECONDS (a marker row in the fansub group cache)."""
    try:
        from resources.lib.database.animeCache import AnimeCache
        AnimeCache().execute_sql(
            "INSERT OR REPLACE INTO anime_fansub_groups (anidb_id, groups_json, expires) VALUES (?, ?, ?)",
            (int(anidb_id), json.dumps({_FAILED_KEY: True}), int(time.time()) + _FAIL_COOLDOWN_SECONDS),
        )
        _memo.pop(anidb_id, None)
    except Exception:
        pass


def _to_int(parts, index):
    try:
        return int(parts[index])
    except (ValueError, IndexError):
        return None


def _store_group_data(anidb_id, groups):
    """Store fetched group data in SQLite cache."""
    try:
        from resources.lib.database.animeCache import AnimeCache
        AnimeCache().set_fansub_groups(anidb_id, groups)
        _memo.pop(anidb_id, None)
    except Exception:
        pass


# ── UDP Client ────────────────────────────────────────────────────────────────

class _AniDBUDPClient:
    """Minimal AniDB UDP API client for GROUPSTATUS queries."""

    def __init__(self):
        self._sock = None
        self._session = None

    def get_fansub_groups(self, anidb_id, username, password):
        """Authenticate and fetch GROUPSTATUS for an anime.

        Returns:
            dict of {group_name_lower: {'category': str, 'completion': int}}
            or None on failure.
        """
        for attempt in range(_MAX_RETRIES):
            try:
                self._connect()
                if not self._auth(username, password):
                    return None
                result = self._group_status(anidb_id)
                self._logout()
                return result
            except Exception as e:
                g.log(f"AniDB UDP attempt {attempt + 1} failed: {e}", "debug")
                self._reset()
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(2)
        return None

    def _connect(self):
        """Create UDP socket."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(_TIMEOUT)

    def _send(self, command):
        """Send a UDP command and return the response string."""
        global _last_command_time
        elapsed = time.time() - _last_command_time
        if elapsed < _RATE_LIMIT:
            time.sleep(_RATE_LIMIT - elapsed)

        self._sock.sendto(command.encode("utf-8"), (_UDP_HOST, _UDP_PORT))
        _last_command_time = time.time()

        response, _ = self._sock.recvfrom(4096)
        return response.decode("utf-8", errors="replace").strip()

    def _auth(self, username, password):
        """Authenticate with AniDB. Returns True on success."""
        cmd = (
            f"AUTH user={username}&pass={password}"
            f"&protover={_PROTO_VER}&client={_CLIENT}"
            f"&clientver={_CLIENT_VER}&enc=UTF8"
        )
        resp = self._send(cmd)
        # Expected: "200 {key} LOGIN ACCEPTED" or "201 {key} LOGIN ACCEPTED - NEW VERSION"
        if resp.startswith("200 ") or resp.startswith("201 "):
            parts = resp.split(" ", 2)
            if len(parts) >= 2:
                self._session = parts[1]
                g.log("AniDB UDP: authenticated", "debug")
                return True
        g.log(f"AniDB UDP auth failed: {resp[:50]}", "debug")
        return False

    def _group_status(self, anidb_id):
        """Fetch GROUPSTATUS for an anime. Returns group dict or None."""
        if not self._session:
            return None
        resp = self._send(f"GROUPSTATUS aid={anidb_id}&s={self._session}")
        return self._parse_group_status(resp)

    def _parse_group_status(self, response):
        """Parse GROUPSTATUS multi-line response into group dict.

        Response format (checked against other AniDB UDP clients):
        225 GROUPSTATUS
        {gid}|{group_name}|{completion_state}|{last_episode}|{rating}|{votes}|{episode_range}

        completion_state: 1=ongoing, 2=stalled, 3=complete, 4=dropped, 5=finished, 6=specials only
        325 NO SUCH GROUPS FOUND and 330 NO SUCH ANIME are final answers and give {} (cached as "no groups").

        Returns {group_name_lower: {'category': str, 'completion': int}}
        or None when the reply is neither (busy, banned, error): the caller then stores a short failure
        marker instead, so a transient failure is not stored as "no groups" for 30 days.
        """
        if response.startswith(("325", "330")):
            return {}
        if not response.startswith("225"):
            g.log(f"AniDB UDP GROUPSTATUS unexpected: {response[:50]}", "warning")
            return None

        lines = response.split("\n")
        # One raw data line, logged so the field layout can be checked against a real reply.
        sample = next((ln.strip() for ln in lines[1:] if ln.strip()), "")
        if sample:
            g.log(f"AniDB UDP: GROUPSTATUS sample line: {sample[:120]}", "info")
        groups = {}
        for line in lines[1:]:  # Skip status line
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            try:
                group_name = parts[1].strip()
                completion_state = int(parts[2])
            except (ValueError, IndexError):
                continue
            if not group_name:
                continue

            # Map completion state to a category hint
            # Complete/finished groups are preferred quality sources
            category = "complete" if completion_state in (3, 5) else "active"

            groups[group_name.lower()] = {
                "category": category,
                "completion": completion_state,
                "name": group_name,
                "last_episode": _to_int(parts, 3),
                "rating": _to_int(parts, 4),  # AniDB group rating for this anime, 0-1000
                "votes": _to_int(parts, 5),
                "episodes": parts[6].strip() if len(parts) > 6 else "",
            }

        g.log(f"AniDB UDP: parsed {len(groups)} fansub groups", "debug")
        return groups

    def _logout(self):
        """Send LOGOUT command."""
        if self._session and self._sock:
            try:
                self._send(f"LOGOUT s={self._session}")
            except Exception:
                pass
        self._reset()

    def _reset(self):
        """Close socket and clear session."""
        self._session = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
