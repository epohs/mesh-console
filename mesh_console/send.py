"""The sending tier: asking mesh-collector to transmit, and nothing more.

This is the only module in the project that imports `mesh_link`, and it is
neither of the other two tiers. It is not the read tier — `db/` opens the archive
`mode=ro` and has no business knowing a socket exists. It is not the interface —
nothing here draws anything, and every function below is callable and testable
with no terminal attached, which is the same argument that keeps `state.py` at the
package root. Importing this module reaches for no database and no terminal.

**This project never transmits.** It asks the collector to, over a Unix socket,
and the collector is the one holding the serial port and the only process that
writes the archive — including the row for a message sent from here. What comes
back is a report of what the collector did.

`mesh_link` is an optional dependency (`uv sync --extra send`), so importing this
module raises ImportError on a read-only install. That is deliberate: the caller
guards the import and offers no compose box when it fails, which makes a
read-only console read-only by what is installed rather than by a runtime flag.

Every failure leaves here as a `SendFailed` carrying a code and a sentence
written for a person. `mesh_link`'s own exception types stop at this boundary, so
the interface never imports the protocol to find out what went wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from mesh_link import (
  BROADCAST,
  ERR_BAD_RESPONSE,
  ERR_BUSY,
  ERR_CHANNEL_NOT_TRACKED,
  ERR_FRAME_TOO_LARGE,
  ERR_INTERNAL,
  ERR_INVALID_REQUEST,
  ERR_SEND_FAILED,
  ERR_TIMEOUT,
  ERR_TX_DISABLED,
  ERR_UNREACHABLE,
  ERR_UNSUPPORTED_VERSION,
  ControlClient,
  ControlError,
  MAX_TEXT_BYTES,
  resolve_socket_path,
)

from mesh_console.config import Config


# How long to wait for the collector to answer a send. The collector's drain is
# the only thread that touches the radio, so a send queues behind any other send
# in flight and then waits on the radio itself. mesh-link's own default is 35s
# and there is no reason to disagree with it.
SEND_TIMEOUT = 35.0

# Asking whether anything is listening is a connect, a status frame and a reply
# over a local socket, with nothing going on the air. If that cannot be answered
# quickly it is not going to be answered.
PROBE_TIMEOUT = 2.0


# What a failure means, and what to do about it. Keyed on mesh_link's codes,
# which exist precisely so a client can tell these apart without matching on
# message text. "Nothing is listening" and "that channel is not archived" want
# different sentences, and none of these may read like the archive having gone
# away — that is a different failure with a different remedy, and it has its own
# banner in the interface.
#
# **The keys are mesh_link's constants rather than the strings behind them**, so
# a code renamed there fails this import instead of quietly dropping a sentence
# into FALLBACK_ADVICE — which is a regression nothing would report, because the
# fallback is a perfectly ordinary-looking message. Two of them, ERR_UNREACHABLE
# and ERR_BAD_RESPONSE, are the client's own rather than the protocol's; they were
# added to mesh_link's exports for exactly this, since a caller meets them as
# often as it meets the rest.
#
# Re-read for direct messages rather than extended, which is what they needed:
# every sentence below is still true of one, since none of them names a broadcast
# or a channel except ERR_CHANNEL_NOT_TRACKED — and the collector skips that check
# entirely for a direct destination, so that code can no longer come back for one.
# It stays in the table because it is still reachable for a channel message, and a
# code with no sentence would fall through to FALLBACK_ADVICE.
FAILURE_ADVICE = {
  ERR_UNREACHABLE: (
    "No collector is listening. It has to be running with ENABLE_TX on, as the "
    "same user, for anything to send."
  ),
  ERR_TX_DISABLED: (
    "The collector is running but will not transmit. ENABLE_TX is off there."
  ),
  ERR_CHANNEL_NOT_TRACKED: (
    "The collector does not archive that channel, so it refuses to send on it — "
    "a message there would be visible to nobody."
  ),
  ERR_SEND_FAILED: "The radio refused the message. Nothing went out.",
  ERR_INVALID_REQUEST: "The collector rejected the message as malformed.",
  ERR_BUSY: "The collector has more sends queued than it will hold. Try again.",
  ERR_TIMEOUT: "The collector accepted the message but did not report back in time.",
  ERR_UNSUPPORTED_VERSION: (
    "This console and that collector disagree about the mesh-link protocol. "
    "Upgrade whichever is behind."
  ),
  ERR_FRAME_TOO_LARGE: "The message was too large to send.",
  ERR_BAD_RESPONSE: "The collector answered with something this console cannot read.",
  ERR_INTERNAL: "The collector hit an error it could not describe.",
}

FALLBACK_ADVICE = "The collector refused the message."




class SendFailed(RuntimeError):
  """A send that did not happen, or that cannot be confirmed to have happened.

  `code` is `mesh_link`'s, so a caller can branch on it; `detail` is what the
  collector said, kept because it often names the actual device error. `advice`
  is the sentence to put in front of a person.
  """


  def __init__(self, code: str, detail: str) -> None:
    self.code = code
    self.detail = detail
    self.advice = FAILURE_ADVICE.get(code, FALLBACK_ADVICE)

    super().__init__(f"{self.advice} ({detail})")




def send_enabled() -> bool:
  """Whether this console has been configured to offer sending at all.

  The first of three independent gates, and the cheapest: it touches nothing.
  The second is the collector's published `accepts_transmit`, which says a socket
  was being served when it last started; the third is asking whether one is
  answering now, which is the only authoritative one. All three fail closed, and
  the interface requires all three.
  """
  return bool(Config.get("ENABLE_SEND", False))




def socket_path() -> Path:
  """Where to look for the collector's control socket.

  Unset means the platform default `mesh_link` resolves, which is the same one
  the collector uses — so a collector and a console run by the same user on the
  same host agree without either being configured. They are the only pair that
  can: the default lives under the user's own runtime directory, so a collector
  running as somebody else needs this set on both sides.
  """
  return resolve_socket_path(Config.get("CONTROL_SOCKET_PATH") or None)




def text_byte_length(text: str) -> int:
  """What the 233-byte cap actually counts.

  The limit is bytes of UTF-8, not characters, so a message of emoji runs out
  roughly four times sooner than the same length of English. Counting characters
  and hoping would mean a message rejected after it was written.
  """
  return len(text.encode("utf-8"))




def fits_in_one_packet(text: str) -> bool:
  return text_byte_length(text) <= MAX_TEXT_BYTES




class Sender:
  """A client of one collector's control socket.

  Both methods block: they open a socket, write, and wait. **Neither belongs on
  an event loop.** The read tier's queries run there because each is a fast
  indexed read of a local file; that reasoning does not survive a round trip that
  waits on a radio.
  """


  def __init__(
    self,
    path: Optional[Path] = None,
    *,
    timeout: float = SEND_TIMEOUT,
  ) -> None:
    self.path = path if path is not None else socket_path()
    self.timeout = timeout




  def is_available(self) -> bool:
    """Whether a collector is answering right now, and willing to transmit.

    Puts nothing on the air — it asks for status, which exists so this question
    can be answered without keying up. A collector that is listening but has
    transmitting off answers `accepts_transmit: false`, and that is a no: the
    point of asking is to find out whether offering a compose box would be
    honest.
    """
    try:
      with ControlClient(self.path, timeout=PROBE_TIMEOUT) as link:
        status = link.status()
    except ControlError:
      return False
    except OSError:
      return False

    return bool(status.get("accepts_transmit"))




  def send_to_channel(
    self,
    text: str,
    channel_index: int,
    *,
    reply_to: Optional[int] = None,
    emoji: Optional[bool] = None,
  ) -> dict[str, Any]:
    """Ask the collector to broadcast one message on one channel.

    `want_ack` is left alone: `mesh_link` defaults it by destination, and acks are
    wasted airtime on a broadcast.

    The result carries the `message_id` the radio assigned and whether the
    collector archived the row. Both matter to a caller — the id because it is
    what a reply would reference, and `archived` because a message that went out
    without being written will never appear in the message list, and saying
    nothing about that would look like a failed send.
    """
    try:
      with ControlClient(self.path, timeout=self.timeout) as link:
        return link.send_text(
          text,
          destination=BROADCAST,
          channel_index=channel_index,
          reply_to=reply_to,
          emoji=emoji,
        )
    except ControlError as e:
      raise SendFailed(e.code, e.message) from e
    except OSError as e:
      # Anything the client did not already turn into a ControlError. Reported as
      # unreachable because that is what it is: this end could not complete the
      # exchange, and nothing is known to have been sent.
      raise SendFailed(ERR_UNREACHABLE, str(e)) from e




  def send_to_peer(
    self,
    text: str,
    node_id: str,
    *,
    channel_index: int = 0,
    reply_to: Optional[int] = None,
    emoji: Optional[bool] = None,
  ) -> dict[str, Any]:
    """Ask the collector to send one message to one node.

    **`want_ack` is not passed, and must not be.** `mesh_link` resolves it from the
    destination — `SendTextRequest.resolve_want_ack()` returns `is_direct` when it
    is None — so a direct message gets an ack and a broadcast does not, decided in
    one place with a check of its own in the mesh-link suite. Passing it explicitly
    here would let this console quietly disagree with the library about it.

    `channel_index` is the encryption context rather than the destination: the
    collector forwards it to `sendText` unchanged, and a direct message still rides
    on a channel. The caller reads the collector's published `primary_channel` for
    it instead of taking the protocol's default of 0, which is right on a device
    whose primary is 0 and wrong on one where it is not.

    `node_id` is validated by `mesh_link` rather than here, and strictly: `^all` or
    `!` plus eight lowercase hex digits, which are the two forms meshtastic's
    `_sendPacket` resolves without consulting its node database. That is not
    cosmetic — the branch it keeps a typo away from calls `sys.exit()`. A
    well-formed id belonging to a node this archive has never heard of is fine and
    is how a mesh works.
    """
    try:
      with ControlClient(self.path, timeout=self.timeout) as link:
        return link.send_text(
          text,
          destination=node_id,
          channel_index=channel_index,
          reply_to=reply_to,
          emoji=emoji,
        )
    except ControlError as e:
      raise SendFailed(e.code, e.message) from e
    except OSError as e:
      raise SendFailed(ERR_UNREACHABLE, str(e)) from e




__all__ = [
  "FAILURE_ADVICE",
  "MAX_TEXT_BYTES",
  "PROBE_TIMEOUT",
  "SEND_TIMEOUT",
  "SendFailed",
  "Sender",
  "fits_in_one_packet",
  "send_enabled",
  "socket_path",
  "text_byte_length",
]
