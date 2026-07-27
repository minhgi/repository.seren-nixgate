# -*- coding: utf-8 -*-
from base64 import b64encode
import re
import requests
import time
import unicodedata
from urllib.parse import quote

import xbmcgui

from resources.lib.common.source_utils import (
    clean_title,
    get_info,
    get_quality,
    de_string_size,
    remove_from_title,
    remove_country,
)
from resources.lib.modules.exceptions import PreemptiveCancellation

from providerModules.a4kNewsgroups import common

_exclusions = ["soundtrack", "gesproken", "sample", "trailer", "extras only", " ost"]
_resolutions = [
    "2160p",
    "216op",
    "4k",
    "1080p",
    "1o8op",
    "108op",
    "1o80p",
    "720p",
    "72op",
    "480p",
    "48op",
]

# CJK character ranges — EasyNews Solr doesn't index CJK well; skip CJK-heavy aliases
_CJK_RE = re.compile(r"[　-鿿豈-﫿가-힯]")

# Maximum alias queries added per search (keeps wall-clock time bounded)
_MAX_ALIAS_QUERIES = 3


class sources:
    def __init__(self):
        self.language = ["en"]
        self.base_link = "https://members.easynews.com"
        self.search_link = "/2.0/search/solr-search/advanced"
        self.query_link = f"{self.base_link}{self.search_link}"
        self.auth = self._get_auth()
        self.start_time = 0
        self.sort_params = {
            "s1": "relevance",
            "s1d": "-",
            "s2": "dsize",
            "s2d": "-",
            "s3": "dtime",
            "s3d": "-",
        }
        self.search_params = {
            "st": "adv",
            "sb": 1,          # Solr search backend (required for phrase matching)
            "fex": "m4v,3gp,mov,divx,xvid,wmv,avi,mpg,mpeg,mp4,mkv,avc,flv,webm",
            "fty[]": "VIDEO",
            "spamf": 1,       # Spam filter: drop posts crossposted >100 times
            "u": "1",         # Remove duplicate results
            "gx": 1,
            "pno": 1,         # Page number
            "sS": 3,
            "pby": 1000,      # Results per page (was 350)
            "safeO": 0,
        }
        self.search_params.update(self.sort_params)

    def _get_auth(self):
        auth = None
        try:
            username = common.get_setting("easynews.username")
            password = common.get_setting("easynews.password")
            if username == "" or password == "":
                return auth
            user_info = f"{username}:{password}"
            user_info = user_info.encode("utf-8")
            auth = f"Basic {b64encode(user_info).decode('utf-8')}"
        except Exception as e:
            common.log(f"a4kNewsgroups.easynews: could not authorize - {e}", "error")
        return {"Authorization": auth}

    def _return_results(self, source_type, sources, preemptive=False):
        if preemptive:
            common.log(
                f"a4kNewsgroups.{source_type}.easynews: cancellation requested",
                "info",
            )
        elif preemptive is None:
            common.log(
                f"a4kNewsgroups.{source_type}.easynews: not authorized",
                "info",
            )

        common.log(f"a4kNewsgroups.{source_type}.easynews: {len(sources)}", "info")
        common.log(
            f"a4kNewsgroups.{source_type}.easynews: took {int((time.time() - self.start_time) * 1000)} ms",
            "info",
        )
        return sources

    def _make_query(self, query):
        query = "".join(
            [c for c in unicodedata.normalize("NFKD", query) if unicodedata.category(c) != "Mn"]
        )

        params = dict(self.search_params)
        params["gps"] = query
        results = requests.get(
            self.query_link,
            params=params,
            headers=self.auth,
            timeout=20,
        ).json()

        down_url = results.get("downURL")
        dl_farm = results.get("dlFarm")
        dl_port = results.get("dlPort")
        files = results.get("data", [])

        return down_url, dl_farm, dl_port, files

    def _process_item(self, item, down_url, dl_farm, dl_port, simple_info):
        post_hash, size, post_title, ext, duration = (
            item["0"],
            item["4"],
            item["10"],
            item["11"],
            item.get("14", ""),
        )

        if item.get("virus"):
            return
        if item.get("type", "").upper() != "VIDEO":
            return
        # Duration filter: skip clips shorter than ~5 minutes (trailers, samples)
        if re.match(r"^\d+s", duration) or re.match(r"^[0-5]m", duration):
            return

        cleaned = clean_title(post_title)

        if not self.title_check(post_title, simple_info):
            return
        if self._check_exclusions(cleaned):
            return
        if not self._check_languages(item.get("alangs", []), item.get("slangs", [])):
            return

        stream_url = down_url + quote(f"/{dl_farm}/{dl_port}/{post_hash}{ext}/{post_title}{ext}")
        file_dl = f"{stream_url}|Authorization={quote(self.auth['Authorization'])}"

        # Title is authoritative; fullres ("1920 x 1080") or width fills in when title
        # has no resolution token. fullres tried first — it is non-zero more often than
        # the integer width field on posts that lack explicit resolution in the title.
        quality = get_quality(post_title)
        if quality == "SD":
            fullres = item.get("fullres", "") or ""
            m_res = re.match(r"(\d+)\s*[xX]\s*(\d+)", fullres)
            width = int(m_res.group(1)) if m_res else int(item.get("width", 0) or 0)
            if width > 1920:
                quality = "4K"
            elif width > 1280:
                quality = "1080p"
            elif width > 720:
                quality = "720p"
            # else: keep "SD" — width ≤ 720 or unknown confirms SD

        source = {
            "scraper": "easynews",
            "release_title": post_title,
            "info": get_info(post_title),
            "size": de_string_size(size),
            "quality": quality,
            "url": file_dl,
            "debrid_provider": "Usenet",
            "headers": self.auth,
            "filetype": ext,
        }
        return source

    def _run_queries(self, queries, simple_info):
        """Run queries sequentially; return deduplicated sources list."""
        seen_urls = set()
        results = []

        for q in queries:
            try:
                down_url, dl_farm, dl_port, files = self._make_query(q)
                for item in files:
                    source = self._process_item(item, down_url, dl_farm, dl_port, simple_info)
                    if source is not None and source["url"] not in seen_urls:
                        seen_urls.add(source["url"])
                        results.append(source)
            except PreemptiveCancellation:
                raise
            except Exception as e:
                common.log(f"a4kNewsgroups.easynews: query failed: {e}", "warning")

        return results

    @staticmethod
    def _alias_queries(primary_title, aliases, suffix):
        """Build up to _MAX_ALIAS_QUERIES additional search strings from the alias list.

        Filters CJK-heavy strings (EasyNews Solr won't index them meaningfully) and
        deduplicates against the primary title."""
        seen = {primary_title.lower()}
        extra = []
        for alias in aliases:
            if not alias:
                continue
            if len(_CJK_RE.findall(alias)) > len(alias) * 0.4:
                continue
            a = clean_title(alias)
            if not a or a.lower() in seen or len(a) < 3:
                continue
            seen.add(a.lower())
            extra.append(f'{a} {suffix}')
            if len(extra) >= _MAX_ALIAS_QUERIES:
                break
        return extra

    def episode(self, simple_info, info, **kwargs):
        self.start_time = time.time()
        sources = []
        if not self.auth:
            return self._return_results("episode", sources, preemptive=None)

        show_title = clean_title(simple_info["show_title"])
        season_xx = simple_info["season_number"].zfill(2)
        episode_xx = simple_info["episode_number"].zfill(2)
        absolute_number = simple_info.get("absolute_number", "")
        is_anime = simple_info["isanime"]
        aliases = simple_info.get("show_aliases", [])

        season_ep = f"S{season_xx}E{episode_xx}"
        queries = [f'{show_title} {season_ep}']

        # Absolute-number fallback for anime
        # (previously dead code — return was inside the for loop so this never ran)
        if is_anime and absolute_number:
            queries.append(f'{show_title} {absolute_number}')

        # Bare E{xx} query: K-drama files often use "Show E01" without a season prefix.
        # Only for non-anime season-1 (bare E files have no season context).
        if not is_anime and season_xx == "01":
            queries.append(f'{show_title} E{episode_xx}')

        # Alias queries: enriched by Seren's AniList/MAL/Asian-drama title expansion
        queries.extend(self._alias_queries(show_title, aliases, season_ep))

        try:
            sources = self._run_queries(queries, simple_info)
        except PreemptiveCancellation:
            return self._return_results("episode", sources, preemptive=True)

        return self._return_results("episode", sources)

    def movie(self, title, year, imdb, simple_info=None, info=None, **kwargs):
        self.start_time = time.time()
        sources = []
        if not self.auth:
            return self._return_results("movie", sources, preemptive=None)

        _si = simple_info or {}
        title = clean_title(_si.get("title") or title)
        year = str(_si.get("year") or year)
        aliases = _si.get("aliases", [])

        queries = [f'{title} {year}']
        queries.extend(self._alias_queries(title, aliases, year))

        try:
            sources = self._run_queries(queries, _si)
            if not sources:
                # Last-resort: title only, no year (original fallback behaviour)
                down_url, dl_farm, dl_port, files = self._make_query(title)
                for item in files:
                    source = self._process_item(item, down_url, dl_farm, dl_port, _si)
                    if source is not None:
                        sources.append(source)
        except PreemptiveCancellation:
            return self._return_results("movie", sources, preemptive=True)

        return self._return_results("movie", sources)

    @staticmethod
    def title_check(post_title, simple_info):
        meta_title = simple_info.get("title") or simple_info.get("show_title") or ""
        aliases = simple_info.get("aliases") or simple_info.get("show_aliases") or []
        post_cleaned = clean_title(post_title)
        if "show_title" in simple_info:
            country = simple_info.get("country", "")
            year = simple_info.get("year", "")
            post_cleaned = remove_country(post_cleaned, country)
            post_cleaned = remove_from_title(post_cleaned, year)
        meta_cleaned = clean_title(meta_title)
        titles_cleaned = [meta_cleaned, *[clean_title(alias) for alias in aliases]]
        episode_regex = r"(.*)((?:s(\d+) ?e(\d+))|(?:season ?(\d+) ?(?:episode|ep) ?(\d+))|(?: (\d+) ?x ?(\d+)))(.*)"

        split = re.split(rf"{simple_info.get('year')}", post_cleaned, 1, re.I)[0]
        split = re.split(
            rf"{'|'.join(_resolutions)}",
            split,
            1,
            re.I,
        )[0]
        split = split.rstrip()

        if "show_title" in simple_info:
            data = re.search(episode_regex, split)

            if data is None:
                # Bare-episode fallback: handles EP NN, E NN, and romanized "Title NN"
                # patterns that have no season prefix and don't match episode_regex.
                ep_bare = None

                # Pattern A: explicit ep/e prefix ("EP 08", "EP08", "E01", "E 01")
                m = re.search(r'(.*?)\sep\s*(\d+)(?:\s|$)', split, re.I)
                if m is None:
                    m = re.search(r'(.*?)\se\s*(\d+)(?:\s|$)', split, re.I)
                if m:
                    show_title_bare = m.group(1).rstrip()
                    ep_bare = int(m.group(2))
                    if not any(
                        show_title_bare == t or (t and show_title_bare.endswith(' ' + t))
                        for t in titles_cleaned
                    ):
                        return False
                else:
                    # Pattern B: romanized/alias title anchors the bare number
                    # e.g. "[RH] Danshi Koukousei no Nichijou - 01 [hash]" →
                    # clean → "rh danshi koukousei no nichijou 01 hash"
                    matched = False
                    for t in titles_cleaned:
                        if not t or len(t) < 5:
                            continue
                        m2 = re.search(
                            rf'(?:^|\s){re.escape(t)}\s+(\d{{1,3}})(?:\s|$)',
                            split, re.I
                        )
                        if m2:
                            ep_bare = int(m2.group(1))
                            matched = True
                            break
                    if not matched:
                        return False

                # Season gate: bare episodes carry no season context.
                # Accept only when: season==1 and ep matches, OR absolute_number matches.
                ep_expected = int(simple_info["episode_number"])
                abs_num     = int(simple_info.get("absolute_number") or 0)
                season      = int(simple_info["season_number"])
                if not ((season == 1 and ep_bare == ep_expected) or
                        (abs_num > 0 and ep_bare == abs_num)):
                    return False
                return True

            data = data.groups()

            show_title = (data[0] or "").rstrip()

            # endswith fix: handles group-prefixed titles like "[ZECHS] Show" →
            # "zechs show", where show_title.endswith(" show") identifies the show.
            if not any(
                show_title == t or (t and show_title.endswith(' ' + t))
                for t in titles_cleaned
            ):
                return False

            numbers = []
            for pos in [(2, 3), (4, 5), (6, 7)]:
                if not any([None in (data[pos[0]], data[pos[1]])]):
                    numbers.append((int(data[pos[0]]), int(data[pos[1]])))
            if numbers and not any(
                [
                    i
                    == (
                        int(simple_info["season_number"]),
                        int(simple_info["episode_number"]),
                    )
                    for i in numbers
                ]
            ):
                return False
            return True
        elif any([split == title for title in titles_cleaned]):
            return True

        return False

    @staticmethod
    def _check_exclusions(clean_title):
        return any([i in clean_title for i in _exclusions])

    @staticmethod
    def _check_languages(alangs, slangs):
        english_audio = alangs is None or "eng" in alangs
        english_subtitles = slangs is None or "eng" in slangs
        return english_audio or english_subtitles

    @staticmethod
    def get_listitem(return_data):
        list_item = xbmcgui.ListItem(path=return_data["url"], offscreen=True)
        list_item.setContentLookup(False)
        list_item.setProperty("isFolder", "false")
        list_item.setProperty("isPlayable", "true")

        return list_item
