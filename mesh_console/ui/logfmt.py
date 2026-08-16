"""Reading one line of a log this project did not write.

The log viewer streams whatever `LOG_COMMAND` prints, so everything here is
pattern-matching against somebody else's output rather than parsing a format we
control. That sets the rule the whole module follows: **a line that does not
match is passed through untouched.** No line is ever dropped, reordered or
rewritten on a guess — the viewer's job is to show what the command said, and
the most this may do is restate a timestamp, drop the transport's own framing
from in front of the message, pad the level marker out to a fixed column, and
colour what is left. Nothing is ever truncated to make a column fit.

Four things are recognised, in this order:

1. **A leading ISO-8601 instant**, which is turned into the local time in the
   form the rest of the console shows times in. See `localise_stamp` for why an
   explicit offset is required.
2. **The writer's name and pid** — `python[2560]: ` — which is journald's
   framing rather than anything the collector wrote, and is removed. See
   `strip_source`.
3. **A level marker** — `[DEBUG]`, `[ERROR]` — which is what the collector's own
   `LOG_FORMAT` puts at the front of every line it logs, and what the viewer
   filters on.
4. **Any other bracketed tag** in that position, which is not a level and is not
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

# journald's own framing between the stamp and the message: the syslog identifier
# of whatever wrote the line, and its pid — `python[2560]: `. The identifier is
# the *interpreter's* name here, because that is what systemd sees exec'd, so it
# does not even name the collector; and the pid is the same number on every line
# until the service restarts. Neither is information about the record it sits in
# front of, and together they cost twenty-two columns of the message's own room.
# Jason's, 2026-08-10.
#
# **The pid is required, and that is what makes this safe to remove.** An
# identifier alone is indistinguishable from a word the message opens with — a
# line reading `Error: no such device` would lose `Error: ` to a pattern that did
# not insist on the brackets. With them there is no such line: `[0-9]+` in
# brackets immediately before a colon is journald's shape and nothing else's. A
# line journald wrote without a pid (`kernel:`) keeps it, which is the same
# answer this module gives everywhere it is not certain.
#
# No `^`: this is matched from the end of the stamp rather than the start of the
# line, so the position is established by something already recognised instead of
# being guessed at. See `strip_source`.
#
# **One space at the end, not `\s+`, and that is load-bearing.** journald joins
# its prefix to the message with exactly one space, so one space is all that
# belongs to journald — anything past it is the message's own indentation. A
# protobuf dump logged with `TIDY_LOGS` off is a record across a dozen lines whose
# nesting *is* that leading whitespace, and `\s+` here swallowed it, turning
# `   free: 16` into `free: 16` and flattening the structure the dump was written
# to show.
SOURCE = re.compile(r"\s+[^\s\[\]:]+\[\d+\]: ?")

# A bracketed tag standing on its own, which is what a level marker looks like.
# **The bracket has to start a word**, and that is what keeps journald's
# `mesh-collector[1234]:` out of it — the `[` there follows a letter, so it is
# part of a name rather than a marker, and the `[DEBUG]` further along the line
# is the first thing this matches. No whitespace inside, which is what keeps
# `[Errno 2]` out of it.
TAG = re.compile(r"(?:^|(?<=\s))\[([^\[\]\s]+)\]")

# The marker is padded to a fixed width so the message starts in the same column
# on every line. A log is read down its columns rather than along its lines, and a
# message column that steps sideways by one is a column the eye has to re-find on
# every row. Jason's, 2026-08-10.
#
# **The stamp in front of it is deliberately *not* padded, and one space separates
# it from the marker.** It could be: `LOG_TIME_FORMAT` asks for no leading zeros,
# so `%-m/%-d %-I:%M:%S %p` renders between fourteen columns (`1/1 1:00:00 AM`)
# and seventeen (`12/31 10:50:54 PM`), and a seventeen-wide field would hold the
# message still across every hour and date. It was written that way first and
# reverted, because the cost is visible on every single line and the benefit is
# not: padding to the widest *possible* stamp puts two idle columns in front of
# the marker all day, to buy alignment across a boundary crossed twice a day.
#
# So the log steps one column right when the hour reaches double digits, and again
# when a date changes width. Within any one of those spans — which is what a
# reader is actually looking at — every stamp is the same width and every message
# lines up. Jason's call, 2026-08-10.

# The marker is seven, which is `[DEBUG]` and `[ERROR]` — **not `[CRITICAL]`, the
# widest a level can be.** Sizing to the widest possible marker looks like the
# careful choice and is the wrong one, because the two markers that need the extra
# room are the two that almost never arrive. Measured over a real collector log of
# 2004 lines: 1456 `[DEBUG]`, 537 `[INFO]`, 4 `[WARNING]`, 1 `[ERROR]`, no
# `[CRITICAL]`. A field of ten spends three columns on every one of those lines to
# align four of them.
#
# So `[WARNING]` and `[CRITICAL]` overflow, and their message starts two or three
# columns right of everything else. That is a real inconsistency accepted on
# purpose, and it is cheap here for a reason worth writing down: those lines are
# the startup banners — MQTT proxying, transmit enabled — several hundred
# characters of prose that wrap over many rows anyway. A column is what a line is
# *scanned* by, and nobody scans a paragraph.
#
# **Overflowing rather than abbreviating is the point.** `[WARN]` and `[CRIT]`
# would align everything at this width, and were considered; they would also mean
# the level on screen is not the level in the journal, so grepping for what you
# read stops matching. `level_of` reads the marker off this same text, and while
# `LEVEL_ALIASES` would carry `WARN`, there is no `CRIT` in Python's vocabulary —
# a critical line would quietly classify as no level at all and fall out of the
# level filter. Jason's call, 2026-08-10.
#
# A tag wider than the field — someone else's `[a-long-tag]` — overflows the same
# way and keeps its single separating space, rather than being truncated to fit.
MARKER_FIELD = 7

# The stamp's separator and the marker it precedes, matched from the end of the
# stamp. `[ ]+` rather than `\s+` on both sides so a tab is never mistaken for
# field padding, and the `(?=\S)` means a line with nothing after its marker is
# left alone instead of being padded into trailing whitespace.
_STAMPED_MARKER = re.compile(r"[ ]+(\[[^\[\]\s]+\])[ ]+(?=\S)")

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

# The collector's line about the device it is plugged into: `Self !eeb826a4
# updated: rssi -87`, written by the tidy log's grouping (mesh-collector's
# selflog.py) once per TIDY_LOG_LOCAL_NODE_PERIOD. Only the word is claimed —
# the id after it is the node run above, painted by the same loop that paints
# every other id. The lookahead is what keeps this honest: `Self` must be
# followed by a node id, so a message that merely opens with the word — someone
# logging "Self test passed", say — is message and stays the message's colour.
# Matched at the start of the message only (see `_mark`), which is the one
# position the collector writes it in; a `Self !hex` in the middle of somebody's
# prose is quoting, not reporting.
SELF_MARK = re.compile(r"Self (?=![0-9a-fA-F]{8}(?![0-9A-Za-z]))")




class LogLine(NamedTuple):
  """One line as the viewer holds it: what to print, and what it is.

  `text` is the line after `localise_stamp` and `strip_source`, so it is what
  goes on screen and what any search of the scrollback would be searching — a
  search for a pid will not find one, because by here it is gone. `level` is one
  of `LEVELS` or `NO_LEVEL`, and is what the filter matches on.

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




def strip_source(line: str) -> str:
  """Drop journald's `python[2560]: ` from between the stamp and the message.

  **What it removes is the transport's, not the log's.** The collector logs
  through `logging` with `[%(levelname)s]` as its whole prefix; everything
  between the timestamp and that marker was added by journald on the way past,
  and names the interpreter and the pid rather than the record. Two lines of a
  protobuf dump differ in their content and in nothing else, so repeating the
  writer's name on both of them is a column of the same string all the way down
  the pane.

  **This also decides whether wrapped lines stay aligned.** The indent a
  continuation hangs from is measured to the message column, and the rule at
  `_MIN_MESSAGE_ROOM` drops the alignment when that column leaves too little room
  to wrap in. With journald's framing the prefix is forty-five columns and an
  eighty-column terminal gave the alignment up entirely; without it the same
  terminal keeps it. Removing this is what buys that back — see `wrap`.

  **Run after `localise_stamp` and anchored to what it wrote.** The match starts
  at the end of the local stamp, so this only ever removes a run in the one
  position journald puts it in. A line whose stamp was not recognised — no
  offset to convert, or not a stamp at all — is left completely alone, which is
  the same answer `localise_stamp` gives those lines and keeps this from reaching
  into the output of a `LOG_COMMAND` that is not journald at all.

  **It wants `--no-hostname`, which `LOG_COMMAND` already passes.** With the
  hostname in, journald writes `<stamp> pi4 python[2560]: ` and the identifier no
  longer follows the stamp directly, so nothing here matches and the whole run
  stays. That is deliberate rather than a gap: a hostname is the one field in
  there that is real information, a reader who turned it back on asked to see
  which machine spoke, and quietly eating it to reach the pid behind it would be
  answering a question nobody put. Keep the hostname and you keep the framing
  with it; the shipped command drops both.
  """
  stamp = LOCAL_STAMP.match(line)
  if stamp is None:
    return line

  match = SOURCE.match(line, stamp.end())
  if match is None:
    return line

  # One space back in place of the run: the stamp and what follows it are two
  # fields and this is the join between them. Whatever the message's first
  # characters are — a marker, or the indentation of a dump's continuation — they
  # survive intact, because `SOURCE` claimed only journald's own single separator.
  return line[:stamp.end()] + " " + line[match.end():]




def align_fields(line: str) -> str:
  """Pad the marker to a fixed width, so every message starts in one column.

  `[DEBUG]` is seven characters and `[INFO]` is six, so an unpadded log puts its
  messages in two different columns and the eye re-finds the text on every row.
  The marker is padded to `MARKER_FIELD` and one space separates each field from
  the next.

  **The stamp is not padded**, so the whole line still steps sideways when the
  hour or the date changes width — see the note above `MARKER_FIELD` for why that
  is the better trade than two idle columns on every row.

  **Only a line carrying both fields is reflowed, and everything else passes
  through.** A line with a stamp but no marker is a continuation — the body of a
  protobuf dump logged with `TIDY_LOGS` off — and its leading whitespace is the
  producer's own nesting rather than a field this may re-space. Padding the stamp
  on those lines would move that nesting for no gain, so they are left exactly as
  they arrived. Their message therefore does not sit in the shared column; that is
  the honest trade, and the alternative is inventing an empty marker field for a
  line that never had one.

  **A marker wider than `MARKER_FIELD` overflows rather than being cut**, and that
  includes two of the five levels: `[WARNING]` and `[CRITICAL]` start their message
  two and three columns right of everything else. The field is sized for the log
  that exists rather than the log that is possible — see `MARKER_FIELD` for the
  counts that decided it. Truncating a level, or someone else's `[a-long-tag]`, to
  fit a column would be rewriting what the command said, which is the one thing
  this module does not do.
  """
  stamp = LOCAL_STAMP.match(line)
  if stamp is None:
    return line

  match = _STAMPED_MARKER.match(line, stamp.end())
  if match is None:
    return line

  return (line[:stamp.end()]
          + " "
          + match.group(1).ljust(MARKER_FIELD)
          + " "
          + line[match.end():])




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
  # Order matters, and each step depends on the one before it: `strip_source`
  # finds its run by measuring from the end of the stamp `localise_stamp` has just
  # written, and `align_fields` can only put the marker in its column once the
  # framing between the two is gone.
  text = align_fields(strip_source(localise_stamp(line)))
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
# **The rule survives its original reason, and is kept for the next one.** It was
# a number because journald's prefix was long: the unit and pid arrived in front
# of the level, making `8/7 2:52:29 PM mesh-collector[1234]: [DEBUG] ` forty-five
# columns, and the rule decided the Pi's behaviour rather than being a formality.
# `strip_source` now removes that run, so a journald line prefixes a stamp and a
# level — twenty-two columns, the same as the testbed — and an eighty-column
# terminal keeps its alignment where it used to give it up.
#
# What still needs the rule is a narrow pane rather than a long prefix. Asking
# whether the indent leaves a readable column is the question that actually
# matters; the rule here was once "less than half the pane", which threw the
# alignment away on an eighty-column terminal with thirty-five perfectly good
# columns left to wrap in.
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
  """Colours the timestamp, the level marker, any node id and the collector's
  `Self` mark, and leaves the rest alone.

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

    # The one word of a message this paints: `Self`, when it opens the message
    # and a node id follows — the collector reporting on its own radio. Anchored
    # past the marker's padding the same way `message_indent` finds the message
    # column, so a `Self` anywhere later in the line is left alone. Four
    # characters, not the match — `SELF_MARK` claims the joining space to insist
    # on the id, and the space is not part of the word.
    spacing = re.compile(r"\s*").match(plain, tag.end())
    start = spacing.end() if spacing else tag.end()
    if SELF_MARK.match(plain, start):
      self._paint(text, "self", start, start + len("Self"))


  def _paint(self, text: Text, key: str, start: int, end: int) -> None:
    style = self.resolve(key)
    if style is not None:
      text.stylize(style, start, end)
