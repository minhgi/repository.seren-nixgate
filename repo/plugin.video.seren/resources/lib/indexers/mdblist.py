from functools import cached_property
from urllib import parse

import xbmc
import xbmcgui

from resources.lib.indexers.apibase import ApiBase
from resources.lib.modules.globals import g


class MDBListAPI(ApiBase):
    """
    Class to handle interactions with the MDBList API
    """

    ApiUrl = "https://api.mdblist.com/"

    username_setting_key = "mdblist.username"
    is_supporter_setting_key = "mdblist.is_supporter"

    def __init__(self):
        self.api_key = None
        self.username = None
        self._load_settings()

    @cached_property
    def session(self):
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3 import Retry

        class _CappedRetry(Retry):
            """MDBList's documented Retry-After can be up to 12 hours
            (mdblist.apib); urllib3 respects that header by default and
            sleeps the full duration on the calling thread, which can hang
            a foreground Kodi action for hours. Capping the sleep (not
            disabling the header entirely) still honours short, legitimate
            backoff windows correctly - it only bounds the pathological
            case. Retries are exhausted the same as any other failure,
            already caught by get()/get_json()'s try/except."""

            _MAX_RETRY_AFTER = 15

            def get_retry_after(self, response):
                retry_after = super().get_retry_after(response)
                if retry_after is None:
                    return None
                return min(retry_after, self._MAX_RETRY_AFTER)

        session = requests.Session()
        retries = _CappedRetry(
            total=4,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        session.mount("https://", HTTPAdapter(max_retries=retries, pool_maxsize=100))
        return session

    # region Auth
    def authorize(self):
        """
        Prompts for the user's personal MDBList API key, validates it against GET /user
        and stores the key + linked username if valid
        :return: None
        """
        api_key = xbmcgui.Dialog().input(g.get_language_string(30948), type=xbmcgui.INPUT_ALPHANUM)
        if not api_key:
            return

        self.api_key = api_key
        self.username = None
        username = self.get_username()
        if not username:
            self.api_key = None
            xbmcgui.Dialog().ok(g.ADDON_NAME, g.get_language_string(30951))
            return

        g.set_setting("mdblist.apikey", api_key)
        self.username = username
        g.set_setting(self.username_setting_key, username)
        xbmcgui.Dialog().ok(g.ADDON_NAME, g.get_language_string(30952))
        xbmc.executebuiltin(f'RunPlugin("{g.BASE_URL}?action=syncMDBListActivities")')

    def revoke_auth(self):
        """
        Clears the stored MDBList API key and linked username
        :return:
        """
        g.clear_setting("mdblist.apikey")
        g.clear_setting(self.username_setting_key)
        g.clear_setting(self.is_supporter_setting_key)
        self.api_key = None
        self.username = None
        xbmcgui.Dialog().ok(g.ADDON_NAME, g.get_language_string(30950))

    def _load_settings(self):
        self.api_key = g.get_setting("mdblist.apikey") or None
        self.username = g.get_setting(self.username_setting_key) or None

    # endregion

    @staticmethod
    def _get_headers():
        return {"Content-Type": "application/json", "User-Agent": g.USER_AGENT}

    def get(self, url, **params):
        """
        Performs a GET request to specified endpoint and returns response
        :param url: endpoint to perform request against
        :param params: URL params for request
        :return: request response
        """
        timeout = params.pop("timeout", 10)
        params.setdefault("apikey", self.api_key)
        return self.session.get(
            parse.urljoin(self.ApiUrl, url),
            params=params,
            headers=self._get_headers(),
            timeout=timeout,
        )

    def get_json(self, url, **params):
        """
        Performs a GET request to specified endpoint and returns JSON response
        :param url: endpoint to perform request against
        :param params: URL params for request
        :return: JSON response, or None on failure
        """
        try:
            response = self.get(url, **params)
        except Exception as e:
            g.log(f"Failed to contact MDBList: {e}", "error")
            return None
        if response is None or not response.ok:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def post(self, url, data, **params):
        """
        Performs a POST request to specified endpoint and returns response
        :param url: endpoint to perform request against
        :param data: POST data to send to endpoint
        :param params: URL params for request
        :return: request response, or None on failure
        """
        timeout = params.pop("timeout", 10)
        params.setdefault("apikey", self.api_key)
        try:
            return self.session.post(
                parse.urljoin(self.ApiUrl, url),
                params=params,
                json=data,
                headers=self._get_headers(),
                timeout=timeout,
            )
        except Exception as e:
            g.log(f"Failed to contact MDBList: {e}", "error")
            return None

    def post_json(self, url, data, **params):
        """
        Performs a POST request to specified endpoint and returns JSON response
        :param url: endpoint to perform request against
        :param data: POST data to send to endpoint
        :param params: URL params for request
        :return: JSON response, or None on failure
        """
        response = self.post(url, data, **params)
        if response is None or not response.ok:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def put(self, url, data, **params):
        """
        Performs a PUT request to specified endpoint and returns response
        :param url: endpoint to perform request against
        :param data: PUT data to send to endpoint
        :param params: URL params for request
        :return: request response, or None on failure
        """
        timeout = params.pop("timeout", 10)
        params.setdefault("apikey", self.api_key)
        try:
            return self.session.put(
                parse.urljoin(self.ApiUrl, url),
                params=params,
                json=data,
                headers=self._get_headers(),
                timeout=timeout,
            )
        except Exception as e:
            g.log(f"Failed to contact MDBList: {e}", "error")
            return None

    def delete(self, url, **params):
        """
        Performs a DELETE request to specified endpoint and returns response
        :param url: endpoint to perform request against
        :param params: URL params for request
        :return: request response, or None on failure
        """
        timeout = params.pop("timeout", 10)
        params.setdefault("apikey", self.api_key)
        try:
            return self.session.delete(
                parse.urljoin(self.ApiUrl, url),
                params=params,
                headers=self._get_headers(),
                timeout=timeout,
            )
        except Exception as e:
            g.log(f"Failed to contact MDBList: {e}", "error")
            return None

    def get_username(self):
        """
        Fetch the username linked to the currently held API key
        :return: string username, or None if the key is invalid/unreachable
        """
        user_details = self.get_json("user")
        if not user_details:
            return None
        return user_details.get("username")

    def is_supporter(self):
        """
        Whether the current account has MDBList supporter status (gates the
        rating-threshold filters on list-item browsing, which the API rejects
        for non-supporters). Cached after the first check since this rarely
        changes - avoids a GET /user round-trip every time the filter menu opens.
        Accounts authorized before this setting existed have no cached value yet,
        so this lazily backfills on first use rather than requiring re-auth.
        Fails closed (treated as non-supporter) if never cached and the live
        check fails, so a transient API error just hides the feature instead of
        exposing params the server would reject.
        :return: bool
        """
        cached = g.get_setting(self.is_supporter_setting_key)
        if cached:
            return cached == "true"
        user_details = self.get_json("user") or {}
        supporter = bool(user_details.get("is_supporter"))
        if user_details:
            g.set_setting(self.is_supporter_setting_key, "true" if supporter else "false")
        return supporter
