"""The presentation tier: the terminal interface onto a Mesh Collector archive.

`main()` is the entry point, and `scripts/run_console.py` is the thin wrapper
systemd-style invocation and local development both go through.

Read-only by construction: the connection this is handed was opened `mode=ro`
with `query_only` set, and the only file this process writes is its own record of
what you have already read, under `~/.local/state/mesh-console/`.

Everything the archive says is rendered with markup disabled — see
`mesh_console.ui.widgets`, which is where the rows are built. Layout and colours
live in `mesh_console.tcss` alongside this file.

Sending, when it is configured at all, goes out through `mesh_console.send`: this
file decides when to offer a compose box and what to say about the result, and
that module is the only one that knows a socket exists.
"""

from __future__ import annotations

import sqlite3
import webbrowser

from pathlib import Path
from time import monotonic
from typing import Any, Optional

from rich.style import Style
from rich.text import Text

from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Label, ListItem, ListView, Static

from mesh_console import __version__, db
from mesh_console.db import (
  REQUIRED_SCHEMA,
  ArchiveUnavailable,
  SchemaVersionMismatch,
  get_meta_bool,
  open_archive,
)
from mesh_console.config import Config
from mesh_console.state import (
  SCOPE_CHANNEL,
  SCOPE_CONVERSATION,
  SCOPE_DIRECT,
  ReadPositions,
)
from mesh_console.ui.format import (
  format_channel_label,
  format_coordinates,
  format_device_name,
  format_firmware,
  format_message_detail,
  format_node_detail,
  format_node_display_name,
  format_peer_label,
  format_reply_marker,
  format_timestamp,
  format_uptime,
)
from mesh_console.ui.rows import derive_rows
from mesh_console.ui.screens import LogViewerScreen, MenuScreen
from mesh_console.ui.tapbacks import is_emoji_only
from mesh_console.ui.theme import (
  DEFAULT_THEME, RXONLY_DARK, RXONLY_LIGHT, THEMES, palette_for,
)
from mesh_console.ui.widgets import (
  Breadcrumbs,
  ChannelItem,
  ConversationItem,
  Crumb,
  DetailView,
  EmptyItem,
  MenuFooter,
  MessageItem,
  MessageList,
  NodeItem,
  reconcile,
  striped,
)
from mesh_console.window import fetch_edge_window


# mesh-link is an optional dependency, so this import is the install answering
# the question "can this console send at all?" — a plain `uv sync` leaves it
# unanswerable and this console read-only, which is the point. Guarded here
# rather than in mesh_console.send so that module can be unambiguously a
# mesh-link client, and so nothing below has to import the protocol.
try:
  from mesh_console import send
except ImportError:
  send = None


# Consecutive failed polls before the interface says so, matching
# max_poll_failures in RxOnly's rxonly.js. One missed read of a database being
# written to is normal; three in a row is worth reporting.
MAX_POLL_FAILURES = 3

# How long the terminal has to have been behind another window before coming back
# to it counts as returning to a stale session rather than glancing away.
#
# **The poll never stopped while you were gone**, and that is the thing this number
# is about. What goes stale is what the poll deliberately declines to touch under a
# reader's cursor — `refresh_node_rows` says so at length — so a minute away leaves
# the node list in the order it was heard in a minute ago, missing every node first
# heard since. A minute is long enough that nothing a reader does within one glance
# rebuilds the list under them, and short enough that coming back from a coffee
# always does.
STALE_AFTER_SECONDS = 60.0

# How many missed polls it takes before the gap is read as this process having been
# stopped rather than as time passing: a suspended job, a closed laptop, a box that
# was too loaded to run us. Focus reporting cannot see any of those — the terminal
# never said anything, because from its side nothing happened — so this is the
# backstop under `on_app_focus`, and the only one in a terminal that does not report
# focus at all. Four intervals rather than two: a single late poll on a busy Pi is
# normal and must not rebuild the sidebar.
STALE_GAP_INTERVALS = 4

# How long a node list the reader is *holding* has to sit still before redrawing it
# counts as safe.
#
# **This is the only thing standing between the sidebar and what RxOnly does.**
# `update_nodes_list` (nodes.js:159) re-fetches the loaded window every poll,
# re-appends it in fresh order and inserts whatever is new, and the console has
# always refused because a terminal's place in a list is a cursor rather than a
# scroll offset — `refresh_node_rows` argues that at length and the argument still
# holds. But it holds against moving rows *under somebody's hand*, and most of the
# time there is no hand: the keyboard is in the message pane, or on the dashboard,
# or its owner is in a browser on another monitor. So the refusal is now conditional
# on there being a cursor to protect, and where there is not, the sidebar keeps up
# with RxOnly poll for poll.
#
# An interval was tried first and was the wrong shape. Rebuilding every two minutes
# meant a node first heard a minute ago was simply missing, which is how this was
# reported from the Pi: two lists side by side, identical but for the newest
# arrival. Half a minute here is not that interval — it only ever delays a redraw
# for a reader whose keyboard is in this list and who has just moved in it, and it
# does not apply at all to anyone else.
NODE_LIST_IDLE_SECONDS = 30.0

# How close the cursor has to get to either end of the loaded messages before
# the next page is fetched.
PAGE_TRIGGER_DISTANCE = 3

# How close to the end of the node list the reader has to *scroll* before the next
# page is fetched, in lines. The web's is `scrollHeight - 100` (nodes.js:346) — 100
# pixels against a 36-pixel row, a shade under three rows. A node row here is three
# lines: its name, the time it was last heard, and the blank line under it. So two
# rows is the same distance in the units a terminal has, and it is deliberately
# shorter than the pixel figure because a terminal row is a bigger fraction of the
# viewport than a browser row is.
NODE_SCROLL_TRIGGER_LINES = 6

# The same distance for the message list, and it exists for a different reason: the
# node list gained a scroll trigger because its scrollbar went away, and this one
# because *reading* went away from the cursor. A reader who never presses an arrow
# key scrolls, so scrolling has to be what fetches — see `messages_scrolled`. The
# cursor still triggers too, for the case scrolling cannot reach: at the top of the
# loaded window `scroll_y` is already 0, so walking the selection up onto the first
# row moves no pixels and would fetch nothing.
MESSAGE_SCROLL_TRIGGER_LINES = 6

# **Where the read line sits: this far up from the bottom of the message pane.**
#
# Jason's, and the rule it replaces was the cursor — a message was read when the
# cursor had been on or past it. Reading is now what RxOnly means by it: a message
# is read when the viewport has carried it far enough up the pane. Far enough is a
# fifth of the pane, so the bottom two messages or so of a scrolling channel are
# not claimed as read while they are still at the edge of vision.
#
# The floor matters more than the fraction. A fifth of a 40-line pane is 8 lines,
# about two messages, which is the intent; a fifth of a 12-line pane is 2 lines,
# less than one message, and the bottom message would count as read the moment it
# was fully on screen. Four lines is a message and its gap.
READ_MARGIN_FRACTION = 0.2
READ_MARGIN_MIN_LINES = 4

# How long the node filter waits after a keystroke before it queries. Matches
# search_debounce_delay in RxOnly's rxonly.js, in seconds rather than
# milliseconds. Textual's set_timer is enough for this; there is no thread.
NODE_FILTER_DEBOUNCE = 0.3

# What the mark at the right of the filter box is drawn with — RxOnly's
# `.nodes-search-clear`, which is an inline SVG there and has to be one character here.
#
# **U+2715, and the width is the whole of why.** This was going to be the enclosed
# `ⓧ` (U+24E7), which is the nicer glyph and is East Asian *Ambiguous* in
# `EastAsianWidth.txt`: Rich measures it as one cell, and a terminal configured to draw
# ambiguous glyphs double-wide draws it as two — so the mark would have been laid out
# for one column and painted over another. U+2715 is Neutral, which is one cell
# everywhere, and the stylesheet reserves a second column anyway.
FILTER_CLEAR_GLYPH = "✕"

# A filtered node list is fetched whole rather than paged, which is what RxOnly
# does (nodes.js:62) and is the simpler thing to be right about: a search that
# pages has to keep an offset in step with a query the reader is still typing.
# fetch_nodes clamps to this internally, so it is also the point past which a
# search stops being complete — see rebuild_nodes, which says so on screen
# rather than quietly showing the first thousand.
NODE_SEARCH_LIMIT = 1000


# What #main is showing. `viewing_messages` used to be the whole of this, and a
# boolean stopped being able to say once node detail and message detail existed.
# It survives as a property so that every gate written against it — the compose
# box, the poll, the message status line — keeps meaning what it meant.
VIEW_DASHBOARD = "dashboard"
VIEW_MESSAGES = "messages"
# The direct message index: one row per correspondent, which is what the sidebar's
# Direct Messages entry now opens. It is not VIEW_MESSAGES because nothing in it is
# a message — no pager, no read marker, no compose box — and every gate written
# against `viewing_messages` is right to exclude it.
VIEW_DIRECT = "direct"
VIEW_NODE = "node"
VIEW_MESSAGE = "message"

# What the flat direct message list is called, in the sidebar and in the trail a
# conversation reached through it shows. One constant because they have to agree:
# a breadcrumb naming a sidebar entry that reads differently is worse than no
# breadcrumb.
DIRECT_MESSAGES_LABEL = "Direct Messages"


# What the menu says about sending, in one word each. **Four states and not three,
# because the fourth is the common one:** a console configured correctly whose
# collector is simply not running right now is not misconfigured, and saying so
# would send a reader hunting for a settings mistake that does not exist. That
# happens on every collector restart.
#
# Set in `assess_sending` and `on_probe_finished`, beside the gates they describe,
# rather than worked out again from the same flags somewhere else — a second reading
# of the same conditions is a second thing to keep in step. `send_unavailable_reason`
# stays what it was: a sentence for a notification, too long for a menu row and too
# specific to be a status.
SEND_DISABLED = "disabled"          # ENABLE_SEND is off. Nothing is wrong.
SEND_MISCONFIGURED = "misconfigured"  # Asked for, and contradicted by the install
                                      # or by what the archive says the writer offers.
SEND_UNAVAILABLE = "unavailable"    # Set up correctly; nothing answered the socket.
SEND_CHECKING = "checking"          # The probe is in flight. See `probe_collector`.
SEND_ENABLED = "enabled"            # A collector answered.


# Where a clicked breadcrumb goes. Tags rather than callables, because they travel
# through a Rich style's metadata to reach `action_breadcrumb` — the widget renders
# the trail and this side decides what a step of it means.
#
# There are three and not four: a crumb naming the view you are already in is not a
# target at all, which is why `Breadcrumbs.set_trail` refuses to link the last one.
CRUMB_DASHBOARD = "dashboard"
CRUMB_MESSAGES = "messages"
CRUMB_DIRECT = "direct"




class MeshConsoleApp(App):
  """Reads one archive. Never writes it."""


  TITLE = "Mesh Console"

  CSS_PATH = "mesh_console.tcss"

  # Textual's command palette is off, and ctrl+p opens `MenuScreen` instead — a
  # short list of what this console can do, in place of a fuzzy search over it.
  # A search box over four commands is a quiz; Jason's call. Turning the palette
  # off is also what frees its binding: with this True, App owns ctrl+p and a
  # binding below would never see the key.
  ENABLE_COMMAND_PALETTE = False

  # Single letters, and a compose box that will contain the letter q.
  #
  # These are not modal and do not need to be. A focused Input consumes every
  # printable key before Textual looks for a binding, so typing `q` in the
  # compose box types a q — verified, because a compose box that quits mid-word
  # would be found within a minute of use and there is no warning of it in the
  # code. `escape` is not printable, so it bubbles, which is what makes it the
  # right key for leaving the box.
  # **What is advertised is decided per view, in `check_action`, and not here.** A
  # binding listed below is a key that exists; whether the footer offers it is a
  # question about what is on screen and where the focus is, which is the shape
  # Jason asked for. `q` is the only one with no view to answer to — and even it
  # steps aside while a text box has the keyboard.
  BINDINGS = [
    # `quit_console` rather than Textual's own `quit`, and the reason is the gating
    # rather than the quitting: `ctrl+q` is App's binding and its action *is* `quit`,
    # so hiding the letter by that name would have taken the framework's emergency
    # exit away from a reader inside the compose box — the one place they are most
    # likely to reach for it. The letter gets a name this app can gate; the control
    # key keeps the one it always had.
    Binding("q", "quit_console", "Quit"),
    # Hidden on the dashboard, where it is the view you are already in.
    Binding("d", "dashboard", "Dashboard"),
    # Offered in the message list, and only while there is something below the
    # cursor to jump to — see `can_jump_newest`. It was advertised in every view,
    # including two that have no message list at all.
    Binding("g", "jump_newest", "Jump to newest"),
    # **Not in the footer, and the key still works.** It was there when it was the
    # only thing that moved any of this: the sidebar's node rows, the collector
    # probe and the node ordering all sat still until it was pressed. The poll now
    # keeps the rows and the counts current on its own, so what is left for `r` is
    # the three things the poll deliberately declines to do under a reader's cursor
    # — reorder the node list, splice in nodes first heard since the last load, and
    # re-ask whether a collector is listening. That is maintenance rather than
    # navigation, and the footer is six keys of room best spent on where you can go.
    #
    # `show=False` hides it from `Footer`, which filters on `binding.show`, without
    # touching `Screen.active_bindings` — so the key is unchanged and `check_action`
    # still gates it. It is listed in the ctrl+p menu instead; see `action_menu`.
    Binding("r", "refresh", "Refresh", show=False),
    # The menu, on the key the command palette it replaced always had. `priority`
    # is what the palette's own binding carried and buys the same thing: the key
    # works from inside the compose box, where a reader is at least as likely to
    # want the log viewer as anywhere else.
    #
    # **`show=False`, and it is still in the footer** — docked right, in the
    # corner the palette's own key had, by `MenuFooter`. Hiding it was about the
    # left-hand run, where six single keys is the room there is and a chord
    # sitting among them reads as a seventh; the corner is a different piece of
    # the bar and was standing empty. What it says is "Commands" rather than
    # "Palette": what opens is a list of four, and a palette is the fuzzy search
    # over hundreds that this deliberately is not.
    Binding("ctrl+p", "menu", "Commands", show=False, priority=True),
    Binding("c", "compose", "Compose"),
    # In every view, because the node list is in every view — the sidebar does not go
    # away. It drops out only while a text box already holds the keyboard, which
    # includes the filter box itself.
    Binding("f", "filter_nodes", "Filter nodes"),
    # Out of the filter box and into what it filters, which is the other half of `f`.
    #
    # **The one binding in this list that exists *for* a focused text box**, and the
    # only reason it can: `Input` binds neither arrow key vertically, so `down` bubbles
    # out of the box and reaches this. It is offered nowhere else — see `check_action`,
    # which gates it on the filter having the keyboard, because `down` belongs to
    # whichever list or pane is focused everywhere else and must not be taken from it.
    #
    # Shown, unlike `tab`. While the filter has the keyboard every other letter drops
    # out of the footer, so there is room; and this is the moment the key is worth
    # naming, since a reader who has just typed a filter has nowhere else to be told
    # that the results are one keypress away.
    #
    # **`priority` for the footer's sake rather than the key's, and it is needed.**
    # `Input` is a `ScrollView` and therefore a `ScrollableContainer`, which binds
    # `down` to its own `scroll_down` with `show=False` — a real binding on a widget
    # that cannot scroll vertically. `Screen.active_bindings` keys its map by key and
    # keeps the first namespace to claim one, so the entry the footer would have shown
    # was being displaced by a hidden binding that does nothing, and only a priority
    # binding replaces an existing entry there. The action worked without this; the
    # advertisement did not.
    #
    # Priority means this is consulted before the focused widget in every view, which
    # is safe here for exactly one reason: `check_action` answers False unless the
    # filter box has the keyboard, and a refused binding falls through to the rest of
    # the chain — so `down` is still the message list's, the node list's and a detail
    # pane's wherever those have the focus. Same shape as the log viewer's `tab`.
    Binding("down", "step_into_nodes", "Node list", priority=True),
    # Out of the compose box and back into what it posts into, which is the other half
    # of `tab` reaching the box at all. Without these the box is a place the arrow keys
    # do nothing and `escape`/`shift+tab` are the only ways out — reachable in one
    # keypress and leavable only by knowing which of two other keys to reach for.
    #
    # **The two keys land in two places, and that is the point.** `up` goes to the
    # message the reader has read to, which is behind them; `down` goes to the first one
    # they have not, which is ahead. So the pair reads as "back to what I have seen" and
    # "on to what I have not" rather than as two ways to leave a box.
    #
    # On a detail page both simply hand the keyboard to the pane, because there is no
    # list to land in and a long message still has to be scrollable from here — see
    # `leave_compose_for_panel`. Same `priority` and the same reason as `down` above:
    # `Input` is a `ScrollableContainer` and binds both to its own scrolling.
    Binding("up", "compose_to_read", "Last read", priority=True),
    Binding("down", "compose_to_unread", "First unread", priority=True),
    # Sideways between the sidebar and the pane it opens, which is Jason's and is the
    # shape a tree control has: `right` steps into what the cursor names, `left` steps
    # back out to the list that named it. `enter` and `shift+tab` already do both and
    # keep doing them; this is the pair that needs no reaching.
    #
    # Gated hard on which panel has the keyboard, because both keys belong to a text
    # box the moment one has it — `left` in the compose box moves through what you have
    # typed, and a priority binding that took it would be a genuine fault rather than a
    # missing feature. `check_action` is what keeps that from happening.
    Binding("right", "open_highlighted_channel", "Open", show=False, priority=True),
    Binding("left", "leave_for_channels", "Channels", show=False, priority=True),
    # Reply, and shift-R rather than `r`, which has been refresh since Phase 3.
    # Renaming a verified binding to free up the letter would have cost more than
    # a shift key does, and a mistyped `r` refreshes, which is harmless.
    Binding("R", "reply", "Reply"),
    # The map link, without a mouse. A terminal running a Textual app has mouse
    # reporting on, which means the *terminal* no longer sees a click as its own:
    # cmd-click and the right-click "Open Link" menu are the terminal's features and
    # it has handed the button over. So clicking is the path that depends on what is
    # installed, and this is the one that always works. Offered only on a node that
    # reported a position — see `check_action`.
    Binding("o", "open_map", "Open map"),
    # Only ever reaches an action from a detail pane: a ListView and an Input both
    # claim `enter` before an app binding is consulted, so this adds a route out of
    # node detail without touching what enter means in the three lists or in the
    # compose box. Advertised only where it goes somewhere, like `c` and `escape`.
    Binding("enter", "open_conversation", "Conversation"),
    # Shown, unlike the compose-only `escape` it replaced. It was reasonable to
    # hide a key whose whole job was leaving a box you had deliberately entered;
    # it is the way out of a detail view, and a reader who has walked into one
    # from the node list has no other obvious way back.
    Binding("escape", "back", "Back"),
    # Around the panels, in the order the reader asked for rather than the order
    # they happen to be built in — see `focus_ring`.
    #
    # **`priority` because `Screen` already owns this key**: its own
    # `Binding("tab", "app.focus_next")` sits between the focused widget and this
    # class in the binding chain, so an ordinary binding here would never be
    # consulted. Priority bindings are checked from the App down, which is what makes
    # this one win — and is also why `focus_ring` had to join `MODAL_SHADOWED`: the
    # log viewer binds `tab` to its level filter, at priority, and would otherwise
    # have lost the key to this.
    #
    # `show=False` on both. The footer's left-hand run is six single keys of room and
    # these are a key and a chord that do one thing between them; a reader who presses
    # `tab` finds out what it does immediately and permanently.
    Binding("tab", "focus_ring(1)", "Next panel", show=False, priority=True),
    Binding(
      "shift+tab", "focus_ring(-1)", "Previous panel", show=False, priority=True
    ),
  ]

  # Which actions are reached by a key a text box would eat first — every letter
  # binding above, plus `enter`, which an Input claims as its own submit. While one of
  # the two boxes has the keyboard none of these can be reached, so the footer stops
  # offering them; `escape` is the only way out and is deliberately absent from the
  # set. See `check_action`.
  #
  # **Written out rather than derived from BINDINGS**, because what belongs here is
  # the set of actions this class owns and not the set a text box must leave alone:
  # `quit`, the copy and focus bindings, and this app's own `menu` are all reached by
  # control-key combinations that a text box passes through. A guard that caught
  # them by accident would take `ctrl+q` and `ctrl+p` away from a reader in the
  # compose box, which it did once.
  TEXT_BOX_SHADOWED = frozenset({
    "quit_console",
    "dashboard",
    "jump_newest",
    "refresh",
    "compose",
    "filter_nodes",
    "reply",
    "open_map",
    "open_conversation",
  })

  # And the set a *modal* takes the keyboard from, which is the same idea with three
  # more members: `escape` has a job inside every modal so the app's `back` must
  # not also fire, ctrl+p opening a second menu over the first helps nobody, and
  # `tab` is the log viewer's level filter — a screen with one focusable list in it
  # has no use for a ring around three panels it is covering. `ctrl+q` is deliberately
  # absent, as above.
  #
  # `focus_ring` is in this set and deliberately *not* in `TEXT_BOX_SHADOWED`: `tab`
  # is not a printable key, so neither Input eats it, and tab out of the filter box or
  # the compose box is exactly how a reader gets back to a panel.
  # The four arrow bindings join it too. Each is gated on which panel has the keyboard,
  # and while a modal is up `self.focused` is that screen's — so those gates already
  # answer no and this set is belt to their braces. It is here so that adding a fifth
  # arrow binding gated some other way cannot quietly reach across a dialog.
  MODAL_SHADOWED = TEXT_BOX_SHADOWED | {
    "back", "menu", "focus_ring",
    "step_into_nodes", "compose_to_read", "compose_to_unread",
    "open_highlighted_channel", "leave_for_channels",
  }

  # Which heading names the panel a widget belongs to, for the focus rule. The main
  # pane's is the breadcrumb trail, which is the one label above it and already says
  # which of the three panes is showing.
  #
  # The compose box is in here rather than left out, and the filter box points at the
  # Nodes heading rather than at nothing: both are *inside* a panel, so the rule keeps
  # naming the panel while the keyboard is in the box that belongs to it. Which of the
  # two the keyboard is actually in is a question the boxes answer themselves — see
  # `#node-filter:focus` and `#compose:focus`, both of which change their own fill.
  PANEL_HEADINGS = {
    "channels": "#channels-heading",
    "nodes": "#nodes-heading",
    "node-filter": "#nodes-heading",
    "messages": "#breadcrumbs",
    "dashboard-pane": "#breadcrumbs",
    "detail-pane": "#breadcrumbs",
    "compose": "#breadcrumbs",
  }


  # The two text boxes, in the order an unclaimed paste should prefer them — the
  # same pair `typing_in_a_box()` names, and here the order carries the meaning.
  # The node filter is in the sidebar and so is on screen in every view, which
  # makes "the first box on screen" the wrong question and this list the right
  # one. See `_paste_target()`, which is the only reader.
  PASTE_TARGETS = ("#compose", "#node-filter")

  # Every heading the rule can be drawn under, which is what makes clearing it one
  # loop rather than a branch per panel.
  PANEL_HEADING_SELECTORS = ("#channels-heading", "#nodes-heading", "#breadcrumbs")


  def __init__(self, connection: sqlite3.Connection) -> None:
    super().__init__()

    # Here rather than in on_mount, and that is not a style preference: the
    # stylesheet is parsed before mount, and it resolves `$rx-*` against the
    # active theme's variables. Registered later, every one of those is an
    # undefined variable and the whole sheet fails to load. Both themes are
    # registered so the ctrl+p menu can switch between them, which is the
    # closest a terminal gets to the web interface's prefers-color-scheme.
    for palette_theme in THEMES:
      self.register_theme(palette_theme)
    self.theme = DEFAULT_THEME

    # **And every other theme is taken back out, because picking one crashed the
    # app.** `App.__init__` registers Textual's own dozen, and the command palette
    # this app carried at the time listed whatever was registered — so "Change
    # theme" offered `nord`, `dracula` and the rest, none of which define a single
    # `$rx-*` variable. Choosing one re-parsed this stylesheet against a theme it
    # is not written for, and the parse does not degrade: it failed on line 36,
    # `background: $rx-error`, and took the console down. Jason found this the way
    # these are always found. The palette has since given way to the ctrl+p menu,
    # which only ever offers the two themes below — this stays as the backstop
    # that keeps a stray `self.theme = "nord"` from anywhere ever being fatal.
    #
    # Unregistering rather than filtering what offers them, because the offering
    # reads `available_themes` and there is no hook between the two. Nothing is
    # lost that was working: this project carries RxOnly's palette on purpose, and
    # a theme that replaces half of it with Textual's own is not a theme of this
    # interface — it is `#device-bar` in our colours next to a `ListView` in
    # someone else's. Light and dark are both still here, which is the choice that
    # was actually being offered.
    #
    # Done after the two above are registered and one of them is active, so this
    # never removes the theme in force. `get_css_variables` below is the other
    # half — this stops anything leading anyone there, that stops it being fatal.
    for name in list(self.available_themes):
      if name not in (theme.name for theme in THEMES):
        self.unregister_theme(name)

    self.conn: Optional[sqlite3.Connection] = connection
    self.positions = ReadPositions.load()

    # The position of whichever node detail is on screen, for the map link's click
    # action. None when the view is not a node, or the node reported no position.
    self.current_map_url: Optional[str] = None

    self.show_direct_messages: bool = Config.get("SHOW_DIRECT_MESSAGES", False)
    # Passed to every node *list* read, and to no node *lookup*: a node detail
    # opened from a row, or a sidebar row refreshed by id, resolves regardless.
    self.list_unnamed_nodes: bool = Config.get("LIST_UNNAMED_NODES", False)
    self.page_size: int = int(Config.get("PAGE_SIZE", 50))
    self.poll_interval: float = float(Config.get("POLL_INTERVAL", 10))

    # The loaded window of messages, oldest first, and where its edges are.
    #
    # `messages` is the archive's list and stays that way. `rows` is what the
    # ListView actually holds, derived from it — a tapback is absorbed into the
    # message it answers rather than being a row of its own, so the two are no
    # longer the same length and no index means the same thing in both. Every
    # index that reaches the ListView is a row index; every cursor and read
    # position is about a message. `rebuild_rows()` is the only thing that
    # crosses between them.
    self.messages: list[dict[str, Any]] = []
    self.rows: list[dict[str, Any]] = []
    self.current_is_dm: bool = False
    self.current_channel_index: Optional[int] = None
    # Which peer's conversation is open, and None in every other view — including
    # the flat direct message list, which is direct messages without being a
    # conversation with anybody.
    #
    # **A separate field rather than a widened `current_channel_index`.** The
    # brief warned that a parameter named for a channel index while holding a node
    # id reads fine for a session and then costs an afternoon, and the two are
    # genuinely different things: a channel index is an int and the encryption
    # context a message rides on, a peer is a hex string and who it is addressed
    # to. A direct message has both at once, so neither can stand in for the
    # other. It also means `current_channel_index` never has to be renamed, and
    # the thirteen places that thread it through are untouched.
    self.current_peer: Optional[str] = None
    self.current_channel_label: str = ""
    self.oldest_cursor: Optional[tuple[int, int]] = None
    self.newest_cursor: Optional[tuple[int, int]] = None
    self.has_more_older: bool = False
    self.has_more_newer: bool = False
    self.is_loading: bool = False

    # Where the pane scrolls to when a channel opens *that still has something
    # unread in it*: the row holding the message this reader last read, put back on
    # the read line so that everything above it is read and the unread messages sit
    # below. Named by message and resolved to a row by `rebuild_rows()`, because
    # which row holds a message depends on what else is loaded. A channel with
    # nothing unread ignores this and opens at its end — see `messages_fully_read`.
    self.resume_message_id: Optional[int] = None
    self.resume_index: int = 0

    # The newest row the read line has passed — the message this reader is up to.
    # **Nothing on screen shows it**, and that is the point: it is what the first
    # `up` or `down` selects, so a reader who starts navigating starts from where
    # they had read to rather than from the top of the loaded window.
    self.read_row: int = 0

    # message_id -> the index of the row that draws it. A tapback maps to the
    # row of the message it was absorbed into, so every loaded message has an
    # entry whether or not it has a row of its own.
    self.row_of_message: dict[int, int] = {}

    # The message whose detail is open, so leaving it comes back onto that message
    # rather than onto wherever reading has got to. See `show_message_detail`.
    self.detail_message_id: Optional[int] = None

    # **True while a channel is being put where it belongs, and it exists to stop
    # the act of opening one from reading it.** Rendering a window and scrolling it
    # to the resume point cannot happen in the same breath — the rows have to be
    # laid out before they can be measured — and in between them the pane is sitting
    # at whatever scroll position the rebuild left, firing scroll events. Marking
    # read off those intermediate positions advanced the marker past messages nobody
    # had seen, intermittently, depending on how many refreshes landed first.
    # See `mark_read_from_viewport` and `position_message_pane`.
    self.positioning: bool = False

    # Which act of positioning owns the flag. The scroll that lands a channel is
    # deferred work, and deferred work from one open can still be queued when the
    # next open starts — rapid channel switches did exactly that. A chain that
    # finds the epoch has moved on returns without touching anything: it is not
    # its pane any more, and above all it must not lower a gate a newer open is
    # relying on. Raised by `begin_positioning`, checked wherever a deferred
    # scroll fires.
    self.positioning_epoch: int = 0

    # Which of #main's three panes is showing, where `escape` goes back to, and
    # which node the detail pane is on — a node's battery and last_seen move
    # while you are looking at them, so the poll keeps that one pane current.
    # A message does not change once it is archived, so there is no equivalent
    # for the other detail view, and RxOnly refreshes exactly the same one.
    self.view: str = VIEW_DASHBOARD
    self.return_view: str = VIEW_DASHBOARD
    self.detail_node_id: Optional[str] = None

    # Which channels the sidebar is showing, so an unread count is asked for
    # exactly those rather than for every position ever recorded — a channel the
    # collector has stopped tracking still has a read position on disk.
    self.tracked_channel_indexes: list[int] = []

    # The node list: what it is filtered by, how much of it is loaded, and how
    # much there is. Absent a filter it pages; with one it is fetched whole.
    self.node_search: str = ""
    self.node_offset: int = 0
    self.node_total: int = 0
    self.node_matches: int = 0
    self.nodes_loading: bool = False
    self.node_filter_timer: Optional[Any] = None

    self.poll_failures: int = 0
    self.positions_dirty: bool = False

    # ------------------------------------------------------- the stale session
    #
    # When the terminal last went behind another window, and whether the message
    # pane was following the live end at that moment. Both are None/False while
    # the terminal has the focus.
    #
    # **Whether it was following has to be caught at the blur and cannot be asked
    # again on the way back.** `has_more_newer` is the answer to "is there
    # something below the loaded window", and the poll goes on running the whole
    # time you are away — so on a busy channel it is legitimately True by the time
    # you return, whether you left reading the live end or left paged back. Asked
    # then, every return looks like a reader who had deliberately scrolled up, and
    # the one case that most wants catching up is the one that never would be.
    self.blurred_at: Optional[float] = None
    self.blurred_following: bool = False

    # When `poll()` last ran. A gap far longer than the interval is not slow
    # polling, it is this process not having been running — see STALE_GAP_INTERVALS.
    self.last_poll: Optional[float] = None

    # When the reader last moved in the node list, by cursor or by wheel. None
    # until they touch it, which is the state a redraw is safest in — and set back
    # to None after every rebuild, because a rebuild scrolls and re-highlights the
    # list itself and none of that is the reader doing anything. See
    # `rebuild_nodes`, and `positioning` for the same problem in the message pane.
    self.node_list_touched: Optional[float] = None

    # A resync that is owed but has not been run, because something was on top of
    # the main screen when it was asked for. Redrawing the screen underneath a
    # modal is work nobody can see, and the log viewer is exactly where a reader
    # sits while they are away from the console. The poll picks it up once the
    # stack is back down to one; see `request_resync`.
    self.resync_owed: bool = False
    self.resync_following: bool = False

    # When the poll first found something on top of the main screen, and therefore
    # how much of the screen underneath it has skipped drawing. None while the main
    # screen is on top, and None again the moment it is back — see `poll_hidden`
    # and `screen_returned`.
    #
    # **Measured from the first skipped poll rather than from the push**, which
    # under-reports by up to one interval and is the right trade: a push hook would
    # have to be maintained at every call site that ever opens a screen, and what
    # this number is compared against is a minute. It also makes the arithmetic
    # honest in the one case that matters — a menu opened and closed between two
    # polls skipped nothing, so it left nothing to catch up on, and this stays None.
    self.hidden_since: Optional[float] = None

    # Watched so a swapped device or a rebuilt database is noticed rather than
    # rendered as though nothing happened.
    self.known_local_node_id: Optional[str] = None
    self.known_first_seen: Optional[int] = None

    # Which device this archive belongs to, which is what makes a message row
    # yours. Read from meta rather than inferred, and kept apart from
    # known_local_node_id above: that one answers "has the device changed?", this
    # one answers "who am I?", and conflating them would mean the first swap
    # detection silently decided how messages are attributed.
    self.local_node_id: Optional[str] = None

    # Sending. Three independent gates, all of which fail closed, and the box is
    # offered only when all three are open:
    #
    #   send_configured   this console was asked to offer it (ENABLE_SEND)
    #   send is not None  mesh-link is installed, so it is possible at all
    #   send_available    a collector answered a status request just now
    #
    # The third is the only authoritative one — the collector may have stopped
    # since it last published anything — and it is also the only one that costs a
    # socket round trip, so meta.accepts_transmit is consulted first to avoid
    # asking at all when the answer is already known to be no.
    # Read from Config directly rather than through mesh_console.send, because
    # the interesting case is exactly the one where that module could not be
    # imported: sending asked for, and nothing installed to do it with.
    self.send_configured: bool = bool(Config.get("ENABLE_SEND", False))
    self.send_available: bool = False
    self.send_unavailable_reason: Optional[str] = None
    # One word for the menu, from the same gates. Starts at the truth for a console
    # that has not asked yet: `assess_sending` runs at mount and settles it.
    self.send_state: str = SEND_DISABLED
    self.sender: Optional[Any] = None
    self.send_in_flight: bool = False

    # Which message the next send answers, held as the whole row rather than an
    # id so the box can say who wrote it and what it said without going back to
    # the archive. Cleared by escape, by a completed send, and by leaving the
    # view — a reply to a message in one conversation must not follow the reader
    # into another.
    self.reply_to_message: Optional[dict[str, Any]] = None

    # Which channel index a direct message rides on, which is its encryption
    # context rather than its destination. The collector forwards it to sendText
    # unchanged, and `SendTextRequest.channel_index` defaults to 0 — right on this
    # device and wrong on one whose primary channel is not 0. The collector
    # publishes the answer, so it is read rather than assumed. Set at mount.
    self.primary_channel: int = 0




  def get_css_variables(self) -> dict[str, str]:
    """Textual's variables, with this palette underneath them always.

    **The floor under the crash above.** `mesh_console.tcss` is written against
    `$rx-*` throughout, and those exist only because the active theme carries them —
    so a theme that does not is not a different-looking console, it is a stylesheet
    that will not parse and an app that stops. Unregistering the built-ins closes the
    route a reader can take to that; this closes the rest, including the ones no
    comment can anticipate — a Textual release adding a theme, a future setting
    restoring one by name, a line of code assigning `self.theme`.

    Underneath rather than on top: a real rxonly theme's own values win, which is
    also why this is not a way to avoid keeping the two in step. The values are the
    same ones by construction — `palette_for` returns the very dictionaries the
    themes were built from.

    Light or dark is taken from whatever theme is active, so the fallback at least
    lands the right way up in a surround this project did not choose.
    """
    variables = {**palette_for(self.current_theme.dark), **super().get_css_variables()}
    # `super()` sets this to its own dict on the way past; the stylesheet gets what
    # is returned here, so anything reading the attribute should see the same thing.
    self.theme_variables = variables
    return variables




  @property
  def viewing_messages(self) -> bool:
    """Whether #main is showing a channel's messages.

    A property rather than a field so that no code path can leave it disagreeing
    with `self.view`. It was a plain boolean until this slice, when node detail
    and message detail made "not the dashboard" and "the messages" stop being
    the same statement — and the gates that read it (`compose_available()`,
    `update_compose()`, the poll, the message status line) all wanted the
    narrower one. Keeping the name means none of them had to change.
    """
    return self.view == VIEW_MESSAGES




  @property
  def monochrome(self) -> bool:
    """Whether this session has no colour to say anything with.

    **Only the cases that are declarations, never a guess.** `no_color` is Textual's
    own reading of the `NO_COLOR` environment variable, which the reader sets on
    purpose and which Textual then acts on by stripping colour out of every cell;
    `console.color_system` comes back None for a terminal that reports itself dumb.
    Both are certain.

    What this deliberately does *not* try to answer is whether a terminal claiming
    truecolor really shows it. It cannot be known — `TERM=vt100` reports truecolor
    here and Textual writes truecolor escapes regardless — and neither can the case
    that actually matters most, a reader who sees colour but cannot tell
    `$rx-outbound` from `$rx-accent-node`. Guessing at either would mean showing the
    glyph on sessions that do not need it, which is the thing this exists to avoid.
    Forcing it on for good is `return True`.

    Read once per row that asks, which is cheap: both halves are attributes settled
    at startup.
    """
    return self.no_color or self.console.color_system is None


  @property
  def viewing_detail(self) -> bool:
    """Whether #main is showing one node or one message."""
    return self.view in (VIEW_NODE, VIEW_MESSAGE)




  @property
  def viewing_conversation(self) -> bool:
    """Whether #main is showing the direct messages with one particular peer.

    **This is the flag that "is a DM" used to be, and they have come apart.**
    `current_is_dm` says which table is being read, and stays exactly that — the
    flat list and a conversation are both `direct_messages`, and every read that
    picks a table still asks only that. What changed is that "direct messages" no
    longer implies "no recipient": a conversation has one, which is the whole point
    of it, and `compose_available()` is the gate that cared.

    A conversation is not a fifth view. It is messages-shaped — same list, same
    pager, same read marker, same `escape` — so it reuses VIEW_MESSAGES with a peer
    set, which is what Phase 3's slice 2 made `viewing_messages` a property in
    order to allow. A fifth view would have been a second thing to keep in step
    with the four gates that branch on being in the messages at all.
    """
    return self.viewing_messages and self.current_peer is not None




  def current_scope(self) -> tuple[str, Optional[Any]]:
    """Which read position the open view is about: a scope and its key.

    Three kinds of place, and the one function that decides which of them is open,
    so that no caller has to reassemble the answer out of two booleans and a
    string. The flat direct message list and a conversation are deliberately
    different scopes over the same rows — see `ReadPositions` for why folding them
    together would over-mark.
    """
    if self.current_peer is not None:
      return (SCOPE_CONVERSATION, self.current_peer)
    if self.current_is_dm:
      return (SCOPE_DIRECT, None)
    return (SCOPE_CHANNEL, self.current_channel_index)




  def peer_of(self, message: dict[str, Any]) -> Optional[str]:
    """Which node the other end of a direct message is.

    `to_node if from_node == local else from_node`, which is the whole rule. It
    needs `local_node_id` — the archive's answer to "who am I", read from `meta` at
    mount and moved by `check_for_state_change()` on a device swap — and not
    `known_local_node_id`, which answers "has the device changed?" and would mean
    swap detection silently decided who a conversation was with.

    None when the archive has named no device, because then there is no way to tell
    which end of the row is the peer: an unattributable row cannot be assigned to a
    conversation any more than it can be claimed as yours.
    """
    if self.local_node_id is None:
      return None

    if message.get("from_node") == self.local_node_id:
      return message.get("to_node")
    return message.get("from_node")




  # ---------------------------------------------------------------- composition


  def compose(self) -> ComposeResult:
    yield Static(id="connection-error")
    # `markup=False` like every other widget that renders what the archive says. It
    # is belt-and-braces now that `update_device_bar` builds a `Text`, and it is the
    # declaration that stops the next person putting an f-string back.
    yield Static(id="device-bar", markup=False)

    with Horizontal(id="body"):
      with Vertical(id="sidebar"):
        # Both headings carry an id, because both are looked up by one: they are
        # where the focus rule is drawn. See `PANEL_HEADINGS`.
        yield Label("Channels", id="channels-heading", classes="panel-heading")
        yield ListView(id="channels")
        yield Label("Nodes", id="nodes-heading", classes="panel-heading")
        # Between the heading and the list it filters, which is where RxOnly
        # puts it and where it reads as belonging to the list below rather than
        # to the sidebar as a whole.
        #
        # The row exists to put the clear mark at the box's right edge; the box
        # keeps its own id, which is what every other reference to it goes
        # through — `PANEL_HEADINGS`, `PASTE_TARGETS`, the focus ring, and both
        # `Input` handlers all name `#node-filter` and none of them care what it
        # is wrapped in. See the stylesheet for why this is two widgets rather
        # than one with something drawn over it.
        with Horizontal(id="node-filter-row"):
          yield Input(id="node-filter", placeholder="Filter nodes…")
          yield Static(id="node-filter-clear", markup=False)
        yield ListView(id="nodes")

      with Vertical(id="main"):
        # Says where you are when nothing in the sidebar is highlighted, which
        # is exactly the state the two detail views introduced.
        yield Breadcrumbs(id="breadcrumbs", markup=False)
        yield VerticalScroll(Static(id="dashboard", markup=False), id="dashboard-pane")
        yield MessageList(id="messages")
        # One pane for both detail views. They differ in what they render and
        # in nothing else — same scrolling, same escape, same absence of a
        # compose box — so a second pane would be a second thing to keep in step.
        yield VerticalScroll(DetailView(id="detail", markup=False), id="detail-pane")
        yield Static(id="messages-status", markup=False)
        # Below the status line, so a "newer messages below" note stays attached
        # to the list it is about. Hidden until there is somewhere to send.
        yield Input(id="compose")
        yield Static(id="compose-status", markup=False)

    yield MenuFooter()




  def on_mount(self) -> None:
    self.show_view(VIEW_DASHBOARD)
    self.set_breadcrumbs("Dashboard")

    # The clear mark's content, written once. Only its visibility moves after this —
    # see `update_node_filter_clear` — so the `@click` is attached in one place and
    # never rebuilt per keystroke.
    #
    # A `Text` with a `@click` meta rather than a marked-up string, which is how every
    # other clickable thing in this file is built and for the same two reasons: a span
    # carrying one is a link as far as Textual is concerned, which is what earns it the
    # `link-*-hover` treatment, and nothing goes through an action string to be parsed.
    # `app.`-qualified because Textual brokers a `@click` from the widget it landed on,
    # and a bare name would resolve against that `Static`, which has no such action.
    self.query_one("#node-filter-clear", Static).update(
      Text(FILTER_CLEAR_GLYPH, style=Style(meta={"@click": "app.clear_node_filter"}))
    )

    # Scrolling the node list pages it, the way the wheel does on the web. Here
    # rather than in `compose` because the widget has to exist to be watched, and
    # `init=False` because a list at the top of itself has not been scrolled anywhere.
    self.watch(
      self.query_one("#nodes", ListView), "scroll_y", self.nodes_scrolled, init=False
    )

    # And scrolling the message list both reads it and pages it, which is the whole
    # of what moved off the cursor. Same reactive, same reason it is a watch rather
    # than a handler: `scroll_y` moves for the wheel, the arrow keys and a drag
    # alike, and an event handler would have had to name each of those.
    self.watch(
      self.query_one("#messages", ListView), "scroll_y", self.messages_scrolled,
      init=False,
    )

    # And the focus rule, on the one reactive every way of moving focus goes through.
    # `init` is left alone here, unlike the two above: the initial value is a fact
    # worth painting rather than a scroll that has not happened, and `AUTO_FOCUS` has
    # put the keyboard in the channel list by the time this runs.
    self.watch(self.screen, "focused", self.focus_moved)

    # Who this archive says the attached device is. Every message row is compared
    # against it to decide whether it is one of yours, and it is which end of a
    # direct message the peer is.
    #
    # **Before the sidebar is built, and that ordering is load-bearing now.** It used
    # to be read after, which was harmless while nothing drawn at mount depended on
    # it — until the unread counts started excluding this device's own messages.
    # Read second, `local_node_id` was still None for the first paint, so a channel
    # you had talked on came up with its own messages counted as unread and quietly
    # corrected itself ten seconds later on the first poll.
    self.local_node_id = self.read(db.get_meta, "local_node_id")

    self.refresh_sidebar()
    self.refresh_dashboard()

    # The collector's own primary channel, which a direct message is encrypted
    # against. 0 on this device and on the fixture; read rather than assumed
    # because a device configured otherwise would send DMs on the wrong one, and
    # the collector already publishes the answer.
    self.primary_channel = self.read(db.get_meta_int, "primary_channel", 0) or 0

    # Remember which device this archive belongs to, so the first poll compares
    # against it rather than reporting a swap that never happened.
    stats = self.read(
      db.fetch_stats, self.show_direct_messages,
      list_unnamed=self.list_unnamed_nodes,
    )
    if stats is not None:
      self.check_for_state_change(stats)

    self.assess_sending()

    self.set_interval(self.poll_interval, self.poll)




  def on_unmount(self) -> None:
    self.flush_positions()
    if self.conn is not None:
      self.conn.close()
      self.conn = None




  # ------------------------------------------------------------------ the reads
  #
  # SQLite reads of a local file are fast enough to run on the event loop: the
  # collector keeps a bounded archive, and every query here is either a count or
  # one indexed page. A read that fails is a connection problem, handled in one
  # place by dropping the connection so the next poll reopens it.


  def read(self, operation, *args, **kwargs):
    """Run one archive read, or return None if the archive can't be reached."""
    if self.conn is None:
      return None

    try:
      return operation(self.conn, *args, **kwargs)
    except sqlite3.Error as e:
      self.log(f"archive read failed: {e}")
      self.drop_connection()
      return None




  def drop_connection(self) -> None:
    if self.conn is not None:
      try:
        self.conn.close()
      except sqlite3.Error:
        pass
    self.conn = None




  def reconnect(self) -> bool:
    """Try to reopen the archive. The schema is re-checked on the way in."""
    try:
      self.conn = open_archive()
    except Exception as e:
      self.log(f"could not reopen the archive: {e}")
      self.conn = None
      return False
    return True




  # -------------------------------------------------------------------- sidebar


  def refresh_sidebar(self) -> None:
    stats = self.read(
      db.fetch_stats, self.show_direct_messages,
      list_unnamed=self.list_unnamed_nodes,
    )
    channels = self.read(db.fetch_channels)

    if stats is None or channels is None:
      return

    self.update_device_bar(stats)
    self.rebuild_channels(channels, stats)
    self.reload_nodes()




  def unread_counts(self) -> dict[int, int]:
    """How many messages are waiting in each channel, by channel index.

    Empty when the archive can't be read, which shows as no unread badges rather
    than as wrong ones — the same failure this reader takes everywhere else.
    """
    cursors = {
      index: cursor
      for index in self.tracked_channel_indexes
      if (cursor := self.positions.cursor(SCOPE_CHANNEL, index)) is not None
    }
    # Passed so the count leaves out what this device sent. None until the archive
    # names a device, which is the case where nothing can be attributed at all.
    return self.read(
      db.fetch_unread_channel_counts, cursors, self.local_node_id
    ) or {}




  def unread_conversation_counts(self) -> dict[int, int]:
    """How many direct messages are waiting in each conversation, by peer.

    One query for every peer, on the same `VALUES`-join pattern the channel counts
    use. Empty when the archive cannot be read or has named no device — without a
    local node there is no way to say which end of a row the peer is, so there are
    no conversations to count rather than wrong ones.
    """
    if self.local_node_id is None:
      return {}

    return self.read(
      db.fetch_unread_conversation_counts,
      self.local_node_id,
      self.positions.conversation_cursors(),
    ) or {}




  def rebuild_channels(
    self,
    channels: list[dict[str, Any]],
    stats: dict[str, Any],
  ) -> None:
    channel_list = self.query_one("#channels", ListView)

    self.tracked_channel_indexes = [c["channel_index"] for c in channels]
    unread = self.unread_counts()

    # Reused the way the node list's rows are, and for the same reason — see
    # `reconcile`. This list is short and rebuilt far less often, so the blink was
    # never the complaint here; it is the same blink all the same, and one list
    # drawing itself by a different rule from the one beside it is how the two drift
    # apart. Keyed on the channel index, with the direct message row keyed apart from
    # it: a channel index of None is what that row has, and `is_dm` is what makes it
    # unambiguous rather than the one row that happens to have no index.
    showing = {
      (item.is_dm, item.channel_index): item
      for item in channel_list.children
      if isinstance(item, ChannelItem)
    }

    wanted: list[ListItem] = []

    for channel in channels:
      index = channel["channel_index"]
      label = format_channel_label(channel)
      count = channel.get("message_count", 0)
      item = showing.get((False, index))
      if item is None:
        item = ChannelItem(label, channel_index=index, count=count, unread=unread.get(index, 0))
      else:
        item.set_row(label, count, unread.get(index, 0), "")
      wanted.append(item)

    if self.show_direct_messages:
      # Displaying them is this process's decision; whether there are any to
      # display is the collector's, published in meta. Saying which it is beats
      # an unexplained empty list.
      stores_dms = self.read(get_meta_bool, "stores_direct_messages")
      note = "" if stores_dms else "not archived"
      # The drawn count, not the archive one: this row's number sits next to a list
      # that folds reactions into pills, and the Network Stats block is where the
      # archive figure belongs. `fetch_stats` returns both and says why.
      total_dms = stats["stats"]["drawn_direct_messages"]
      unread_dms = self.unread_direct_count()

      item = showing.get((True, None))
      if item is None:
        item = ChannelItem(
          DIRECT_MESSAGES_LABEL,
          is_dm=True,
          count=total_dms,
          unread=unread_dms,
          note=note,
        )
      else:
        item.set_row(DIRECT_MESSAGES_LABEL, total_dms, unread_dms, note)
      wanted.append(item)

    if not channels and not self.show_direct_messages:
      wanted = [self.empty_row(channel_list, "No channels")]

    # The stripe used to be handed out here, one row at a time, with a comment on the
    # direct message row about continuing the channels' parity rather than starting
    # over. `reconcile` does it from each row's final position instead, which is that
    # rule stated once for the whole list rather than per row.
    reconcile(channel_list, wanted)

    # **A rebuilt list has a rebuilt cursor**, which `clear()` used to see to and
    # which surviving rows would otherwise carry through. Dropped explicitly for the
    # reason `rebuild_nodes` gives at more length: what the rows do is being changed
    # here, not what the list does.
    channel_list.index = None

    # And the mark that is not the cursor is asked for again. It says which view is
    # open rather than where the arrow keys go, so a rebuild that produced no rows
    # has to take the claim away as much as one that reordered them has to move it.
    # `set_class` both ways, so it is as correct over reused rows as over new ones.
    self.update_sidebar_current()




  def unread_direct_count(self) -> int:
    """Direct messages waiting across every conversation, or none when not shown.

    **A roll-up of the conversations now, which is the opposite of what it was.** It
    used to be the flat list's own high-water mark, and the docstring here used to
    explain why that could not be a roll-up: two orderings, one marker each, and
    folding them together over-marks. That reasoning was about a *view* — the flat
    chronological list — and Jason has replaced it with an index of people. With no
    second ordering there is no second marker to keep honest, and the badge is free
    to answer the question the sidebar is actually asking: how much is waiting.

    Counted in messages rather than in conversations, so it reads in the same unit as
    the channel badges directly above it. Jason's call.

    `SCOPE_DIRECT` is no longer read or written. It stays defined in
    `mesh_console.state` because state files on disk still carry it, and an unknown
    key there is ignored rather than an error.
    """
    if not self.show_direct_messages:
      return 0
    return sum(self.unread_conversation_counts().values())




  def empty_row(self, listing: ListView, text: str) -> EmptyItem:
    """The one row a list with nothing in it shows, reused if it is already there.

    Every other row in these lists is now matched to the widget already showing it
    rather than built afresh (see `reconcile`), and this is the same move for the row
    that says why there are no others. Without it a list that is empty and stays
    empty — a filter matching nothing while the reader keeps typing, most often —
    would tear its own message down and build it back on every pass, which is
    precisely the blink being removed everywhere else.

    Handed the text on every call rather than only when building, because the reason
    a list is empty can change while it is empty: the node list says one thing when
    the archive has no nodes and another when the filter matched none of them.
    """
    for child in listing.children:
      if isinstance(child, EmptyItem):
        child.set_text(text)
        return child

    return EmptyItem(text)




  # ----------------------------------------------------------- the node list
  #
  # Two modes, and RxOnly's answer to how they differ, kept: an unfiltered list
  # pages as the cursor walks down it, and a filtered one is fetched whole. A
  # search that paged would have to keep an offset in step with a query the
  # reader is still typing, for a list that a filter has usually made short.


  def reload_nodes(self) -> None:
    """Fetch the first page — or the whole match — and rebuild the list."""
    page = self.read(
      db.fetch_nodes,
      NODE_SEARCH_LIMIT if self.node_search else self.page_size,
      0,
      self.node_search or None,
      list_unnamed=self.list_unnamed_nodes,
    )
    if page is None:
      return

    self.node_offset = len(page["nodes"])
    self.node_matches = page["meta"]["total"]
    if not self.node_search:
      self.node_total = page["meta"]["total"]

    self.rebuild_nodes(page["nodes"])




  def rebuild_nodes(self, nodes: list[dict[str, Any]]) -> None:
    """Draw this page of nodes, reusing every row that is already showing one.

    **"Rebuild" is now a description of the result rather than of the method**, and
    `reconcile` says at length why: tearing fifty rows down and building fifty back
    blinks the sidebar empty for the better part of a second on the Pi, and the poll
    does this every ten seconds. What actually changes between two polls is the
    order, because a node that has just been heard sorts to the top of `last_seen
    DESC` — so the rows move, and `set_node` refreshes what they say.

    The cursor is still dropped afterwards, which is what a rebuild has always done
    here and is the one part of the old behaviour that had to be asked for rather
    than inherited: with the rows surviving, the highlight survives with them. It
    could reasonably be kept — this list's widgets now follow their nodes, so a
    highlight would follow the same node up the order — but that is a change to what
    the sidebar does rather than to how it draws, and it is not this one.
    """
    node_list = self.query_one("#nodes", ListView)

    showing = {
      item.node_id: item
      for item in node_list.children
      if isinstance(item, NodeItem)
    }

    wanted: list[ListItem] = []
    for node in nodes:
      item = showing.get(node["node_id"])
      if item is None:
        item = NodeItem(node)
      else:
        item.set_node(node)
      wanted.append(item)

    if not nodes:
      wanted = [self.empty_row(node_list, "No matching nodes" if self.node_search else "No nodes")]

    reconcile(node_list, wanted)

    # A rebuild has always left this list with no cursor in it — `clear()` set the
    # index to None and nothing put it back — and the rows outliving the rebuild is
    # exactly what would change that. Said out loud so it stays a decision.
    node_list.index = None

    self.update_nodes_heading(len(nodes))
    # Same reason as `rebuild_channels`: a filter or an `r` rebuilds these rows and
    # the cursor lands on the first of them. A node whose detail is open and which
    # the filter has excluded cannot be pointed at, so the list stops claiming to.
    self.update_sidebar_current()

    # **A rebuild scrolls this list and re-highlights it, and neither is the reader
    # touching it.** `clear()` sends the scroll back to the top and moves the
    # selection, so both stamps below fire on the way through — and a rebuild that
    # made the list look busy would hold off the next one for
    # `NODE_LIST_IDLE_SECONDS`, turning a signal about the reader into an interval
    # of its own. The same trap `positioning` exists for in the message pane.
    #
    # Cleared through `call_later` rather than here, because the events have not
    # arrived yet: `ListView.Highlighted` is a posted message and the scroll watcher
    # runs off a reactive, so both land after this returns. `call_later` runs once
    # the pump has drained them, which is exactly when the stamp they left is the
    # only one there is.
    self.call_later(self.disown_node_list_touch)




  def update_nodes_heading(self, shown: int) -> None:
    """How many nodes there are — and, when a filter is on, how many it matched.

    **"x of n" means filtered, and nothing else.** It used to also mean "this much
    of the list has been paged in", so an unfiltered sidebar read `Nodes (50 of 97)`
    with nothing typed in the box and no filter to speak of. Two different facts
    wearing one form, and the paging one is not a fact about the mesh: it is
    bookkeeping about a fetch, which the reader did not ask for and cannot act on.
    RxOnly says exactly this and says it in one place — `update_all_node_counts`
    writes `(total)` into the heading *only* when the search box is empty
    (rxonly.js:172), and the search path writes `(matches of total)` instead
    (nodes.js:127). This is that rule, and it is now the same rule in both.

    What the paging count was doing here is worth naming, because it was doing
    something: this list gave up its scrollbar so its rows could reach the sidebar
    divider, and the heading was the substitute progress cue. The replacement is
    better than a number — the wheel now pages the list, so scrolling to the bottom
    of what is loaded fetches the rest rather than stopping there. See
    `nodes_scrolled`.

    The truncation case stays and is still spelled out rather than left implied.
    `fetch_nodes` clamps a limit to 1000, so a filter matching more than that returns
    the first thousand by last_seen and nothing says so — a search that silently stops
    being a search. Saying "first 1000 of 1204 matching" costs one word and is the
    difference between a narrow filter and a wrong answer.
    """
    heading = self.query_one("#nodes-heading", Label)

    if self.node_search:
      if self.node_matches > NODE_SEARCH_LIMIT:
        heading.update(
          f"Nodes (first {shown} of {self.node_matches} matching)"
        )
      else:
        heading.update(f"Nodes ({self.node_matches} of {self.node_total})")
      return

    heading.update(f"Nodes ({self.node_total})")




  def load_more_nodes(self) -> None:
    """Append the next page of nodes, for a list that is not filtered.

    Guarded against re-entry the way `load_older` is, because the highlight that
    triggers it fires again as the list grows underneath the cursor.
    """
    if self.nodes_loading or self.node_search:
      return
    if self.node_offset >= self.node_total:
      return

    self.nodes_loading = True
    try:
      page = self.read(
        db.fetch_nodes, self.page_size, self.node_offset, None,
        list_unnamed=self.list_unnamed_nodes,
      )
      if page is None or not page["nodes"]:
        return

      node_list = self.query_one("#nodes", ListView)
      # Continuing from `node_offset`, which is how many rows are already on
      # screen, so the stripe carries across a page boundary instead of restarting
      # and putting two shaded rows next to each other every 50 nodes.
      for row, node in enumerate(page["nodes"], start=self.node_offset):
        node_list.append(striped(NodeItem(node), row))

      self.node_offset += len(page["nodes"])
      self.node_total = page["meta"]["total"]
      self.update_nodes_heading(self.node_offset)

    finally:
      self.nodes_loading = False




  def on_click(self, event: events.Click) -> None:
    """Clicking the Nodes heading takes the list back to the top.

    RxOnly's `handle_nodes_heading_click` (views.js:567) is
    `nodes_list.scrollTo({top: 0, behavior: "smooth"})` and this is the same thing:
    the heading names the list, so pressing it is a way back to the start of it —
    worth more here than on the web, because this list gave up its scrollbar and a
    reader four hundred nodes down has no thumb to drag back.

    Handled on the App rather than on a `Label` subclass because a `Click` bubbles
    from the widget it landed on up through its ancestors to here, and the widget it
    landed on is a plain `Label` that exists only to hold a string. A subclass would
    be a class per clickable label; this is one branch.

    `animate=True` is the `behavior: "smooth"` half, and it also does the work of
    saying what happened: a list that jumps has arguably not moved, while one that
    travels shows the reader it went somewhere. Nothing is scrolled if nothing is
    scrolled — `scroll_home` on a list already at the top is a no-op.
    """
    if event.widget is not None and event.widget.id == "nodes-heading":
      self.query_one("#nodes", ListView).scroll_home(animate=True)




  def messages_scrolled(self, scroll_y: float) -> None:
    """Reading and paging, both of which the message list scroll now drives.

    **Reading**, because `mark_read_from_viewport` is the read marker and the
    viewport is what moved. **Paging**, because a reader who never presses an arrow
    key never moved a cursor for `on_list_view_highlighted` to notice — the same
    gap `nodes_scrolled` was written to close, arriving here for the same reason one
    step later.

    The cursor path stays as well as this, rather than instead of it, and the case
    it covers is the one scrolling cannot: at the top of the loaded window `scroll_y`
    is already 0, so walking the selection up onto the first row moves nothing and
    would fetch nothing. `load_older` and `load_newer` carry their own re-entry
    guards, so both paths can ask at once without racing.
    """
    if not self.viewing_messages:
      return

    # A pane that is being put where it belongs fires scroll events on the way, and
    # none of them is the reader doing anything. `mark_read_from_viewport` already
    # refuses these; the pager has to refuse them too, or opening a channel near its
    # older edge fetches a page off a position nobody was ever shown.
    if self.positioning:
      return

    self.mark_read_from_viewport()

    # A page already arriving moves the scroll on its own — the rebuild, the cursor
    # restore, the viewport restore. Every one of those lands here, and each used to
    # queue another fetch that ran after the first finished, walking the window
    # further than the reader asked. The pane's position is the load's business
    # until its restore has landed; a reader still near the edge after that will
    # move the wheel again, and that event books the next page.
    if self.is_loading:
      return

    messages_view = self.query_one("#messages", ListView)

    if scroll_y <= MESSAGE_SCROLL_TRIGGER_LINES and self.has_more_older:
      self.call_next(self.load_older)
    elif (
      messages_view.max_scroll_y - scroll_y <= MESSAGE_SCROLL_TRIGGER_LINES
      and self.has_more_newer
    ):
      self.call_next(self.load_newer)




  def nodes_scrolled(self, scroll_y: float) -> None:
    """Pull the next page in when the list is scrolled near its end.

    **The cursor path was not enough on its own.** `on_node_highlighted` pages when
    the cursor walks toward the bottom, and the wheel scrolls this list without moving
    the cursor — so a reader who scrolled to the end of the loaded fifty sat there
    with nothing arriving, which is the other half of what the heading's "50 of 97"
    was quietly reporting. RxOnly pages on its own scroll event
    (`handle_nodes_scroll`, nodes.js:325) and this is the same trigger in lines.

    Watched rather than handled: a ListView's `scroll_y` is a reactive, and it moves
    for the wheel, the arrow keys, and a drag alike. An event handler would have had
    to name each of those. Wired up in `on_mount` with `init=False`, so a list that
    starts at the top is not mistaken for one scrolled to the bottom of nothing.

    `load_more_nodes` carries every guard this needs — re-entry, an active filter, and
    everything already loaded — so this decides only where "near the end" is.
    """
    # The wheel half of "somebody is using this list"; `on_node_highlighted` is the
    # cursor half. Both stamp it, because a rebuild has to stay away from either.
    self.node_list_touched = monotonic()

    node_list = self.query_one("#nodes", ListView)
    if node_list.max_scroll_y - scroll_y <= NODE_SCROLL_TRIGGER_LINES:
      self.load_more_nodes()




  def action_filter_nodes(self) -> None:
    """Put the cursor in the node filter."""
    self.query_one("#node-filter", Input).focus()




  def action_step_into_nodes(self) -> None:
    """`down` in the filter box hands the keyboard to the list below it.

    Jason's, and it closes the one gap the panel ring left: the filter is the last stop
    in `focus_ring`, so reaching the results from it meant either tabbing all the way
    round or `escape`, which reads as abandoning what you typed rather than acting on
    it. The filter's whole job is to shorten that list, and this is the press that
    arrives at the shortened version of it.

    **It places a cursor as well as moving focus, and it has to.** `rebuild_nodes`
    ends every rebuild with `index = None` — deliberately, so a list redrawn under a
    reader does not claim a highlight nobody put there — which means a freshly filtered
    list has no cursor at all, and focusing it alone would have shown nothing and cost
    a second `down` to get a first row. So the first press lands on the first result,
    which is the same bargain `MessageList._select_start` makes one pane over: take the
    selection out of nothing, or move it if there already is one.

    An existing cursor is left where it is, so a reader who walked into the list, came
    back to the box and pressed `down` again returns to their place rather than to the
    top. `escape` is unchanged and still goes to the list without touching its cursor —
    the two keys now differ in exactly that, which is the difference between acting on
    a filter and putting it down.
    """
    node_list = self.query_one("#nodes", ListView)
    node_list.focus()

    # `children` rather than the loaded count: an empty archive draws the "No nodes"
    # placeholder as a row, and a cursor on it is what `tab` then `down` would have
    # produced anyway. Nothing is selectable there — `on_list_view_selected` asks for a
    # `NodeItem` — so the highlight is the whole of what happens.
    if node_list.index is None and node_list.children:
      node_list.index = 0




  def action_compose_to_read(self) -> None:
    """`up` in the compose box: back to the message the reader has read to."""
    self.leave_compose_for_panel(unread=False)




  def action_compose_to_unread(self) -> None:
    """`down` in the compose box: on to the first message they have not read."""
    self.leave_compose_for_panel(unread=True)




  def leave_compose_for_panel(self, *, unread: bool) -> None:
    """Hand the keyboard from the box to the panel it posts into, on a named row.

    **Where "read to" comes from, and why it is not measured again here.** `read_row` is
    the app's one answer to how far this reader has got — `mark_read_from_viewport` sets
    it from the read line, a fifth of the pane up from the bottom, so it already means
    "the lowest message that has genuinely been past the reader's eye". Asking the
    viewport a second time with slightly different arithmetic would be a second answer
    to one question, and the two would drift. It is also exactly where the *first* arrow
    key lands when the pane is reached any other way — `MessageList.start_row`, set by
    `set_read_row` — so arriving from the box lands where arriving from `tab` lands, and
    the box is not a special case with its own idea of where the reader was.

    The unread landing is the row after it, which is the honest local meaning of "the
    first one not read": rows are in archive order, so the message after the last one
    read is the next one to read. Clamped, so a fully-read channel keeps the cursor on
    its final message rather than off the end — there being nothing unread is a real
    answer and the last message is where the reader already is.

    **Moving this cursor does not itself mark anything read**, and that is deliberate
    and unchanged — reading is the scroll position (`mark_read_from_viewport`), not the
    highlight. In practice the two agree here: landing on an unread row below the fold
    scrolls the pane to show it, and that scroll is what marks what it carried past the
    read line. A row already on screen is not claimed, which is right — it was visible,
    not necessarily read.

    A detail pane gets the keyboard and nothing else. There is no list to land in, and
    a message long enough to outrun its pane still has to be scrollable from a box that
    now takes the focus on the way in — see `enter_detail`.
    """
    if self.viewing_detail:
      self.query_one("#detail-pane", VerticalScroll).focus()
      return

    messages = self.query_one("#messages", MessageList)
    messages.focus()

    if not messages.children:
      return

    last = len(messages.children) - 1
    target = self.read_row + 1 if unread else self.read_row
    messages.index = max(0, min(target, last))




  def action_open_highlighted_channel(self) -> None:
    """`right` on a channel opens it, which is what `enter` on it does.

    Reached through the list's own `action_select_cursor` rather than by calling
    `open_channel` here, so there is one route into a channel and not two: that action
    posts `ListView.Selected`, which `on_list_view_selected` already turns into the
    right one of `open_channel` and `open_direct_index` depending on the row. A second
    caller would have had to know that difference, and would have been the copy that
    fell behind the next time the sidebar grew a kind of row.
    """
    self.query_one("#channels", ListView).action_select_cursor()




  def action_leave_for_channels(self) -> None:
    """`left` in the message pane goes back to the list that opened it.

    The cursor is left exactly where it is, in both lists. `left` is a statement about
    where the keyboard should be and not about what the reader has finished with — the
    message they were on is still where they were, and `right` or `enter` on the channel
    brings them back to it.
    """
    self.query_one("#channels", ListView).focus()




  def on_node_filter_changed(self, value: str) -> None:
    """Requery after the typing stops, rather than once per keystroke.

    300ms, the same wait RxOnly uses (`search_debounce_delay`, rxonly.js:25).
    A Textual timer is enough — the query is one indexed read of a local file,
    and the reason to wait is the reader's typing rather than the query's cost.
    """
    if self.node_filter_timer is not None:
      self.node_filter_timer.stop()

    self.node_filter_timer = self.set_timer(
      NODE_FILTER_DEBOUNCE, lambda: self.apply_node_filter(value.strip())
    )




  def update_node_filter_clear(self, value: str) -> None:
    """Show the clear mark while there is something to clear, and hide it otherwise.

    Called undebounced, unlike the query beside it, and that is deliberate: whether
    the box has anything in it is a fact about the box rather than a question for the
    archive, so there is nothing to wait for. RxOnly toggles its own button from
    inside the debounce (nodes.js:36) and pays 300ms for it; a mark that appeared a
    third of a second after the first keystroke would read as lag.
    """
    self.query_one("#node-filter-clear", Static).set_class(bool(value), "visible")




  def action_clear_node_filter(self) -> None:
    """Empty the filter box and put the whole node list back.

    Reached by clicking the `✕` at the right of the box, which is RxOnly's
    `clear_nodes_search` (nodes.js:141) and the one thing this had no equivalent for:
    a filter could be typed with the mouse nowhere near the keyboard and then only be
    taken off by hand, a character at a time.

    **The keyboard is left exactly where it was, and nothing here has to arrange
    that.** Textual focuses on mouse-down before forwarding the click, but
    `get_focusable_widget_at` walks up from the widget under the pointer and stops at
    the first *focusable* node — a `Static` is not one, and neither is the row, the
    sidebar or the screen above it, so it finds nothing and no focus moves. A reader
    who was typing in the box is still typing in it; one who was reading a channel
    stays in the channel. There is nothing to restore, which is why nothing is saved.

    Applied at once rather than through `NODE_FILTER_DEBOUNCE`. The wait exists to let
    typing finish and a click has finished by the time it arrives — the same reason
    `on_input_submitted` stops the timer before applying.

    **The `Input.Changed` that emptying the box posts arrives after this returns**, and
    finds nothing left to do: Textual queues messages rather than calling handlers, so
    the two lines below run first, and by the time `on_node_filter_changed` sets its
    own timer `node_search` is already `""` and the `apply_node_filter` at the end of
    it returns early. The mark is hidden here rather than left to that handler so it
    goes in the same frame as the text it describes.
    """
    self.query_one("#node-filter", Input).value = ""
    self.update_node_filter_clear("")

    if self.node_filter_timer is not None:
      self.node_filter_timer.stop()
      self.node_filter_timer = None

    self.apply_node_filter("")




  def apply_node_filter(self, search: str) -> None:
    self.node_filter_timer = None
    if search == self.node_search:
      return

    self.node_search = search
    self.reload_nodes()




  def update_device_bar(self, stats: dict[str, Any]) -> None:
    """The band under the top of the screen: which device, and how many nodes it knows.

    **The device's name is the way home**, which is RxOnly's
    `<h1><a class="device-bar-home">` (index.html:32) and what Jason asked for. The
    node count beside it is not: it is a fact about the mesh rather than a place, and
    RxOnly does not wrap it either.

    A `Text` rather than an f-string, because only part of the line is clickable and a
    string has no way to say which. It also settles the markup question by
    construction — a device called `[bold]` is a device with an odd name, and a `Text`
    is never parsed for markup. That was worth fixing on the way past: this widget was
    the one place a mesh-supplied name reached a `Static` that had not been told
    `markup=False`, against what this module's own docstring says it does everywhere.
    """
    local_node = stats.get("local_node")
    device = format_device_name(local_node)
    total_nodes = stats["stats"]["total_nodes"]

    bar = Text(no_wrap=True, overflow="ellipsis")
    # `app.dashboard` reuses the action `d` is bound to rather than adding a second
    # way to say the same thing. `app.`-qualified for the reason every other `@click`
    # in this file is: a bare name resolves against the widget that was clicked.
    # Bold, and only the name: it is the heading, and the count beside it is a fact
    # about the mesh rather than part of the title. Weight rather than colour, which
    # is what RxOnly's `.device-bar h1 { font-weight: 500 }` does with the room a
    # browser has for half a step; a terminal has bold or not.
    bar.append(
      device, style=Style(bold=True, meta={"@click": "app.dashboard"})
    )
    bar.append(f" — {total_nodes} nodes")

    self.query_one("#device-bar", Static).update(bar)




  # ------------------------------------------------------------------ dashboard


  def refresh_dashboard(self) -> None:
    """Render the dashboard. Deliberately does not check for a device swap.

    Handling a swap ends in showing the dashboard, so checking here would have
    this call back into itself — and each pass would see the new device as
    another change. The check belongs to the poll, which is the one place that
    asks "has the archive become something else?"
    """
    stats = self.read(
      db.fetch_stats, self.show_direct_messages,
      list_unnamed=self.list_unnamed_nodes,
    )
    if stats is None:
      return

    self.query_one("#dashboard", Static).update(self.render_dashboard(stats))




  def render_dashboard(self, stats: dict[str, Any]) -> str:
    local_node = stats.get("local_node") or {}
    counts = stats["stats"]

    lines: list[str] = [format_device_name(local_node or None), ""]

    lines.append("Local Node")
    if local_node:
      fields: list[tuple[str, Any]] = [
        ("Node ID", local_node.get("node_id")),
        ("Hardware", local_node.get("hardware")),
        ("Role", local_node.get("role")),
        ("First Seen", format_timestamp(local_node.get("first_seen"))),
        ("Last Seen", format_timestamp(local_node.get("last_seen"))),
      ]
      if local_node.get("battery_level") is not None:
        fields.append(("Battery", f"{local_node['battery_level']}%"))
      if local_node.get("voltage") is not None:
        fields.append(("Voltage", f"{local_node['voltage']}V"))

      # The radio-health trio, on the dashboard because this panel is the attached
      # device describing itself and these three are what it says about its own
      # radio: how busy the channel is, how much of the airtime this node is using,
      # and how long since it booted. They reach the archive through local stats,
      # which only the attached device sends. A peer's copy of the same three is on
      # its node detail panel and nowhere else.
      #
      # `is not None` rather than a truth test, the same distinction battery and
      # voltage draw above: a channel utilization of 0.0 on a quiet mesh is a
      # reading, and an uptime of 0 is a device that just rebooted.
      if local_node.get("channel_util") is not None:
        fields.append(("Channel Util", f"{local_node['channel_util']:.2f}%"))
      if local_node.get("air_util_tx") is not None:
        fields.append(("Air Util TX", f"{local_node['air_util_tx']:.2f}%"))
      uptime = format_uptime(local_node.get("uptime_seconds"))
      if uptime is not None:
        fields.append(("Uptime", uptime))

      # 14 rather than the 12 this was: "Channel Util:" is thirteen characters, and
      # a label that overruns its column takes the whole block's alignment with it.
      for label, value in fields:
        if value not in (None, ""):
          lines.append(f"  {label + ':':<14} {value}")
    else:
      lines.append("  No node information available")

    lines += ["", "Network Stats"]
    lines.append(f"  {'Nodes:':<18} {counts['total_nodes']}")
    lines.append(f"  {'Messages:':<18} {counts['total_messages']}")
    if self.show_direct_messages:
      lines.append(f"  {'Direct Messages:':<18} {counts['total_direct_messages']}")
    lines.append(f"  {'Channels:':<18} {counts['total_channels']}")

    return "\n".join(lines)




  def check_for_state_change(self, stats: dict[str, Any]) -> None:
    """Notice a swapped device or a rebuilt database, rather than rendering on.

    RxOnly reloads the page here. There is no page to reload, so the sidebar is
    rebuilt and the view drops back to the dashboard: whatever channel was open
    may not mean the same thing any more.
    """
    local_node = stats.get("local_node")
    if not local_node:
      return

    node_id = local_node.get("node_id")
    first_seen = local_node.get("first_seen")

    if self.known_local_node_id is None:
      self.known_local_node_id = node_id
      self.known_first_seen = first_seen
      return

    if node_id == self.known_local_node_id and first_seen == self.known_first_seen:
      return

    reason = (
      "Device changed" if node_id != self.known_local_node_id
      else "Database rebuilt"
    )
    self.known_local_node_id = node_id
    self.known_first_seen = first_seen

    # A different device means a different answer to "which messages are mine",
    # so the attribution has to move with it or every row would be compared
    # against a node that is no longer attached.
    self.local_node_id = node_id

    self.notify(f"{reason} — reloading the archive.", severity="warning")
    self.show_dashboard()
    self.refresh_sidebar()




  # ----------------------------------------------------------------- navigation


  async def action_quit_console(self) -> None:
    """`q`. Textual's own quit, under a name this app is free to gate.

    A one-line delegation on purpose: what quitting *means* stays the framework's —
    `App.action_quit` is a coroutine that calls `self.exit()`, and `on_unmount` is
    still what flushes read positions and closes the connection. All this name buys is
    a `check_action` branch that does not reach `ctrl+q`. See `TEXT_BOX_SHADOWED`.
    """
    await self.action_quit()




  def action_dashboard(self) -> None:
    self.show_dashboard()




  def show_view(self, view: str) -> None:
    """Show one of #main's three panes and hide the other two.

    One place decides what is displayed, so a view can be added without every
    caller having to remember what to switch off. The compose box is asked
    again afterwards because `compose_available()` gates on the view, and it
    must never appear over a detail pane.
    """
    self.view = view

    # Leaving a node takes its map link with it. This was already true in practice —
    # `render_node_detail` overwrites it on the way into the next node, and a link
    # can only be clicked while it is on screen — but `o` asks the question without a
    # link under a pointer, so the field has to actually mean what its declaration
    # says rather than merely never having been caught out.
    if view != VIEW_NODE:
      self.current_map_url = None

    self.query_one("#dashboard-pane", VerticalScroll).display = view == VIEW_DASHBOARD
    self.query_one("#messages", ListView).display = view in (
      VIEW_MESSAGES, VIEW_DIRECT
    )
    self.query_one("#detail-pane", VerticalScroll).display = self.viewing_detail

    self.update_sidebar_current()
    self.update_compose()
    self.refresh_bindings()




  def update_sidebar_current(self) -> None:
    """Mark the sidebar row for the view that is open, and unmark every other.

    **The mark is on the row, not on the cursor, and that is the whole fix.** The
    first attempt put a class on the *list* and let the cursor's own highlight be
    the "you are here" — which works right up until the cursor moves. Arrow through
    the channels without opening one, or click a node to read it and then click
    another, and the lit row follows the cursor while the main pane stays where it
    was. Jason saw that as highlighting that worked sometimes; it worked exactly
    when the cursor happened to still be on the open view.

    So the two questions get two marks. `-highlight` is Textual's cursor and answers
    "where will the arrow keys go" — painted only while that list has focus, because
    that is the only time the answer matters. `current` is this, and answers "which
    of these am I looking at" — painted whether or not anything has focus, and moved
    only by opening something.

    Re-run on every view change and after either list is rebuilt, because a rebuild
    makes new row objects and classes do not survive it.
    """
    channels = self.query_one("#channels", ListView)
    nodes = self.query_one("#nodes", ListView)

    in_a_channel = self.view in (VIEW_MESSAGES, VIEW_DIRECT, VIEW_MESSAGE)

    for item in channels.children:
      item.set_class(
        in_a_channel
        and isinstance(item, ChannelItem)
        and item.is_dm == self.current_is_dm
        and item.channel_index == self.current_channel_index,
        "current",
      )

    # A node whose detail is open but which is not in the loaded page simply marks
    # nothing — the list is paged, and a node reached by filtering or from a link in
    # a message can easily sit outside the window. Nothing lit is the honest answer;
    # the cursor is not borrowed to stand in for it.
    for item in nodes.children:
      item.set_class(
        self.view == VIEW_NODE
        and isinstance(item, NodeItem)
        and item.node_id == self.detail_node_id,
        "current",
      )




  # ------------------------------------------------------------ which panel is live
  #
  # Three questions, and the sidebar's two marks above answer only two of them.
  # `current` is which view is open and `-highlight` is where a list's arrow keys
  # would go; this is which panel the arrow keys are talking to *at all*, which
  # neither of those could say — the message pane deliberately has no selection until
  # an arrow key asks for one, so a focused message pane had no mark of any kind.


  def focus_moved(self, focused: Optional[Widget]) -> None:
    """Draw the focus rule under the heading of whichever panel has the keyboard.

    A watch on `Screen.focused` rather than a handler, for the same reason the two
    scroll watchers in `on_mount` are watches: focus moves for `tab`, for a click, for
    `c` and `f`, and for `escape` leaving a box — and every one of those goes through
    this one reactive, where an event handler would have had to name each of them.

    Nothing focused draws no rule anywhere, which is the honest answer and is also a
    real resting position now rather than only a startup state: it is the first stop
    in `focus_ring`.
    """
    heading = (
      self.PANEL_HEADINGS.get(focused.id or "") if focused is not None else None
    )

    for selector in self.PANEL_HEADING_SELECTORS:
      self.query_one(selector).set_class(selector == heading, "focused-panel")




  def main_pane_id(self) -> Optional[str]:
    """Which of #main's three panes is the one on screen, and whether tab visits it.

    None means the ring skips the main pane in this view, which is the dashboard's
    usual answer and Jason's: the sidebar is where a dashboard is navigated from, and
    a stop that only ever passes the keyboard through is a stop worth not having.

    **The rule is about rows, not about which view it is.** A pane with rows in it is
    always worth focusing, because focusing it is how the cursor gets somewhere;
    `#messages` is therefore never skipped, even on a channel short enough to fit. A
    pane that can only be scrolled is worth focusing exactly when there is something
    to scroll — so the dashboard and the two detail views are asked whether they
    overflow, and a summary or a node that fits its pane is left out.

    That is one rule for two answers rather than a special case for the dashboard, and
    it closes the hole the special case would have left: node detail with a position
    block runs past the bottom of a short terminal, and with the main pane out of the
    ring on principle there would have been no keyboard route to the rest of it.
    """
    if self.viewing_messages or self.view == VIEW_DIRECT:
      return "messages"

    pane_id = "detail-pane" if self.viewing_detail else "dashboard-pane"
    pane = self.query_one(f"#{pane_id}", VerticalScroll)

    # `max_scroll_y` is what a scroll would be clamped to, so anything above zero is
    # content the pane cannot show at once. Asked of the pane rather than tracked,
    # because it changes with the terminal's height as well as with what is in it.
    return pane_id if pane.max_scroll_y > 0 else None




  def focus_ring(self) -> list[Optional[str]]:
    """The panels `tab` visits, in order, for the view that is on screen.

    Jason's order, which is not the order the widgets are built in and is the reason
    this exists at all. Textual's own `tab` walks the DOM, and the DOM has the filter
    box above the list it filters — so tabbing from the channels landed in a text box
    on the way to the nodes, and the two lists a reader moves between were not
    adjacent in the one sequence that moves between them. The filter is the last stop
    here instead: it belongs to the node list, and you reach it after the thing it
    acts on rather than in front of it.

    **None is a stop, not the absence of one.** A ring with nowhere to rest cannot be
    left, and a reader who has finished with the panels should be able to put the
    keyboard down — with nothing focused, the single-letter bindings are unambiguous
    and no list is quietly holding the arrow keys. It is first because that is where
    the app starts.

    **The compose box follows the pane it belongs to, and it was left out first.** The
    argument for leaving it out was that `c` reaches it and `escape` leaves it, both
    saying what they do, and that a text box in a ring walked with `tab` is somewhere a
    reader arrives by accident with a half-typed message in front of them. Jason's
    call to put it in, and the reading that wins is the reader's: having tabbed to a
    channel's messages or to one message's detail, the next thing wanted is usually to
    answer it, and `tab` is the key already in hand. It sits immediately after the main
    pane rather than at the end, so the panel and the box that posts into it are
    adjacent — which is also why it is not simply appended: reaching the reply for the
    thing you are reading should not mean walking past the node list.

    Only when there is somewhere to send. `compose_available` is the same question the
    box's own visibility asks, so this never offers a stop that is `display: none` —
    which would be a `tab` that appears to do nothing, since Textual refuses focus to
    an invisible widget and the ring would have handed it the keyboard anyway.
    """
    ring: list[Optional[str]] = [None, "#channels"]

    main_pane = self.main_pane_id()
    if main_pane is not None:
      ring.append(f"#{main_pane}")

    # Straight after the pane, and gated on the same answer that decides whether the
    # box is on screen at all — a read-only console never grows this stop.
    if self.compose_available():
      ring.append("#compose")

    ring.append("#nodes")
    ring.append("#node-filter")
    return ring




  def action_focus_ring(self, step: int) -> None:
    """One step around `focus_ring`, forwards for `tab` and backwards for `shift+tab`.

    Focus that the ring does not name is treated as the resting position, so `tab` from
    it goes to the channels and `shift+tab` to the filter. Refusing to move would have
    been the other reading and is the worse one: `tab` is the key for "somewhere else",
    and the place a reader is most likely to want out of is a box they are typing in.

    That fallback covers less than it used to now the compose box is a stop of its own,
    and it is not dead: a box that was available when it took the keyboard and is not
    when `tab` is pressed — a channel that stopped being sendable, a collector that
    stopped answering — is focus the ring no longer names, and this is what gets the
    reader out of it.
    """
    ring = self.focus_ring()

    focused = self.focused
    here = f"#{focused.id}" if focused is not None and focused.id else None
    index = ring.index(here) if here in ring else 0

    target = ring[(index + step) % len(ring)]
    if target is None:
      self.screen.set_focus(None)
      return

    self.query_one(target).focus()




  def render_node_detail(self, node: dict[str, Any], conversation) -> Text:
    """Node detail with this stylesheet's colours, and a map link that can be followed.

    The styles are asked of the widget rather than named here, so `format.py` holds
    no colour and the theme still decides. `current_map_url` is recorded on the way
    past because the click action fires later, from a style's metadata, and has no
    other way to know which node is on screen.
    """
    detail = self.query_one("#detail", DetailView)
    self.current_map_url = format_coordinates(node)

    return format_node_detail(
      node,
      conversation,
      label_style=detail.get_component_rich_style("detail--label"),
      # `app.`-qualified, because Textual dispatches a `@click` action against the
      # widget that was clicked and the bare name reached nothing. See the comment
      # on the span in `format_node_detail`.
      map_action="app.open_map",
    )




  def action_open_map(self) -> None:
    """Open the node's position in whatever the desktop opens URLs with.

    Two ways in, and they are the two that do not depend on the terminal:

    - **`o`**, which is the one that always works. A Textual app has mouse reporting
      on, so the terminal has handed the mouse over and its own link handling —
      cmd-click, the right-click "Open Link" item — is no longer reachable over the
      app's own surface. A key is not something the terminal can take away.
    - **Clicking the link**, via the `app.open_map` `@click` meta on its style. This
      is Textual's dispatch rather than the terminal's, so it works wherever the
      mouse reaches the app at all.

    There was a third — an OSC 8 `link=` on the same span, for a terminal that handles
    hyperlinks itself — and it is gone, because over a surface that has taken the
    mouse it was unreachable anyway and it made the hover strobe. See
    `format_node_detail`, which is where the span is built and why is written down.

    `webbrowser` is stdlib and adds no dependency. It is wrapped because a headless
    or locked-down machine has nothing to open with, and a failed map link is not a
    reason to take the interface down.
    """
    if not self.current_map_url:
      return

    try:
      opened = webbrowser.open(self.current_map_url)
    except Exception:
      opened = False

    if not opened:
      self.notify(
        f"Could not open a browser. The link is {self.current_map_url}",
        severity="warning",
        timeout=10,
      )




  def set_breadcrumbs(self, *crumbs: "str | Crumb") -> None:
    """The trail across the top of #main, mirroring RxOnly's `set_breadcrumbs`.

    Worth having here for the reason `PHASE-3-HANDOFF.md` said it was not: with
    only a dashboard and a channel, the sidebar highlight and `d` already said
    where you were. A detail view is the first place with no sidebar entry
    highlighted and no channel open, and `sub_title` alone cannot say both which
    channel you came from and what you are looking at.

    A crumb with a `CRUMB_*` target is clickable and goes straight there; a bare
    string is not, which is what every trail's last crumb is. `escape` still walks
    back up one step at a time, and is still the whole of the keyboard route — see
    `action_breadcrumb`.
    """
    self.query_one("#breadcrumbs", Breadcrumbs).set_trail(*crumbs)




  async def action_breadcrumb(self, target: str) -> None:
    """Go where a clicked crumb points.

    Reached only from a `@click` on a crumb's own span, with the target it was built
    with — so this never has to work out *which* crumb was pressed from a position,
    and adding a step to a trail cannot silently change what an existing one does.

    Each branch defers to the method that already knows how to get there, rather
    than reimplementing it a second way:

    - **The dashboard** is `show_dashboard()`, the same as `d`.
    - **The messages** are `leave_detail()`, the same as `escape` from a detail view —
      and that is what answers Jason's "maintain the scroll position": it reopens the
      channel through `open_channel`, which resumes on the message this reader's read
      position names. The position was flushed on the way *into* the detail, so the
      message it names is the row the cursor was on when they pressed enter. A
      conversation is reopened as a conversation, peer and all.
    - **The direct message list** is the flat list, opened exactly as pressing enter
      on its sidebar entry opens it.

    Guarded on the view rather than trusting the trail: a crumb is only rendered
    where it makes sense, but an action that can be dispatched is worth making
    unable to act on a view it was not built for.
    """
    if target == CRUMB_DASHBOARD:
      self.show_dashboard()
      return

    if target == CRUMB_MESSAGES:
      if self.viewing_detail:
        await self.leave_detail()
      return

    if target == CRUMB_DIRECT:
      if not self.show_direct_messages:
        return
      await self.open_direct_index()




  def show_dashboard(self) -> None:
    self.flush_positions()

    self.show_view(VIEW_DASHBOARD)
    self.set_message_status(None)
    self.set_breadcrumbs("Dashboard")

    self.refresh_dashboard()
    self.sub_title = ""




  async def on_list_view_selected(self, event: ListView.Selected) -> None:
    """Enter opens whatever the cursor is on, in whichever list holds it.

    This used to return for anything that was not `#channels`. Both other lists
    now have somewhere to go, and a message list that did nothing on enter was
    the only reason `fetch_message` had no caller outside the resume path.
    """
    item = event.item
    list_id = event.list_view.id

    if list_id == "channels" and isinstance(item, ChannelItem):
      # The direct message entry opens the index of correspondents; a channel opens
      # its messages. They used to be the same call with a flag, which is what made
      # the flat list a message view in the first place.
      if item.is_dm:
        await self.open_direct_index()
        return

      await self.open_channel(
        is_dm=item.is_dm,
        channel_index=item.channel_index,
        label=item.channel_name,
      )
      return

    if list_id == "messages" and isinstance(item, ConversationItem):
      await self.open_conversation(item.peer)
      return

    if list_id == "nodes" and isinstance(item, NodeItem):
      self.show_node_detail(item.node_id)
      return

    if list_id == "messages" and isinstance(item, MessageItem):
      # In the flat direct message list, enter opens the conversation the row
      # belongs to rather than the row's own detail — which is the route from a DM
      # you received to the thread it is part of, and the reason the flat list is
      # worth keeping. Nothing is lost: it lands on the very message that was
      # pressed, so its detail is one more enter away, from inside the thread.
      if self.current_is_dm and self.current_peer is None:
        await self.open_conversation_for(item.message)
        return

      self.show_message_detail(item.message["message_id"])




  async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
    if event.list_view.id == "nodes":
      self.on_node_highlighted(event)
      return

    if event.list_view.id != "messages":
      return
    if event.item is None or not isinstance(event.item, MessageItem):
      return

    index = event.list_view.index
    if index is None:
      return

    # **Moving the selection does not mark anything read**, and that is the change:
    # reading is `mark_read_from_viewport`, driven by the scroll this move may or may
    # not cause. A selection walked down inside the visible rows has scrolled nothing
    # past the read line and has read nothing; one walked off the bottom scrolls, and
    # the watcher marks what the scroll carried up.

    # Walking down to the newest row is how the reader stops having anywhere to jump
    # to, and walking back up is how they get it again — so the footer is re-asked on
    # every move of this cursor. `R` depends on the selection existing at all now: a
    # channel just opened has none, and does not advertise a reply until an arrow key
    # has said which message the reply would answer.
    self.refresh_bindings()

    # Never let the two trigger zones swallow the whole loaded window: with a
    # small page size, an unclamped distance makes every position "near both
    # edges" and fetches pages the reader has not walked toward.
    #
    # Counted in rows rather than messages, because rows are what the cursor
    # moves through — a window whose tapbacks have all been absorbed holds fewer
    # rows than messages, and measuring the distance in the wrong one would
    # trigger a fetch early or not at all.
    trigger = min(PAGE_TRIGGER_DISTANCE, len(self.rows) // 3)

    if index <= trigger and self.has_more_older:
      await self.load_older()
    elif index >= len(self.rows) - 1 - trigger and self.has_more_newer:
      await self.load_newer()




  def on_node_highlighted(self, event: ListView.Highlighted) -> None:
    """Pull the next page of nodes in as the cursor approaches the end of them.

    The list's own equivalent of RxOnly's infinite scroll. A terminal list has a
    cursor rather than a scrollbar, so the trigger is the cursor nearing the
    bottom rather than the viewport doing so — the same substitution the message
    pager already makes.
    """
    self.node_list_touched = monotonic()

    index = event.list_view.index
    if index is None or self.node_search:
      return

    loaded = self.node_offset
    if loaded and index >= loaded - 1 - min(PAGE_TRIGGER_DISTANCE, loaded // 3):
      self.load_more_nodes()




  # --------------------------------------------------------------- detail views
  #
  # One node, or one message, in full. Both exist because the read tier has
  # returned more than a list row can show since Phase 3 — a node's coordinates,
  # a message's signal metrics — and neither had anywhere to be rendered.


  def show_node_detail(self, node_id: str) -> None:
    node = self.read(db.fetch_node, node_id)
    if node is None:
      self.notify(f"No node {node_id} in the archive.", severity="warning")
      return

    # The conversation block is where a per-conversation unread count is visible,
    # and the only place: a conversation has no sidebar entry to carry one. None
    # when this node is not somebody this console can converse with, in which case
    # the block is absent rather than empty.
    conversation = (
      self.conversation_summary(node_id) if self.can_converse_with(node_id) else None
    )

    # Before `enter_detail`, which changes the view — and `show_view` asks the
    # sidebar to point at whatever is now open, which for a node means this one.
    self.detail_node_id = node_id

    self.enter_detail(
      VIEW_NODE,
      format_node_display_name(node),
      self.render_node_detail(node, conversation),
    )




  def show_message_detail(self, message_id: int) -> None:
    message = self.read(db.fetch_message, message_id, self.current_is_dm)
    if message is None:
      self.notify(f"No message {message_id} in the archive.", severity="warning")
      return

    # **The box on this page answers this message.** That is the whole of what makes
    # a compose box here different from the one under the message list: there, `R`
    # arms a reply on top of an ordinary box and `escape` disarms it; here the reply
    # *is* the page, so it is set on the way in and cleared on the way out, and there
    # is no in-between state to step through. See `action_back`.
    #
    # Set before `enter_detail`, and that ordering is load-bearing: `show_view()`
    # asks `update_compose()` on its way through, and the border title has to name
    # what it is answering the first time it is drawn rather than a beat later.
    self.reply_to_message = message

    # Which message to come back onto when this page is left. **It has to be
    # recorded, and it did not used to have to be**: while the cursor was the read
    # marker, "where the reader was" and "what this reader had read to" were the same
    # row, so reopening the channel from its stored position landed on it for free.
    # Reading is the scroll position now, and it legitimately runs ahead of the row a
    # reader stopped on to open — so the way back has to name the message rather than
    # rely on the coincidence. See `leave_detail`.
    self.detail_message_id = message_id

    self.enter_detail(VIEW_MESSAGE, f"Message {message_id}", format_message_detail(message))




  def enter_detail(self, view: str, crumb: str, body: str) -> None:
    """Show a detail pane, remembering what to go back to.

    The read position is flushed on the way in but the cursor is not moved:
    coming back has to land on the same message, and looking at a node is not
    reading a channel.
    """
    self.flush_positions()

    # A pending reply belongs to the view it was armed in. Message detail is the one
    # way in here that wants one and sets its own just above; every other route
    # leaves whatever `R` armed behind, rather than carrying it onto a page about
    # something else.
    if view != VIEW_MESSAGE:
      self.clear_reply()

    # Only remember a place worth returning to. Walking node → node must not
    # make the way back a chain of nodes.
    if not self.viewing_detail:
      self.return_view = self.view

    # The channel crumb is the one Jason named: clicking it goes back to the messages
    # on the row the cursor was on, which is what `leave_detail` already does and what
    # the `flush_positions()` above makes true.
    trail: list["str | Crumb"] = [Crumb("Dashboard", CRUMB_DASHBOARD)]
    if self.return_view == VIEW_DIRECT:
      trail.append(Crumb(DIRECT_MESSAGES_LABEL, CRUMB_DIRECT))
    elif self.return_view == VIEW_MESSAGES and self.current_channel_label:
      trail.append(Crumb(self.current_channel_label, CRUMB_MESSAGES))
    trail.append(crumb)

    self.show_view(view)
    self.set_breadcrumbs(*trail)
    self.set_message_status(None)
    self.query_one("#detail", Static).update(body)

    # **A message's detail opens in its reply box; a node's opens in the pane.** Jason's,
    # and it follows from what the two pages are for: this one arms a reply on the way in
    # and the reply *is* the page — see the note in `show_message_detail` — so the box is
    # what the reader came here to use, and landing anywhere else costs a keypress to
    # correct. A node has nothing to type into and its pane is the whole of it.
    #
    # After `show_view`, which is load-bearing: `update_compose()` runs inside it and is
    # what gives the box its `available` class, and Textual refuses focus to a widget
    # that is still `display: none`. Asked rather than assumed, because a read-only
    # console reaches this line with no box to focus and must land on the pane.
    if view == VIEW_MESSAGE and self.compose_available():
      self.query_one("#compose", Input).focus()
    else:
      self.query_one("#detail-pane", VerticalScroll).focus()

    self.sub_title = crumb




  async def leave_detail(self) -> None:
    """Go back to whatever the detail view was opened from.

    A channel is reopened rather than merely redisplayed, because the archive
    may have moved on while the detail was up and the resume path is the one
    that knows where this reader was.

    **Leaving takes the reply with it.** A message's detail arms one on the way in,
    and it means "answer the message this page is about" — a meaning that does not
    survive the page. Carried back into the channel it would sit in the box's border
    title answering something no longer on screen, which is the same reason a reply
    has never been allowed to follow a reader from one conversation into another.
    """
    self.clear_reply()

    if self.return_view == VIEW_DIRECT:
      await self.open_direct_index()
      return

    if self.return_view == VIEW_MESSAGES and self.current_channel_label:
      # A conversation is reopened as a conversation. Coming back from a message's
      # detail into the flat list would silently widen the view a message was read
      # in, and — worse — would take the compose box's recipient away.
      if self.current_peer is not None:
        await self.open_conversation(self.current_peer)
        return

      await self.open_channel(
        is_dm=self.current_is_dm,
        channel_index=self.current_channel_index,
        label=self.current_channel_label,
        land_on=self.detail_message_id,
      )
      return

    self.show_dashboard()




  # -------------------------------------------------------------- conversations
  #
  # A conversation is the direct messages with one peer, and it exists because a
  # compose box needs a recipient that cannot change under an arrow key. The flat
  # direct message list is every conversation at once and so has no recipient at
  # all — which is why it got no box in Phase 4, and why that reasoning still holds
  # for it and no longer holds for this.
  #
  # There are two ways in, and both were wanted: enter on a row of the flat list,
  # which is how a DM you received leads to the thread it belongs to, and enter on
  # a node's detail, which is how you start one with somebody who has not written
  # to you. Neither of them is the cursor deciding where a message goes.


  def can_converse_with(self, node_id: Optional[str]) -> bool:
    """Whether this console can open a conversation with one node.

    Three reasons it cannot, and all of them fail closed:

    - **Direct messages are not being shown.** `SHOW_DIRECT_MESSAGES` is this
      process's decision about whether to display them at all, and a conversation
      displays them. The flat list is gated on it and so is this.
    - **The archive has named no device.** Then which end of a row is the peer is
      unanswerable, so there are no conversations to be in.
    - **It is the attached device itself.** A node cannot hold a conversation with
      itself: every row would be both ends of it, and the collector would be asked
      to transmit to its own id.

    Note what is *not* on this list: whether sending is available. Reading a
    conversation is reading, and the flat list has never needed a collector to be
    running. The compose box inside it is gated separately, by the same three gates
    it has been gated by since Phase 4.
    """
    if not self.show_direct_messages:
      return False
    if self.local_node_id is None or not node_id:
      return False
    return node_id != self.local_node_id




  def conversation_summary(self, peer: str) -> dict[str, Any]:
    """How many direct messages there are with one peer, and how many are unread.

    Zero of each is a real answer and not an absent one: a node you have never
    messaged is still somebody you can message, so the caller renders the block
    either way.
    """
    conversations = self.read(db.fetch_conversations, self.local_node_id) or []
    unread = self.unread_conversation_counts()

    for conversation in conversations:
      if conversation["peer"] == peer:
        return {
          "message_count": conversation["message_count"],
          "unread": unread.get(peer, 0),
        }

    return {"message_count": 0, "unread": 0}




  async def open_direct_index(self) -> None:
    """Show who this device has direct messages with, one row per person.

    **What the sidebar's Direct Messages entry opens, in place of the flat list.**
    That list was every conversation at once in one chronological run, which made it
    the one message view with no recipient — no compose box, and a row that had to
    name both of its ends before you could tell whose it was. Grouping them is
    Jason's call and it removes the exception rather than working around it: every
    direct message is now read inside a conversation, which has a peer, a compose box
    and a read position of its own.

    `fetch_conversations` has returned exactly these rows since Phase 5 and until now
    had one caller, filling in the block on a node's detail page.
    """
    self.flush_positions()

    self.current_is_dm = True
    self.current_channel_index = None
    self.current_peer = None
    self.current_channel_label = DIRECT_MESSAGES_LABEL
    self.sub_title = DIRECT_MESSAGES_LABEL
    self.clear_reply()

    # Nothing here is a message, so nothing here is paged. The window state is
    # cleared rather than left holding whatever channel was open last, so a stale
    # `has_more_newer` cannot make `g` think there is somewhere to jump to.
    self.messages = []
    self.rows = []
    self.row_of_message = {}
    self.has_more_older = False
    self.has_more_newer = False

    self.show_view(VIEW_DIRECT)
    self.set_breadcrumbs(Crumb("Dashboard", CRUMB_DASHBOARD), DIRECT_MESSAGES_LABEL)
    self.set_message_status(None)

    # The list this view borrows was a channel until a moment ago, and two pieces of
    # that channel are still on it: `start_row`, where the first arrow key would
    # land, and `index`, wherever the cursor was left. Neither survives the swap as
    # anything meaningful — both are row numbers into a list that is about to hold a
    # handful of correspondents instead of a page of messages — and a stale `index`
    # past the new end is exactly the cursor Textual walked off the list from, which
    # is the crash this clears. Cleared *before* the rebuild so `rebuild_conversations`
    # does not read the dead cursor as a correspondent worth restoring.
    listing = self.query_one("#messages", MessageList)
    listing.start_row = 0
    listing.index = None

    # The channel's rows are taken down *awaited*, not left to `reconcile`. Reconcile
    # exists so a poll's redraw never shows an empty frame, but this is not a redraw —
    # it is a view swap, and every row on the list is leaving whichever way it goes.
    # What awaiting buys is a `children` list that means what it says: `remove()`
    # queues a `Prune` and a row keeps answering to `children` until it is pumped, so
    # a cursor placed by position now would be placed among ghosts and go stale the
    # moment they leave — which is how the first fix for this crash reintroduced it.
    await listing.remove_children()

    self.rebuild_conversations()

    # The index opens with the newest conversation selected, unlike a channel, which
    # opens with no cursor at all. A channel is read by scrolling and a highlight
    # there would claim a choice the reader had not made; this list exists only to
    # have one of its rows chosen, so the cursor starting on the likeliest row is
    # the choice half-made for them. Found rather than assumed to be row zero, so
    # the "No direct messages" row is never the thing selected.
    for item in listing.children:
      if isinstance(item, ConversationItem):
        listing.index = listing.children.index(item)
        break

    listing.focus()




  def rebuild_conversations(self) -> None:
    """Draw the index: every peer, newest first, with what is waiting from them.

    **Nothing at all when the archive has named no device**, and that guard is not
    belt-and-braces. `_PEER_OF_ROW` is `CASE WHEN from_node = ? THEN to_node ELSE
    from_node END`, and in SQL `from_node = NULL` is NULL rather than false — so the
    CASE falls to its ELSE for every row and each message is filed as a conversation
    with its own *sender*, including the ones this device sent. Without a local node
    there is no way to say which end of a row is the peer, which is the same reason
    `unread_conversation_counts` and `can_converse_with` both refuse; this makes the
    index refuse in the same place rather than inventing correspondents.
    """
    if self.local_node_id is None:
      listing = self.query_one("#messages", ListView)
      reconcile(listing, [self.empty_row(listing, "No direct messages")])
      return

    conversations = self.read(db.fetch_conversations, self.local_node_id) or []
    unread = self.unread_conversation_counts()
    # The left-hand name is this device, in the same short form the rows use for the
    # other end — so `RX1 › POMM` reads as the trail a message took.
    local_label = self.local_short_name()

    listing = self.query_one("#messages", ListView)

    # Which correspondent the cursor was on, so the poll's rebuild does not drag the
    # reader back to the top of the list every ten seconds. Restored by peer rather
    # than by row, because the order is by recency and a message arriving is exactly
    # what moves somebody up it.
    was_on = None
    if listing.index is not None and 0 <= listing.index < len(listing.children):
      selected = listing.children[listing.index]
      was_on = getattr(selected, "peer", None)

    # The accent means unread here as it does on a direct message row, so the list
    # keeps the class that says which of the two meanings is in force.
    listing.add_class("direct")

    # **The list this mattered most for.** The node list is redrawn from the poll
    # only when nobody is holding it; this one is redrawn on every poll the index is
    # open, so the empty frame `reconcile` describes was landing in front of a reader
    # who was looking straight at it, every ten seconds. Keyed on the peer, which is
    # already how the cursor is put back below — a row here *is* a correspondent, and
    # the recency order it is drawn in is the thing a message arriving changes.
    showing = {
      item.peer: item
      for item in listing.children
      if isinstance(item, ConversationItem)
    }

    wanted: list[ListItem] = []
    for conversation in conversations:
      peer = conversation["peer"]
      item = showing.get(peer)
      if item is None:
        item = ConversationItem(conversation, local_label, unread=unread.get(peer, 0))
      else:
        item.set_conversation(conversation, local_label, unread.get(peer, 0))
      wanted.append(item)

    if not conversations:
      reconcile(listing, [self.empty_row(listing, "No direct messages")])
      return

    reconcile(listing, wanted)

    # Put the cursor back on the correspondent it was on, not on the row number it
    # was on — the order is by recency and a message arriving is exactly what moves
    # somebody up it. Cleared first so that a peer who has dropped out of the list
    # entirely leaves no cursor behind rather than leaving it pointing at whoever
    # inherited the row number, which is what `clear()` used to see to.
    #
    # Asked of `wanted` rather than of the children, because a row on its way out is
    # still among the children until its `Prune` is pumped and would have answered to
    # the peer first.
    if was_on is not None:
      listing.index = None
      for item in wanted:
        if getattr(item, "peer", None) == was_on:
          listing.index = listing.children.index(item)
          break




  def local_short_name(self) -> str:
    """This device's short name, for the left-hand side of a conversation row.

    Falls back through the long name to the hex id, the way every other node label
    in this project does — and to "You" when the archive has named no device at all,
    which is the case where there are no conversations to draw anyway.
    """
    if self.local_node_id is None:
      return "You"

    node = self.read(db.fetch_node, self.local_node_id)
    if node is None:
      return self.local_node_id
    return node.get("short_name") or node.get("long_name") or self.local_node_id




  async def open_conversation(
    self,
    peer: str,
    *,
    land_on: Optional[int] = None,
  ) -> None:
    """Show the direct messages with one peer, and nobody else.

    The peer is fixed by opening the view and is not read back off the cursor
    afterwards, which is the whole property this view exists to have.
    """
    node = self.read(db.fetch_node, peer)
    label = format_peer_label(node, peer)

    await self.open_channel(
      is_dm=True,
      channel_index=None,
      label=label,
      peer=peer,
      land_on=land_on,
      # Named as direct messages however it was reached, because that is what it
      # is — and because the sidebar has no entry to highlight for a conversation,
      # which is the situation breadcrumbs were added for. Both steps are clickable,
      # so the middle one is the way back to the index of correspondents, which is
      # also where `escape` goes.
      trail=(
        Crumb("Dashboard", CRUMB_DASHBOARD),
        Crumb(DIRECT_MESSAGES_LABEL, CRUMB_DIRECT),
        label,
      ),
    )




  async def open_conversation_for(self, message: dict[str, Any]) -> None:
    """Open the conversation one direct message belongs to, landing on it."""
    peer = self.peer_of(message)

    if peer is None:
      # No device named, so which end of this row is the peer is unanswerable.
      # The row's own detail is still worth showing, and is what enter did here
      # before conversations existed.
      self.notify(
        "This archive has not named a device, so there is no way to tell whose "
        "conversation this is.",
        severity="warning",
      )
      self.show_message_detail(message["message_id"])
      return

    await self.open_conversation(peer, land_on=message["message_id"])




  async def action_open_conversation(self) -> None:
    """Open the conversation with the node whose detail is on screen.

    Bound to `enter`, which reaches this only from a detail pane: a ListView and an
    Input both claim `enter` for themselves before an app binding is consulted, so
    this cannot steal the key from opening a row or from sending a message.
    Verified against textual 8.2.8 rather than assumed, because it is the kind of
    thing that would be found by a message going somewhere unexpected.
    """
    if self.view != VIEW_NODE or self.detail_node_id is None:
      return
    if not self.can_converse_with(self.detail_node_id):
      return

    await self.open_conversation(self.detail_node_id)




  # ------------------------------------------------------------------- messages


  async def open_channel(
    self,
    *,
    is_dm: bool,
    channel_index: Optional[int],
    label: str,
    peer: Optional[str] = None,
    land_on: Optional[int] = None,
    trail: Optional[tuple["str | Crumb", ...]] = None,
  ) -> None:
    """Show one channel, conversation or the flat DM list, resuming where you were.

    **The name is kept even though a conversation is not a channel**, because this
    already opened the flat direct message list, which is not one either: what it
    has always meant is "show a message view and resume in it". Everything under it
    — the resume window, the pager, the read marker, the compose box — is the same
    work for all three, and splitting it would have meant two copies of the part
    that is easy to get subtly wrong.

    `land_on` overrides the resume position with a specific message, for arriving
    from a row that named one. `trail` overrides the breadcrumbs, so a conversation
    reached from the flat list can say it came through there.
    """
    self.flush_positions()

    # **Raised here rather than in `position_message_pane`, and the difference is
    # the whole of an intermittent bug.** Rendering a window awaits, and a pane that
    # is mid-rebuild fires scroll events while it does — so the flag has to be up
    # before the first await, not after the last one. Set late, reopening a channel
    # advanced the read marker past an unread message about two runs in five,
    # depending on how many refreshes landed before the pane was positioned.
    epoch = self.begin_positioning()

    self.current_is_dm = is_dm
    self.current_channel_index = channel_index
    self.current_peer = peer
    self.current_channel_label = label
    self.sub_title = label

    # A reply is answered in the view it was raised in. Carrying one across would
    # mean a send addressed to this conversation quoting a message from the last.
    self.clear_reply()

    self.show_view(VIEW_MESSAGES)
    self.set_breadcrumbs(*(trail or (Crumb("Dashboard", CRUMB_DASHBOARD), label)))
    messages_view = self.query_one("#messages", ListView)
    # Which of the two things the accent bar means here. A class on the list rather
    # than on every row, because it is a fact about the view: in direct messages the
    # header names both ends, so the margin is free to say "not read yet" instead of
    # "mine". See the stylesheet.
    messages_view.set_class(is_dm, "direct")

    if land_on is not None:
      # Arrived pointing at a particular message, so that is where to be — a
      # reader who pressed enter on a row has said which one they mean, and it is
      # a stronger statement about where to land than a remembered position is.
      loaded = self.load_window_around(land_on)
    else:
      position = self.positions.get(*self.current_scope())
      loaded = self.load_resume_window(position) if position else None

    if loaded is None:
      # Never read, or the remembered message has since been pruned. RxOnly
      # loads the oldest page here so the channel can be read from its start,
      # and pre-marks nothing as read; this does the same.
      loaded = self.load_oldest_window()

    if loaded is None:
      self.messages = []
      self.rebuild_rows()
      await self.render_rows()
      self.set_message_status("Could not read the archive.")
      # No box over an unreadable channel: a message sent here would be archived
      # into something this console has just failed to read.
      #
      # `send_state` is deliberately left alone. This is one view vetoing its own
      # box, not a change in whether sending is set up — the menu's Sending line
      # describes the arrangement between this console and its collector, and a
      # channel that would not load says nothing about either.
      self.send_available = False
      self.update_compose()
      self.refresh_bindings()
      self.end_positioning(epoch)
      return

    await self.render_rows()

    # **Nothing is selected, and the pane scrolls instead.** That is the same landing
    # the cursor used to make; what is gone is the cursor, because a highlight on an
    # arbitrary row claimed a choice the reader had not made. Where it scrolls to is
    # `position_message_pane`, which has the two landings and the argument for them.
    messages_view.index = None

    # **A channel with nothing unread in it opens at its end, and one with something
    # left to read opens at the resume point.** `land_on` is neither: arriving from a
    # row that named a message is a statement about where to be that outranks both,
    # so it keeps the resume landing on the message it asked for.
    to_end = land_on is None and self.messages_fully_read()

    # The read row is where the first arrow key lands. At the end that is the last
    # message — the reader is up to date, so the message they are up to is the newest
    # one, not whatever the marker happened to stop on.
    self.set_read_row(len(self.rows) - 1 if to_end else self.resume_index)
    self.position_message_pane(self.resume_index, to_end=to_end)
    messages_view.focus()
    self.update_message_status()

    # Last, so the box is labelled with the channel that is now open rather than
    # the one that was.
    self.update_compose()
    self.refresh_bindings()




  def load_resume_window(
    self,
    position: dict[str, int],
  ) -> Optional[list[dict[str, Any]]]:
    """Older context, the remembered message, and everything after it.

    Returns None when the remembered message is gone from the archive, which is
    the collector having pruned past it — the caller falls back to the newest page.
    """
    return self.load_window_around(position["message_id"])




  def load_window_around(self, message_id: int) -> Optional[list[dict[str, Any]]]:
    """One page either side of a named message, with that message in the middle.

    What resuming has always done, with the message named directly rather than
    taken out of a stored position — because there is now a second way to arrive
    somewhere specific: pressing enter on a row of the flat direct message list,
    which lands in that peer's conversation on that message.

    None when the message is not in the archive, which for a resume is the
    collector having pruned past it. The caller falls back to a fresh load.
    """
    anchor = self.read(db.fetch_message, message_id, self.current_is_dm)
    if anchor is None:
      return None

    cursor = db.cursor_of(anchor)

    older = self.read(
      db.fetch_message_page,
      self.current_is_dm,
      self.current_channel_index,
      peer=self.current_peer,
      before=cursor,
      limit=self.page_size,
    )
    newer = self.read(
      db.fetch_message_page,
      self.current_is_dm,
      self.current_channel_index,
      peer=self.current_peer,
      after=cursor,
      limit=self.page_size,
    )

    if older is None or newer is None:
      return None

    self.messages = older["messages"] + [anchor] + newer["messages"]
    self.has_more_older = older["meta"]["has_more_older"]
    self.has_more_newer = newer["meta"]["has_more_newer"]
    self.oldest_cursor = older["meta"]["oldest"] or cursor
    self.newest_cursor = newer["meta"]["newest"] or cursor

    # The anchor message, by id rather than by position. `rebuild_rows()`
    # turns that into a row index — and if it has since
    # become a tapback absorbed into another row, it resolves to the row holding
    # it, so a position recorded before this slice still resumes somewhere real.
    self.resume_message_id = anchor["message_id"]
    self.rebuild_rows()

    return self.messages




  def load_oldest_window(self) -> Optional[list[dict[str, Any]]]:
    """The start of the channel, for a channel that has never been read."""
    return self.load_window(newest=False)




  def load_newest_window(self) -> Optional[list[dict[str, Any]]]:
    """The live end of the channel."""
    return self.load_window(newest=True)




  def load_window(self, *, newest: bool) -> Optional[list[dict[str, Any]]]:
    """Load one edge of the channel and adopt it as the open window.

    The fetch and the edge arithmetic are in `window.py`, which needs nothing
    from the App but `read` and the scope. What stays here is adopting the six
    fields it produces, and the row rebuild, which is the App's.
    """
    window = fetch_edge_window(
      self.read,
      is_dm=self.current_is_dm,
      channel_index=self.current_channel_index,
      peer=self.current_peer,
      newest=newest,
      limit=self.page_size,
    )
    if window is None:
      return None

    self.messages = window.messages
    self.has_more_older = window.has_more_older
    self.has_more_newer = window.has_more_newer
    self.oldest_cursor = window.oldest_cursor
    self.newest_cursor = window.newest_cursor
    self.resume_message_id = window.resume_message_id

    self.rebuild_rows()

    return self.messages




  def is_outbound(self, message: dict[str, Any]) -> bool:
    """Whether this device sent this message.

    `from_node == meta.local_node_id`, which is settled decision 4 and needs no
    column that was not already there. It works for both tables and in both
    directions: nothing but the collector's own send path ever writes a row
    attributed to the local node, because LoRa is half-duplex and the radio never
    hears itself.

    False when the archive has not named a device, which is honest — an
    unattributable row is better left looking like everyone else's than claimed.
    """
    if self.local_node_id is None:
      return False
    return message.get("from_node") == self.local_node_id




  # ------------------------------------------------------- messages into rows
  #
  # A tapback is a reaction rather than a message, so it is drawn on the message
  # it answers instead of taking a line of its own — which is what RxOnly does,
  # and which is the one thing in this slice that changes what a row *is*.
  #
  # `self.messages` stays the archive's list, unchanged and in archive order.
  # `self.rows` is derived from it and is what the ListView holds. The rule for
  # keeping them straight is short: anything that came from the ListView is a row
  # index, and anything that goes into a cursor or a read position is a message.


  def rebuild_rows(self) -> None:
    """Re-derive `rows`, `row_of_message` and `resume_index` from `messages`.

    The tapback rules that decide which messages become rows are in `ui/rows.py`,
    which needs nothing from the App but the two fields passed in.
    """
    self.rows, self.row_of_message, self.resume_index = derive_rows(
      self.messages, self.resume_message_id,
    )




  def is_unread(self, message: dict[str, Any]) -> bool:
    """Whether this message is still waiting to be read, in the view that is open.

    The same test the sidebar's counts make, asked of one message instead of a
    table: after this scope's read marker, and not one of ours.

    **A message this device sent is never unread**, which is Jason's rule and the
    same one the counts adopted: you were there when it went out. What makes a
    conversation wait for attention again is a reply arriving after it, and that
    reply is somebody else's message and unread on its own account.

    No marker means nothing in this view has been read, so everything inbound in it
    is — which is the first-run case rather than an edge case.
    """
    if self.is_outbound(message):
      return False

    scope, key = self.current_scope()
    cursor = self.positions.cursor(scope, key)
    if cursor is None:
      return True

    return (message["rx_time"], message["id"]) > cursor




  def build_message_item(self, row: dict[str, Any]) -> MessageItem:
    """The single place a message row is constructed.

    Takes a derived row rather than a message, so that whether something is
    yours and what has been said back to it are both settled before the widget
    exists — the widget renders, it does not decide.
    """
    message = row["message"]
    return MessageItem(
      message,
      outbound=self.is_outbound(message),
      unread=self.is_unread(message),
      tapbacks=row["tapbacks"],
      orphan=row.get("orphan_tapback", False),
    )




  async def render_rows(self) -> None:
    """Bring the message list in line with `self.rows`, touching only what changed.

    This cleared and refilled the list wholesale until scrolling back through a
    long channel was watched doing it: every widget on screen destroyed and
    rebuilt for one page of history, which the reader saw as the channel blinking
    empty while the scroll position waited a refresh for `restore_viewport`. The
    argument for wholesale — an arriving page can change rows already on screen,
    because a tapback in it absorbs into a parent above — is real, but it names
    which rows cannot be kept rather than condemning the rest. **A rendered row
    carries on exactly when it still draws the same message with the same
    reactions**; a row whose reactions changed is replaced where it stands, a row
    whose message left the window (absorbed by a parent paging in) is removed, and
    the new page's rows are spliced in around what survives. The comparison is by
    id, so nothing here re-derives what `rebuild_rows` already decided.

    Both orders come from the archive, which is what makes the splice this simple:
    whatever both sides hold, they hold in the same sequence, so keeping is never
    reordering.

    The wholesale path remains for the states where nothing on screen is a message
    row — the empty-channel placeholder, and the conversation index, which shares
    this ListView. A fresh channel replaces everything through the same diff:
    nothing matches, nothing is kept, which is the old behaviour arrived at
    honestly.

    The cursor is kept by message, not by position — see `restore_cursor`.
    """
    messages_view = self.query_one("#messages", ListView)

    if not self.rows:
      await messages_view.clear()
      await messages_view.append(ListItem(Label("No messages in this channel.")))
      return

    def signature(message: dict[str, Any], tapbacks: list[dict[str, Any]]) -> tuple:
      return (
        message["message_id"],
        tuple(tapback["message_id"] for tapback in tapbacks),
      )

    on_screen = list(messages_view.children)
    if not all(isinstance(item, MessageItem) for item in on_screen):
      await messages_view.clear()
      await messages_view.extend(self.build_message_item(row) for row in self.rows)
      return

    wanted = [signature(row["message"], row["tapbacks"]) for row in self.rows]
    wanted_set = set(wanted)

    stale = [
      position for position, item in enumerate(on_screen)
      if signature(item.message, item.tapbacks) not in wanted_set
    ]
    if stale:
      await messages_view.remove_items(stale)

    kept = {
      signature(item.message, item.tapbacks)
      for item in on_screen
    } & wanted_set

    # Splice the new rows in around the survivors, in batches of neighbours. The
    # insertion index is the row's own position, because everything before it in
    # `self.rows` is already in the list by the time its batch goes in — kept rows
    # never moved, and earlier batches are mounted before later ones are placed.
    batch: list[MessageItem] = []
    batch_at = 0
    splices: list[tuple[int, list[MessageItem]]] = []
    for position, row in enumerate(self.rows):
      if wanted[position] in kept:
        if batch:
          splices.append((batch_at, batch))
          batch = []
        continue
      if not batch:
        batch_at = position
      batch.append(self.build_message_item(row))
    if batch:
      splices.append((batch_at, batch))

    for position, items in splices:
      if position >= len(messages_view.children):
        await messages_view.extend(items)
      else:
        await messages_view.insert(position, items)




  def restore_cursor(self, message_id: Optional[int]) -> None:
    """Put the cursor back on the row holding one message.

    Positions shift whenever a page arrives, and after this slice they shift by
    an amount that is not the length of the page — absorbing two tapbacks out of
    a fifty-message page moves everything below it by forty-eight. Naming the
    message instead of counting the rows is both simpler and right by
    construction.
    """
    if message_id is None:
      return

    index = self.row_of_message.get(message_id)
    if index is None:
      return

    self.query_one("#messages", ListView).index = index




  async def load_older(self) -> None:
    """Prepend the previous page, keeping the cursor on the same message."""
    if self.is_loading or not self.has_more_older or self.oldest_cursor is None:
      return
    # Not while a channel is being put where it belongs: a page arriving mid-open
    # renumbers the resume row under the deferred scroll that is about to use it.
    # The poll is the one caller that can still get here during that window.
    if self.positioning:
      return

    self.is_loading = True
    try:
      page = self.read(
        db.fetch_message_page,
        self.current_is_dm,
        self.current_channel_index,
        peer=self.current_peer,
        before=self.oldest_cursor,
        limit=self.page_size,
      )
      if page is None:
        return

      older = page["messages"]
      if not older:
        self.has_more_older = False
        return

      self.has_more_older = page["meta"]["has_more_older"]
      self.oldest_cursor = page["meta"]["oldest"]

      # Where the reader is, before the window changes underneath them: the row at
      # the top of the pane, and the selection if there is one. Both are named by
      # message rather than by position, because an older page can absorb reactions
      # that were rows a moment ago — the number of new rows is not the number of
      # new messages, so nothing here can be restored by counting.
      anchor = self.viewport_anchor()
      held = self.message_at_cursor()

      # The batch holds every paint until the splice is in and the pane is back on
      # its anchor. The splice awaits, and an await is an opening for the screen's
      # own update to draw the list half-changed — which the reader saw, every
      # page, as the channel jumping and settling.
      with self.batch_update():
        self.messages = older + self.messages
        self.rebuild_rows()
        await self.render_rows()

        self.restore_cursor(held)
        # After the selection, so it wins: setting an index scrolls the list to
        # show it, which would otherwise undo this.
        self.restore_viewport(anchor)

    finally:
      self.is_loading = False
      self.update_message_status()




  async def load_newer(self) -> None:
    """Append the next page toward the live end of the channel.

    **The read marker is re-measured afterwards, because a page arriving here can
    move what "the end" is without moving anything on screen.** A reaction in the
    arriving page attaches to a message already on the list rather than becoming a
    row of its own, so the pane does not grow, nothing scrolls, and none of the
    events that drive `mark_read_from_viewport` fire — while the archive's newest
    message is now one the stored position sits behind.

    Nothing could clear that afterwards either, and that is what made it a stuck
    count rather than a late one. Reading is scrolling now, and a reader already at
    the bottom of a fully loaded channel has nowhere left to scroll; the reaction
    has no row to walk the cursor onto, because it was absorbed into one. So a `🎉`
    from anybody else on the newest message left the channel showing one unread with
    the last message of it on screen and read — Jason's report, and the exact state
    the bottom-of-a-fully-loaded-channel branch of `mark_read_from_viewport` exists
    to prevent. It was never reached because nothing asked it.

    In the `finally` so every exit re-measures, including the two that change what
    the end means without appending anything: an empty page lowers `has_more_newer`,
    which is the other half of that branch's test. It is safe to ask on the paths
    where nothing moved — `ReadPositions.set` only ever moves a marker forward, so a
    re-measure that computes an older row than the stored one records nothing.

    `call_after_refresh` rather than a direct call, for the reason the guards inside
    `mark_read_from_viewport` are there at all: a row that has just absorbed a
    reaction is a line taller than the layout that measured it, and reading its
    `virtual_region` before the next pass measures the channel as it was one line
    ago. Deferring by a refresh is how the rest of this app waits for geometry.
    """
    if self.is_loading or not self.has_more_newer or self.newest_cursor is None:
      return
    # Same stand-down as `load_older`, same reason.
    if self.positioning:
      return

    self.is_loading = True
    try:
      page = self.read(
        db.fetch_message_page,
        self.current_is_dm,
        self.current_channel_index,
        peer=self.current_peer,
        after=self.newest_cursor,
        limit=self.page_size,
      )
      if page is None:
        return

      newer = page["messages"]
      if not newer:
        self.has_more_newer = False
        return

      self.has_more_newer = page["meta"]["has_more_newer"]
      self.newest_cursor = page["meta"]["newest"]

      anchor = self.viewport_anchor()
      held = self.message_at_cursor()

      # The same paint-holding batch as `load_older`, same reason.
      with self.batch_update():
        self.messages = self.messages + newer
        self.rebuild_rows()
        await self.render_rows()

        # Appending cannot move anything by itself, but absorbing can: a reaction
        # in the arriving page attaches to a message above the viewport and takes
        # a row away from underneath it.
        self.restore_cursor(held)
        self.restore_viewport(anchor)

    finally:
      self.is_loading = False
      self.update_message_status()
      self.call_after_refresh(self.mark_read_from_viewport)




  async def show_sent_message(self) -> None:
    """Land on what was just sent, from wherever it was sent from.

    From the message list this is `show_newest` and has been since Phase 4: the row
    the collector wrote is at the live end, and reloading from the archive is how it
    gets on screen. `take_focus` is false there because the reader is still in the
    compose box and may be typing the next one — having the cursor jump out mid-word
    was the thing that argument was written about.

    **From a message's detail it is a step further out**, because the row that was
    just written is in the channel and a detail pane cannot show it. So the view goes
    back to the channel first — `leave_detail`, the same path `escape` takes, which
    also drops the reply the page had armed — and then to its live end. Focus is
    taken here rather than declined: the box being left behind is the one the message
    was typed in, so the keyboard belongs with the messages, on the reply that just
    landed.
    """
    if self.view == VIEW_MESSAGE:
      await self.leave_detail()
      await self.show_newest()
      return

    await self.show_newest(False)




  async def action_jump_newest(self) -> None:
    """Drop the loaded window and reload the live end of the channel."""
    await self.show_newest()




  async def show_newest(self, take_focus: bool = True) -> None:
    """Reload the live end of the channel and put the cursor on it.

    `take_focus` is false when this follows a send: the message list scrolling to
    what you just sent is right, and having the cursor jump out of the compose box
    you are still typing in is not.
    """
    if not self.viewing_messages:
      return

    if self.load_newest_window() is None:
      return

    await self.render_rows()

    messages_view = self.query_one("#messages", ListView)
    if self.rows:
      # The live end is the bottom of the pane, not a row under a selection. Nothing
      # is selected here for the same reason nothing is selected when a channel
      # opens, and `mark_read_from_viewport` takes the bottom-of-a-loaded-channel
      # branch, so jumping to newest still reads the channel to its end.
      messages_view.index = None
      self.set_read_row(len(self.rows) - 1)
      messages_view.scroll_end(animate=False, immediate=True)
      self.mark_read_from_viewport()
    if take_focus:
      messages_view.focus()

    self.update_message_status()




  def action_menu(self) -> None:
    """ctrl+p. Four commands in a list, in place of Textual's command palette.

    The palette is a fuzzy search over everything an app can do, which is the
    right shape for hundreds of commands and the wrong one for four — a search
    box over four rows is a quiz. Jason's call. What the palette carried is
    re-offered here: refresh (which left the footer when the poll took over most
    of its job — the entry is how a reader still finds `r`), and the theme
    switch, which had no other path and is what keeps `rxonly-light` reachable.

    The entries are built at open so the theme row can say which way it will
    switch, and the screen reports a key back rather than acting itself — the
    dispatch stays here, where the actions live.

    The header above them is built here for the same reason and one more: three of
    its four facts are archive reads, and an archive read that fails has to leave
    the menu openable. `self.read` returns None on a dropped connection, and every
    line below has something true to say about None.
    """
    dark = self.theme == RXONLY_DARK.name
    self.push_screen(
      MenuScreen(
        (
          # **Not "raw", which it usually is not.** The collector tidies its own
          # output before writing it — dropping heartbeats, joining records,
          # decoding payloads — and "raw" promised the opposite of what arrives.
          # It cannot say "tidy" either: `TIDY_LOGS` is the collector's setting,
          # this process cannot read it, and `LOG_COMMAND` can be pointed at any
          # command on any host, so what this pane shows need not come from the
          # collector whose archive is open. Naming the source is the one claim
          # that is true in every arrangement. Jason's, 2026-08-07.
          ("logs", "View collector log"),
          ("refresh", "Refresh"),
          ("theme", "Switch to light theme" if dark else "Switch to dark theme"),
          ("quit", "Quit"),
        ),
        header=self.menu_facts(),
        title=f" mesh-console {__version__} ",
      ),
      self.menu_chosen,
    )


  def menu_facts(self) -> tuple[tuple[str, str], ...]:
    """The four lines above the rule: what this program is and what it is reading.

    Read at open rather than kept current, which is the right trade for a modal
    that is on screen for a second or two — and an easy one now that every fact
    here changes on the collector's schedule rather than the mesh's: firmware
    moves when the device is reflashed and the collector restarts, which is
    never while this menu is up.

    Every line survives an unreadable archive, because `self.read` answers None
    and the menu has to open regardless. A dropped connection reports `unknown`
    schema and `unknown` firmware, which is what this process actually knows at
    that moment.
    """
    # The file, not the path. The path is long, often absolute, and identifies the
    # host rather than the archive; the basename is what distinguishes two archives
    # from each other, which is the question this line exists for.
    db_path = str(Config.get("DB_PATH", "") or "")
    archive = Path(db_path).name if db_path else "none configured"

    # Both halves, because the archive's own version cannot be wrong on its own —
    # this console refuses to open anything below its floor, so a mismatch would
    # have stopped it at startup. What this pair *can* show is the console being
    # behind its collector: an archive at 0.11.0 against a reader that requires
    # 0.10.0 is reading a schema with columns it knows nothing about.
    schema = self.read(db.get_meta, "schema_version") or "unknown"

    # Two reads and one line: the version is the device's own claim, recorded by
    # the collector at its last startup; the channel is the collector's lookup
    # against Meshtastic's release listing, because the device does not know its
    # own channel. Either read failing leaves something true to print — see
    # format_firmware for what each absence means.
    #
    # This line replaced "Updated" (how long since anything arrived), and the
    # trade is real: the menu no longer answers "is this archive still being
    # written?" anywhere. Jason's call, 2026-08-15 — the firmware of the radio
    # he is physically flashing is the fact he was opening this menu for.
    firmware = format_firmware(
      self.read(db.get_meta, "firmware_version"),
      self.read(db.get_meta, "firmware_channel"),
    )

    return (
      ("Archive", archive),
      ("Schema", f"{schema} · reads {REQUIRED_SCHEMA}+"),
      ("Firmware", firmware),
      ("Sending", self.send_state),
    )


  def menu_chosen(self, choice: Optional[str]) -> None:
    """Run what the menu picked. None is escape, and does nothing.

    Every branch but one is a return to the main screen, and says so — the poll
    stopped drawing while this was open and `screen_returned` is what starts it
    again. The exception is the viewer, which is not a return at all: the main
    screen is covered again in the same breath, and the clock the guard keeps should
    go on running across the handover rather than be reset by the instant between two
    modals. Pushed with the same callback, so the viewer's own close is the return.
    """
    if choice == "logs":
      self.push_screen(
        LogViewerScreen(Config.get("LOG_COMMAND", "")), self.screen_returned
      )
      return

    self.screen_returned()

    if choice == "refresh":
      # Through the scheduler because this callback is synchronous and refresh
      # is a coroutine; `call_later` awaits it on the message pump.
      self.call_later(self.action_refresh)
    elif choice == "theme":
      self.theme = (
        RXONLY_LIGHT.name if self.theme == RXONLY_DARK.name else RXONLY_DARK.name
      )
    elif choice == "quit":
      self.call_later(self.action_quit_console)




  async def action_refresh(self) -> None:
    self.refresh_sidebar()

    # Re-ask whether a collector is listening. It is the one piece of state here
    # that can change without the archive changing, so a refresh is how a console
    # started before its collector finds out.
    self.assess_sending()

    if self.viewing_messages:
      await self.load_newer()
    else:
      self.refresh_dashboard()




  def set_message_status(self, text: Optional[str]) -> None:
    status = self.query_one("#messages-status", Static)
    if text:
      status.update(text)
      status.add_class("visible")
    else:
      status.remove_class("visible")




  def update_message_status(self) -> None:
    if not self.viewing_messages:
      self.set_message_status(None)
      self.refresh_bindings()
      return

    if self.has_more_newer:
      self.set_message_status("Newer messages below — press g to jump to newest")
    else:
      self.set_message_status(None)

    # The status line and `g` answer the same question — is there anything below? —
    # so the footer is re-asked wherever the answer is recomputed. Everything that
    # moves the loaded window already calls this: opening a channel, either pager,
    # the jump itself, and the poll noticing something newer.
    self.refresh_bindings()




  # -------------------------------------------------------------------- sending
  #
  # Everything in this section is about *asking*. mesh-collector holds the serial
  # port and is the only process that transmits or writes the archive, including
  # the row for a message composed here. This console opens a socket, says what it
  # would like sent, and reports what came back.


  def assess_sending(self) -> None:
    """Work out whether to offer a compose box, cheapest question first.

    Only the last of these costs a socket round trip, and it is the only one that
    is authoritative — `accepts_transmit` says the collector was serving a socket
    when it last started, which is a fact about the past. A reader that offered a
    compose box on the strength of it would be failing open.
    """
    self.send_available = False
    self.send_unavailable_reason = None

    # Rebuilt below if the gates still open. A send already in flight is unaffected
    # — the worker took its own reference before this could run.
    self.sender = None

    if not self.send_configured:
      self.send_state = SEND_DISABLED
      self.update_compose()
      return

    if send is None:
      # Asked for, and impossible: the install has no mesh-link on its import
      # path. Said out loud rather than left as a box that never appears, because
      # a missing extra is a configuration mistake and silence looks identical to
      # "the collector is not running".
      self.send_state = SEND_MISCONFIGURED
      self.send_unavailable_reason = (
        "ENABLE_SEND is on but mesh-link is not installed, so this console "
        "cannot send. Reinstall with `uv sync --extra send`, or turn ENABLE_SEND "
        "off."
      )
      self.notify(self.send_unavailable_reason, severity="warning", timeout=12)
      self.update_compose()
      return

    if not self.read(get_meta_bool, "accepts_transmit"):
      # The collector last came up without a control socket. Nothing to ask, and
      # asking would mean a connect attempt that is certain to fail.
      #
      # Misconfigured rather than unavailable, and it is the softer of the two
      # misconfigurations: this console was told to offer sending and the archive
      # says its writer does not carry messages, which is a contradiction between
      # two processes' settings rather than a mistake in either one. A collector
      # deliberately started without a socket lands here too, so the word is about
      # the arrangement and not an accusation.
      self.send_state = SEND_MISCONFIGURED
      self.send_unavailable_reason = (
        "The collector is not serving a control socket, so there is nothing to "
        "send through."
      )
      self.update_compose()
      return

    self.sender = send.Sender()
    # Every gate that can be answered cheaply is open; the socket has the last word
    # and is being asked in a thread. Anyone reading the state before the answer
    # arrives gets the truth about where it stands rather than a guess at what it
    # will be — see `on_probe_finished`.
    self.send_state = SEND_CHECKING
    self.update_compose()
    self.probe_collector()




  @work(thread=True, group="probe", exclusive=True)
  def probe_collector(self) -> None:
    """Ask a collector whether it is there, off the event loop.

    In a thread because the answer involves connecting to a socket and waiting,
    and a two-second wait on the event loop is two seconds of frozen interface at
    startup. Nothing goes on the air: this is a status request, which exists so
    the question can be asked without keying up.
    """
    sender = self.sender
    if sender is None:
      return

    available = sender.is_available()
    self.call_from_thread(self.on_probe_finished, available)




  def on_probe_finished(self, available: bool) -> None:
    self.send_available = available

    # The socket had the last word. Not misconfigured either way: everything this
    # console could check was right, and all that is left is whether anything is
    # listening — which is a fact about a running process, not about settings.
    self.send_state = SEND_ENABLED if available else SEND_UNAVAILABLE

    if not available and self.send_unavailable_reason is None:
      self.send_unavailable_reason = (
        f"Nothing answered at {send.socket_path()}, so there is nothing to send "
        f"through. Press r once the collector is running."
      )

    self.update_compose()
    self.refresh_bindings()




  def compose_available(self) -> bool:
    """Whether a message could be composed right now.

    Sending has to be configured, possible and answering — and there has to be
    exactly one place for a message to go, which the view itself has to name.

    **This is where the Phase 4 deferral was written down, and the reason it gave
    still stands.** The flat direct message list is excluded, and not because it is
    direct messages: because it is *every* conversation at once, so there is no
    recipient a box could be addressed to, and taking the peer from whichever row
    the cursor sits on would change the destination under an arrow key. A
    conversation has a peer that belongs to the view rather than to the cursor,
    which is precisely what was missing, so it gets a box and the flat list still
    does not.

    **A message's detail gets one too, and passes the same test.** Jason asked for
    it — open a message, and there is a field to answer it in. That reads like a new
    exception and is the opposite of one: a detail pane shows exactly one message, so
    its destination belongs to the view every bit as much as a conversation's peer
    does, and there is no cursor within reach of it. What disqualified the flat list
    was never "it is not the message list"; it was that the recipient would have come
    from whichever row happened to be highlighted.

    The destination needs no new plumbing, because a message's detail can only be
    reached from the channel or conversation the message is in: `on_list_view_selected`
    is the only route to it, and from the flat list `enter` opens the conversation
    instead. So `current_channel_index` and `current_peer` still describe where the
    message lives while its detail is up, which is exactly where an answer to it
    belongs.
    """
    if not (self.send_available and (self.viewing_messages or self.view == VIEW_MESSAGE)):
      return False

    if self.current_is_dm:
      return self.current_peer is not None

    return self.current_channel_index is not None




  def reply_available(self) -> bool:
    """Whether the highlighted row is something the next send could answer.

    Needs a compose box — a reply is a send — and a row under the cursor to
    reference. **An absorbed tapback has no row of its own, so it cannot be
    highlighted and therefore cannot be replied to**, which is not a limitation
    worth working around: the pill sits on the message it answers, so replying to
    that message is what its position already implies.

    **`viewing_messages` is load-bearing here and was not always needed.** While a
    compose box implied an open message list, `compose_available()` said this on its
    own. It no longer does: a message's detail has a box, and the hidden `#messages`
    list underneath still holds its rows and its index — so without this test `R`
    would be offered on a detail page and would arm a reply to whatever row the
    cursor had been left on, which is not the message on screen. The detail page has
    its own answer to that and needs no key: its box is already a reply.
    """
    return (
      self.viewing_messages
      and self.compose_available()
      and self.message_row_at_cursor() is not None
    )




  def typing_in_a_box(self) -> bool:
    """Whether the cursor is in one of the two text boxes.

    Both of them are `Input`s, and a focused `Input` consumes every printable key
    before Textual looks for a binding — which is what makes typing `q` in the
    compose box type a q rather than quitting. The footer is the other half of that
    fact: a letter advertised while the reader is typing is a letter that does
    something else entirely.
    """
    focused = self.focused
    return focused is not None and focused.id in ("compose", "node-filter")




  def on_paste(self, event: events.Paste) -> None:
    """Give the macOS emoji picker's insertion somewhere to land.

    Picking an emoji on a Mac does not arrive as a keystroke. The terminal receives
    it as a bracketed paste, and it is delivered while the picker window is still
    the thing holding the OS focus. Textual answers that focus loss by clearing
    focus outright — `App._watch_app_focus` calls `screen.set_focus(None)` and
    remembers who had it — so by the time the paste arrives `App.on_event` has no
    focused widget to route it to and hands it to the screen instead, which has no
    handler for a paste. The emoji lands on the floor, and from the reader's side
    the box they were typing in simply ignores them.

    **Reaching this handler is itself the test that the paste was unclaimed.** A
    focused `Input` consumes a paste in `Input._on_paste` and calls `event.stop()`
    on it, so nothing that landed properly ever arrives here. That is why this asks
    only *where* the text belongs and never whether something is focused: the first
    version of this checked `self.focused is None` and refused every paste that
    arrived after focus had been restored, which was most of them.

    Guarded on the target still being on screen, because `#compose` is hidden in
    the views with nowhere to send. An emoji quietly inserted into a box that
    nobody can see is a worse answer than the dropped keystroke it replaces.
    """
    if not event.text:
      return

    target = self._paste_target()
    if target is None:
      return

    # Same first-line-only rule the `Input` itself applies to a paste, so a
    # multi-line clipboard behaves here exactly as it would when focused.
    event.stop()
    target.insert_text_at_cursor(event.text.splitlines()[0])




  def _paste_target(self) -> Optional[Input]:
    """Which box an unclaimed paste belongs in, or None to let it fall.

    First choice is the box that had the keyboard before the picker took the focus
    away, which Textual set aside in `_last_focused_on_app_blur`.

    That is empty more often than it looks. Textual clears it again on the way back
    in, and the widget it held is only a text box if the reader was already typing —
    reply to a message and the keyboard is on the message list, which is focusable
    and cannot hold text. So there has to be a fallback, and which box it picks is
    the whole substance of this method.

    **`PASTE_TARGETS` order, never DOM order.** An earlier version of this took the
    first `Input` on screen, on the belief that only one is ever live at a time. That
    is false: the node filter sits in the sidebar and is on screen in every view,
    including while reading a channel, and it is declared before the compose box —
    so every emoji picked for a reply went into the node filter instead.

    That the reader was *using* the node filter is the case the first branch already
    answers, because then it had the keyboard and was set aside by name. Reaching the
    fallback means a list had the keyboard, and then the box worth filling is the one
    for composing when there is one, and the filter only when there is not.

    Scoped to the current screen so a modal's box cannot be filled from behind it.
    """
    last = self._last_focused_on_app_blur
    if self._is_live_input(last):
      return last

    for selector in self.PASTE_TARGETS:
      for candidate in self.screen.query(selector):
        if self._is_live_input(candidate):
          return candidate

    return None




  @staticmethod
  def _is_live_input(widget) -> bool:
    """Whether this is a text box a reader can actually see."""
    return isinstance(widget, Input) and widget.display and widget.visible




  def can_jump_newest(self) -> bool:
    """Whether there is a live end to jump to that the cursor is not already on.

    Two ways for there to be something below: messages the archive has that this
    window has not loaded (`has_more_newer`, which is also what the status line
    reports), and loaded rows the cursor has not walked down to. Either one means `g`
    moves the reader somewhere; neither means it reloads the window they are already
    looking at, which is a key that appears to do nothing.

    Measured on the cursor rather than the viewport because the cursor is what this
    list navigates with and what `show_newest` moves — a ListView keeps its cursor
    on screen, so a cursor on the last row is a list scrolled to its end.
    """
    if not self.viewing_messages or not self.rows:
      return False
    if self.has_more_newer:
      return True

    index = self.query_one("#messages", ListView).index
    return index is None or index < len(self.rows) - 1




  def check_action(self, action: str, parameters: tuple[object, ...]) -> Optional[bool]:
    """Hide bindings that have nothing to do, rather than showing them inert.

    A read-only console should look like one, not like a console with a feature
    switched off, so this drops `c` out of the footer entirely rather than showing
    it greyed. `escape` is the same argument: it is offered where there is
    somewhere to go back to, and absent on the dashboard where there is not.

    **False is what hides a binding here, not None** — the reverse of what the
    two names suggest. `Screen.active_bindings` skips a binding whose action state
    `is False` and keeps one that returned None with `enabled=bool(None)`, so None
    means "shown, and does nothing", which is the more confusing of the two
    outcomes. Getting this backwards is invisible from the Python and only shows up
    in the footer, which is why there is a check on it.

    **Every branch below is about the panel in focus or the view on screen**, which
    is Jason's ask: a command bar listing what is relevant here rather than
    everything the app can do. `Screen._watch_focused` refreshes the bindings on
    every focus change and `update_message_status` does it when the message window
    moves, so the answers below are re-asked whenever they can have changed.
    """
    # Before everything, including the text-box guard: while a modal is up, this
    # app's keys must not reach the view underneath it. A `d` typed at the menu
    # switching the dashboard behind the dialog is the same lie the text-box
    # guard exists to prevent, one layer up. Only the actions this class owns —
    # `ctrl+q` stays the framework's emergency exit here exactly as it does in
    # the compose box, and for the same reason. The modal's own bindings are its
    # screen's and are not consulted here.
    if action in self.MODAL_SHADOWED and isinstance(self.screen, ModalScreen):
      return False

    # First among the app's own answers: a key the focused box would eat
    # cannot reach its action, so the footer should not offer it. Advertising
    # `q Quit` under a half-typed message is the footer describing a different program
    # from the one the keyboard is talking to.
    if action in self.TEXT_BOX_SHADOWED and self.typing_in_a_box():
      return False

    # Where you already are is not somewhere to go. `d` is the way back to the
    # dashboard from the other three views and does nothing on the dashboard itself.
    if action == "dashboard":
      return True if self.view != VIEW_DASHBOARD else False

    if action == "jump_newest":
      return True if self.can_jump_newest() else False

    # The four keys below are `priority` bindings, which means this class is asked about
    # them *before* the focused widget is. That is the only way to have them at all —
    # a `ListView` and an `Input` both bind all four to their own scrolling — and it is
    # also why each gate has to be exact: a False here falls through to whatever the
    # focused widget wanted the key for, and a True takes it away. `left` and `right`
    # inside the compose box are the case that matters most, since those move through
    # text a reader has typed.
    if action in ("compose_to_read", "compose_to_unread"):
      # Only from the box, and only where there is a panel above it to go to. The flat
      # direct message index has no box, so it never reaches this.
      focused = self.focused
      if focused is None or focused.id != "compose":
        return False
      return True if (self.viewing_messages or self.viewing_detail) else False

    if action == "open_highlighted_channel":
      # `right` belongs to the channel list and to nothing else. A row has to be under
      # the cursor for there to be anything to open — an empty list or a list the reader
      # has not put a cursor in yet answers no.
      focused = self.focused
      if focused is None or focused.id != "channels":
        return False
      return True if self.query_one("#channels", ListView).index is not None else False

    if action == "leave_for_channels":
      # `left` belongs to the message pane. Deliberately not offered from the node list:
      # the nodes are not something the channels opened, so there is nothing to step
      # back out to, and `tab` is the way between the two sidebar panels.
      focused = self.focused
      return True if (focused is not None and focused.id == "messages") else False

    if action == "step_into_nodes":
      # The one action gated on a text box *having* the keyboard rather than on it not
      # having it, which is why it is absent from `TEXT_BOX_SHADOWED` — the guard above
      # would have taken away the only key this exists to provide. Absent from
      # `MODAL_SHADOWED` for a reason that needs no set: with a modal up, `self.focused`
      # is that screen's, so the test below already answers no.
      focused = self.focused
      return True if (focused is not None and focused.id == "node-filter") else False

    if action == "compose":
      return True if self.compose_available() else False

    if action == "reply":
      return True if self.reply_available() else False

    if action == "open_conversation":
      # Advertised only on a node's detail, which is the only place the binding
      # can be reached from anyway — every list and the compose box claim `enter`
      # themselves. Saying so in the footer is how a reader finds the route.
      return True if (
        self.view == VIEW_NODE and self.can_converse_with(self.detail_node_id)
      ) else False

    if action == "open_map":
      # `current_map_url` rather than the view, because a node that has never sent
      # a position has no link on screen and nothing for this to open. It is set
      # by `render_node_detail` on the way past and cleared when the view leaves a
      # node, so it is already the exact answer to "is there a map link here?"
      return True if (self.view == VIEW_NODE and self.current_map_url) else False

    if action == "back":
      # `escape` is the way out of a text box, which is why `back` is deliberately not
      # in `TEXT_BOX_SHADOWED`: it is the one key of this app's own that a focused
      # Input passes through, and the only one the footer keeps offering while the
      # reader is typing.
      in_a_box = self.typing_in_a_box()
      has_reply = self.reply_to_message is not None
      return True if (in_a_box or has_reply or self.view != VIEW_DASHBOARD) else False

    return True




  def update_compose(self) -> None:
    """Show or hide the compose box, and label it with where a message would go.

    The title is the whole of the safety argument for having no confirmation step.
    Phase 4 settled that for a channel — the risk is not *whether* you meant to
    send but *where* it was going — and Jason confirmed it holds for a person, on
    the reasoning that a conversation names its recipient more clearly than a
    channel view names a channel. So the destination is on screen the entire time
    the box is open, and for a peer it is the short name **and** the hex id, because
    two nodes can share a short name and nothing on a public mesh stops them.
    """
    box = self.query_one("#compose", Input)
    available = self.compose_available()

    if not available:
      box.remove_class("available")
      # Anything typed for one channel must not follow the reader into another.
      box.value = ""
      self.clear_reply()
      self.set_compose_status(None)
      return

    box.add_class("available")

    if self.current_peer is not None:
      destination = f"to {self.current_channel_label}"
    else:
      destination = f"to {self.current_channel_label} (ch {self.current_channel_index})"

    marker = format_reply_marker(self.reply_to_message)
    box.border_title = f"{destination} · {marker}" if marker else destination

    self.update_byte_counter(box)




  def update_byte_counter(self, box: Optional[Input] = None) -> None:
    """Count what the limit actually counts, and count it as it is typed.

    233 bytes of UTF-8, not 233 characters, so a message of emoji runs out roughly
    four times sooner than English. A live count is more honest than a rejection
    after the fact — `mesh_link.protocol.validate_text` would refuse it anyway,
    but by then it has been written.
    """
    box = box if box is not None else self.query_one("#compose", Input)
    if send is None:
      return

    used = send.text_byte_length(box.value)
    box.border_subtitle = f"{used}/{send.MAX_TEXT_BYTES}"

    # **There was a `· reaction` hint here, and schema 0.10.0 took it away.**
    #
    # An emoji-only reply used to come back from the archive as a tapback,
    # because `is_tapback()` was "replies to something, and is emoji-only" and
    # the archive recorded no intent — so the console appeared to send reactions
    # for free, and the hint existed so that nobody discovered it by accident
    # from a message that turned into a pill.
    #
    # The archive records intent now, and `_archive_outbound` writes emoji=0,
    # truthfully: mesh-link's `SendTextRequest` has no emoji field, so what left
    # this node was a reply and every other client on the mesh rendered it as
    # one. The pill was this reader agreeing with itself about a message nobody
    # else saw that way. Transmit-side tapbacks wait on a protocol revision; the
    # hint would now promise one this console cannot send, so it goes with it.
    if used > send.MAX_TEXT_BYTES:
      box.add_class("over-limit")
    else:
      box.remove_class("over-limit")




  def action_compose(self) -> None:
    """Put the cursor in the compose box."""
    if not self.compose_available():
      return
    self.query_one("#compose", Input).focus()




  def action_reply(self) -> None:
    """Make the next message answer the highlighted one, and start typing it.

    `reply_to` has been carried by `send_to_channel` and chained correctly by the
    collector since Phase 4, with a check in the mesh-link suite; nothing in the
    interface set it. This is the whole of what was missing — a key, and somewhere
    to hold which message the next send answers.
    """
    if not self.reply_available() or self.send_in_flight:
      return

    row = self.message_row_at_cursor()
    if row is None:
      return

    self.reply_to_message = row["message"]
    self.update_compose()
    # `escape` gains a meaning the moment a reply is pending, so the footer has to
    # be asked again or it would keep advertising the old answer.
    self.refresh_bindings()
    self.query_one("#compose", Input).focus()




  def clear_reply(self) -> None:
    """Stop answering anything. Cheap enough to call unconditionally."""
    self.reply_to_message = None




  async def action_back(self) -> None:
    """Leave whatever the reader is currently inside, one step at a time.

    **This is `escape`, and it had to become focus-aware.** It used to be
    `leave_compose`, which moved focus to `#messages` whenever a channel was
    open. There are now three other things `escape` can plausibly mean, and two
    of them are wrong to answer that way: pressing it in the node filter would
    have thrown the keyboard into the message list from the far side of the
    screen, and pressing it on the dashboard would have done nothing.

    So the focused widget is asked first, and only then the view. In every
    branch the message cursor is left exactly where it is — `focus()` does not
    move a ListView's index, and it must not, because the cursor is this
    interface's read marker and leaving a text box is not reading.
    """
    focused = self.focused
    focused_id = focused.id if focused is not None else None

    if focused_id == "node-filter":
      # Back to the list it filters, not across to the messages. Clearing the
      # filter here was considered and left out: escape means "stop typing in this
      # box", and a reader who wants the whole list back can empty it.
      #
      # Asked before the reply below, because the filter is a different pane of the
      # screen — escape in it must not reach across and cancel something in the
      # message pane.
      self.query_one("#nodes", ListView).focus()
      return

    if self.view == VIEW_MESSAGE:
      # **Asked before the reply branch, because on this page there is no such thing
      # as stopping answering.** A message's detail arms its own reply and the reply
      # is the point of the page; cancelling it would leave a plain compose box on a
      # page whose entire subject is the thing it had been answering, and cost a
      # second escape to get out of. So the reply leaves with the view rather than
      # ahead of it.
      #
      # **Whether there is a step out of the box first now depends on whether anything
      # has been typed, and that is because the box is focused on arrival.** It used to
      # be two steps unconditionally — out of the box, then out of the view, matching
      # the message list — and the argument for that was that entering the box had been
      # a deliberate act (`c` or `R`) which escape should undo one layer at a time.
      # `enter_detail` puts the keyboard in the box for this view now, so on an empty
      # box the first escape would undo something the reader never chose and leaving a
      # message would cost two presses where it cost one. A test caught exactly that.
      #
      # Typed text still earns the step. `escape` means "stop typing in this box" and
      # never "discard that" anywhere else in this interface, and a draft is the one
      # thing here worth protecting from a single keypress — so a box with something in
      # it keeps the two steps, and the reader can see why: the text is still on screen.
      if focused_id == "compose" and self.query_one("#compose", Input).value:
        self.query_one("#detail-pane", VerticalScroll).focus()
        return

      await self.leave_detail()
      return

    if self.reply_to_message is not None:
      # The innermost thing to be inside. A reader in the compose box with a reply
      # pending presses escape twice to leave: once to stop answering, once to
      # leave the box. The box says which state it is in, both times. This is the
      # `R` case — a reply armed on top of an ordinary box, which is a state worth
      # being able to step out of without leaving the channel.
      self.clear_reply()
      self.update_compose()
      self.refresh_bindings()
      return

    if focused_id == "compose":
      if self.viewing_messages:
        self.query_one("#messages", ListView).focus()
      return

    if self.viewing_detail:
      await self.leave_detail()
      return

    if self.viewing_conversation:
      # Up one step rather than all the way out: a conversation is inside the index
      # now, and the trail says so. A channel has nothing above it but the dashboard.
      await self.open_direct_index()
      return

    if self.viewing_messages or self.view == VIEW_DIRECT:
      self.show_dashboard()




  def on_input_changed(self, event: Input.Changed) -> None:
    if event.input.id == "node-filter":
      # The mark first and unwaited, the query after and debounced. See
      # `update_node_filter_clear` for why the two are not on the same clock.
      self.update_node_filter_clear(event.value)
      self.on_node_filter_changed(event.value)
      return

    if event.input.id != "compose":
      return

    self.update_byte_counter(event.input)

    # Whatever the last send said no longer describes what is in the box.
    self.set_compose_status(None)




  async def on_input_submitted(self, event: Input.Submitted) -> None:
    """Enter sends. The box has named its destination the whole time it was open."""
    if event.input.id == "node-filter":
      # Don't make someone who has finished typing wait out the debounce.
      if self.node_filter_timer is not None:
        self.node_filter_timer.stop()
      self.apply_node_filter(event.value.strip())
      self.query_one("#nodes", ListView).focus()
      return

    if event.input.id != "compose":
      return

    if not self.compose_available() or self.sender is None:
      return

    text = event.input.value.strip()
    if not text:
      return

    if self.send_in_flight:
      self.set_compose_status("Still waiting on the last message.", failed=False)
      return

    over = send.text_byte_length(text) - send.MAX_TEXT_BYTES
    if over > 0:
      self.set_compose_status(
        f"{over} byte{'s' if over != 1 else ''} too long — one message has to fit "
        f"{send.MAX_TEXT_BYTES} bytes of UTF-8, and emoji cost four each.",
        failed=True,
      )
      return

    self.send_in_flight = True

    # Shut the box while the send is out. Not cosmetic: the text has to stay
    # exactly where it is so a failure can hand it back untouched, and clearing it
    # on success is then guaranteed to clear the message that was sent rather than
    # whatever was typed in the seconds since.
    event.input.disabled = True
    self.set_compose_status(f"Sending to {self.current_channel_label}…", failed=False)

    reply_to = (
      self.reply_to_message["message_id"] if self.reply_to_message is not None else None
    )

    # The destination is taken from the view, not from the cursor, and this is the
    # line where that matters: whatever the reader has been arrowing through, a
    # conversation's peer is the peer the box has been naming the whole time.
    self.send_message(
      text,
      peer=self.current_peer,
      channel_index=(
        self.primary_channel if self.current_peer is not None
        else self.current_channel_index
      ),
      reply_to=reply_to,
    )




  @work(thread=True, group="send")
  def send_message(
    self,
    text: str,
    *,
    peer: Optional[str],
    channel_index: int,
    reply_to: Optional[int] = None,
  ) -> None:
    """Hand one message to the collector, off the event loop.

    **This is the one place this project genuinely needs a thread.** The call
    blocks until the collector's drain has transmitted and answered — up to 35
    seconds, because it queues behind any other send and then waits on the radio.
    Doing that on Textual's event loop would freeze the interface mid-send. The
    read path runs on the event loop because every query there is a fast indexed
    read of a local file; that argument does not survive a round trip that waits
    on a radio.

    One worker for both destinations rather than two. What differs between them is
    a single argument to `mesh_console.send`; everything after it — the failure
    translation, the `BaseException` guard, clearing `send_in_flight` — is identical,
    and a second copy would have been a second place for the outcome handling to
    drift.
    """
    sender = self.sender
    if sender is None:
      return

    # A reply whose text is nothing but emoji is a reaction, and goes out with the
    # flag that says so. Jason's call, and the trade it accepts is that a deliberate
    # one-emoji *reply* can no longer be sent — a one-emoji message still can, which
    # is the case that matters, because without a reply_to there is nothing for a
    # reaction to attach to and the flag is never set.
    #
    # `is_emoji_only` rather than a rule written here, because it is the same
    # predicate `is_tapback()` fell back on for every row written before the flag
    # existed: one to three clusters, by the scanner in `tapbacks.py`. Sharing it
    # means the reactions this console sends are exactly the ones it would have
    # recognised, and the two cannot drift apart on what counts as emoji.
    emoji = reply_to is not None and is_emoji_only(text)

    try:
      if peer is not None:
        result = sender.send_to_peer(
          text, peer, channel_index=channel_index, reply_to=reply_to, emoji=emoji
        )
      else:
        result = sender.send_to_channel(
          text, channel_index, reply_to=reply_to, emoji=emoji
        )
    except send.SendFailed as failure:
      self.call_from_thread(self.on_send_failed, failure.advice, failure.detail)
      return
    except BaseException as e:
      # The drain on the far side catches BaseException for the same reason: a
      # library that can end a process is not one to leave a gap for. Here it also
      # means send_in_flight is always cleared.
      self.call_from_thread(
        self.on_send_failed, "The send did not complete.", str(e) or type(e).__name__
      )
      return

    self.call_from_thread(self.on_send_succeeded, result)




  def on_send_failed(self, advice: str, detail: str) -> None:
    """Say what went wrong, and keep the message.

    The text stays in the box on purpose: the remedy for most of these is to try
    again, and a console that cleared the box would have thrown away the message
    to report that it had not been sent.
    """
    self.send_in_flight = False
    self.reopen_compose()
    self.set_compose_status(f"Not sent. {advice} ({detail})", failed=True)




  def reopen_compose(self) -> None:
    """Take the box out of its sending state, and put the cursor back in it.

    Focus has to be restored explicitly: Textual blurs a widget when it is
    disabled, so without this a completed send would leave the keyboard nowhere in
    particular.
    """
    box = self.query_one("#compose", Input)
    box.disabled = False

    if self.compose_available():
      box.focus()




  def on_send_succeeded(self, result: dict[str, Any]) -> None:
    self.send_in_flight = False

    box = self.query_one("#compose", Input)
    box.value = ""
    # The message that was being answered has been answered. A reply that stayed
    # pending would silently attach itself to the next thing typed.
    self.clear_reply()
    self.reopen_compose()
    self.update_compose()

    if result.get("archived"):
      # The collector wrote the row before answering, so it is already there to
      # be read. Nothing special is inserted into the list: the archive is the
      # single source of what was said, and this reloads the live end from it —
      # which is also what a message arriving from anyone else does.
      self.set_compose_status(None)
      self.call_later(self.show_sent_message)
      return

    # It went out and was not recorded, so it will never appear in the list. A
    # collector with STORE_DIRECT_MESSAGES off does this to direct messages, and
    # an archive write that fails does it to anything. Saying nothing would look
    # exactly like a failed send.
    self.set_compose_status(
      "Sent, but the collector did not archive it, so it will not appear above.",
      failed=True,
    )




  def set_compose_status(self, text: Optional[str], failed: bool = False) -> None:
    status = self.query_one("#compose-status", Static)

    if not text:
      status.remove_class("visible")
      status.remove_class("failed")
      return

    status.update(text)
    status.add_class("visible")
    if failed:
      status.add_class("failed")
    else:
      status.remove_class("failed")




  # --------------------------------------------------------------- read tracking


  def messages_fully_read(self) -> bool:
    """Whether the loaded view has nothing unread left in it.

    **Asked of the stored read cursor and not of `resume_index`**, and that is the
    whole point of it. The resume row is where reading got to, and on a fully-read
    channel it is the last row — but only if the last thing that moved the marker
    was the bottom-of-a-loaded-channel branch of `mark_read_from_viewport`. A reader
    whose final scroll stopped with the newest message inside the read margin left
    the marker a row or two short while that same branch, reached later, cleared the
    sidebar count. Trusting the row would then open a channel the badge calls read
    at a resume point three messages up, which is the report this answers.

    `has_more_newer` first, because a cursor at the end of the *loaded* window says
    nothing about a channel with pages below it — those messages are unread and not
    yet fetched. A cursor of None is a channel never read at all.
    """
    if self.has_more_newer or not self.messages:
      return False

    cursor = self.positions.cursor(*self.current_scope())
    if cursor is None:
      return False

    # The newest of everything loaded, reactions included — the same value the
    # bottom-of-the-channel branch stores, so the two agree about what "the end"
    # is rather than each deciding for itself.
    return db.cursor_of(max(self.messages, key=db.cursor_of)) <= cursor




  def read_margin(self, height: int) -> int:
    """How far up from the bottom of the pane the read line sits, in lines."""
    return max(READ_MARGIN_MIN_LINES, int(height * READ_MARGIN_FRACTION))




  def mark_read_from_viewport(self) -> None:
    """Record that everything the viewport has carried above the read line is read.

    **This is RxOnly's rule, and it replaced the cursor's.** A message used to be
    read when the cursor had been on it or past it, the cursor being the one thing
    a terminal has that a scroll position is on the web. Jason's call to change it,
    and the reason is that the cursor was doing two jobs — saying where you are and
    saying what you have seen — and a reader who scrolls rather than walks was doing
    neither. Read is now what it is in the browser: scrolled far enough up the pane.

    Far enough is `read_margin` lines from the bottom, so the last message or two of
    a channel that is still scrolling are not claimed while they sit at the edge of
    vision. A row counts when its **top** clears the line — a message is several
    lines tall, and waiting for its last line would leave a long message unread
    while the reader was already past it.

    The two carve-overs from the cursor version both survive, because neither was
    about the cursor:

    A row's reactions are newer than the row's own message, and often newer than
    several rows below it — a `👍` on this morning's message can arrive after this
    afternoon's. So the marker for a row in the middle of the list is the row's own
    message and nothing more. Sweeping in its reactions would claim every message
    between the two as read.

    That alone would mean an absorbed or held tapback could never be marked read,
    and a channel whose newest message is a reaction would sit permanently one short
    of read, which the sidebar displays as a count that will not clear. So the
    bottom of a fully loaded channel is the exception: nothing below and nothing
    left to fetch means everything in the window has been seen, reactions included.
    That exception is also what stops the read margin from being a floor the badge
    can never get under — the margin holds back the last two messages *while there
    is still somewhere to scroll*, and stops holding them back when there is not.
    """
    if not self.viewing_messages or not self.rows or self.positioning:
      return

    messages_view = self.query_one("#messages", ListView)
    height = messages_view.size.height
    if not height:
      return

    # **Only a settled list can be read.** Between a window changing and the redraw
    # finishing, the rendered rows disagree with `self.rows` in count; between the
    # redraw and the next layout pass, every row reports its `virtual_region` at
    # y=0. Measured in either state, the arithmetic below claims the whole window:
    # rows all at zero are all "above the read line", and a `max_scroll_y` of
    # nothing makes anywhere "the bottom of a fully loaded channel". `positioning`
    # guards exactly one of the windows where that happened — opening a channel —
    # but a page arriving rebuilds the same list with no flag up, and a scroll
    # event landing in that gap marked messages read that nobody had seen. The
    # guard belongs here, where the measuring is, so no caller has to know.
    rows_on_screen = self.message_rows()
    if len(rows_on_screen) != len(self.rows):
      return
    if len(rows_on_screen) > 1 and all(
      row.virtual_region.y == 0 for row in rows_on_screen
    ):
      return

    # **The last message being on screen is the end, not the scrollbar being at its
    # stop.** Those were the same thing until the pane could be scrolled past its
    # content: `scroll_y >= max_scroll_y` now means "into the blank lines below the
    # channel", and reading it that way would have made the four lines compulsory —
    # a reader who scrolled until the newest message was fully visible and stopped
    # there, which is every reader, would have been left holding the unread count
    # this branch exists to clear. So the question is asked about the message.
    #
    # Its *bottom*, unlike the read line's rule for rows in the middle of the list,
    # because there is nothing below it to go on to: a long final message whose top
    # has cleared the read line but whose last lines have not been on screen has not
    # been read, and there is no next scroll coming to finish it.
    at_the_end = not self.has_more_newer and (
      rows_on_screen[-1].virtual_region.bottom
      <= messages_view.scroll_offset.y + height
    )

    if at_the_end and self.messages:
      marker = max(self.messages, key=db.cursor_of)
      self.set_read_row(len(self.rows) - 1)
    else:
      # Measured in the scrollable content's own coordinates, which is what
      # `virtual_region` is and what makes this independent of where the list
      # happens to be on screen.
      line = messages_view.scroll_offset.y + height - self.read_margin(height)

      read = [
        index for index, item in enumerate(rows_on_screen)
        if item.virtual_region.y <= line
      ]
      if not read:
        return

      marker = self.rows[read[-1]]["message"]

    scope, key = self.current_scope()
    moved = self.positions.set(
      scope,
      key,
      marker["message_id"],
      marker["rx_time"],
      marker["id"],
    )
    if moved:
      self.positions_dirty = True
      self.clear_unread_through(marker)

    # **Where the reader has read to is the stored position, not what is on screen
    # now**, and the difference shows the moment they scroll back up: the viewport
    # says the read line is over row six again, and `ReadPositions` — which refuses
    # to move backwards — still says row thirteen, correctly, because scrolling back
    # over something does not unread it. Taking the row from the position rather than
    # from `read` is also what survives a page arriving, since prepending shifts
    # every index and a message id is the one name that does not move.
    position = self.positions.get(scope, key)
    if position:
      self.set_read_row(self.row_of_message.get(position["message_id"], self.read_row))




  def set_read_row(self, index: int) -> None:
    """Record how far this reader has read, and tell the list where to start.

    Two places rather than one because they answer different questions and only
    one of them is the app's: `read_row` is what has been read, `MessageList.start_row`
    is where the first arrow key lands. They are the same number, and this is the
    only thing that sets either, so they cannot drift.
    """
    self.read_row = index
    self.query_one("#messages", MessageList).start_row = index




  def viewport_anchor(self) -> Optional[tuple[int, int]]:
    """Which message is at the top of the pane, and how much of it is above it.

    **What holds a reader's place across a page arriving**, now that the cursor may
    not exist to hold it. `restore_cursor` named a message and let the list scroll to
    it; this names the message the reader is actually looking at, and the number of
    its lines already scrolled past, so putting it back is exact rather than
    approximate.

    None when there is nothing on screen to anchor to.
    """
    messages_view = self.query_one("#messages", ListView)
    top = messages_view.scroll_offset.y

    for item in self.message_rows():
      region = item.virtual_region
      if region.y + region.height > top:
        return (item.message["message_id"], top - region.y)

    return None




  def restore_viewport(self, anchor: Optional[tuple[int, int]]) -> None:
    """Put the pane back where `viewport_anchor` found it.

    Deferred for the same reason `position_message_pane` is: the rows this has to
    measure have just been rebuilt and are not laid out yet.

    **The failure mode this is guarding against is a loop, not a jump.** Scrolling
    near the top is what fetches the previous page; if the fetch left the pane still
    near the top, it would fetch again, and walk the whole channel backwards in one
    gesture. Landing the anchor where it was leaves the newly prepended page above
    the viewport, which is exactly the distance that stops the trigger firing.
    """
    if anchor is None:
      return

    message_id, offset = anchor

    # If a channel open claims the pane before this fires, the open's own scroll is
    # the one that matters — a same-channel reopen would otherwise find this anchor
    # message in the fresh rows and drag the pane off the resume point it was just
    # put on. A different channel never matches the message and never scrolls, but
    # that is luck of the ids, not a guard.
    epoch = self.positioning_epoch

    def scroll() -> None:
      if epoch != self.positioning_epoch:
        return
      for item in self.message_rows():
        if item.message["message_id"] == message_id:
          self.query_one("#messages", ListView).scroll_to(
            y=item.virtual_region.y + offset, animate=False, immediate=True
          )
          return

    # Before the next paint, not merely after the next refresh: forcing the reflow
    # gives the spliced rows positions now, so the pane can be put back inside the
    # loader's paint-suppressing batch and no frame is ever drawn with the anchor
    # adrift. `_refresh_layout` is Textual's own internal rather than an interface
    # promise, hence the guard — without it the deferred pass below still lands,
    # one painted frame later, which is what this always was.
    refresh_layout = getattr(self.screen, "_refresh_layout", None)
    if refresh_layout is not None:
      refresh_layout()
      scroll()

    # And once more after the refresh, so this stays the last word on where the
    # pane sits: restoring the cursor may have queued a scroll of its own, and a
    # selection landing where it likes is the thing this exists to prevent.
    self.call_after_refresh(scroll)




  def begin_positioning(self) -> int:
    """Raise the positioning gate and stake a new claim to it.

    Returns the epoch the caller now owns. Every earlier claim is dead from this
    moment: a deferred scroll still queued from a previous open compares its epoch
    against `positioning_epoch` and returns. That comparison is what a bare bool
    could not give — two overlapping opens both held "True", and whichever chain
    settled last lowered the gate and scrolled the pane, on a channel that was no
    longer its business.
    """
    self.positioning = True
    self.positioning_epoch += 1
    return self.positioning_epoch




  def end_positioning(self, epoch: int) -> None:
    """Lower the positioning gate, if it is still this caller's to lower."""
    if epoch == self.positioning_epoch:
      self.positioning = False




  def position_message_pane(self, index: int, *, to_end: bool = False) -> None:
    """Put the pane where the channel being opened should open.

    Two landings, and which one is right is a question about the channel rather
    than about this pane:

    `to_end` is **the default position of a fully-read channel** — the last message
    against the bottom with `MessageList.OVERSCROLL_LINES` of blank space beneath
    it. A channel with nothing unread in it has no resume point worth honouring:
    every message is behind you, so the end is where you left off, and the blank
    lines are what make the end a place the pane can rest. Before those lines
    existed this landing did not exist either — the read line is a fifth of the
    pane up from the bottom, so putting the last message *there* pushed nothing
    below it except the fold, and the reader came back to a channel whose final
    message or two were off screen despite being read.

    Otherwise one row's top goes on the read line, so everything above it is
    exactly what this reader had read and the unread messages sit below. That is
    the resume landing, and it still belongs to any channel that has something
    left to read in it.

    Deferred with `call_after_refresh` because a row's `virtual_region` is a fact
    about a laid-out list, and this is called immediately after `render_rows` has
    cleared and refilled one. Asking before the layout pass gives every row a y of
    zero and scrolls nowhere.

    Clamped by the list itself: a channel shorter than the pane has nothing to
    scroll, and lands at the top with everything visible — which
    `mark_read_from_viewport` then reads as the bottom of a fully loaded channel and
    marks read, correctly, because it all fits on screen and has all been seen.
    """
    # The flag is already up when this is reached from `open_channel`; claiming a
    # fresh epoch on top of that is deliberate, so that this chain — not the one
    # from any earlier open still in the refresh queue — is the one that owns it.
    epoch = self.begin_positioning()

    def scroll(attempts: int = 0) -> None:
      if epoch != self.positioning_epoch:
        # A newer open owns the pane. Its chain will position it and lower the
        # gate; touching either from here would be scrolling somebody else's
        # channel and reading from wherever that left the viewport.
        return

      rows = self.message_rows()
      messages_view = self.query_one("#messages", ListView)
      height = messages_view.size.height

      # **One refresh is not always enough for the rows to have positions**, and
      # measuring them before they do is what made this intermittent. Every row
      # reports a `virtual_region` at y=0, so the pane scrolls to the top instead of
      # to the resume point, and the read marker is then derived from a viewport
      # showing the wrong part of the channel — advancing past messages nobody had
      # seen, on about two runs in five. A list of more than one row where every row
      # starts at zero has not been laid out yet.
      #
      # `positioning` deliberately stays up across the retries: it is the thing
      # keeping these intermediate positions from being read as progress. Bounded,
      # because a retry that never gave up would leave reading switched off.
      unlaid = len(rows) > 1 and all(row.virtual_region.y == 0 for row in rows)
      if (unlaid or not height) and attempts < 5:
        self.call_after_refresh(scroll, attempts + 1)
        return

      # From here reading goes back on whatever happens: a channel that cannot be
      # positioned must not leave it switched off for the rest of the session. The
      # marker stays where it was, which is the safe direction.
      self.end_positioning(epoch)

      if not rows or not height:
        return

      if to_end:
        # `scroll_end` goes to `max_scroll_y`, which `MessageList` has already
        # extended past the last message — so this is the blank-lines landing
        # without this having to know how many lines that is.
        messages_view.scroll_end(animate=False, immediate=True)
        self.mark_read_from_viewport()
        return

      if index < 0 or index >= len(rows):
        return

      target = rows[index].virtual_region.y - (height - self.read_margin(height))

      # **An unread message below the fold is the bug this whole change is about,
      # and it is not only fully-read channels that had it.** The resume row goes on
      # the read line whether or not what follows it fits underneath, so a channel
      # with two unread messages and a tall read margin opened with the newer one
      # off screen — waiting behind a scroll the reader had no reason to think was
      # needed. Where the tail does fit, drop far enough that its last line is on
      # screen.
      #
      # Only when the whole of it is loaded: with `has_more_newer` there is more
      # unread below than this pane could show however it is scrolled, and reading
      # forward from the resume point is then exactly the right landing.
      #
      # The second condition is what keeps this from swallowing the resume point. A
      # long unread tail would need a scroll that carried the resume row off the top
      # of the pane, and a landing that hides where you had read to — marking the
      # rows in between read on the way past — is worse than a scroll. So the drop
      # is taken only while the resume row survives it.
      if not self.has_more_newer:
        tail = rows[-1].virtual_region.bottom - height
        if tail > target and rows[index].virtual_region.y >= tail:
          target = tail

      messages_view.scroll_to(y=target, animate=False, immediate=True)
      self.mark_read_from_viewport()

    self.call_after_refresh(scroll)




  def message_rows(self) -> list[MessageItem]:
    """The rendered message rows, in the order they are drawn.

    `ListView.children` rather than a query, because the order is the point and a
    query does not promise one. The placeholder an empty channel gets is a plain
    `ListItem` and drops out here.
    """
    return [
      item for item in self.query_one("#messages", ListView).children
      if isinstance(item, MessageItem)
    ]




  def clear_unread_through(self, marker: dict[str, Any]) -> None:
    """Take the unread accent off every row the marker has now passed.

    The rows are already on screen, so this updates them in place rather than
    rebuilding the list — the same reason `ChannelItem.set_counts` exists. A rebuild
    here would drop the scroll position that just did the reading.

    Compared against the marker rather than against a row position, because the
    bottom of a fully loaded channel moves the marker to the newest message in the
    window whichever row is drawing it — see `mark_read_from_viewport`. Going by
    position would leave an absorbed tapback's row still accented after the marker
    had passed the message it holds.
    """
    threshold = (marker["rx_time"], marker["id"])

    for item in self.query(MessageItem).results(MessageItem):
      if item.has_class("unread") and (
        item.message["rx_time"], item.message["id"]
      ) <= threshold:
        item.set_unread(False)




  def message_row_at_cursor(self) -> Optional[dict[str, Any]]:
    """The derived row the cursor is on, or None when there is no row under it.

    A row rather than a message, because a reply needs the message and the byte
    counter's reaction hint needs nothing else — and because a row is what the
    ListView's index actually indexes. `message_at_cursor` is the id alone, which is
    what the pager wants.
    """
    index = self.query_one("#messages", ListView).index
    if index is None or index < 0 or index >= len(self.rows):
      return None
    return self.rows[index]




  def message_at_cursor(self) -> Optional[int]:
    """The message_id of the row the cursor is on, or None if there is no row."""
    row = self.message_row_at_cursor()
    if row is None:
      return None
    return row["message"]["message_id"]




  def flush_positions(self) -> None:
    if self.positions_dirty:
      self.positions.save()
      self.positions_dirty = False




  # ---------------------------------------------------------------------- poll


  def poll(self) -> None:
    """Look for new messages, and for the archive coming back after a failure."""
    # Measured before anything can fail, so a gap is the gap between two attempts
    # rather than between two successes — an archive that was unreadable for five
    # minutes is not a stale session, it is a reported outage, and it has its own
    # banner.
    now = monotonic()
    gap = None if self.last_poll is None else now - self.last_poll
    self.last_poll = now

    if self.conn is None and not self.reconnect():
      self.record_poll_failure()
      return

    # **Everything below this line draws on the main screen, and with something
    # pushed over it there is nothing there to see.** Measured against the fixture
    # archive: a poll on the dashboard view issues eleven statements — four counts,
    # a `LEFT JOIN … GROUP BY` across the whole messages table, the local node, both
    # unread tallies and the node page — and then rebuilds the device bar, the
    # dashboard, every channel row's counts and every node row. With the log viewer
    # on top it issued the same eleven and redrew the same widgets, none of which is
    # reachable: `#log-frame` has its own background and covers everything but the
    # two-row ring of padding around it. On the Pi that was a ~32% CPU spike for half
    # a second, every ten seconds, for nothing anybody could look at.
    #
    # The reconnect above stays on this side of the guard, because an archive that
    # has gone away is worth noticing whatever is on screen, and because it is the
    # one thing here that costs nothing when there is nothing wrong.
    if len(self.screen_stack) > 1:
      self.poll_hidden(now)
      return

    stats = self.read(
      db.fetch_stats, self.show_direct_messages,
      list_unnamed=self.list_unnamed_nodes,
    )
    if stats is None:
      self.record_poll_failure()
      return

    self.poll_failures = 0
    self.set_connection_error(False)

    self.update_device_bar(stats)
    self.check_for_state_change(stats)

    # A resync deferred while the log viewer was on top, now that it is not. Asked
    # here rather than from a screen hook because the poll is already the thing
    # that runs on its own every few seconds, and being a few seconds late to
    # rebuild a sidebar nobody has looked at yet costs nothing.
    #
    # The stack depth this used to test for is the guard's business now, and by here
    # it is always one — `screen_returned` is what runs the poll that finds this
    # flag, usually within a frame of the viewer closing rather than at the next
    # interval.
    if self.resync_owed:
      self.flush_positions()
      self.call_later(self.resync)
      return

    # The process itself stopped running: suspended, slept, or starved. The
    # terminal reported no blur because from its side nothing happened, so this is
    # the only thing that notices — and what it notices is the same staleness
    # `on_app_focus` handles, so it goes the same way out.
    if gap is not None and gap > self.poll_interval * STALE_GAP_INTERVALS:
      self.flush_positions()
      self.request_resync(not self.has_more_newer)
      return

    if self.viewing_messages:
      self.poll_messages()
    elif self.view == VIEW_DIRECT:
      # A conversation's counts move while the index is on screen, and a peer who
      # writes for the first time is a row that is not there yet. Rebuilt whole
      # rather than patched in place: unlike the node list this one is short, and
      # its order is by recency, which is exactly what a new message changes.
      self.rebuild_conversations()
    elif self.viewing_detail:
      self.poll_detail()
    else:
      self.query_one("#dashboard", Static).update(self.render_dashboard(stats))

    # Outside the branch, because an unread count is about the sidebar rather
    # than about whatever #main is showing — and the one view where it changes
    # fastest is the one where you are reading the messages it counts.
    self.refresh_channel_counts(stats)

    # **Two refreshes of the same rows, and which one runs is a question about the
    # reader rather than about the archive.** With nobody holding the list, the
    # sidebar asks what RxOnly asks — the newest page, in order — and keeps up with
    # it poll for poll. With a cursor in it, the older in-place refresh runs
    # instead: every row current, none of them moved. One query either way.
    if self.node_list_left_alone():
      self.refresh_node_list(stats)
    else:
      self.refresh_node_rows(stats)

    self.flush_positions()




  def poll_hidden(self, now: float) -> None:
    """The whole of what a poll owes while something is on top of the main screen.

    **Two reads in place of eleven and a full redraw** — a meta lookup and the node
    row it names — and neither is read for what it says. Nothing here consumes the
    local node it fetches; what is wanted is whether it can be fetched at all. Without
    a read of some kind an
    archive that has gone away looks exactly like a healthy one for as long as the
    viewer is up — `self.conn` is only ever cleared by a read that fails, so nothing
    would retry the connection and the banner would still be absent at the moment the
    stack came back down, which is precisely when a reader is looking for it.

    None is an ambiguous answer here and `self.conn` is what disambiguates it: an
    archive whose collector has not yet named the attached device answers None with
    the connection perfectly intact, and that is not a failure. `self.read` drops the
    connection on its way out of a failed read and nothing else in this class does,
    so the connection being gone is the failure, and the None is not.

    **A swapped device or a rebuilt database is deliberately not handled here.** The
    probe would see it — the identity is in the row it just read — and acting on it
    would mean a notification nobody is looking at and a drop back to a dashboard
    nobody can see. Leaving `known_local_node_id` untouched is what keeps it
    noticeable instead: the first visible poll after the stack comes down compares
    against the device from before the viewer was ever opened, finds the change
    sitting there waiting, and does the whole of `check_for_state_change` at the
    moment there is somebody to tell.

    Read positions are still flushed. They are not view work — a poll behind the
    viewer is the only thing that would ever persist what was marked read just before
    it opened, and the write costs nothing on a poll where nothing was marked.
    """
    if self.hidden_since is None:
      self.hidden_since = now

    self.read(db.fetch_local_node)
    if self.conn is None:
      self.record_poll_failure()
      return

    # Guarded rather than said every time, because `set_connection_error` writes to
    # a widget on the screen underneath and a widget refreshed is a screen the
    # compositor has to consider again. A modal screen in Textual sits on a 60%
    # background, so what is underneath is genuinely still being composited — the
    # cheapest thing to hand it is no change at all.
    if self.poll_failures:
      self.poll_failures = 0
      self.set_connection_error(False)

    self.flush_positions()




  def screen_returned(self, _result: Any = None) -> None:
    """The main screen is on top again. Undo exactly as much as the guard skipped.

    Wired as the dismiss callback of both screens that can cover the main one, so
    the catch-up happens within a frame of the viewer closing rather than up to a
    poll interval later. That distinction is new and it is the reason this exists:
    the deferral this pairs with used to leave only the sidebar's *ordering* stale,
    which nobody notices, and now leaves every number on screen as old as the
    viewer, which they would.

    Nothing to do unless a poll was actually skipped. A menu opened and closed
    between two polls froze nothing at all, and `hidden_since` is still None.

    **How long it was covered decides which of two different catch-ups runs, and
    conflating them was the trap.** A minute in the log viewer leaves a screen stale
    the way a late poll is stale: every value on it is a few seconds old, and one
    poll fixes all of it without moving anything. An hour leaves the *shape* of the
    screen wrong as well — the node list is in the order things were heard in an hour
    ago, missing everything first heard since — and the only answer to that is the
    rebuild `resync` does. But a rebuild deselects whatever row was selected, so
    reaching for it on every return would mean a two-second glance at ctrl+p came
    back having thrown away the node somebody was about to press enter on. So the
    line is `STALE_AFTER_SECONDS`, which is the same number `on_app_focus` draws for
    the same distinction, and for anything short of it this is one ordinary poll.

    Whether the message pane was following can be asked here rather than caught on
    the way in, which is where it parts company with `blurred_following`. That one
    cannot be asked late because the poll goes on appending messages while the
    terminal is behind another window; this one can, because the poll behind a modal
    is precisely what has *not* been running, so the answer is still the one from
    before the screen was covered.

    **Both catch-ups go through the scheduler, and that is not a style choice.**
    `Screen.dismiss` invokes this callback and *then* pops (screen.py:2064-2067), so
    for the length of this method the screen being closed is still on the stack —
    which means a `poll()` called directly from here would find a depth of two, take
    the guard's branch straight back out, and arm `hidden_since` afresh on a screen
    that is one instant from being gone. `request_resync` reads the same depth to
    decide whether it may run now. Verified against the installed Textual rather than
    assumed, and it is why the work is handed to the message pump instead: by the time
    the pump reaches it the pop has happened and both of them see the depth they are
    asking about.
    """
    hidden_since = self.hidden_since
    self.hidden_since = None

    if hidden_since is None:
      return

    if monotonic() - hidden_since >= STALE_AFTER_SECONDS:
      self.call_later(
        self.request_resync, self.viewing_messages and not self.has_more_newer
      )
      return

    self.call_later(self.poll)




  def poll_detail(self) -> None:
    """Keep an open node detail current, the way RxOnly's slow poll does.

    Only the node view. A node's battery, voltage and last_seen move while you
    are looking at them; an archived message does not change, so there is
    nothing to re-read for the other detail view.
    """
    if self.view != VIEW_NODE or self.detail_node_id is None:
      return

    node = self.read(db.fetch_node, self.detail_node_id)
    if node is None:
      return

    # The conversation block has to be rebuilt with it, or the first poll would
    # quietly take it away — and its unread count moves for the same reason the
    # rest of this view does: a message can arrive while you are looking at it.
    conversation = (
      self.conversation_summary(self.detail_node_id)
      if self.can_converse_with(self.detail_node_id) else None
    )

    self.query_one("#detail", DetailView).update(
      self.render_node_detail(node, conversation)
    )




  def poll_messages(self) -> None:
    """Note or absorb messages that have arrived since the last read.

    New messages are only appended when the live end is already on screen. A
    reader who has paged back gets told there is something newer instead of
    having it spliced in below them.
    """
    latest = self.read(
      db.newest_cursor,
      self.current_is_dm,
      self.current_channel_index,
      peer=self.current_peer,
    )
    if latest is None or latest == self.newest_cursor:
      return

    if self.has_more_newer:
      self.update_message_status()
      return

    self.call_later(self.absorb_newer)




  async def absorb_newer(self) -> None:
    """Take the page the poll has seen arrive, and give the claim back if it can't.

    **`has_more_newer` has to be raised before the fetch and that is what made it
    a trap.** `load_newer` refuses to run while it is False — it is the pager's
    own gate — so the poll has to claim there is something below before it can ask
    for it. But `load_newer` has three ways of standing down after that claim has
    been made: a page already in flight, a pane mid-positioning, and a read that
    failed. None of them lowered the flag again, and the poll above reads a raised
    flag as "the reader has paged back and has been told", so from that moment on
    every poll took the branch above and no message was ever appended again.

    Nothing cleared it either. The scroll trigger would have, but a reader sitting
    at the live end scrolls nothing, which is precisely the reader this is for.
    What it looked like was a channel that stopped receiving while the sidebar
    count beside it went on climbing, and the only ways out were `g` or reopening
    the channel.

    So the claim is made here, where it can be taken back, and it is taken back by
    asking whether the window actually moved rather than by trying to enumerate
    which of `load_newer`'s exits was used. A window that did not move is a claim
    about nothing, and the next poll is free to ask again.
    """
    before = self.newest_cursor
    self.has_more_newer = True

    try:
      await self.load_newer()
    finally:
      # `load_newer` lowers the flag itself when the page comes back empty, and
      # sets it from the page's own meta when it does not; neither is undone here.
      # This is only the case where nothing happened at all.
      if self.newest_cursor == before and self.has_more_newer:
        self.has_more_newer = False
        self.update_message_status()




  # ------------------------------------------------------------- stale sessions
  #
  # **Nothing here pauses the poll, and the symptom this answers looks exactly as
  # though something had.** Leave the console in a window behind another one for
  # twenty minutes, come back, and the nodes and the messages have stopped moving
  # while the collector is demonstrably still writing. Nothing stopped: the
  # `set_interval` in `on_mount` is never touched again, and Textual's own blur
  # handler only flips `app_focus`. The poll ran every ten seconds of those twenty
  # minutes.
  #
  # What goes stale is the work the poll deliberately declines to do while a reader
  # might have a cursor in it. `refresh_node_rows` will not reorder the node list,
  # will not insert a node first heard since the last load, and will not remove a
  # pruned one — all three for good reasons, and all three add up to a sidebar that
  # is as old as your absence. `poll_messages` will not splice a page in above a
  # reader who has paged back. Every one of those is right while somebody is
  # reading and wrong the moment they have been gone for a minute.
  #
  # So the answer is not to stop and start the poll. It is to do, from time to time,
  # the three things the poll spends every ten seconds refusing to do — which is
  # what `r` has always been for. This presses it for you.
  #
  # **What decides when is whether anybody is holding the list, and nothing else.**
  # The first version of this asked the terminal instead — resync when focus comes
  # back after a minute away — and it was wrong in a way that took a second report
  # from the Pi to see: a console left visible beside a browser is never blurred, so
  # it never came back, so it stayed as stale as it had always been. An interval was
  # the second answer and was still the wrong shape, because a node first heard a
  # minute ago was simply missing until the interval came round. The question was
  # never how long it had been; it was whether redrawing would move something under
  # a hand. `poll()` asks that directly now — see `node_list_left_alone` — and where
  # the answer is no, the sidebar keeps up with RxOnly poll for poll.
  #
  # The focus and gap paths below survive as the fast route for a reader who has
  # demonstrably just come back: they catch the message pane up as well, which the
  # node refresh does not do and should not. Worth keeping, not worth depending on,
  # because nothing guarantees a terminal reports focus at all.


  def disown_node_list_touch(self) -> None:
    """Forget a touch the interface caused rather than the reader. See `rebuild_nodes`."""
    self.node_list_touched = None




  def node_list_left_alone(self) -> bool:
    """Whether the node list can be redrawn without moving anything under a hand.

    Two ways to be sure of that, and the first covers almost every case. **If the
    keyboard is somewhere else there is no cursor in this list to protect** — the
    reader is in the message pane, or the compose box, or looking at a browser on
    another monitor — and the list may be redrawn as freely as RxOnly redraws its
    own. A wheel can still scroll a list that is not focused, and that stamps the
    same clock, but a scroll position is what `rebuild_nodes` restores anyway.

    **Focus alone would not do**, which is why the second way exists. Focus outlives
    use: leaving a node's detail hands the keyboard back to this list, and
    `show_dashboard` leaves it wherever it was, so a reader who opened a node an
    hour ago and has read messages since still has focus sitting here. Gated on
    focus alone their sidebar would never be redrawn again — the same bug this is
    fixing, one level down. So a focused list also qualifies once it has sat still
    for `NODE_LIST_IDLE_SECONDS`.

    Never touched reads as left alone. A list nobody has put a cursor in is the
    safest one to redraw, not the least safe.
    """
    focused = self.focused
    if focused is None or focused.id != "nodes":
      return True

    if self.node_list_touched is None:
      return True

    return monotonic() - self.node_list_touched >= NODE_LIST_IDLE_SECONDS




  def refresh_node_list(self, stats: dict[str, Any]) -> None:
    """Re-read the page the sidebar is showing and redraw it if it has moved.

    **This is `update_nodes_list` (nodes.js:159), and it is what the console
    refused to do until it could tell whether anyone was holding the list.** It
    asks the question `fetch_nodes` answers — which nodes are the most recently
    heard, in what order — rather than the one `fetch_nodes_by_id` answers, which
    is what the rows already on screen say today. That is the whole difference
    between a sidebar that gains a node the moment it is first heard and one that
    cannot gain a node at all.

    **Rebuilt only when the answer actually changed.** Most polls it has not: the
    same nodes come back in the same order, and then this is exactly the in-place
    refresh it replaces — every row handed its fresh values, no widget torn down,
    no cursor moved. A rebuild costs fifty rows of teardown and construction, and
    on a Pi that is not free (see the To-Do in README.md), so it is spent only when
    the order or the membership is genuinely different from what is drawn.

    The counts follow the same split `refresh_node_rows` uses. `node_total` is the
    archive's, which the poll has already read for the device bar; the page's own
    total is the number of *matches*, which is the same thing only when nothing is
    filtered.
    """
    page = self.read(
      db.fetch_nodes,
      NODE_SEARCH_LIMIT if self.node_search else self.page_size,
      0,
      self.node_search or None,
      list_unnamed=self.list_unnamed_nodes,
    )
    if page is None:
      return

    nodes = page["nodes"]
    self.node_total = stats["stats"]["total_nodes"]
    self.node_matches = page["meta"]["total"]

    items = list(self.query(NodeItem).results(NodeItem))

    if [item.node_id for item in items] == [node["node_id"] for node in nodes]:
      fresh = {node["node_id"]: node for node in nodes}
      for item in items:
        item.set_node(fresh[item.node_id])
      self.update_nodes_heading(len(nodes))
      return

    # `reload_nodes` keeps these two in step for the same reason: the offset is how
    # much of the list is loaded, and a rebuild has just decided that afresh.
    self.node_offset = len(nodes)
    self.rebuild_nodes(nodes)




  def on_app_blur(self, event: events.AppBlur) -> None:
    """The terminal has gone behind something else.

    Textual's driver asks for focus reporting (`\\x1b[?1004h`) on the way in, so
    this arrives in Terminal.app, iTerm2, kitty and WezTerm. It does not arrive
    under a tmux without `focus-events on`, and it says nothing about a suspended
    or slept process — the gap check in `poll()` is what covers both.

    Defined as `on_app_blur` alongside `App._on_app_blur`, which is a different
    method on a different class in the MRO: Textual dispatches every match it
    finds, so the framework's own handler still runs and `app_focus` is still
    maintained. Verified rather than assumed.
    """
    self.blurred_at = monotonic()
    self.blurred_following = self.viewing_messages and not self.has_more_newer




  def on_app_focus(self, event: events.AppFocus) -> None:
    """The terminal is in front again. Catch up if it was gone long enough."""
    away = self.blurred_at
    following = self.blurred_following
    self.blurred_at = None

    # No recorded blur means the focus event is not the far end of an absence —
    # the first one at startup, or a terminal that reports focus without having
    # reported the matching loss of it.
    if away is None or monotonic() - away < STALE_AFTER_SECONDS:
      return

    self.request_resync(following)




  def request_resync(self, following: bool) -> None:
    """Ask for a catch-up, now or as soon as the main screen is on top again.

    `following` is whether the message pane was at the live end when the session
    went stale, and it decides whether the catch-up moves the reader. It is passed
    in rather than worked out here because by the time this runs the answer has
    usually changed — see `blurred_following`.
    """
    self.resync_owed = True
    self.resync_following = following

    # Under the log viewer this would rebuild a screen nobody can see, and the log
    # viewer is where a reader most often is while the console is going stale
    # underneath. `poll()` picks it up when the stack comes back down.
    if len(self.screen_stack) == 1:
      self.call_later(self.resync)




  async def resync(self) -> None:
    """Do once what the poll refuses to do continuously.

    The same work `r` does, for the same reason and with one addition: `r` is a
    reader asking, so it can leave them where they are, and this is a reader coming
    back, so the message pane follows the live end if that is where they left it.

    **This moves things under the cursor, and that is the trade.** The node list is
    rebuilt whole, so it reorders, gains the nodes heard while you were away, and
    loses the pruned ones — and whatever row was selected in it is not selected
    afterwards. The reason this is acceptable here and not in the poll is the whole
    of why the poll refuses: the objection to reordering is that it happens under
    somebody's hands. This is the one instant it demonstrably is not, because the
    terminal has just this moment been brought back and nothing has been typed into
    it yet.

    Focus is left where it was. A reader who was in the compose box comes back to
    the compose box, which is the same call `show_sent_message` makes and for the
    same reason.
    """
    # Both ways in can be taken for the same absence — a laptop closed on a
    # backgrounded window comes back with a blur to answer *and* a poll gap to
    # explain — and each queues a call. The flag is what one absence owes, so the
    # second call finds it settled and has nothing to do.
    if not self.resync_owed:
      return

    self.resync_owed = False
    following = self.resync_following

    if self.conn is None and not self.reconnect():
      self.record_poll_failure()
      return

    # The sidebar first, because it is the half that cannot fix itself: the counts
    # were current all along, the ordering and the membership have been frozen
    # since the last real load.
    self.refresh_sidebar()

    # Whether a collector is listening is the one piece of state that moves without
    # the archive moving, so a console that was up while its collector was
    # restarted finds out here — the same reasoning as `action_refresh`.
    self.assess_sending()

    if self.viewing_messages:
      if following:
        await self.show_newest(take_focus=False)
      else:
        # Left paged back on purpose. Nothing is spliced in; the status line is
        # re-asked so `g` is offered, which is what a reader in that position
        # already expects to have to press.
        self.update_message_status()
    elif self.view == VIEW_DIRECT:
      self.rebuild_conversations()
    elif self.viewing_detail:
      self.poll_detail()
    else:
      self.refresh_dashboard()




  def refresh_channel_counts(self, stats: dict[str, Any]) -> None:
    """Update the sidebar counts in place, without rebuilding the list.

    Rebuilding would drop whatever the reader had selected, so a poll that only
    changes a number only changes a number. Both numbers move for their own
    reasons: the total when the collector archives something, the unread count
    when either that happens or this reader moves the cursor.
    """
    channel_counts = stats["stats"]["channel_counts"]
    # Whatever `rebuild_sidebar` put there, or the poll would rewrite the row with
    # a number of a different kind ten seconds later.
    total_dms = stats["stats"]["drawn_direct_messages"]

    unread = self.unread_counts()
    unread_dms = self.unread_direct_count()

    for item in self.query(ChannelItem).results(ChannelItem):
      if item.is_dm:
        item.set_counts(total_dms, unread_dms)
      elif item.channel_index is not None:
        item.set_counts(
          channel_counts.get(item.channel_index, 0),
          unread.get(item.channel_index, 0),
        )




  def refresh_node_rows(self, stats: dict[str, Any]) -> None:
    """Bring the node rows already on screen up to date, without rebuilding them.

    The sidebar's half of what `poll_detail` does for the main pane: a node's name
    and the time it was last heard both move while you are looking at them, and
    until this existed the only thing that moved them was `r`. That was the gap —
    the node list is the largest block of timestamps on screen, and it was the one
    that went stale.

    **In place, and in the order that is already there — which is where this parts
    company with RxOnly, deliberately.** `update_nodes_list` (nodes.js:159) re-fetches
    the loaded window, reuses each `li` it already has by node id, and re-appends them
    in the fresh order, inserting any node it has not seen. It can afford that because
    a browser's place in a list is a scroll offset, which it restores afterwards with
    `get_scroll_anchor`; a reorder two hundred pixels above the viewport is invisible.

    A terminal's place in a list is the cursor, and there is no equivalent of "off
    screen above" — the list is thirty rows and the reader is arrowing through them.
    Reordering it every ten seconds would move rows out from under that cursor,
    including the row someone was about to press enter on. So the rows are asked
    about by node id (`fetch_nodes_by_id`) rather than re-paged, which is what makes
    "refresh this row" mean the same node every time. What that costs, and what it is
    worth:

    - **No reorder.** A node heard just now keeps its position until the next real
      load. Its timestamp is current, which is the part that was asked for.
    - **No insert.** A node the archive has never seen before does not appear, for the
      same reason `poll_messages` refuses to splice a page in above the reader. The
      heading count moves, which is how you can tell there is one.
    - **No removal.** A row whose node has been pruned is left saying what it last
      said rather than blanked or dropped — dropping it would shift every row below.

    `r` is what does all three, and that is now its actual job rather than being the
    only way anything in the sidebar moved at all.
    """
    items = list(self.query(NodeItem).results(NodeItem))
    if not items:
      return

    fresh = self.read(db.fetch_nodes_by_id, [item.node_id for item in items])
    if fresh is None:
      return

    for item in items:
      node = fresh.get(item.node_id)
      if node is not None:
        item.set_node(node)

    # Said even when no row changed, because this number is what reports the node
    # this refresh could not show: a node first heard since the last load is absent
    # from the rows above and present in the total, so `Nodes (50 of 82)` becoming
    # `Nodes (50 of 83)` is the only thing on screen saying a new one has arrived.
    #
    # `total_nodes` is the count the poll has just read for the device bar, so the
    # unfiltered heading costs no query of its own. A filtered one does: `Nodes (12
    # of 82)` is 12 matching out of 82 in the archive, and only the second half of
    # that is a question `fetch_stats` can answer.
    self.node_total = stats["stats"]["total_nodes"]

    if not self.node_search:
      self.update_nodes_heading(self.node_offset)
      return

    matches = self.read(
      db.fetch_nodes, 0, 0, self.node_search,
      list_unnamed=self.list_unnamed_nodes,
    )
    if matches is None:
      return

    self.node_matches = matches["meta"]["total"]
    # A filtered list is fetched whole rather than paged, so what is loaded is what
    # it matched — clamped where `fetch_nodes` stops returning more, which is the
    # case `update_nodes_heading` spells out rather than truncating quietly.
    self.update_nodes_heading(min(self.node_matches, NODE_SEARCH_LIMIT))




  def record_poll_failure(self) -> None:
    self.poll_failures += 1
    if self.poll_failures >= MAX_POLL_FAILURES:
      self.set_connection_error(True)




  def set_connection_error(self, visible: bool) -> None:
    banner = self.query_one("#connection-error", Static)
    if visible:
      banner.update(
        f"Cannot read {db.archive_path()} — retrying every "
        f"{self.poll_interval:g}s"
      )
      banner.add_class("visible")
    else:
      banner.remove_class("visible")




def main() -> None:
  """Load configuration, open the archive, and hand it to the interface."""
  Config.load()

  # Everything that can be wrong with the path or the schema surfaces here,
  # before the alternate screen is entered — an error printed over a torn-down
  # TUI is much harder to read than one printed to a normal terminal. These two
  # already carry a message written for a person, so print that and nothing else:
  # a traceback would bury it.
  try:
    connection = open_archive()
  except (ArchiveUnavailable, SchemaVersionMismatch) as e:
    raise SystemExit(f"mesh-console: {e}") from None

  MeshConsoleApp(connection).run()
