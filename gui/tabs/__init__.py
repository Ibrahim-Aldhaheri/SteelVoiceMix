"""Per-tab widgets for the main window.

Each tab is a self-contained QWidget that knows how to:
  - build its own UI
  - receive update calls from the daemon-event router in main_window
  - send user-initiated commands back via a daemon-client handle

The main window wires daemon signals to tab methods and delegates user
input handling to the tabs themselves. Keeps main_window.py focused on
window chrome (header, tabs, footer, tray) instead of feature logic.
"""
