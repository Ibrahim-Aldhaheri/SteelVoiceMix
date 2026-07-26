"""GUI interaction tests for the surround stage editor."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from tests.conftest import pump


def _surround_with_profile(window, qapp):
    win, sent = window
    win.signals.surround_hrir_changed.emit("/tmp/profile.wav")
    win.signals.device_variant_changed.emit("wireless")
    pump(qapp)
    win.surround_tab.stage.resize(360, 360)
    pump(qapp)
    return win, sent


def _press(widget, key):
    widget.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))


def test_stage_hidden_until_profile(window, qapp):
    win, _ = window
    # No profile yet -> stage card hidden. Use isHidden() (the explicit
    # flag) rather than isVisible(), which is False for any widget on a
    # non-current stack page regardless of its own flag.
    assert win.surround_tab._stage_card.isHidden()
    win.signals.surround_hrir_changed.emit("/tmp/p.wav")
    pump(qapp)
    assert not win.surround_tab._stage_card.isHidden()


def _wait(qapp, ms=400):
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    qapp.processEvents()


def test_keyboard_level_and_command(window, qapp):
    win, sent = _surround_with_profile(window, qapp)
    st = win.surround_tab.stage
    st.set_selected("fc")
    for _ in range(4):
        _press(st, Qt.Key_Up)  # +0.5 dB each, live
    assert abs(st.channels()["fc"].gain_db - 2.0) < 1e-6
    # Commit is debounced: it fires once after the last keypress, not
    # once per key (avoids a respawn storm).
    gain_cmds_before = sum(
        1 for c, _ in sent if c == "set-surround-channel-gain"
    )
    _wait(qapp)
    gain_cmds_after = sum(
        1 for c, k in sent
        if c == "set-surround-channel-gain" and k.get("channel") == "fc"
    )
    assert gain_cmds_after >= 1
    # And it debounced to a single commit for the run of 4 presses.
    assert gain_cmds_after - gain_cmds_before <= 1


def test_bounds_pageup_pagedown(window, qapp):
    win, _ = _surround_with_profile(window, qapp)
    st = win.surround_tab.stage
    st.set_selected("sr")
    _press(st, Qt.Key_PageUp)
    assert st.channels()["sr"].gain_db == 6.0
    _press(st, Qt.Key_PageDown)
    assert st.channels()["sr"].gain_db == -24.0


def test_lfe_is_not_radially_editable(window, qapp):
    win, _ = _surround_with_profile(window, qapp)
    st = win.surround_tab.stage
    st.set_selected("lfe")
    before = st.channels()["lfe"].gain_db
    _press(st, Qt.Key_Up)
    assert st.channels()["lfe"].gain_db == before  # non-positional


def test_spinbox_changes_level_and_sends(window, qapp):
    win, sent = _surround_with_profile(window, qapp)
    sur = win.surround_tab
    sur.stage.set_selected("fr")
    sur.level_spin.setValue(-6.0)
    pump(qapp)
    assert sur.stage.channels()["fr"].gain_db == -6.0
    assert any(c == "set-surround-channel-gain" and k.get("channel") == "fr"
               for c, k in sent)


def test_mute_and_solo(window, qapp):
    win, sent = _surround_with_profile(window, qapp)
    sur = win.surround_tab
    sur.stage.set_selected("fc")
    sur.mute_btn.setChecked(True)
    pump(qapp)
    assert sur.stage.channels()["fc"].muted
    assert any(c == "set-surround-channel-mute" for c, _ in sent)
    sur.solo_btn.setChecked(True)
    pump(qapp)
    assert sur.stage.solo() == "fc"


def test_preset_apply_and_undo(window, qapp):
    win, _ = _surround_with_profile(window, qapp)
    sur = win.surround_tab
    keys = [sur.preset_combo.itemData(i) for i in range(sur.preset_combo.count())]
    sur.preset_combo.setCurrentIndex(keys.index("dialogue"))
    sur._on_apply_preset()
    pump(qapp)
    assert sur.stage.channels()["fc"].gain_db == 4.0
    sur._on_undo()
    pump(qapp)
    assert sur.stage.channels()["fc"].gain_db == 0.0


def test_reset_levels(window, qapp):
    win, _ = _surround_with_profile(window, qapp)
    sur = win.surround_tab
    sur.stage.set_gain("fc", 5.0)
    sur._on_reset_levels()
    pump(qapp)
    assert sur.stage.channels()["fc"].gain_db == 0.0


def test_persistence_per_device(window, qapp):
    win, _ = _surround_with_profile(window, qapp)
    sur = win.surround_tab
    sur.stage.set_selected("fc")
    sur.level_spin.setValue(-3.0)
    pump(qapp)
    profiles = win.settings.get("surround_channel_profiles", {})
    assert profiles.get("wireless", {}).get("fc", {}).get("gain_db") == -3.0


def test_keyboard_arrows_cycle_selection(window, qapp):
    # Left/Right move the selection around the ring; real Tab is left
    # to focus traversal (not handled here).
    win, _ = _surround_with_profile(window, qapp)
    st = win.surround_tab.stage
    st.set_selected("fc")
    _press(st, Qt.Key_Right)
    first = st.selected()
    assert first != "fc"
    _press(st, Qt.Key_Left)
    assert st.selected() == "fc"


def test_undo_after_drag_restores(window, qapp):
    # B3 regression: undo must snapshot BEFORE the drag, so undoing a
    # drag actually reverts it.
    win, _ = _surround_with_profile(window, qapp)
    sur = win.surround_tab
    st = sur.stage
    st.set_selected("fr")
    st.drag_started.emit("fr")          # host snapshots here
    st.set_gain("fr", -18.0)            # the "drag"
    st.gain_committed.emit("fr", -18.0)
    pump(qapp)
    assert st.channels()["fr"].gain_db == -18.0
    sur._on_undo()
    pump(qapp)
    assert st.channels()["fr"].gain_db == 0.0


def test_batched_push_always_sends_mute_state(window, qapp):
    # B4 regression: a preset that un-mutes must tell the daemon.
    win, sent = _surround_with_profile(window, qapp)
    sur = win.surround_tab
    keys = [sur.preset_combo.itemData(i) for i in range(sur.preset_combo.count())]
    sur.preset_combo.setCurrentIndex(keys.index("5.1"))
    sur._on_apply_preset()  # mutes sl/sr
    sur.preset_combo.setCurrentIndex(keys.index("flat"))
    sent.clear()
    sur._on_apply_preset()  # un-mutes everything
    batched = [k for c, k in sent if c == "set-surround-gains"]
    assert batched, "expected a batched set-surround-gains command"
    chans = batched[-1]["channels"]
    assert chans["sl"]["muted"] is False and chans["sr"]["muted"] is False


def test_reset_clears_solo_on_daemon(window, qapp):
    # B5 regression: reset must send set-surround-solo "" to the daemon.
    win, sent = _surround_with_profile(window, qapp)
    sur = win.surround_tab
    sur.stage.set_selected("fc")
    sur.solo_btn.setChecked(True)
    sent.clear()
    sur._on_reset_levels()
    solo_cmds = [k for c, k in sent if c == "set-surround-solo"]
    assert any(k.get("channel") == "" for k in solo_cmds)


def test_profile_pushed_to_daemon_on_connect(window, qapp):
    win, sent = _surround_with_profile(window, qapp)
    sent.clear()
    win.surround_tab.on_service_connected()
    assert any(c == "set-surround-gains" for c, _ in sent)


def test_test_tone_blocked_when_surround_off(window, qapp):
    win, sent = _surround_with_profile(window, qapp)
    sur = win.surround_tab
    sur._enabled = False
    sur.stage.set_selected("fc")
    sent.clear()
    sur._on_test()
    assert not any(c == "surround-test-tone" for c, _ in sent)
