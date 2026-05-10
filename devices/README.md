# Device descriptors

YAML files in this directory document the HID surface SteelVoiceMix
talks to — one file per device family. Modeled loosely on the same
format used by [Linux-Arctis-Manager (LAM)][lam] and
[Arctis Sound Manager (ASM)][asm] so descriptors can be cross-checked
against upstream when an opcode is in doubt.

[lam]: https://github.com/elegos/Linux-Arctis-Manager/tree/develop/src/linux_arctis_manager/devices
[asm]: https://github.com/loteran/Arctis-Sound-Manager

## Status — descriptive, not yet loaded at runtime

The Rust daemon still has hardcoded device behavior in `src/hid.rs`
and `src/protocol.rs`. These YAML files exist so:

1. Every opcode the daemon sends is **declared in one human-readable
   place**. New contributors can read the YAML before reading
   `hid.rs`.
2. **PR reviews can verify byte-exactness** against ASM/LAM by diffing
   the corresponding YAML files instead of grepping Rust enums.
3. **New device support starts as a YAML PR** — contributors can
   submit a descriptor for an Arctis variant we don't yet handle, and
   the runtime work to wire it up can land in a follow-up.
4. **Future runtime loader is straightforward** when we want it: each
   `update_sequence` already reads as a literal byte recipe.

When the runtime loader lands, the source-of-truth flips: the YAML
becomes authoritative and Rust constants disappear. Until then, the
two sides need to stay in sync — adding a new opcode means updating
both `hid.rs` and the YAML in the same PR.

## Format

See `nova_pro_wireless.yaml` for the canonical example. Key sections:

- `device:` — identity (vendor ID, product IDs, command interface,
  message length, padding rules).
- `device_init:` — opcodes sent on connect to bring the headset into
  a known state. Order matters; values referencing
  `settings.<name>` are substituted at send-time from persisted
  preferences.
- `status:` — the polled status report request opcode and the
  byte-offset map of the response, plus how each field is
  interpreted (percentage / on-off / int-string mapping).
- `settings:` — the user-facing controls grouped by category
  (headset / microphone / power_management / wireless / anc).
  Each entry has its `update_sequence` (the opcode bytes), a
  type (slider / discrete_map / toggle), and any value mapping.

## Cross-references

When opcodes here don't match ASM/LAM, that's a red flag that
deserves a comment explaining why. The hardware-safety rule from
`CONTRIBUTING.md` still applies: bytes hitting the device firmware
must be byte-exact from a verified upstream source.
