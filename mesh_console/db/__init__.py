"""The read tier: opening the archive, and every question asked of it.

`db` is what both siblings call the layer that talks to SQLite —
`mesh_collector/db/` writes it, `rxonly/web/db.py` reads it — and this is that
layer for this project. Two modules, and this file is the surface both are used
through, so a caller needs to know a name rather than which module holds it:

  - `connection` — how the archive is opened, which schema this code reads, and
    the `meta` accessors the collector's published policy comes back through.
  - `queries` — the reads themselves, in the shapes the interface renders.

**Nothing in here imports the interface, and nothing in here can write.** The
connection is `mode=ro` with `query_only` set, so mesh-collector remains the only
writer by construction rather than by convention. That also means this tier is
usable — and testable — without a terminal attached, which is why the read
position, the one thing this project does write, lives outside it in
`mesh_console.state`.
"""

from mesh_console.db.connection import (
  REQUIRED_SCHEMA,
  ArchiveNotConfigured,
  ArchiveUnavailable,
  SchemaVersionMismatch,
  archive_path,
  check_schema_version,
  get_db_connection,
  get_meta,
  get_meta_bool,
  get_meta_int,
  is_compatible,
  open_archive,
)
from mesh_console.db.queries import (
  FALLBACK_MAX_MESSAGES,
  cursor_of,
  fetch_channels,
  fetch_conversations,
  fetch_latest_rx_time,
  fetch_local_node,
  fetch_message,
  fetch_message_page,
  fetch_node,
  fetch_nodes,
  fetch_nodes_by_id,
  fetch_stats,
  fetch_unread_channel_counts,
  fetch_unread_conversation_counts,
  fetch_unread_direct_count,
  newest_cursor,
)


__all__ = [
  # connection
  "REQUIRED_SCHEMA",
  "ArchiveNotConfigured",
  "ArchiveUnavailable",
  "SchemaVersionMismatch",
  "archive_path",
  "check_schema_version",
  "get_db_connection",
  "get_meta",
  "get_meta_bool",
  "get_meta_int",
  "is_compatible",
  "open_archive",
  # queries
  "FALLBACK_MAX_MESSAGES",
  "cursor_of",
  "fetch_channels",
  "fetch_conversations",
  "fetch_latest_rx_time",
  "fetch_local_node",
  "fetch_message",
  "fetch_message_page",
  "fetch_node",
  "fetch_nodes",
  "fetch_nodes_by_id",
  "fetch_stats",
  "fetch_unread_channel_counts",
  "fetch_unread_conversation_counts",
  "fetch_unread_direct_count",
  "newest_cursor",
]
