"""The palette, taken from RxOnly's web stylesheet.

**Why this file exists.** mesh-console and RxOnly are two mediums showing one
archive, and until now the console inherited whatever theme Textual shipped while
the web app carried its own palette — so the two looked unrelated rather than
like one suite. Every colour below is the same colour as the corresponding CSS
custom property in `rxonly/rxonly/web/static/css/rxonly.css`, converted from
`oklch()` to sRGB hex because Textual has no `oklch()`.

**The mapping is one-to-one and deliberately boring**: `--color-text-muted` in
that stylesheet is `$rx-text-muted` here, same value. When one moves, move the
other; the comment beside each entry is the source it was converted from, so the
check is mechanical rather than a matter of taste.

**Three roles have no counterpart in the web interface**, because RxOnly has
nothing that plays them: the accent bar on a message this device sent, a failed
send, and the connection banner. Those are derived rather than borrowed — same
lightness band and the same modest chroma as the palette, so they read as part of
it. They are marked `derived` below.

Not mirrored, and deliberately: RxOnly stripes odd rows in its lists with
`color-mix(in oklch, var(--color-bg-surface-alt) 25%, transparent)`. Textual's CSS
has no `nth-child`, so a stripe here would have to be applied per row from the
Python that builds the list — a real change to how rows are constructed, for the
faintest cue in the palette. The row cursor already says where you are.
"""

from __future__ import annotations

from textual.theme import Theme


# --- dark, from rxonly.css's `@media (prefers-color-scheme: dark)` block -------

DARK_PALETTE = {
  "rx-bg-app":          "#0D0600",  # --color-bg-app          oklch(0.1309 0.0271 81.12)
  "rx-bg-surface":      "#0D0904",  # --color-bg-surface      oklch(0.1448 0.0141 81.12)
  "rx-bg-surface-alt":  "#262422",  # --color-bg-surface-alt    oklch(0.2617 0.0047 67.62)
  "rx-bg-below-surface":"#2D2822",  # --color-bg-below-surface  oklch(0.28 0.0121 73.06)
  "rx-text-primary":    "#33859F",  # --color-text-primary    oklch(0.578 0.0873 222.49)
  "rx-text-secondary":  "#99D2CD",  # --color-text-secondary  oklch(0.8232 0.059 189.29)
  "rx-text-muted":      "#1A5466",  # --color-text-muted      oklch(0.4172 0.0654 222.5)
  "rx-link":            "#6EB1BD",  # --color-link            oklch(72% 0.07 210)
  "rx-accent-node":     "#EDB442",  # --color-accent-node     oklch(0.8029 0.1422 81.12)
  "rx-accent-channel":  "#C23126",  # --color-accent-channel  oklch(0.5385 0.1835 29.03)
  "rx-outbound":        "#46B68C",  # derived  oklch(0.70 0.12  165)
  "rx-warning":         "#F2823B",  # derived  oklch(0.72 0.16  50) — see the note below
  "rx-error":           "#E04C44",  # derived  oklch(0.62 0.185 27)
  "rx-on-accent":       "#0D0600",  # derived — see the note below
}

# --- light, from the `prefers-color-scheme: light` block ----------------------

LIGHT_PALETTE = {
  "rx-bg-app":          "#F5F3F2",  # --color-bg-app            oklch(0.9659 0.0027 66.71)
  "rx-bg-surface":      "#F0ECE8",  # --color-bg-surface        oklch(0.9451 0.0074 73.06)
  "rx-bg-surface-alt":  "#EEE8E0",  # --color-bg-surface-alt    oklch(0.9332 0.0121 73.06)
  "rx-bg-below-surface":"#D6D0C8",  # --color-bg-below-surface  oklch(0.8591 0.0121 73.06)
  "rx-text-primary":    "#28938D",  # --color-text-primary      oklch(0.6039 0.0939 189.29)
  "rx-text-secondary":  "#33859F",  # --color-text-secondary    oklch(0.578 0.0873 222.49)
  "rx-text-muted":      "#779FAE",  # --color-text-muted        oklch(0.6795 0.0489 222.5)
  "rx-link":            "#6EB1BD",  # --color-link              oklch(72% 0.07 210)
  "rx-accent-node":     "#A4770F",  # --color-accent-node       oklch(0.60 0.12 81.12)
  "rx-accent-channel":  "#D75749",  # --color-accent-channel    oklch(0.6202 0.1641 29.03)
  "rx-outbound":        "#0A9068",  # derived  oklch(0.58 0.12  165)
  "rx-warning":         "#C46016",  # derived  oklch(0.60 0.15  50) — see the note below
  "rx-error":           "#D33B36",  # derived  oklch(0.58 0.19  27)
  "rx-on-accent":       "#0D0600",  # derived — dark in both palettes, see below
}

# `rx-accent-node` moved in the light palette only, on 2026-08-13, and the mapping
# above is still one-to-one: rxonly.css moved first and the full reasoning is in the
# comment there. The short of it is that the light entry was the dark entry copied —
# 0.8042 against 0.8029 — and had never been checked against a near-white page, where
# it managed **1.69:1**. This suite's word for "a node" was therefore unreadable in the
# light theme in both interfaces at once: every sidebar row here, every `.node-link`
# there, and every node id in the log viewer.
#
# It is 3.63:1 now, beside `rx-accent-channel`'s 3.55:1 — which is the entry that was
# tuned for both themes and is the model for what this should have been. Same hue,
# darkened rather than replaced.
#
# **The lightness is also a gamut floor, and that is what ties it to this file.** Below
# 0.60 the chroma at hue 81.12 leaves sRGB, and the hex here would then be *this*
# project's clipping of an out-of-gamut colour while the browser applied its own — two
# different answers to "what colour is it", which is exactly the drift the one-to-one
# mapping exists to prevent. Both sides agree at 0.60.
#
# The dark entry is untouched at 10.74:1. Found by the suite testbed, which is also
# where the arithmetic above is asserted.

# `rx-warning` was `oklch(… 70)` in both palettes and moved to hue 50 on 2026-08-07.
# It was an amber sitting eleven degrees of hue off `--color-accent-node`, which is
# this interface's yellow for *a node* — and the log viewer puts the two on screen
# together, `[WARNING]` in the marker column and node ids through the message beside
# it. In the dark palette they were 0.060 apart in OKLab, which is a few times the
# just-noticeable difference and nowhere near far enough for two runs that mean
# unrelated things. At hue 50 the dark pair is 0.117 apart, and orange is what
# `[WARNING]` should have looked like anyway. Jason's, having read one.
#
# The cost is on the other side, and it is real: hue 50 walks toward `rx-error` at
# hue 27, so the light palette's warning-to-error distance falls from 0.137 to 0.081.
# That is still well clear, the two levels are also told apart by the word inside the
# brackets, and the light entry buys back some legibility on the way — 3.55:1 against
# `rx-bg-surface` where the old amber managed 3.16:1.

# `rx-on-accent` is the text colour that sits on the row cursor, and it is dark in
# both palettes rather than inverting like everything else. That is because the
# cursor's background does not invert: it is `--color-link`, which rxonly.css
# defines as the same `oklch(72% 0.07 210)` in both of its theme blocks. Dark text
# on that blue is about 8:1; Textual's own `auto` picks white for it, which is
# nearer 2:1. One colour is correct for both themes here precisely because the
# background under it is one colour in both.

# `--color-bg-below-surface` was what `.device-bar` took in rxonly.css, and it is the
# one entry here whose story has run backwards. It was defined only in that
# stylesheet's light theme until 2026-08-06, so the web's dark device bar fell back to
# the page background; it gained a dark value that day, `oklch(0.28 0.0121 73.06)`,
# chosen to reproduce the light theme's bar-to-page separation of 1.38:1.
#
# **Equal separation turned out not to mean equal weight.** A band lighter than a
# near-black page emits; the same ratio darker than a light page recedes. So the web
# header now takes `--color-bg-header` — a variable that had been defined in both
# themes and used by nothing — at a third of that separation in the dark, and this
# console's own header stopped being a filled band at all and became a rule in
# `$rx-bg-surface-alt`, matching the sidebar divider.
#
# What is left using this colour is the message list's cursor, here and nowhere on
# the web. The mapping is still one-to-one and the value still matches; it is simply
# read from this side only. Chosen for it because it is the one background in the
# palette that steps the *same* distance off the page in both themes, which is the
# property that made it wrong for a header and right for a row.


def _theme(name: str, palette: dict[str, str], *, dark: bool) -> Theme:
  """One theme from one palette.

  `text` and `text-muted` are overridden because Textual derives them from the
  background as an auto-contrast percentage, which is a reasonable default and
  not this palette: RxOnly's body text is `--color-text-primary` and its muted
  text is a specific dim teal, not a transparency of the foreground. Everything
  else Textual derives is left alone.
  """
  return Theme(
    name=name,
    dark=dark,
    background=palette["rx-bg-app"],
    surface=palette["rx-bg-surface"],
    panel=palette["rx-bg-surface-alt"],
    foreground=palette["rx-text-primary"],
    # The interactive accent — borders, the row cursor, focus. RxOnly's link
    # colour, which is the same job in the same suite.
    primary=palette["rx-link"],
    secondary=palette["rx-text-secondary"],
    # What a focused input is outlined in, in both mediums: RxOnly's node search
    # input takes `border-color: var(--color-accent-node)` on focus.
    accent=palette["rx-accent-node"],
    success=palette["rx-outbound"],
    warning=palette["rx-warning"],
    error=palette["rx-error"],
    variables={
      **palette,
      "text": palette["rx-text-primary"],
      "text-muted": palette["rx-text-muted"],
    },
  )


def palette_for(dark: bool) -> dict[str, str]:
  """The `$rx-*` values a stylesheet needs, for a dark or a light surround.

  Exists so nothing outside this file has to know which of the two dictionaries
  above is which. Its caller is `MeshConsoleApp.get_css_variables`, which supplies
  these underneath whatever theme is active — see the comment there for why a
  stylesheet written against `$rx-*` cannot be left depending on one being.
  """
  return DARK_PALETTE if dark else LIGHT_PALETTE


RXONLY_DARK = _theme("rxonly-dark", DARK_PALETTE, dark=True)
RXONLY_LIGHT = _theme("rxonly-light", LIGHT_PALETTE, dark=False)

# Dark is the default because it is the one the web interface is usually looked
# at in and the one Jason compared against. Both are registered, so the built-in
# command palette can switch between them without this file changing.
DEFAULT_THEME = RXONLY_DARK.name

THEMES = (RXONLY_DARK, RXONLY_LIGHT)
