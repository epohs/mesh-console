"""What counts as a tapback, and how a message row shows the ones it collected.

A tapback is a reaction: a reply whose text is nothing but emoji. RxOnly groups
them onto the message they answer instead of listing them as messages, and this
does the same — see `messages.js`'s `is_tapback`, `is_emoji_only` and
`render_tapbacks`, which are what the rules below mirror.

**This is its own module rather than more of `ui/format.py` on purpose.** Deciding
whether a string is emoji is not formatting, and doing it without
`\\p{Extended_Pictographic}` or a grapheme segmenter takes about six times as much
code as it takes interface. Behind `is_tapback()`, `group_tapbacks()` and
`format_tapback_line()` none of that is visible; `format.py` stays about strings.

### Why there is a table here instead of a dependency

RxOnly gets both primitives from the browser: `Intl.Segmenter` for grapheme
clusters and `\\p{Extended_Pictographic}` for the character test. **Python has
neither.** `re` rejects `\\p{...}` outright, there is no grapheme segmenter in the
standard library, and `unicodedata.category()` is not a substitute — it answers
`So` for `👍` and for `⌘`, which is not an emoji, so a category test says every
`⌘` is a reaction. The third-party `regex` package would supply both exactly.

Jason's call was the table, so that `mesh-console` keeps the single required
dependency two suite checks assert the shape of. The cost is bounded: when this
is wrong, a tapback renders as an ordinary message row with its reply bar, which
is what every one of them did before this module existed.

### What the scanner does not do

It is not UAX #29. It handles the sequences a reaction is actually made of —
a lone pictograph, a variation selector, a skin-tone modifier, a ZWJ sequence, a
regional-indicator pair, an enclosing keycap and a tag sequence — and it ignores
`Prepend` and `SpacingMark`, which no emoji uses. Getting a cluster boundary wrong
inside ordinary text costs nothing, because text fails the pictographic test
whatever way it is divided; the only thing at risk is the 1-to-3 count, and only
for a string already made entirely of emoji.

**Flags and keycaps are deliberately not tapbacks**, which is not an oversight but
a mirror: `🇺🇸` is a pair of regional indicators and `1️⃣` starts with the digit
one, and neither of those characters is Extended_Pictographic, so RxOnly's own
test rejects both. The two readers agree, and they agree for the same reason.
"""

from __future__ import annotations

import unicodedata

from bisect import bisect_right
from typing import Any, Optional


# A reaction is one to three emoji. Four is a short message, and RxOnly draws the
# line in the same place (`is_emoji_only`, messages.js:47).
MAX_TAPBACK_CLUSTERS = 3

# More than this many of one emoji collapse into a single count pill, which is
# then not a link to any one of them. Matches render_tapbacks, messages.js:177.
GROUP_THRESHOLD = 5

# Pills shown before the rest become "+N more". Matches max_pills, messages.js:174.
MAX_PILLS = 10

ZWJ = "‍"


# Extended_Pictographic, from Unicode's `emoji-data.txt`. Every emoji that can
# lead a cluster is in here, and — importantly — three kinds of character that
# travel with emoji are *not*: skin-tone modifiers (1F3FB..1F3FF, which is why the
# range above them stops at 1F3FA), regional indicators (1F1E6..1F1FF, likewise),
# and variation selectors. Those are components, not pictographs, so a cluster is
# tested for containing one of these rather than consisting only of them.
_PICTOGRAPHIC_RANGES: tuple[tuple[int, int], ...] = (
  (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049),
  (0x2122, 0x2122), (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA),
  (0x231A, 0x231B), (0x2328, 0x2328), (0x2388, 0x2388), (0x23CF, 0x23CF),
  (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB),
  (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x2605),
  (0x2607, 0x2612), (0x2614, 0x2685), (0x2690, 0x2705), (0x2708, 0x2712),
  (0x2714, 0x2714), (0x2716, 0x2716), (0x271D, 0x271D), (0x2721, 0x2721),
  (0x2728, 0x2728), (0x2733, 0x2734), (0x2744, 0x2744), (0x2747, 0x2747),
  (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757),
  (0x2763, 0x2767), (0x2795, 0x2797), (0x27A1, 0x27A1), (0x27B0, 0x27B0),
  (0x27BF, 0x27BF), (0x2934, 0x2935), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C),
  (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D),
  (0x3297, 0x3297), (0x3299, 0x3299),
  (0x1F000, 0x1F0FF), (0x1F10D, 0x1F10F), (0x1F12F, 0x1F12F),
  (0x1F16C, 0x1F171), (0x1F17E, 0x1F17F), (0x1F18E, 0x1F18E),
  (0x1F191, 0x1F19A), (0x1F1AD, 0x1F1E5), (0x1F201, 0x1F20F),
  (0x1F21A, 0x1F21A), (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A),
  (0x1F23C, 0x1F23F), (0x1F249, 0x1F3FA), (0x1F400, 0x1F53D),
  (0x1F546, 0x1F64F), (0x1F680, 0x1F6FF), (0x1F774, 0x1F77F),
  (0x1F7D5, 0x1F7FF), (0x1F80C, 0x1F80F), (0x1F848, 0x1F84F),
  (0x1F85A, 0x1F85F), (0x1F888, 0x1F88F), (0x1F8AE, 0x1F8FF),
  (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945), (0x1F947, 0x1FAFF),
  (0x1FC00, 0x1FFFD),
)

# Bisected rather than walked: the table is sorted and disjoint, so a lookup is
# one binary search instead of eighty comparisons per character.
_RANGE_STARTS = tuple(lo for lo, _ in _PICTOGRAPHIC_RANGES)




def _is_pictographic(char: str) -> bool:
  """Whether one character is Extended_Pictographic."""
  code = ord(char)
  index = bisect_right(_RANGE_STARTS, code) - 1
  if index < 0:
    return False
  return code <= _PICTOGRAPHIC_RANGES[index][1]




def _is_regional_indicator(char: str) -> bool:
  return 0x1F1E6 <= ord(char) <= 0x1F1FF




def _is_extending(char: str) -> bool:
  """Whether a character attaches to the cluster before it rather than starting one.

  The four explicit ranges are what emoji are built out of and what
  `unicodedata` will not tell you: a variation selector is `Mn` only some of the
  time, a skin-tone modifier is `Sk`, a keycap is `Me`, and a tag character is
  `Cf`. The category test after them catches ordinary combining marks, which a
  reaction will not contain but a message might.
  """
  code = ord(char)

  if 0xFE00 <= code <= 0xFE0F:      # variation selectors: ❤ vs ❤️
    return True
  if 0x1F3FB <= code <= 0x1F3FF:    # skin-tone modifiers
    return True
  if code == 0x20E3:                # combining enclosing keycap
    return True
  if 0xE0020 <= code <= 0xE007F:    # tag characters, as in subdivision flags
    return True

  return unicodedata.category(char) in ("Mn", "Me")




def grapheme_clusters(text: str) -> list[str]:
  """Split text into what a reader sees as single characters.

  Enough of UAX #29 for emoji and no more — see the module docstring for what is
  left out and why that is safe here.
  """
  clusters: list[str] = []
  index = 0
  length = len(text)

  while index < length:
    start = index

    if _is_regional_indicator(text[index]):
      # A flag is exactly two of these. A third starts a new flag rather than
      # joining this one, which is why this takes a pair and not a run.
      index += 1
      if index < length and _is_regional_indicator(text[index]):
        index += 1
    else:
      index += 1

    while index < length and _is_extending(text[index]):
      index += 1

    # A zero-width joiner welds what follows onto this cluster, however many
    # times it happens: 👨‍👩‍👧 is one family, not three people.
    while index < length and text[index] == ZWJ:
      index += 1
      if index >= length:
        break
      index += 1
      while index < length and _is_extending(text[index]):
        index += 1

    clusters.append(text[start:index])

  return clusters




def is_emoji_only(text: Optional[str]) -> bool:
  """Whether text is one to three emoji and nothing else.

  Mirrors `is_emoji_only` in RxOnly's messages.js, including the bound: a
  cluster passes when it *contains* a pictographic character rather than when
  every character in it is one, because a skin tone and a variation selector are
  components and would fail the stricter test.
  """
  if not text:
    return False

  trimmed = text.strip()
  if not trimmed:
    return False

  clusters = grapheme_clusters(trimmed)
  if not 1 <= len(clusters) <= MAX_TAPBACK_CLUSTERS:
    return False

  return all(
    any(_is_pictographic(char) for char in cluster)
    for cluster in clusters
  )




def is_tapback(message: dict[str, Any]) -> bool:
  """Whether a message is a reaction to another one rather than a message.

  A reaction is always a reply, so replying to nothing settles it: emoji-only
  text addressed to the channel is somebody saying `👍` out loud, and it stays a
  message.

  Past that, the archive may simply know. Schema 0.10.0 records the firmware's
  own emoji flag, and where it is present it decides — the sending client said
  what it was doing, and is_emoji_only() only ever inferred it from the text.
  The inference is wrong in both directions: a deliberate one-emoji reply reads
  as a reaction, and a client that reacts with something this function does not
  count as emoji-only does not.

  `emoji is None` is a row written before 0.10.0, whose flag was never recorded
  and is deliberately never backfilled. The heuristic remains the whole answer
  for those rows and only for those — tested with `is None` rather than
  falsiness, because a recorded 0 is an answer and means "not a reaction".

  Mirrors `is_tapback` in RxOnly's messages.js, flag-first branch included. The
  two are kept in step by hand; neither imports the other.
  """
  if message.get("reply_to") is None:
    return False

  emoji = message.get("emoji")
  if emoji is not None:
    return emoji == 1

  return is_emoji_only(message.get("text"))




def group_tapbacks(tapbacks: list[dict[str, Any]]) -> list[str]:
  """The pills for one message's reactions, in the order they should print.

  RxOnly's rules, kept: oldest first, grouped by the emoji itself, a group of
  more than five collapsed to a count, at most ten pills and then a `+N more`.
  The one difference is what a pill is — a terminal has no link to hang on it, so
  an individual pill carries its author's name and a collapsed one carries the
  number that replaced the names.
  """
  if not tapbacks:
    return []

  ordered = sorted(tapbacks, key=lambda t: (t.get("rx_time") or 0, t.get("id") or 0))

  groups: dict[str, list[dict[str, Any]]] = {}
  for tapback in ordered:
    emoji = (tapback.get("text") or "").strip()
    groups.setdefault(emoji, []).append(tapback)

  pills: list[str] = []
  for emoji, group in groups.items():
    if len(group) > GROUP_THRESHOLD:
      pills.append(f"{emoji} {len(group)}")
      continue
    for tapback in group:
      author = tapback.get("from_node_short_name") or ""
      pills.append(f"{emoji} {author}".strip())

  overflow = len(pills) - MAX_PILLS
  if overflow > 0:
    return pills[:MAX_PILLS] + [f"+{overflow} more"]

  return pills




def format_tapback_line(tapbacks: list[dict[str, Any]]) -> Optional[str]:
  """One line of reactions to sit under a message, or None when there are none."""
  pills = group_tapbacks(tapbacks)
  if not pills:
    return None
  return "   ".join(pills)
