"""Deriving the rows the message list draws from the messages the archive gave us.

A tapback is a reaction rather than a message, so it is drawn on the message it
answers instead of taking a line of its own. That leaves the drawn list a
different length from the loaded one, and `derive_rows` is the only thing in the
project that crosses between them: it answers what the rows are, which row draws a
given message, and where a resume lands.

**This is its own module rather than more of `ui/format.py`**, on the argument
`ui/tapbacks.py` already makes: deciding which messages become rows is not turning
a row into a string, and `format.py` stays about strings. It is under `ui/` rather
than beside `state.py` at the package root because a row index is the interface's
coordinate system — a read position is about a message, and that is what stays in
`state.py`.

Nothing here touches a widget, a database or a terminal, so the rules below can be
exercised by calling `derive_rows` with a list of dicts. `test_drawn_counts.py` and
`test_unread_reactions.py` reach them by mounting the app, because what those check
is what the sidebar counts and the list draws.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Optional

from mesh_console.ui.tapbacks import is_tapback




class DerivedRows(NamedTuple):
  """One pass over the loaded messages: the rows, the index into them, and the resume.

  `row_of_message` has an entry for every loaded message, including a tapback that
  was absorbed into another row — it maps to the row it was drawn on. `resume_index`
  is `resume_message_id` resolved through that map, and 0 when there is none.
  """

  rows: list[dict[str, Any]]
  row_of_message: dict[int, int]
  resume_index: int




def derive_rows(
  messages: list[dict[str, Any]],
  resume_message_id: Optional[int],
) -> DerivedRows:
  """Derive the rendered rows from the loaded messages.

  A tapback is absorbed into its parent when the parent is in the loaded
  window. Only backwards: a tapback is by definition newer than what it
  answers, so a parent that is in the window at all is already in it by the
  time its reaction is reached.

  **A tapback whose parent is not in the window is held, not drawn.** This said
  the opposite until Jason found a `🏓` sitting in a channel as a message with a
  reply bar, sixty-one rows below a page fifty long: the message it answered was
  off the top of the window, so it had never been absorbed. Drawing it was the
  safer-looking answer — a reaction is at least not lost — and it is wrong twice
  over. It puts a row in the list that the archive does not consider a message,
  and it does it precisely when the reader has no way to tell what the reaction
  is *for*, because the thing it reacts to is the one thing not on screen.

  RxOnly has always held them (`pending_tapbacks`, messages.js:29): a tapback it
  cannot attach goes into a map and is flushed onto its parent the moment one
  arrives. This is the same, minus the map — every page is re-derived from
  `messages` from scratch, so walking to the older edge and pulling the parent in
  re-runs this and the reaction lands on it.

  A held tapback is still *in* `messages`, which is what keeps it from being
  permanently unread: `mark_read_from_viewport` sweeps the whole window at the
  bottom of a fully loaded channel, and that has never gone by rows.

  **Holding is for a parent that exists. A parent that does not is drawn.** The
  🏓 rule above is about a window, and paging is what settles it — walk far
  enough back and the parent arrives. When the parent is not in the archive at
  all, nothing is coming: the reaction is held forever, drawn nowhere, and
  counted anyway, which is how a channel came to read `Primary (1)` above `No
  messages in this channel.` The live archive has exactly one such row, a `💪`
  answering a message this radio never received.

  `reply_to_text is None` is what separates the two, and it is trustworthy
  because the query LEFT JOINs the parent against the whole table rather than
  against the loaded page — see `_MESSAGE_COLUMNS` in db/queries.py. NULL there
  is the archive saying the parent is not in it, not the window saying it has
  not been paged in.

  Such a row is drawn as itself, with a muted note where the reply bar would
  be, because the reply bar's job is to quote the parent and there is no parent
  to quote. This will become more common rather than less: tapbacks are always
  newer than their parents, so MAX_MESSAGES pruning takes the parent first.
  """
  rows: list[dict[str, Any]] = []
  row_of_message: dict[int, int] = {}
  held: set[int] = set()

  for message in messages:
    if is_tapback(message) and message.get("reply_to_text") is None:
      # Orphaned: drawn as an ordinary row and flagged so the widget can say
      # why it has no reply bar. Registered in `row_of_message` like any drawn
      # message — it has a row of its own now, and a resume naming it should
      # land on it rather than on the row above.
      row_of_message[message["message_id"]] = len(rows)
      rows.append({"message": message, "tapbacks": [], "orphan_tapback": True})
      continue

    if is_tapback(message):
      parent = message["reply_to"]
      # A reaction to a held tapback is held too. `row_of_message` answers for
      # one of those with the row *above* it, which is the right answer for a
      # cursor and the wrong one to hang an emoji on.
      parent_row = None if parent in held else row_of_message.get(parent)

      if parent_row is not None:
        rows[parent_row]["tapbacks"].append(message)
        # So a read position or a resume that names the tapback still resolves
        # to a row — the row it is now drawn on.
        row_of_message[message["message_id"]] = parent_row
        continue

      held.add(message["message_id"])
      # It has no row, and a resume that names it has to land somewhere real:
      # the nearest row above, which is the message it arrived after.
      row_of_message[message["message_id"]] = max(len(rows) - 1, 0)
      continue

    row_of_message[message["message_id"]] = len(rows)
    rows.append({"message": message, "tapbacks": []})

  if resume_message_id is not None:
    resume_index = row_of_message.get(resume_message_id, 0)
  else:
    resume_index = 0

  return DerivedRows(rows, row_of_message, resume_index)
