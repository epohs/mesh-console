"""The console's two modal screens: the ctrl+p menu, and the raw log viewer.

Both are pushed onto the screen stack over the one screen the app otherwise
lives on, and both are dumb on purpose: the menu is handed its entries and
reports which one was chosen, and the viewer is handed a command line and
streams whatever it prints. Neither reads the app's state, so either can be
mounted and driven on its own — the same rule `MessageList.start_row` follows.
"""

from __future__ import annotations

import asyncio

from typing import Iterable, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView

from mesh_console.ui.logfmt import LEVELS, NO_LEVEL, LogLine, notice, read_line, wrap
from mesh_console.ui.widgets import LogFooter, LogStream


class MenuItem(ListItem):
  """One menu row, carrying the key its selection reports."""


  def __init__(self, key: str, label: str) -> None:
    self.key = key
    super().__init__(Label(label, markup=False))




class MenuScreen(ModalScreen[Optional[str]]):
  """What ctrl+p opens: a short list of commands, not a search box.

  This replaced Textual's command palette, which is a fuzzy search over
  everything the app can do — the right shape for an editor with hundreds of
  commands and the wrong one for a console with four. A reader should see what
  the commands *are*, not be asked to guess at one.

  Dismisses with the chosen entry's key, or None for escape. The entries arrive
  from the caller because their labels are the caller's state — "Switch to
  light theme" depends on which theme is up, and this screen has no business
  knowing.
  """


  BINDINGS = [
    Binding("escape", "close", "Close", show=False),
  ]


  def __init__(self, entries: Iterable[tuple[str, str]]) -> None:
    self.entries = tuple(entries)
    super().__init__()


  def compose(self) -> ComposeResult:
    with Vertical(id="menu"):
      yield ListView(
        *(MenuItem(key, label) for key, label in self.entries),
        id="menu-list",
      )


  def on_mount(self) -> None:
    listing = self.query_one("#menu-list", ListView)
    listing.focus()
    # A menu opens pointing at something, unlike the message list: every row
    # here is a command, and enter with no selection would mean nothing.
    listing.index = 0


  def on_list_view_selected(self, event: ListView.Selected) -> None:
    event.stop()
    if isinstance(event.item, MenuItem):
      self.dismiss(event.item.key)


  def action_close(self) -> None:
    self.dismiss(None)




class LogViewerScreen(ModalScreen[None]):
  """The collector's raw output, streamed live from a command on this host.

  The console cannot see the collector's log through the archive — the archive
  holds messages, and the log lives wherever the collector's stdout went. So
  this runs a command (`LOG_COMMAND`, journalctl against the shipped unit by
  default) and shows its merged stdout and stderr as it arrives. When the
  command cannot run or exits, what the shell said and how it ended are written
  into the stream itself, journald-style, because an empty pane that knows why
  it is empty should say so.

  **Following is a fact about where the reader is, not a mode.** A line arriving
  while the viewport sits at the end pushes the log up smoothly; one arriving
  while the reader has scrolled back lands below without moving anything —
  Textual's own `auto_scroll` yanks to the bottom on every write wherever you
  are, which is exactly the jump this refuses, so each write decides for itself
  from `is_vertical_scroll_end`. The scrollback cap works the same way: pruning
  drops lines off the *top*, which would slide history under a reader mid-way
  through it, so the cap is enforced only while the viewport is at the end and
  catches up when it returns there.

  **The screen holds the lines, and the pane shows the ones the filter allows.**
  It did not used to — the `Log` was handed each line as it arrived and was the
  only copy of the scrollback there was — and the level filter is what changed
  that: switching to `[ERROR]` has to be able to reach back over lines already
  scrolled past, which means something has to still have them. So `lines` is the
  scrollback and the pane is a rendering of it, rebuilt on every filter change.
  The cap moved with it and kept its rule.

  **Wrapping made that separation load-bearing rather than merely tidy.** One
  line in `lines` is one thing the collector logged; in the pane it is however
  many rows that takes at the current width, indented under its own message so
  the timestamp and level keep a column of their own. Width is not a property of
  a log line, so the wrap cannot be done once on the way in — it is done in
  `fill`, from the scrollback, every time the width changes. That is what makes
  a terminal resize re-flow four thousand lines of history correctly instead of
  leaving them broken where the old width happened to break them.
  """


  BINDINGS = [
    Binding("escape", "close", "Close"),
    # Cycle the level filter: everything, then each level that has actually
    # turned up in this stream, then back. Offered only once there is more than
    # one thing to cycle between — see `check_action` and `levels_present`.
    #
    # `show=False` and it is still in the bar: `LogFooter` puts it there as
    # `tab Log level [DEBUG]`, with the level in the colour the pane paints it.
    # A `Binding` carries one description fixed at class definition and this one
    # has to say which level is showing, so the footer builds the entry instead.
    #
    # **Always bound, and `priority` so it stays that way.** It was gated on
    # there being two levels to cycle between, on the reasoning that a key with
    # nothing to do should not be offered — which was wrong twice. It is wrong
    # for the reader, because on a quiet collector every line in the buffer is
    # DEBUG and the filter Jason asked for was invisible exactly when he went
    # looking for it. And it is wrong mechanically: `check_action` returning
    # False takes the binding out of `active_bindings` altogether, so `tab`
    # stopped being this screen's key and fell through to whatever else claims
    # it — `Screen`'s own `app.focus_next`, and past that whatever the terminal
    # does with a key nothing has bound. A modal with nothing focusable in it has
    # no business handing `tab` to a focus command. Bound unconditionally, there
    # is no fall-through to reason about.
    Binding("tab", "cycle_level", "Log level", show=False, priority=True),
    # Advertised whenever the reader is not at the end — the same conditional
    # offer as the message list's `g`, under the same key. See `check_action`.
    Binding("g", "jump_end", "Jump to newest"),
    # **Both hidden, and both in the bar as one entry.** `LogFooter` renders them
    # as `↑/↓ Scroll`, which says the same thing in a fraction of the room — two
    # descriptions of one idea was more bar than the idea is worth.
    #
    # **There were four.** Left and right were the answer to a long DEBUG line
    # running off the right edge, back when nothing wrapped and the horizontal
    # scrollbar was hidden — the comment in `mesh_console.tcss` that gives up both
    # scrollbars is where that trade was made. `fill` wraps every line to the pane
    # now, so there is nothing to the right of the pane to reach and the two keys
    # were a bar entry offering to do nothing. Removed with the entry rather than
    # left bound and silent, which would have been a key that does nothing and no
    # way to find that out. Jason's, 2026-08-07.
    Binding("up", "scroll_up", "Scroll up", show=False),
    Binding("down", "scroll_down", "Scroll down", show=False),
    Binding("pageup", "page_up", "Page up", show=False),
    Binding("pagedown", "page_down", "Page down", show=False),
    Binding("home", "scroll_home", "Oldest", show=False),
    Binding("end", "jump_end", "Newest", show=False),
  ]

  # Lines kept once the reader is at the end. Roughly an hour of a DEBUG-level
  # collector; a reading session that scrolls back holds more until it returns.
  SCROLLBACK_CAP = 5000


  # What the filter is showing everything, spelled the way the subtitle says it.
  ALL_LEVELS = ""


  def __init__(self, command: str) -> None:
    self.command = command
    self._process: Optional[asyncio.subprocess.Process] = None
    # The scrollback, filter or no filter. The pane holds a subset of this.
    self.lines: list[LogLine] = []
    # Which level is being shown, or `ALL_LEVELS`.
    self.level = self.ALL_LEVELS
    # Every level this stream has produced. See `levels_present`.
    self.seen_levels: set[str] = set()
    super().__init__()


  def compose(self) -> ComposeResult:
    with Vertical(id="log-frame"):
      stream = LogStream(
        id="log-stream", auto_scroll=False, max_lines=self.SCROLLBACK_CAP)
      # The screen keeps the keyboard, or the status bar goes quiet: a focused
      # Log answers the arrow keys through bindings it inherits with
      # `show=False`, and the footer advertises the focused widget's binding for
      # a key, not this screen's. Decided before mounting because that is when
      # the screen auto-focuses whatever will take it — by `on_mount` the Log
      # already has the keyboard and refusing focus no longer returns it. With
      # nothing here focusable, the bindings above are the ones in force and the
      # bar reads `esc` and the arrows, as asked. The wheel is unaffected — it
      # goes to the widget under the pointer, focus or no focus.
      stream.can_focus = False
      yield stream
      yield LogFooter()


  def on_mount(self) -> None:
    frame = self.query_one("#log-frame", Vertical)
    # The frame says what is being watched, because the stream itself may never
    # get the chance to — a command that cannot start prints one line and stops.
    frame.border_title = self.command
    self.show_level()

    self.watch(
      self.query_one("#log-stream", LogStream), "scroll_y", self.stream_scrolled,
      init=False,
    )
    self.run_worker(self.follow(), exclusive=True)


  def on_unmount(self) -> None:
    # The worker is cancelled with the screen; the subprocess is not the
    # worker's child to take down, so it is killed here — journalctl -f would
    # otherwise follow forever with nobody reading.
    if self._process is not None and self._process.returncode is None:
      try:
        self._process.kill()
      except ProcessLookupError:
        pass


  async def follow(self) -> None:
    """Run the command and stream every line it prints, then say how it ended."""
    try:
      self._process = await asyncio.create_subprocess_shell(
        self.command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
      )
    except OSError as error:
      self.append(notice(f"-- could not run: {error} --"))
      return

    assert self._process.stdout is not None
    while True:
      line = await self._process.stdout.readline()
      if not line:
        break
      self.append_line(line.decode(errors="replace").rstrip("\r\n"))

    status = await self._process.wait()
    # The journald idiom for lines that are about the stream rather than of it.
    # A shell that could not find the command has already printed its own
    # complaint above; this says the following has stopped either way.
    self.append(
      notice(f"-- log command exited with status {status}; not following --"))


  def append_line(self, line: str) -> None:
    """One raw line onto the end, as if the command had printed it.

    Kept as the way in for anything holding a string rather than a `LogLine`,
    which is the stream worker's caller and the suite's. The level of the line
    before is passed along so an unmarked line can continue it — see `read_line`.
    """
    self.append(read_line(line, self.lines[-1].level if self.lines else NO_LEVEL))


  def append(self, entry: LogLine) -> None:
    """One line onto the end, following only if the reader is already there."""
    stream = self.query_one("#log-stream", LogStream)
    following = stream.is_vertical_scroll_end

    self.lines.append(entry)

    if entry.level != NO_LEVEL and entry.level not in self.seen_levels:
      self.seen_levels.add(entry.level)
      # A new stop in the cycle, and the second one is what makes `tab` worth
      # advertising at all. Nothing else would ask the footer to look again — the
      # binding's answer depends on what has arrived, not on where the reader is.
      self.refresh_bindings()

    # The cap prunes off the top, which under a reader scrolled back would slide
    # the history they are reading. Lifted while they are away from the end,
    # restored when they return — `stream_scrolled` puts it back and the next
    # write prunes the excess. The scrollback and the pane are capped separately
    # and at the same number, which since the pane started wrapping is the same
    # number counting two different things: `self.lines` holds logical lines and
    # the pane holds the physical ones they wrap into, so at the cap the pane
    # shows the newest few thousand *rows* of a scrollback of five thousand
    # *lines*. That keeps the relationship the cap was there to keep — the pane
    # is a subset of the scrollback, never the other way round — and the lines it
    # falls short by are still there for a filter change or a resize to find.
    if following and len(self.lines) > self.SCROLLBACK_CAP:
      del self.lines[:len(self.lines) - self.SCROLLBACK_CAP]
    stream.max_lines = self.SCROLLBACK_CAP if following else None

    if not self.shows(entry):
      # Filtered out. It is in the scrollback and a filter change will find it,
      # but nothing arrived on screen, so nothing below the viewport is new.
      return

    stream.write_lines(
      wrap(entry.text, stream.content_size.width), scroll_end=following)


  def shows(self, entry: LogLine) -> bool:
    """Whether the current filter lets this line onto the screen."""
    return (
      entry.notice
      or self.level == self.ALL_LEVELS
      or entry.level == self.level
    )


  def stream_scrolled(self, scroll_y: float) -> None:
    """Re-offer or retire the jump as the reader moves; restore the cap at the end."""
    stream = self.query_one("#log-stream", LogStream)
    if stream.is_vertical_scroll_end:
      stream.max_lines = self.SCROLLBACK_CAP
    self.refresh_bindings()


  def levels_present(self) -> list[str]:
    """The levels this stream has produced, in severity order.

    **What the filter cycles through is what has turned up, not what could.**
    A stop for `[CRITICAL]` on a collector that has never logged one is a stop
    whose whole result is an empty pane. A level that turns up later joins the
    cycle then. This is about the *stops*, not about whether `tab` is offered —
    the key is always bound, and on a stream of nothing but DEBUG the cycle is
    two stops that happen to show the same lines, which is a filter honestly
    reporting that there is nothing to filter out.

    Read from `seen_levels`, which only ever grows, rather than from the
    scrollback itself. Recounting five thousand lines on every arriving line is
    the obvious cost; the subtler reason is that a shrinking cycle would move
    under a reader's thumb — a level whose last line aged out would silently drop
    a stop, and the next tab would land somewhere other than where the one before
    it did. The price is that such a level keeps a stop that now shows nothing,
    which reads as "none of those left in the scrollback" and is true.
    """
    return [level for level in LEVELS if level in self.seen_levels]


  def action_cycle_level(self) -> None:
    """tab. Everything, then each level present, then everything again."""
    cycle = [self.ALL_LEVELS] + self.levels_present()
    try:
      position = cycle.index(self.level)
    except ValueError:
      # The level being shown has aged out of the scrollback entirely. Back to
      # everything, which is the one stop that is always in the cycle.
      position = -1

    self.level = cycle[(position + 1) % len(cycle)]
    self.repaint()


  def fill(self, width: int, scroll_end: bool) -> None:
    """Rebuild the pane from the scrollback: every line the filter allows, wrapped.

    The one place the pane is rewritten from scratch. Both callers below reach
    the same state and differ only in where they leave the reader, which is the
    one thing the two of them genuinely disagree about.
    """
    stream = self.query_one("#log-stream", LogStream)
    rows: list[str] = []
    for entry in self.lines:
      if self.shows(entry):
        rows.extend(wrap(entry.text, width))

    stream.clear()
    stream.write_lines(rows)
    if scroll_end:
      stream.scroll_end(animate=False, immediate=True)


  def repaint(self) -> None:
    """Rebuild the pane under a filter that has just changed.

    Lands at the end, deliberately: a filter change is a question about what is
    in the log, and the newest matching line is the answer to it. Holding the
    old scroll position would be meaningless anyway — the pane it referred to no
    longer exists.
    """
    stream = self.query_one("#log-stream", LogStream)
    self.fill(stream.content_size.width, scroll_end=True)

    self.show_level()
    self.refresh_bindings()


  def on_log_stream_resized(self, event: LogStream.Resized) -> None:
    """Re-wrap the pane to its new width, without moving the reader off what they were reading.

    **Not `repaint`, and the difference is the scroll.** A filter change is
    something the reader asked the log a question with, so it answers at the
    newest line. A resize is not a question — the terminal changed shape, or the
    pane was laid out for the first time — and yanking a reader who is halfway up
    a stack trace to the bottom because they widened their window would be a
    surprise nothing asked for. Following is preserved as a fact about where they
    were, the same rule `append` follows.

    Re-wrapping five thousand lines on every resize event is the cost, paid while
    a window is being dragged. It is a string operation per line with no rendering
    behind it — Textual redraws the visible strip either way — and a resize is
    already a full relayout, so this is not what makes one expensive.
    """
    stream = self.query_one("#log-stream", LogStream)
    self.fill(event.width, scroll_end=stream.is_vertical_scroll_end)


  def show_level(self) -> None:
    """Say which level is showing, under the command that is producing it.

    In the frame's subtitle rather than a line of its own: it is a fact about the
    pane, the way the border title is a fact about where the pane's contents come
    from, and a filter that is on has to be visible or an empty pane reads as a
    silent collector.
    """
    frame = self.query_one("#log-frame", Vertical)
    frame.border_subtitle = (
      "" if self.level == self.ALL_LEVELS else f" {self.level} only "
    )


  def check_action(self, action: str, parameters: tuple[object, ...]) -> Optional[bool]:
    """`g` is offered only when it would show something new. See the app's own
    `check_action` for why False rather than None is what hides a binding."""
    if action == "jump_end":
      # **Not at the end is the whole condition.** It also required a line to
      # have *arrived* while the reader was scrolled up, which read as the same
      # rule and is not: scroll back through two hundred lines with nothing new
      # coming and there are a hundred and eighty below the viewport, no way to
      # the end offered, and nothing to say why. On this collector the next line
      # can be five minutes away. The message list's `can_jump_newest` — which
      # this claimed to match — asks only whether the cursor is off the last row,
      # and this now asks the same question of the viewport.
      stream = self.query_one("#log-stream", LogStream)
      return True if not stream.is_vertical_scroll_end else False

    return True


  def action_close(self) -> None:
    self.dismiss(None)


  def action_jump_end(self) -> None:
    self.query_one("#log-stream", LogStream).scroll_end(animate=False, immediate=True)


  def action_scroll_up(self) -> None:
    self.query_one("#log-stream", LogStream).scroll_up(animate=False)


  def action_scroll_down(self) -> None:
    self.query_one("#log-stream", LogStream).scroll_down(animate=False)


  def action_page_up(self) -> None:
    self.query_one("#log-stream", LogStream).scroll_page_up(animate=False)


  def action_page_down(self) -> None:
    self.query_one("#log-stream", LogStream).scroll_page_down(animate=False)


  def action_scroll_home(self) -> None:
    self.query_one("#log-stream", LogStream).scroll_home(animate=False)
