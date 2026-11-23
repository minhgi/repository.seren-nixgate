"""The Trakt / MDBList / Simkl / WeTrakr manager on the calendar windows (Context Menu key on a row or tile).

The same manager the item's Context Menu opens in a list (gui/trakt_context_menu.py), run on top of the open calendar window.
Which of the four are offered follows the manager's own gates, so a scope that would open an empty menu is not listed.
"""

import json

import xbmcgui

from resources.lib.modules.globals import g

ACTION_CONTEXT_MENU = 117

STR_NO_OPTIONS = 31673

_SCOPE_HEADER_STRINGS = (("trakt", 30286), ("mdblist", 30961), ("simkl", 31085), ("wetrakr", 31699))


def available_scopes():
    """The managers that currently have options, with the gates TraktContextMenu.__init__ applies."""
    scopes = []
    if g.get_setting("trakt.auth") and g.get_bool_setting("trakt.enabled", False):
        scopes.append("trakt")
    if g.get_bool_setting("mdblist.enabled") and (g.get_setting("mdblist.auth") or g.get_setting("mdblist.apikey")):
        scopes.append("mdblist")
    if g.get_setting("simkl.auth") and g.get_bool_setting("simkl.enabled"):
        scopes.append("simkl")
    if g.wetrakr_active():
        scopes.append("wetrakr")
    return scopes


def show_args(trakt_show_id):
    return {"mediatype": "tvshow", "trakt_id": trakt_show_id}


def _action_args(args):
    """A row's args as the dict the manager needs (the sync databases keep them as a JSON string)."""
    if isinstance(args, str):
        from resources.lib.common import tools

        args = tools.deconstruct_action_args(args)
    if isinstance(args, dict) and args.get("mediatype") and args.get("trakt_id"):
        return args
    return None


def open_manager(args):
    """Offer the managers for the item behind `args` and run the chosen one. Returns True when an option was run
    (anything that asked for a refresh: the window should reload its data), False when nothing changed."""
    action_args = _action_args(args)
    scopes = available_scopes()
    if not action_args or not scopes:
        g.notification(g.ADDON_NAME, g.get_language_string(STR_NO_OPTIONS))
        return False
    if len(scopes) == 1:
        scope = scopes[0]
    else:
        headers = dict(_SCOPE_HEADER_STRINGS)
        choice = xbmcgui.Dialog().select(g.ADDON_NAME, [g.get_language_string(headers[s]) for s in scopes])
        if choice < 0:
            return False
        scope = scopes[choice]

    from resources.lib.common import tools
    from resources.lib.gui.trakt_context_menu import TraktContextMenu

    g.calendar_option_ran = False
    try:
        # an exception here would be swallowed by the window's callback and leave the key looking dead
        item_information = tools.get_item_information(action_args)
        if item_information is None:
            raise ValueError(f"no stored information for {json.dumps(action_args)}")
        TraktContextMenu(item_information, scope)
    except Exception as exc:  # pylint: disable=broad-except
        g.log(f"calendar_context_menu: the {scope} manager could not open for {action_args}: {exc}", "error")
        g.notification(g.ADDON_NAME, g.get_language_string(STR_NO_OPTIONS))
        return False
    return g.calendar_option_ran
