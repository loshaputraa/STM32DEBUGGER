# ============================================================================
# Status Bar - PySide6 Status Bar Component
# ============================================================================

from PySide6.QtWidgets import QStatusBar, QLabel


class StatusBar(QStatusBar):
    """Custom status bar with multiple widgets"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        """Build status bar"""

        # Separator
        self.addPermanentWidget(QLabel(" | "))

        # Connection status
        self.connection_label = QLabel("● Disconnected")
        self.connection_label.setStyleSheet("color: red;")
        self.addPermanentWidget(self.connection_label)

        # Separator
        self.addPermanentWidget(QLabel(" | "))

        # Latency
        self.latency_label = QLabel("Latency: -- ms")
        self.latency_label.setStyleSheet("color: orange;")
        self.addPermanentWidget(self.latency_label)

        # Separator
        self.addPermanentWidget(QLabel(" | "))

        # Sample count
        self.sample_label = QLabel("Samples: 0")
        self.addPermanentWidget(self.sample_label)

        # Separator
        self.addPermanentWidget(QLabel(" | "))

        # Recording status
        self.recording_label = QLabel("⚪ Not Recording")
        self.recording_label.setStyleSheet("color: gray;")
        self.addPermanentWidget(self.recording_label)

    def set_connection(self, connected, stm32_alive=False):
        """Update connection status"""
        if connected and stm32_alive:
            self.connection_label.setText("● Connected")
            self.connection_label.setStyleSheet("color: green;")
        elif connected:
            self.connection_label.setText("● ESP32 Only")
            self.connection_label.setStyleSheet("color: orange;")
        else:
            self.connection_label.setText("● Disconnected")
            self.connection_label.setStyleSheet("color: red;")

    def set_latency(self, latency_ms):
        if latency_ms is None:
            latency_ms = 0.0
        self.latency_label.setText(f"Latency: {latency_ms:.1f} ms")
    def set_samples(self, count):
        """Update sample count"""
        self.sample_label.setText(f"Samples: {count}")

    def set_recording(self, recording):
        """Update recording status"""
        if recording:
            self.recording_label.setText("🔴 Recording")
            self.recording_label.setStyleSheet("color: red;")
        else:
            self.recording_label.setText("⚪ Not Recording")
            self.recording_label.setStyleSheet("color: gray;")