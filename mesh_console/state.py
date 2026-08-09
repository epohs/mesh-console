"""Where this console remembers what you have already read.

**mesh-collector is the only process that writes the archive.** Read position is
this client's business and nobody else's — two people reading the same archive
from two terminals have different unread counts — so it lives in the user's own
state directory, never in a table in the shared database. RxOnly keeps the same
thing in localStorage for the same reason.

The file is `~/.local/state/mesh-console/read-positions.json`, honouring
XDG_STATE_HOME when it is set. It is written whole, through a temporary file and
a rename, so an interrupted write can't leave a half-parsed file behind. It is
also entirely disposable: delete it and every channel simply reads as unread.

There are three kinds of place a reader can be, and each keeps its own position:
a channel, keyed by index; the flat direct message list; and a conversation with
one peer, keyed by that peer's node id. **A scope is named rather than implied.**
It used to be a bool called `is_dm` plus an int called `channel_index`, which was
exactly right while there were two kinds — and became unable to say which of
three you meant the moment conversations existed. Widening `channel_index` to
hold a node id would have left a parameter whose name lied; see
`PHASE-5-HANDOFF.md`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

from pathlib import Path
from typing import Any, Optional


APP_DIR_NAME = "mesh-console"
STATE_FILE_NAME = "read-positions.json"

CHANNELS_KEY = "channels"
DIRECT_MESSAGES_KEY = "direct_messages"
CONVERSATIONS_KEY = "conversations"

# The three kinds of thing a read position can be about. A closed set, defined
# here and passed only by this project's own code, so an unrecognized one is a
# programming mistake rather than bad input — and it raises, because a scope typo
# that silently stopped recording positions would look exactly like a feature
# that works.
SCOPE_CHANNEL = "channel"
SCOPE_DIRECT = "direct"
SCOPE_CONVERSATION = "conversation"

SCOPES = (SCOPE_CHANNEL, SCOPE_DIRECT, SCOPE_CONVERSATION)




def state_dir() -> Path:
  """The directory this console keeps its own state in."""
  xdg_state = os.environ.get("XDG_STATE_HOME")
  base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
  return base / APP_DIR_NAME




def state_file() -> Path:
  return state_dir() / STATE_FILE_NAME




class ReadPositions:
  """The last message read in each channel, conversation and the DM list.

  **The flat direct message list and a conversation have separate positions over
  the same rows, and that is deliberate.** A position is a high-water mark over
  one ordering, and the flat list's ordering interleaves every peer — so sweeping
  a conversation's marker into the flat one would have to claim every *other*
  peer's older messages as read on the way past, which is precisely the
  over-marking that "the cursor is the read marker" exists to avoid. Two markers
  is the price of not over-marking, and each is honest about what its own view has
  actually shown you. The visible consequence is that reading HILL's thread does
  not clear the flat list's unread count; `PHASE-5-HANDOFF.md` says so out loud.

  A position is `{message_id, rx_time, id}` rather than a bare id, because
  ordering the archive by message_id would order it by the packet ids the mesh
  hands out rather than by the times the messages carry, and because a pruned id
  has to be recognizable as gone.

  **All three fields are load-bearing and they do different jobs.** `message_id`
  is how the message is found again — it is what `fetch_message` takes, and what
  survives the archive being rebuilt underneath a stored position. `rx_time` and
  `id` are the archive's own sort key, the same `(rx_time, id)` pair `cursor_of`
  returns, and they are what "later than" means here.

  Ordering by `(rx_time, message_id)` was the earlier arrangement and it was
  wrong in a way nothing exercised until unread counts arrived. rx_time is whole
  seconds off the mesh, so ties are routine, and a packet id is effectively
  random — so of two messages sharing a second, the later one has a lower
  message_id about half the time. `set()` would then refuse to advance onto it,
  because it looked like moving backwards, and the channel would sit one message
  short of read for good. Invisible while nothing counted the difference; a
  permanent "1 unread" once something did.

  A stored position with no `id` is discarded rather than migrated. Nothing is
  deployed, this file is explicitly disposable, and the cost of dropping one is
  that a channel reads as unread — which is also the state of a first run.

  **The file's shape gained a key rather than changing one.** `PHASE-5-BRIEF.md`
  expected the stored `direct_messages` position to be discarded, on the
  assumption that a conversation list would replace the flat list it describes.
  The flat list was kept instead, so that position still describes the same view
  over the same rows in the same order and is still correct — nothing changed
  meaning, so there is nothing to discard. A state file written before
  conversations existed resumes channels and the flat list exactly as it did, and
  every conversation reads as unread, which is the state of a first run.
  """


  def __init__(self, positions: Optional[dict[str, Any]] = None) -> None:
    self._channels: dict[int, dict[str, int]] = {}
    self._direct: Optional[dict[str, int]] = None
    self._conversations: dict[str, dict[str, int]] = {}

    if positions:
      self._adopt(positions)




  @classmethod
  def load(cls) -> "ReadPositions":
    """Read the state file, or start empty when there isn't one yet.

    An unreadable or malformed file is not an error worth interrupting a reading
    session for: the worst case is that channels look unread, which is also the
    state of a first run.
    """
    path = state_file()

    try:
      raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
      return cls()
    except (OSError, ValueError) as e:
      logging.warning("Ignoring unreadable read positions at %s: %s", path, e)
      return cls()

    if not isinstance(raw, dict):
      logging.warning("%s is not a JSON object; ignoring it", path)
      return cls()

    return cls(raw)




  def _adopt(self, positions: dict[str, Any]) -> None:
    """Take in whatever parsed, discarding anything that isn't a position."""
    channels = positions.get(CHANNELS_KEY)
    if isinstance(channels, dict):
      for key, value in channels.items():
        position = _clean_position(value)
        if position is None:
          continue
        try:
          self._channels[int(key)] = position
        except (TypeError, ValueError):
          continue

    self._direct = _clean_position(positions.get(DIRECT_MESSAGES_KEY))

    conversations = positions.get(CONVERSATIONS_KEY)
    if isinstance(conversations, dict):
      for key, value in conversations.items():
        position = _clean_position(value)
        # A peer key is a node id and stays a string — unlike a channel index,
        # there is nothing to coerce, and anything that is not a string is not a
        # node id.
        if position is None or not isinstance(key, str) or not key:
          continue
        self._conversations[key] = position




  def get(self, scope: str, key: Optional[Any] = None) -> Optional[dict[str, int]]:
    """The stored position for one scope, or None if it has never been read.

    `key` is a channel index for SCOPE_CHANNEL, a peer's node id for
    SCOPE_CONVERSATION, and unused for SCOPE_DIRECT, which is a single view and
    therefore a single position.
    """
    if scope == SCOPE_DIRECT:
      return self._direct

    if scope == SCOPE_CHANNEL:
      if key is None:
        return None
      return self._channels.get(key)

    if scope == SCOPE_CONVERSATION:
      if not key:
        return None
      return self._conversations.get(key)

    raise ValueError(f"unknown read-position scope {scope!r}; expected one of {SCOPES}")




  def set(
    self,
    scope: str,
    key: Optional[Any],
    message_id: int,
    rx_time: int,
    row_id: int,
  ) -> bool:
    """Record a position, but only ever move it forward.

    Returns whether anything changed, so a caller can skip the write. Scrolling
    back through old messages must not un-read the newer ones already seen —
    which is also why the comparison falls back to `id`: several messages can
    share an rx_time, and `id` is how the archive itself breaks that tie.
    """
    position = {
      "message_id": int(message_id),
      "rx_time": int(rx_time),
      "id": int(row_id),
    }
    current = self.get(scope, key)

    if current is not None and not _is_after(position, current):
      return False

    if scope == SCOPE_DIRECT:
      self._direct = position
      return True

    if scope == SCOPE_CHANNEL:
      if key is None:
        return False
      self._channels[key] = position
      return True

    # SCOPE_CONVERSATION — `get` above has already rejected an unknown scope.
    if not key:
      return False
    self._conversations[key] = position
    return True




  def cursor(
    self,
    scope: str,
    key: Optional[Any] = None,
  ) -> Optional[tuple[int, int]]:
    """The `(rx_time, id)` cursor to count unread messages after.

    None means this channel has never been read, so everything in it is unread.

    This replaced `unread_from()`, which returned the rx_time alone and had no
    caller. A bare timestamp cannot express "after this message": counting
    `rx_time > position` loses every message tied with the read marker, and
    counting `>=` counts the marker itself. The pair is what `fetch_message_page`
    compares and what `cursor_of` returns, so a count and a page now agree about
    where the reader is.
    """
    position = self.get(scope, key)
    if position is None:
      return None
    return (position["rx_time"], position["id"])




  def conversation_cursors(self) -> dict[str, tuple[int, int]]:
    """Every conversation's cursor, for counting unread per peer in one query.

    Keyed by peer, in the shape `fetch_unread_conversation_counts` takes — the
    same arrangement `unread_counts()` uses for channels. A peer absent from this
    has never been read, and every message in that conversation is unread.
    """
    return {
      peer: (position["rx_time"], position["id"])
      for peer, position in self._conversations.items()
    }




  def to_dict(self) -> dict[str, Any]:
    payload: dict[str, Any] = {
      CHANNELS_KEY: {
        str(index): position for index, position in sorted(self._channels.items())
      },
    }
    if self._direct is not None:
      payload[DIRECT_MESSAGES_KEY] = self._direct
    if self._conversations:
      payload[CONVERSATIONS_KEY] = {
        peer: position for peer, position in sorted(self._conversations.items())
      }
    return payload




  def save(self) -> None:
    """Write the whole file atomically. Failure is logged, never raised.

    Losing a read position is a smaller harm than dropping the user out of the
    interface, and this is the one place the console writes anything at all.
    """
    path = state_file()

    try:
      path.parent.mkdir(parents=True, exist_ok=True)

      # NamedTemporaryFile in the target directory, so the rename stays on one
      # filesystem and is therefore atomic.
      with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{STATE_FILE_NAME}.",
        delete=False,
      ) as handle:
        json.dump(self.to_dict(), handle, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)

      temp_path.replace(path)

    except OSError as e:
      logging.warning("Could not save read positions to %s: %s", path, e)




def _clean_position(value: Any) -> Optional[dict[str, int]]:
  """Accept a `{message_id, rx_time, id}` triple of integers, or nothing.

  A position missing any of the three is discarded rather than repaired. `id` in
  particular is what makes the ordering the archive's own, and a position
  without it cannot be compared against one that has it — see the class
  docstring for why guessing was the worse option.
  """
  if not isinstance(value, dict):
    return None

  fields = {}
  for name in ("message_id", "rx_time", "id"):
    field = value.get(name)
    if not isinstance(field, int) or isinstance(field, bool):
      return None
    fields[name] = field

  return fields




def _is_after(candidate: dict[str, int], current: dict[str, int]) -> bool:
  """Whether candidate sits later in the archive than current.

  `(rx_time, id)`, which is the order `fetch_message_page` reads in and the
  order `cursor_of` describes. Ordering on message_id instead put this out of
  step with the archive across an rx_time tie — see the class docstring.
  """
  if candidate["rx_time"] != current["rx_time"]:
    return candidate["rx_time"] > current["rx_time"]
  return candidate["id"] > current["id"]
