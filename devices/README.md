# Device protocol references

Markdown files in this directory document the HID surface
SteelVoiceMix talks to — one file per device family.

## What this is, and what it isn't

**This is**: a contributor-facing protocol reference. Every byte the
daemon sends is documented in one place with a status column
(shipped / planned / cross-checked-against-upstream).

**This isn't**: machine-readable. The Rust daemon hardcodes opcodes
in `src/hid.rs` and `src/protocol.rs`. Nothing parses these `.md`
files. When the daemon and a doc disagree, **the daemon is
authoritative** — fix the doc.

If we ever wire a real runtime loader (when a third device family
incoming, or a contributor submits a new Arctis variant), we'll move
to a typed format with serde derive — likely TOML or RON, not YAML.
The earlier YAML scaffold was misleading: it looked loadable but
nothing read it. Markdown removes that pretense.

## Format

See `nova_pro_wireless.protocol.md` for the canonical example. Tables
group:

- **Identity** — vendor / product IDs, message length, padding.
- **Init handshake** — opcodes sent on connect, in order.
- **Status polling** — request opcode + offset map of the response.
- **Auxiliary status reports** — async base-station messages.
- **User-facing settings** — control opcodes by category.
- **OLED gauge** — brightness only; frame protocol lives in
  `ggoled_lib/`.

Every row carries a status column:

| Symbol | Meaning |
|:---:|---|
| ✅ | Shipped — daemon sends or parses this today |
| 🚧 | Planned — staged for an upcoming beta |
| 🔁 | Cross-checked against ASM and/or LAM upstream |

## Contributing

When adding or changing an opcode:

1. Update `src/hid.rs` / `src/protocol.rs` (the source of truth).
2. Update the matching row in the corresponding `.protocol.md` file
   in the same PR — keep the doc in sync.
3. Cite the source: ASM YAML row, LAM YAML row, or a Wireshark
   capture. Per `CONTRIBUTING.md`'s hardware-safety rule, every byte
   that hits firmware must be byte-exact from a verified upstream.

When adding a brand-new device:

1. Open an issue first — we don't have multi-device support in the
   daemon yet, so the runtime work is non-trivial.
2. The device's `.protocol.md` reference can land before the runtime
   work as documentation; we'll cite the issue from the doc.
