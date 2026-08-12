# TAG v2 USB wire contract

Status: **approved implementation contract, not yet hardware-captured release evidence**.
The machine-readable source of truth is [`TAG_V2.json`](TAG_V2.json).

TAG v2 widens the timestamp, adds a CRC trailer, and uses a distinct magic/command
pair. It does not reinterpret a TAG v1 frame.

| Offset | Size | Field | Encoding |
| ---: | ---: | --- | --- |
| 0 | 2 | magic | `A5 5B` |
| 2 | 1 | type | `1=tactile`, `2=imu`, `3=mag` |
| 3 | 2 | payload length | little-endian `u16` |
| 5 | 4 | per-modality sequence | little-endian `u32` |
| 9 | 8 | device timestamp, microseconds | little-endian `u64` |
| 17 | `plen` | payload | unchanged from TAG v1 |
| `17 + plen` | 4 | CRC-32/ISO-HDLC | little-endian `u32` over header + payload |

The packed header format is `<2sBHIQ` and its exact size is 17 bytes. `plen` counts
payload bytes only; it excludes both the header and the four-byte CRC trailer.
Payload sizes remain tactile packed12 = 120 bytes, IMU = 12 bytes, and
magnetometer = 6 bytes.

The CRC is the reflected IEEE CRC-32 (`CRC-32/ISO-HDLC`): polynomial `0xEDB88320`,
initial state `0xFFFFFFFF`, final XOR `0xFFFFFFFF`, serialized as `<I`. It covers
the exact 17 header bytes followed by the exact `plen` payload bytes and is
equivalent to `zlib.crc32(header + payload)`. TAG v1 remains byte-identical and has
no CRC trailer.

## Negotiation

Firmware reports `tag_ver_max` as a JSON integer in `GET CONFIG`.

- missing or `1`: host sends `STREAM TAG ON` and decodes TAG v1 (`A5 5A`)
- `2` or newer: a v2-capable host sends `STREAM TAG2 ON` and decodes TAG v2
- host stops the selected stream with its matching `OFF` command
- a host must cap selection at its own known maximum; it must not guess a future
  header layout

The connection handshake sends both stop commands so recovery is idempotent after a
previous process dies in either mode.

## Boot identity and reboot boundary

`boot_id` is session metadata, not part of the approved data frame. Firmware
starts TAG2 with the exact line `#STREAM TAG2 on boot_id=<32 lowercase hex>`. CONFIG may expose
the same value only as exactly 32 lowercase hexadecimal characters. JSON integers
and uppercase strings are invalid. The SDK assembles a split ACK before decoding binary data,
compares it with CONFIG and any pre-pause session identity, captures it at stream
start, and clears it before every new CONFIG exchange.

The host drains bytes buffered before sending `STREAM TAG2 ON`. The first complete
response after that command must be the exact ACK; no diagnostic-line whitelist is
part of the protocol. An unknown line, `#ERR`, malformed `#STREAM TAG2`, binary byte,
or timeout fails closed. Bytes following the ACK newline are the first binary frame
bytes and must be preserved. Firmware must write the ACK as one checked complete
response before enabling TAG2. It must not depend on RTS; the host asserts DTR and
keeps RTS low.

That means a CONFIG `boot_id` proves which boot a newly started stream belongs to,
but cannot by itself prove that the device did not reboot in the middle of an open
binary stream. Mid-stream reboot detection needs one of these firmware decisions:

1. include a boot/session identifier in every frame or a periodic authenticated
   sideband record, or
2. terminate/re-enumerate USB on reboot so the host must run CONFIG negotiation again.

The ACK bytes and boot-id width are locked, but still need real firmware captures
before 0.9.13 is called qualified.

## Test vectors

`TAG_V2.json` locks negotiation, identity encoding, CRC parameters, all three
modality layouts, endian order, timestamps beyond multiple u32 epochs, and expected
values. It is deliberately labelled `implementation-contract-not-hardware-captured`.
Final release evidence must add bytes
captured from the tagged 0.9.13 firmware artifact rather than relabeling synthetic
vectors as hardware truth.
