# ============================================================================
# Variable Table - PySide6 Variable Display Component
# ============================================================================

import time
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QPushButton, QLabel
from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QColor
from datetime import datetime

# How long a row stays highlighted after its value actually changes, and
# what colors it fades between. A row that's still highlighted is "live";
# a row that's back to normal hasn't changed recently - which is exactly
# what makes stale data visually obvious.
HIGHLIGHT_DURATION_S = 1.5
HIGHLIGHT_COLOR = QColor(39, 174, 96)     # green flash on change
NORMAL_BG_COLOR = QColor(0x3c, 0x3c, 0x3c)  # matches the table's existing #3c3c3c background


class VariableTable(QWidget):
    """Variable table display component"""

    # Emitted when the user clicks Refresh. The owning app should send a
    # LIST command to the device AND call sync_variables() so the table
    # only ever shows variables currently known to the project (nothing
    # left behind from a previous firmware/session).
    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.variables = {}
        self.last_changed = {}  # var_name -> time.time() of its last actual VALUE change
        self.setup_ui()

        # Periodic fade tick: refresh() only naturally re-runs when some
        # variable's value updates. Without this, a row that stops
        # updating would stay highlighted forever (since nothing would
        # ever repaint it), defeating the point of a staleness indicator.
        # This timer re-evaluates every row's highlight state on a clock,
        # independent of whether new data has arrived.
        self.fade_timer = QTimer(self)
        self.fade_timer.timeout.connect(self.refresh)
        self.fade_timer.start(200)

    def setup_ui(self):
        """Build UI"""
        layout = QVBoxLayout(self)

        # Header
        header_layout = QHBoxLayout()
        title = QLabel("Variables")
        title.setStyleSheet("font-weight: bold; font-size: 11pt;")
        header_layout.addWidget(title)

        self.refresh_btn = QPushButton("🔄 Refresh")
        self.refresh_btn.setMaximumWidth(100)
        self.refresh_btn.setStyleSheet("background-color: #007acc; color: white; padding: 2px 8px;")
        self.refresh_btn.clicked.connect(self.on_refresh_clicked)
        header_layout.addWidget(self.refresh_btn)

        header_layout.addStretch()
        layout.addLayout(header_layout)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["ID", "Name", "Value", "Status", "Updated"])
        self.table.setStyleSheet("background-color: #3c3c3c; color: #d4d4d4;")
        layout.addWidget(self.table)

    def on_refresh_clicked(self):
        """Refresh button handler: just repaint now, then ask the owning
        app (main_pyside6.py) to re-query the device and prune stale rows."""
        self.refresh()
        self.refresh_requested.emit()

    def set_refresh_enabled(self, enabled: bool):
        """Enable/disable the Refresh button. Used by the owning app to
        block re-clicks while a LIST request is in flight - without this,
        rapid re-clicks each reset the in-progress LIST snapshot before
        the previous one's CONFIRM:LIST could ever land, so it looked like
        Refresh was permanently stuck doing nothing."""
        self.refresh_btn.setEnabled(enabled)
        self.refresh_btn.setText("🔄 Refreshing..." if not enabled else "🔄 Refresh")

    def clear_all(self):
        """Wipe every row - used when flashing starts, since whatever
        firmware is about to replace the current one may have a completely
        different variable set. Simpler and more immediate than waiting on
        a LIST round-trip after reconnect."""
        self.variables = {}
        self.last_changed = {}
        self.refresh()

    def sync_variables(self, current_vars: dict):
        """Rebuild table state from current_vars ONLY - any variable
        previously shown but absent from current_vars (e.g. left over from
        a prior firmware/session) is dropped instead of lingering forever."""
        new_variables = {}
        new_last_changed = {}

        for name, data in current_vars.items():
            value = data.get("value", 0) if isinstance(data, dict) else data
            status = data.get("status", "OK") if isinstance(data, dict) else "OK"
            prev = self.variables.get(name)
            changed = prev is None or str(prev.get("value")) != str(value)

            new_variables[name] = {
                "value": value,
                "status": status,
                "timestamp": datetime.now().isoformat()
            }
            # Preserve existing highlight timing for unchanged vars,
            # otherwise treat as freshly changed.
            new_last_changed[name] = self.last_changed.get(name) if not changed and name in self.last_changed else time.time()

        self.variables = new_variables
        self.last_changed = new_last_changed
        self.refresh()

    def add_variable(self, name, value, status="OK"):
        """Add or update variable"""
        prev = self.variables.get(name)
        changed = prev is None or str(prev.get("value")) != str(value)

        self.variables[name] = {
            "value": value,
            "status": status,
            "timestamp": datetime.now().isoformat()
        }
        if changed:
            self.last_changed[name] = time.time()
        self.refresh()

    def set_value(self, name, value):
        """Set variable value"""
        if name in self.variables:
            changed = str(self.variables[name]["value"]) != str(value)
            self.variables[name]["value"] = value
            self.variables[name]["timestamp"] = datetime.now().isoformat()
            if changed:
                self.last_changed[name] = time.time()
            self.refresh()

    @staticmethod
    def _lerp_color(c1, c2, frac):
        """Linear interpolation between two QColors, frac clamped to [0, 1]"""
        frac = max(0.0, min(1.0, frac))
        r = int(c1.red() + (c2.red() - c1.red()) * frac)
        g = int(c1.green() + (c2.green() - c1.green()) * frac)
        b = int(c1.blue() + (c2.blue() - c1.blue()) * frac)
        return QColor(r, g, b)

    def _row_color(self, name):
        """Highlight color for a row based on how recently its value changed"""
        last_change = self.last_changed.get(name)
        if last_change is None:
            return NORMAL_BG_COLOR

        elapsed = time.time() - last_change
        if elapsed >= HIGHLIGHT_DURATION_S:
            return NORMAL_BG_COLOR

        # Fade from HIGHLIGHT_COLOR (just changed) to NORMAL_BG_COLOR
        # (fully faded) over HIGHLIGHT_DURATION_S seconds.
        frac = elapsed / HIGHLIGHT_DURATION_S
        return self._lerp_color(HIGHLIGHT_COLOR, NORMAL_BG_COLOR, frac)

    def refresh(self):
        """Refresh table display, highlighting rows that changed recently"""
        self.table.setRowCount(len(self.variables))

        for idx, (name, data) in enumerate(self.variables.items()):
            bg = self._row_color(name)

            id_item = QTableWidgetItem(str(idx + 1))
            name_item = QTableWidgetItem(name)
            value_item = QTableWidgetItem(str(data["value"]))
            status_item = QTableWidgetItem(data["status"])

            timestamp = data["timestamp"]
            if isinstance(timestamp, str):
                timestamp = timestamp.split('T')[-1][:8]
            updated_item = QTableWidgetItem(str(timestamp))

            for item in (id_item, name_item, value_item, status_item, updated_item):
                item.setBackground(bg)

            self.table.setItem(idx, 0, id_item)
            self.table.setItem(idx, 1, name_item)
            self.table.setItem(idx, 2, value_item)
            self.table.setItem(idx, 3, status_item)
            self.table.setItem(idx, 4, updated_item)

    def get_variable(self, name):
        """Get variable data"""
        return self.variables.get(name)

    def get_all_variables(self):
        """Get all variables"""
        return self.variables.copy()