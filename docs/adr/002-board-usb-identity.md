# ADR-002: A board's USB identity filters its port and is recorded with every result

## Status
Accepted

## Context
A board is recognised by the chip espflash reports and, optionally, its `mac`; the USB side of the port is ignored for detected boards.
Records in `results.jsonl` keep only the port path and the MAC, so a board's kind is guessed from `usbmodem` versus `usbserial` names.
Recorded runs show one board key used for a C3 SuperMini (native USB) and a C3 DevKitM-1 (USB-UART bridge), and a MAC under another repo's key.
The family-resemblance check B07 papers over this by electing a "home MAC" by run count, which a few extra runs can overturn.
`usb_id`/`usb_serial` already exist, but only to locate a `detect = false` board without `--port`.

## Decision
- Every board may carry `usb_ids = ["vid:pid", …]`; a port is a candidate only when its VID:PID is in the list.
- `usb_id` remains valid as a one-entry `usb_ids`; a board setting both is a config error.
- `usb_serial` needs `usb_ids` or `usb_id` and narrows the match to that serial number, on any board.
- Detection drops non-matching ports before probing, so espflash never opens a port of the wrong kind.
- A port that reports no VID:PID never matches, and an explicit `--port` that does not match ends the run with `error`.
- The filter applies when the board is picked; after a flash the port is followed by path, as today.
- Every `results.jsonl` entry records `usb_id` and `usb_serial` of the picked port, read from `list_ports.comports()`, null when unknown.
- Nothing opens or resets a port to learn its identity, and `detect` is unchanged.

## Consequences
- A wrong-kind board is refused before a flash instead of failing later on its banner.
- Records carry a board's kind directly, so B07 can drop the run-count rule once consumers re-run.
- `303a:1001` is shared by every native-USB ESP chip, so the filter separates bridge kinds, not C3 from S3; the chip probe and `mac` still do that.
- A board whose catalogue entry lists the wrong VID:PID is refused until `bench.toml` is fixed, with no override flag.
- Firmware that re-enumerates under another PID (USB-OTG) passes the pick before its flash, but a re-pick later in the same run may refuse it.

## Alternatives Considered
|                    Alternative | Pros                       | Cons                                 | Why Rejected                     |
|-------------------------------:|:---------------------------|:-------------------------------------|:---------------------------------|
|         Keep the home-MAC rule | No code change             | Run counts decide identity           | The stopgap this replaces        |
| A second field beside `usb_id` | Literal reading of request | Two fields that must agree           | One concept is simpler           |
|        Warn and trust `--port` | Escape hatch for odd parts | Bypassable by the person it protects | A mismatch is a setup error      |
|   Read IDs via espflash output | One source of truth        | Needs a probe, which opens the port  | No new port open is a constraint |
