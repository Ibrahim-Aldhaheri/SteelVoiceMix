"""Interaction tests — drive real widgets and assert real behaviour."""
from __future__ import annotations

from tests.conftest import pump


# --------------------------------------------------------------- Home

def test_home_quick_actions_navigate(window, qapp):
    win, _ = window
    win.home_tab.navigate.emit("equalizer")
    pump(qapp)
    assert win.nav.currentRow() == win._page_index["equalizer"]
    win.home_tab.navigate.emit("microphone")
    pump(qapp)
    assert win.nav.currentRow() == win._page_index["microphone"]


def test_home_battery_before_presence_recovers(window, qapp):
    """A battery event arriving before the presence event must still
    show the reading (was a stranding bug)."""
    win, _ = window
    win.signals.connected.emit()
    win.signals.battery_updated.emit(63, "discharging")
    pump(qapp)
    assert win.home_tab.battery_value.text() == "63%"
    assert win.home_tab.device_status.text() == "Ready"


def test_home_disconnected_shows_service_message(window, qapp):
    win, _ = window
    win.signals.disconnected.emit()
    pump(qapp)
    assert "service" in win.home_tab.device_status.text().lower()


def test_home_low_battery_wording(window, qapp):
    win, _ = window
    win.signals.connected.emit()
    win.signals.oled_presence_changed.emit(True)
    win.signals.battery_updated.emit(8, "discharging")
    pump(qapp)
    assert "critical" in win.home_tab.device_status.text().lower()


# --------------------------------------------------------------- Equalizer

def test_eq_toggle_and_slider_send_commands(window, qapp):
    win, sent = window
    eq = win.eq_tab
    eq.eq_toggle.setChecked(True)
    pump(qapp)
    eq.band_sliders[0].setValue(40)
    eq.band_sliders[0].sliderReleased.emit()
    pump(qapp)
    cmds = [c for c, _ in sent]
    assert "set-eq-enabled" in cmds
    assert any(c.startswith("set-eq-band") for c in cmds)


def test_eq_channel_switch(window, qapp):
    win, _ = window
    eq = win.eq_tab
    eq.channel_combo.setCurrentIndex(1)
    pump(qapp)
    assert eq._current_channel == "chat"


def test_eq_channel_combo_has_no_emoji(window):
    win, _ = window
    eq = win.eq_tab
    texts = [eq.channel_combo.itemText(i) for i in range(eq.channel_combo.count())]
    for t in texts:
        assert all(ord(ch) < 0x2600 for ch in t), f"emoji in channel label: {t!r}"


# --------------------------------------------------------------- Surround

def test_surround_profile_then_enable_flow(window, qapp):
    win, sent = window
    sur = win.surround_tab
    # Toggle disabled until a profile is set.
    assert not sur.enable_toggle.isEnabled()
    sur.on_hrir_changed("/tmp/profile.wav")
    pump(qapp)
    assert sur.enable_toggle.isEnabled()
    sur.enable_toggle.setChecked(True)
    pump(qapp)
    assert ("set-surround-enabled", {"enabled": True}) in sent


def test_surround_clearing_profile_disables(window, qapp):
    win, _ = window
    sur = win.surround_tab
    sur.on_hrir_changed("/tmp/profile.wav")
    sur.on_enabled_changed(True)
    pump(qapp)
    sur.on_hrir_changed("")
    pump(qapp)
    assert not sur.enable_toggle.isEnabled()
    assert not sur.enable_toggle.isChecked()


# --------------------------------------------------------------- Sinks

def test_sink_toggle_sends_command(window, qapp):
    win, sent = window
    win.sinks_tab.media_btn.click()
    pump(qapp)
    assert any("media" in c for c, _ in sent)
