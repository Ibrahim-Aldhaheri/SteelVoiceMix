//! HID communication with the SteelSeries Arctis Nova Pro Wireless base station.

use hidapi::{HidApi, HidDevice};
use thiserror::Error;

// USB IDs
pub const VENDOR_ID: u16 = 0x1038;  // SteelSeries
pub const PRODUCT_ID: u16 = 0x12E0; // Arctis Nova Pro Wireless base station
pub const HID_INTERFACE: i32 = 0x04; // Control interface
pub const MSG_LEN: usize = 64;

// HID protocol bytes
pub const TX: u8 = 0x06; // Host → base station
#[allow(dead_code)]
pub const RX: u8 = 0x07; // Base station → host (for reference)

// Parameter IDs (second byte in messages)
pub const OPT_SONAR_ICON: u8 = 0x8D;
pub const OPT_CHATMIX_ENABLE: u8 = 0x49;
pub const OPT_CHATMIX: u8 = 0x45;
pub const OPT_BATTERY: u8 = 0xB0;

/// Battery status decoded from HID response.
#[derive(Debug, Clone, serde::Serialize)]
pub struct BatteryStatus {
    /// Battery level 0–100%.
    pub level: u8,
    /// "active", "charging", or "offline".
    pub status: String,
}

/// A HID message received from the base station.
#[derive(Debug)]
pub enum HidEvent {
    ChatMix { game_vol: u8, chat_vol: u8 },
    Battery(BatteryStatus),
    Unknown,
}

/// Error type for HID operations.
#[derive(Debug, Error)]
pub enum HidError {
    #[error("Base station not found")]
    DeviceNotFound,
    #[error("Failed to open device: {0}")]
    OpenFailed(String),
    #[error("Device disconnected")]
    Disconnected,
    #[error("HID API error: {0}")]
    ApiError(String),
}

/// Wrapper around an open HID device.
pub struct NovaDevice {
    dev: HidDevice,
}

impl NovaDevice {
    /// Find and open the base station HID device.
    pub fn open() -> Result<Self, HidError> {
        let api = HidApi::new().map_err(|e| HidError::ApiError(e.to_string()))?;

        let path = api
            .device_list()
            .find(|d| {
                d.vendor_id() == VENDOR_ID
                    && d.product_id() == PRODUCT_ID
                    && d.interface_number() == HID_INTERFACE
            })
            .map(|d| d.path().to_owned())
            .ok_or(HidError::DeviceNotFound)?;

        let dev = api
            .open_path(&path)
            .map_err(|e| HidError::OpenFailed(e.to_string()))?;

        dev.set_blocking_mode(false)
            .map_err(|e| HidError::OpenFailed(e.to_string()))?;

        Ok(NovaDevice { dev })
    }

    /// Send a HID message to the base station.
    fn send(&self, data: &[u8]) -> Result<(), HidError> {
        let mut msg = [0u8; MSG_LEN];
        let len = data.len().min(MSG_LEN);
        msg[..len].copy_from_slice(&data[..len]);
        self.dev.write(&msg).map_err(|_| HidError::Disconnected)?;
        Ok(())
    }

    /// Enable ChatMix mode and Sonar icon on the base station.
    pub fn enable_chatmix(&self) -> Result<(), HidError> {
        self.send(&[TX, OPT_CHATMIX_ENABLE, 1])?;
        self.send(&[TX, OPT_SONAR_ICON, 1])?;
        Ok(())
    }

    /// Disable ChatMix mode and Sonar icon.
    pub fn disable_chatmix(&self) -> Result<(), HidError> {
        let _ = self.send(&[TX, OPT_CHATMIX_ENABLE, 0]);
        let _ = self.send(&[TX, OPT_SONAR_ICON, 0]);
        Ok(())
    }

    /// Read a HID message with timeout (milliseconds). Returns None on timeout.
    pub fn read(&self, timeout_ms: i32) -> Result<Option<Vec<u8>>, HidError> {
        let mut buf = [0u8; MSG_LEN];
        let n = self
            .dev
            .read_timeout(&mut buf, timeout_ms)
            .map_err(|_| HidError::Disconnected)?;
        if n == 0 {
            Ok(None)
        } else {
            Ok(Some(buf[..n].to_vec()))
        }
    }

    /// Parse a raw HID message into a typed event.
    pub fn parse_event(msg: &[u8]) -> HidEvent {
        if msg.len() < 4 {
            return HidEvent::Unknown;
        }
        match msg[1] {
            OPT_CHATMIX => HidEvent::ChatMix {
                game_vol: msg[2],
                chat_vol: msg[3],
            },
            OPT_BATTERY if msg.len() >= 16 => {
                let raw_level = msg[6];
                let level = ((raw_level as u16) * 100 / 8).min(100) as u8;
                let status = match msg[15] {
                    0x01 => "offline",
                    0x02 => "charging",
                    _ => "active",
                };
                HidEvent::Battery(BatteryStatus {
                    level,
                    status: status.to_string(),
                })
            }
            _ => HidEvent::Unknown,
        }
    }

    /// Request battery status from the base station.
    pub fn request_battery(&self) -> Result<(), HidError> {
        self.send(&[TX, OPT_BATTERY])
    }

    /// Poll for battery status (sends request and reads responses until battery or timeout).
    pub fn get_battery(&self) -> Result<Option<BatteryStatus>, HidError> {
        self.request_battery()?;
        for _ in 0..10 {
            if let Some(msg) = self.read(500)? {
                if let HidEvent::Battery(b) = Self::parse_event(&msg) {
                    return Ok(Some(b));
                }
            }
        }
        Ok(None)
    }

    /// Best-effort query of the current ChatMix dial position.
    /// Not all firmware revisions respond to this — returns `Ok(None)` if the
    /// device stays silent, and the caller should fall back to last-known state.
    pub fn get_chatmix(&self) -> Result<Option<(u8, u8)>, HidError> {
        self.send(&[TX, OPT_CHATMIX])?;
        for _ in 0..5 {
            if let Some(msg) = self.read(200)? {
                if let HidEvent::ChatMix { game_vol, chat_vol } = Self::parse_event(&msg) {
                    return Ok(Some((game_vol, chat_vol)));
                }
            }
        }
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pad64(prefix: &[u8]) -> Vec<u8> {
        let mut buf = vec![0u8; MSG_LEN];
        buf[..prefix.len()].copy_from_slice(prefix);
        buf
    }

    #[test]
    fn parse_chatmix_event_decodes_both_channels() {
        // Frame layout: [RX, OPT_CHATMIX, game, chat, ...padding]
        let msg = pad64(&[RX, OPT_CHATMIX, 80, 35]);
        match NovaDevice::parse_event(&msg) {
            HidEvent::ChatMix { game_vol, chat_vol } => {
                assert_eq!(game_vol, 80);
                assert_eq!(chat_vol, 35);
            }
            other => panic!("expected ChatMix, got {other:?}"),
        }
    }

    #[test]
    fn parse_chatmix_handles_zero_and_full_positions() {
        let full = pad64(&[RX, OPT_CHATMIX, 100, 0]);
        let zero = pad64(&[RX, OPT_CHATMIX, 0, 100]);
        assert!(matches!(
            NovaDevice::parse_event(&full),
            HidEvent::ChatMix { game_vol: 100, chat_vol: 0 }
        ));
        assert!(matches!(
            NovaDevice::parse_event(&zero),
            HidEvent::ChatMix { game_vol: 0, chat_vol: 100 }
        ));
    }

    #[test]
    fn parse_battery_scales_raw_to_percent_and_decodes_status() {
        // level byte at index 6, status byte at index 15.
        // raw=8 → 100%, raw=4 → 50%, raw=0 → 0%.
        let mut msg = pad64(&[RX, OPT_BATTERY]);
        msg[6] = 8;
        msg[15] = 0x02; // charging
        match NovaDevice::parse_event(&msg) {
            HidEvent::Battery(b) => {
                assert_eq!(b.level, 100);
                assert_eq!(b.status, "charging");
            }
            other => panic!("expected Battery, got {other:?}"),
        }

        msg[6] = 4;
        msg[15] = 0x01; // offline
        match NovaDevice::parse_event(&msg) {
            HidEvent::Battery(b) => {
                assert_eq!(b.level, 50);
                assert_eq!(b.status, "offline");
            }
            _ => panic!("expected Battery"),
        }

        msg[6] = 0;
        msg[15] = 0x00; // active (default)
        match NovaDevice::parse_event(&msg) {
            HidEvent::Battery(b) => {
                assert_eq!(b.level, 0);
                assert_eq!(b.status, "active");
            }
            _ => panic!("expected Battery"),
        }
    }

    #[test]
    fn parse_battery_clamps_raw_levels_above_scale() {
        // Some firmware revisions have briefly reported raw > 8; we want
        // the decoded percentage capped at 100 rather than overflowing.
        let mut msg = pad64(&[RX, OPT_BATTERY]);
        msg[6] = 12;
        match NovaDevice::parse_event(&msg) {
            HidEvent::Battery(b) => assert_eq!(b.level, 100),
            _ => panic!("expected Battery"),
        }
    }

    #[test]
    fn parse_event_rejects_short_frames() {
        assert!(matches!(NovaDevice::parse_event(&[]), HidEvent::Unknown));
        assert!(matches!(
            NovaDevice::parse_event(&[RX, OPT_CHATMIX, 50]),
            HidEvent::Unknown
        ));
    }

    #[test]
    fn parse_event_returns_unknown_for_unrecognised_opcodes() {
        let msg = pad64(&[RX, 0xFF, 1, 2]);
        assert!(matches!(NovaDevice::parse_event(&msg), HidEvent::Unknown));
    }

    #[test]
    fn battery_needs_full_16_byte_frame_to_decode() {
        // Short battery frames (< 16 bytes) must fall through to Unknown
        // so we don't read past the slice.
        let short = vec![RX, OPT_BATTERY, 0, 0, 0, 0, 8];
        assert!(matches!(
            NovaDevice::parse_event(&short),
            HidEvent::Unknown
        ));
    }
}
