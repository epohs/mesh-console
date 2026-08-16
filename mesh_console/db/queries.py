"""Reads of the archive, in the shapes the interface renders.

Every query RxOnly serves over HTTP, this project runs directly against the same
SQLite file. The two readers stay independent — no shared package, no import
across projects — so these are deliberate reimplementations of the same reads,
not a library extracted from the web app.

One difference from RxOnly worth knowing: channel messages and direct messages
are fetched by a single function here rather than by two near-identical endpoints.
The cursor arithmetic is the subtle part and it is character-for-character the
same in both of RxOnly's endpoints today — which is the argument for writing it
once here, not evidence that it has drifted. Both tables return the same row
keys, with the columns direct messages don't have present and null, so the
interface never has to ask which kind of message it is holding.
"""

from __future__ import annotations

import sqlite3

from typing import Any, Optional

from mesh_console.db.connection import get_meta, get_meta_int


# Page size to clamp to when the collector hasn't published its retention limit.
# Matches the default page size, so paging still works without over-reading an
# archive whose size we can't confirm. An unpublished limit means we don't know
# what the collector keeps, not that we may assume a generous one.
FALLBACK_MAX_MESSAGES = 50

# The columns every message row carries, whichever table it came from.
#
# `m.emoji` is why this reader's floor moved to schema 0.10.0. Because this list
# is shared by both tables, selecting it here required the column on
# direct_messages as well as messages — the schema added both for that reason.
_MESSAGE_COLUMNS = """
  m.id, m.message_id, m.from_node, m.text, m.rx_time,
  m.snr, m.rssi, m.reply_to, m.via_mqtt, m.emoji,
  n.long_name AS from_node_long_name,
  n.short_name AS from_node_short_name,
  tn.long_name AS to_node_long_name,
  tn.short_name AS to_node_short_name,
  parent.text AS reply_to_text,
  parent.from_node AS reply_to_from_node,
  pn.short_name AS reply_to_from_node_short_name
"""

# Channel messages carry two columns direct messages have no equivalent for.
# to_node used to be a third, until schema 0.7.0 gave direct messages a real
# recipient — reporting NULL for it now would be inventing a gap that no longer
# exists, and an outbound direct message would lose the only record of who it
# went to.
_CHANNEL_EXTRA = "m.channel_index, m.to_node, m.hop_count"
_DM_EXTRA = "NULL AS channel_index, m.to_node, NULL AS hop_count"

# The six telemetry columns after altitude arrived in schema 0.8.0, and
# selecting them here is why this reader's floor was 0.8.0 rather than 0.7.0.
# They are latest-value, not a series: each is the most recent reading of its
# kind, and NULL for a node that has never sent that telemetry arm. Every one
# of the four node queries below reads this list, so the whole reader gained
# them at once.
#
# `hops_away` is 0.9.0's and costs this reader nothing to add, because the floor
# already sits at 0.10.0 for `emoji` — the column is older than the oldest
# archive this code will open. Read it with IS NULL and never with falsiness:
# 0 hops is a direct neighbour, which is the loudest reading in the column, and
# the field renderer in ui/format.py honours that distinction.
_NODE_COLUMNS = """
  node_id, short_name, long_name, hardware, role,
  first_seen, last_seen, hops_away, battery_level, voltage,
  snr, rssi, latitude, longitude, altitude,
  temperature, humidity, pressure,
  channel_util, air_util_tx, uptime_seconds
"""

# "The mesh has told us a name for this node", and the one place it is written.
#
# Names and nothing else. A node can report a hardware model and no name at all, so
# testing `hardware` would keep rows this is meant to hide; and a `long_name` of
# 'Meshtastic 18b7' is a real name that unconfigured firmware genuinely announces,
# not a fabricated one. Unnamed is `long_name IS NULL AND short_name IS NULL`, and
# this is its negation. RxOnly spells the same predicate the same way in its
# web/db.py — a deliberate reimplementation, like everything else in this module.
_NAMED_NODE = "(long_name IS NOT NULL OR short_name IS NOT NULL)"




def _node_where(*conditions: str, list_unnamed: bool = False) -> str:
  """A WHERE clause for a node *list*, honouring the caller's LIST_UNNAMED_NODES.

  Returns "" only when there is nothing at all to restrict — no caller condition
  and the reader has asked to see unnamed nodes.

  Composed from a list rather than by patching a clause, which is what makes the
  filtered and unfiltered cases the same code: `fetch_nodes` used to interpolate a
  `match_clause` that was either a whole `WHERE ...` or the empty string, so a
  predicate appended as `AND (...)` broke the empty case and one prepended as
  `WHERE ...` broke the search case. Joining fragments has neither edge.

  Conditions are ANDed, so a caller passing an OR chain must parenthesise it — the
  search clause in `fetch_nodes` does. **This is for lists only.** `fetch_node` and
  `fetch_nodes_by_id` resolve nodes by the ids they are handed and ignore the flag
  entirely: see CONSOLE_CONFIG in mesh_console/config.py for why.
  """
  clauses = [condition for condition in conditions if condition]

  if not list_unnamed:
    clauses.append(_NAMED_NODE)

  if not clauses:
    return ""

  return "WHERE " + " AND ".join(clauses)




def _message_table(is_dm: bool) -> tuple[str, str]:
  """Return the table name and the extra column list for one kind of message."""
  if is_dm:
    return "direct_messages", _DM_EXTRA
  return "messages", _CHANNEL_EXTRA




# How a peer is derived from a direct message row: the end of it that is not us.
# Every row in direct_messages involves the local node — the collector archives an
# inbound DM only when `to_id == local_node_id`, and an outbound row always has
# `from_node = local` — so this is total over the table as it is actually written.
_PEER_OF_ROW = "CASE WHEN from_node = ? THEN to_node ELSE from_node END"


def _scope_clauses(
  is_dm: bool,
  channel_index: Optional[int],
  peer: Optional[str],
) -> tuple[list[str], list[Any]]:
  """Which rows belong to the thing being read: a channel, or one conversation.

  Returned as fragments rather than a finished WHERE clause because
  `fetch_message_page` needs the same restriction in four places — the page, its
  total, and the two has_more probes — and a channel filter that reached only some
  of them was how RxOnly's pager came to disagree with its own has_more.

  **A peer cannot ride in on `channel_index`.** That is an int and this is a hex
  string, and the one is the encryption context a message goes out on while the
  other is who it is addressed to. Two parameters, and the direct message branch
  ignores the channel index exactly as it did before.

  The peer test names both ends. `(from_node = ? OR to_node = ?)` is redundant
  today — one of the two is always the local node — but being explicit about both
  ends costs one comparison and is what keeps this correct if a collector ever
  archives a DM it merely overheard.
  """
  parts: list[str] = []
  params: list[Any] = []

  if is_dm:
    if peer is not None:
      parts.append("(m.from_node = ? OR m.to_node = ?)")
      params.extend((peer, peer))
    return parts, params

  if channel_index is not None:
    parts.append("m.channel_index = ?")
    params.append(channel_index)

  return parts, params




def fetch_channels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
  """Channels with their message counts, for the sidebar."""
  rows = conn.execute(
    """
    SELECT c.channel_index, c.name, COUNT(m.id) AS message_count
    FROM channels c
    LEFT JOIN messages m ON c.channel_index = m.channel_index
    GROUP BY c.channel_index, c.name
    ORDER BY c.channel_index
    """
  ).fetchall()

  return [dict(row) for row in rows]




def fetch_local_node(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
  """The attached device, resolved through meta.local_node_id.

  meta stores the id in nodes.node_id's hex format, so there is no conversion
  here — that was Phase 0's whole point.
  """
  local_node_id = get_meta(conn, "local_node_id")
  if local_node_id is None:
    return None

  row = conn.execute(
    f"SELECT {_NODE_COLUMNS} FROM nodes WHERE node_id = ?",
    (local_node_id,),
  ).fetchone()

  if row is None:
    # The collector has named the device but not yet recorded a NodeInfo for it.
    return {"node_id": local_node_id}

  return dict(row)




def fetch_stats(
  conn: sqlite3.Connection,
  show_direct_messages: bool = False,
  list_unnamed: bool = False,
) -> dict[str, Any]:
  """Dashboard counts and the local node.

  `total_nodes` follows `fetch_nodes`: it is the number the sidebar heading reports
  and the number the list pages towards, so counting rows the list will not show
  would make `Nodes (84)` name a set the reader cannot reach the end of. The local
  node below is resolved by id and is deliberately not filtered — the attached
  device is reported whether or not it has been given a name.
  """
  total_nodes = conn.execute(
    f"SELECT COUNT(*) AS count FROM nodes {_node_where(list_unnamed=list_unnamed)}"
  ).fetchone()["count"]
  total_messages = conn.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"]
  total_channels = conn.execute("SELECT COUNT(*) AS count FROM channels").fetchone()["count"]

  # Whether to display direct messages is this process's decision, and defaults
  # to no regardless of what the archive holds.
  if show_direct_messages:
    total_direct_messages = conn.execute(
      "SELECT COUNT(*) AS count FROM direct_messages"
    ).fetchone()["count"]
  else:
    total_direct_messages = 0

  channel_counts = {
    row["channel_index"]: row["message_count"]
    for row in conn.execute(
      """
      SELECT c.channel_index, COUNT(m.id) AS message_count
      FROM channels c
      LEFT JOIN messages m ON c.channel_index = m.channel_index
      GROUP BY c.channel_index
      """
    ).fetchall()
  }

  return {
    "local_node": fetch_local_node(conn),
    "stats": {
      "total_nodes": total_nodes,
      "total_messages": total_messages,
      "total_channels": total_channels,
      "total_direct_messages": total_direct_messages,
      "channel_counts": channel_counts,
    },
  }




def fetch_unread_channel_counts(
  conn: sqlite3.Connection,
  cursors: dict[int, tuple[int, int]],
  local_node_id: Optional[str] = None,
) -> dict[int, int]:
  """How many messages sit after the read marker in each channel.

  `cursors` maps a channel index to the `(rx_time, id)` pair the reader has read
  through, as `ReadPositions.cursor()` returns it. A channel absent from it has
  never been read, and every message in it is unread.

  **One query rather than one per channel**, on the pattern `fetch_stats` uses
  for `channel_counts` — but the threshold differs per channel, so the positions
  travel in as a `VALUES` list and are joined against rather than sitting in a
  single `WHERE`. There are rarely more than a handful of channels, so per-channel
  queries would have been defensible too; this way the sidebar costs one round
  trip however many channels the collector tracks, and the count and the page
  agree because both compare the same pair.

  The comparison is `(rx_time, id)` and not rx_time alone. rx_time is whole
  seconds off the mesh, so ties are routine: counting `rx_time > marker` loses
  every message tied with the marker, and `>=` counts the marker itself. Both are
  wrong by an amount that looks exactly like a real unread message.

  **`local_node_id` excludes this device's own messages, because a message you sent
  is not one waiting to be read.** Sending on a channel used to raise that channel's
  unread badge, which is the archive's answer to "what is after the marker" but not
  the reader's question. Optional and defaulting to None so the exclusion is off
  unless a caller names a device — an archive that has named none cannot attribute
  any row, and `from_node != NULL` is NULL for every row, which would have counted
  nothing at all.
  """
  # SQLite has no empty VALUES list, and a reader with nothing read yet is the
  # first run rather than an edge case. One row of nulls joins to no channel, so
  # every channel falls through to its full count and there is one code path.
  if cursors:
    values = ", ".join("(?, ?, ?)" for _ in cursors)
    params: list[Any] = []
    for channel_index, (rx_time, row_id) in cursors.items():
      params.extend((channel_index, rx_time, row_id))
  else:
    values = "(NULL, NULL, NULL)"
    params = []

  # Appended, not interpolated: positional parameters follow the order the `?`s
  # appear in the text, and this one is after the VALUES list.
  mine = ""
  if local_node_id is not None:
    mine = "AND m.from_node != ?"
    params.append(local_node_id)

  rows = conn.execute(
    f"""
    WITH position(channel_index, rx_time, row_id) AS (VALUES {values})
    SELECT c.channel_index, COUNT(m.id) AS unread
    FROM channels c
    LEFT JOIN position p ON p.channel_index = c.channel_index
    LEFT JOIN messages m
      ON m.channel_index = c.channel_index
     {mine}
     AND (p.channel_index IS NULL
          OR m.rx_time > p.rx_time
          OR (m.rx_time = p.rx_time AND m.id > p.row_id))
    GROUP BY c.channel_index
    """,
    params,
  ).fetchall()

  return {row["channel_index"]: row["unread"] for row in rows}




def fetch_unread_direct_count(
  conn: sqlite3.Connection,
  cursor: Optional[tuple[int, int]] = None,
  local_node_id: Optional[str] = None,
) -> int:
  """How many direct messages sit after the read marker, or all of them.

  Separate from the channel counts because the direct message list is one
  conversation-shaped thing with one position, not a row per channel — the same
  reason `fetch_stats` counts it separately.

  **This is where Jason noticed it**: every direct message in this archive was one
  this device sent, and the sidebar was reporting all three as unread. A message you
  sent is not waiting to be read. `local_node_id` is what makes the row attributable
  and is optional for the reason it is optional on the channel counts — with no
  device named, nothing can be claimed as yours, and a `!= NULL` comparison would
  silently exclude everything rather than nothing.
  """
  mine = ""
  params: list[Any] = []
  if local_node_id is not None:
    mine = "from_node != ?"
    params.append(local_node_id)

  if cursor is None:
    where = f"WHERE {mine}" if mine else ""
    return conn.execute(
      f"SELECT COUNT(*) AS count FROM direct_messages {where}", params
    ).fetchone()["count"]

  rx_time, row_id = cursor
  after = "(rx_time > ? OR (rx_time = ? AND id > ?))"
  params.extend((rx_time, rx_time, row_id))

  return conn.execute(
    f"""
    SELECT COUNT(*) AS count FROM direct_messages
    WHERE {f'{mine} AND ' if mine else ''}{after}
    """,
    params,
  ).fetchone()["count"]




def fetch_conversations(
  conn: sqlite3.Connection,
  local_node_id: str,
) -> list[dict[str, Any]]:
  """Which peers this device has exchanged direct messages with, newest first.

  The only genuinely new shape of read this phase needs, and it is the shape
  `fetch_stats` already uses for `channel_counts`: group, count, order by the
  newest thing in each group.

  `local_node_id` is passed in rather than read from `meta` here, so that the
  answer to "which end of this row is the peer" cannot disagree with the answer the
  interface uses to decide which rows are yours. Both come from
  `meta.local_node_id`; this makes them the same value rather than two reads of it.

  A row whose derived peer is NULL is left out. Since schema 0.7.0 `to_node` is
  populated in both directions so there should be none — and a conversation with
  nobody is not something to render a compose box for.
  """
  rows = conn.execute(
    f"""
    SELECT d.peer,
           COUNT(*) AS message_count,
           MAX(d.rx_time) AS newest_rx_time,
           pn.short_name AS peer_short_name,
           pn.long_name AS peer_long_name
    FROM (
      SELECT {_PEER_OF_ROW} AS peer, rx_time
      FROM direct_messages
    ) d
    -- The peer's own name, so a row can be rendered without a second read per
    -- conversation. Left, because a node that has never sent a NodeInfo is still
    -- somebody you have a conversation with; the caller falls back to the hex id.
    LEFT JOIN nodes pn ON pn.node_id = d.peer
    WHERE d.peer IS NOT NULL
    GROUP BY d.peer, pn.short_name, pn.long_name
    ORDER BY newest_rx_time DESC, d.peer
    """,
    (local_node_id,),
  ).fetchall()

  return [dict(row) for row in rows]




def fetch_unread_conversation_counts(
  conn: sqlite3.Connection,
  local_node_id: str,
  cursors: dict[str, tuple[int, int]],
) -> dict[str, int]:
  """How many direct messages sit after the read marker in each conversation.

  The per-peer counterpart of `fetch_unread_channel_counts`, and the same
  arrangement for the same reasons: one query however many peers there are, the
  positions travelling in as a `VALUES` list because the threshold differs per
  peer, and `(rx_time, id)` compared as a pair because rx_time is whole seconds
  off the mesh and a tie with the marker is routine.

  It differs in one way. Channels have an authoritative list of their own to join
  from, and peers do not — a conversation exists because there are messages in it —
  so the list of peers is derived from the table in the same query.

  It shares the exclusion, though: a direct message this device sent is not one
  waiting to be read here either. `local_node_id` was already required — it is how
  the peer of a row is derived — so unlike the other two counts there is no
  optional-parameter case to reason about.
  """
  # SQLite has no empty VALUES list, and a reader with nothing read yet is the
  # first run rather than an edge case. One row of nulls joins to no peer, so every
  # conversation falls through to its full count and there is one code path.
  if cursors:
    values = ", ".join("(?, ?, ?)" for _ in cursors)
    params: list[Any] = [local_node_id]
    for peer, (rx_time, row_id) in cursors.items():
      params.extend((peer, rx_time, row_id))
  else:
    values = "(NULL, NULL, NULL)"
    params = [local_node_id]

  # The join's own `?`, after the VALUES list in the text and so after those
  # parameters here. Same id as the one the CTE derives the peer with.
  params.append(local_node_id)

  rows = conn.execute(
    f"""
    WITH dm AS (
      SELECT id, rx_time, from_node, {_PEER_OF_ROW} AS peer
      FROM direct_messages
    ),
    peers AS (SELECT DISTINCT peer FROM dm WHERE peer IS NOT NULL),
    position(peer, rx_time, row_id) AS (VALUES {values})
    SELECT p.peer, COUNT(m.id) AS unread
    FROM peers p
    LEFT JOIN position pos ON pos.peer = p.peer
    LEFT JOIN dm m
      ON m.peer = p.peer
     -- Not counted: a message this device sent. Filtered on the join rather than
     -- in the CTE, so a conversation in which we have only ever spoken still
     -- appears here with a count of zero instead of dropping out of `peers`.
     AND m.from_node != ?
     AND (pos.peer IS NULL
          OR m.rx_time > pos.rx_time
          OR (m.rx_time = pos.rx_time AND m.id > pos.row_id))
    GROUP BY p.peer
    """,
    params,
  ).fetchall()

  return {row["peer"]: row["unread"] for row in rows}




def fetch_message_page(
  conn: sqlite3.Connection,
  is_dm: bool = False,
  channel_index: Optional[int] = None,
  *,
  peer: Optional[str] = None,
  after: Optional[tuple[int, int]] = None,
  before: Optional[tuple[int, int]] = None,
  newest: bool = False,
  limit: int = 50,
) -> dict[str, Any]:
  """One page of messages, oldest-first, with cursors for both directions.

  Exactly one of after / before / newest is meaningful: `after` walks forward,
  `before` walks back, `newest` ignores both and returns the tail. Rows always
  come back oldest-first whichever way the page was reached, so the interface
  appends and prepends without reordering.

  **A cursor is an `(rx_time, id)` pair, not a timestamp.** rx_time is whole
  seconds off the mesh, so two messages in one channel routinely share one, and
  a bare `rx_time < cursor` drops every message in the boundary second —
  including ones the previous page never showed. Paging back through a tie then
  loses a message silently, with no gap to see. The pair is also exactly what the
  has_more checks below compare, which is the point: the query that finds the
  next page and the query that reports whether one exists now agree.

  RxOnly's two message endpoints took a bare `before_rx_time` and had this bug;
  they take the pair now, and keep the bare form working by resolving it against
  the archive. The two projects agree again.

  **`peer` narrows a direct message page to one conversation**, and is what makes
  a compose box addressable: the recipient is a property of the page rather than of
  whichever row the cursor happens to be on. It is keyword-only, along with the
  cursors, so that a node id can never land in `channel_index` by being passed one
  position early.
  """
  table, extra_columns = _message_table(is_dm)

  # The collector owns the retention limit; clamp to what it publishes rather
  # than to this process's own idea of it.
  policy_key = "max_direct_messages" if is_dm else "max_messages"
  max_messages = get_meta_int(conn, policy_key, FALLBACK_MAX_MESSAGES)

  limit = max(1, min(limit, max_messages))

  scope_parts, scope_params = _scope_clauses(is_dm, channel_index, peer)

  where_parts: list[str] = list(scope_parts)
  params: list[Any] = list(scope_params)

  if not newest:
    if after is not None:
      where_parts.append("(m.rx_time > ? OR (m.rx_time = ? AND m.id > ?))")
      params.extend((after[0], after[0], after[1]))
    elif before is not None:
      where_parts.append("(m.rx_time < ? OR (m.rx_time = ? AND m.id < ?))")
      params.extend((before[0], before[0], before[1]))

  where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

  # Total for whatever is being read — this channel, this conversation, or the
  # whole table — unfiltered by cursor. One expression rather than a branch per
  # scope, so a new scope cannot arrive with a total that ignores it.
  scope_where = (" WHERE " + " AND ".join(scope_parts)) if scope_parts else ""
  total = conn.execute(
    f"SELECT COUNT(*) AS count FROM {table} m{scope_where}",
    scope_params,
  ).fetchone()["count"]

  # Walking backwards means taking the most recent N and reversing them.
  reversed_scan = newest or before is not None
  order_clause = (
    "ORDER BY m.rx_time DESC, m.id DESC" if reversed_scan
    else "ORDER BY m.rx_time ASC, m.id ASC"
  )

  rows = [
    dict(row) for row in conn.execute(
      f"""
      SELECT {extra_columns}, {_MESSAGE_COLUMNS}
      FROM {table} m
      LEFT JOIN nodes n ON m.from_node = n.node_id
      LEFT JOIN {table} parent ON m.reply_to = parent.message_id
      LEFT JOIN nodes tn ON m.to_node = tn.node_id
      LEFT JOIN nodes pn ON parent.from_node = pn.node_id
      {where_clause}
      {order_clause}
      LIMIT ?
      """,
      (*params, limit),
    ).fetchall()
  ]

  if reversed_scan:
    rows.reverse()

  has_more_older = False
  has_more_newer = False

  if rows:
    # The same restriction the page itself used. A has_more that asked a wider
    # question than the page would report a message in another conversation as
    # more of this one.
    scope_filter = "".join(f" AND {part}" for part in scope_parts)

    oldest, newest_row = rows[0], rows[-1]

    has_more_older = conn.execute(
      f"""
      SELECT 1 FROM {table} m
      WHERE (m.rx_time < ? OR (m.rx_time = ? AND m.id < ?)){scope_filter}
      LIMIT 1
      """,
      (oldest["rx_time"], oldest["rx_time"], oldest["id"], *scope_params),
    ).fetchone() is not None

    has_more_newer = conn.execute(
      f"""
      SELECT 1 FROM {table} m
      WHERE (m.rx_time > ? OR (m.rx_time = ? AND m.id > ?)){scope_filter}
      LIMIT 1
      """,
      (newest_row["rx_time"], newest_row["rx_time"], newest_row["id"], *scope_params),
    ).fetchone() is not None

  return {
    "meta": {
      "limit": limit,
      "total": total,
      "has_more_older": has_more_older,
      "has_more_newer": has_more_newer,
      "channel_index": None if is_dm else channel_index,
      # Which conversation this page is, when it is one. Reported for the same
      # reason channel_index is: a caller holding a page should not have to
      # remember what it asked for to know what it got.
      "peer": peer if is_dm else None,
      "max_messages": max_messages,
      # The cursors to hand back for the next page in either direction, so no
      # caller has to reassemble a pair out of the rows and risk dropping the id.
      "oldest": cursor_of(rows[0]) if rows else None,
      "newest": cursor_of(rows[-1]) if rows else None,
    },
    "messages": rows,
  }




def cursor_of(message: dict[str, Any]) -> tuple[int, int]:
  """The `(rx_time, id)` cursor identifying one message's place in the archive."""
  return (message["rx_time"], message["id"])




def fetch_message(
  conn: sqlite3.Connection,
  message_id: int,
  is_dm: bool = False,
) -> Optional[dict[str, Any]]:
  """A single message by message_id, with its channel and node names resolved."""
  table, extra_columns = _message_table(is_dm)

  # Only channel messages have a channel to name.
  channel_column = "NULL AS channel_name" if is_dm else "c.name AS channel_name"
  channel_join = (
    "" if is_dm
    else "LEFT JOIN channels c ON m.channel_index = c.channel_index"
  )

  row = conn.execute(
    f"""
    SELECT {extra_columns}, {channel_column}, {_MESSAGE_COLUMNS}
    FROM {table} m
    LEFT JOIN nodes n ON m.from_node = n.node_id
    {channel_join}
    LEFT JOIN {table} parent ON m.reply_to = parent.message_id
    LEFT JOIN nodes tn ON m.to_node = tn.node_id
    LEFT JOIN nodes pn ON parent.from_node = pn.node_id
    WHERE m.message_id = ?
    """,
    (message_id,),
  ).fetchone()

  return dict(row) if row else None




def fetch_nodes(
  conn: sqlite3.Connection,
  limit: int = 50,
  offset: int = 0,
  search: Optional[str] = None,
  list_unnamed: bool = False,
) -> dict[str, Any]:
  """One page of nodes, most recently seen first, optionally filtered.

  The search terms are parenthesised because `_node_where` ANDs what it is given: a
  bare OR chain would bind as `id LIKE ? OR name LIKE ? OR (name LIKE ? AND named)`
  and match unnamed nodes by id after all. Searching an id is exactly how an unnamed
  node would otherwise be stumbled onto, so the filter has to survive it — one
  switch, one meaning, list and count and filter box together.
  """
  limit = max(0, min(limit, 1000))
  offset = max(0, offset)

  if search:
    pattern = f"%{search}%"
    match_clause = _node_where(
      "(node_id LIKE ? OR short_name LIKE ? OR long_name LIKE ?)",
      list_unnamed=list_unnamed,
    )
    match_params: tuple[Any, ...] = (pattern, pattern, pattern)
  else:
    match_clause = _node_where(list_unnamed=list_unnamed)
    match_params = ()

  total = conn.execute(
    f"SELECT COUNT(*) AS count FROM nodes {match_clause}",
    match_params,
  ).fetchone()["count"]

  rows = conn.execute(
    f"""
    SELECT {_NODE_COLUMNS}
    FROM nodes
    {match_clause}
    ORDER BY last_seen DESC
    LIMIT ? OFFSET ?
    """,
    (*match_params, limit, offset),
  ).fetchall()

  return {
    "meta": {
      "limit": limit,
      "offset": offset,
      "total": total,
      "search": search,
    },
    "nodes": [dict(row) for row in rows],
  }




def fetch_node(conn: sqlite3.Connection, node_id: str) -> Optional[dict[str, Any]]:
  """A single node by its hex id. **Ignores LIST_UNNAMED_NODES, always.**

  That flag is about discovery — meeting a node nobody named while reading a list —
  and this is resolution: the caller already holds the id. Do not add `_node_where`
  to this query, or to `fetch_nodes_by_id` below.
  """
  row = conn.execute(
    f"SELECT {_NODE_COLUMNS} FROM nodes WHERE node_id = ?",
    (node_id,),
  ).fetchone()

  return dict(row) if row else None




def fetch_nodes_by_id(
  conn: sqlite3.Connection,
  node_ids: list[str],
) -> dict[str, dict[str, Any]]:
  """The current state of some named nodes, keyed by hex id.

  **Named rather than paged, which is the whole point of it.** `fetch_nodes` answers
  "what are the most recently heard nodes?", and its answer moves: it orders by
  `last_seen DESC`, so the node that was row 30 a minute ago may be row 12 now. The
  sidebar needs the other question — "what do the rows I am already showing say
  today?" — because refreshing them by re-reading a page would silently swap which
  node each row is about, under a cursor the reader is using.

  A node that is not in the archive is absent from the result rather than present
  and empty. Pruned is a real case, and the caller leaves that row alone rather
  than blanking it.

  Returned as a dict because every caller wants it by id: `IN` does not preserve
  the order it was given, and a list would have to be re-indexed by every one of
  them.
  """
  if not node_ids:
    return {}

  # Chunked because SQLITE_MAX_VARIABLE_NUMBER is a real ceiling — 999 on builds
  # older than 3.32 — and NODE_SEARCH_LIMIT already lets a thousand rows be
  # loaded. Two round trips beats an OperationalError at exactly the list size a
  # wide filter produces.
  nodes: dict[str, dict[str, Any]] = {}

  for start in range(0, len(node_ids), 500):
    chunk = node_ids[start:start + 500]
    placeholders = ", ".join("?" for _ in chunk)
    rows = conn.execute(
      f"SELECT {_NODE_COLUMNS} FROM nodes WHERE node_id IN ({placeholders})",
      tuple(chunk),
    ).fetchall()

    for row in rows:
      node = dict(row)
      nodes[node["node_id"]] = node

  return nodes




def newest_cursor(
  conn: sqlite3.Connection,
  is_dm: bool = False,
  channel_index: Optional[int] = None,
  *,
  peer: Optional[str] = None,
) -> Optional[tuple[int, int]]:
  """The cursor of the newest message in a channel, or None when it holds none.

  This is the poll. It rides the covering index the collector already maintains
  — the ORDER BY matches the index's own order, so it reads one row — and WAL
  means it never blocks the writer, so asking every few seconds is cheap.

  It returns the same `(rx_time, id)` pair the fetch takes, rather than a bare
  MAX(rx_time), so that "has anything arrived?" is answered exactly: a message
  landing in the same second as the current newest moves the id even though it
  leaves the timestamp alone.

  **It takes the same `peer` the page does, and has to.** Without it the poll
  answers about the whole direct message table, so a message from any other peer
  reads as new in the conversation on screen — and the reader gets a page fetched
  for a message that is not in it.
  """
  table, _ = _message_table(is_dm)

  parts, params = _scope_clauses(is_dm, channel_index, peer)
  where = ("WHERE " + " AND ".join(parts)) if parts else ""

  row = conn.execute(
    f"""
    SELECT m.rx_time, m.id FROM {table} m
    {where}
    ORDER BY m.rx_time DESC, m.id DESC
    LIMIT 1
    """,
    params,
  ).fetchone()

  return (row["rx_time"], row["id"]) if row else None
