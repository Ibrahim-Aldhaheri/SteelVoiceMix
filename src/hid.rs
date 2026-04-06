//! HID communication with the SteelSeries Arctis Nova Pro Wireless base station.

use std::fmt;

use hidapi::{HidApi, HidDevice};

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
#[derive(Debug)]
pub enum HidError {
    DeviceNotFound,
    OpenFailed(String),
    Disconnected,
    ApiError(String),
}

impl fmt::Display for HidError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            HidError::DeviceNotFound => write!(f, "Base station not found"),
            HidError::OpenFailed(e) => write!(f, "Failed to open device: {e}"),
            HidError::Disconnected => write!(f, "Device disconnected"),
            HidError::ApiError(e) => write!(f, "HID API error: {e}"),
        }
    }
}

impl std::error::Error for HidError {}

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
}
