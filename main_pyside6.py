# ============================================================================
# STM32 Debugger - Dynamic PySide6 Application
# ============================================================================
# Entry point: python main_pyside6.py

import sys
import csv
import time
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QMessageBox, QFileDialog, QTabWidget, QLineEdit, QPushButton, QLabel,
    QComboBox
)
from PySide6.QtCore import QTimer

# Import custom components
from gui.Theme import Theme
from gui.VisualizationPanel import VisualizationPanel
from gui.EventLog import EventLog
from gui.VariableTable import VariableTable
from gui.StatusBar import StatusBar
from gui.FlashDialog import FlashDialog
from gui.PathPanel import PathPanel
from gui.PIDTunerPanel import PIDTunerPanel
from gui.ArUcoPanel import ArUcoPanel

# Config is the single source of truth for ESP32_IP / ESP32_PORT
try:
    from backend.config import ESP32_IP, ESP32_PORT
except ImportError:
    ESP32_IP, ESP32_PORT = "10.99.230.117", 5000

# Try loading real backend data structures and bridge
try:
    from backend.tcp_client import DebugBridge
    from backend import data_store
except ImportError as e:
    import traceback
    print(f"Warning: Using mock backend - real import failed: {e}")
    traceback.print_exc()

    class MockDataStore:
        variables = {}
        events = []

        @staticmethod
        def start_recording(ip=None): pass

        @staticmethod
        def stop_recording(): pass

        @staticmethod
        def is_list_in_progress(): return False

        @staticmethod
        def start_list_snapshot(): pass

        @staticmethod
        def abort_list_snapshot(): pass


    data_store = MockDataStore()


    class MockDebugBridge:
        def __init__(self, ip=None, port=None):
            self.ip = ip or ESP32_IP
            self.port = port or ESP32_PORT
            self.connected = True
            self.connecting = False
            self.stm32_alive = True
            self.latest_latency_ms = 12.5

        def connect(self): pass

        def disconnect(self): pass

        def send_command(self, cmd): print(f"[Mock] {cmd}")

        def claim_motion(self, axes=7): pass


    DebugBridge = MockDebugBridge


# ============================================================================
# Main Application Window
# ============================================================================

class STM32DebuggerApp(QMainWindow):
    """Complete STM32 Debugger Application with Dynamic Variable Support"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("STM32 Debug Bridge - Dynamic Debugger (PySide6)")
        self.setGeometry(50, 50, 1500, 950)

        # Bridge & Network
        self.bridge = DebugBridge(ESP32_IP, ESP32_PORT)
        self.bridge.connect()
        self.esp32_ip = ESP32_IP

        # Auto-reconnect Tracking
        self.is_reconnecting = False
        self.last_reconnect_time = 0.0
        self._reconnect_attempt_started = False
        # FIX: update_gui() below runs off a QTimer that keeps firing every
        # 100ms even while a modal QDialog (like FlashDialog) is exec()'d -
        # QDialog.exec() only blocks input, not the rest of the event loop.
        # Without this flag, disconnecting the bridge in open_flash_dialog()
        # made update_gui() see "not connected" and auto-reconnect the
        # DebugBridge every ~2s for the whole duration of the flash,
        # fighting the flasher for the ESP32's single TCP client slot.
        self.flashing_in_progress = False

        # Dynamic Data Tracking
        self.known_variables = set()
        self.known_events_count = 0
        self.recording = False
        self.recorded_samples = []
        self.demo_step = 0
        self.flash_worker = None

        # LIST-refresh tracking: a monotonically increasing token so a
        # timeout from an old click can't misfire after a newer refresh
        # already completed successfully.
        self._list_refresh_token = 0
        self.LIST_TIMEOUT_MS = 3000

        # Apply theme
        Theme.apply(QApplication.instance())

        # Build UI
        self.setup_ui()

        self.var_table.refresh_requested.connect(self.on_variable_refresh)

        # Timers
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_gui)
        self.update_timer.start(100)

    def register_variable(self, name, value):
        """Auto-register new variable when STM32 sends it"""
        if name not in self.known_variables:
            self.known_variables.add(name)
            self.var_table.add_variable(name, value, "OK")
            self.viz_panel.add_variable(name)
            self.event_log.add_event(f"Registered variable: {name}", "INFO")

    def auto_reconnect_bridge(self):
        """Continuously attempt background reconnection when disconnected"""
        if self.is_reconnecting or self.bridge.connecting:
            return

        self.is_reconnecting = True
        self._reconnect_attempt_started = False
        if hasattr(self, 'bridge') and self.bridge:
            try:
                self.bridge.disconnect()
            except Exception:
                pass

            QTimer.singleShot(500, self._do_auto_reconnect)

    def _do_auto_reconnect(self):
        if hasattr(self, 'bridge') and self.bridge:
            try:
                self.bridge.connect()
            except Exception:
                pass
        self._reconnect_attempt_started = True

    def switch_tab(self, index):
        """Switch view tab and sync button active states.

        Indices: 0=Variables, 1=Events, 2=Graphs, 3=Path, 4=PID Tuner,
                 5=ArUco.
        """
        self.tabs.setCurrentIndex(index)
        self.btn_tab_vars.setChecked(index == 0)
        self.btn_tab_events.setChecked(index == 1)
        self.btn_tab_graphs.setChecked(index == 2)
        self.btn_tab_path.setChecked(index == 3)
        self.btn_tab_pid.setChecked(index == 4)
        self.btn_tab_aruco.setChecked(index == 5)

    def setup_ui(self):
        """Build complete UI"""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Content Area
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(10, 10, 10, 10)
        content_layout.setSpacing(10)

        # Command bar with Dropdown
        cmd_layout = QHBoxLayout()
        cmd_layout.addWidget(QLabel("Command:"))

        self.cmd_type_combo = QComboBox()
        self.cmd_type_combo.addItems(["SET:", "GET:", "LIST"])
        self.cmd_type_combo.setStyleSheet("background-color: #252526; color: white; padding: 4px;")
        self.cmd_type_combo.currentTextChanged.connect(self.on_cmd_type_changed)
        cmd_layout.addWidget(self.cmd_type_combo)

        self.cmd_entry = QLineEdit()
        self.cmd_entry.setPlaceholderText("Distance=50")
        self.cmd_entry.returnPressed.connect(self.send_command)
        cmd_layout.addWidget(self.cmd_entry)

        send_btn = QPushButton("Send")
        send_btn.setMaximumWidth(80)
        send_btn.setStyleSheet("background-color: #27ae60; color: white;")
        send_btn.clicked.connect(self.send_command)
        cmd_layout.addWidget(send_btn)

        content_layout.addLayout(cmd_layout)

        # Controls Bar (Firmware Flashing + View Buttons + Recording side-by-side)
        ctrl_layout = QHBoxLayout()

        # Firmware Controls
        ctrl_layout.addWidget(QLabel("Firmware:"))

        self.flash_btn = QPushButton("🔧 Flash STM32F401")
        self.flash_btn.setMaximumWidth(200)
        self.flash_btn.setStyleSheet("background-color: #007acc; color: white;")
        self.flash_btn.clicked.connect(self.open_flash_dialog)
        ctrl_layout.addWidget(self.flash_btn)

        ctrl_layout.addSpacing(15)

        # Tab Navigation Buttons (Variables, Events, Graphs, Path)
        tab_btn_style = """
            QPushButton {
                background-color: #2d2d2d;
                color: #d4d4d4;
                border: 1px solid #3c3c3c;
                padding: 5px 14px;
                border-radius: 3px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3e3e42;
            }
            QPushButton:checked {
                background-color: #007acc;
                color: white;
                border: 1px solid #0098ff;
            }
        """

        self.btn_tab_vars = QPushButton("📋 Variables")
        self.btn_tab_events = QPushButton("📝 Events")
        self.btn_tab_graphs = QPushButton("📊 Graphs")
        self.btn_tab_path = QPushButton("🗺 Path")
        self.btn_tab_pid = QPushButton("🎛 PID Tuner")
        self.btn_tab_aruco = QPushButton("🎯 ArUco")

        for btn in (self.btn_tab_vars, self.btn_tab_events,
                    self.btn_tab_graphs, self.btn_tab_path, self.btn_tab_pid,
                    self.btn_tab_aruco):
            btn.setStyleSheet(tab_btn_style)
            btn.setCheckable(True)

        self.btn_tab_vars.setChecked(True)

        self.btn_tab_vars.clicked.connect(lambda: self.switch_tab(0))
        self.btn_tab_events.clicked.connect(lambda: self.switch_tab(1))
        self.btn_tab_graphs.clicked.connect(lambda: self.switch_tab(2))
        self.btn_tab_path.clicked.connect(lambda: self.switch_tab(3))
        self.btn_tab_pid.clicked.connect(lambda: self.switch_tab(4))
        self.btn_tab_aruco.clicked.connect(lambda: self.switch_tab(5))

        ctrl_layout.addWidget(self.btn_tab_vars)
        ctrl_layout.addWidget(self.btn_tab_events)
        ctrl_layout.addWidget(self.btn_tab_graphs)
        ctrl_layout.addWidget(self.btn_tab_path)
        ctrl_layout.addWidget(self.btn_tab_pid)
        ctrl_layout.addWidget(self.btn_tab_aruco)

        ctrl_layout.addSpacing(25)

        # Recording Controls
        ctrl_layout.addWidget(QLabel("Recording:"))

        self.record_btn = QPushButton("⏺ Start")
        self.record_btn.setMaximumWidth(100)
        self.record_btn.setStyleSheet("background-color: #c0392b; color: white;")
        self.record_btn.clicked.connect(self.toggle_recording)
        ctrl_layout.addWidget(self.record_btn)

        export_btn = QPushButton("⬇ Export CSV")
        export_btn.setMaximumWidth(120)
        export_btn.setStyleSheet("background-color: #8e44ad; color: white;")
        export_btn.clicked.connect(self.export_csv)
        ctrl_layout.addWidget(export_btn)

        self.record_status = QLabel("⚪ Not Recording | Samples: 0")
        ctrl_layout.addWidget(self.record_status)

        ctrl_layout.addStretch()
        content_layout.addLayout(ctrl_layout)

        # Tabs (Hide native tab bar to use the custom buttons above)
        self.tabs = QTabWidget()
        self.tabs.tabBar().hide()

        self.var_table = VariableTable()
        self.tabs.addTab(self.var_table, "📋 Variables")

        self.event_log = EventLog()
        self.event_log.add_event("System initialized", "INFO")
        self.tabs.addTab(self.event_log, "📝 Events")

        self.viz_panel = VisualizationPanel()
        self.tabs.addTab(self.viz_panel, "📊 Graphs")

        self.path_panel = PathPanel(bridge=self.bridge)
        self.tabs.addTab(self.path_panel, "🗺 Path")

        # PID Tuner: gain editing, live P/I/D graph, step/path tests and the
        # auto-tune loop. It logs its own status lines into the Event Log.
        self.pid_tuner = PIDTunerPanel(bridge=self.bridge,
                                       event_log=self.event_log)
        self.tabs.addTab(self.pid_tuner, "🎛 PID Tuner")

        # ArUco: camera preview + detection on the PC, pose streamed to the
        # STM32 over the ESP32 bridge (SET:aruco_en=1 arms it).
        self.aruco_panel = ArUcoPanel(bridge=self.bridge,
                                      event_log=self.event_log)
        self.tabs.addTab(self.aruco_panel, "🎯 ArUco")

        content_layout.addWidget(self.tabs)
        main_layout.addWidget(content)

        # Status Bar
        self.status_bar = StatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.set_connection(self.bridge.connected, self.bridge.stm32_alive)

    def on_cmd_type_changed(self, text):
        """Update placeholder and enablement based on command type"""
        if text == "SET:":
            self.cmd_entry.setEnabled(True)
            self.cmd_entry.setPlaceholderText("Distance=50")
        elif text == "GET:":
            self.cmd_entry.setEnabled(True)
            self.cmd_entry.setPlaceholderText("Distance")
        elif text == "LIST":
            self.cmd_entry.clear()
            self.cmd_entry.setEnabled(False)
            self.cmd_entry.setPlaceholderText("No arguments needed")

    def send_command(self):
        """Send formatted command over UART bridge"""
        prefix = self.cmd_type_combo.currentText()
        arg = self.cmd_entry.text().strip()

        if prefix == "LIST":
            command = "LIST"
        else:
            if not arg:
                return
            command = f"{prefix}{arg}"

        self.bridge.send_command(command)
        self.cmd_entry.clear()

    def update_gui(self):
        """Periodically sync with backend data_store and update UI"""
        # Auto-reconnect handler: continuously retry every 2 seconds if connection is lost
        if not self.flashing_in_progress and not self.bridge.connected and not self.bridge.connecting and not self.is_reconnecting:
            now = time.time()
            if now - self.last_reconnect_time > 2.0:
                self.last_reconnect_time = now
                self.auto_reconnect_bridge()

        # Detect when an in-flight reconnect attempt has finished
        if self.is_reconnecting and self._reconnect_attempt_started and not self.bridge.connecting:
            self.is_reconnecting = False
            if self.bridge.connected:
                self.event_log.add_event("Reconnected to ESP32 bridge", "SUCCESS")

        # Connection Status for Bottom Status Bar
        self.status_bar.set_connection(self.bridge.connected, self.bridge.stm32_alive)

        # Keep the PID Tuner's test buttons in step with the link. It stays
        # disabled until the STM32 is actually alive, so a test can't be
        # started against a half-open link (same rule as the LIST refresh).
        self.pid_tuner.set_link_state(self.bridge.connected, self.bridge.stm32_alive)

        # Same rule for the ArUco tab: no arming until the STM32 is alive.
        self.aruco_panel.set_link_state(self.bridge.connected, self.bridge.stm32_alive)

        if self.bridge.latest_latency_ms is not None:
            self.status_bar.set_latency(self.bridge.latest_latency_ms)
        else:
            self.status_bar.set_latency(0.0)

        # Keep the Refresh button disabled until the STM32 has actually
        # said it's alive (not just "TCP socket connected") - stops LIST
        # from being sent into a device that isn't listening on UART yet
        # (e.g. mid-reboot right after a flash). Don't fight the "in
        # flight" disable from a click that's already awaiting CONFIRM:LIST.
        if not data_store.is_list_in_progress():
            self.var_table.set_refresh_enabled(self.bridge.connected and self.bridge.stm32_alive)

        # Update Bottom Status Bar
        self.status_bar.set_samples(len(self.recorded_samples))
        self.status_bar.set_recording(self.recording)

        # Update Recording Toolbar Status Label
        if self.recording:
            self.record_status.setText(f"🔴 Recording | Samples: {len(self.recorded_samples)}")
            self.record_status.setStyleSheet("color: #e74c3c; font-weight: bold;")
        else:
            self.record_status.setText(f"⚪ Not Recording | Samples: {len(self.recorded_samples)}")
            self.record_status.setStyleSheet("color: #7f8c8d;")

        # Synchronize dynamic variables from data_store
        latest_vars = dict(data_store.variables)
        graph_samples = {}

        for var_name, data in latest_vars.items():
            val = data.get("value", 0)
            status = data.get("status", "OK")

            # Register dynamic variables on discovery
            if var_name not in self.known_variables:
                self.register_variable(var_name, val)

            # Update Table View
            self.var_table.add_variable(var_name, val, status)
            graph_samples[var_name] = val

            # Record sample if recording is active
            if self.recording:
                self.recorded_samples.append({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    "name": var_name,
                    "value": val
                })

        # Send sample bundle to graph visualization
        if graph_samples:
            self.viz_panel.add_sample(graph_samples)

        # Sync events log and handle LIST event copying
        latest_events = list(data_store.events)
        if len(latest_events) > self.known_events_count:
            for evt in latest_events[self.known_events_count:]:
                msg = evt.get("event", "")
                evt_type = evt.get("type", "INFO").upper()

                # Catch LIST confirmation and snapshot all variables into the Event Log
                if "CONFIRM:LIST" in msg or "Variables listed" in msg or evt.get("command") == "LIST":
                    for var_name, var_data in data_store.variables.items():
                        val = var_data.get("value", 0)
                        self.event_log.add_event(f"LIST VAR -> {var_name} = {val}", "INFO")

                    # By this point backend.data_store has already pruned any
                    # variable that wasn't in the device's LIST response
                    # (see data_store.finish_list_snapshot(), called from
                    # parser.py the moment CONFIRM:LIST was parsed). Re-sync
                    # the table now so removed variables actually disappear
                    # from the UI instead of lingering with a stale value.
                    current_vars = dict(data_store.variables)
                    self.var_table.sync_variables(current_vars)
                    self.known_variables = set(current_vars.keys())
                    self.var_table.set_refresh_enabled(True)
                    print(f"[DEBUG:main] CONFIRM:LIST handled - var_table now synced to: "
                          f"{sorted(current_vars.keys())}")
                    self.event_log.add_event(
                        f"[DEBUG] Table re-synced after LIST. Now showing: {sorted(current_vars.keys())}", "DEBUG")

                self.event_log.add_event(msg, evt_type)
            self.known_events_count = len(latest_events)

        # Update UI views
        self.var_table.refresh()

    def on_variable_refresh(self):
        """Handle VariableTable's Refresh click. Immediately prunes any
        local rows already known to be stale, then asks the device for its
        authoritative variable set - data_store.start_list_snapshot() + the
        parser's finish_list_snapshot() (triggered by the CONFIRM:LIST that
        comes back) will drop anything the device didn't report, and
        update_gui() re-syncs the table the moment that confirmation
        arrives (see the CONFIRM:LIST handling above)."""
        current_vars = dict(data_store.variables)
        print(f"[DEBUG:main] on_variable_refresh() - bridge.connected={self.bridge.connected}, "
              f"stm32_alive={self.bridge.stm32_alive}, current known vars: {sorted(current_vars.keys())}")

        self.var_table.sync_variables(current_vars)
        self.known_variables = set(current_vars.keys())

        # FIX: bridge.connected only means the TCP socket to the ESP32 is
        # up - it says nothing about whether the STM32 itself has finished
        # booting and is actually listening on UART yet (e.g. right after
        # a flash). Sending LIST while connected-but-not-alive puts bytes
        # on a UART the MCU isn't reading yet, so they're silently lost
        # and CONFIRM:LIST never comes back - looking exactly like a
        # broken LIST function when it's really a "sent too early" issue.
        if self.bridge.connected and self.bridge.stm32_alive:
            self._list_refresh_token += 1
            token = self._list_refresh_token

            self.var_table.set_refresh_enabled(False)
            data_store.start_list_snapshot()
            self.bridge.send_command("LIST")
            self.event_log.add_event(
                f"[DEBUG] Refresh clicked - LIST sent. Known before: {sorted(current_vars.keys())}", "DEBUG")

            QTimer.singleShot(self.LIST_TIMEOUT_MS, lambda: self._check_list_timeout(token))
        else:
            reason = ("bridge not connected (TCP socket down)" if not self.bridge.connected
                       else "TCP connected but STM32 hasn't reported alive yet (still booting?)")
            print(f"[DEBUG:main] on_variable_refresh() - LIST NOT sent: {reason}")
            self.event_log.add_event(
                f"[DEBUG] Refresh clicked but LIST was NOT sent - {reason}. "
                f"Wait for the status bar to show Connected, then retry.", "WARNING")

    def _check_list_timeout(self, token):
        """Fires LIST_TIMEOUT_MS after a LIST was sent. If nothing has
        completed the snapshot by then (no CONFIRM:LIST ever arrived - e.g.
        device still rebooting after a flash, or link is dead), surface
        that clearly instead of leaving Refresh silently stuck."""
        if token != self._list_refresh_token:
            return  # a newer refresh click already superseded this one

        if data_store.is_list_in_progress():
            print("[DEBUG:main] LIST TIMEOUT - no CONFIRM:LIST received in time")
            self.event_log.add_event(
                f"[DEBUG] LIST TIMEOUT - device did not send CONFIRM:LIST within "
                f"{self.LIST_TIMEOUT_MS / 1000:.0f}s. It may still be rebooting after a flash, "
                f"or the UART/TCP link is not actually up despite bridge.connected=True.", "ERROR")
            data_store.abort_list_snapshot()

        self.var_table.set_refresh_enabled(True)

    def open_flash_dialog(self):
        """Open firmware flashing dialog and handle TCP bridge handoff"""
        # FIX: set this BEFORE disconnecting, so update_gui() (which keeps
        # firing on its QTimer even during the modal dialog.exec() below)
        # never gets a chance to see "disconnected" and kick off an
        # auto-reconnect that fights the flasher for the ESP32's one and
        # only TCP client slot.
        self.flashing_in_progress = True

        # A running PID-tuner test would keep the robot driving (and keep
        # path_active set on the MCU) across the flash, so park it before the
        # ESP32's single TCP client slot is handed to the flasher.
        if hasattr(self, 'pid_tuner'):
            self.pid_tuner.stop_all()

        if hasattr(self, 'aruco_panel'):
            self.aruco_panel.stop_all()

        # Explicitly park a running path too; don't rely on the 1.5 s
        # PATH:PING link-loss timeout while the ESP32 is handed to the flasher.
        if hasattr(self, 'path_panel'):
            self.path_panel.stop_all()

        if hasattr(self, 'bridge') and self.bridge:
            self.bridge.disconnect()

        # Entering flash mode: wipe the variable table and backend store
        # now, rather than waiting for a LIST round-trip after reconnect.
        # The firmware about to be written may have a completely different
        # variable set, and the old one is about to stop being valid the
        # moment the chip resets into the bootloader anyway.
        data_store.variables.clear()
        data_store.abort_list_snapshot()
        self.var_table.clear_all()
        self.known_variables = set()
        self.event_log.add_event("Entered flash mode - variable table cleared", "INFO")

        dialog = FlashDialog(self, self.esp32_ip)
        res = dialog.exec()

        self.flashing_in_progress = False

        if res:
            self.event_log.add_event("STM32F401 firmware flashed successfully", "SUCCESS")

        if hasattr(self, 'bridge') and self.bridge:
            self.bridge.connect()

    def toggle_recording(self):
        """Start/stop sample recording session"""
        self.recording = not self.recording
        if self.recording:
            self.record_btn.setText("⏹ Stop")
            self.record_btn.setStyleSheet("background-color: #27ae60; color: white;")
            self.recorded_samples.clear()
            if hasattr(data_store, "start_recording"):
                data_store.start_recording(self.esp32_ip)
            self.event_log.add_event("Recording session started", "SUCCESS")
        else:
            self.record_btn.setText("⏺ Start")
            self.record_btn.setStyleSheet("background-color: #c0392b; color: white;")
            if hasattr(data_store, "stop_recording"):
                data_store.stop_recording()
            self.event_log.add_event("Recording session stopped", "WARNING")

    def export_csv(self):
        """Export recorded data to CSV with columns: Timestamp, VariableName, Value"""
        if not self.recorded_samples:
            QMessageBox.warning(self, "No Data", "No samples recorded to export.")
            return

        default_filename = f"stm32_session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

        filepath, _ = QFileDialog.getSaveFileName(
            self,
            "Export CSV",
            default_filename,
            "CSV Files (*.csv);;All Files (*)"
        )

        if filepath:
            try:
                with open(filepath, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(["Timestamp", "VariableName", "Value"])
                    for sample in self.recorded_samples:
                        writer.writerow([
                            sample["timestamp"],
                            sample["name"],
                            sample["value"]
                        ])
                QMessageBox.information(self, "Success", f"Exported {len(self.recorded_samples)} samples!")
                self.event_log.add_event(f"Exported {len(self.recorded_samples)} samples to CSV", "SUCCESS")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export CSV: {str(e)}")

    def show_graphs(self):
        """Switch view directly to Graphs tab"""
        self.switch_tab(2)


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    app = QApplication(sys.argv)
    window = STM32DebuggerApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()