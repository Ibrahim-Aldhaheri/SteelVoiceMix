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
    /// False on wireless variants whose firmware rejects the larger
    /// Feature reports used for bitmap draws. Brightness (a small
    /// Output report) still works in this mode, so the handle stays
    /// alive even though `show()` becomes a no-op.
    can_draw: bool,
}

impl ChatMixGauge {
    /// Connect to the OLED display and set initial brightness.
    /// `brightness` is clamped to 1..=10. Returns Ok whenever the
    /// device opens and accepts the brightness write — even if the
    /// gauge-draw probe fails, since brightness is the user-facing
    /// capability the GUI gates the Deck tab on.
    pub fn new(brightness: u8) -> Result<Self, String> {
        let device = Device::connect().map_err(|e| format!("OLED open failed: {e}"))?;
        let level = brightness.clamp(1, 10);
        device
            .set_brightness(level)
            .map_err(|e| format!("OLED brightness failed: {e}"))?;
        // Probe with a blank frame — wireless variants accept
        // connect() + set_brightness() (Output report) but reject
        // feature reports with EINVAL. Capture the verdict here so
        // every subsequent show() short-circuits without wasting
        // ~970 ms per attempt inside ggoled_lib.
        let blank = Bitmap::new(DISPLAY_W, DISPLAY_H, false);
        let can_draw = match device.draw(&blank, 0, 0) {
            Ok(()) => true,
            Err(e) => {
                warn!("OLED gauge unsupported on this firmware ({e}) — brightness still controllable");
                false
            }
        };
        info!("OLED display connected (gauge={})", if can_draw { "supported" } else { "unsupported" });
        Ok(ChatMixGauge { device, can_draw })
    }

    /// True iff this device accepts the larger Feature reports used
    /// for bitmap draws. Wireless variants return false here.
    pub fn can_draw(&self) -> bool {
        self.can_draw
    }

    /// Update brightness post-connect. Wireless variants typically
    /// accept this even when they reject the larger draw feature
    /// reports, so it works whether or not the gauge is being drawn.
    /// `level` is clamped to 1..=10.
    pub fn set_brightness(&self, level: u8) -> bool {
        let clamped = level.clamp(1, 10);
        match self.device.set_brightness(clamped) {
            Ok(()) => true,
            Err(e) => {
                warn!("OLED set_brightness({clamped}) failed: {e}");
                false
            }
        }
    }

    /// Draw the ChatMix gauge with current game/chat volumes (0-100).
    /// Returns `false` only on a runtime draw failure (so the caller
    /// can drop the handle); known-unsupported devices return `true`
    /// from a fast no-op so the handle survives for brightness control.
    pub fn show(&mut self, game_vol: u8, chat_vol: u8) -> bool {
        if !self.can_draw {
            return true;
        }
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
            return false;
        }
        true
    }

    /// Blank the display. No-op on devices that don't accept draws.
    pub fn clear(&mut self) {
        if !self.can_draw {
            return;
        }
        let bmp = Bitmap::new(DISPLAY_W, DISPLAY_H, false);
        if let Err(e) = self.device.draw(&bmp, 0, 0) {
            warn!("OLED clear failed: {e}");
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
