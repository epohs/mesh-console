from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


# Settings are grouped by which process owns them. This project reads the
# archive and never writes it, so it carries only what a reader needs: anything
# describing how the archive is collected belongs to mesh-collector, and a
# setting absent from this surface cannot be set here from any source.
#
# What the collector keeps — retention limits, tracked channels, whether direct
# messages are archived at all — is read from the meta table at runtime, not
# configured here. See mesh-collector's schema.sql for the published keys.

# Needed by anything that opens the archive.
#
# DB_PATH has no default, and that is the honest answer rather than a missing one.
# mesh-collector writes the archive inside its own checkout, so the file this
# project reads lives in a directory belonging to a different project, on a path
# only this install knows. A plausible-looking default like "data/db.sqlite"
# would point at a directory *here* — which is precisely the wrong place, since
# anything sitting there would be a stale copy while the collector wrote
# elsewhere. Unset, startup says "tell me where it is" and stops.
SHARED_CONFIG = {
  "DEBUG": False,                     # Enable verbose logging
  "DB_PATH": "",                      # Absolute path to mesh-collector's SqLite archive
}

# Presenting the archive in a terminal. Owned by this project; RxOnly owns its own.
#
# ENABLE_SEND is off by default and is not ENABLE_TX. The collector transmits;
# this console only asks it to, over the control socket, and the two settings name
# two different capabilities held by two different processes. Keeping the names
# distinct is what makes "neither reader can be configured to transmit" a true
# statement rather than a hopeful one — a stray MESH_CONSOLE_ENABLE_TX reaches
# nothing here, and this key reaches nothing there.
#
# Sending is also opt-in in what gets installed: mesh-link is an optional
# dependency, so a console installed with a plain `uv sync` has nothing this
# setting could switch on. See pyproject.toml.
CONSOLE_CONFIG = {
  "SHOW_DIRECT_MESSAGES": False,      # Should the console display archived direct messages
  "POLL_INTERVAL": 10,                # Seconds between checks for new messages
  "PAGE_SIZE": 50,                    # Messages and nodes fetched per page
  "ENABLE_SEND": False,               # Offer a compose box, and ask the collector to transmit
  "CONTROL_SOCKET_PATH": "",          # mesh-collector's control socket; empty means the platform default
  # What "View raw logs" runs and streams. The console cannot see the collector's
  # log through the archive — the log lives wherever the collector's stdout goes —
  # so this is a command run on the console's own host. The default matches the
  # shipped systemd unit (deploy/mesh-collector.service.example); a collector run
  # some other way needs this pointed at wherever its output lands, e.g.
  # "tail -F /path/to/collector.log". `-n 200` backfills recent history so the
  # viewer is not empty at open.
  #
  # `-o short-iso-precise` is asked for so the timestamps arrive in a form the
  # viewer can restate in the reader's own timezone — see `logfmt.localise_stamp`,
  # which converts a leading ISO-8601 stamp and requires the offset that format
  # carries. journald's default `short` format prints local time already and is
  # left alone rather than guessed at, so dropping this flag costs the conversion
  # and nothing else: the level colouring and the level filter read the
  # `[LEVELNAME]` marker, which is the collector's own and is there either way.
  #
  # `--no-hostname` because the hostname is the same on every line of a log you
  # are reading one machine's worth of, and it is not free: it sits in front of
  # the level marker, so it widens the column the viewer hangs a wrapped line's
  # continuation from. With it, an 80-column terminal gives up the alignment
  # entirely — see `_MIN_MESSAGE_ROOM` in ui/logfmt.py. Without it, the same
  # terminal keeps it.
  "LOG_COMMAND": ("journalctl -u mesh-collector -f -n 200"
                  " -o short-iso-precise --no-hostname"),
}

CONSOLE_SETTINGS = {**SHARED_CONFIG, **CONSOLE_CONFIG}

CONFIG_FILE_PATH = Path(__file__).parent / "config.json"
SAMPLE_CONFIG_FILE_PATH = Path(__file__).parent / "config-sample.json"

# Environment variable names are prefixed per process, so co-hosted processes
# don't share one namespace: this console reads MESH_CONSOLE_DB_PATH, the web app
# reads RXONLY_DB_PATH, the collector reads MESH_COLLECTOR_DB_PATH. Keys in
# config.json stay unprefixed, which is what lets one shared config.json serve
# every process while each still sees only its own surface.
DEFAULT_ENV_PREFIX = "MESH_CONSOLE_"




class Config:
  """
  Central configuration loader.
  Priority: environment variables > config.json > defaults.
  """

  values: dict[str, Any] = {}
  env_prefix: str = DEFAULT_ENV_PREFIX
  _loaded: bool = False




  @classmethod
  def load(
    cls,
    env_prefix: str = DEFAULT_ENV_PREFIX,
    settings: dict[str, Any] = CONSOLE_SETTINGS,
  ) -> None:
    """Load configuration values. Only runs once.

    A setting exported or written for a co-hosted process — the collector's
    SERIAL_PORT, say — can neither be read by nor reconfigure this one. Keys
    outside the surface are ignored, wherever they came from.
    """
    if cls._loaded:
      return

    cls.env_prefix = env_prefix
    cls.values = settings.copy()

    if CONFIG_FILE_PATH.exists():
      try:
        with open(CONFIG_FILE_PATH, "r") as f:
          file_config = json.load(f)
        for key, value in file_config.items():
          if key not in settings:
            continue
          if not cls._matches_default_type(value, settings[key]):
            # Keeping the default fails closed; coercing would let the *string*
            # "false" turn ENABLE_SEND or SHOW_DIRECT_MESSAGES on, because any
            # non-empty string is truthy.
            print(
              f"Warning: config.json {key}={value!r} is not a "
              f"{type(settings[key]).__name__}; keeping the default {settings[key]!r}"
            )
            continue
          cls.values[key] = value
      except Exception as e:
        print(f"Warning: Failed to read {CONFIG_FILE_PATH}: {e}")

    for key, default_val in settings.items():
      env_key = f"{cls.env_prefix}{key}"
      env_val = os.getenv(env_key)
      if env_val is not None:
        try:
          cls.values[key] = cls._cast_env_value(env_val, default_val)
        except Exception:
          print(f"Warning: Could not cast environment variable {env_key}='{env_val}'")

    cls._loaded = True




  @classmethod
  def get(cls, key: str, default: Any = None) -> Any:
    """Retrieve a config value by key."""
    if not cls._loaded:
      cls.load()
    return cls.values.get(key, default)




  @staticmethod
  def _matches_default_type(value: Any, default_val: Any) -> bool:
    """Whether a config.json value has the type its default establishes.

    None passes: JSON null means "explicitly unset", and a None value fails
    closed wherever it is read. bool is checked before int because it is a
    subclass of it, in both directions: True is not a poll interval, and 1 is
    not an authorization.
    """
    if value is None:
      return True
    if isinstance(default_val, bool):
      return isinstance(value, bool)
    if isinstance(default_val, int):
      return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default_val, float):
      return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default_val, list):
      # Integer lists: every element an int, and not a bool pretending.
      return isinstance(value, list) and all(
        isinstance(item, int) and not isinstance(item, bool) for item in value
      )
    if isinstance(default_val, str):
      return isinstance(value, str)
    return True




  @staticmethod
  def _cast_env_value(env_val: str, default_val: Any) -> Any:
    """Cast environment variable string to the type of default_val."""
    if isinstance(default_val, bool):
      return env_val.lower() in ("true", "1", "yes")
    if isinstance(default_val, int):
      return int(env_val)
    if isinstance(default_val, float):
      return float(env_val)
    if isinstance(default_val, list):
      # Integer lists arrive comma-separated: "0,2,3"
      return [int(part) for part in env_val.split(",") if part.strip()]
    return env_val
