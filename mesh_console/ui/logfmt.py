"""Reading one line of a log this project did not write.

The log viewer streams whatever `LOG_COMMAND` prints, so everything here is
pattern-matching against somebody else's output rather than parsing a format we
control. That sets the rule the whole module follows: **a line that does not
match is passed through untouched.** No line is ever dropped, reordered or
rewritten on a guess — the viewer's job is to show what the command said, and
the most this may do is restate a timestamp and colour two runs of it.

Three things are recognised, in this order:

1. **A leading ISO-8601 instant**, which is turned into the local time in the
   form the rest of the console shows times in. See `localise_stamp` for why an
   explicit offset is required.
2. **A level marker** — `[DEBUG]`, `[ERROR]` — which is what the collector's own
   `LOG_FORMAT` puts at the front of every line it logs, and what the viewer
   filters on.
3. **Any other bracketed tag** in that position, which is not a level and is not
   treated as one. `[testbed]` is the case that exists: the testbed prefixes its
   own lines into the same file the collector's output goes to.

The same marker does one more job at the end of this module: it is where a
wrapped line resumes. See `wrap`.
"""

from __future__ import annotations

import re

from datetime import datetime
from typing import Callable, NamedTuple, Optional

from rich.cells import cell_len
from rich.highlighter import Highlighter
from rich.style import Style
from rich.text import Text


# What `LogHighlighter` asks for a colour: a key — `time`, `debug`, `tag` — and
# back either a style or None for "leave this run alone".
Resolver = Callable[[str], Optional[Style]]


# **The five levels are Python's, and that is the whole list.** `logging` defines
# DEBUG, INFO, WARNING, ERROR and CRITICAL and nothing else, the collector logs
# through `logging` with `[%(levelname)s]` as its whole prefix
# (mesh_collector/collector/__init__.py), and so those five names are exactly what
# can appear in that position. There is no `[LOG]` — Jason asked, and there is
# not: it is not a level in `logging`, journald's priorities do not carry it
# either, and a filter offering one would be a filter for a thing that never
# arrives.
#
# In severity order, which is the order the filter cycles in.
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# The two names `logging` keeps as synonyms — `logging.WARN` and `logging.FATAL`
# are the same integers as WARNING and CRITICAL. Nothing in this project logs
# under them; they are here because a log command can be pointed at any process
# and some of them do.
LEVEL_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL"}

# What `level` is when a line carries no level marker: the testbed's own
# `[testbed]` lines, journald's boot banners, a traceback's continuation lines,
# and the viewer's own notices. Not a level, and deliberately not sorted among
# them.
NO_LEVEL = ""


# The console's own time format, one field shorter than `format_timestamp`'s.
# That function's `8/7/2026, 2:16:16 PM` is the shape this borrows — same
# separators, same 12-hour clock, same `%-` widths, which is what makes a log
# line read as part of this interface. The year comes off because it repeats on
# every one of five thousand lines and the date beside it already anchors them,
# and the milliseconds come off with it: the file keeps them, and within one
# second the order the lines arrived in *is* the order they are shown in.
LOG_TIME_FORMAT = "%-m/%-d %-I:%M:%S %p"

# An ISO-8601 instant at the front of a line, **with its offset**, which is the
# part that matters — see `localise_stamp`.
ISO_STAMP = re.compile(
  r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})"
)

# `LOG_TIME_FORMAT`'s output, for the highlighter to find again. **These two are
# one shape written twice**, and they live next to each other so they stay that
# way: `localise_stamp` writes the stamp and the highlighter has only the
# finished string to work from, because a `Log` stores plain text and styles it
# at render. Change one and this stops colouring.
LOCAL_STAMP = re.compile(r"^\d{1,2}/\d{1,2} \d{1,2}:\d{2}:\d{2} [AP]M")

# A bracketed tag standing on its own, which is what a level marker looks like.
# **The bracket has to start a word**, and that is what keeps journald's
# `mesh-collector[1234]:` out of it — the `[` there follows a letter, so it is
# part of a name rather than a marker, and the `[DEBUG]` further along the line
# is the first thing this matches. No whitespace inside, which is what keeps
# `[Errno 2]` out of it.
TAG = re.compile(r"(?:^|(?<=\s))\[([^\[\]\s]+)\]")

# A node id as Meshtastic writes one: `!` and eight hex digits, which is the node
# number in hex and is how every id in this archive is spelled — `!eeb826a4`. The
# boundaries are what keep it honest in a log full of other hex: nothing
# alphanumeric on either side, so a nine-digit run is not an eight-digit id with a
# character after it, and `!` is required so a bare `eeb826a4` is not claimed.
#
# **Anywhere on the line, not in a fixed position.** Unlike the two runs above,
# this is a thing the *message* says rather than a field in front of it, and the
# collector says it in several shapes — `from !eeb826a4`, `id: "!eeb826a4"`,
# `'id': '!1124fca4'`. Matching the id itself rather than any of those contexts is
# what catches all three and whatever the library prints next.
NODE_ID = re.compile(r"(?<![0-9A-Za-z])!([0-9a-fA-F]{8})(?![0-9A-Za-z])")




class LogLine(NamedTuple):
  """One line as the viewer holds it: what to print, and what it is.

  `text` is the line after `localise_stamp`, so it is what goes on screen and
  what any search of the scrollback would be searching. `level` is one of
  `LEVELS` or `NO_LEVEL`, and is what the filter matches on.

  `notice` marks the viewer's own lines — the two it writes itself when the
  command cannot start or has stopped. **A notice is shown under every filter.**
  It carries no level, so a level filter would otherwise hide the one line that
  explains why nothing else is arriving, which is the opposite of what a filter
  is for.
  """

  text: str
  level: str = NO_LEVEL
  notice: bool = False




def localise_stamp(line: str) -> str:
  """Restate a leading ISO-8601 instant as a local time, or return the line as it came.

  **This is the whole of the console's timezone policy, applied to the one thing
  that was not already following it.** The archive stores instants as Unix epoch
  integers, which name a moment and no timezone at all, and both readers turn
  them into the reader's own local time when they print them — `format_timestamp`
  here, `toLocaleString` in RxOnly's `rxonly.js`. A log line arriving in UTC was
  the one timestamp on screen not in the zone the reader lives in.

  **An explicit offset is required, and that is the point rather than an
  oversight.** `2026-08-07T18:16:16.293Z` and `...-0500` each name a real
  instant, so converting them is arithmetic. A stamp with no offset names a wall
  clock and nothing else — converting it would mean assuming a zone it did not
  state, and assuming UTC would shift a log that was already local by the size of
  the reader's own offset. Those lines are left exactly as they arrived, which is
  the honest answer and is also the right one in practice: journald's default
  format prints local time already.
  """
  match = ISO_STAMP.match(line)
  if match is None:
    return line

  try:
    moment = datetime.fromisoformat(match.group())
  except ValueError:
    # A shape this recognised and `fromisoformat` did not. Nothing is lost by
    # leaving it alone, which is what every unmatched line gets anyway.
    return line

  return moment.astimezone().strftime(LOG_TIME_FORMAT) + line[match.end():]




def level_of(line: str) -> str:
  """The line's level, or `NO_LEVEL` — the first standalone bracketed tag, if it names one.

  The first, because the collector's format puts the level at the front and the
  message follows it: a message that itself contains `[INFO]` is a message about
  a log line, not one.
  """
  match = TAG.search(line)
  if match is None:
    return NO_LEVEL

  name = match.group(1).upper()
  name = LEVEL_ALIASES.get(name, name)
  return name if name in LEVELS else NO_LEVEL




def tagged(line: str) -> bool:
  """Whether the line carries a bracketed marker of its own, level or not."""
  return TAG.search(line) is not None




def read_line(line: str, previous: str = NO_LEVEL) -> LogLine:
  """A raw line from the command, ready for the viewer to hold and print.

  **A line with no marker at all continues the one before it**, and takes its
  level. This is not a nicety: the collector logs a protobuf dump as one record
  across a dozen lines, and only the first of them carries `[DEBUG]` — the rest
  are the braces and the fields. Classified on their own they have no level, so
  filtering to `[DEBUG]` kept the line that opens the record and dropped its
  entire body, which is a filter that hides the thing you filtered for.

  `previous` is the level of the line before this one, which the caller has and
  this does not. `NO_LEVEL` inherits nothing, so a run of unmarked lines at the
  top of a stream stays unmarked.

  **A line with a marker that is not a level does not inherit**, which is what
  separates a continuation from a line of somebody else's. `[testbed] exec ...`
  states what it is; `   free: 16` states nothing and is therefore part of
  whatever stated last.
  """
  text = localise_stamp(line)
  level = level_of(text)
  if level == NO_LEVEL and not tagged(text):
    level = previous
  return LogLine(text, level)




def notice(text: str) -> LogLine:
  """One of the viewer's own lines, shown whatever the filter says."""
  return LogLine(text, NO_LEVEL, notice=True)




def message_indent(line: str) -> int:
  """The column the message starts in — past the timestamp and past the marker.

  **The first standalone bracketed tag, which is the same rule `level_of` uses**,
  and deliberately so: whatever that function decided the line's marker was is
  what a wrapped continuation of the line should resume after. A line with no
  marker — the viewer's own notices, journald's boot banners, a raw protobuf
  dump's fields — has no message column either, and gets 0.

  Any tag, not only a level. `[testbed]` occupies the same run in the same
  position, and a continuation of one of those lines wants the same column as a
  continuation of the collector's.
  """
  match = TAG.search(line)
  if match is None:
    return 0

  # Past the whitespace after it as well, since that is where the message
  # actually begins. `\s+` and not a single space: nothing here controls how the
  # producing format spaced its own prefix.
  spacing = re.compile(r"\s*").match(line, match.end())
  return spacing.end() if spacing else match.end()




# Whitespace runs kept rather than discarded, so a line can be reassembled from
# its pieces exactly as it arrived when it turns out not to need breaking.
_CHUNKS = re.compile(r"(\s+)")

# The narrowest message column worth aligning to. Below this the indent is
# dropped and continuations start at the margin — a message wrapping in a gutter
# is harder to read than one that is not aligned at all.
#
# **This is a number because journald has a long prefix.** Under the testbed the
# prefix is a stamp and a level, twenty-two columns, and any rule at all keeps
# the indent. Under `journalctl` the unit and pid come too — `8/7 2:52:29 PM
# mesh-collector[1234]: [DEBUG] ` is forty-five — so the rule decides the Pi's
# behaviour rather than being a formality. Asking whether the indent leaves a
# readable column is the question that actually matters; the rule here was once
# "less than half the pane", which threw the alignment away on an eighty-column
# terminal with thirty-five perfectly good columns left to wrap in.
_MIN_MESSAGE_ROOM = 32




def wrap(line: str, width: int) -> list[str]:
  """One line as the lines it occupies at `width`, aligned under its own message.

  **Wrapping is the console's, not the log's.** Textual's `Log` renders every
  line with `no_wrap=True` and has no setting that changes it, so a line longer
  than the pane is clipped at the right edge and reachable only by scrolling
  sideways. What arrives here is one logical line and what goes back is the run
  of physical lines it becomes; the viewer holds the logical ones and the pane
  holds these, which is what lets a resize re-wrap without re-reading anything.

  **The continuation lines are indented to the message column** — under the `C`
  of `Channel config`, not under the timestamp — so the marker column stays a
  column. A wrapped line otherwise restarts at the left margin, where the eye
  reads it as a new record with a missing timestamp, which is the opposite of
  what it is. See `message_indent` for where that column is.

  Measured in cells rather than characters, because a mesh's node names are not
  ASCII: `long_name: "🦊1"` is two cells for one character, and a wrap counted in
  characters would run those lines a cell or two past the edge it was called to
  stay inside.

  Words longer than a line are broken rather than allowed to overhang, which on
  this log means a base64 public key. Nothing else here is that long.
  """
  if width <= 0 or cell_len(line) <= width:
    # The overwhelmingly common case, and it returns the string it was handed:
    # a line that fits is not rewritten, re-spaced, or stripped.
    return [line]

  indent = message_indent(line)
  # Dropped when it would leave the message too narrow a column to wrap in. See
  # `_MIN_MESSAGE_ROOM` — under journald's longer prefix this is the rule that
  # decides whether the alignment survives an eighty-column terminal.
  if width - indent < _MIN_MESSAGE_ROOM:
    indent = 0

  pad = " " * indent
  lines: list[str] = []
  current = ""

  def limit() -> int:
    """The room on the line being built: full width first, less the indent after."""
    return width if not lines else max(width - indent, 1)

  def flush() -> None:
    nonlocal current
    # Trailing whitespace goes at the break, as any wrap does — it is the join
    # between two words, and this is where they stopped being joined.
    lines.append(("" if not lines else pad) + current.rstrip())
    current = ""

  for chunk in filter(None, _CHUNKS.split(line)):
    if chunk.isspace():
      # Leading whitespace on a fresh line is a break's leftovers, not content.
      if current:
        current += chunk
      continue

    while cell_len(chunk) > limit() - cell_len(current):
      if current.strip():
        # Try the whole word again on a line of its own.
        flush()
        continue
      head, chunk = _cut(chunk, limit())
      current = head
      flush()

    current += chunk

  if current.strip():
    flush()

  return lines or [line]




def _cut(word: str, cells: int) -> tuple[str, str]:
  """A word too long for one line, split at `cells` — as much as fits, and the rest.

  At least one character always moves, even where that one character is wider
  than the room left for it: the alternative is a loop that asks the same
  question forever.
  """
  taken = 0
  for position, character in enumerate(word):
    step = cell_len(character)
    if taken + step > cells:
      cut = position or 1
      return word[:cut], word[cut:]
    taken += step

  return word, ""




class LogHighlighter(Highlighter):
  """Colours the timestamp, the level marker and any node id, and leaves the rest alone.

  A `Log` keeps its content as plain strings and calls this on each line as it
  renders it, which is why this is a highlighter rather than something that built
  a `Text` up front: styling here survives the scrollback cap, the filter
  rewriting the pane, and a theme change, none of which have to know about
  colour at all.

  The colours are not named here and are not held here either: this is given a
  function and asks it for a style by key at the moment it paints one.
  `LogStream` answers out of its component classes, so the colour of a level is
  whatever `mesh_console.tcss` currently says it is — which is what makes a theme
  switch recolour the scrollback without anything here being told. A key the
  resolver has no answer for is simply not painted, so a partial palette, or one
  asked for before the stylesheet has been applied, is safe.
  """


  def __init__(self, resolve: Optional[Resolver] = None) -> None:
    self.resolve: Resolver = resolve or (lambda key: None)


  def highlight(self, text: Text) -> None:
    plain = text.plain

    # **A line that starts indented carries no marker of its own.** It is the
    # continuation of the one above — either `wrap` put it there, or the producer
    # did, which is what a protobuf dump's fields are with TIDY_LOGS off. Either
    # way the brackets a message happens to contain are message: without this,
    # `Config-tracked channel indexes: [0]` wrapping onto a second line would
    # paint that `[0]` in the marker colour, in the marker's column, on a line
    # whose actual marker is a row above.
    #
    # The node ids below are not skipped with it, and that is the point of doing
    # them separately: a wrapped `Received nodeinfo:` dump carries its ids on the
    # continuation rows more often than on the first.
    if not plain[:1].isspace():
      self._mark(text, plain)

    for node in NODE_ID.finditer(plain):
      self._paint(text, "node", node.start(), node.end())


  def _mark(self, text: Text, plain: str) -> None:
    """The two runs in front of the message: when it happened, and what kind it is."""
    stamp = LOCAL_STAMP.match(plain)
    if stamp is not None:
      self._paint(text, "time", stamp.start(), stamp.end())

    # From the end of the stamp, so a date could never be mistaken for a tag.
    tag = TAG.search(plain, stamp.end() if stamp else 0)
    if tag is None:
      return

    name = tag.group(1).upper()
    name = LEVEL_ALIASES.get(name, name)
    # A tag that is not a level still gets a colour of its own — `[testbed]` is
    # the one that exists — because it is a marker in the same position doing the
    # same job, and leaving it the colour of the message would read as the
    # message starting with a bracket.
    self._paint(text, name.lower() if name in LEVELS else "tag", tag.start(), tag.end())


  def _paint(self, text: Text, key: str, start: int, end: int) -> None:
    style = self.resolve(key)
    if style is not None:
      text.stylize(style, start, end)
