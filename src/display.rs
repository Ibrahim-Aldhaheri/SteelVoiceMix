//! OLED display support for the Arctis Nova Pro base station screen.
//! Renders a ChatMix gauge with game/chat volume bars.

use ggoled_lib::{Bitmap, Device};
use log::{info, warn};

const DISPLAY_W: usize = 128;
const DISPLAY_H: usize = 64;

// Bar layout
const BAR_WIDTH: usize = 100;
const BAR_HEIGHT: usize = 20;
const BAR_LEFT: usize = (DISPLAY_W - BAR_WIDTH) / 2;
const GAME_BAR_Y: usize = 12;
const CHAT_BAR_Y: usize = 44;

/// ChatMix gauge rendered on the OLED display.
pub struct ChatMixGauge {
    device: Device,
}

impl ChatMixGauge {
    /// Connect to the OLED display and set initial brightness.
    pub fn new() -> Result<Self, String> {
        let device = Device::connect().map_err(|e| format!("OLED open failed: {e}"))?;
        device
            .set_brightness(5)
            .map_err(|e| format!("OLED brightness failed: {e}"))?;
        info!("OLED display connected");
        Ok(ChatMixGauge { device })
    }

    /// Draw the ChatMix gauge with current game/chat volumes (0-100).
    pub fn show(&mut self, game_vol: u8, chat_vol: u8) {
        let mut bmp = Bitmap::new(DISPLAY_W, DISPLAY_H, false);

        // Draw game bar background (outline)
        fill_rect(&mut bmp, BAR_LEFT, GAME_BAR_Y, BAR_WIDTH, BAR_HEIGHT, true);
        // Clear interior for background effect
        fill_rect(
            &mut bmp,
            BAR_LEFT + 1,
            GAME_BAR_Y + 1,
            BAR_WIDTH - 2,
            BAR_HEIGHT - 2,
            false,
        );
        // Draw game fill
        let game_fill = BAR_WIDTH.saturating_sub(2) * (game_vol as usize) / 100;
        if game_fill > 0 {
            fill_rect(
                &mut bmp,
                BAR_LEFT + 1,
                GAME_BAR_Y + 1,
                game_fill,
                BAR_HEIGHT - 2,
                true,
            );
        }

        // Draw chat bar background (outline)
        fill_rect(&mut bmp, BAR_LEFT, CHAT_BAR_Y, BAR_WIDTH, BAR_HEIGHT, true);
        // Clear interior
        fill_rect(
            &mut bmp,
            BAR_LEFT + 1,
            CHAT_BAR_Y + 1,
            BAR_WIDTH - 2,
            BAR_HEIGHT - 2,
            false,
        );
        // Draw chat fill
        let chat_fill = BAR_WIDTH.saturating_sub(2) * (chat_vol as usize) / 100;
        if chat_fill > 0 {
            fill_rect(
                &mut bmp,
                BAR_LEFT + 1,
                CHAT_BAR_Y + 1,
                chat_fill,
                BAR_HEIGHT - 2,
                true,
            );
        }

        if let Err(e) = self.device.draw(&bmp, 0, 0) {
            warn!("OLED draw failed: {e}");
        }
    }

    /// Blank the display.
    pub fn clear(&mut self) {
        let bmp = Bitmap::new(DISPLAY_W, DISPLAY_H, false);
        if let Err(e) = self.device.draw(&bmp, 0, 0) {
            warn!("OLED clear failed: {e}");
        }
    }

    /// Set display brightness (0-10).
    pub fn set_brightness(&mut self, value: u8) {
        if let Err(e) = self.device.set_brightness(value) {
            warn!("OLED set brightness failed: {e}");
        }
    }
}

fn fill_rect(bmp: &mut Bitmap, x: usize, y: usize, w: usize, h: usize, value: bool) {
    for row in y..y + h {
        for col in x..x + w {
            if row < bmp.h && col < bmp.w {
                bmp.data.set(row * bmp.w + col, value);
            }
        }
    }
}
