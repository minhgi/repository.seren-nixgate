import threading
import time
from functools import cached_property

import xbmc
import xbmcaddon
import xbmcgui

from resources.lib.common import tools
from resources.lib.modules.exceptions import LockTimeout
from resources.lib.modules.global_lock import GlobalLock
from resources.lib.modules.globals import g

# WeTrakr hands its app key (client ID) to the developer of each app. This one belongs to Minh's WeTrakr developer app (the same one
# TMDb Helper uses); WeTrakr's developer confirmed (2026-11-22) that it may be embedded in the add-on. It is a public client ID: the sign-in
# is the device flow, which needs the client ID only, and a client secret is never used or stored. The setting wetrakr.clientid
# (expert level) overrides it for someone with their own WeTrakr developer app.
WETRAKR_CLIENT_ID = "90630f7b0f1ffa5d0b8079a22ebe2d6c"

BASE_URL = "https://api.wetrakr.com"
API_VERSION = "1"
REQUEST_TIMEOUT = 10  # seconds; scrobbles are posted from Kodi's player callback thread, so a dead connection must not hold it long
REFRESH_MARGIN = 3600  # refresh the access token (valid 7 days) once less than an hour is left
LIST_PAGE_LIMIT = 5000  # rows per compact page (WeTrakr's maximum)
LIST_MAX_PAGES = 40  # backstop for the cursor loop
JOURNAL_PAGE_LIMIT = 1000  # entries per journal page (WeTrakr's maximum)
JOURNAL_MAX_PAGES = 3  # a journal longer than this is not worth reading: the sync falls back to reading the lists in full
DEFAULT_BACKOFF = 60  # seconds without requests after HTTP 429 when the answer carries no Retry-After
SIGNED_OUT_NOTICE = "Seren.WeTrakr.SignedOutNotice"  # runtime setting: the "authorize again" notice is shown once per Kodi session

# Seren's own settings of this service
AUTH_KEY = "wetrakr.auth"
REFRESH_KEY = "wetrakr.refresh"
EXPIRES_KEY = "wetrakr.expires"
STATUS_KEY = "wetrakr.status"  # the signed-in marker the settings rows gate on (hidden; Account Manager writes it too)
USERID_KEY = "wetrakr.userid"  # the WeTrakr user id (from the access token) the stored username belongs to
USERNAME_KEY = "wetrakr.username"  # shown in the settings (GET /account is closed to third-party apps: the name comes from the public profile, see get_username)

_BLOCKED_UNTIL = [0.0]  # time.monotonic() until which an HTTP 429 asks for silence (shared by every API object of this process)


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def official_wetrakr_scrobbles():
    """Why the official script.wetrakr add-on already reports the playing item to WeTrakr ('' when it does not). That add-on scrobbles
    every play, plugin:// streams included, unless its scrobble_library_only is on (then only Kodi library files). A second report from
    Seren would reach WeTrakr twice, from two clients. Not covered when it is missing, disabled, signed out, has its tokens rejected
    (auth_invalid) or has both of its scrobbling switches off."""
    try:
        if not xbmc.getCondVisibility("System.AddonIsEnabled(script.wetrakr)"):
            return ""
        official = xbmcaddon.Addon("script.wetrakr")
        if not official.getSetting("api_token") or official.getSetting("auth_invalid") == "true":
            return ""
        if official.getSetting("track_watched") == "false" and official.getSetting("track_playing") == "false":
            return ""
        if official.getSetting("scrobble_library_only") != "true":
            return "script.wetrakr scrobbles every play"
        library_item = _to_int(xbmc.getInfoLabel("VideoPlayer.DBID")) > 0
        return "script.wetrakr scrobbles Kodi library files" if library_item else ""
    except Exception as e:
        g.log(f"WeTrakr: script.wetrakr check failed - {type(e).__name__}", "warning")
        return ""


class WeTrakrAPI:
    """
    WeTrakr REST client (https://api.wetrakr.com). Written without the shared Seren API helpers on purpose: WeTrakr's 4xx answers carry
    what the add-on needs (404 title not found, 401 token rejected) and nothing here ever logs a token, the client ID or an OAuth
    body - only method, path and status code.

    WeTrakr rotates the refresh token on every refresh, so the stored login is read again straight from Kodi before every use (the
    service, the player and the settings buttons are separate processes) and the refresh itself runs under a global lock.
    """

    def __init__(self):
        pass

    @cached_property
    def session(self):
        import requests

        return requests.Session()

    # region stored login
    @staticmethod
    def _stored_login():
        """The WeTrakr login saved in Kodi right now - (access token, refresh token, expiry) - read straight from the add-on, not
        through Seren's settings cache, so a refresh made by another process is never missed."""
        addon = xbmcaddon.Addon("plugin.video.seren")
        return (
            (addon.getSetting(AUTH_KEY) or "").strip(),
            (addon.getSetting(REFRESH_KEY) or "").strip(),
            _to_int(addon.getSetting(EXPIRES_KEY)),
        )

    @property
    def client_id(self):
        """The app key sent with every request: the one in the settings (an override for someone with their own WeTrakr developer
        app), otherwise Seren's own, built in."""
        return (g.get_setting("wetrakr.clientid", "") or "").strip() or WETRAKR_CLIENT_ID

    @property
    def access_token(self):
        return self._stored_login()[0]

    @property
    def refresh_token(self):
        return self._stored_login()[1]

    @property
    def is_authorized(self):
        access, refresh, _ = self._stored_login()
        return bool(access or refresh)

    def _expiring(self):
        """True when the access token is known to expire within REFRESH_MARGIN seconds (an unknown expiry counts as fine)"""
        expires = self._stored_login()[2]
        return bool(expires) and expires - time.time() < REFRESH_MARGIN

    def _save_tokens(self, data):
        """Saves the tokens of a 200 answer from /oauth/device/token or /oauth/token/refresh. The refresh answer repeats the new
        refresh token under new_refresh_token as well; either carries the same value."""
        access = f'{data.get("access_token") or ""}'
        refresh = f'{data.get("refresh_token") or data.get("new_refresh_token") or ""}'
        if not access or not refresh:
            return False
        g.set_setting(AUTH_KEY, access)
        g.set_setting(REFRESH_KEY, refresh)
        g.set_setting(EXPIRES_KEY, str(int(time.time()) + _to_int(data.get("expires_in"), 604800)))
        return True

    def _clear_auth_settings(self):
        # the status row first: it decides which rows the settings show; Kodi ignores an empty write to an integer setting, so the
        # expiry is set to 0 (which _expiring reads as "unknown")
        g.clear_setting(STATUS_KEY)
        g.clear_setting(USERNAME_KEY)
        g.clear_setting(USERID_KEY)
        g.clear_setting(AUTH_KEY)
        g.clear_setting(REFRESH_KEY)
        g.set_setting(EXPIRES_KEY, "0")

    # endregion

    # region requests
    def _send(self, method, path, body=None, params=None, token=None, backoff=True, timeout=None):
        """One HTTP request. Returns the Response (any status) or None when nothing could be sent or no answer came (network failure,
        or an HTTP 429 asked for silence a moment ago). backoff=False is for the sign-in polls, where 429 means slow_down."""
        if backoff and time.monotonic() < _BLOCKED_UNTIL[0]:
            return None
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": g.USER_AGENT,
            "wetrakr-api-version": API_VERSION,
            "wetrakr-api-key": self.client_id,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self.session.request(
                method, f"{BASE_URL}{path}", json=body, params=params, headers=headers, timeout=timeout or REQUEST_TIMEOUT
            )
        except Exception as e:
            g.log(f"WeTrakr {method} {path}: request failed - {type(e).__name__}", "warning")
            return None
        g.log(f"WeTrakr {method} {path} -> {response.status_code}", "debug")
        if backoff and response.status_code == 429:
            wait = _to_int(response.headers.get("Retry-After"), DEFAULT_BACKOFF) or DEFAULT_BACKOFF
            _BLOCKED_UNTIL[0] = time.monotonic() + wait
            g.log(f"WeTrakr {method} {path}: HTTP 429, no requests for {wait}s", "warning")
        return response

    @staticmethod
    def _json(response):
        if response is None:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def user_request(self, method, path, body=None, params=None, timeout=None):
        """Request that needs the signed-in user. The access token is refreshed first when it is about to expire, and once after an
        HTTP 401. Returns the Response (the caller reads its status) or None when nothing could be sent."""
        token = self._usable_token()
        if not token:
            return None
        response = self._send(method, path, body, params, token, timeout=timeout)
        if response is not None and response.status_code == 401:
            if self._refresh(seen_access=token):
                token = self.access_token
                response = self._send(method, path, body, params, token, timeout=timeout) if token else None
        return response

    def get_json(self, path, params=None, timeout=None):
        """GET of a user endpoint that answers one JSON object (e.g. /sync/last_activities): the dict, or None when it could not be read"""
        response = self.user_request("GET", path, params=params, timeout=timeout)
        if response is None or response.status_code != 200:
            return None
        return self._json(response) or None

    def get_list(self, path, extra=None):
        """Every row of a compact cursor list: (ok, rows). ok is False, and rows empty, when any page failed, the cursor repeated or the
        page cap was hit, or the rows read do not add up to the item count WeTrakr reports: a partial list must never replace stored data.
        extra = more query values for every page (from_date: only the rows changed since then, 3.9.177)."""
        rows, seen, after, expected = [], set(), None, None
        for page in range(LIST_MAX_PAGES):
            params = {**(extra or {}), "compact": "true", "limit": LIST_PAGE_LIMIT}
            if after:
                params["after"] = after
            response = self.user_request("GET", path, params=params)
            if response is None or response.status_code != 200:
                return False, []
            try:
                data = response.json()
            except ValueError:
                return False, []
            if not isinstance(data, list):
                return False, []
            rows.extend(data)
            if page == 0:
                expected = _to_int(response.headers.get("X-Pagination-Item-Count"), None)
            after = response.headers.get("X-Pagination-Next")
            if not after:
                break
            if after in seen:
                return False, []
            seen.add(after)
        else:
            return False, []
        if expected is not None and len(rows) != expected:
            g.log(f"WeTrakr {path}: read {len(rows)} rows but the list holds {expected}, keeping the stored data", "warning")
            return False, []
        return True, rows

    def get_journal(self, from_date, category="watched"):
        """GET /sync/journal (3.9.177): what changed since from_date, oldest first, read page by page. Returns (state, entries, visible_until):
        state "ok" (entries is the complete answer; visible_until is the newest moment the journal shows, WeTrakr writes it a few seconds late, so
        the NEXT read must start from it, never from the clock), "expired" (from_date is older than the journal keeps: do a full read), "big"
        (more than JOURNAL_MAX_PAGES pages: a full read is cheaper) or "failed" (nothing could be read; keep what is stored)."""
        entries, until, expected = [], None, None
        for page in range(1, JOURNAL_MAX_PAGES + 2):
            response = self.user_request(
                "GET", "/sync/journal",
                params={"from_date": from_date, "category": category, "limit": JOURNAL_PAGE_LIMIT, "page": page},
            )
            if response is None:
                return "failed", [], None
            if response.status_code == 400 and self._json(response).get("error") == "JOURNAL_EXPIRED":
                return "expired", [], None
            if response.status_code != 200:
                return "failed", [], None
            data = self._json(response)
            rows, seen_until = data.get("journal"), data.get("visible_until")
            if not isinstance(rows, list) or not isinstance(seen_until, str) or not seen_until:
                return "failed", [], None
            until = seen_until if until is None else min(until, seen_until)  # the oldest of the answers: nothing newer is certain to be complete
            entries.extend(rows)
            if page == 1:
                expected = _to_int(response.headers.get("X-Pagination-Item-Count"), None)
            if page >= (_to_int(response.headers.get("X-Pagination-Page-Count"), 0) or 0):
                break
            if page >= JOURNAL_MAX_PAGES:
                return "big", [], None
        if expected is not None and len(entries) != expected:
            g.log(f"WeTrakr /sync/journal: read {len(entries)} entries but the journal holds {expected}, keeping the stored data", "warning")
            return "failed", [], None
        return "ok", entries, until

    def get_pages(self, path, params=None, limit=100, max_pages=10, partial_ok=False):
        """Rows of a cursor list that needs its own query (not the compact one): (ok, rows). Like get_list, ok is False when a page failed or
        the cursor repeated. The page cap is a failure too, unless partial_ok: then the rows read so far are the answer (a list sorted newest
        first, of which only the top is wanted)."""
        rows, seen, after = [], set(), None
        for _ in range(max_pages):
            query = dict(params or {})
            query["limit"] = limit
            if after:
                query["after"] = after
            response = self.user_request("GET", path, params=query)
            if response is None or response.status_code != 200:
                return False, []
            try:
                data = response.json()
            except ValueError:
                return False, []
            if not isinstance(data, list):
                return False, []
            rows.extend(data)
            after = response.headers.get("X-Pagination-Next")
            if not after:
                return True, rows
            if after in seen:
                return False, []
            seen.add(after)
        return (True, rows) if partial_ok else (False, [])

    @staticmethod
    def _forget_synced_data():
        """Nothing of one account may survive into the next: empty the local copy and reset its stamps (sign-in and logout)"""
        try:
            from resources.lib.database.wetrakr_sync import WeTrakrSyncDatabase

            WeTrakrSyncDatabase().flush_activities()
        except Exception as e:
            g.log(f"WeTrakr sync data not cleared - {type(e).__name__}", "warning")

    def scrobble(self, action, payload, params=None):
        """POST /scrobble/<start|pause|stop>"""
        return self.user_request("POST", f"/scrobble/{action}", body=payload, params=params)

    # endregion

    # region token refresh
    def _usable_token(self):
        access, refresh, _ = self._stored_login()
        if (not access or self._expiring()) and refresh:
            self._refresh(seen_access=access)
            access = self._stored_login()[0]
        return access

    def _refresh(self, seen_access):
        """Trades the stored refresh token for new tokens. seen_access is the access token the caller used (empty if it had none).
        True when a usable access token is stored afterwards. The login is only cleared when WeTrakr says the refresh token is dead
        (HTTP 401: invalid, expired, revoked, rotated more than a minute ago, or the user is disabled - the docs say it never works
        again); a 400 (the docs: no refresh token in the request), a network failure, a 429 or a 5xx keeps it for the next try."""
        try:
            # Only serialises the refreshes of the service, the player and the settings script: the login is read again inside the lock,
            # so the process that comes second finds the rotated login and sends no refresh of its own.
            with GlobalLock(self.__class__.__name__, timeout=30):
                access, refresh, _ = self._stored_login()
                if access and access != seen_access and not self._expiring():
                    g.log("WeTrakr login was renewed elsewhere - using the saved one")
                    return True
                if not refresh:
                    self._signed_out("no refresh token")
                    return False
                response = self._send("POST", "/oauth/token/refresh", {"refresh_token": refresh})
                if response is None:
                    return False
                if response.status_code == 200 and self._save_tokens(self._json(response)):
                    g.log("Refreshed WeTrakr token")
                    return True
                if response.status_code == 401:
                    stored = self._stored_login()[1]
                    if stored and stored != refresh:
                        # rotated by another process while this request was on its way: use its value once
                        retry = self._send("POST", "/oauth/token/refresh", {"refresh_token": stored})
                        if retry is not None and retry.status_code == 200 and self._save_tokens(self._json(retry)):
                            return True
                    self._signed_out("refresh refused (HTTP 401)")
                else:
                    g.log(f"WeTrakr token refresh: HTTP {response.status_code}, login kept", "warning")
                return False
        except LockTimeout:
            g.log("WeTrakr token refresh: another refresh is still running, giving up for now", "warning")
            stored = self._stored_login()
            return bool(stored[0]) and not self._expiring()

    def _signed_out(self, reason):
        self._clear_auth_settings()
        g.log(f"WeTrakr signed out locally: {reason}", "warning")
        if g.get_runtime_setting(SIGNED_OUT_NOTICE):
            return
        g.set_runtime_setting(SIGNED_OUT_NOTICE, "1")
        xbmcgui.Dialog().notification(g.ADDON_NAME, g.get_language_string(31694))

    # endregion

    # region username / account info
    @staticmethod
    def _user_id(token):
        """The numeric user id inside the access token (a JWT: its payload is only decoded, never logged - it also holds the e-mail
        address). '' when there is none."""
        try:
            import base64
            import json

            part = (token or "").split(".")[1]
            return str(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("user_id") or "")
        except Exception:
            return ""

    def get_profile(self):
        """The public profile of the signed-in user (GET /users/{id}, no token needed), or None. GET /account itself answers 403
        NOT_PUBLIC to third-party apps, but the access token carries the user id and the profile holds info.username."""
        try:
            user_id = self._user_id(self.access_token)
            if not user_id:
                return None
            response = self._send("GET", f"/users/{user_id}")
            if response is None or response.status_code != 200:
                return None
            data = self._json(response)
            if isinstance(data.get("data"), dict):
                data = data["data"]
            return data if isinstance(data.get("info"), dict) else None
        except Exception:
            g.log_stacktrace()
            return None

    def get_username(self):
        """The WeTrakr username of the login, '' when it cannot be found (a private profile answers a minimal profile without it)"""
        profile = self.get_profile()
        return str(((profile or {}).get("info") or {}).get("username") or "")

    def save_username(self):
        """Looks the username up and stores it in wetrakr.username, with the user id it belongs to (nothing is written when the profile
        cannot be read). Returns the name."""
        profile = self.get_profile()
        if profile is None:
            return ""
        name = str((profile.get("info") or {}).get("username") or "")
        if name:
            g.set_setting(USERNAME_KEY, name)
        else:
            g.clear_setting(USERNAME_KEY)  # a private profile: no name to show, and never the name of an earlier account
        g.set_setting(USERID_KEY, self._user_id(self.access_token))
        return name

    def ensure_username(self):
        """A login saved before the Username row existed, or pushed by Account Manager (also one of another account), has no name or the name of
        an earlier account: look it up when the user id inside the access token is not the one the stored name was found for. No request
        otherwise. Called by the periodic sync, so no settings window is open to swallow the write."""
        if not self.is_authorized:
            return
        user_id = self._user_id(self.access_token)
        if not user_id or g.get_setting(USERID_KEY, "") == user_id:
            return
        if g.get_setting(USERNAME_KEY, ""):
            g.clear_setting(USERNAME_KEY)  # it belongs to another account; shown again once the lookup works
        self.save_username()

    def account_info_to_dialog(self):
        """Shows the signed-in WeTrakr account (username, user id, plan, member since, sign-in) in a select dialog; mirrors Simkl's"""
        title = f"{g.ADDON_NAME}: WeTrakr Account"
        saved_user = g.get_setting(USERNAME_KEY, "")
        try:
            if not self.is_authorized:
                xbmcgui.Dialog().ok(g.ADDON_NAME, "WeTrakr: Not authorized - authorize from settings.")
                return
            profile = self.get_profile() or {}
            info = profile.get("info") or {}
            items = [f"Username:  {info.get('username') or saved_user or 'N/A'}"]
            if profile.get("id"):
                items.append(f"User ID:  {profile['id']}")
            if profile.get("plan"):
                plan = str(profile["plan"])
                items.append(f"Plan:  {plan.upper() if len(plan) <= 3 else plan.capitalize()}")
            if profile.get("joined_at"):
                items.append(f"Joined:  {str(profile['joined_at'])[:19].replace('T', ' ').replace('Z', '')}")
            if g.get_setting(STATUS_KEY, ""):
                items.append(f"Sign-in:  {g.get_setting(STATUS_KEY)}")
            online = self.get_json("/sync/last_activities") is not None
            items.append("Status:  Online" if online else "Status:  Offline (WeTrakr unreachable or the sign-in was rejected)")
            xbmcgui.Dialog().select(title, items)
        except Exception as e:
            g.log(f"WeTrakr account_info_to_dialog error: {type(e).__name__}: {e}", "error")
            xbmcgui.Dialog().select(title, ([f"Username:  {saved_user}"] if saved_user else []) + ["Status:  Offline (WeTrakr unreachable)"])

    # endregion

    # region sign in / out
    def _device_failure(self, status):
        """The message for a failed sign-in step ('' for a status with no message of its own)"""
        if status in (401, 423):
            message = g.get_language_string(31693)
            if (g.get_setting("wetrakr.clientid", "") or "").strip():
                message += " " + g.get_language_string(31698)
            return message
        return {
            404: g.get_language_string(31692),
            409: g.get_language_string(31692),
            410: g.get_language_string(31692),
            418: g.get_language_string(31696),
            429: g.get_language_string(31697),
        }.get(status, "")

    @staticmethod
    def _delete_old_qr_files(keep):
        try:
            import glob
            import os

            if not keep:
                return
            for old in glob.glob(os.path.join(os.path.dirname(keep), "wetrakr_qr_*.png")):
                if old != keep:
                    os.remove(old)
        except Exception as e:
            g.log(f"WeTrakr: old QR files not removed - {type(e).__name__}", "debug")

    def auth(self):
        """
        Performs the WeTrakr device flow: shows a scannable QR code alongside the verification address and the code (also copied to
        the clipboard), polls until the user approves on another device, then stores the access and refresh tokens. Needs the client
        ID only, never a client secret.
        :return: None
        """
        response = self._send("POST", "/oauth/device/code", {"client_id": self.client_id}, backoff=False)
        data = self._json(response)
        if response is None or response.status_code != 200 or not data.get("device_code"):
            status = response.status_code if response is not None else None
            xbmcgui.Dialog().ok(
                g.ADDON_NAME,
                self._device_failure(status) or g.get_language_string(31691),
            )
            return

        device_code = data["device_code"]
        user_code = data.get("user_code", "")
        verification_url = data.get("verification_url") or "https://wetrakr.com/activate"
        expires_in = _to_int(data.get("expires_in"), 600)
        interval = max(_to_int(data.get("interval"), 5), 1)

        tools.copy2clip(user_code)
        # the link the official WeTrakr Kodi add-on uses: the page opens with the code already filled in
        # the picture changes with every sign-in (it holds the code) and Kodi caches textures by path: one file per code, the old
        # ones deleted
        qr_name = "wetrakr_qr_" + "".join(ch for ch in user_code if ch.isalnum()) + ".png"
        qr_image = tools.make_qr(f"{verification_url}?code={user_code}&platform=kodi", qr_name)
        self._delete_old_qr_files(qr_image)

        from resources.lib.gui.windows.qr_auth_window import QRAuthWindow

        window = QRAuthWindow("qr_auth_window.xml", g.ADDON_PATH)
        window.set_title(g.get_language_string(31688))
        window.set_qr_image(qr_image)
        window.set_details(
            g.get_language_string(31075).format(verification_url),
            g.get_language_string(31076).format(user_code),
        )
        window_thread = threading.Thread(target=window.doModal)
        window_thread.start()

        approved = None
        failure = ""
        canceled = False
        started = time.monotonic()
        next_poll = started + interval
        try:
            while not canceled and approved is None and not failure:
                if g.abort_requested():
                    return
                xbmc.sleep(500)
                elapsed = time.monotonic() - started
                if window.canceled_by_user:
                    canceled = True
                    break
                if elapsed >= expires_in:
                    break
                minutes, seconds = divmod(max(int(expires_in - elapsed), 0), 60)
                window.set_status(f"{minutes:02d}:{seconds:02d}")
                if time.monotonic() < next_poll:
                    continue
                next_poll = time.monotonic() + interval
                reply = self._send(
                    "POST", "/oauth/device/token", {"code": device_code, "client_id": self.client_id}, backoff=False
                )
                if reply is None:
                    continue
                if reply.status_code == 200:
                    approved = reply
                elif reply.status_code == 429:  # slow_down: the interval was too short
                    interval += 5
                elif reply.status_code == 400 and self._json(reply).get("error") == "authorization_pending":
                    continue
                else:
                    failure = self._device_failure(reply.status_code) or g.get_language_string(31691)
        finally:
            window.close()
            window_thread.join(5)
            del window

        if canceled:
            return
        if approved is not None and self._save_tokens(self._json(approved)):
            self._forget_synced_data()  # a different account must not inherit the last one's marks (the stamps decide what is read)
            g.set_setting(STATUS_KEY, g.get_language_string(31695).format(time.strftime("%Y-%m-%d")))
            g.clear_setting(USERNAME_KEY)  # an earlier account's name never stays; the lookup below fills the new one
            g.clear_setting(USERID_KEY)
            self.save_username()
            g.clear_runtime_setting(SIGNED_OUT_NOTICE)
            g.log("WeTrakr authenticated successfully")
            xbmcgui.Dialog().ok(g.ADDON_NAME, g.get_language_string(31689))
            return
        xbmcgui.Dialog().ok(g.ADDON_NAME, failure or g.get_language_string(31692))

    def revoke_auth(self):
        """
        Logs out of WeTrakr: ends this add-on's session at WeTrakr (POST /oauth/logout with the refresh token) and clears the stored
        login. A failure to reach WeTrakr still clears the login here; the session then stays valid there until it expires.
        :return: None
        """
        access, refresh, _ = self._stored_login()
        revoked = False
        if access and refresh:
            response = self._send("POST", "/oauth/logout", {"refresh_token": refresh}, token=access, backoff=False)
            revoked = response is not None and response.status_code == 200
        self._clear_auth_settings()
        self._forget_synced_data()
        g.log(f"WeTrakr logged out (session revoked at WeTrakr: {'yes' if revoked else 'no'})")
        xbmcgui.Dialog().ok(g.ADDON_NAME, g.get_language_string(31690))

    # endregion
