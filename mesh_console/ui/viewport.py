"""How far up the pane the reader has read, and which row that is.

The read line is a line across the message pane, `read_margin` lines up from the
bottom of it. A row whose top has crossed it has been read. That rule and the one
exception to it are all this module is: it is handed the rows' measured extents
and where the pane is scrolled to, and it answers with a row index.

**This is under `ui/` rather than at the package root**, which is the opposite
call to `window.py`'s and the same one `ui/rows.py` made. Every number crossing
this seam is the interface's coordinate system — a row's extent in lines, the
pane's height in lines, a scroll offset in lines, and a row index back. Resize
the pane and all of them change, which is exactly what an `(rx_time, id)` cursor
naming a place in the archive does not do; that is `window.py`'s stated test and
this fails it. A read *position* is about a message and stays in `state.py`. This
answers in rows, and `mark_read_from_viewport` is what turns the row into a
message.

**The App measures and this decides, and that is where the seam is.** Reading a
`virtual_region` off a laid-out `ListItem` needs a mounted app; working out what
the numbers mean does not. So the caller hands over `RowSpan`s of plain integers
rather than widgets — importing Textual here would put these rules back behind a
mounted app, which is the thing three sessions of measurement say is not a net.

Nothing here touches a widget, a terminal or the archive.
`testbed/tests/test_viewport.py` exercises the rules by passing lists of
integers.
"""

from __future__ import annotations

from typing import NamedTuple, Optional


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




class RowSpan(NamedTuple):
  """One rendered row's vertical extent, in the scrolled content's own coordinates.

  A row's `virtual_region.y` and `.bottom`, measured from the top of the whole
  scrolled content rather than from the top of the pane. Both ends, rather than a
  top and a height, because the two rules below ask different ones: a row in the
  middle of the list is read when its **top** clears the read line, and the last
  row of a fully loaded channel when its **bottom** comes on screen.
  """

  top: int
  bottom: int




class ReadThrough(NamedTuple):
  """The row the reader has read through, and which of the two rules said so.

  `at_the_end` is the bottom-of-a-fully-loaded-channel case, and the caller is
  told about it because the two rules disagree about more than which row: at the
  end everything loaded has been seen, reactions included, and a reaction is not
  a row. So `index` is not enough to name what was read there, and
  `mark_read_from_viewport` takes its marker from the messages instead.
  """

  index: int
  at_the_end: bool




def read_margin(height: int) -> int:
  """How far up from the bottom of the pane the read line sits, in lines."""
  return max(READ_MARGIN_MIN_LINES, int(height * READ_MARGIN_FRACTION))




def read_through(
  rows: list[RowSpan],
  *,
  row_count: int,
  height: int,
  scroll_y: int,
  has_more_newer: bool,
) -> Optional[ReadThrough]:
  """Which row the viewport has carried above the read line, if any.

  `rows` is what the list is drawing, measured; `row_count` is how many rows the
  App's model says there should be. They are the same list counted from two
  sides, and a disagreement between them is the whole point of the first guard
  below — so passing `len(rows)` for `row_count` disables it.

  Far enough is `read_margin` lines from the bottom, so the last message or two of
  a channel that is still scrolling are not claimed while they sit at the edge of
  vision. A row counts when its **top** clears the line — a message is several
  lines tall, and waiting for its last line would leave a long message unread
  while the reader was already past it.

  **None is "do not mark", which is not the same as "nothing is read".** Four
  ways to it: no rows at all, a list mid-redraw whose count disagrees, a list not
  laid out yet, and a read line above the first row — which takes a pane fewer
  than four lines tall at the top of its channel, the margin's floor being four.
  In every one the marker stays where it was, which is the safe direction:
  reading only ever moves forwards.
  """
  # Not reachable from `mark_read_from_viewport`, which guards on its rows before
  # it measures anything. Answering here anyway, because a rule that says nothing
  # is read beats one that raises on the way to the same place.
  if not rows:
    return None

  # **Only a settled list can be read.** Between a window changing and the redraw
  # finishing, the rendered rows disagree with the App's `rows` in count; between
  # the redraw and the next layout pass, every row reports its `virtual_region` at
  # y=0. Measured in either state, the arithmetic below claims the whole window:
  # rows all at zero are all "above the read line", and a `max_scroll_y` of
  # nothing makes anywhere "the bottom of a fully loaded channel". `positioning`
  # guards exactly one of the windows where that happened — opening a channel —
  # but a page arriving rebuilds the same list with no flag up, and a scroll
  # event landing in that gap marked messages read that nobody had seen. The
  # guard belongs here, with the arithmetic, so no caller has to know.
  if len(rows) != row_count:
    return None
  if len(rows) > 1 and all(row.top == 0 for row in rows):
    return None

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
  if not has_more_newer and rows[-1].bottom <= scroll_y + height:
    return ReadThrough(index=len(rows) - 1, at_the_end=True)

  # Measured in the scrollable content's own coordinates, which is what
  # `virtual_region` is and what makes this independent of where the list
  # happens to be on screen.
  line = scroll_y + height - read_margin(height)

  read = [index for index, row in enumerate(rows) if row.top <= line]
  if not read:
    return None

  return ReadThrough(index=read[-1], at_the_end=False)
