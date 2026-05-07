//! USB hotplug watcher for the Arctis Nova Pro Wireless base station.
//!
//! Why this exists: hidapi's file descriptor stays "open" through PC
//! suspend/resume, so reads return `Ok(None)` (timeout) forever even
//! though the kernel has re-enumerated the USB device underneath. The
//! battery-poll watchdog (~3 minutes) catches this eventually, but a
//! libusb hotplug callback gets us a definitive signal in milliseconds.
//!
//! ASM uses pyudev for the same reason. Same idea here, just via
//! rusb (libusb's hotplug API), which on Linux uses udev underneath.
//!
//! What we do NOT do here: own the HID device, claim interfaces, or
//! issue any USB transfer. hidapi continues to drive all I/O. We're a
//! pure event observer.
//!
//! On systems where libusb hotplug isn't available, `start` returns
//! `None` and the daemon falls back to its battery-poll watchdog
//! (still works, just slower).

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use log::{info, warn};
use rusb::{Context, Device, Hotplug, HotplugBuilder, UsbContext};

use crate::hid::{PRODUCT_IDS, VENDOR_ID};

/// Background watcher of USB add/remove events for the Nova Pro
/// Wireless base station. Maintains the `device_present` flag the
/// mixer event loop polls each iteration.
///
/// Drops cleanly: stopping the daemon drops the registration, which
/// deregisters the libusb callback; the watcher thread then exits on
/// the next libusb event loop tick (bounded by the 1s timeout).
pub struct HotplugWatcher {
    // Held to keep the libusb callback registered. Dropping this
    // de-registers; that's deliberate — when the daemon shuts down,
    // we want the kernel-side observer gone too.
    _registration: rusb::Registration<Context>,
}

struct Callback {
    device_present: Arc<AtomicBool>,
}

fn matches_nova_pro(device: &Device<Context>) -> bool {
    device
        .device_descriptor()
        .map(|desc| {
            desc.vendor_id() == VENDOR_ID && PRODUCT_IDS.contains(&desc.product_id())
        })
        .unwrap_or(false)
}

impl Hotplug<Context> for Callback {
    fn device_arrived(&mut self, device: Device<Context>) {
        // libusb fires for every SteelSeries device on the bus
        // because we filter by VID only (the builder doesn't take
        // a PID list). Drop events for non-Nova-Pro PIDs.
        if !matches_nova_pro(&device) {
            return;
        }
        info!("USB hotplug: Nova Pro arrived");
        self.device_present.store(true, Ordering::Relaxed);
    }

    fn device_left(&mut self, device: Device<Context>) {
        if !matches_nova_pro(&device) {
            return;
        }
        info!("USB hotplug: Nova Pro left");
        self.device_present.store(false, Ordering::Relaxed);
    }
}

/// Start the watcher. Returns `None` when libusb's hotplug API isn't
/// available at runtime — the daemon's battery-poll watchdog still
/// catches stale-fd cases, just on a slower cadence.
pub fn start(device_present: Arc<AtomicBool>) -> Option<HotplugWatcher> {
    if !rusb::has_hotplug() {
        warn!("libusb hotplug API not available — falling back to battery-poll watchdog only");
        return None;
    }

    let ctx = match Context::new() {
        Ok(c) => c,
        Err(e) => {
            warn!("Failed to create libusb context for hotplug watcher: {e}");
            return None;
        }
    };

    // Synchronous initial enumerate — seeds `device_present` BEFORE
    // returning, so the mixer's first event-loop iteration doesn't
    // race the watcher thread's callback. Enumerate=true on the
    // builder also fires the async callback once for already-present
    // devices, but that runs on a separate thread; without this
    // seed, the mixer can flap connect/disconnect on startup.
    if let Ok(devices) = ctx.devices() {
        let already_present = devices.iter().any(|d| matches_nova_pro(&d));
        if already_present {
            device_present.store(true, Ordering::Relaxed);
        }
    }

    // Filter by VID only — the builder takes one PID at a time and
    // we want to catch all three Nova Pro variants (Wireless +
    // Wired-USB + Wired-Xbox). The callback re-checks the PID via
    // matches_nova_pro() to ignore unrelated SteelSeries devices.
    // `enumerate=true` fires `device_arrived` for already-present
    // devices too, so the flag is always seeded correctly.
    let registration = match HotplugBuilder::new()
        .vendor_id(VENDOR_ID)
        .enumerate(true)
        .register(
            &ctx,
            Box::new(Callback {
                device_present: device_present.clone(),
            }),
        ) {
        Ok(r) => r,
        Err(e) => {
            warn!("Failed to register libusb hotplug callback: {e}");
            return None;
        }
    };

    // Drive the libusb event loop on a dedicated thread. handle_events
    // dispatches our callback. The 1s timeout means a clean shutdown
    // (registration drop) takes effect within ~1s.
    thread::spawn(move || loop {
        match ctx.handle_events(Some(Duration::from_secs(1))) {
            Ok(()) => {}
            Err(e) => {
                warn!("libusb handle_events error: {e}");
                // Don't tight-loop on persistent errors — usually a
                // transient bus state during suspend transitions.
                thread::sleep(Duration::from_secs(1));
            }
        }
    });

    info!(
        "USB hotplug watcher armed for VID 0x{:04x} PIDs {:?}",
        VENDOR_ID, PRODUCT_IDS
    );
    Some(HotplugWatcher {
        _registration: registration,
    })
}
