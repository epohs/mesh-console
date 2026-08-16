"""Which slice of the archive is loaded, and where its edges are.

A window is a page of messages plus the four facts that say where that page sits
in the archive: whether there is more in either direction, and the cursor at each
end. Everything here is fetch-then-derive — ask the archive for a page, then work
out from what came back what the window now is.

**This is at the package root rather than under `ui/`**, unlike `ui/rows.py`,
which went there because a row index is the interface's coordinate system. A
cursor is not one: it is an `(rx_time, id)` pair naming a place in the archive,
and it means the same thing with no terminal attached. That is `send.py`'s stated
principle — "every function below is callable and testable with no terminal
attached, which is the same argument that keeps `state.py` at the package root" —
and which slice is loaded falls on the `state.py` side of it.

**The App keeps the window-edge fields; this module only produces a value to
assign from.** `has_more_older`, `has_more_newer`, `oldest_cursor` and
`newest_cursor` are touched by fifteen other methods on the class: the scroll
triggers, the `g` jump's reset, the status line, the end-of-window tests, the
poll and the absorb behind it — which deliberately raises `has_more_newer`
*before* a fetch, and has a docstring saying why. An extraction that owned these
fields would have to hand every one of them back.

**The async pagers `load_older` and `load_newer` stay on the App and are not
served from here.** They fetch the same way and then do interface work around it:
a `batch_update` held open across a re-render, a viewport anchor and a cursor to
put back. What they share with the functions here is the fetch and the edge
arithmetic, not the method — which is the argument for lifting a *fetch* rather
than a method.

Nothing here touches a widget or a terminal, and nothing opens a connection: the
one dependency is a `read` callable that runs one query and answers None when the
archive cannot be reached. `testbed/tests/test_window.py` exercises the rules by
passing a fake one.
"""

from __future__ import annotations

from typing import Any, Callable, NamedTuple, Optional

from mesh_console import db




class Window(NamedTuple):
  """One loaded slice of a channel: the messages, both edges, and where to resume.

  `resume_message_id` names a message rather than a row, because which row holds a
  message depends on what else is loaded — resolving it to a row is
  `rebuild_rows()`'s job, and happens after this value has been assigned.

  A cursor is None only when the page came back empty, which is an empty channel:
  there is no message to take an `(rx_time, id)` pair from. Only
  `fetch_edge_window` can produce that — a window built around an anchor always
  has the anchor in it, so it always has a pair to hand back.
  """

  messages: list[dict[str, Any]]
  has_more_older: bool
  has_more_newer: bool
  oldest_cursor: Optional[tuple[int, int]]
  newest_cursor: Optional[tuple[int, int]]
  resume_message_id: Optional[int]




def fetch_edge_window(
  read: Callable[..., Any],
  *,
  is_dm: bool,
  channel_index: Optional[int],
  peer: Optional[str],
  newest: bool,
  limit: int,
) -> Optional[Window]:
  """One page from an edge of the channel — the live end, or the start.

  `newest` picks the edge: True is the tail, which is where a channel is read
  from; False is the oldest page, which is where a channel that has never been
  read opens.

  **None means the read failed, not that the channel is empty.** An empty channel
  is a Window with no messages in it and both cursors None. The caller relies on
  the difference: a failed load leaves the channel unopened and says so, and an
  empty one opens normally.

  **The resume lands on the edge the page was fetched from** — the last message
  at the newest edge, the first at the oldest — because that is the message the
  reader is looking at when the window arrives. It is an id rather than an index
  because a reaction in this page may be drawn on some other row; see
  `ui/rows.py`.

  The scope arguments mirror `db.fetch_message_page`'s own, keyword-only past
  `is_dm` for that function's stated reason: a node id must never land in
  `channel_index` by being passed one position early.
  """
  page = read(
    db.fetch_message_page,
    is_dm,
    channel_index,
    peer=peer,
    newest=newest,
    limit=limit,
  )
  if page is None:
    return None

  messages = page["messages"]

  if messages:
    edge = messages[-1] if newest else messages[0]
    resume_message_id = edge["message_id"]
  else:
    resume_message_id = None

  return Window(
    messages=messages,
    has_more_older=page["meta"]["has_more_older"],
    has_more_newer=page["meta"]["has_more_newer"],
    oldest_cursor=page["meta"]["oldest"],
    newest_cursor=page["meta"]["newest"],
    resume_message_id=resume_message_id,
  )




def fetch_window_around(
  read: Callable[..., Any],
  message_id: int,
  *,
  is_dm: bool,
  channel_index: Optional[int],
  peer: Optional[str],
  limit: int,
) -> Optional[Window]:
  """One page either side of a named message, with that message in the middle.

  What resuming has always done, with the message named directly rather than
  taken out of a stored position — because there is now a second way to arrive
  somewhere specific: pressing enter on a row of the flat direct message list,
  which lands in that peer's conversation on that message.

  None when the message is not in the archive, which for a resume is the
  collector having pruned past it. The caller falls back to a fresh load.

  **A failed read answers None as well, and nothing here tells the two apart.**
  `read` returns None when SQLite raised, so an absent anchor and an unreachable
  archive arrive at the same check, and the two page reads after it can fail the
  same way. Four causes, one return value, one fallback. `fetch_edge_window`
  does draw the distinction, because there None-versus-empty separates an
  unreachable archive from a channel nobody has posted in; here every road ends
  at a fresh load.

  **An empty page either side stands in the anchor's own cursor.** A message
  with nothing older than it is the first one in the channel, which is ordinary:
  the page comes back with `meta.oldest` of None, and the place to page from
  next is still real — it is the anchor. `fetch_edge_window` needs no
  equivalent, because an empty page there means an empty channel.

  The scope arguments mirror `db.fetch_message_page`'s own, keyword-only past
  `message_id` for that function's stated reason: a node id must never land in
  `channel_index` by being passed one position early.
  """
  anchor = read(db.fetch_message, message_id, is_dm)
  if anchor is None:
    return None

  cursor = db.cursor_of(anchor)

  older = read(
    db.fetch_message_page,
    is_dm,
    channel_index,
    peer=peer,
    before=cursor,
    limit=limit,
  )
  newer = read(
    db.fetch_message_page,
    is_dm,
    channel_index,
    peer=peer,
    after=cursor,
    limit=limit,
  )

  if older is None or newer is None:
    return None

  messages = older["messages"] + [anchor] + newer["messages"]

  # The anchor message, by id rather than by position. `rebuild_rows()`
  # turns that into a row index — and if it has since
  # become a tapback absorbed into another row, it resolves to the row holding
  # it, so a position recorded before this slice still resumes somewhere real.
  resume_message_id = anchor["message_id"]

  return Window(
    messages=messages,
    has_more_older=older["meta"]["has_more_older"],
    has_more_newer=newer["meta"]["has_more_newer"],
    oldest_cursor=older["meta"]["oldest"] or cursor,
    newest_cursor=newer["meta"]["newest"] or cursor,
    resume_message_id=resume_message_id,
  )
