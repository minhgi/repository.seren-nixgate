"""Universal Playback Watchdog

Monitors actual playback after source resolution. If a stream stalls
(getTime() stops advancing), automatically stops and cycles to the next
source. Works for all source types: Easynews direct, Premiumize CDN,
Real-Debrid, TorBox, AllDebrid, hosters, etc.

Architecture:
  - Replaces resolve_silent_or_visible() + play_source() when enabled
  - Contains its own resolve loop (reuses Resolver.resolve_single_source)
  - Reuses ContentVerifier for content mismatch detection
  - Reuses CloudMiss detection with same runtime flags
  - Uses xbmc.Player().play() instead of setResolvedUrl for retry capability
  - On success: returns (stream_link, release_title) — caller attaches SerenPlayer
  - On failure: returns (None, None) — caller handles rescrape/fallback

Integration:
  - Router.getSources action (autoplay path only, not source select)
  - Controlled by playback.watchdog setting (default off)
  - Sets cloud_miss_rescrape / content_verify_rescrape flags for router rescrape
"""

import copy
import importlib
import sys
import time
from urllib.parse import parse_qsl

import xbmc
import xbmcaddon
import xbmcgui

from resources.lib.modules.globals import g
from resources.lib.modules.resolver import Resolver
from resources.lib.modules.resolver.content_verifier import ContentVerifier

# Resume seek retry (3.9.104). Kodi can drop the first seekTime() when it arrives while the stream is still being
# opened (a slow HTTP/debrid stream: the player's own start-of-file seek then runs after ours and wins), so the episode
# played from 0 although a resume point was found. _wait_for_settle re-seeks, a few times at most and only while the
# player is evidently still running from the start (never after the user has moved the position themselves).
_RESUME_RETRY_MAX = 3  # extra seekTime() calls after the first one
_RESUME_RETRY_GAP = 3.0  # seconds that must pass since the previous seek before another is sent
_RESUME_RETRY_WINDOW = 15.0  # seconds after the first seek in which a retry may still be sent
_RESUME_RETRY_ADVANCING_POLLS = 2  # consecutive polls with getTime() advancing (a position stuck at 0 is a seek in progress)
_RESUME_RETRY_MIN_SHORTFALL = 10.0  # the position must be at least this far below the target to count as "not resumed"

# Slow stream open (3.9.106). A Usenet/Easynews file can take 20+ s to open (seek into the file, stream probe). Stopping
# or replacing the player while Kodi is still opening the stream froze Kodi on the Shield (the GUI thread waited for the
# player's open thread, which was waiting for the display-mode switch): the watchdog's 20 s stop landed exactly there.
# So after the normal wait the watchdog keeps waiting while the player is still opening, up to this hard cap, and only
# gives up at once when the player has gone idle (then there is nothing to stop).
_OPEN_WAIT_BASE = 20.0  # seconds a start is awaited before the player's state is looked at
_OPEN_WAIT_CAP = 90.0  # seconds after play() beyond which an opening stream is given up on

# Failed open vs user stop (3.9.107). When Kodi itself cannot open a stream (a dead or very slow host: two 20 s cURL
# timeouts, about 41 s) it reports onPlayBackStopped, exactly like a user pressing Stop, so the watchdog quit with
# "User stopped playback" and never tried the next source; and a Stop pressed during an open blocks Kodi's window until
# the open thread ends, so its callback arrived after the 20 s check, which then blacklisted the source and started the
# next one. During the start-up phase only, a stop callback counts as a failed source when the user's input idle timer
# (xbmc.getGlobalIdleTime) was never reset while the stream was opening (Kodi itself resets it when play() starts, so
# it is watched from then on, not compared with its value at play()); anything else stays a user stop.
_END_TOLERANCE = 5.0  # seconds: a stream that ends this close to its total duration reached its end (Seren 3.9.114)
_OPEN_CALLBACK_WAIT = 25.0  # seconds to wait for the stop/error callback of an open that ended without one
_NO_INPUT_MIN_AGE = 3.0  # a stop this soon after play() is read as the user's unless the idle timer was already high
_IDLE_RELIABLE_MIN = 2.0  # idle seconds at play() above which a key press right after play() is still visible
_IDLE_TOLERANCE = 1.0  # getGlobalIdleTime() counts whole seconds

# Fast failure on the first attempt (3.9.108). A source that fails within about a second (an expired link, 403/404/410, a
# reset connection) was still read as the user's Stop when it came before _NO_INPUT_MIN_AGE and the idle timer could not
# vouch for the user (it reads ~0 at the first play(): Seren's own windows reset it), so the playback ended with "User
# stopped" and the next source was never tried. In that one ambiguous case the link is probed with a tiny ranged GET:
# an HTTP error or a connection failure means the source failed, anything else keeps the old reading (a user stop).
_PROBE_TIMEOUT = (3.0, 3.0)  # (connect, read) seconds; Windows needs ~2 s to report a refused connection; waits at most this long once


def _probe_stream(link):
    """Ask the host whether a stream link still answers: returns (verdict, detail).

    verdict "failed" = the host answered with an HTTP error or the connection failed or was reset;
    "ok" = it answered with a success status; "unknown" = not an http(s) link, or the probe timed out or could not be
    made. The detail is a status code or an exception class name only: the link and its headers can carry a token and are
    never logged.
    """
    try:
        url, _, header_part = str(link).partition("|")
        if not url.lower().startswith(("http://", "https://")):
            return "unknown", "not an http link"
        headers = dict(parse_qsl(header_part)) if header_part else {}
        headers.setdefault("User-Agent", "Kodi")
        headers["Range"] = "bytes=0-0"
        import requests

        response = requests.get(url, headers=headers, stream=True, timeout=_PROBE_TIMEOUT, allow_redirects=True)
        try:
            status = response.status_code
        finally:
            response.close()
        if status >= 400 and status != 416:
            return "failed", f"HTTP {status}"
        return "ok", f"HTTP {status}"
    except Exception as exc:  # pylint: disable=broad-except
        name = type(exc).__name__
        if "Timeout" in name:
            return "unknown", name
        if "Connection" in name or "Reset" in name or "Closed" in name or "Aborted" in name:
            return "failed", name
        return "unknown", name


# A stalled stream behind a held pause (3.9.116). While the video is paused (a4kSubtitles' search dialog holds it for the
# whole monitor window when it finds nothing), getTime() cannot move, so the stall timer is held and a source that opened and
# then stopped delivering data was committed. Kodi keeps filling its read-ahead cache while the video is paused; its fill level
# (Player.CacheLevel, whole percent of filecache.memorysize) keeps rising on a live stream and stays put on a dead one, and sits
# at exactly 100 once the cache is full (measured on a real 2.2 GB file: healthy = no flat stretch over about 2 s, stalled after
# 100 MB = flat for the rest of the pause, full cache = 100). A pause the user caused (any key / remote / mouse since the hold
# began) is never judged, and anything that cannot be read leaves the old behaviour (hold) untouched.
_CACHE_FULL_LEVEL = 95  # a level at or above this is a full cache, not a stalled stream
_CACHE_WHOLE_PROGRESS = 90  # Player.ProgressCache at or above this: the whole video is already cached (a short file)
_PAUSE_RECENT_INPUT = 3.0  # seconds: input this close to the first hold reading is taken as the user pausing


class _CacheStallWatch:
    """Judge a held pause: True once Kodi's cache stopped filling for flat_seconds while it is neither full nor complete."""

    def __init__(self, flat_seconds):
        self.flat_seconds = flat_seconds
        self.reset()

    def reset(self):
        self._level = None
        self._level_at = 0.0
        self._grew = False
        self._idle = None
        self._idle_at = 0.0
        self._input = False

    def update(self, now, level, progress, idle):
        """One reading: cache level and progress (None = unreadable) and the user idle seconds (None = unreadable)."""
        if idle is None:
            self._input = True  # cannot tell whether the user paused: leave the pause alone
        else:
            if self._idle is None:
                if idle < _PAUSE_RECENT_INPUT:
                    self._input = True  # the user touched something moments before the hold began: their pause
            elif idle < self._idle + (now - self._idle_at) - _IDLE_TOLERANCE:
                self._input = True
            self._idle, self._idle_at = idle, now
        if self._input or level is None or progress is None:
            return False
        if self._level is None or level != self._level:
            if self._level is not None and level > self._level:
                self._grew = True
            self._level, self._level_at = level, now
            return False
        return (
            self._grew
            and now - self._level_at >= self.flat_seconds
            and level < _CACHE_FULL_LEVEL
            and progress < _CACHE_WHOLE_PROGRESS
        )


class PlaybackWatchdog(xbmc.Player):
    """Stall-detecting playback manager.

    Resolves sources one at a time, plays each via xbmc.Player().play(),
    monitors getTime() for progress, and automatically retries the next
    source if the stream stalls.
    """

    def __init__(self):
        super().__init__()
        self.stall_timeout = g.get_int_setting("watchdog.stallTimeout", 10)
        self.monitor_window = g.get_int_setting("watchdog.monitorWindow", 30)
        self.max_retries = g.get_int_setting("watchdog.maxRetries", 3)

        # Playback state — set by Kodi callbacks
        self._playback_error = False
        self._playback_stopped = False
        self._user_stopped = False  # True only when user explicitly stopped (backspace/stop)
        self.user_stopped_early = False  # Public: caller can check if user stopped during retry
        # Public: what the router hands to the next watchdog when a rescrape follows (see attempt_playback's carry)
        self.attempts_made = 0
        self.failed_titles = set()
        self._monitor = xbmc.Monitor()

        # AV change tracking — fires when subtitle/audio streams change
        self._last_av_change = 0.0
        self._attempt_started = time.time()  # for the callback log lines
        self._resume_seek_at = None  # time.time() of the first resume seek of the current attempt

        # Detect subtitle services so we can wait for them to finish.
        # Gated on user setting — some setups have subtitle services that
        # are fast enough to not need the extended settle window.
        self._subtitle_detection = (
            g.get_bool_setting("watchdog.subtitleDetection", False)
            and self._detect_subtitle_service()
        )

    @staticmethod
    def _detect_subtitle_service():
        """Check if a subtitle service addon is installed and enabled."""
        for addon_id in (
            "service.subtitles.a4ksubtitles",
            "service.subtitles.opensubtitles",
            "service.subtitles.subscene",
        ):
            try:
                xbmcaddon.Addon(addon_id)
                g.log(f"Watchdog: Detected subtitle service '{addon_id}'", "info")
                return True
            except Exception:
                continue
        return False

    # ── Kodi player callbacks ──────────────────────────────────────────────

    def onAVChange(self):
        self._last_av_change = time.time()

    def onPlayBackError(self):
        self._playback_error = True
        self._log_callback("onPlayBackError")

    def onPlayBackStopped(self):
        self._playback_stopped = True
        self._user_stopped = True  # Explicit stop — backspace, stop button, or OS signal
        self._log_callback("onPlayBackStopped")

    def onPlayBackEnded(self):
        self._playback_stopped = True
        self._log_callback("onPlayBackEnded")

    def _log_callback(self, name):
        """Record which callback set a flag and whether the player is playing at that moment (see _playback_ended)."""
        try:
            playing = self.isPlaying()
        except Exception:
            playing = "?"
        g.log(
            f"Watchdog: callback {name} (isPlaying={playing}, "
            f"{time.time() - self._attempt_started:.1f}s after the attempt began)",
            "debug",
        )

    def _playback_ended(self, where):
        """True when playback really ended or failed.

        onPlayBackStopped / onPlayBackEnded / onPlayBackError are only delivered when this thread yields to Kodi
        (waitForAbort), which can be seconds after the event. A callback for an EARLIER playback (the episode that just
        finished, or the source _safe_stop() has just stopped) then lands inside the next attempt, after _try_play
        cleared the flags, and made a stream that was opening fine look dead: it was blacklisted and the next source
        interrupted it (a ~60 s hang between episodes), or a stop callback ended the retry loop as "user stopped".
        If a flag is set but the player is playing, the callback is stale: clear all three flags and carry on.
        A real stop or error leaves the player idle, so it is still reported.
        """
        if not (self._playback_error or self._playback_stopped):
            return False
        try:
            playing = self.isPlaying()
        except Exception:
            playing = False
        if not playing:
            return True
        g.log(
            f"Watchdog: ignoring a stale playback callback during {where} "
            f"(error={self._playback_error}, stopped={self._playback_stopped}, user_stopped={self._user_stopped}) "
            f"- the player is playing",
            "info",
        )
        self._playback_error = False
        self._playback_stopped = False
        self._user_stopped = False
        return False

    def _track_position(self):
        """Remember the last position and total time Kodi reported (read only while the player is playing)."""
        try:
            position = float(self.getTime())
            total = float(self.getTotalTime())
        except Exception:
            return
        self._last_position, self._last_total = position, total

    def _ended_at_stream_end(self):
        """True when the stream finished by itself: Kodi reported the natural end (onPlayBackEnded, which sets neither the error nor
        the user-stop flag) and the last position was within _END_TOLERANCE of the total time. Such a stream (a clip shorter than the
        monitor window, or a seek to the end inside it) is not a failed source: blacklisting it and playing the next source
        replays the item the viewer just watched (E7)."""
        total = getattr(self, "_last_total", 0.0)
        return bool(
            self._playback_stopped
            and not self._playback_error
            and not self._user_stopped
            and total > 0
            and getattr(self, "_last_position", 0.0) >= total - _END_TOLERANCE
        )

    # ── Main entry point ───────────────────────────────────────────────────

    def attempt_playback(self, sources, item_information, pack_select=False, resume_time=None, carry=None):
        """Resolve sources with playback stall detection.

        Iterates through sources, resolves each, verifies content, plays it,
        and monitors for stalls. On stall, stops and tries the next source.

        Args:
            sources: Sorted list of source dicts
            item_information: Metadata on the item to play
            pack_select: Force manual file selection if True
            resume_time: Seek position in seconds (from bookmark) or None
            carry: (attempts used, failed release titles) from the pass before a rescrape, or None. The rescrape
                continues that pass: the attempt counter keeps counting and a release that already failed is not
                tried again. A rescrape can only start while attempts < max_retries (the limit is checked at the
                top of each source, before either rescrape break), so the new pass always has at least one try.

        Returns:
            (stream_link, release_title) on success — stream is already playing
            (None, None) if all sources failed or retries exhausted
        """
        self._resume_time = float(resume_time) if resume_time else None
        self._playback_marker = ""
        resolver = Resolver()
        verifier = ContentVerifier(item_information, sources)
        attempts = carry[0] if carry else 0
        cloud_miss_count = 0
        cloud_miss_threshold = g.get_int_setting("general.cloudMissThreshold", 3)
        failed_titles = set(carry[1]) if carry else set()  # Skip same release on different debrid after failure
        self.attempts_made = attempts
        self.failed_titles = failed_titles

        g.log(
            f"Watchdog: Starting with {len(sources)} sources, "
            f"max {self.max_retries} retries, "
            f"stall timeout {self.stall_timeout}s, "
            f"monitor window {self.monitor_window}s",
            "info",
        )
        if carry:
            g.log(
                f"Watchdog: Continuing after a rescrape - {attempts} of {self.max_retries} attempts already used, "
                f"skipping {len(failed_titles)} release(s) that already failed",
                "info",
            )

        for source in sources:
            if attempts >= self.max_retries:
                g.log(
                    f"Watchdog: Max retries ({self.max_retries}) reached, giving up",
                    "warning",
                )
                break

            if self._monitor.abortRequested():
                break

            # Skip sources with same release title as a previously failed
            # attempt — if a file stalled on Premiumize, the same file on
            # Real-Debrid will likely have the same problem.
            src_title = source.get("release_title", "")
            if src_title and src_title in failed_titles:
                continue

            # Same torrent already failed content verification on another debrid service
            if verifier.already_mismatched(source):
                continue

            # ── Resolve ────────────────────────────────────────────────────
            # A record left by an earlier source that failed must not outlive it: the next playback claims
            # whatever the slot holds when it starts, even if its own source stores nothing (e.g. Usenet).
            self._discard_pending_cleanup("before resolving the next source")
            try:
                stream_link, release_title = resolver.resolve_single_source(
                    source, item_information, pack_select
                )
            except Exception:
                g.log_stacktrace()
                continue

            # ── Cloud miss detection ───────────────────────────────────────
            if stream_link is None and source.get("_cloud_miss"):
                cloud_miss_count += 1
                g.log(
                    f"Watchdog: Cloud miss {cloud_miss_count}/{cloud_miss_threshold}: "
                    f"{source.get('debrid_provider', '?')} no longer has "
                    f"{source.get('release_title', 'N/A')}",
                    "warning",
                )
                if cloud_miss_count >= cloud_miss_threshold:
                    g.set_runtime_setting("cloud_miss_rescrape", "true")
                    break
                continue

            if not stream_link:
                continue

            # ── Content verification ───────────────────────────────────────
            if not verifier.verify(source, release_title):
                g.log(
                    f"Watchdog: Content mismatch — "
                    f"'{release_title or source.get('release_title', 'N/A')}'",
                    "warning",
                )
                stream_link = None
                if verifier.should_rescrape():
                    g.set_runtime_setting("content_verify_rescrape", "true")
                    break
                continue

            # ── Play and monitor ───────────────────────────────────────────
            attempts += 1
            self.attempts_made = attempts
            g.log(
                f"Watchdog: Attempt {attempts}/{self.max_retries} — "
                f"'{source.get('release_title', 'N/A')}' "
                f"[{source.get('quality', '?')}] "
                f"via {source.get('debrid_provider', source.get('provider', '?'))}",
                "info",
            )

            result = self._try_play(stream_link, source, item_information)

            if result == "success":
                g.log(
                    f"Watchdog: Playback committed after {attempts} attempt(s)",
                    "info",
                )
                return stream_link, release_title

            if result == "ended":
                g.log(
                    f"Watchdog: the stream reached its end {time.time() - self._attempt_started:.1f}s into the attempt "
                    f"(position {self._last_position:.1f}s of {self._last_total:.1f}s) - a natural end, not a failed source; "
                    f"not blacklisting it and not trying another source",
                    "info",
                )
                self.user_stopped_early = True  # same quiet exit as a stop: nothing is left to play
                g.clear_playback_marker(self._playback_marker)
                self._discard_pending_cleanup("stream ended inside the monitor window")
                return None, None

            if result == "stall":
                failed_titles.add(src_title)
                g.log(
                    f"Watchdog: Stall on attempt {attempts}, "
                    f"blacklisting '{src_title}', cycling to next source",
                    "warning",
                )
                xbmcgui.Dialog().notification(
                    g.ADDON_NAME,
                    "Stream stalled — trying next source...",
                    time=3000,
                )
                continue

            # "error" — playback never started OR user explicitly stopped.
            if self._user_stopped:
                self.user_stopped_early = True
                g.log("Watchdog: User stopped playback — exiting retry loop", "info")
                g.clear_playback_marker(self._playback_marker)
                self._discard_pending_cleanup("user stopped")
                return None, None

            failed_titles.add(src_title)
            g.log(
                f"Watchdog: Playback error on attempt {attempts}, "
                f"blacklisting '{src_title}'",
                "warning",
            )
            continue

        g.clear_playback_marker(self._playback_marker)
        self._discard_pending_cleanup("no source played")
        return None, None

    # ── Playback attempt ───────────────────────────────────────────────────

    def _try_play(self, stream_link, source, item_information):
        """Play a stream and monitor for stalls.

        Returns:
            "success" — stream is playing and progressing normally
            "stall"   — stream started but getTime() stopped advancing
            "ended"   — the stream reached its end inside the settle / monitor window (natural end)
            "error"   — stream never started or Kodi reported an error
        """
        # Let Kodi deliver callbacks that are already waiting (earlier playback, the previous attempt's stop) before the
        # flags are cleared, so they cannot land inside this attempt. _playback_ended() catches any that still do.
        self._monitor.waitForAbort(0.2)

        # Reset state
        self._playback_error = False
        self._playback_stopped = False
        self._user_stopped = False
        self._last_av_change = time.time()
        self._attempt_started = time.time()
        self._resume_seek_at = None
        self._last_position = 0.0
        self._last_total = 0.0

        # Build ListItem
        listitem = self._build_listitem(stream_link, source, item_information)

        # Add to Kodi playlist so SerenPlayer's smart playlist logic
        # sees size==1 and can build the binge-watch queue
        g.PLAYLIST.clear()
        g.PLAYLIST.add(stream_link, listitem)

        # Tell other add-ons this is Seren's play before Kodi starts it (it shows no plugin origin for a stream URL)
        self._playback_marker = g.set_playback_marker(item_information)

        # Play from playlist via xbmc.Player (NOT setResolvedUrl)
        self.play(g.PLAYLIST)
        play_at = time.time()
        self._idle_last = None
        self._idle_last_at = None
        self._idle_input_seen = False
        self._sample_idle()
        self._idle_first = self._idle_last
        g.log(f"Watchdog: play() sent (user idle timer {self._idle_last}s)", "info")

        # Wait for playback to actually start by polling isPlayingVideo().
        # We do NOT rely on onAVStarted callbacks — they may not route to
        # this Player instance when the plugin context has been cancelled.
        playback_started = False
        open_began = time.time()
        still_opening_logged = False
        while True:
            if self._monitor.waitForAbort(0.5):
                return "error"
            self._sample_idle()
            if self._playback_ended("start-up"):
                return self._open_phase_error(play_at, "stop/error callback", stream_link)
            if self.isPlayingVideo():
                try:
                    t = self.getTime()
                    if t >= 0:
                        playback_started = True
                        break
                except Exception:
                    pass

            waited = time.time() - open_began
            if waited < _OPEN_WAIT_BASE:
                continue
            # Past the normal wait: a stream that is still opening is waited for, never stopped or replaced mid-open
            # (a stop here, or the next source's play(), is what froze Kodi on the Shield).
            try:
                still_opening = self.isPlaying()
            except Exception:
                still_opening = False
            if not still_opening:
                # The player is idle: nothing to wait for, nothing to stop. Its stop/error callback may still be on
                # its way (Kodi's window is blocked until the open thread ends), so wait for it before deciding.
                self._await_stop_callback(_OPEN_CALLBACK_WAIT)
                break
            if waited >= _OPEN_WAIT_CAP:
                break
            if not still_opening_logged:
                still_opening_logged = True
                g.log(
                    f"Watchdog: stream still opening after {waited:.0f}s - waiting up to {_OPEN_WAIT_CAP:.0f}s "
                    f"instead of stopping it",
                    "info",
                )

        if not playback_started:
            g.log(f"Watchdog: Playback never started within {time.time() - open_began:.0f}s", "warning")
            was_playing = self.isPlaying()
            self._safe_stop()
            if was_playing:
                # The stop was Seren's own (the cap): its callback must not read as a user stop. Kodi's window can stay
                # blocked for ~20 s while the open thread ends, so wait for that callback before the next source's
                # play() goes out (replacing a half-closed player is what froze Kodi).
                self._await_stop_callback(_OPEN_CALLBACK_WAIT)
            return self._open_phase_error(play_at, "never started", stream_link)

        if still_opening_logged:
            g.log(f"Watchdog: slow stream opened after {time.time() - open_began:.1f}s", "info")
        g.log(
            f"Watchdog: Playback confirmed at {self.getTime():.1f}s",
            "info",
        )

        # Close all Seren overlay windows (persistent_background, get_sources)
        # so the video is visible during the monitoring window
        g.close_busy_dialog()
        g.close_all_dialogs()

        # Seek to resume position BEFORE monitoring starts — so the user
        # sees the correct position immediately, not 30s from the beginning.
        if self._resume_time and self._resume_time > 0:
            try:
                self.seekTime(self._resume_time)
                self._resume_seek_at = time.time()
                g.log(
                    f"Watchdog: Seeked to resume position {self._resume_time:.0f}s",
                    "info",
                )
            except Exception as e:
                g.log(f"Watchdog: Seek failed: {e}", "warning")

        # Wait for playback to settle after seek + subtitle loading before
        # starting the stall monitor. This prevents false stall detection
        # from seek rebuffering or a4kSubtitles loading external subs.
        if not self._wait_for_settle():
            if self._ended_at_stream_end():
                return "ended"
            self._safe_stop()
            return "error"

        # Playback settled — monitor for stalls during the monitoring window
        return self._monitor_playback()

    def _wait_for_settle(self):
        """Wait for playback to stabilize after seek and subtitle loading.

        Two conditions must be met before the stall monitor starts:
        1. Seek settled (if a seek was performed): getTime() reports a
           position within 30s of the resume target AND is advancing.
           This prevents false settle when getTime() returns 0.0 before
           Kodi has processed the seek.
        2. AV settled: no onAVChange callbacks for a quiet period.
           When a subtitle service (e.g. a4kSubtitles) selects or downloads
           subtitles, Kodi fires onAVChange. We wait for these to stop
           before starting the stall monitor. If a subtitle service is
           detected, we use a longer quiet period and timeout to give it
           time to finish searching providers and downloading.

        Returns True when settled, False if playback died during settle.
        """
        # Subtitle services like a4kSubtitles can take 20-60s to search
        # multiple providers and download. Use longer timeouts when detected.
        if self._subtitle_detection:
            settle_timeout = 30
            av_quiet_threshold = 5.0
        else:
            settle_timeout = 15
            av_quiet_threshold = 2.0

        settle_start = time.time()
        last_time = -1.0
        seek_settled = not bool(self._resume_time)  # Skip seek check if no resume
        seek_target = float(self._resume_time) if self._resume_time else 0.0
        seek_position_ok = False  # getTime() is near the resume target

        # Resume seek retry state (see _RESUME_RETRY_*). All locals, so every source attempt starts clean.
        first_seek_at = self._resume_seek_at or settle_start
        last_seek_at = first_seek_at
        resume_retries = 0
        advancing_polls = 0
        previous_position = None

        g.log(
            f"Watchdog: Waiting for playback to settle... "
            f"(subtitle_detection={self._subtitle_detection}, "
            f"av_quiet={av_quiet_threshold}s, timeout={settle_timeout}s)",
            "info",
        )

        while time.time() - settle_start < settle_timeout:
            if self._monitor.waitForAbort(0.5):
                return False
            if self._playback_ended("settle"):
                return False
            if not self.isPlayingVideo():
                self._await_stop_callback()
                return False
            self._track_position()

            # ── Check seek settled ─────────────────────────────────────
            try:
                current = self.getTime()
            except Exception:
                continue

            # Count consecutive polls in which the position moved forward: a position stuck at 0 means the stream is
            # still opening or a seek is still being carried out, and a second seek then would only stack on the first.
            if previous_position is not None and current > previous_position + 0.1:
                advancing_polls += 1
            else:
                advancing_polls = 0
            previous_position = current

            if not seek_settled:
                # Resume seek retry. Kodi can drop the seek sent right after "Playback confirmed" (the player's own
                # start-of-file seek then runs after it). Send it again when ALL of these hold: the position is
                # advancing, it is well below the target, and it is no further along than playback running from the
                # start could be (so a position the user chose with the seek bar is never pulled back), the last seek
                # is a few seconds old, and we are still early in the attempt. Capped at _RESUME_RETRY_MAX.
                now = time.time()
                looks_unresumed = (
                    current < seek_target - _RESUME_RETRY_MIN_SHORTFALL and current <= (now - first_seek_at) + 5
                )
                if (
                    resume_retries < _RESUME_RETRY_MAX
                    and advancing_polls >= _RESUME_RETRY_ADVANCING_POLLS
                    and now - last_seek_at >= _RESUME_RETRY_GAP
                    and now - first_seek_at <= _RESUME_RETRY_WINDOW
                    and looks_unresumed
                ):
                    resume_retries += 1
                    last_seek_at = now
                    advancing_polls = 0
                    try:
                        self.seekTime(seek_target)
                        g.log(
                            f"Watchdog: Resume position not reached (playing at {current:.1f}s, target {seek_target:.0f}s) "
                            f"- seeking again ({resume_retries}/{_RESUME_RETRY_MAX})",
                            "info",
                        )
                    except Exception as e:
                        g.log(f"Watchdog: Resume seek retry failed: {e}", "warning")
                    seek_position_ok = False
                    continue

                # First check: is getTime() reporting a position near the
                # seek target? Before Kodi processes the seek, getTime()
                # returns 0.0 or the pre-seek position.
                # (A position that is "within 30s" only because the target itself is early and playback is still
                # running from the start is not a landed seek.)
                if not seek_position_ok:
                    if abs(current - seek_target) <= 30 and not looks_unresumed:
                        seek_position_ok = True
                        last_time = current
                    continue  # Keep waiting for position to reach target

                # Second check: is getTime() advancing from the new position?
                if current > last_time + 0.3:
                    seek_settled = True
                    g.log(
                        f"Watchdog: Seek settled at {current:.1f}s "
                        f"(target was {seek_target:.0f}s)",
                        "info",
                    )
                    if resume_retries:
                        g.log(
                            f"Watchdog: Resume position reached after {resume_retries} retr"
                            f"{'y' if resume_retries == 1 else 'ies'} (first seek was not carried out)",
                            "info",
                        )
                last_time = current
                continue  # Don't check AV until seek is done

            # ── Check AV changes have quieted ──────────────────────────
            # onAVChange fires whenever subtitle/audio streams are added,
            # removed, or selected — by Kodi itself or subtitle services.
            time_since_av = time.time() - self._last_av_change
            if time_since_av >= av_quiet_threshold:
                g.log(
                    f"Watchdog: Settled — seek done, no AV changes "
                    f"for {time_since_av:.1f}s "
                    f"(total settle: {time.time() - settle_start:.1f}s)",
                    "info",
                )
                return True

            last_time = current

        # Timed out — proceed anyway (better than blocking forever)
        if not seek_settled:
            try:
                position_now = f"{self.getTime():.1f}s"
            except Exception:
                position_now = "unknown"
            g.log(
                f"Watchdog: Resume position {seek_target:.0f}s was not reached (playing at {position_now} after "
                f"{resume_retries} retr{'y' if resume_retries == 1 else 'ies'}) - playback continues from there",
                "warning",
            )
        g.log(
            f"Watchdog: Settle timeout ({settle_timeout}s) — proceeding with monitoring",
            "warning",
        )
        return True

    def _monitor_playback(self):
        """Watch getTime() for stalls during the monitoring window.

        Returns "success" if playback progresses normally through the
        monitoring window, "stall" if getTime() doesn't advance for
        stall_timeout seconds, "error" if playback stops unexpectedly.

        AV changes from subtitle services (stream selection, subtitle
        download) can cause brief rebuffers. If an onAVChange fired
        recently, we forgive the stall rather than cycling sources.
        """
        monitor_start = time.time()
        last_time = -1.0
        stall_start = None
        paused_logged = False
        cache_watch = _CacheStallWatch(self.stall_timeout)

        while not self._monitor.abortRequested():
            if self._playback_ended("monitor"):
                return "ended" if self._ended_at_stream_end() else "error"

            if not self.isPlayingVideo():
                self._await_stop_callback()
                return "ended" if self._ended_at_stream_end() else "error"

            self._track_position()

            # Check if we've passed the monitoring window
            elapsed = time.time() - monitor_start
            if elapsed >= self.monitor_window:
                return "success"

            # Check playback progress
            try:
                current = self.getTime()
            except Exception:
                self._await_stop_callback()
                return "error"

            if current > last_time + 0.5:
                # Time is advancing — playback is healthy
                last_time = current
                stall_start = None
                cache_watch.reset()
            else:
                # Time not advancing because the user paused the video: that is
                # not a dead stream, so don't run the stall timer while paused.
                if self._is_user_paused():
                    if not paused_logged:
                        g.log("Watchdog: Paused by user — stall timer held", "info")
                        paused_logged = True
                    stall_start = None
                    # A pause nobody asked for (a subtitle dialog holding the video) must not hide a dead stream: Kodi keeps
                    # filling its cache while paused, so a cache that stops filling below "full" is a stall.
                    level, progress = self._cache_reading()
                    if cache_watch.update(time.time(), level, progress, self._idle_seconds()):
                        g.log(
                            f"Watchdog: Paused, and Kodi's cache stopped filling {self.stall_timeout}s ago at {level}% "
                            f"(video {progress}% cached) — the stream has stalled",
                            "warning",
                        )
                        self._safe_stop()
                        return "stall"
                    if self._monitor.waitForAbort(0.5):
                        return "error"
                    continue
                paused_logged = False
                cache_watch.reset()

                # Time not advancing — but if subtitle detection is on and
                # a subtitle service just triggered an AV change, forgive
                # the brief pause rather than counting it as a stall.
                if self._subtitle_detection:
                    time_since_av = time.time() - self._last_av_change
                    if time_since_av < 3.0:
                        stall_start = None
                        if self._monitor.waitForAbort(0.5):
                            return "error"
                        continue

                if stall_start is None:
                    stall_start = time.time()
                elif time.time() - stall_start >= self.stall_timeout:
                    # Stalled for too long
                    g.log(
                        f"Watchdog: Stream stalled for {self.stall_timeout}s "
                        f"at position {current:.1f}s",
                        "warning",
                    )
                    self._safe_stop()
                    return "stall"

            if self._monitor.waitForAbort(0.5):
                return "error"

        return "error"

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _discard_pending_cleanup(reason):
        """Drop the smart-delete record of a source that did not play (see debrid_utils.discard_pending_cleanup)."""
        try:
            from resources.lib.debrid.debrid_utils import discard_pending_cleanup

            discard_pending_cleanup(reason)
        except Exception as e:
            g.log(f"Watchdog: could not discard the pending cleanup record: {e}", "warning")

    @staticmethod
    def _idle_seconds():
        """Seconds since the last user input (key, remote, mouse), or None when Kodi cannot tell."""
        try:
            return float(xbmc.getGlobalIdleTime())
        except Exception:
            return None

    def _sample_idle(self):
        """Take one reading of the user idle timer and note whether it was reset (= user input) since the last one.

        The timer only ever counts up unless the user presses a key, moves the mouse or uses the remote (Kodi also
        resets it when play() starts, which is why the first reading is the baseline). A reading that is lower than
        the last one plus the time passed means input. An unreadable timer counts as input (the old behaviour).
        """
        idle = self._idle_seconds()
        now = time.time()
        if idle is None:
            self._idle_input_seen = True
            return
        if self._idle_last is not None and idle < self._idle_last + (now - self._idle_last_at) - _IDLE_TOLERANCE:
            self._idle_input_seen = True
        self._idle_last = idle
        self._idle_last_at = now

    def _open_phase_error(self, play_at, where, stream_link=None):
        """End of the start-up phase without a started video: always "error"; decides whether it was a user stop.

        Kodi reports a failed open as onPlayBackStopped, which sets _user_stopped and makes attempt_playback quit as
        "User stopped playback". When the idle timer was never reset while the stream was opening (and either at least
        _NO_INPUT_MIN_AGE s have passed since play() or the timer was already high at play()), nobody pressed anything,
        so the stop is the source failing:
        clear the stop flags so the retry loop blacklists the source and tries the next one. Any input, or an idle
        timer that cannot be read, leaves the flags alone (a user stop, as before).
        A stop sooner than _NO_INPUT_MIN_AGE after play() with no reliable idle reading (3.9.108) cannot be told apart by
        the timer, so the link is probed (_probe_stream): an HTTP error or connection failure = the source failed.
        """
        if not self._user_stopped:
            return "error"
        self._sample_idle()  # catches a key press during the callback wait too
        elapsed = time.time() - play_at
        # A key press is only visible as a reset when the timer had room to drop: right after play() that needs it to
        # have been >= _IDLE_RELIABLE_MIN already (it is when the user was waiting, e.g. for a scrape or an auto-next).
        reliable_early = self._idle_first is not None and self._idle_first >= _IDLE_RELIABLE_MIN
        no_input = (not self._idle_input_seen) and (elapsed >= _NO_INPUT_MIN_AGE or reliable_early)
        facts = f"{where}, {elapsed:.1f}s after play(), idle timer now {self._idle_last}s, input seen: {self._idle_input_seen}"
        if no_input:
            g.log(f"Watchdog: stream failed to open (Kodi stopped it with no user input: {facts}) - next source", "info")
            self._user_stopped = False
            self._playback_stopped = False
        elif not self._idle_input_seen and stream_link:
            # Too early for the idle timer to tell (it reads ~0 at the first play()): ask the host instead.
            verdict, detail = _probe_stream(stream_link)
            if verdict == "failed":
                g.log(
                    f"Watchdog: stream failed to open (Kodi stopped it {elapsed:.1f}s after play() and the source "
                    f"does not answer: {detail}; {where}) - next source",
                    "info",
                )
                self._user_stopped = False
                self._playback_stopped = False
            else:
                g.log(f"Watchdog: stop during stream open treated as a user stop ({facts}; probe: {verdict} {detail})", "info")
        else:
            g.log(f"Watchdog: stop during stream open treated as a user stop ({facts})", "info")
        return "error"

    def _await_stop_callback(self, timeout=2.0):
        """The player went idle: give Kodi's callbacks a moment to say why.

        isPlayingVideo() turns False as soon as Kodi stops the player, but onPlayBackStopped / onPlayBackError
        only run once this thread yields to Kodi (waitForAbort), up to about a second later. Deciding "error"
        before that left _user_stopped False, so a stop pressed inside the monitor window was blacklisted as a
        failed stream and the next source started. Yield here until a callback has set its flag.
        """
        deadline = time.time() + timeout
        while not (self._playback_error or self._playback_stopped):
            if time.time() >= deadline or self._monitor.waitForAbort(0.1):
                break

    @staticmethod
    def _cache_reading():
        """(Player.CacheLevel, Player.ProgressCache) as whole percents; None for a value Kodi does not give."""

        def read(label):
            try:
                return int(float(xbmc.getInfoLabel(label)))
            except (TypeError, ValueError):
                return None

        return read("Player.CacheLevel"), read("Player.ProgressCache")

    @staticmethod
    def _is_user_paused():
        """True while the video is paused, not while Kodi is rebuffering a stalled stream."""
        return xbmc.getCondVisibility("Player.Paused") and not xbmc.getCondVisibility(
            "Player.Caching"
        )

    def _safe_stop(self):
        """Stop playback, suppressing errors if already stopped."""
        try:
            if self.isPlaying():
                self.stop()
            # Yield to Kodi (not xbmc.sleep) so the stop callback is delivered now, not inside the next attempt
            self._monitor.waitForAbort(0.5)
        except Exception:
            pass

    @staticmethod
    def _build_listitem(stream_link, source, item_information):
        """Build a Kodi ListItem with metadata for OSD display.

        Handles both direct URL strings and adaptive source dicts.
        """
        info = copy.deepcopy(item_information.get("info", {}))
        g.clean_info_keys(info)
        g.convert_info_dates(info)

        if isinstance(stream_link, dict) and stream_link.get("type") == "adaptive":
            if g.ADDON_USERDATA_PATH not in sys.path:
                sys.path.append(g.ADDON_USERDATA_PATH)
            provider = stream_link["provider_imports"]
            provider_module = importlib.import_module(f"{provider[0]}.{provider[1]}")
            if not hasattr(provider_module, "get_listitem") and hasattr(
                provider_module, "sources"
            ):
                provider_module = provider_module.sources()
            item = provider_module.get_listitem(stream_link)
            g.set_video_info(item, info)
        else:
            item = xbmcgui.ListItem(path=stream_link)
            info["FileNameAndPath"] = stream_link
            g.set_video_info(item, info)
            item.setProperty("IsPlayable", "true")

        art = item_information.get("art", {})
        item.setArt(art if isinstance(art, dict) else {})
        cast = item_information.get("cast", [])
        g.set_video_cast(item, cast if isinstance(cast, list) else [])
        # Same ids as add_directory_item gives the list row: the show's ids for episodes and seasons. Read from the
        # uncleaned info, since clean_info_keys drops every *_id key.
        raw_info = item_information.get("info", {})
        id_prefix = "tvshow." if raw_info.get("mediatype") in g._EPISODE_SEASON_TYPES else ""
        g.set_video_unique_ids(
            item,
            {
                unique_id_key: raw_info[f"{id_prefix}{id_key}"]
                for id_key, unique_id_key in g._ID_KEYS.items()
                if raw_info.get(f"{id_prefix}{id_key}")
            },
        )
        return item
