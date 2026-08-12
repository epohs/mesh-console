"""The list rows: one class per kind of thing the sidebar and message pane hold.

Each takes an archive row and is responsible for turning it into something
readable. **All of them render with `markup=False`.** Message text, node names and
channel names all come off the mesh, so a channel called `[bold]` is a channel
with an odd name, not an instruction to this program.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Optional, Sequence

from rich.style import Style
from rich.text import Text

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Footer, Label, ListItem, ListView, Log, Static
# `Footer` is public and the key rows it is built from are not, so the corner
# below is assembled from a private name. It is the same class Textual's own
# footer yields for every key, and taking it is what keeps the docked key
# identical to its neighbours rather than a hand-drawn imitation of one.
from textual.widgets._footer import FooterKey

from mesh_console.ui.format import (
  NOTE_SEPARATOR,
  format_message_notes,
  format_node_display_name,
  format_outbound_glyph,
  format_reply_line,
  format_time_short,
  format_timestamp,
)
from mesh_console.ui.logfmt import LogHighlighter
from mesh_console.ui.tapbacks import format_tapback_line


# The alternating row background, which RxOnly gets from
# `.channels-list li:nth-child(odd)` and `.nodes-list li:nth-child(odd)`. Textual's
# CSS has no `nth-child`, so the parity has to be decided here, where the rows are
# made, and carried as a class the stylesheet can see.
#
# Only the two sidebar lists stripe, in both interfaces — the web's rule names those
# two and not the message list, where a row is several lines tall and a stripe would
# band the text rather than separate the rows.
STRIPE_CLASS = "stripe"




def striped(item: ListItem, index: int) -> ListItem:
  """Mark every other row, matching which rows RxOnly stripes.

  `nth-child(odd)` is 1-based and so picks the first, third and fifth rows; this is
  handed a 0-based index, which is why the test is for an even one. Returns the item
  so it can be written inline in an `append`.

  **Sets the class both ways rather than only adding it**, because `reconcile` hands
  rows back here after moving them and a row that has changed parity has to lose the
  stripe as well as gain one. Adding only was correct while every row was newly built
  and therefore unstriped to begin with; it would have left a shaded row shaded as it
  moved up the list.
  """
  item.set_class(index % 2 == 0, STRIPE_CLASS)
  return item




def reconcile(listing: ListView, wanted: Sequence[ListItem]) -> None:
  """Make `listing` hold exactly `wanted`, in that order, reusing the rows it has.

  **This exists because clearing a list and refilling it makes the list blink**, and
  on a Pi it blinks for the better part of a second. Neither half of `clear()` plus
  `append()` is synchronous in Textual: `clear` leaves the old rows in the tree with
  a `Prune` posted to them, and each appended row composes its own children when its
  `Mount` is pumped. Measured on the Pi against a real archive, the compositor paints
  a sidebar with *no rows on it at all* about 450ms in — old rows out of the layout,
  new ones not yet arranged into it — and the filled frame does not arrive for
  another 250 to 400ms. Every rebuild, and since the poll started redrawing the node
  list whenever nobody is holding it, that is every ten seconds.

  Suspending repaints across the gap was tried first and is the wrong tool: a batch
  freezes the whole screen, this redraw runs precisely when the reader's hands are
  somewhere else — the compose box, most likely — and a mask long enough to cover the
  Pi would sit on their keystrokes for the best part of a second, every ten seconds.

  So the answer is not to hide the teardown but to stop doing one. A row is a widget
  that already knows how to be updated in place (`NodeItem.set_node` and its
  siblings), and between two polls the *set* of rows almost never changes — what
  changes is their order, because a node that was just heard sorts to the top. Rows
  that survive are moved rather than rebuilt, so there is nothing to be missing while
  the compositor waits, and the expensive half of a rebuild is not spent either: 50
  `NodeItem`s and their 100 labels cost about 150ms of blocked event loop on the Pi
  just to construct.

  The caller decides what `wanted` holds, because only the caller knows how to match
  an archive row to the widget already showing it and how to refresh one that has
  moved on. What happens here is the DOM work that is the same every time.

  **Order is settled by naming neighbours, never by index.** A removed row is still
  in `listing.children` until its `Prune` is pumped, so positions counted now are
  positions that include rows on their way out. Walking backwards and asking only
  that each row precede the next puts them all in order in one pass — moving a row to
  before its successor cannot disturb the pairs already settled behind it — and rows
  awaiting removal fall out from between them as they go.
  """
  keep = {id(item) for item in wanted}
  for child in list(listing.children):
    if id(child) not in keep:
      child.remove()

  # `parent` is set by `_register`, which mounting does synchronously, so this is
  # "not mounted yet" and stays true only for rows this call has just built.
  fresh = [item for item in wanted if item.parent is None]
  if fresh:
    listing.mount_all(fresh)

  children = listing.children
  for position in range(len(wanted) - 2, -1, -1):
    if children.index(wanted[position]) > children.index(wanted[position + 1]):
      listing.move_child(wanted[position], before=wanted[position + 1])

  # **The stripe is what a redraw costs now, and it is priced per row that changes.**
  # A `set_class` that changes nothing is free; one that does calls
  # `App.update_styles`, which reapplies the stylesheet to that row and its labels.
  # Moving one node to the top of a fifty row page shifts every row it passed, and
  # all of them change parity — measured on the Pi, a node coming up from row 40 cost
  # 40 flips and 122ms, against 6 flips and 20ms for one coming up from row 6. That
  # is the whole of what a redraw costs once the teardown is gone, and it is still
  # well under what the teardown cost unconditionally.
  #
  # **Asking for one update over the whole list instead is slower, and was tried.**
  # `update_styles` walks the node and every descendant, so one call over the list is
  # 151 nodes — the list, fifty rows, a hundred labels — where forty per-row calls
  # are 120. Measured, that traded a 32-to-132ms spread for a flat 135ms: it pays for
  # every row on every redraw, including the rows that did not move. The per-row cost
  # is proportional to the rows that actually changed, which is the right shape.
  for position, item in enumerate(wanted):
    striped(item, position)




class EmptyItem(ListItem):
  """The single row a list shows when it has nothing to show.

  A class of its own so it can be reused the way every other row now is. As a bare
  `ListItem(Label(...))` it had no identity `reconcile` could match, so a list that
  was empty and stayed empty tore its own "No messages" down and built it back every
  poll — the blink this all exists to remove, on the one row still capable of it.
  """


  def __init__(self, text: str) -> None:
    self._label = Label(text, markup=False)
    super().__init__(self._label)


  def set_text(self, text: str) -> None:
    """Say something else, for a list whose reason to be empty has changed.

    The node list has two — nothing in the archive, or nothing matching the filter —
    and typing into the search box moves between them.
    """
    self._label.update(text)




class Crumb(NamedTuple):
  """One step in the trail: what it says, and where clicking it goes.

  `target` is one of the `CRUMB_*` constants in `mesh_console.ui`, or None for a
  crumb that is not a way anywhere — which is every crumb naming the place you are
  already at. It is a tag rather than a callable so that the widget stays a
  renderer: what a target *means* is the app's business, and `action_breadcrumb` is
  the one place that decides.

  RxOnly's crumbs carry `{label, href, view}` (`rxonly.js:911`), which is the same
  shape for the same reason — a trail that can be walked back up has to know what
  each step refers to, not just what it reads as.
  """

  label: str
  target: Optional[str] = None




class Breadcrumbs(Static):
  """The trail across the top of the main pane, with its parts styled separately.

  A `Text` built span by span rather than a marked-up string, because a crumb can
  be a channel name off the mesh and this project renders those with `markup=False`
  everywhere — a channel called `[bold]` is a channel with an odd name. `Text.append`
  takes styles as objects, so there is nothing in the string for anything to parse.

  The colours come from the stylesheet rather than from Python, so they live in
  `mesh_console.tcss` with every other colour and follow the theme.

  **The trail is a control, and it did not used to be.** This docstring said the
  opposite until Jason asked for it: "a terminal navigates with a key, not by
  clicking a trail", with `escape` as the way back up. That reasoning held while the
  trail was the only thing on screen a mouse could plausibly want, and it is the
  divergence from RxOnly — whose crumbs have always been `<a href>` — that a reader
  coming from the web page would notice first. `escape` still walks back up one step
  at a time; clicking goes straight to the step you named.
  """


  COMPONENT_CLASSES = {"breadcrumbs--separator"}

  # RxOnly's `.breadcrumbs li:not(:last-child)::after` content, with room around it.
  SEPARATOR = "  ›  "


  def set_trail(self, *crumbs: "str | Crumb") -> None:
    """Render the trail: the first crumb bold, and every step with somewhere to go a link.

    Takes a bare string or a `Crumb`, so a caller with nothing to navigate to writes
    the string it always wrote — which is every trail's last crumb, and the whole of
    the dashboard's.

    **The last crumb is never a link, whatever it was handed.** It names the place
    you are already at, and clicking it would either do nothing or reload the view
    underneath the reader. Enforced here rather than left to the callers, because it
    is the invariant that keeps the trail honest and a future trail could otherwise
    break it without anything failing.

    **The first crumb is bold, which is the opposite end from RxOnly.** It bolds the
    *current* page (`[aria-current="page"]`) and styles its ancestors as links; on
    the dashboard, where the trail is one crumb long, the two agree. Jason asked for
    the first, so the first it is; flipping it is `position == 0` becoming
    `position == last`.

    No colour is set on a clickable crumb. A span carrying `@click` is a link as far
    as Textual is concerned, so it takes `link-color` and `link-style` from the
    stylesheet — and, more to the point, `link-*-hover`, which is the only thing that
    says the crumb can be clicked before it is.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    last = len(crumbs) - 1

    for position, crumb in enumerate(crumbs):
      step = crumb if isinstance(crumb, Crumb) else Crumb(crumb)

      if position:
        text.append(self.SEPARATOR, style=self.get_component_rich_style(
          "breadcrumbs--separator"))

      style = Style(bold=True) if position == 0 else Style()
      if step.target is not None and position != last:
        # A 2-tuple rather than `f"app.breadcrumb('{target}')"`. `_broker_event`
        # accepts either, and this way the parameter never goes through an action
        # string — so nothing has to be escaped and no label can be mistaken for
        # syntax. `app.`-qualified because Textual brokers a `@click` from the widget
        # it landed on, and a bare name would resolve against this widget, which has
        # no such action. That failure is silent; see `action_open_map`.
        style += Style(meta={"@click": ("app.breadcrumb", (step.target,))})

      text.append(step.label, style=style)

    self.update(text)




class DetailView(Static):
  """The pane that shows one node or one message in full.

  It exists as a class only to own one component class: the detail body wants its
  field labels a different colour from their values, and colours belong in
  `mesh_console.tcss` rather than in the formatter. The formatter is handed the
  style and stays free of literals.

  **No helper methods on this class, deliberately.** The obvious pair —
  `label_style()` and `link_style()` — cannot both be written, because
  `Widget.link_style` is already a Textual property and Textual reads it while
  rendering any span carrying a `@click`. Shadowing it with a method makes the pane
  raise `AttributeError: 'function' object has no attribute '_null'` from inside
  `Strip._apply_link_style`, which names nothing that would lead you back here.
  Callers use `get_component_rich_style` directly.
  """


  COMPONENT_CLASSES = {"detail--label"}




class ChannelItem(ListItem):
  """A sidebar entry for one channel, or for the direct message list.

  It keeps its name, count and note as separate values rather than only as a
  rendered string, so refreshing a count on a poll doesn't mean parsing the
  label back apart — a channel legitimately named "Net (backup)" would not
  survive that.
  """


  def __init__(
    self,
    channel_name: str,
    *,
    is_dm: bool = False,
    channel_index: Optional[int] = None,
    count: int = 0,
    unread: int = 0,
    note: str = "",
  ) -> None:
    self.is_dm = is_dm
    self.channel_index = channel_index
    # Not `name`: Widget already owns that attribute.
    self.channel_name = channel_name
    self.count = count
    self.unread = unread
    self.note = note

    self._label = Label(self.label_text(), markup=False)
    super().__init__(self._label)

    if unread:
      self.add_class("unread")




  def label_text(self) -> str:
    """`LongFast (5) · 2 unread`, with each part left out when it has nothing to say.

    The unread count is a separate field rather than folded into the total,
    because they answer different questions — how much is in this channel, and
    how much of it is waiting for you — and RxOnly has no equivalent to copy: it
    expresses unread by scrolling and styling rows, and never as a number.
    """
    text = f"{self.channel_name} ({self.count})"
    if self.unread:
      text += f" · {self.unread} unread"
    if self.note:
      text += f" — {self.note}"
    return text




  def set_counts(self, count: int, unread: int) -> None:
    """Update both numbers in place, without rebuilding the sidebar.

    Rebuilding would drop whatever the reader had selected, which is the reason
    this widget keeps its parts rather than only a rendered string.
    """
    if count == self.count and unread == self.unread:
      return

    self.count = count
    self.unread = unread
    self._label.update(self.label_text())

    # A channel that has just been read to the end should stop looking unread
    # without waiting for the sidebar to be rebuilt.
    self.set_class(bool(unread), "unread")




  def set_row(self, channel_name: str, count: int, unread: int, note: str) -> None:
    """Redraw the whole row from a fresher archive row, for `reconcile`.

    `set_counts` is the poll's version of this and moves the two numbers alone,
    because those are the only parts a poll can change. A sidebar rebuild is where a
    channel can also have been renamed, or where the collector's answer about
    archiving direct messages has changed under the row that reports it, so this
    takes every field the constructor takes.
    """
    if (channel_name, count, unread, note) == (
      self.channel_name, self.count, self.unread, self.note
    ):
      return

    self.channel_name = channel_name
    self.count = count
    self.unread = unread
    self.note = note
    self._label.update(self.label_text())
    self.set_class(bool(unread), "unread")




class NodeItem(ListItem):
  """A sidebar entry for one node: its name, and when it was last heard.

  Two lines, because RxOnly's is two — `.nodes-list .node-link` sets
  `flex-direction: column` so the name stacks above a `.node-last-seen` timestamp.
  The web makes that second line smaller *and* dimmer; a terminal has one type
  size, so it carries the dimness alone and the colour does the whole job.

  It keeps its archive row and holds references to both labels, for the same reason
  `ChannelItem` keeps its counts apart from its rendered string: the poll refreshes
  a row in place, and rebuilding the list to move a timestamp would throw away
  whatever the reader had selected. See `set_node`.
  """


  def __init__(self, node: dict[str, Any]) -> None:
    self.node_id: str = node["node_id"]
    self.node = node

    # `_name_label`, not `_name`: `DOMNode.__init__` owns `_name` and sets it from
    # its `name` argument, so a label assigned there is replaced by None the moment
    # `super().__init__()` runs and `compose` then tries to mount it. The same
    # collision `ChannelItem` avoids one class up, one underscore further in.
    self._name_label = Label(
      format_node_display_name(node), markup=False, classes="node-name"
    )
    # **Always constructed, and hidden when there is nothing to say**, rather than
    # conditionally yielded. The row still reads the same — `display: none` takes no
    # line, so a node with no last_seen is one line tall exactly as it was — but the
    # label exists to be updated when a silent node is finally heard from. Yielding
    # it conditionally meant that node could never grow its second line without the
    # whole list being rebuilt.
    self._last_seen_label = Label("", markup=False, classes="node-last-seen")

    super().__init__()

    self._apply_last_seen(node)


  def compose(self) -> ComposeResult:
    yield self._name_label
    yield self._last_seen_label


  def _apply_last_seen(self, node: dict[str, Any]) -> None:
    """Set the second line, or take it away when the archive has no timestamp.

    Absent rather than blank: an empty line under a name is a row that looks like
    it failed to render.
    """
    last_seen = format_timestamp(node.get("last_seen"))
    self._last_seen_label.update(last_seen)
    self._last_seen_label.display = bool(last_seen)


  def set_node(self, node: dict[str, Any]) -> bool:
    """Redraw this row from a fresher archive row, without rebuilding the list.

    Both lines can move while the sidebar is on screen and for different reasons:
    the timestamp every time the node is heard, the name the first time it sends a
    NodeInfo — until then it is listed by its hex id.

    **This does not re-sort the list, deliberately.** `fetch_nodes` orders by
    `last_seen DESC`, so keeping the sidebar in that order under a poll would move
    rows out from under the cursor every ten seconds, which is the same reason
    `refresh_channel_counts` updates numbers in place instead of rebuilding. The
    order on screen is the order as of the last real load, and `r` is what reorders
    it.

    Returns whether anything changed, so a caller can skip the work of asking the
    stylesheet to redraw a row that says what it said before.
    """
    if node == self.node:
      return False

    self.node = node
    self._name_label.update(format_node_display_name(node))
    self._apply_last_seen(node)
    return True




class ConversationItem(ListItem):
  """One correspondent: who the thread is with, when it last moved, how big it is.

  **This replaced the flat chronological list of every direct message.** That list
  was every conversation at once — the reason it could never have a compose box, and
  the reason a row in it had to name both its ends before you could tell who it was
  with. Jason's call: group them, and let a row be a person.

  Two lines, the shape `NodeItem` already uses in the same sidebar-sized idiom: who,
  then the quieter facts about them. The first line is the trail a message takes,
  `RX1 › POMM`, so the list reads the same way its rows do.
  """


  def __init__(
    self,
    conversation: dict[str, Any],
    local_label: str,
    unread: int = 0,
  ) -> None:
    self.peer: str = conversation["peer"]
    self.conversation = conversation
    self.unread = unread

    self._title = Label(
      f"{local_label}  ›  {self._peer_label()}", markup=False, classes="conversation-peer"
    )
    self._summary = Label("", markup=False, classes="conversation-summary")

    super().__init__()

    self._apply_summary()
    # The same accent, and the same meaning it has on a message row: something here
    # is waiting for you. A conversation you have only spoken in never lights up.
    self.set_class(bool(unread), "unread")


  def compose(self) -> ComposeResult:
    yield self._title
    yield self._summary


  def _peer_label(self) -> str:
    """The peer's short name, or the hex id when the archive has no name for them.

    Short name alone rather than `format_peer_label`'s name-and-id, for the reason
    `format_direct_parties` gives: this is a list of people you have talked to, not a
    destination about to be addressed.
    """
    return self.conversation.get("peer_short_name") or self.peer


  def _apply_summary(self) -> None:
    count = self.conversation.get("message_count") or 0
    summary = f"{format_time_short(self.conversation.get('newest_rx_time'))}"
    summary += f"  ·  {count} message{'s' if count != 1 else ''}"
    if self.unread:
      summary += f"  ·  {self.unread} unread"
    self._summary.update(summary)


  def set_unread(self, unread: int) -> None:
    """Update the count in place, the way `ChannelItem.set_counts` does.

    Rebuilding the list on a poll would drop whichever conversation the reader had
    selected, and this list is navigated with the cursor.
    """
    if unread == self.unread:
      return
    self.unread = unread
    self._apply_summary()
    self.set_class(bool(unread), "unread")


  def set_conversation(
    self, conversation: dict[str, Any], local_label: str, unread: int
  ) -> None:
    """Redraw the row from a fresher archive row, for `reconcile`.

    Everything on it moves while it is on screen and a message arriving moves all of
    it at once: the count, the time of the newest message, and how much of the thread
    is still waiting. The peer is what identifies the row, so it is the one thing
    this cannot change — `reconcile` is only ever handed this row for the same
    correspondent it was built for.

    `local_label` is passed rather than remembered because the first line names both
    ends, and this device's own short name arrives late — until it has sent a
    NodeInfo the archive has only its hex id.
    """
    unchanged = conversation == self.conversation and unread == self.unread

    # Assigned before the title is built, because `_peer_label` reads the peer's
    # short name off it — and that name arriving is one of the things that moves
    # this line.
    self.conversation = conversation
    self.unread = unread

    # Said even when nothing else has, because the other half of the line is
    # `local_label`, which this row does not keep and cannot compare against.
    self._title.update(f"{local_label}  ›  {self._peer_label()}")

    if unchanged:
      return

    self._apply_summary()
    self.set_class(bool(unread), "unread")




class MessageList(ListView):
  """The message pane's list, which starts with nothing selected and no read job.

  A `ListView` is a cursor widget, and this one is deliberately used without a
  cursor most of the time: reading a channel is scrolling it now, so a highlight
  sitting on a row would be claiming a choice the reader had not made. `index` is
  None until an arrow key asks for a selection.

  **The only thing this class adds is where that first arrow key lands.** Textual's
  own `action_cursor_down` on an unselected list goes to row zero, which is the top
  of the loaded window — usually a screen or more above what the reader is looking
  at, and it would yank the pane up there. `start_row` is where the app says the
  reader has read to (`MeshConsoleApp.read_row`), so the first press selects the
  message they are up to and the second moves off it.

  `start_row` is pushed in rather than read off the app, so this stays a widget
  that can be mounted and driven without one.
  """


  start_row: int = 0


  def _select_start(self) -> bool:
    """Take the selection out of nothing, if there is nothing. True if it did."""
    if self.index is not None:
      return False
    if not self.children:
      return False

    self.index = max(0, min(self.start_row, len(self.children) - 1))
    return True


  def action_cursor_down(self) -> None:
    if self._select_start():
      return
    super().action_cursor_down()


  def action_cursor_up(self) -> None:
    if self._select_start():
      return
    super().action_cursor_up()




class MessageHeader(Horizontal):
  """A message's top row: who sent it hard left, when it arrived hard right.

  RxOnly's `.message-header` is `display: flex; justify-content: space-between`
  with the sender and the timestamp as its two ends, which is what this is — a
  horizontal container rather than one label with separators, because the gap
  between the two has to be whatever the terminal is wide and a padded string
  would be wrong at every width but one.

  **The sender is the node, in full: `Long Name (SHORT)`.** It used to be the
  short name alone, which is the one place in this suite that named a node
  differently from everywhere else in it — the sidebar, the node detail and the
  breadcrumbs have always used `format_node_display_name`, and so has the web's
  message list (`format_node_display_html`, rxonly.js:245). A node that has sent
  no NodeInfo has neither name and falls through to its hex id, which is a real
  case rather than a placeholder: `!5d81cc30` is somebody, and it is the only
  thing anyone knows about them.

  The colour is `$rx-accent-node`, the yellow this suite gives node names
  everywhere, and it is applied to the name and not to the notes beside it. That
  is the one style here that cannot come from a rule on the label, because label
  and notes share a line — hence the component class.
  """


  COMPONENT_CLASSES = {"message-header--node", "message-header--node-outbound"}


  def __init__(self, message: dict[str, Any], outbound: bool = False) -> None:
    self.message = message
    self.outbound = outbound
    super().__init__(classes="message-header")


  def compose(self) -> ComposeResult:
    # `markup=False` for the same reason every other row in this file sets it: a
    # node called `[bold]` is a node with an odd name.
    identity = Text(no_wrap=True, overflow="ellipsis")

    # **The sender's own name is what says the message is yours**, in `$rx-outbound`
    # rather than the usual node yellow. The accent bar down the row's left edge and
    # the word `You` beside the name are both gone: three cues for one fact, and the
    # left edge is now spoken for by the selection.
    #
    # `partial=True`, and it is the difference between a coloured name and a coloured
    # box. The full component style is the rule combined with everything it inherits,
    # so it carries this widget's background as well as the rule's colour — baked
    # into the span when the row is built, and therefore still the unselected page
    # colour when anything is drawn behind it. A partial style is only what the rule
    # itself sets.
    name = format_node_display_name(self.message)
    if self.outbound:
      name += format_outbound_glyph(self.app.monochrome)

    identity.append(name, style=self.get_component_rich_style(
      "message-header--node-outbound" if self.outbound else "message-header--node",
      partial=True,
    ))

    notes = format_message_notes(self.message)
    if notes:
      identity.append(f"{NOTE_SEPARATOR}{notes}")

    yield Label(identity, markup=False, classes="message-identity")
    yield Label(
      format_time_short(self.message["rx_time"]), markup=False, classes="message-time"
    )




class MessageItem(ListItem):
  """One message: who sent it, when, what it replies to, its text, its reactions.

  A message this device sent is rendered distinguished rather than filtered or
  left looking like everyone else's — a settled decision, and the reason is that
  a channel you have talked on reads wrong otherwise: your own messages are the
  ones you need to find when you are checking whether something went out.

  Whether a row is outbound is decided by the caller and passed in, not worked out
  here. The test is `from_node == meta.local_node_id`, which needs the archive's
  idea of which device this is — a question about the archive, not about a row.
  The tapbacks arrive the same way and for the same kind of reason: which
  messages were absorbed into this one is a question about the loaded window, not
  about a row.

  `orphan` is decided in the same place and on the same principle. Whether this
  reaction's parent is anywhere in the archive is a question about the archive,
  answered by `rebuild_rows` from the LEFT JOIN, and a row that has to ask it
  here would be deciding rather than rendering.
  """


  def __init__(
    self,
    message: dict[str, Any],
    outbound: bool = False,
    tapbacks: Optional[list[dict[str, Any]]] = None,
    unread: bool = False,
    orphan: bool = False,
  ) -> None:
    self.message = message
    self.outbound = outbound
    self.tapbacks = tapbacks or []
    self.orphan = orphan
    super().__init__()

    if outbound:
      # **Nothing in the stylesheet reads this any more** — the distinction moved
      # into the header, where it is the sender's own name in `$rx-outbound`. It
      # stays because it is the row saying what it is rather than how it looks, and
      # because `#messages.direct`'s unread rule and the suite both ask a row
      # whether it is one of yours.
      self.add_class("outbound")

    self.set_class(unread, "unread")




  def set_unread(self, unread: bool) -> None:
    """Mark this row as waiting to be read, or stop.

    Called as the read marker moves, so the accent bar on a direct message goes out
    the moment the cursor passes it rather than at the next rebuild of the list —
    which is what "it should go away once it has been opened" asks for, given that
    landing the cursor on a row is what opening one means here.
    """
    self.set_class(unread, "unread")




  def compose(self) -> ComposeResult:
    # **The reply bar comes first, above the row naming the sender.** RxOnly's
    # template has always had it there (`message_item.html`, the `<a>` before the
    # `<header>`), and the console had it between the header and the text — which
    # put what is being answered *after* the answer's byline and read as though the
    # quote belonged to the reply's author. Above, it is the thing this message
    # arrived on top of, which is what it is.
    if self.orphan:
      # In the reply bar's place, and it has to be here rather than left to
      # `format_reply_line`: that function already declines a message whose
      # parent is missing (it has nothing to excerpt), so an orphan reaction
      # would otherwise render as a bare `💪` from nobody, with no indication it
      # was aimed at anything at all.
      #
      # **Styled as the bar, which reverses what this comment used to say.** The
      # old reasoning was that the bar quotes a parent and there is no parent to
      # quote, so the fill would promise a message the archive does not have. That
      # is right about the excerpt and too strong about the band: what the band
      # actually says is "this message answers something", which is exactly the
      # fact an orphan reaction needs stated. RxOnly reached the same conclusion
      # and now draws it as `.message-reply-bar .message-reply-untracked`; these
      # are those two classes, and the leading asterisk is its asterisk.
      #
      # Nothing here has to un-promise a link the way the web side does — RxOnly
      # had to demote its `<a>` to a `<p>` and rescope a hover rule, while this
      # bar was never clickable in either state.
      yield Label(
        "* Reacting to an earlier message",
        markup=False,
        classes="message-reply-bar message-reply-untracked",
      )
    else:
      reply_line = format_reply_line(self.message)
      if reply_line is not None:
        yield Label(reply_line, markup=False, classes="message-reply-bar")

    yield MessageHeader(self.message, self.outbound)

    # The one-column step in from the header is `.message-text`'s own padding and
    # belongs to every message, not to replies alone — see the stylesheet, which is
    # also why this yields a plain class again rather than deciding one.
    yield Label(self.message.get("text") or "", markup=False, classes="message-text")

    # Under the text, because a reaction answers the message it sits below. The
    # emoji come off the mesh like everything else, so markup stays off here too.
    tapback_line = format_tapback_line(self.tapbacks)
    if tapback_line:
      yield Label(tapback_line, markup=False, classes="message-tapbacks")




class MenuFooter(Footer):
  """The footer, with ctrl+p put back in the corner the command palette had.

  Textual docks one key on the right of the footer, behind a divider rule, and
  fills it only when `ENABLE_COMMAND_PALETTE` is on. This app turns the palette
  off to free the chord for its own menu (see `MeshConsoleApp`), and the corner
  went empty with it — so the one chord in the interface was advertised nowhere
  and a reader had to already know it to find the log viewer or the theme
  switch. This puts the key back on the app's binding.

  The `-command-palette` class is Textual's own and is what carries the dock,
  the divider and the compact-mode padding; wearing it is why this reads as the
  same corner rather than a key that happens to be last. The binding stays
  `show=False` so the ordinary left-hand run does not list it twice.

  Only the app's screen uses this. The log viewer keeps a plain `Footer`,
  correctly: ctrl+p is shadowed inside a modal, so there is nothing to offer.
  """


  # The app's menu binding, by the key Textual files it under in the screen's
  # binding map. Deliberately not `App.COMMAND_PALETTE_BINDING` — that names the
  # palette this replaced, which is switched off, and reading it here would tie
  # this to a binding the app does not have.
  MENU_KEY = "ctrl+p"


  def compose(self) -> ComposeResult:
    yield from super().compose()

    # The same guard the base class opens with: before the first
    # `bindings_changed` there is no binding map to read and nothing is drawn.
    if not self._bindings_ready:
      return

    try:
      _node, binding, enabled, tooltip = self.screen.active_bindings[self.MENU_KEY]
    except KeyError:
      # Gated off in this view. The corner then stays empty for the same reason
      # any other key leaves the bar — `check_action` said it has nothing to do.
      return

    yield FooterKey(
      binding.key,
      self.app.get_key_display(binding),
      binding.description,
      binding.action,
      classes="-command-palette",
      disabled=not enabled,
      tooltip=tooltip or binding.description,
    )




class LogStream(Log):
  """The log viewer's pane: a `Log` that colours the timestamp and the level.

  A `Log` renders each line through `self.highlighter` if `highlight` is on, and
  that hook is the whole of this class's reason to exist. Everything the viewer
  does to its content — capping the scrollback, rewriting the pane when the level
  filter changes — happens on plain strings, and the colour is applied at render
  from whatever the stylesheet currently says. So a theme change recolours five
  thousand lines by clearing a cache, and the screen never touches a `Style`.

  The keys the highlighter looks up are the component classes below with their
  prefix removed: `log-stream--debug` supplies `debug`. See `mesh_console.tcss`
  for which palette colour each one takes and why.

  **The pane does not wrap and cannot be made to** — `Log.render_line` builds its
  `Text` with `no_wrap=True` and there is no setting either way — so the wrapping
  is the screen's, done to the width this reports. That makes the width a thing
  the screen has to be told about when it changes, which is what `Resized` is
  for: `Resize` itself does not bubble, so a screen watching for one hears about
  its own size and never about this widget's.
  """


  class Resized(Message):
    """The pane's usable width changed; anything wrapped to the old one is stale.

    Carries the width rather than leaving the handler to ask for it, so what gets
    re-wrapped is the width that prompted the re-wrap.
    """

    def __init__(self, width: int) -> None:
      self.width = width
      super().__init__()


  COMPONENT_CLASSES = {
    "log-stream--time",
    "log-stream--debug",
    "log-stream--info",
    "log-stream--warning",
    "log-stream--error",
    "log-stream--critical",
    "log-stream--tag",
    "log-stream--node",
  }

  PREFIX = "log-stream--"


  def __init__(self, **kwargs: Any) -> None:
    # `highlight=True` is what makes `Log.render_line` consult the highlighter at
    # all; without it the object below is built and never called.
    super().__init__(highlight=True, **kwargs)
    self.highlighter = LogHighlighter(self.component_style)


  def on_resize(self, event: events.Resize) -> None:
    # Every resize, including the first: the pane is written to before it has
    # been laid out — the viewer's own notice about a command that would not
    # start arrives that early — and at that point the width is 0 and nothing
    # can be wrapped to it. The first of these is what puts those lines right.
    self.post_message(self.Resized(self.content_size.width))


  def component_style(self, key: str) -> Optional[Style]:
    """The stylesheet's colour for one highlighter key, or None to leave it alone.

    **Resolved on each render rather than cached at a style change**, which is
    not the obvious way round and is the only one that is correct. The hook that
    looks like the place to cache — `notify_style_update` — runs from
    `Stylesheet.replace_rules`, and `_process_component_classes` runs *after* it:
    on the first application there is nothing to read yet, and on a theme change
    what is there is still the old theme's. So a cache filled there is empty at
    first and stale afterwards, which was a `KeyError` on mount and would have
    been the wrong colours after ctrl+p.

    Reading through costs a dict lookup per painted run on lines that are on
    screen, and `Log` caches the rendered strip anyway — clearing that cache is
    all `Log.notify_style_update` does, and it is what makes a theme change
    repaint with the colours below without this class doing anything.
    """
    name = self.PREFIX + key
    if name not in self.COMPONENT_CLASSES:
      return None
    try:
      return self.get_component_rich_style(name)
    except KeyError:
      # Asked for before the stylesheet has been applied to this widget. Not an
      # error — the line is drawn uncoloured and the next render has the styles.
      return None




class ScrollKeys(FooterKey):
  """The arrows as one footer entry: `↑/↓ Scroll`.

  They were four entries — `↑ Scroll up`, `↓ Scroll down` and the two the hidden
  horizontal scrollbar made necessary — which is four descriptions of one idea
  taking half the bar to say it. Jason's. Collapsing them changed what the footer
  said and not what the keys did.

  **Then the pane learned to wrap and there were two.** `←` and `→` existed to
  follow a line off the right edge; nothing runs off it now, so both the keys and
  their share of this entry are gone — see the note on `LogViewerScreen.BINDINGS`.
  What is left is still assembled here rather than left as two bindings, because
  two entries for one idea is the same waste at half the size.

  Textual has a `KeyGroup` for grouped bindings, and it is not this: it lays the
  keys out as separate widgets with margins between them, which cannot put a
  glyph in the gap. The separators are wanted, and they are wanted in a different
  colour from the arrows, so this renders the whole run itself.
  """


  COMPONENT_CLASSES = {
    "scroll-keys--arrow",
    "scroll-keys--separator",
  }

  ARROWS = ("↑", "↓")
  SEPARATOR = "/"


  def __init__(self, description: str = "Scroll") -> None:
    # `down` is what a click sends, being the one of the four a reader pressing a
    # scroll control almost always means. The key display is assembled in
    # `render` and this argument goes unused, so it is passed empty rather than
    # given a value that would be a lie if anything read it.
    super().__init__("down", "", description, "scroll_down")


  def render(self) -> Text:
    key_style = self.get_component_rich_style("footer-key--key")
    description_style = self.get_component_rich_style("footer-key--description")
    key_padding = self.get_component_styles("footer-key--key").padding
    description_padding = self.get_component_styles("footer-key--description").padding

    # Built onto the key style rather than replacing it, so the arrows and the
    # slashes keep the key background the rest of the bar's keys sit on and
    # change only their colour. A component style resolved on its own carries a
    # background of its own, which would punch two holes in the run.
    arrow_style = key_style + Style(
      color=self.get_component_rich_style("scroll-keys--arrow").color)
    separator_style = key_style + Style(
      color=self.get_component_rich_style("scroll-keys--separator").color)

    keys: list[tuple[str, Style]] = [(" " * key_padding.left, key_style)]
    for position, arrow in enumerate(self.ARROWS):
      if position:
        keys.append((self.SEPARATOR, separator_style))
      keys.append((arrow, arrow_style))
    keys.append((" " * key_padding.right, key_style))

    label_text = Text.assemble(
      *keys,
      (
        " " * description_padding.left
        + self.description
        + " " * description_padding.right,
        description_style,
      ),
    )
    label_text.stylize_before(self.rich_style)
    return label_text




class LevelKey(FooterKey):
  """`tab  Log level [DEBUG]`, with the level in the colour the log gives it.

  The chip is the point: a filter that is on has to be legible from the bar, and
  naming the level in the same colour the pane paints it makes the connection
  without a word of explanation. **The colour is not chosen here and not
  duplicated here** — `LogFooter` reads it off the `LogStream` itself, so the two
  cannot drift; there is one definition of what `[ERROR]` looks like and it is in
  the stylesheet.

  With no filter on, the chip reads `all` in the description's own colour, which
  is the honest shape for a word that is not a level and has no marker in the log.
  """


  def __init__(self, key_display: str, description: str, chip: str,
               chip_style: Optional[Style] = None) -> None:
    self.chip = chip
    self.chip_style = chip_style
    super().__init__("tab", key_display, description, "cycle_level")


  def render(self) -> Text:
    key_style = self.get_component_rich_style("footer-key--key")
    description_style = self.get_component_rich_style("footer-key--description")
    key_padding = self.get_component_styles("footer-key--key").padding
    description_padding = self.get_component_styles("footer-key--description").padding

    # As in `ScrollKeys`: the chip keeps the description's background and takes
    # only the level's colour, so it reads as part of the run rather than a
    # patch laid over it.
    chip_style = description_style
    if self.chip_style is not None and self.chip_style.color is not None:
      chip_style = description_style + Style(color=self.chip_style.color)

    label_text = Text.assemble(
      (
        " " * key_padding.left + self.key_display + " " * key_padding.right,
        key_style,
      ),
      (" " * description_padding.left + self.description + " ", description_style),
      (self.chip + " " * description_padding.right, chip_style),
    )
    label_text.stylize_before(self.rich_style)
    return label_text




class LogFooter(Footer):
  """The log viewer's bar: the ordinary keys, then the level chip and the arrows.

  Both of the entries this adds say something a `Binding` cannot. A binding
  carries one description string fixed at class definition, and these are a
  colour read from another widget and a run of four glyphs in two colours — so
  the bindings behind them are `show=False` and the bar is told about them here.

  Composed rather than rendered: `Footer` recomposes whenever the screen's
  bindings change, and `LogViewerScreen.repaint` refreshes them on every filter
  change, so the chip below is rebuilt with the new level for free.
  """


  # What the chip says when nothing is filtered out. Not `[all]`, because the
  # brackets in this bar mean "this exact marker, as the log prints it", and
  # there is no `[all]` line in any log.
  UNFILTERED = "all"


  def compose(self) -> ComposeResult:
    yield from super().compose()

    if not self._bindings_ready:
      return

    screen = self.screen

    # Offered on the same terms as the binding itself, whatever those are: read
    # from `active_bindings` rather than assumed, so a screen that gates the key
    # gets a bar that agrees with it. The log viewer binds `tab` unconditionally
    # — see `LogViewerScreen.BINDINGS` for why — so in practice the chip is
    # always there, reading `all` until something is filtered.
    if "tab" in screen.active_bindings:
      _node, binding, _enabled, _tooltip = screen.active_bindings["tab"]
      level = getattr(screen, "level", "")
      stream = self.screen.query_one("#log-stream", LogStream)
      # `data_bind`, as `Footer.compose` does for every key it yields itself:
      # `FooterKey.compact` defaults to True, and the class it sets takes the
      # padding off the key cap. Left unbound, these two would sit tight against
      # their labels while `esc` beside them kept its spacing.
      yield LevelKey(
        self.app.get_key_display(binding),
        binding.description,
        f"[{level}]" if level else self.UNFILTERED,
        stream.component_style(level.lower()) if level else None,
      ).data_bind(compact=Footer.compact)

    yield ScrollKeys().data_bind(compact=Footer.compact)
