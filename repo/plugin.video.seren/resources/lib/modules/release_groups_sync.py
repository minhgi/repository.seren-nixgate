"""Weekly update of the Release Group lists, built on this device from the upstream projects (no hosted file, nothing to rebuild).

Sources (all MIT, see resources/data/release_groups.NOTICE.txt):

    TRaSH Guides            docs/json/<radarr|sonarr>/cf/*.json    the tier and low-quality lists, read file by file
    Dictionarry database    ops/*.sql                              community tiers and bans, a numbered log of SQL changes
    Dumpstarr database      ops/*.sql                              community tiers, baseline groups and bans, same format

The two databases are replayed into an in-memory SQLite database the same way Profilarr does it (schema first, then every ops file in
number order, foreign keys on). The SQL is untrusted text from the internet, so it only runs behind a guard: an authorizer that
allows plain table work and refuses ATTACH, PRAGMA, virtual tables and file functions, a time cap, size caps on every download and
archive member, and nothing is ever written to disk except the finished list.

The result is release_groups_live.json in the add-on data folder, in the same layout as the bundled TRaSH snapshot (which stays
the fallback). Where TRaSH and a community list disagree about a group, TRaSH wins: a group TRaSH names is removed from the
community lists. Community "good" groups form one tier below every TRaSH tier, community bans score below unknown groups but above
TRaSH's own low-quality list. A rejected or failed update keeps the previous list and is retried after six hours.

Runs from the service's housekeeping cycle (run_if_due, at most once a week, only with the setting turned on - it is off by default - and
only while the Release Group sort level is in use) or from Settings (update_now_action, any time). Never from a scrape, never needed by the sorter.
"""
import datetime
import io
import json
import os
import re
import sqlite3
import time
import zipfile

import requests

from resources.lib.modules import release_groups
from resources.lib.modules.globals import g

BUILDER_VERSION = 2  # raise when the merge rules change: a list built by an older builder is rebuilt at the next check
SYNC_INTERVAL = 7 * 24 * 3600
FAIL_COOLDOWN = 6 * 3600
STATE_FILE = "release_groups_sync.json"
LOCK_FILE = "release_groups_sync.lock"
LOCK_STALE_SECONDS = 15 * 60
TOTAL_BUDGET_SECONDS = 180  # a normal update takes about 15 s; the manual button shows a busy dialog that cannot be cancelled
REPLAY_BUDGET_SECONDS = 90
MAX_OP_ERRORS = 3  # Profilarr stops on the first one; upstream replays clean (0 errors when this was written)

SCORE_COMMUNITY_GOOD = 1500  # below the lowest TRaSH movie / TV tier (1600)
SCORE_COMMUNITY_ANIME = 50  # below the lowest TRaSH anime tier (100)
SCORE_COMMUNITY_BAD = -5000  # TRaSH's own low-quality list stays at -10000

TRASH = ("TRaSH-Guides/Guides", "master")
DICTIONARRY = ("Dictionarry-Hub/database", "v2")
DUMPSTARR = ("Dumpstarr/Database", "stable")
SCHEMA_REPO = "Dictionarry-Hub/schema"
SUPPORTED_SCHEMAS = ("1.1.0",)  # the schema version both databases declare in pcd.json; anything else is rejected

MAX_ARCHIVE_BYTES = 12 * 1024 * 1024
MAX_MEMBERS = 3000
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_TOTAL_SQL_BYTES = 24 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
_HEADERS = {"User-Agent": "Seren"}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_OPS_RE = re.compile(r"^[^/]+/ops/(\d+)\.[^/]+\.sql$")
_PCD_RE = re.compile(r"^[^/]+/pcd\.json$")

# what is read from each community database - explicit names, so a renamed list upstream is noticed (the guard) and never
# silently moves groups between good and bad
_TIER_NAME_RE = re.compile(r"\bTier \d+\b", re.IGNORECASE)
_NOT_GENERAL_GOOD_RE = re.compile(r"anime|trash|sidca|banned|\blq\b", re.IGNORECASE)  # SIDCA = "typically banned, allowed for LQ profiles"
_DUMP_GOOD_EXTRA = ("Baseline Groups", "WEB Scene")
_BAD_LISTS = (
    "Banned Groups", "Banned Groups (Efficient)", "Banned Groups (Regular)", "Dumpstarr Banned Groups", "Dumpstarr LQ Groups",
    "Dumpstarr LQ Groups (Automation)", "Dumpstarr LQ RG (Automation)", "LQ", "TRaSH Banned Groups",
)
# anime: only Dumpstarr has current lists (Dictionarry deleted its anime tiers upstream, ops/129); they are release-title patterns
_ANIME_DUMPSTARR_RE = re.compile(r"^(?:Anime (?:BD|WEB) Tier \d+|Anime Baseline Groups)$")
_ANIME_JUNK = frozenset(("remux", "web", "webrip", "webdl", "bluray", "bdrip", "raw", "raws", "dub", "sub", "subs"))

# TRaSH: the same lists tools/build_release_groups.py used to snapshot
_FAMILIES = {
    "movie": ("radarr", ["remux-tier-%02d", "uhd-bluray-tier-%02d", "hd-bluray-tier-%02d", "web-tier-%02d"], ["lq"]),
    "episode": ("sonarr", ["remux-tier-%02d", "uhd-bluray-tier-%02d", "hd-bluray-tier-%02d", "web-tier-%02d"], ["lq"]),
    "anime_movie": ("radarr", ["anime-bd-tier-%02d", "anime-web-tier-%02d"], ["anime-lq-groups"]),
    "anime_episode": ("sonarr", ["anime-bd-tier-%02d", "anime-web-tier-%02d"], ["anime-lq-groups"]),
}
_SPEC_TOKENS = {
    ("Remux", False, True): ("req", "remux"),
    ("Not REMUX", True, True): ("not", "remux"),
    ("Not WEBDL", True, True): ("not", "web"),
    ("Not WEBRIP", True, True): ("not", "web"),
    ("2160p", False, True): ("req", "2160p"),
    ("Not 2160p", True, True): ("not", "2160p"),
    ("BLURAY", False, True): ("req", "bluray"),
    ("WEBDL", False, False): ("any", "web"),
    ("WEBRIP", False, False): ("any", "web"),
    ("WEB", False, False): ("any", "web"),
    ("Bluray", False, False): ("any", "bluray"),
    ("Bluray Remux", False, False): ("any", "remux"),
    ("DVD", False, False): ("any", "dvd"),
}
_PATTERN_IMPLS = {"ReleaseGroupSpecification": "group", "ReleaseTitleSpecification": "title"}


class SyncError(Exception):
    """An update that must not be used; the previous list stays."""


class SyncAborted(Exception):
    """Kodi is shutting down; not a failure, nothing is remembered."""


# --------------------------------------------------------------------------------------------------------- state and lock

def _path(name):
    return os.path.join(g.ADDON_USERDATA_PATH, name)


def read_state():
    try:
        with open(_path(STATE_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _save_state(state):
    try:
        _write_atomic(_path(STATE_FILE), json.dumps(state, separators=(",", ":")))
    except OSError as e:
        g.log(f"Release group update: state not saved: {e}", "warning")


def _acquire_lock():
    """One update at a time across plugin interpreters; a lock older than LOCK_STALE_SECONDS is taken over."""
    path = _path(LOCK_FILE)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(time.time()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) < LOCK_STALE_SECONDS:
                    return False
                os.remove(path)
            except OSError:
                return False
        except OSError:
            return False
    return False


def _release_lock():
    try:
        os.remove(_path(LOCK_FILE))
    except OSError:
        pass


def enabled():
    return g.get_bool_setting("releasegroups.autoupdate", False)  # off until the user turns it on (or presses Update now)


# --------------------------------------------------------------------------------------------------------- network

def _check_abort(deadline):
    if g.abort_requested():
        raise SyncAborted()
    if time.monotonic() > deadline:
        raise SyncError("the update took too long")


def _http_get(url, max_bytes, headers=None, allow_404=False):
    """The body of a URL as bytes. None for a 404 when allowed. Anything else unusual raises SyncError."""
    try:
        response = requests.get(url, headers={**_HEADERS, **(headers or {})}, timeout=(8, 30), stream=True)
    except requests.RequestException as e:
        raise SyncError(f"could not reach {url.split('/')[2]}: {e}") from e
    try:
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code != 200:
            raise SyncError(f"{url.split('/')[2]} answered {response.status_code}")
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > max_bytes:
                raise SyncError(f"download from {url.split('/')[2]} is larger than {max_bytes} bytes")
        return bytes(body)
    except requests.RequestException as e:
        raise SyncError(f"download from {url.split('/')[2]} failed: {e}") from e
    finally:
        response.close()


def _head_sha(repo_branch):
    repo, branch = repo_branch
    body = _http_get(f"https://api.github.com/repos/{repo}/commits/{branch}", 1024, {"Accept": "application/vnd.github.sha"})
    sha = (body or b"").decode("ascii", "ignore").strip()
    if not _SHA_RE.match(sha):
        raise SyncError(f"no commit id for {repo}")
    return sha


def _archive(repo, ref):
    return _http_get(f"https://codeload.github.com/{repo}/zip/{ref}", MAX_ARCHIVE_BYTES)


def _trash_json(sha, path):
    body = _http_get(f"https://raw.githubusercontent.com/{TRASH[0]}/{sha}/{path}", MAX_JSON_BYTES, allow_404=True)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as e:
        raise SyncError(f"TRaSH file {path} is not valid JSON") from e


# --------------------------------------------------------------------------------------------------------- archives

def read_ops_archive(data):
    """(pcd.json as a dict or None, [(number, file name, sql text)] sorted by number) from a GitHub zip, in memory, with caps."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise SyncError("an archive is not a valid zip") from e
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise SyncError("an archive has too many files")
    pcd = None
    ops = []
    total = 0
    for info in infos:
        name = info.filename
        if ".." in name.split("/") or name.startswith("/") or "\\" in name:
            continue  # nothing is ever extracted, but a path like that is not a file this reads
        is_ops = _OPS_RE.match(name)
        if not is_ops and not _PCD_RE.match(name):
            continue
        if info.file_size > MAX_MEMBER_BYTES:
            raise SyncError(f"{name} is too large")
        with zf.open(info) as member:
            raw = member.read(MAX_MEMBER_BYTES + 1)  # bounded however large the header says the member is
        if len(raw) > MAX_MEMBER_BYTES:
            raise SyncError(f"{name} is too large")
        total += len(raw)  # what was really read
        if total > MAX_TOTAL_SQL_BYTES:
            raise SyncError("an archive holds too much data")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise SyncError(f"{name} is not UTF-8") from e
        if is_ops:
            ops.append((int(is_ops.group(1)), os.path.basename(name), text))
        else:
            try:
                pcd = json.loads(text)
            except ValueError as e:
                raise SyncError("pcd.json is not valid JSON") from e
    ops.sort(key=lambda item: item[0])
    return pcd, ops


def _schema_version(pcd):
    deps = (pcd or {}).get("dependencies") or {}
    for url, version in deps.items():
        if str(url).rstrip("/").lower().endswith("dictionarry-hub/schema"):
            return str(version)
    return None


# --------------------------------------------------------------------------------------------------------- sandboxed replay

# SQLite authorizer action codes. The data ops (Dictionarry / Dumpstarr) only ever INSERT, UPDATE and DELETE rows, so once the schema is
# loaded nothing else is allowed. The schema ops need tables, an index and one ALTER (no triggers, views or drops: the pinned schema
# version has none, and a new schema version is rejected by SUPPORTED_SCHEMAS anyway).
_DML_ACTIONS = frozenset((
    9,  # DELETE
    18,  # INSERT
    20,  # READ
    21,  # SELECT
    22,  # TRANSACTION
    23,  # UPDATE
    32,  # SAVEPOINT
    33,  # RECURSIVE (a recursive CTE; the progress handler bounds it)
))
_DDL_ACTIONS = frozenset((
    1,  # CREATE INDEX
    2,  # CREATE TABLE
    26,  # ALTER TABLE
    27,  # REINDEX (SQLite asks for it internally while CREATE INDEX builds the new index)
))  # always refused: PRAGMA (19), ATTACH (24), DETACH (25), ANALYZE (28), virtual tables (29, 30), triggers, views, DROP
_SQLITE_FUNCTION = 31
_DENIED_FUNCTIONS = frozenset(("load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"))


def _make_authorizer(state):
    def _authorizer(action, arg1, arg2, dbname, source):
        if action == _SQLITE_FUNCTION:
            return 1 if str(arg2 or "").lower() in _DENIED_FUNCTIONS else 0
        if action in _DML_ACTIONS or (state["ddl"] and action in _DDL_ACTIONS):
            return 0
        return 1  # SQLITE_DENY
    return _authorizer


def _open_sandbox(deadline):
    con = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    con.execute("PRAGMA foreign_keys=ON")  # before the authorizer: Profilarr runs with foreign keys on, renames cascade through them

    def lookup(table):
        def _fn(name):
            row = con.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise ValueError(f"{table} not found: {name}")
            return row[0]
        return _fn

    for fn_name, table in (("qp", "quality_profiles"), ("cf", "custom_formats"), ("dp", "delay_profiles"), ("tag", "tags")):
        con.create_function(fn_name, 1, lookup(table))
    tripped = {"timeout": False, "ddl": True}  # ddl: True while the trusted schema loads, False for the data ops
    con.set_authorizer(_make_authorizer(tripped))
    for limit, value in ((0, 8000000), (1, 2200000)):  # SQLITE_LIMIT_LENGTH (no string or blob over 8 MB), SQLITE_LIMIT_SQL_LENGTH
        try:
            con.setlimit(limit, value)  # Python 3.11+; older Pythons rely on the size caps and the time cap
        except (AttributeError, sqlite3.Error, OverflowError):
            break

    def _progress():
        if g.abort_requested() or time.monotonic() > deadline:
            tripped["timeout"] = True
            return 1  # non-zero interrupts the running statement
        return 0

    con.set_progress_handler(_progress, 20000)
    return con, tripped


def _split_statements(text):
    statements = []
    buffer = ""
    for line in text.splitlines(True):
        if not buffer and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if buffer.strip():
        statements.append(buffer)
    return statements


def replay(con, tripped, ops):
    """Run ops files in order, each inside a savepoint (a file that fails is rolled back whole). Returns the number of failed files."""
    errors = 0
    for _number, name, text in ops:
        if tripped["timeout"]:
            raise SyncError("the database replay took too long")
        con.execute("SAVEPOINT op")
        try:
            for statement in _split_statements(text):
                con.execute(statement)
        except sqlite3.Error as e:
            errors += 1
            con.execute("ROLLBACK TO op")
            g.log(f"Release group update: {name} failed and was skipped: {e}", "debug")
        con.execute("RELEASE op")
    if tripped["timeout"]:
        raise SyncError("the database replay took too long")
    return errors


def build_database(schema_ops, db_ops, deadline):
    con, tripped = _open_sandbox(deadline)
    try:
        errors = replay(con, tripped, schema_ops)
        tripped["ddl"] = False  # from here on only rows may change
        errors += replay(con, tripped, db_ops)
    except Exception:
        con.close()
        raise
    return con, errors


# --------------------------------------------------------------------------------------------------------- reading names out of the databases

def _norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


_LOOKAROUND_RE = re.compile(r"\(\?<?[=!][^)]*\)")
_FLAGS_RE = re.compile(r"\(\?[a-z]+\)")
_LITERAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def literal_names(pattern):
    """Plain group names out of a .NET style pattern: the longest (a|b|c) alternation, or a single literal. Returns (names, skipped).

    Python's re cannot compile these patterns (variable-width lookbehind), so names are lifted out and rebuilt as ^(a|b|c)$ lists."""
    stack = []
    spans = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            spans.append((stack.pop(), i))
        i += 1
    bodies = []
    for start, end in spans:
        body = pattern[start + 1:end]
        if body.startswith("?:"):
            bodies.append(body[2:])
        elif not body.startswith("?"):
            bodies.append(body)
    if not bodies:  # no group: strip lookarounds, flags and \b \s, keep what is left when it is one literal
        body = _FLAGS_RE.sub("", _LOOKAROUND_RE.sub("", pattern))
        bodies = [body.replace("\\s", "").replace("\\b", "").replace("^", "").replace("$", "")]
    best = ([], 0)
    for body in bodies:  # the group that holds the names is the one with the most plain literals (not a lookaround or a prefix group)
        names = []
        skipped = 0
        for part in re.split(r"(?<!\\)\|", body):
            candidate = re.sub(r"\\(.)", r"\1", part).strip()
            if _LITERAL_RE.match(candidate):
                names.append(candidate)
            elif candidate:
                skipped += 1
        if len(names) >= len(best[0]):
            best = (names, skipped)
    return best


def _lists(con, wanted, any_type=False):
    """{custom format name: [names]} for the custom formats `wanted(name)` accepts: their release-group patterns, not negated."""
    type_clause = "" if any_type else "AND cc.type = 'release_group'"
    query = f"""SELECT cc.custom_format_name, re.pattern FROM custom_format_conditions cc
                JOIN condition_patterns cp ON cp.custom_format_name = cc.custom_format_name AND cp.condition_name = cc.name
                JOIN regular_expressions re ON re.name = cp.regular_expression_name
                WHERE cc.negate = 0 {type_clause}"""
    out = {}
    skipped = 0
    for cf_name, pattern in con.execute(query):
        if not wanted(cf_name):
            continue
        names, s = literal_names(pattern)
        skipped += s
        out.setdefault(cf_name, []).extend(names)
    return out, skipped


def read_community(dictionarry_con, dumpstarr_con):
    """Group names from the two databases: {'good': {...}, 'bad': {...}, 'anime_good': {...}, 'stats': {...}}.

    Raises SyncError when an expected list family is missing (an upstream rename), so the update is rejected instead of
    quietly dropping or mixing lists."""
    def is_good_dictionarry(name):
        return bool(_TIER_NAME_RE.search(name)) and not _NOT_GENERAL_GOOD_RE.search(name)

    def is_good_dumpstarr(name):
        return (bool(_TIER_NAME_RE.search(name)) and not _NOT_GENERAL_GOOD_RE.search(name)) or name in _DUMP_GOOD_EXTRA

    d_good, s1 = _lists(dictionarry_con, is_good_dictionarry)
    u_good, s2 = _lists(dumpstarr_con, is_good_dumpstarr)
    d_bad, s3 = _lists(dictionarry_con, lambda n: n in _BAD_LISTS)
    u_bad, s4 = _lists(dumpstarr_con, lambda n: n in _BAD_LISTS)
    u_anime, s5 = _lists(dumpstarr_con, lambda n: bool(_ANIME_DUMPSTARR_RE.match(n)), any_type=True)
    s6 = 0
    if len(d_good) < 10 or len(u_good) < 5 or "Baseline Groups" not in u_good:
        raise SyncError(f"the community tier lists were not found as expected ({len(d_good)} + {len(u_good)})")
    if not (d_bad or u_bad):
        raise SyncError("the community ban lists were not found")
    if len(u_anime) < 5 or "Anime Baseline Groups" not in u_anime:
        raise SyncError(f"the community anime lists were not found as expected ({len(u_anime)})")

    def collect(*groups):
        names = {}
        for group in groups:
            for values in group.values():
                for name in values:
                    names.setdefault(_norm(name), name)
        return names

    good = collect(d_good, u_good)
    bad = collect(d_bad, u_bad)
    anime = {k: v for k, v in collect(u_anime).items() if len(k) >= 3 and k not in _ANIME_JUNK}
    for key in bad:  # a ban from either community list outweighs a tier listing in the other
        good.pop(key, None)
        anime.pop(key, None)
    return {
        "good": good, "bad": bad, "anime_good": anime,
        "stats": {"skipped_patterns": s1 + s2 + s3 + s4 + s5 + s6, "good_lists": len(d_good) + len(u_good),
                  "bad_lists": len(d_bad) + len(u_bad), "anime_lists": len(u_anime)},
    }


# --------------------------------------------------------------------------------------------------------- TRaSH

def _parse_cf(cf, label, app=None):
    scores = cf.get("trash_scores") or {}
    score = scores.get("default")
    if score is None:
        return None  # not a scoring format (e.g. anime-dual-audio)
    tier = {"name": cf["name"], "score": score, "mode": None, "req": [], "not": [], "any": [], "patterns": [],
            "anime_score": scores.get(f"anime-{app}") if app else None}
    optional_impls = set()
    for spec in cf["specifications"]:
        impl = spec["implementation"]
        if impl in _PATTERN_IMPLS:
            mode = _PATTERN_IMPLS[impl]
            if tier["mode"] not in (None, mode):
                raise SyncError(f"TRaSH {label} mixes group and title specs")
            tier["mode"] = mode
            tier["patterns"].append(spec["fields"]["value"])
            continue
        key = (spec["name"], bool(spec["negate"]), bool(spec["required"]))
        if key not in _SPEC_TOKENS:  # a new kind of TRaSH requirement is never silently ignored
            raise SyncError(f"TRaSH {label} has an unknown requirement {impl} {key}")
        which, token = _SPEC_TOKENS[key]
        if which == "any":
            optional_impls.add(impl)
        if token not in tier[which]:
            tier[which].append(token)
    if len(optional_impls) > 1:
        raise SyncError(f"TRaSH {label} has optional specs of several types")
    if not tier["patterns"]:
        return None
    return tier


def _usable_patterns(tier):
    """Keep only patterns Python's re accepts (TRaSH writes .NET regex)."""
    good = []
    for pattern in tier["patterns"]:
        try:
            re.compile(pattern, re.IGNORECASE)
            good.append(pattern)
        except re.error:
            pass
    tier["patterns"] = good
    try:
        re.compile("|".join(f"(?:{p})" for p in good), re.IGNORECASE)
        tier["combinable"] = True
    except re.error:
        tier["combinable"] = False
    return tier


def build_trash(sha, deadline):
    out = {}
    for section, (app, families, singles) in _FAMILIES.items():
        tiers = []
        lq = []
        for family in families:
            for number in range(1, 20):
                _check_abort(deadline)
                label = f"{app}/{family % number}"
                cf = _trash_json(sha, f"docs/json/{app}/cf/{family % number}.json")
                if cf is None:
                    break
                tier = _parse_cf(cf, label, app)
                if tier:
                    tiers.append(_usable_patterns(tier))
        for single in singles:
            _check_abort(deadline)
            cf = _trash_json(sha, f"docs/json/{app}/cf/{single}.json")
            if cf is None:
                continue
            tier = _parse_cf({**cf, "trash_scores": {"default": -10000}}, f"{app}/{single}", None)
            if tier:
                _usable_patterns(tier)
                lq.append({"mode": tier["mode"], "patterns": tier["patterns"], "combinable": tier["combinable"]})
        out[section] = {"tiers": tiers, "lq": lq}
    # TRaSH scores some general tiers again in its anime profile: those groups are ranked in the anime section too, at that score
    for general, anime in (("movie", "anime_movie"), ("episode", "anime_episode")):
        for tier in out[general]["tiers"]:
            if tier.get("anime_score") is not None:
                copy = {k: v for k, v in tier.items() if k != "anime_score"}
                copy["name"] = tier["name"] + " (anime profile)"
                copy["score"] = tier["anime_score"]
                out[anime]["tiers"].append(copy)
    for section in out.values():
        for tier in section["tiers"]:
            tier.pop("anime_score", None)
        section["tiers"].sort(key=lambda t: -t["score"])
    return out


_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.]{1,}")
_NAME_LIST_RE = re.compile(r"^\^\((.+)\)\$$")


def trash_names(sections):
    """Normalized names TRaSH ranks: {'general': {...}, 'anime': {...}} (anime patterns are title patterns, so tokens are read)."""
    general = set()
    for section in ("movie", "episode"):
        data = sections.get(section) or {}
        for item in list(data.get("tiers", ())) + list(data.get("lq", ())):
            if item.get("mode") != "group":
                continue
            for pattern in item.get("patterns", ()):
                match = _NAME_LIST_RE.match(pattern)
                if match:
                    general.update(_norm(re.sub(r"\\(.)", r"\1", n)) for n in match.group(1).split("|"))
                else:  # TRaSH also writes some groups as \b(Name)\b or with lookarounds: take every word so none is missed
                    general.update(_norm(t) for t in _TOKEN_RE.findall(pattern.replace("\\", " ")))
    general.discard("")
    anime = set()
    for section in ("anime_movie", "anime_episode"):
        data = sections.get(section) or {}
        for item in list(data.get("tiers", ())) + list(data.get("lq", ())):
            for pattern in item.get("patterns", ()):
                anime.update(_norm(t) for t in _TOKEN_RE.findall(pattern.replace("\\", " ")))
    for word in ("bd", "web", "dl", "rip", "dvd", "remux"):
        anime.discard(word)
    return {"general": general, "anime": anime}


# --------------------------------------------------------------------------------------------------------- assembling the list

def _trash_matchers(trash):
    """TRaSH's own compiled matchers, the same ones the sorter will run: ([(mode, matcher)] for movie / episode, the same for anime)."""
    general = []
    anime = []
    for section, bucket in (("movie", general), ("episode", general), ("anime_movie", anime), ("anime_episode", anime)):
        data = trash.get(section) or {}
        for item in list(data.get("tiers", ())) + list(data.get("lq", ())):
            if item.get("patterns"):
                bucket.append((item["mode"], release_groups._Matcher(item["patterns"], item.get("combinable", True))))
    return general, anime


def trash_covers(name, matchers):
    """True when any TRaSH tier or low-quality matcher in the list ranks this group name (as the sorter would see it)."""
    for mode, matcher in matchers:
        texts = (name,) if mode == "group" else (f"[{name}]", f"Show - 01 [{name}]", f"Movie 2020 1080p-{name}")
        if any(matcher.search(text) for text in texts):
            return True
    return False


def _escaped(names):
    return "|".join(re.escape(n) for n in sorted(names, key=str.lower))


def merge(trash, community, shas):
    """TRaSH sections plus the community lists, TRaSH winning every disagreement. Returns the dict that becomes the live file."""
    known = trash_names(trash)
    general_matchers, anime_matchers = _trash_matchers(trash)
    # a community name is dropped when TRaSH ranks it anywhere the sorter would meet it: by the plain name list, or because a TRaSH
    # matcher (which can write a group as \b(Name)\b or with lookarounds) hits it. Anime scoring also applies the general low-quality
    # lists, so a community ban is checked against the anime tiers too, and an anime group against the general ones.
    good = {k: v for k, v in community["good"].items() if k not in known["general"] and not trash_covers(v, general_matchers)}
    bad = {k: v for k, v in community["bad"].items()
           if k not in known["general"] and not trash_covers(v, general_matchers) and not trash_covers(v, anime_matchers)}
    anime = {k: v for k, v in community["anime_good"].items()
             if k not in known["anime"] and k not in known["general"]
             and not trash_covers(v, anime_matchers) and not trash_covers(v, general_matchers)}
    live = {section: {"tiers": list(data["tiers"]), "lq": list(data["lq"])} for section, data in trash.items()}
    if good:
        tier = {"name": "Community groups (Dictionarry / Dumpstarr)", "score": SCORE_COMMUNITY_GOOD, "mode": "group", "req": [], "not": [],
                "any": [], "patterns": [f"^({_escaped(good.values())})$"], "combinable": True}
        for section in ("movie", "episode"):
            live[section]["tiers"].append(tier)
    if bad:
        item = {"mode": "group", "score": SCORE_COMMUNITY_BAD, "patterns": [f"^({_escaped(bad.values())})$"], "combinable": True}
        for section in ("movie", "episode"):
            live[section]["lq"].append(item)
    if anime:
        names = _escaped(anime.values())
        tier = {"name": "Community anime groups (Dictionarry / Dumpstarr)", "score": SCORE_COMMUNITY_ANIME, "mode": "title", "req": [], "not": [],
                "any": [], "patterns": [f"\\[(?:{names})\\]|-(?:{names})\\b"], "combinable": True}
        for section in ("anime_movie", "anime_episode"):
            live[section]["tiers"].append(tier)
    for section in live.values():
        section["tiers"].sort(key=lambda t: -t["score"])
    live["meta"] = {
        "schema": release_groups.LIVE_SCHEMA,
        "builder": BUILDER_VERSION,
        "generated": datetime.date.today().isoformat(),
        "sources": shas,
        "counts": {"community_good": len(good), "community_bad": len(bad), "community_anime": len(anime),
                   "dropped_for_trash": (len(community["good"]) - len(good)) + (len(community["bad"]) - len(bad)),
                   "skipped_patterns": community["stats"]["skipped_patterns"]},
        "license": "TRaSH Guides, Dictionarry and Dumpstarr databases: MIT (see release_groups.NOTICE.txt)",
    }
    return live


def _pattern_count(sections):
    return sum(len(t["patterns"]) for s in release_groups._SECTIONS for t in sections.get(s, {}).get("tiers", ()))


def check_live(live, previous, bundled):
    """Refuse a list that is damaged or shrank suspiciously. Raises SyncError."""
    if not release_groups.valid_live(live):
        raise SyncError("the new list is incomplete")
    for name in release_groups._SECTIONS:
        release_groups._compile_section(live[name])  # a pattern the sorter cannot compile
    for section in release_groups._SECTIONS:
        expected = len((bundled or {}).get(section, {}).get("tiers", ()))
        got = len([t for t in live[section]["tiers"] if not t["name"].startswith("Community")])  # TRaSH's own tiers only
        if expected and got < 0.7 * expected:
            raise SyncError(f"the TRaSH {section} tiers shrank ({got} against {expected})")
        if not any(item.get("score") is None for item in live[section]["lq"]):
            raise SyncError(f"the TRaSH {section} low-quality list is missing")
    counts = live["meta"]["counts"]
    if counts["community_good"] < 30 or counts["community_bad"] < 10 or counts["community_anime"] < 5:
        raise SyncError(f"the community lists are too small ({counts})")
    if previous and release_groups.valid_live(previous):
        before = previous["meta"].get("counts", {})
        for key in ("community_good", "community_bad", "community_anime"):
            if before.get(key) and counts[key] < 0.5 * before[key]:
                raise SyncError(f"{key} fell from {before[key]} to {counts[key]}")
        if _pattern_count(previous) and _pattern_count(live) < 0.7 * _pattern_count(previous):
            raise SyncError("the list lost more than 30% of its patterns")


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------------------------------------- the update

def build_live(shas, deadline, fetch_archive=None, fetch_trash=None):
    """Download, replay, merge and check; returns the new list (not yet written)."""
    archive = fetch_archive or _archive
    trash_sha, dict_sha, dump_sha = shas["trash"], shas["dictionarry"], shas["dumpstarr"]
    pcd_dict, dict_ops = read_ops_archive(archive(DICTIONARRY[0], dict_sha))
    _check_abort(deadline)
    pcd_dump, dump_ops = read_ops_archive(archive(DUMPSTARR[0], dump_sha))
    versions = {_schema_version(pcd_dict), _schema_version(pcd_dump)}
    if len(versions) != 1 or not versions <= set(SUPPORTED_SCHEMAS):
        raise SyncError(f"the databases use schema {sorted(str(v) for v in versions)}, this version supports {SUPPORTED_SCHEMAS}")
    version = versions.pop()
    _pcd, schema_ops = read_ops_archive(archive(SCHEMA_REPO, f"refs/tags/{version}"))
    if not schema_ops or not dict_ops or not dump_ops:
        raise SyncError("an archive held no ops files")
    _check_abort(deadline)
    replay_deadline = min(deadline, time.monotonic() + REPLAY_BUDGET_SECONDS)
    con_dict, err_dict = build_database(schema_ops, dict_ops, replay_deadline)
    try:
        con_dump, err_dump = build_database(schema_ops, dump_ops, replay_deadline)
    except Exception:
        con_dict.close()
        raise
    try:
        if err_dict + err_dump > MAX_OP_ERRORS:
            raise SyncError(f"{err_dict + err_dump} upstream ops files failed to replay")
        community = read_community(con_dict, con_dump)
    finally:
        con_dict.close()
        con_dump.close()
    community["stats"]["op_errors"] = err_dict + err_dump
    _check_abort(deadline)
    if fetch_trash is not None:
        trash = fetch_trash(trash_sha, deadline)
    else:
        trash = build_trash(trash_sha, deadline)
    return merge(trash, community, {"trash": trash_sha, "dictionarry": dict_sha, "dumpstarr": dump_sha})


def sync(force=False, head_fn=None, fetch_archive=None, fetch_trash=None):
    """One update. Returns (status, message): status is 'updated', 'unchanged', 'busy', 'aborted' or 'failed'."""
    if not _acquire_lock():
        return "busy", "another update is running"
    state = read_state()
    now = time.time()
    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    try:
        head = head_fn or _head_sha
        shas = {"trash": head(TRASH), "dictionarry": head(DICTIONARRY), "dumpstarr": head(DUMPSTARR)}
        _check_abort(deadline)
        live_file = release_groups.live_path()
        previous = release_groups._read_live()
        if (not force and previous is not None and previous.get("meta", {}).get("sources") == shas
                and previous.get("meta", {}).get("builder") == BUILDER_VERSION):
            state.update({"last_check": now, "last_error": None})
            _save_state(state)
            return "unchanged", "the lists are already up to date"
        live = build_live(shas, deadline, fetch_archive, fetch_trash)
        bundled = _read_json_file(release_groups._DATA_PATH)
        check_live(live, previous, bundled)
        _write_atomic(live_file, json.dumps(live, ensure_ascii=False, separators=(",", ":")))
        release_groups.reload()
        counts = live["meta"]["counts"]
        state.update({"last_check": now, "last_success": now, "last_error": None, "last_fail": 0, "sources": shas, "counts": counts})
        _save_state(state)
        message = (f"{counts['community_good']} community groups, {counts['community_bad']} banned, "
                   f"{counts['community_anime']} anime added to the TRaSH tiers")
        g.log(f"Release group lists updated: {message}", "info")
        return "updated", message
    except SyncAborted:
        return "aborted", "Kodi is closing"
    except SyncError as e:
        state.update({"last_fail": now, "last_error": str(e)})
        _save_state(state)
        g.log(f"Release group lists not updated, the previous list stays: {e}", "warning")
        return "failed", str(e)
    except Exception as e:  # never let a surprise reach the service
        state.update({"last_fail": now, "last_error": f"{type(e).__name__}: {e}"})
        _save_state(state)
        g.log(f"Release group lists not updated, the previous list stays: {type(e).__name__}: {e}", "warning")
        return "failed", f"{type(e).__name__}: {e}"
    finally:
        _release_lock()


def run_if_due():
    """The service's weekly check: only with the setting on and the Release Group level in use, at most once a week."""
    try:
        if not enabled():
            return None
        from resources.lib.modules.anime.seadex import level_enabled
        if not level_enabled():
            return None
        state = read_state()
        now = time.time()
        for key, interval in (("last_check", SYNC_INTERVAL), ("last_fail", FAIL_COOLDOWN)):
            age = now - float(state.get(key) or 0)
            if 0 <= age < interval:
                return None
        return sync()
    except Exception as e:
        g.log(f"Release group update check failed: {e}", "warning")
        return None


def update_now_action():
    """Settings > Source Sorting > Update release group lists now."""
    g.show_busy_dialog()
    try:
        status, message = sync(force=True)
    finally:
        g.close_busy_dialog()
    title = 31753 if status in ("updated", "unchanged") else 31754  # "Release group lists updated" / "... not updated"
    g.notification(g.ADDON_NAME, f"{g.get_language_string(title)}: {message}")
