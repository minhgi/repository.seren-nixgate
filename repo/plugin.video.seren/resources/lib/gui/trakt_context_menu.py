from functools import cached_property

import xbmcgui

from resources.lib.modules.globals import g


class TraktContextMenu:
    """
    Handles manual user interactions to the Trakt API
    """

    def __init__(self, item_information, scope):
        super().__init__()
        trakt_id = item_information["trakt_id"]
        item_type = item_information["action_args"]["mediatype"].lower()
        display_type = self._get_display_name(item_type)

        self._confirm_item_information(item_information)

        _scope_header_strings = {"trakt": 30286, "mdblist": 30961, "simkl": 31085}
        self.header_text = g.get_language_string(_scope_header_strings.get(scope, 30961))

        self.dialog_list = []
        options = {}

        if scope == "trakt" and g.get_setting("trakt.auth") and g.get_bool_setting("trakt.enabled", True):
            self._handle_watched_options(item_information, item_type)
            self._handle_collected_options(item_information, trakt_id, display_type)
            self._handle_watchlist_options(item_type)
            self._handle_favorited_options(item_type)

            standard_list = [
                g.get_language_string(30280),
                g.get_language_string(30281),
                g.get_language_string(30282).format(display_type),
                g.get_language_string(30283),
            ]

            self.dialog_list.extend(iter(standard_list))
            self._handle_progress_option(item_type, trakt_id)

            options.update(
                {
                    g.get_language_string(30274).format(display_type): {
                        "method": self._add_to_collection,
                        "info_key": "info",
                    },
                    g.get_language_string(30275).format(display_type): {
                        "method": self._remove_from_collection,
                        "info_key": "info",
                    },
                    g.get_language_string(30276): {
                        "method": self._add_to_watchlist,
                        "info_key": "info",
                    },
                    g.get_language_string(30277): {
                        "method": self._remove_from_watchlist,
                        "info_key": "info",
                    },
                    g.get_language_string(30989): {
                        "method": self._add_to_favorites,
                        "info_key": "info",
                    },
                    g.get_language_string(30990): {
                        "method": self._remove_from_favorites,
                        "info_key": "info",
                    },
                    g.get_language_string(30278): {
                        "method": self._mark_watched,
                        "info_key": "info",
                    },
                    g.get_language_string(30279): {
                        "method": self._mark_unwatched,
                        "info_key": "info",
                    },
                    g.get_language_string(30280): {
                        "method": self._add_to_list,
                        "info_key": "info",
                    },
                    g.get_language_string(30281): {
                        "method": self._remove_from_list,
                        "info_key": "info",
                    },
                    g.get_language_string(30282).format(display_type): {
                        "method": self._hide_item,
                        "info_key": "action_args",
                    },
                    g.get_language_string(30283): {
                        "method": self._refresh_meta_information,
                        "info_key": "info",
                    },
                    g.get_language_string(30284): {
                        "method": self._remove_playback_history,
                        "info_key": "info",
                    },
                }
            )

        if scope == "mdblist" and g.get_bool_setting("mdblist.enabled") and g.get_setting("mdblist.apikey"):
            self.dialog_list.extend(
                [
                    g.get_language_string(30954),
                    g.get_language_string(30955),
                    g.get_language_string(31044),
                    g.get_language_string(31045),
                    g.get_language_string(31062),
                ]
            )
            options.update(
                {
                    g.get_language_string(30954): {
                        "method": self._mdblist_mark_watched,
                        "info_key": "info",
                    },
                    g.get_language_string(30955): {
                        "method": self._mdblist_mark_unwatched,
                        "info_key": "info",
                    },
                    g.get_language_string(31044): {
                        "method": self._mdblist_add_to_list,
                        "info_key": "info",
                    },
                    g.get_language_string(31045): {
                        "method": self._mdblist_remove_from_list,
                        "info_key": "info",
                    },
                    g.get_language_string(31062): {
                        "method": self._mdblist_manage_lists,
                        "info_key": "info",
                    },
                }
            )
            if item_type not in ("tvshow", "season"):
                self.dialog_list.append(g.get_language_string(30956))
                options[g.get_language_string(30956)] = {
                    "method": self._mdblist_clear_progress,
                    "info_key": "info",
                }

        if scope == "simkl" and g.get_setting("simkl.auth") and g.get_bool_setting("simkl.enabled"):
            self.dialog_list.extend(
                [
                    g.get_language_string(31086),
                    g.get_language_string(31087),
                    g.get_language_string(31090),
                ]
            )
            options.update(
                {
                    g.get_language_string(31086): {
                        "method": self._simkl_mark_watched,
                        "info_key": "info",
                    },
                    g.get_language_string(31087): {
                        "method": self._simkl_mark_unwatched,
                        "info_key": "info",
                    },
                    g.get_language_string(31090): {
                        "method": self._simkl_set_status,
                        "info_key": "info",
                    },
                }
            )
            # Ratings scoped to movie/tvshow only - Simkl's rating UI (like Trakt's
            # own watchlist/favorites options) has no season/episode precedent, and
            # neither the docs nor either reference addon showed per-episode ratings.
            if item_type in ("movie", "tvshow"):
                self.dialog_list += [
                    g.get_language_string(31088),
                    g.get_language_string(31089),
                ]
                options.update(
                    {
                        g.get_language_string(31088): {
                            "method": self._simkl_rate,
                            "info_key": "info",
                        },
                        g.get_language_string(31089): {
                            "method": self._simkl_clear_rating,
                            "info_key": "info",
                        },
                    }
                )
            # Mirrors MDBList's identical item_type gate on Clear Progress above -
            # playback/resume state is a movie/episode concept, not show/season.
            if item_type in ("movie", "episode"):
                self.dialog_list.append(g.get_language_string(31091))
                options[g.get_language_string(31091)] = {
                    "method": self._simkl_remove_playback_history,
                    "info_key": "info",
                }

        if not self.dialog_list:
            return

        selection = xbmcgui.Dialog().select(
            f"{g.ADDON_NAME}: {self.header_text}",
            self.dialog_list,
        )

        if selection == -1:
            return

        selected_option = self.dialog_list[selection]
        if selected_option not in options:
            return
        else:
            selected_option = options[selected_option]

        selected_option["method"](item_information[selected_option["info_key"]])

    @cached_property
    def trakt_api(self):
        from resources.lib.indexers.trakt import TraktAPI

        return TraktAPI()

    @staticmethod
    def _get_display_name(content_type):
        if content_type == "movie":
            return g.get_language_string(30264)
        else:
            return g.get_language_string(30285)

    def _handle_watchlist_options(self, item_type):
        if item_type not in ["season", "episode"]:
            self.dialog_list += [
                g.get_language_string(30276),
                g.get_language_string(30277),
            ]

    def _handle_favorited_options(self, item_type):
        if item_type not in ["season", "episode"]:
            self.dialog_list += [
                g.get_language_string(30989),
                g.get_language_string(30990),
            ]

    def _handle_progress_option(self, item_type, trakt_id):
        from resources.lib.database.trakt_sync.bookmark import TraktSyncDatabase

        if item_type not in ["show", "season"] and TraktSyncDatabase().get_bookmark(trakt_id):
            self.dialog_list.append(g.get_language_string(30284))

    def _handle_collected_options(self, item_information, trakt_id, display_type):
        if item_information["info"]["mediatype"] == "movie":
            from resources.lib.database.trakt_sync.movies import TraktSyncDatabase

            collection = [i["trakt_id"] for i in TraktSyncDatabase().get_all_collected_movies()]
        else:
            from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

            collection = TraktSyncDatabase().get_collected_shows(force_all=True)
            collection = {i["trakt_id"] for i in collection if i is not None}
            trakt_id = self._get_show_id(item_information["info"])
        if trakt_id in collection:
            self.dialog_list.append(g.get_language_string(30275).format(display_type))
        else:
            self.dialog_list.append(g.get_language_string(30274).format(display_type))

    def _handle_watched_options(self, item_information, item_type):
        if item_type in ["movie", "episode"]:
            if item_information["play_count"] > 0:
                self.dialog_list.append(g.get_language_string(30279))
            else:
                self.dialog_list.append(g.get_language_string(30278))
        elif item_information.get("unwatched_episodes", 0) > 0:
            self.dialog_list.append(g.get_language_string(30278))
        else:
            self.dialog_list.append(g.get_language_string(30279))

    @staticmethod
    def _confirm_item_information(item_information):
        if item_information is None:
            raise TypeError("Invalid item information passed to Trakt Manager")

    @staticmethod
    def _refresh_meta_information(trakt_object):
        from resources.lib.database import trakt_sync

        trakt_sync.TraktSyncDatabase().clear_specific_item_meta(trakt_object["trakt_id"], trakt_object["mediatype"])
        g.container_refresh()
        g.trigger_widget_refresh()

    @staticmethod
    def _confirm_marked_watched(response, type):
        if response["added"][type] > 0:
            return True
        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
            g.get_language_string(30287),
        )
        g.log(f"Failed to mark item as watched\nTrakt Response: {response}")

        return False

    @staticmethod
    def _confirm_marked_unwatched(response, type):
        if response["deleted"][type] > 0:
            return True
        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
            g.get_language_string(30287),
        )
        g.log(f"Failed to mark item as unwatched\nTrakt Response: {response}")
        return False

    @staticmethod
    def _info_to_trakt_object(item_information, force_show=False):
        if force_show and item_information["mediatype"] in ("season", "episode"):
            ids = [{"ids": {"trakt": item_information["trakt_show_id"]}}]
            return {"shows": ids}
        ids = [{"ids": {"trakt": item_information["trakt_id"]}}]
        if item_information["mediatype"] == "movie":
            return {"movies": ids}
        elif item_information["mediatype"] == "season":
            return {"seasons": ids}
        elif item_information["mediatype"] == "tvshow":
            return {"shows": ids}
        elif item_information["mediatype"] == "episode":
            return {"episodes": ids}

    @staticmethod
    def _get_show_id(item_information):
        return (
            item_information["trakt_show_id"]
            if item_information["mediatype"] != "tvshow"
            else item_information["trakt_id"]
        )

    def _mark_watched(self, item_information, silent=False):
        response = self.trakt_api.post_json("sync/history", self._info_to_trakt_object(item_information))

        if item_information["mediatype"] == "movie":
            from resources.lib.database.trakt_sync.movies import TraktSyncDatabase

            if not self._confirm_marked_watched(response, "movies"):
                return
            TraktSyncDatabase().mark_movie_watched(item_information["trakt_id"])
        else:
            from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

            if not self._confirm_marked_watched(response, "episodes"):
                return
            if item_information["mediatype"] == "episode":
                TraktSyncDatabase().mark_episode_watched(
                    item_information["trakt_show_id"],
                    item_information["season"],
                    item_information["episode"],
                )
            elif item_information["mediatype"] == "season":
                show_id = item_information["trakt_show_id"]
                season_no = item_information["season"]
                TraktSyncDatabase().mark_season_watched(show_id, season_no, 1)
            elif item_information["mediatype"] == "tvshow":
                TraktSyncDatabase().mark_show_watched(item_information["trakt_id"], 1)

        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
            g.get_language_string(30288),
        )
        from resources.lib.modules import cross_sync

        cross_sync.mirror_watched("trakt", item_information, True)
        if not silent:
            g.container_refresh()
            g.trigger_widget_refresh()

    def _mark_unwatched(self, item_information):
        response = self.trakt_api.post_json("sync/history/remove", self._info_to_trakt_object(item_information))

        if item_information["mediatype"] == "movie":
            from resources.lib.database.trakt_sync.movies import TraktSyncDatabase

            if not self._confirm_marked_unwatched(response, "movies"):
                return
            TraktSyncDatabase().mark_movie_unwatched(item_information["trakt_id"])

        else:
            from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

            if not self._confirm_marked_unwatched(response, "episodes"):
                return
            if item_information["mediatype"] == "episode":
                TraktSyncDatabase().mark_episode_unwatched(
                    item_information["trakt_show_id"],
                    item_information["season"],
                    item_information["episode"],
                )
            elif item_information["mediatype"] == "season":
                show_id = item_information["trakt_show_id"]
                season_no = item_information["season"]
                TraktSyncDatabase().mark_season_watched(show_id, season_no, 0)
            elif item_information["mediatype"] == "tvshow":
                TraktSyncDatabase().mark_show_watched(item_information["trakt_id"], 0)

        from resources.lib.database.trakt_sync.bookmark import TraktSyncDatabase

        TraktSyncDatabase().remove_bookmark(item_information["trakt_id"])

        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
            g.get_language_string(30289),
        )
        from resources.lib.modules import cross_sync

        cross_sync.mirror_watched("trakt", item_information, False)
        g.container_refresh()
        g.trigger_widget_refresh()

    def _add_to_collection(self, item_information):
        self.trakt_api.post("sync/collection", self._info_to_trakt_object(item_information, True))

        if item_information["mediatype"] == "movie":
            from resources.lib.database.trakt_sync.movies import TraktSyncDatabase

            TraktSyncDatabase().mark_movie_collected(item_information["trakt_id"])
        else:
            from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

            trakt_id = self._get_show_id(item_information)
            TraktSyncDatabase().mark_show_collected(trakt_id, 1)

        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
            g.get_language_string(30290),
        )
        from resources.lib.modules import cross_sync

        cross_sync.mirror_collection_add_from_trakt(item_information)
        g.trigger_widget_refresh()

    def _remove_from_collection(self, item_information):
        self.trakt_api.post("sync/collection/remove", self._info_to_trakt_object(item_information, True))

        if item_information["mediatype"] == "movie":
            from resources.lib.database.trakt_sync.movies import TraktSyncDatabase

            TraktSyncDatabase().mark_movie_uncollected(item_information["trakt_id"])
        else:
            from resources.lib.database.trakt_sync.shows import TraktSyncDatabase

            trakt_id = self._get_show_id(item_information)
            TraktSyncDatabase().mark_show_collected(trakt_id, 0)

        g.container_refresh()
        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
            g.get_language_string(30291),
        )
        from resources.lib.modules import cross_sync

        cross_sync.mirror_collection_remove_from_trakt(item_information)
        g.trigger_widget_refresh()

    def _add_to_watchlist(self, item_information):
        self.trakt_api.post("sync/watchlist", self._info_to_trakt_object(item_information, True))
        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
            g.get_language_string(30292),
        )
        from resources.lib.modules import cross_sync

        cross_sync.mirror_watchlist_add_from_trakt(item_information)
        g.trigger_widget_refresh()

    def _remove_from_watchlist(self, item_information):
        self.trakt_api.post("sync/watchlist/remove", self._info_to_trakt_object(item_information, True))
        g.container_refresh()
        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
            g.get_language_string(30293),
        )
        from resources.lib.modules import cross_sync

        cross_sync.mirror_watchlist_remove_from_trakt(item_information)
        g.trigger_widget_refresh()

    def _add_to_favorites(self, item_information):
        response = self.trakt_api.post("sync/favorites", self._info_to_trakt_object(item_information, True))

        if response is not None and response.status_code == 420:
            g.notification(
                f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
                g.get_language_string(30993),
            )
            return

        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
            g.get_language_string(30991),
        )
        g.trigger_widget_refresh()

    def _remove_from_favorites(self, item_information):
        self.trakt_api.post("sync/favorites/remove", self._info_to_trakt_object(item_information, True))
        g.container_refresh()
        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
            g.get_language_string(30992),
        )
        g.trigger_widget_refresh()

    def _add_to_list(self, item_information):
        from resources.lib.modules.metadataHandler import MetadataHandler

        get = MetadataHandler.get_trakt_info
        lists = self.trakt_api.get_json("users/me/lists")
        selection = xbmcgui.Dialog().select(
            f"{g.ADDON_NAME}: {g.get_language_string(30296)}",
            [get(i, "name") for i in lists],
        )
        if selection == -1:
            return
        selection = lists[selection]
        self.trakt_api.post_json(
            f"users/me/lists/{selection['trakt_id']}/items",
            self._info_to_trakt_object(item_information, True),
        )
        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
            g.get_language_string(30294).format(get(selection, "name")),
        )
        g.trigger_widget_refresh()

    def _remove_from_list(self, item_information):
        from resources.lib.modules.metadataHandler import MetadataHandler

        get = MetadataHandler.get_trakt_info
        lists = self.trakt_api.get_json("users/me/lists")
        selection = xbmcgui.Dialog().select(
            f"{g.ADDON_NAME}: {g.get_language_string(30296)}",
            [get(i, "name") for i in lists],
        )
        if selection == -1:
            return
        selection = lists[selection]
        self.trakt_api.post_json(
            f'users/me/lists/{selection["trakt_id"]}/items/remove', self._info_to_trakt_object(item_information, True)
        )

        g.container_refresh()
        g.notification(
            f"{g.ADDON_NAME}: {g.get_language_string(30286)}",
            g.get_language_string(30295).format(get(selection, "name")),
        )
        g.trigger_widget_refresh()

    def _hide_item(self, item_information):
        from resources.lib.database.trakt_sync.hidden import TraktSyncDatabase

        if item_information['mediatype'] == "movie":
            section = "calendar"
            sections_display = [g.get_language_string(30298)]
            selection = 0
        else:
            sections_display = [g.get_language_string(30297), g.get_language_string(30298)]
            selection = xbmcgui.Dialog().select(
                f"{g.ADDON_NAME}: {g.get_language_string(30299)}",
                sections_display,
            )
            if selection == -1:
                return
            sections = ["progress_watched", "calendar"]
            section = sections[selection]

        self.trakt_api.post_json(
            f"users/hidden/{section}",
            self._info_to_trakt_object(item_information, True),
        )

        if item_information['mediatype'] == "movie":
            TraktSyncDatabase().add_hidden_item(item_information['trakt_id'], "movie", section)
        else:
            TraktSyncDatabase().add_hidden_item(
                item_information.get("trakt_show_id", item_information['trakt_id']), "tvshow", section
            )

        g.container_refresh()
        g.notification(
            g.ADDON_NAME,
            g.get_language_string(30300).format(sections_display[selection]),
        )
        g.trigger_widget_refresh()

    def _remove_playback_history(self, item_information):
        media_type = "episode" if item_information["mediatype"] != "movie" else "movie"
        progress = self.trakt_api.get_json(f"sync/playback/{media_type}s")
        if len(progress) == 0:
            return
        if media_type == "movie":
            progress_ids = [i["playback_id"] for i in progress if i["trakt_id"] == item_information["trakt_id"]]
        else:
            progress_ids = [
                i["playback_id"] for i in progress if i["episode"]["trakt_id"] == item_information["trakt_id"]
            ]

        for i in progress_ids:
            self.trakt_api.delete_request(f"sync/playback/{i}")

        from resources.lib.database.trakt_sync.bookmark import TraktSyncDatabase

        TraktSyncDatabase().remove_bookmark(item_information["trakt_id"])

        g.container_refresh()
        g.notification(g.ADDON_NAME, g.get_language_string(30301))
        g.trigger_widget_refresh()

    @cached_property
    def mdblist_api(self):
        from resources.lib.indexers.mdblist import MDBListAPI

        return MDBListAPI()

    @staticmethod
    def _info_to_mdblist_object(item_information):
        mediatype = item_information["mediatype"]
        if mediatype == "movie":
            return {"movies": [{"ids": {"tmdb": item_information["tmdb_id"]}}]}
        if mediatype == "tvshow":
            return {"shows": [{"ids": {"tmdb": item_information["tmdb_id"]}}]}
        if mediatype == "season":
            return {
                "shows": [
                    {
                        "ids": {"tmdb": item_information["tmdb_show_id"]},
                        "seasons": [{"number": item_information["season"]}],
                    }
                ]
            }
        if mediatype == "episode":
            return {
                "shows": [
                    {
                        "ids": {"tmdb": item_information["tmdb_show_id"]},
                        "seasons": [
                            {
                                "number": item_information["season"],
                                "episodes": [{"number": item_information["episode"]}],
                            }
                        ],
                    }
                ]
            }
        return None

    @staticmethod
    def _mdblist_response_key(mediatype):
        return {"movie": "movies", "tvshow": "shows", "season": "seasons", "episode": "episodes"}.get(mediatype)

    def _mdblist_mark_watched(self, item_information):
        payload = self._info_to_mdblist_object(item_information)
        if payload is None:
            return
        response = self.mdblist_api.post_json("sync/watched", payload)
        key = self._mdblist_response_key(item_information["mediatype"])
        if not response or response.get("updated", {}).get(key, 0) <= 0:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            g.log(f"Failed to mark item as watched on MDBList\nResponse: {response}")
            return

        from resources.lib.database.mdblist_sync import MDBListSyncDatabase

        MDBListSyncDatabase().write_watched_locally(item_information["mediatype"], item_information)
        cleared_progress = self._mdblist_clear_playback_entries(item_information)

        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(30957),
        )
        from resources.lib.modules import cross_sync

        cross_sync.mirror_watched("mdblist", item_information, True)
        if cleared_progress:
            g.container_refresh()
            g.trigger_widget_refresh()

    def _mdblist_mark_unwatched(self, item_information):
        payload = self._info_to_mdblist_object(item_information)
        if payload is None:
            return
        response = self.mdblist_api.post_json("sync/watched/remove", payload)
        key = self._mdblist_response_key(item_information["mediatype"])
        if not response or response.get("removed", {}).get(key, 0) <= 0:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            g.log(f"Failed to mark item as unwatched on MDBList\nResponse: {response}")
            return

        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(30958),
        )
        from resources.lib.modules import cross_sync

        cross_sync.mirror_watched("mdblist", item_information, False)

    def _mdblist_clear_playback_entries(self, item_information):
        # No notification/refresh here - mark_watched clears silently while Clear Progress reports success/failure.
        mediatype = item_information["mediatype"]
        if mediatype not in ("movie", "episode"):
            return False

        playback = self.mdblist_api.get_json("sync/playback")
        if not playback:
            return False

        if mediatype == "movie":
            entry_ids = [
                i["id"]
                for i in playback
                if i.get("movie") and i["movie"].get("ids", {}).get("tmdb") == item_information["tmdb_id"]
            ]
        else:
            entry_ids = [
                i["id"]
                for i in playback
                if i.get("episode")
                and i.get("show")
                and i["show"].get("ids", {}).get("tmdb") == item_information["tmdb_show_id"]
                and i["episode"].get("season") == item_information["season"]
                and i["episode"].get("number") == item_information["episode"]
            ]

        if not entry_ids:
            return False

        for entry_id in entry_ids:
            self.mdblist_api.post_json("scrobble/clear", {"id": entry_id})

        return True

    def _mdblist_clear_progress(self, item_information):
        if not self._mdblist_clear_playback_entries(item_information):
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30960),
            )
            return

        g.container_refresh()
        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(30959),
        )
        g.trigger_widget_refresh()

    @staticmethod
    def _info_to_mdblist_list_object(item_information):
        """Flat payload shape required by POST /lists/{listid}/items/{add|remove} - different
        from _info_to_mdblist_object's nested {"ids": {...}} shape used for sync/watched.
        Movie/show granularity only (no season/episode nesting, per the API) - season/episode
        force-resolve to their parent show's tmdb id, mirroring _info_to_trakt_object's
        force_show=True handling of the same limitation on Trakt's own list-items endpoint.
        """
        mediatype = item_information["mediatype"]
        if mediatype == "movie":
            return {"movies": [{"tmdb": item_information["tmdb_id"]}]}
        if mediatype in ("tvshow", "season", "episode"):
            show_tmdb_id = (
                item_information["tmdb_id"] if mediatype == "tvshow" else item_information["tmdb_show_id"]
            )
            return {"shows": [{"tmdb": show_tmdb_id}]}
        return None

    def _mdblist_static_lists(self):
        # Static (non-dynamic) only - dynamic/smart lists reject manual add/remove per the
        # API. Deliberately NOT filtered by mediatype: unlike dynamic lists (whose mediatype
        # reflects a real smart-list rule), a static list accepts movies and shows in the
        # same add/remove payload and its own mediatype field is unreliable - live-verified
        # a freshly-created static list keeps mediatype=None even after an item is added to
        # it, which would wrongly and permanently hide it from every future add/remove
        # picker if filtered on that field (my_lists()'s own mediatype filter is a browse-
        # menu display split, not a real write-eligibility constraint - doesn't apply here).
        lists = self.mdblist_api.get_json("lists/user") or []
        return [lst for lst in lists if not lst.get("dynamic")]

    def _mdblist_create_list(self):
        name = g.get_keyboard_input(heading=g.get_language_string(31047))
        if not name:
            return None
        private = xbmcgui.Dialog().yesno(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31048),
        )

        response = self.mdblist_api.post("lists/user/add", {"name": name, "private": bool(private)})

        if response is not None and response.status_code == 201:
            try:
                list_id = response.json().get("id")
            except ValueError:
                list_id = None
            if list_id is not None:
                return {"id": list_id, "name": name}

        if response is not None and response.status_code == 403:
            try:
                limit = response.json().get("limit", "")
            except ValueError:
                limit = ""
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(31052).format(limit),
            )
            return None

        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(30287),
        )
        g.log(f"Failed to create MDBList list\nResponse: {response}")
        return None

    def _mdblist_add_to_list(self, item_information):
        payload = self._info_to_mdblist_list_object(item_information)
        if payload is None:
            return
        key = "movies" if "movies" in payload else "shows"

        lists = self._mdblist_static_lists()
        labels = [g.get_language_string(31046)] + [lst.get("name", "") for lst in lists]
        selection = xbmcgui.Dialog().select(
            f"{g.ADDON_NAME}: {g.get_language_string(30296)}",
            labels,
        )
        if selection == -1:
            return
        if selection == 0:
            target = self._mdblist_create_list()
            if target is None:
                return
        else:
            target = lists[selection - 1]

        response = self.mdblist_api.post_json(f"lists/{target['id']}/items/add", payload)
        g.log(f"MDBList add-to-list response: {response}", "debug")
        if response is None:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            return

        not_found = (response.get("not_found") or {}).get(key, 0)
        if not_found > 0:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(31051),
            )
            return

        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31049).format(target["name"]),
        )
        g.trigger_widget_refresh()

    def _mdblist_remove_from_list(self, item_information):
        payload = self._info_to_mdblist_list_object(item_information)
        if payload is None:
            return
        key = "movies" if "movies" in payload else "shows"

        lists = self._mdblist_static_lists()
        if not lists:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(31053),
            )
            return

        selection = xbmcgui.Dialog().select(
            f"{g.ADDON_NAME}: {g.get_language_string(30296)}",
            [lst.get("name", "") for lst in lists],
        )
        if selection == -1:
            return
        target = lists[selection]

        response = self.mdblist_api.post_json(f"lists/{target['id']}/items/remove", payload)
        g.log(f"MDBList remove-from-list response: {response}", "debug")
        if response is None:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            return

        not_found = (response.get("not_found") or {}).get(key, 0)
        if not_found > 0:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(31139),
            )
            return

        g.container_refresh()
        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31050).format(target["name"]),
        )
        g.trigger_widget_refresh()

    def _mdblist_manage_lists(self, item_information):
        """Entry point for renaming/reprivacying/deleting a static list. Reuses
        _mdblist_static_lists() rather than my_lists()'s own directory, since static lists
        never appear there (see that method's docstring) - this picker is the only place
        they're currently reachable for management. item_information is unused; the method
        signature is fixed by TraktContextMenu's dispatch contract (options[label]["method"]
        is always called with item_information[info_key]).
        """
        lists = self._mdblist_static_lists()
        if not lists:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(31053),
            )
            return

        selection = xbmcgui.Dialog().select(
            f"{g.ADDON_NAME}: {g.get_language_string(30296)}",
            [lst.get("name", "") for lst in lists],
        )
        if selection == -1:
            return
        target = lists[selection]

        actions = [
            g.get_language_string(31054),
            g.get_language_string(31056) if target.get("private") else g.get_language_string(31055),
            g.get_language_string(31057),
        ]
        action_selection = xbmcgui.Dialog().select(
            f"{g.ADDON_NAME}: {self.header_text}",
            actions,
        )
        if action_selection == -1:
            return

        if action_selection == 0:
            self._mdblist_rename_list(target)
        elif action_selection == 1:
            self._mdblist_set_list_privacy(target)
        elif action_selection == 2:
            self._mdblist_delete_list(target)

    def _mdblist_rename_list(self, target):
        name = g.get_keyboard_input(heading=g.get_language_string(31054), default=target.get("name", ""))
        if not name or name == target.get("name"):
            return
        response = self.mdblist_api.put(f"lists/{target['id']}", {"name": name})
        if response is None or not response.ok:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            g.log(f"Failed to rename MDBList list {target['id']}\nResponse: {response}")
            return
        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31058).format(name),
        )
        g.container_refresh()
        g.trigger_widget_refresh()

    def _mdblist_set_list_privacy(self, target):
        new_private = not target.get("private")
        response = self.mdblist_api.put(f"lists/{target['id']}", {"private": new_private})
        if response is None or not response.ok:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            g.log(f"Failed to update MDBList list {target['id']} privacy\nResponse: {response}")
            return
        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31059),
        )
        g.container_refresh()
        g.trigger_widget_refresh()

    def _mdblist_delete_list(self, target):
        confirmed = xbmcgui.Dialog().yesno(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31060).format(target.get("name", "")),
        )
        if not confirmed:
            return
        response = self.mdblist_api.delete(f"lists/{target['id']}")
        if response is None or not response.ok:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            g.log(f"Failed to delete MDBList list {target['id']}\nResponse: {response}")
            return
        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31061).format(target.get("name", "")),
        )
        g.container_refresh()
        g.trigger_widget_refresh()

    # region Simkl (scope == "simkl")

    @cached_property
    def simkl_api(self):
        from resources.lib.indexers.simkl import SimklAPI

        return SimklAPI()

    @staticmethod
    def _info_to_simkl_object(item_information, extra_fields=None):
        """Nested shape verified against simkl.apib's POST /sync/history and
        POST /sync/ratings examples - identical structure to
        _info_to_mdblist_object's tmdb-keyed shape above. tmdb-keyed (not simkl_id):
        context-menu items reach here as Trakt objects, which carry tmdb_id, not
        simkl_id. extra_fields (e.g. {"rating": 8}) merges into the per-item dict
        alongside "ids", matching sync/ratings' {"rating": N, "ids": {...}} shape.
        """
        mediatype = item_information["mediatype"]
        extra_fields = extra_fields or {}
        if mediatype == "movie":
            return {"movies": [{**extra_fields, "ids": {"tmdb": item_information["tmdb_id"]}}]}
        if mediatype == "tvshow":
            return {"shows": [{**extra_fields, "ids": {"tmdb": item_information["tmdb_id"]}}]}
        if mediatype == "season":
            return {
                "shows": [
                    {
                        **extra_fields,
                        "ids": {"tmdb": item_information["tmdb_show_id"]},
                        "seasons": [{"number": item_information["season"]}],
                    }
                ]
            }
        if mediatype == "episode":
            return {
                "shows": [
                    {
                        **extra_fields,
                        "ids": {"tmdb": item_information["tmdb_show_id"]},
                        "seasons": [
                            {
                                "number": item_information["season"],
                                "episodes": [{"number": item_information["episode"]}],
                            }
                        ],
                    }
                ]
            }
        return None

    @staticmethod
    def _simkl_response_key(mediatype):
        """sync/history and sync/ratings (and their /remove counterparts) all report
        season/episode-granularity counts under <result_key>.episodes, not
        <result_key>.shows (simkl.apib: "the response always reports the actual
        count of episodes affected in added.episodes, so apps can verify the server
        expanded correctly") - the same field name holds under both added (add
        endpoints) and deleted (remove endpoints), only the top-level key differs."""
        return {"movie": "movies", "tvshow": "shows", "season": "episodes", "episode": "episodes"}.get(mediatype)

    @staticmethod
    def _confirm_simkl_write(response, mediatype, result_key="added"):
        """sync/history and sync/ratings report added.<type>: <int count> on success;
        their /remove counterparts (sync/history/remove, sync/ratings/remove) report
        the same int-count shape but under a top-level deleted key instead of added
        (verified in simkl.apib) - callers for the remove endpoints must pass
        result_key="deleted". NOT the same shape as sync/add-to-list, whose
        added.<type> is an array of echoed items rather than a count (see
        _simkl_set_status, which checks that endpoint's response directly rather
        than reusing this helper)."""
        key = TraktContextMenu._simkl_response_key(mediatype)
        return bool(response) and (response.get(result_key) or {}).get(key, 0) > 0

    def _simkl_mark_watched(self, item_information):
        payload = self._info_to_simkl_object(item_information)
        if payload is None:
            return
        response = self.simkl_api.post_json("sync/history", payload)
        if not self._confirm_simkl_write(response, item_information["mediatype"]):
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            g.log(f"Failed to mark item as watched on Simkl\nResponse: {response}")
            return

        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31092),
        )
        from resources.lib.modules import cross_sync

        cross_sync.mirror_watched("simkl", item_information, True)
        g.trigger_widget_refresh()

    def _simkl_mark_unwatched(self, item_information):
        payload = self._info_to_simkl_object(item_information)
        if payload is None:
            return
        response = self.simkl_api.post_json("sync/history/remove", payload)
        if not self._confirm_simkl_write(response, item_information["mediatype"], "deleted"):
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            g.log(f"Failed to mark item as unwatched on Simkl\nResponse: {response}")
            return

        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31093),
        )
        from resources.lib.modules import cross_sync

        cross_sync.mirror_watched("simkl", item_information, False)
        g.trigger_widget_refresh()

    def _simkl_rate(self, item_information):
        rating = xbmcgui.Dialog().input(
            g.get_language_string(31104), "", xbmcgui.INPUT_NUMERIC
        )
        if not rating:
            return
        try:
            rating = int(rating)
        except ValueError:
            return
        # Out-of-range values are silently rejected server-side (HTTP 201, item lands
        # in not_found instead of a 4xx) per simkl.apib - validated client-side here
        # so the user gets immediate feedback rather than a same-looking success
        # notification for a write that didn't actually happen.
        if not 1 <= rating <= 10:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            return

        payload = self._info_to_simkl_object(item_information, {"rating": rating})
        if payload is None:
            return
        response = self.simkl_api.post_json("sync/ratings", payload)
        if not self._confirm_simkl_write(response, item_information["mediatype"]):
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            g.log(f"Failed to rate item on Simkl\nResponse: {response}")
            return

        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31094),
        )
        from resources.lib.modules import cross_sync

        cross_sync.mirror_rating_from_simkl(item_information, rating)

    def _simkl_clear_rating(self, item_information):
        payload = self._info_to_simkl_object(item_information)
        if payload is None:
            return
        response = self.simkl_api.post_json("sync/ratings/remove", payload)
        if not self._confirm_simkl_write(response, item_information["mediatype"], "deleted"):
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            g.log(f"Failed to clear rating on Simkl\nResponse: {response}")
            return

        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31095),
        )
        from resources.lib.modules import cross_sync

        cross_sync.mirror_rating_clear_from_simkl(item_information)

    # Movies skip "watching"/"hold": simkl.apib documents movies as atomic - a
    # "watching" write is silently rewritten to "completed" server-side, and "hold"
    # has no defined behavior for movies.
    _SHOW_STATUSES = [
        ("watching", 31099),
        ("plantowatch", 31100),
        ("hold", 31101),
        ("dropped", 31102),
        ("completed", 31103),
    ]
    _MOVIE_STATUSES = [("plantowatch", 31100), ("completed", 31103), ("dropped", 31102)]

    def _simkl_set_status(self, item_information):
        mediatype = item_information["mediatype"]
        statuses = self._MOVIE_STATUSES if mediatype == "movie" else self._SHOW_STATUSES
        selection = xbmcgui.Dialog().select(
            f"{g.ADDON_NAME}: {g.get_language_string(31090)}",
            [g.get_language_string(sid) for _, sid in statuses],
        )
        if selection == -1:
            return
        to_status = statuses[selection][0]

        # A watch-status bucket is a whole-item property in Simkl, not a per-season/
        # episode one - season/episode force-resolve to their parent show's tmdb id,
        # mirroring _info_to_mdblist_list_object's identical force-resolve above.
        if mediatype == "movie":
            payload = {"movies": [{"to": to_status, "ids": {"tmdb": item_information["tmdb_id"]}}]}
            key = "movies"
        else:
            show_tmdb_id = (
                item_information["tmdb_id"] if mediatype == "tvshow" else item_information["tmdb_show_id"]
            )
            payload = {"shows": [{"to": to_status, "ids": {"tmdb": show_tmdb_id}}]}
            key = "shows"

        response = self.simkl_api.post_json("sync/add-to-list", payload)
        # sync/add-to-list's added.<type> is an array of echoed items, not a count
        # (verified in simkl.apib - different from sync/history/sync/ratings' shape,
        # see _confirm_simkl_write), so length is the success check here.
        if not response or not (response.get("added") or {}).get(key):
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            g.log(f"Failed to set Simkl status\nResponse: {response}")
            return

        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31096),
        )
        if to_status == "plantowatch":
            from resources.lib.modules import cross_sync

            cross_sync.mirror_watchlist_add_from_simkl(item_information)
        g.container_refresh()
        g.trigger_widget_refresh()

    def _simkl_remove_playback_history(self, item_information):
        from resources.lib.database.simkl_sync import SimklSyncDatabase

        mediatype = item_information["mediatype"]
        media_type = "movie" if mediatype == "movie" else "episode"
        tmdb_id = item_information["tmdb_id"] if mediatype == "movie" else item_information["tmdb_show_id"]
        season = item_information.get("season") if media_type == "episode" else None
        number = item_information.get("episode") if media_type == "episode" else None

        simkl_db = SimklSyncDatabase()
        rows = [
            r
            for r in simkl_db.get_bookmarks(media_type)
            if r["tmdb_id"] == tmdb_id
            and (media_type == "movie" or (r["season"] == season and r["number"] == number))
        ]

        if not rows:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(31098),
            )
            return

        cleared = False
        for row in rows:
            playback_id = row["playback_id"]
            if not playback_id:
                continue
            try:
                response = self.simkl_api.delete(f"sync/playback/{playback_id}")
            except Exception as e:
                g.log(f"Failed to remove Simkl playback session {playback_id}: {e}", "error")
                continue
            if response is not None and response.ok:
                cleared = True

        if not cleared:
            g.notification(
                f"{g.ADDON_NAME}: {self.header_text}",
                g.get_language_string(30287),
            )
            return

        simkl_db.remove_bookmark(tmdb_id, media_type, season, number)
        g.container_refresh()
        g.notification(
            f"{g.ADDON_NAME}: {self.header_text}",
            g.get_language_string(31097),
        )
        g.trigger_widget_refresh()

    # endregion
