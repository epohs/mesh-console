import sqlite3

from pathlib import Path
from typing import Optional

from mesh_console.config import Config


# The oldest schema this code can read, not the newest it has seen.
#
# mesh-collector owns the schema and writes the version into meta; the two
# projects upgrade independently, so a reader has to say out loud what it needs.
# The rule the version number follows is documented in mesh-collector's
# schema.sql, which is the authority: a MAJOR bump breaks readers, a MINOR bump
# only adds. So the check below accepts any archive with the same major and a
# version at least this high, and this constant moves only when a query here
# starts depending on something newer.
#
# This is 0.8.0 for a concrete reason: `_NODE_COLUMNS` in queries.py selects the
# six telemetry columns 0.8.0 added, so 0.8.0 is the oldest archive this code can
# open. It was 0.7.0 before that, for direct_messages.to_node, and 0.6.0 before
# that. Each move up is the same trade taken knowingly: the reader gains what the
# new column shows and loses the ability to read the archives written before it.
#
# RxOnly moved to 0.8.0 in the same session and for the same reason. That the two
# readers happen to agree right now is not the arrangement changing — the constants
# are still separate, still deliberately not imported from one another, and the next
# column either of them selects alone will part them again.
REQUIRED_SCHEMA = "0.8.0"




class SchemaVersionMismatch(RuntimeError):
  """Raised when the archive's schema isn't one this code can read."""




def _parse_version(version: str) -> Optional[tuple[int, int, int]]:
  """Split 'MAJOR.MINOR.PATCH' into comparable parts, or None if it isn't one."""
  parts = version.strip().split(".")
  if len(parts) != 3:
    return None
  try:
    return (int(parts[0]), int(parts[1]), int(parts[2]))
  except ValueError:
    return None




def is_compatible(archive_version: str, required: str = REQUIRED_SCHEMA) -> bool:
  """Whether an archive at `archive_version` is readable by code needing `required`.

  Same major, and no older than what is required. An unparseable version is not
  compatible with anything: a reader that cannot tell what it is looking at
  should not proceed to select columns from it.
  """
  found = _parse_version(archive_version)
  needed = _parse_version(required)

  if found is None or needed is None:
    return False

  return found[0] == needed[0] and found >= needed




class ArchiveUnavailable(RuntimeError):
  """Raised when the archive cannot be opened at all."""




class ArchiveNotConfigured(ArchiveUnavailable):
  """Raised when DB_PATH has not been set to anything.

  Separate from the archive being unreachable, because the fix is different: one
  means "tell me where it is", the other means "it isn't there".
  """




def archive_path() -> Optional[Path]:
  """The configured archive as an absolute path, or None when DB_PATH is unset.

  `~` is expanded here because SQLite will not do it — a `DB_PATH` of
  `~/mesh-collector/data/db.sqlite` would otherwise be looked for in a directory
  literally named `~`. Relative paths resolve against the working directory once,
  at startup, so a later `chdir` cannot move the archive, and so every error
  message below can name the absolute path that was actually tried.
  """
  raw = (Config.get("DB_PATH") or "").strip()
  if not raw:
    return None
  return Path(raw).expanduser().resolve()




def get_db_connection() -> sqlite3.Connection:
  """Open the archive read-only.

  mesh-collector is the only writer. query_only makes that structural rather
  than a convention: a stray INSERT in this project fails at the connection
  rather than reaching the archive.
  """
  path = archive_path()
  if path is None:
    raise ArchiveNotConfigured(
      "DB_PATH is not set, so there is no archive to read. Point it at the "
      "database mesh-collector writes — see config-sample.json, or export "
      "MESH_CONSOLE_DB_PATH."
    )

  # Built through as_uri() rather than by interpolation: a path containing '?'
  # or '#' would otherwise be parsed as a URI query or fragment and the open
  # would fail on a filename that is perfectly legal on disk.
  uri = f"{path.as_uri()}?mode=ro"

  conn = sqlite3.connect(
    uri,
    uri=True,
    timeout=2.5,
  )

  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA query_only = ON;")
  conn.execute("PRAGMA busy_timeout = 2500;")

  return conn




def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
  """Read a single meta value, or None if the collector hasn't published it."""
  row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
  return row["value"] if row else None




def get_meta_int(conn: sqlite3.Connection, key: str, fallback: int) -> int:
  """Read an integer meta value, falling back when it is absent or unparseable.

  The fallback is deliberately conservative: an unpublished limit means we don't
  know what the collector keeps, not that we may assume a generous one.
  """
  value = get_meta(conn, key)
  if value is None:
    return fallback
  try:
    return int(value)
  except ValueError:
    return fallback




def get_meta_bool(conn: sqlite3.Connection, key: str, fallback: bool = False) -> bool:
  """Read a 'true'/'false' meta value. Anything unrecognized takes the fallback."""
  value = get_meta(conn, key)
  if value is None:
    return fallback
  return value.strip().lower() == "true"




def check_schema_version(conn: sqlite3.Connection) -> None:
  """Verify the archive's schema before rendering anything.

  Unlike RxOnly, which warns and serves on when the file is missing, this fails.
  A web app has to come up before the collector has necessarily created the
  archive — startup order isn't its to control — but a person typing this command
  is standing right there, and a terminal that opens onto an empty screen when the
  real answer is "that path is wrong" is worse than one that says so and exits.
  """
  try:
    version = get_meta(conn, "schema_version")
  except sqlite3.Error as e:
    raise SchemaVersionMismatch(
      f"The database at {archive_path()} has no readable meta table "
      f"({e}). Mesh Console reads an archive written by mesh-collector; point "
      f"DB_PATH at one."
    ) from e

  if version is None:
    raise SchemaVersionMismatch(
      f"The database at {archive_path()} does not record a "
      f"schema_version. Mesh Console reads schema {REQUIRED_SCHEMA} or newer, "
      f"written by mesh-collector."
    )

  if not is_compatible(version):
    raise SchemaVersionMismatch(
      f"The database at {archive_path()} is schema {version}; Mesh Console "
      f"needs {REQUIRED_SCHEMA} or a later {REQUIRED_SCHEMA.split('.')[0]}.x. "
      f"Upgrade whichever side is behind — mesh-collector writes the schema, "
      f"this project only reads it."
    )




def open_archive() -> sqlite3.Connection:
  """Open the archive and verify its schema. Called once at startup.

  Everything that can go wrong with the path or the schema goes wrong here, with
  a message naming the path, rather than as an OperationalError from inside a
  widget refresh.
  """
  try:
    conn = get_db_connection()
  except sqlite3.Error as e:
    raise ArchiveUnavailable(
      f"Could not open the archive at {archive_path()}: {e}. "
      f"mesh-collector creates it; point DB_PATH at an existing archive."
    ) from e

  try:
    check_schema_version(conn)
  except Exception:
    conn.close()
    raise

  return conn
