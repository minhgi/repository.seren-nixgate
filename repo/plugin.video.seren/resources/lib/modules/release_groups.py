"""Release group ranking from TRaSH Guides tiers, for the "Release Group" sort level.

resources/data/release_groups.json is a snapshot built by tools/build_release_groups.py from the TRaSH Guides custom formats
(MIT, see resources/data/release_groups.NOTICE.txt). It is bundled with the add-on and never fetched at runtime, so ranking is
offline, instant and cannot fail the scrape.

score(source, media, anime) returns a small whole number, higher is better, so the level can sit above Size and Seeders in the
sort order:

    TRaSH tier score  (1950 remux tier 1 ... 1600 web tier 3 for movies and TV; 1400 ... 100 for anime)
    0                 group not in any tier
    -10000            group on TRaSH's low-quality list

The list normally comes from release_groups_sync.py (rebuilt weekly on the device from TRaSH Guides plus the Dictionarry and Dumpstarr
databases: one community tier below every TRaSH tier at 1500, community bans at -5000, anime community groups at 50; TRaSH always wins a
disagreement) and falls back to the bundled TRaSH snapshot. The scores above are the TRaSH snapshot's.

A tier only counts when the release really is the kind of release the tier is for (a remux tier needs a REMUX release, a web tier
a WEB one ...), judged from the info tags and quality Seren already put on the source. Movies use the Radarr tiers, episodes the
Sonarr tiers, anime its own anime tiers (whole-title match, as in TRaSH) plus the general low-quality list.
"""
import json
import os
import re
import time

LQ_SCORE = -10000
_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "release_groups.json")
_SECTIONS = ("movie", "episode", "anime_movie", "anime_episode")

# release_groups_sync.py (run weekly by the service) writes a newer list, built on the device from the TRaSH Guides and the
# Dictionarry / Dumpstarr databases, to this file in the add-on data folder. It is used when valid; the bundled snapshot above is
# the fallback (first run, offline, a rejected or damaged update), so the ranking never depends on the network.
LIVE_FILE_NAME = "release_groups_live.json"
LIVE_SCHEMA = 1

# The score-only fallbacks below (a group followed by _suffix, the last word of the title) must never read an everyday word or a
# format tag as a group, so these are never taken from the last word even when a list names them.
_FALLBACK_DENY = frozenset((
    "ghost", "elite", "pirates", "lion", "panda", "natty", "ime", "kappa", "extreme", "amen", "ivy", "lama", "ben", "kingdom",
    "tbs", "deflate", "fiend", "turg", "evo", "hdt", "web", "webrip", "webdl", "bluray", "bdrip", "dvdrip", "remux", "hdr", "hdr10",
    "dv", "uhd", "x264", "x265", "h264", "h265", "hevc", "avc", "aac", "ac3", "dts", "atmos", "10bit", "8bit", "repack", "proper",
    "internal", "multi", "dual", "dubbed", "subbed", "subs", "final", "extended", "uncut", "complete", "season", "part", "mkv",
))

_EXT_RE = re.compile(r"\.(?:mkv|mp4|m4v|avi|ts|m2ts|wmv|mov|iso|nfo|srt)$", re.IGNORECASE)
_TRAILING_TAG_RE = re.compile(r"\s*[\[(][^\])]*[\])]\s*$")
_GROUP_RE = re.compile(r"-([A-Za-z0-9]+)$")
_GROUP_DOT_RE = re.compile(r"-([A-Za-z0-9]+\.[A-Za-z0-9]+)$")  # YTS.MX
_GROUP_PAIR_RE = re.compile(r"(?:^|[^A-Za-z0-9])([A-Za-z0-9]+-[A-Za-z0-9]+)$")  # D-Z0N3
_TRAILING_TAG_CAPTURE_RE = re.compile(r"\s*[\[(]([^\])]*)[\])]\s*$")

# Anime titles often name the source only in the title text ("[BD 1080p]", "BDRip", "WEB-DL"), not in Seren's tags.
_ANIME_TITLE_TOKENS = (
    ("remux", re.compile(r"\bremux\b", re.IGNORECASE)),
    ("bluray", re.compile(r"\bBD\b|\bBD-?rip\b|\bBR-?rip\b|\bblu-?ray\b|\bBDMV\b", re.IGNORECASE)),
    ("dvd", re.compile(r"\bDVD(?:-?rip)?\b", re.IGNORECASE)),
    ("web", re.compile(r"\bWEB(?:-?DL|-?rip)?\b", re.IGNORECASE)),
)

_sections = None  # {name: {"tiers": [...], "lq": [...]}}; filled on first use
_known = None  # lower-case names of the groups the general lists name (for the score-only fallbacks); filled with _sections
_live_stamp = None  # modification time of the live file when _sections was built (None: no live file)
_stamp_checked = 0.0  # monotonic time of the last look at the live file
_STAMP_CHECK_SECONDS = 30  # a reused plugin interpreter notices a newly written list within this long


class _Matcher:
    """One tier's (or LQ list's) patterns as a single compiled regex; falls back to one regex per pattern."""

    def __init__(self, patterns, combinable):
        self._one = None
        self._many = None
        if combinable:
            try:
                self._one = re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)
                return
            except re.error:
                pass
        self._many = [re.compile(p, re.IGNORECASE) for p in patterns]

    def search(self, text):
        if self._one is not None:
            return self._one.search(text) is not None
        return any(rx.search(text) for rx in self._many)


def _compile_section(raw):
    tiers = []
    for tier in raw.get("tiers", []):
        if not tier.get("patterns"):
            continue
        tiers.append({
            "score": int(tier["score"]),
            "mode": tier["mode"],
            "req": frozenset(tier.get("req", ())),
            "not": frozenset(tier.get("not", ())),
            "any": frozenset(tier.get("any", ())),
            "matcher": _Matcher(tier["patterns"], tier.get("combinable", True)),
        })
    tiers.sort(key=lambda t: -t["score"])  # first match is the highest-scoring one
    # a low-quality list may carry its own (less negative) score; the TRaSH list uses LQ_SCORE
    lq = [{"mode": item["mode"], "score": int(item.get("score", LQ_SCORE)), "matcher": _Matcher(item["patterns"], item.get("combinable", True))}
          for item in raw.get("lq", []) if item.get("patterns")]
    return {"tiers": tiers, "lq": lq}


def live_path():
    """Where release_groups_sync.py keeps the list it built on this device."""
    from resources.lib.modules.globals import g
    return os.path.join(g.ADDON_USERDATA_PATH, LIVE_FILE_NAME)


def _stamp_of_live():
    try:
        return os.stat(live_path()).st_mtime
    except Exception:
        return None


def valid_live(raw):
    """True for a list written by release_groups_sync.py: the right schema and all four sections with tiers."""
    meta = raw.get("meta") if isinstance(raw, dict) else None
    if not isinstance(meta, dict) or meta.get("schema") != LIVE_SCHEMA:
        return False
    return all(isinstance(raw.get(name), dict) and raw[name].get("tiers") for name in _SECTIONS)


def _read_live():
    """The device-built list as parsed JSON, or None when it is missing, damaged or from another schema."""
    try:
        path = live_path()
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return raw if valid_live(raw) else None
    except Exception:
        return None


_NAME_LIST_RE = re.compile(r"^\^\((.+)\)\$$")
_PLAIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _known_names(raw):
    """Lower-case names of every group a general (movie / episode) list names, read from its ^(a|b|c)$ patterns."""
    names = set()
    for section in ("movie", "episode"):
        data = raw.get(section) or {}
        for item in list(data.get("tiers", ())) + list(data.get("lq", ())):
            if item.get("mode") != "group":
                continue
            for pattern in item.get("patterns", ()):
                match = _NAME_LIST_RE.match(pattern)
                if not match:
                    continue
                for name in match.group(1).split("|"):
                    name = re.sub(r"\\(.)", r"\1", name)
                    if _PLAIN_NAME_RE.match(name):
                        names.add(name.lower())
    return names


def _load():
    """Load and compile the list once: the device-built one when valid, else the bundled snapshot.

    Any problem leaves the ranking neutral (score 0 for everything)."""
    global _sections, _known, _live_stamp, _stamp_checked
    if _sections is not None:
        now = time.monotonic()
        if now - _stamp_checked < _STAMP_CHECK_SECONDS:
            return _sections
        _stamp_checked = now
        if _stamp_of_live() == _live_stamp:
            return _sections
    _stamp_checked = time.monotonic()
    _live_stamp = _stamp_of_live()
    sections = {}
    known = set()
    raw = _read_live()
    if raw is not None:
        try:
            sections = {name: _compile_section(raw.get(name) or {}) for name in _SECTIONS}
            known = _known_names(raw)
        except Exception as e:  # a pattern the compiler rejects: fall back to the bundled snapshot
            sections = {}
            raw = None
            try:
                from resources.lib.modules.globals import g
                g.log(f"Release group list from the last update not used: {e}", "warning")
            except Exception:
                pass
    if not sections:
        try:
            with open(_DATA_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            for name in _SECTIONS:
                sections[name] = _compile_section(raw.get(name) or {})
            known = _known_names(raw)
        except Exception as e:
            sections = {}
            known = set()
            try:
                from resources.lib.modules.globals import g
                g.log(f"Release group snapshot not loaded, ranking by release group is neutral: {e}", "warning")
            except Exception:
                pass
    _sections = sections
    _known = known
    return _sections


def reload():
    """Forget the compiled list so the next score() reads the files again (used after an update in the same interpreter)."""
    global _sections, _known, _live_stamp, _stamp_checked
    _sections = None
    _known = None
    _live_stamp = None
    _stamp_checked = 0.0


def extract_group(release_title):
    """The scene-style release group of a title: the text after the last '-' once a file extension and trailing [tags] are removed."""
    title = (release_title or "").strip()
    title = _EXT_RE.sub("", title)
    for _ in range(3):
        stripped = _TRAILING_TAG_RE.sub("", title)
        if stripped == title:
            break
        title = stripped
    match = _GROUP_RE.search(title)
    return match.group(1) if match else ""


def extract_groups(release_title):
    """Every plausible release group of a title, best guess first.

    Handles 'Name-GROUP/folder.name' pairs (each '/' part is tried), trailing [tags] such as '[YTS.MX]', dotted groups
    ('-YTS.MX') and hyphenated groups ('-D-Z0N3', where the plain last token would be 'Z0N3').
    """
    candidates = []
    tag_candidates = []

    def add(value, into=None):
        into = candidates if into is None else into
        if value and value not in into:
            into.append(value)

    for part in (release_title or "").split("/"):
        title = _EXT_RE.sub("", part.strip())
        tags = []
        for _ in range(3):
            match = _TRAILING_TAG_CAPTURE_RE.search(title)
            if not match:
                break
            tags.append(match.group(1).strip())
            title = title[:match.start()]
        for rx in (_GROUP_RE, _GROUP_DOT_RE, _GROUP_PAIR_RE):
            match = rx.search(title)
            if match:
                add(match.group(1))
        match = _GROUP_DOT_RE.search(title)
        if match:
            add(match.group(1).split(".")[0])
        for tag in tags:  # "[YTS.MX]" style group tags
            add(tag, tag_candidates)
    # Bracket tags are often tracker or format stamps ([rarbg], [1080p]); they only stand in for the group when the title
    # has no "-GROUP" suffix, so a stamp can never override or sink a real suffix group.
    return candidates if candidates else tag_candidates


_SUFFIX_GROUP_RE = re.compile(r"-([A-Za-z0-9]+)[_.][A-Za-z0-9_.\[\]-]*$")  # x265-FLUX_EniaHD, x264-GRP.mkv.part
_LAST_WORD_RE = re.compile(r"(?:^|[\s.()\[\]-])([A-Za-z0-9]{3,})\s*$")  # "... 1080p WEB-DL MeGusta"; not after "_" (Dark_Lord is not LoRD)


def score_groups(release_title):
    """The groups score() tries: what extract_groups() finds, or - only when that finds nothing - two safe fallbacks.

    extract_groups() stays as it is because the SeaDex and AniDB matching share it. The fallbacks only run for the score and only
    accept a name one of the general lists already knows (and never an everyday word or format tag, see _FALLBACK_DENY):

        -GROUP_suffix / -GROUP.suffix   the group before an underscore or dot suffix ("...x265-FLUX_EniaHD.mkv")
        last word of the title          a group written without a hyphen ("... WEB-DL MeGusta")
    """
    groups = extract_groups(release_title)
    if groups:
        return groups
    known = _known
    if known is None:
        _load()
        known = _known
    if not known:
        return groups
    title = _EXT_RE.sub("", (release_title or "").strip())
    found = []
    for rx in (_SUFFIX_GROUP_RE, _LAST_WORD_RE):
        match = rx.search(title)
        if match:
            word = match.group(1)
            low = word.lower()
            if low in known and low not in _FALLBACK_DENY and word not in found:
                found.append(word)
    return found


def release_tokens(source, anime=False):
    """What kind of release this is, from the tags and quality Seren already worked out: remux, bluray, web, dvd, 2160p."""
    info = source.get("info") or ()
    tokens = set()
    if "REMUX" in info:
        tokens.add("remux")
    if "BLURAY" in info:
        tokens.add("bluray")
    if "WEB" in info:
        tokens.add("web")
    if "DVDRIP" in info:
        tokens.add("dvd")
    if anime:
        title = source.get("release_title")
        if isinstance(title, str):
            for token, rx in _ANIME_TITLE_TOKENS:
                if rx.search(title):
                    tokens.add(token)
    quality = source.get("quality")
    if isinstance(quality, str) and quality.split("/")[0] == "4K":
        tokens.add("2160p")
    return tokens


def score(source, media, anime=False):
    """Rank of a source's release group: a TRaSH tier score, 0 when unknown, LQ_SCORE when TRaSH lists the group as low quality.

    :param source: Seren source dict (needs release_title; uses info and quality)
    :param media: "movie" or "episode"
    :param anime: use the anime tiers (whole-title match) and add the general low-quality list
    """
    sections = _load()
    title = source.get("release_title") or ""
    if not sections or not isinstance(title, str) or not title:
        return 0
    groups = score_groups(title)
    names = [media]
    if anime:
        names = ["anime_" + media, media]  # anime tiers decide the tier; the general LQ list also applies
    primary = sections.get(names[0])
    if primary is None:
        return 0
    lowest = None
    for name in names:
        section = sections.get(name)
        for lq in (section["lq"] if section else ()):
            texts = [title] if lq["mode"] == "title" else groups
            if any(lq["matcher"].search(text) for text in texts):
                lowest = lq["score"] if lowest is None else min(lowest, lq["score"])
    if lowest is not None:
        return lowest
    tokens = release_tokens(source, anime)
    if anime and not tokens & {"remux", "bluray", "web", "dvd"}:
        # Modern fansub rips ("[SubsPlease] Show - 01 (1080p)") name no source at all. They are web rips, so they may match
        # the anime web tiers; with no BD/remux/DVD tag they can never match a BD tier.
        tokens.add("web")
    for tier in primary["tiers"]:
        if not tier["req"] <= tokens or tier["not"] & tokens:
            continue
        if tier["any"] and not tier["any"] & tokens:
            continue
        texts = [title] if tier["mode"] == "title" else groups
        if any(tier["matcher"].search(text) for text in texts):
            return tier["score"]
    return 0
