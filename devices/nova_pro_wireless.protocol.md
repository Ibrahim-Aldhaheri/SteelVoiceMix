# SteelSeries Arctis Nova Pro Wireless / Wireless X / Wired — HID Protocol Reference

Documentation of every HID byte SteelVoiceMix exchanges with the
headset. **This file is descriptive, not loaded at runtime.** The Rust
daemon hardcodes the same opcodes in `src/hid.rs` and `src/protocol.rs`;
this doc exists so a contributor can read one human-readable place
instead of grepping enums.

When the daemon and this file disagree, the daemon is authoritative —
fix the doc. Hardware-safety rule from `CONTRIBUTING.md` still
applies: every byte in the **Shipped** column must be byte-exact from
ASM, LAM, or a verified Wireshark capture.

**Status legend:**

| Symbol | Meaning |
|:---:|---|
| ✅ | Shipped — daemon sends or parses this today |
| 🚧 | Planned — staged for an upcoming beta (see roadmap notes) |
| 🔁 | Cross-checked against ASM and/or LAM upstream |

---

## Identity

| Field | Value |
|---|---|
| Vendor ID | `0x1038` (SteelSeries) |
| Product ID — Nova Pro Wireless | `0x12E0` |
| Product ID — Nova Pro Wireless X | `0x12E5` |
| Product ID — Nova Pro Wired | `0x12CB` |
| Product ID — Nova Pro Wired (Xbox) | `0x12CD` |
| Command interface | bInterfaceNumber = `4`, bAlternateSetting = `0` |
| TX prefix (host → base) | `0x06` |
| RX prefix (base → host) | `0x07` |
| Message length (wireless) | 64 bytes, padded with `0x00` at end |
| Message length (wired) | 16 bytes, padded with `0x00` at end |

---

## Init handshake

Sent on connect to bring the headset to a known state. Order matters.
Values referencing user preferences are substituted from the daemon's
persisted state.

| Bytes | Purpose | Source | Status |
|---|---|---|---|
| `06 49 01` | Enable ChatMix | LAM, ASM | ✅ 🔁 |
| `06 8D 01` | Show ChatMix icon on OLED | LAM, ASM | ✅ 🔁 |
| `06 C3 <wireless_mode>` | 2.4 GHz wireless mode (`00` speed / `01` range) | LAM, ASM | ✅ 🔁 |
| `06 C1 <pm_shutdown>` | Inactivity power-off timer (see settings table) | LAM, ASM | ✅ 🔁 |
| `06 37 <mic_volume>` | Mic volume (`01` = mute … `0A` = 100 %) | LAM, ASM | ✅ 🔁 |
| `06 27 <mic_gain>` | Mic gain (`01` = low / `02` = high) | LAM, ASM | ✅ 🔁 |
| `06 BF <mic_led_brightness>` | Mute-LED brightness (`01`–`0A`) | LAM, ASM | ✅ 🔁 |
| `06 BD <anc_mode>` | ANC mode (`00` off / `01` transparent / `02` on) | ASM | ✅ 🔁 |
| `06 39 <mic_sidetone>` | Mic sidetone (`00` off / `01`–`03` low/med/high) | LAM, ASM | 🚧 |

Roadmap note for sidetone: opcode is verified against LAM's YAML;
GUI control + protocol enum land in the next deck-features beta
(task 2 of the polish/expand plan).

---

## Status polling

Daemon sends `06 B0` and parses the reply against the offset map
below. Wireless variants return all fields; wired returns only the
ones marked **W** (wired).

### Request

| Bytes | Purpose | Status |
|---|---|---|
| `06 B0` | Request status snapshot | ✅ |

### Response — `0x06b0` payload offsets

| Offset | Field | Encoding | Status |
|:---:|---|---|---|
| `0x06` | `headset_battery_charge` | raw `0x00`–`0x08` → 0–100 % | ✅ |
| `0x07` | `charge_slot_battery_charge` | raw `0x00`–`0x08` → 0–100 % | 🚧 |
| `0x08` | `transparent_noise_cancelling_level` | raw `0x00`–`0x0A` → 0–100 % | 🚧 |
| `0x09` | `mic_status` | `00` unmuted / `01` muted | ✅ |
| `0x0A` | `noise_cancelling` | `00` off / `01` transparent / `02` on | ✅ |
| `0x0B` | `mic_led_brightness` | raw `0x00`–`0x0A` → 0–100 % | ✅ |
| `0x0C` | `auto_off_time_minutes` | enum (see settings table) | ✅ |
| `0x0D` | `wireless_mode` | `00` speed / `01` range | ✅ |
| `0x0E` | `wireless_pairing` | `01` not paired / `04` paired offline / `08` connected | ✅ |
| `0x0F` | `headset_power_status` | `01` offline / `02` cable charging / `08` online | ✅ |

Roadmap notes:
- `charge_slot_battery_charge` (the dock's spare battery) lands as a
  Home-tab readout in task 5 of the polish/expand plan.
- `transparent_noise_cancelling_level` exposes a slider on the Deck
  tab when ANC mode = transparent (task 7).

### Auxiliary status reports

These are separate report IDs the base station sends asynchronously
(not part of the periodic poll).

| First two bytes | Field | Offset | Encoding | Status |
|:---:|---|:---:|---|---|
| `07 25` | `station_volume` | `0x02` | raw `0x38` (56) → 0 % … `0x00` → 100 % (inverted) | 🚧 |
| `07 45` | `media_mix` | `0x02` | 0–100 % | ✅ |
| `07 45` | `chat_mix` | `0x03` | 0–100 % | ✅ |

`station_volume` exposes a read-only base-station volume readout in
task 5.

---

## User-facing settings

Each row's `update_sequence` shows the bytes the daemon sends when
the user changes that setting via the GUI.

### Headset

| Setting | Type | Range / Mapping | Default | Update | Status |
|---|---|---|:---:|---|:---:|
| `mic_gain` | enum | `01` low / `02` high | `02` | `06 27 <value>` | ✅ |
| `anc_mode` | enum | `00` off / `01` transparent / `02` on | `00` | `06 BD <value>` | ✅ |
| `transparent_level` | slider | `00`–`0A`, only meaningful when `anc_mode == transparent` | `05` | `06 B9 <value>` | 🚧 |

### Microphone

| Setting | Type | Range / Mapping | Default | Update | Status |
|---|---|---|:---:|---|:---:|
| `mic_volume` | slider | `01`–`0A` (`01` = mute) | `0A` | `06 37 <value>` | ✅ |
| `mic_sidetone` | enum | `00` off / `01` low / `02` medium / `03` high | `00` | `06 39 <value>` | 🚧 |
| `mic_led_brightness` | slider | `01`–`0A` | `0A` | `06 BF <value>` | ✅ |

### Power management

| Setting | Type | Range / Mapping | Default | Update | Status |
|---|---|---|:---:|---|:---:|
| `pm_shutdown` | enum | `00` never / `01` 1 m / `02` 5 m / `03` 10 m / `04` 15 m / `05` 30 m / `06` 60 m | `05` (matches GG) | `06 C1 <value>` | ✅ |

### Wireless

| Setting | Type | Range / Mapping | Default | Update | Status |
|---|---|---|:---:|---|:---:|
| `wireless_mode` | enum | `00` speed / `01` range | `00` | `06 C3 <value>` | ✅ |

---

## OLED gauge — wireless variants only

The OLED protocol uses a separate USB interface (handled by
`ggoled_lib`) and follows different framing than the control opcodes
above. Only the brightness control comes through the same channel.

| Setting | Type | Range / Mapping | Default | Update | Status |
|---|---|---|:---:|---|:---:|
| `oled_brightness` | slider | `00` off … `0A` full | `08` | `06 85 <value>` | ✅ |

Frame upload + chatmix gauge bytes are documented inline in
`ggoled_lib/`; per the hardware-safety rule in `CONTRIBUTING.md` they
ship byte-exact from ASM and aren't restated here to avoid drift.

---

## Cross-references

- LAM nova_pro_wireless.yaml: <https://github.com/elegos/Linux-Arctis-Manager/blob/develop/src/linux_arctis_manager/devices/nova_pro_wireless.yaml>
- ASM device descriptors: <https://github.com/loteran/Arctis-Sound-Manager>

When a row in this file ships ✅ but disagrees with either upstream,
either:

1. We have a verified Wireshark capture that overrides — note it in
   the row's Source column (e.g. "Wireshark 2026-04, GG 75.x").
2. Or the upstream is wrong and we filed a PR upstream — note the PR
   link.

Disagreement without a noted reason is a bug to investigate.
