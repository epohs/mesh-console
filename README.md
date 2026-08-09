# Mesh Console

<!-- TODO(seam): bold one-line premise, in the shape of README.md:3. -->

<!-- TODO(seam): what-it-is paragraphs. This is the terminal interface onto the same
     database Mesh Collector writes, and the one component that can transmit. Worth
     saying early that reaching it over SSH avoids exposing anything to the web. -->

This project is built for personal use and experimentation, prioritizing clarity, safety, and ease of maintenance over features.

<!-- TODO(seam): warning callout in the shape of README.md:12-20, but for a different
     risk than RxOnly's. Nothing is exposed to the internet here; the thing to be careful
     about is that this component can send. -->


## Installation & Getting Started

This project uses [uv](https://docs.astral.sh/uv/) for Python dependency management and virtual environments.

### Prerequisites
- Python 3.10 or newer
- `uv` installed globally
- A working [Mesh Collector](https://github.com/epohs/mesh-collector) install

### Clone the repository

```
git clone https://github.com/epohs/mesh-console.git
cd mesh-console
```

### Create the virtual environment


Each project in this suite uses its own virtual environment.

```
# Create environment
uv init
# Install dependencies
uv sync
```

<!-- TODO(seam): config section in the shape of README.md:57-69. DB_PATH points at the
     collector's database and is opened read-only. -->

<!-- TODO(seam): running it, in the shape of README.md:72-97. -->

### Sending messages (optional, off by default)

This console never transmits. [Mesh Collector](https://github.com/epohs/mesh-collector) owns the serial port, so it is the only thing that can, and all this does is ask it to over a Unix socket. That means sending needs both ends set up, and either one left alone keeps this a read-only console.

On the collector's side, install it with its transmit extra and set `ENABLE_TX` — its own README covers that. On this side, install with the send extra, which pulls in [Mesh Link](https://github.com/epohs/mesh-link):

```
uv sync --extra send
```

Then set `ENABLE_SEND` to true in your `config.json`. `CONTROL_SOCKET_PATH` is where to find the collector's socket; left empty, both ends work it out the same way and agree without being told — but only if they run as the same user on the same machine, so set it on both if they don't.

A compose box appears at the bottom of a channel, or of a conversation with one node, once all of that is true *and* a collector actually answers. If nothing is listening the box is simply absent, because a box that cannot send is a worse answer than no box. Press `c` to type, `enter` to send, `escape` to go back to reading. Single-letter keys mean nothing while you are typing — `q` in the compose box is a `q`.

> [!WARNING]
> **Anything you send goes out over the air under your node's identity, and a public mesh is public.** There is no confirmation step: `enter` transmits. The box names where it is addressed the whole time it is open — the channel, or the person — so check that line rather than trusting which one you think you are in.

Messages are capped at 233 bytes — that is bytes of UTF-8 rather than characters, so emoji cost four each and a message of them runs out roughly four times sooner than English. The counter beside the box counts what the limit actually counts.

Your own messages are shown rather than filtered, marked `You` and set apart from everything received. They appear because the collector writes them to the archive as it sends them and this reads them back; nothing is inserted into the list directly, which is why a message that went out but was not archived says so instead of quietly never appearing.

Direct messages are sent from a conversation with one node, never from the direct message list — that list is every conversation at once, so there is no one recipient it could be addressed to. Press `enter` on a direct message to open the conversation it belongs to, or `enter` on a node's detail view to start one with a node that has never written to you. The box then names that node by short name *and* node id, because two nodes on a public mesh can share a short name, and the recipient belongs to the conversation rather than to whichever row your cursor happens to be on — arrow keys move you through the messages, never to somebody else. A direct message asks the far end for an acknowledgement and a channel message does not, which the radio decides from the destination.

`R` replies to the message the cursor is on, and the box then says what it is answering as well as where it is going. `escape` stops answering without throwing away what you have typed. A reply whose text is nothing but emoji arrives as a reaction on the message it answers rather than as a message of its own, so the counter beside the box says `reaction` while you type — sending one is useful, discovering it afterwards is not.

> [!NOTE]
> Messages you send from the Meshtastic phone app over Bluetooth never reach the archive. The phone talks to the radio directly, the collector only ever sees what arrives over the air, and the radio does not report its own transmissions back. Nothing sent that way will appear here, and that is not a fault in either program.


<!-- TODO(seam): use case section in the shape of README.md:103-124 — reaching the mesh
     over SSH instead of putting a dashboard on the public internet. -->


Licensed under the GNU AGPL-3.0
Copyright (c) 2026 epohs
