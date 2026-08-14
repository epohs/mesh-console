"""Turning archive rows into the strings the interface prints.

These mirror the formatting helpers in RxOnly's `static/js/rxonly.js`, so the
same archive reads the same way in a terminal as in a browser: a node is
"Long Name (SHORT)" in both, and a reply excerpt is truncated at the same 120
characters. Where a terminal wants something different from a web page — a
timestamp short enough to sit on a message line — that is a separate function
rather than a changed one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from rich.style import Style
from rich.text import Text


# The reply bar shows this much of the message being replied to. Matches
# format_reply_excerpt in RxOnly's messages.js.
REPLY_EXCERPT_LENGTH = 120

UNKNOWN_NODE = "Unknown"




def format_timestamp(unix_timestamp: Optional[int]) -> str:
  """A full timestamp, in the form RxOnly shows: 8/5/2026, 2:23:01 PM.

  `%-m` is a glibc extension that also works on macOS, which the plan verified
  on macOS 26.5 / Python 3.14 — the same reason RxOnly's dashboard uses it.
  """
  if unix_timestamp is None:
    return ""
  try:
    return datetime.fromtimestamp(unix_timestamp).strftime("%-m/%-d/%Y, %-I:%M:%S %p")
  except (ValueError, TypeError, OSError):
    return ""




def format_time_short(unix_timestamp: Optional[int]) -> str:
  """A timestamp compact enough to sit at the end of a message line."""
  if unix_timestamp is None:
    return ""
  try:
    return datetime.fromtimestamp(unix_timestamp).strftime("%-m/%-d %-I:%M %p")
  except (ValueError, TypeError, OSError):
    return ""




def format_node_display_name(node: dict[str, Any]) -> str:
  """A node as "Long Name (SHORT)", falling back through short name to hex id.

  Accepts either a nodes row (`long_name`) or a message row (`from_node_long_name`),
  because both describe the same node and the interface shouldn't have to care
  which query it came from.
  """
  long_name = node.get("long_name") or node.get("from_node_long_name")
  short_name = node.get("short_name") or node.get("from_node_short_name")
  node_id = node.get("node_id") or node.get("from_node")

  if long_name and short_name:
    return f"{long_name} ({short_name})"
  if long_name:
    return long_name
  if short_name:
    return short_name
  if node_id:
    return node_id
  return UNKNOWN_NODE




def format_sender(message: dict[str, Any]) -> str:
  """The short form used in a message list: short name, else long, else id."""
  short_name = message.get("from_node_short_name")
  long_name = message.get("from_node_long_name")
  node_id = message.get("from_node")

  return short_name or long_name or node_id or UNKNOWN_NODE




# The parts of a message's top row, separated the way the sidebar separates a
# channel's counts from its note — one space either side, as the sidebar has it.
NOTE_SEPARATOR = " · "

# What marks a message of this device's own **when the colour marking it cannot be
# seen**, and only then. Jason's, and it is the third answer here: the row carried
# an accent bar down its left edge *and* the word `You`, then `You` alone, and now
# the node's own name in `$rx-outbound` instead of the usual yellow. One cue in the
# place a reader is already looking, rather than three saying the same thing.
#
# The glyph is what the colour cannot do, and it appears only where the colour is
# genuinely absent — see `MeshConsoleApp.monochrome`, which reads that off the
# environment rather than guessing at it. On a terminal showing colour this is the
# empty string and the row is the clean line it was meant to be.
OUTBOUND_GLYPH = "↑"


def format_outbound_glyph(monochrome: bool) -> str:
  """The mark after an outbound node's name, or nothing when colour says it already.

  Attached to the name with a space rather than a `NOTE_SEPARATOR`, because it is
  part of naming the sender and not a note about the message — `via MQTT` is the
  note, and it keeps the separator that says so.
  """
  return f" {OUTBOUND_GLYPH}" if monochrome else ""


def format_message_notes(message: dict[str, Any]) -> str:
  """What follows the node on a message's top row.

  One note today — that the message reached this device over MQTT rather than
  over the air — and a join rather than a return, because `via_mqtt` was not the
  first thing to be said here and will not be the last.

  **Neither the node nor `You` is in here.** Both sit to the left of these notes
  and both are coloured differently from them, so the widget assembles the row
  out of its parts rather than out of one string.
  """
  notes = []

  if message.get("via_mqtt"):
    notes.append("via MQTT")

  return NOTE_SEPARATOR.join(notes)




def format_device_name(node: Optional[dict[str, Any]]) -> str:
  """The attached device's name, for the title bar."""
  if node is None:
    return "Unknown Device"
  return format_node_display_name(node)




def format_reply_excerpt(text: Optional[str], max_length: int = REPLY_EXCERPT_LENGTH) -> str:
  """One line of the message being replied to, truncated with an ellipsis.

  Newlines and runs of spaces collapse, because this has to fit on one row.
  """
  if not text:
    return ""

  cleaned = " ".join(text.split())

  if len(cleaned) <= max_length:
    return cleaned
  return cleaned[:max_length] + "…"




def format_reply_line(message: dict[str, Any]) -> Optional[Text]:
  """The reply bar for a message, or None when it isn't a reply.

  A message can name a `reply_to` whose parent has been pruned out of the
  archive, in which case there is nothing to excerpt and no bar to draw.

  **A `Text` rather than a string, because the bar is three things and RxOnly
  styles them separately**: `Reply to:` is a `<strong>`, the author is plain, and
  the excerpt is an `<em>` (`messages.js:349`). The console had the whole line in
  italic, which read as one quoted phrase and lost the label. Only weights are set
  here — the colour is the whole bar's and lives in `mesh_console.tcss` — so this
  stays a formatter and nothing here needs the palette.
  """
  if message.get("reply_to") is None:
    return None
  if message.get("reply_to_text") is None:
    return None

  author = (
    message.get("reply_to_from_node_short_name")
    or message.get("reply_to_from_node")
    or UNKNOWN_NODE
  )

  line = Text(no_wrap=True, overflow="ellipsis")
  line.append("Reply to:", style=Style(bold=True))
  line.append(f" {author} - ")
  line.append(format_reply_excerpt(message["reply_to_text"]), style=Style(italic=True))
  return line




def format_peer_label(node: Optional[dict[str, Any]], peer_id: str) -> str:
  """A DM recipient as `HILL (!7c2f91a4)` — short name *and* hex id, always both.

  **This is the one label standing between a reader and the wrong stranger**, so
  it names the node id even when there is a perfectly good short name to use
  instead. Two nodes on a public mesh can share a short name, and nothing stops
  them; `format_node_display_name` is the right answer everywhere the cost of
  ambiguity is a mildly confusing list row, and the wrong one here.

  A node that has never sent a NodeInfo has no short name to show, so the label is
  the hex id alone. That is a real case — the fixture has `!5d81cc30` — and it is
  addressable anyway: `validate_destination` resolves any well-formed hex id
  without consulting a node database, which is how a mesh works. The label simply
  has less to say about who you are talking to, and says only what it knows.
  """
  short_name = None
  if node is not None:
    short_name = node.get("short_name") or node.get("from_node_short_name")

  if short_name:
    return f"{short_name} ({peer_id})"
  return peer_id




def format_reply_marker(message: Optional[dict[str, Any]], max_length: int = 40) -> str:
  """What the compose box says it is answering, or nothing when it answers nothing.

  Shorter than `format_reply_excerpt` because this shares a border title with the
  destination, and the destination is the part that must not be pushed off the end.
  """
  if not message:
    return ""

  author = format_sender(message)
  excerpt = format_reply_excerpt(message.get("text"), max_length)

  if excerpt:
    return f"↩ {author}: {excerpt}"
  return f"↩ {author}"




def format_channel_label(channel: dict[str, Any]) -> str:
  """A channel's name, or "Channel N" when the collector recorded none."""
  name = channel.get("name")
  if name:
    return str(name)
  return f"Channel {channel.get('channel_index')}"




def format_fields(fields: list[tuple[str, Any]], indent: str = "  ") -> list[str]:
  """Label-and-value lines, with the empty ones left out entirely.

  A field the collector never recorded is absent from the archive, not zero and
  not the word "None" — a node that has sent no position has no latitude, and
  printing `Latitude: None` claims it reported one. So an empty value drops its
  whole line, the way the dashboard's local-node block already does, and a
  detail view of a sparse row is short rather than full of holes.

  `0` and `0.0` are values and stay: a hop count of zero means heard direct,
  which is the most interesting hop count there is.
  """
  width = max((len(label) for label, _ in fields), default=0) + 1

  return [
    f"{indent}{label + ':':<{width}} {value}"
    for label, value in fields
    if value is not None and value != ""
  ]




def format_coordinates(node: dict[str, Any]) -> Optional[str]:
  """A map link for a node that has reported a position, or None.

  RxOnly shows this as a hyperlink on the node detail view (`views.js:88`). A
  terminal has nowhere to hide a URL behind link text, so it prints the URL itself —
  clickable through the app, openable with `o`, and there to be copied either way.
  """
  latitude = node.get("latitude")
  longitude = node.get("longitude")

  if latitude is None or longitude is None:
    return None

  return f"https://www.openstreetmap.org/?mlat={latitude}&mlon={longitude}#map=9"




def _append_fields(
  text: Text,
  fields: list[tuple[str, Any]],
  label_style: Optional[Style],
  indent: str = "  ",
) -> None:
  """`format_fields`, but appended to a `Text` with the labels styled separately.

  The alignment is `format_fields`'s, deliberately identical — the two are the same
  block in two mediums, and a detail view whose columns did not line up with the
  message detail's would be the first thing anyone noticed. The empty-value rule is
  the same too: a field the collector never recorded drops its whole line.
  """
  width = max((len(label) for label, _ in fields), default=0) + 1

  for label, value in fields:
    if value is None or value == "":
      continue
    text.append(f"{indent}{label + ':':<{width}}", style=label_style or "")
    text.append(f" {value}\n")




def format_node_detail(
  node: dict[str, Any],
  conversation: Optional[dict[str, Any]] = None,
  *,
  label_style: Optional[Style] = None,
  map_action: Optional[str] = None,
) -> Text:
  """Everything the archive holds about one node.

  Three of these fields — latitude, longitude and altitude — are read by
  `fetch_node` and were rendered nowhere in this project until this view existed.

  The six telemetry fields below altitude arrived with schema 0.8.0, and most nodes
  will show none of them: only a node carrying an environment sensor reports
  temperature, humidity or pressure, and the radio-health trio needs a node that
  sends device metrics. `_append_fields` drops a missing line rather than printing a
  zero for it, so a plain node's panel looks exactly as it did before — which is why
  these could be added without a display switch.

  `conversation` is `{message_count, unread}` when this node is one you can hold a
  conversation with, and None when it is not — the attached device itself, or a
  console that cannot send. **A count of zero still gets the block**, because "no
  direct messages yet" and "you cannot start one" are different facts and a node
  you have never messaged is perfectly addressable. The caller decides which case
  this is; this function only renders it.

  **Returns a `Text`, not a string**, so the field labels and the map link can be
  styled without markup — the values here include node names off the mesh, and this
  project renders those with `markup=False` everywhere. The three style arguments
  are optional and default to unstyled, which keeps this callable as a plain
  renderer; the interface passes the component styles its stylesheet resolved.
  `str()` on the result is still the plain text.
  """
  text = Text(no_wrap=False)
  text.append(format_node_display_name(node))
  text.append("\n\n")

  _append_fields(text, [
    ("Node ID", node.get("node_id")),
    ("Long Name", node.get("long_name")),
    ("Short Name", node.get("short_name")),
    ("Hardware", node.get("hardware")),
    ("Role", node.get("role")),
    ("First Seen", format_timestamp(node.get("first_seen"))),
    ("Last Seen", format_timestamp(node.get("last_seen"))),
    # Between Last Seen and Battery because that is where RxOnly puts it, and the
    # two panels are the same block in two mediums. Bare integer, no unit — RxOnly's
    # field map gives it no `format` either, and "3" reads as hops in a row labelled
    # Hops Away. A node the mesh has never told us a hop count for drops the line,
    # like every other absent field; a *zero* is a direct neighbour, which is the
    # loudest thing this column says, and `_append_fields` keeps it because it tests
    # None and "" rather than falsiness.
    ("Hops Away", node.get("hops_away")),
    ("Battery", _with_unit(node.get("battery_level"), "%")),
    ("Voltage", _with_unit(node.get("voltage"), "V")),
    ("SNR", node.get("snr")),
    ("RSSI", node.get("rssi")),
    ("Latitude", node.get("latitude")),
    ("Longitude", node.get("longitude")),
    ("Altitude", _with_unit(node.get("altitude"), "m")),
    ("Temperature", _with_unit(node.get("temperature"), "°C", 1)),
    ("Humidity", _with_unit(node.get("humidity"), "%", 1)),
    ("Pressure", _with_unit(node.get("pressure"), " hPa", 1)),
    ("Channel Util", _with_unit(node.get("channel_util"), "%", 2)),
    ("Air Util TX", _with_unit(node.get("air_util_tx"), "%", 2)),
    ("Uptime", format_uptime(node.get("uptime_seconds"))),
  ], label_style)

  map_link = format_coordinates(node)
  if map_link:
    # `@click` and nothing else. It is Textual's own meta, dispatched to an action in
    # this app, so it works regardless of what the terminal can do. See
    # `action_open_map`.
    #
    # **The OSC 8 `link=` that used to sit beside it is gone, and it is what made
    # hovering the link strobe.** Rich mints a fresh link id every time it copies a
    # style that carries a `link` (`Style.copy`, and every render goes through one),
    # while a style carrying only `meta` keeps the id it was made with. Textual's
    # hover treatment is applied by matching the id it recorded under the pointer
    # against the ids in the freshly rendered line — so with `link=` present the id
    # had moved on by the time the line was drawn, the match failed, and the
    # highlight came and went with every mouse movement across the URL. The trail's
    # crumbs never did this, and this is why: they carry a `@click` and nothing else.
    #
    # Nothing that worked is lost. The comment on `action_open_map` already recorded
    # that a Textual app holds mouse reporting, so the terminal's own hyperlink
    # handling — cmd-click, the right-click menu — is not reachable over the app's
    # surface anyway; what the OSC 8 sequence was buying was a hover that flickered.
    # Clicking still opens the map, `o` still opens it without a mouse, and the URL
    # is still on screen to be copied.
    #
    # **The action has to name a namespace, and this is not a style preference.**
    # Textual brokers a `@click` from the widget the click landed on
    # (`Widget.broker_event` passes `default_namespace=self`), and a bare action name
    # resolves against that namespace — here the `DetailView`, which has no
    # `action_open_map`. `_dispatch_action` then logs "has no target" and returns
    # False, so the link renders, hovers, and does nothing when clicked, with nothing
    # said anywhere a reader would see it. `app.` sends it to the App, which is the
    # object that owns the action. Checked in the suite, because the failure is
    # invisible from the Python.
    #
    # No colour is set here. A span carrying `@click` is a link as far as Textual is
    # concerned, so it takes the pane's `link-color` and `link-style` from the
    # stylesheet — including the hover treatment, which nothing set on the span
    # itself would get.
    style = Style(meta={"@click": map_action}) if map_action else Style()

    text.append("\n")
    text.append(map_link, style=style)
    text.append("\n")

  if conversation is not None:
    count = conversation.get("message_count") or 0
    unread = conversation.get("unread") or 0

    if count:
      summary = f"{count} message{'s' if count != 1 else ''}"
      if unread:
        summary += f" · {unread} unread"
    else:
      summary = "No direct messages yet"

    text.append("\n")
    text.append("Direct Messages\n")
    text.append(f"  {summary}\n")
    text.append("\n")
    text.append("  Press enter to open")

  # In place, and returns None — `Text.rstrip` is not `str.rstrip`.
  text.rstrip()
  return text




def format_message_detail(message: dict[str, Any]) -> str:
  """Everything the archive holds about one message.

  `snr`, `rssi` and `hop_count` are the reason this view is worth having: the
  read tier has returned them since Phase 3 and a message row shows none of
  them, because a list of messages is about what was said rather than how well
  it arrived. An outbound row has all three null — nothing was received, so
  nothing was measured — and they drop out rather than reading as zeroes.
  """
  channel = message.get("channel_name")
  if not channel and message.get("channel_index") is not None:
    channel = f"Channel {message['channel_index']}"

  lines = ["Message Details", ""]

  lines += format_fields([
    ("Message ID", message.get("message_id")),
    ("From", format_node_display_name(message)),
    ("To", message.get("to_node")),
    ("Channel", channel),
    ("Received", format_timestamp(message.get("rx_time"))),
    ("Reply To", message.get("reply_to")),
    ("Hops", message.get("hop_count")),
    ("SNR", message.get("snr")),
    ("RSSI", message.get("rssi")),
    ("Via MQTT", "Yes" if message.get("via_mqtt") else "No"),
  ])

  lines += ["", "Message", ""]
  lines.append(message.get("text") or "")

  return "\n".join(lines)




def _with_unit(value: Any, unit: str, places: Optional[int] = None) -> Optional[str]:
  """`85` becomes `85%`, and nothing stays nothing.

  `places` rounds before labelling, for the 0.8.0 telemetry readings the mesh
  reports at full float precision. The archive keeps every digit the wire sent —
  24.002031 is the real reading and the ingest suite pins it — but a detail panel
  is a label, and this is where the rounding belongs rather than in the query.
  """
  if value is None or value == "":
    return None
  if places is not None:
    try:
      return f"{float(value):.{places}f}{unit}"
    except (TypeError, ValueError):
      return None
  return f"{value}{unit}"




def format_age(unix_timestamp: Optional[int], now: Optional[int] = None) -> str:
  """How long ago that was, coarsely: `4s ago`, `12m ago`, `3h ago`, `2d ago`.

  For the menu's "Updated", where the reader is asking one yes-or-no question —
  is anything still arriving? — and `1d 4h 12m ago` answers it no better than
  `1d ago` while taking three times the room. So this is the coarsest unit that
  fits and nothing below it, which is the opposite of `format_uptime`'s choice
  and correct for the opposite reason: an uptime is a quantity someone reads, an
  age is a quantity someone glances at.

  `now` is a parameter so this can be tested without waiting, and defaults to the
  clock. Both sides are unix seconds in the archive's own terms.

  **A clock that disagrees with the collector's reads as the future**, which is not
  hypothetical between a laptop and a Pi: a timestamp ahead of `now` gives a
  negative age, and `-3s ago` would look like a bug in this console rather than
  like the clock skew it is. Such an age is reported as `just now` — the only
  honest reading of "newer than I think the present is" that does not accuse
  anybody of anything.

  An archive with no messages at all returns `never`, which is a real state on a
  console opened against a collector that has just started.
  """
  if unix_timestamp is None:
    return "never"

  try:
    then = int(unix_timestamp)
  except (TypeError, ValueError):
    return "never"

  current = int(datetime.now().timestamp()) if now is None else int(now)
  elapsed = current - then

  if elapsed < 0:
    return "just now"
  if elapsed < 60:
    return f"{elapsed}s ago"
  if elapsed < 3600:
    return f"{elapsed // 60}m ago"
  if elapsed < 86400:
    return f"{elapsed // 3600}h ago"
  return f"{elapsed // 86400}d ago"




def format_uptime(seconds: Any) -> Optional[str]:
  """`90061` becomes `1d 1h 1m`, and nothing stays nothing.

  Mirrors `format_uptime` in RxOnly's `static/js/rxonly.js` — same seconds in,
  same string out, so a node's uptime reads identically in the terminal and the
  browser. Units the count doesn't reach are dropped rather than printed as zero,
  and the seconds place only appears when it is the whole answer.

  **Zero is a reading, not an absence.** A device that rebooted a moment ago
  reports 0 and gets `0s`; only NULL — a node that has never reported uptime at
  all — returns None and drops its line.
  """
  if seconds is None or seconds == "":
    return None
  try:
    total = int(seconds)
  except (TypeError, ValueError):
    return None
  if total < 0:
    return None

  days, remainder = divmod(total, 86400)
  hours, remainder = divmod(remainder, 3600)
  minutes, whole_seconds = divmod(remainder, 60)

  parts: list[str] = []
  if days:
    parts.append(f"{days}d")
  if hours:
    parts.append(f"{hours}h")
  if minutes:
    parts.append(f"{minutes}m")
  if not parts:
    parts.append(f"{whole_seconds}s")

  return " ".join(parts)
