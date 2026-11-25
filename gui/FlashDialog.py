# ============================================================================
# Flash Dialog - PySide6 Firmware Flashing Interface
# ============================================================================

from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar, QFileDialog, QMessageBox, QCheckBox
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QFont

try:
    from backend.stm32_flasher import STM32Flasher, FlashError
except ImportError:
    from stm32_flasher import STM32Flasher, FlashError

try:
    from backend.config import ESP32_IP as DEFAULT_ESP32_IP, ESP32_PORT as DEFAULT_ESP32_PORT
except ImportError:
    DEFAULT_ESP32_IP, DEFAULT_ESP32_PORT = "192.168.0.36", 5000


class FlashWorker(QThread):
    """Background flashing worker thread using AN3155 protocol"""
    progress = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, bin_file, esp32_ip=DEFAULT_ESP32_IP, port=DEFAULT_ESP32_PORT, auto_bootloader=True):
        super().__init__()
        self.bin_file = bin_file
        self.esp32_ip = esp32_ip
        self.port = port
        self.auto_bootloader = auto_bootloader

    def run(self):
        try:
            self.progress.emit("Connecting to ESP32 on port 5000...")
            flasher = STM32Flasher(self.esp32_ip, self.port)
            flasher.connect()

            def progress_cb(pct):
                self.progress.emit(f"Flashing: {int(pct)}%")

            if self.auto_bootloader:
                self.progress.emit("Requesting auto bootloader entry...")
            self.progress.emit("Entering bootloader sync...")
            flasher.flash_firmware(self.bin_file, progress_callback=progress_cb,
                                    auto_bootloader=self.auto_bootloader)
            flasher.disconnect()

            self.finished.emit(True, "Flash completed successfully!")
        except FlashError as fe:
            self.finished.emit(False, str(fe))
        except Exception as e:
            self.finished.emit(False, str(e))


class FlashDialog(QDialog):
    """Firmware flashing dialog"""

    def __init__(self, parent=None, esp32_ip=DEFAULT_ESP32_IP):
        super().__init__(parent)
        self.setWindowTitle("Flash STM32F401")
        self.setGeometry(100, 100, 500, 320)
        self.esp32_ip = esp32_ip
        self.worker = None

        self.setup_ui()

    def setup_ui(self):
        """Build the dialog UI"""
        layout = QVBoxLayout(self)

        title = QLabel("STM32F401 Wireless Flashing")
        title.setFont(QFont("Arial", 12, QFont.Bold))
        title.setStyleSheet("color: #ffffff;")
        layout.addWidget(title)

        instructions = QLabel(
            "Auto bootloader entry is on by default - no button presses needed.\n"
            "If it's unchecked below (or the current firmware doesn't support\n"
            "it yet), do this first instead:\n"
            "1. Hold BOOT0 button on Black Pill\n"
            "2. Press RESET button\n"
            "3. Release BOOT0 button"
        )
        instructions.setStyleSheet("background-color: #252526; color: #ffffff; padding: 12px; border-radius: 4px; border: 1px solid #3c3c3c;")
        layout.addWidget(instructions)

        self.auto_bootloader_checkbox = QCheckBox("Auto-enter bootloader (no BOOT0 needed)")
        self.auto_bootloader_checkbox.setChecked(True)
        self.auto_bootloader_checkbox.setStyleSheet("color: #ffffff;")
        layout.addWidget(self.auto_bootloader_checkbox)

        file_layout = QHBoxLayout()
        self.file_label = QLabel("No file selected")
        self.file_label.setStyleSheet("color: #ffffff;")
        file_layout.addWidget(self.file_label)

        browse_btn = QPushButton("Browse...")
        browse_btn.setStyleSheet("background-color: #007acc; color: white;")
        browse_btn.clicked.connect(self.select_file)
        file_layout.addWidget(browse_btn)

        layout.addLayout(file_layout)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #cccccc;")
        layout.addWidget(self.status_label)

        btn_layout = QHBoxLayout()

        self.flash_btn = QPushButton("🔧 Flash")
        self.flash_btn.setStyleSheet("background-color: #e74c3c; color: white;")
        self.flash_btn.clicked.connect(self.start_flash)
        self.flash_btn.setEnabled(False)
        btn_layout.addWidget(self.flash_btn)

        close_btn = QPushButton("Close")
        close_btn.setStyleSheet("background-color: #555555; color: white;")
        close_btn.clicked.connect(self.reject)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

    def select_file(self):
        """Select firmware file"""
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Select Firmware", "", "Binary Files (*.bin)"
        )
        if filepath:
            self.selected_file = filepath
            self.file_label.setText(filepath.split('/')[-1])
            self.flash_btn.setEnabled(True)

    def start_flash(self):
        """Start flashing process"""
        if not hasattr(self, 'selected_file'):
            QMessageBox.warning(self, "No File", "Please select a .bin file")
            return

        auto_bootloader = self.auto_bootloader_checkbox.isChecked()
        confirm_note = ("The STM32F401 will enter bootloader mode automatically."
                         if auto_bootloader else
                         "Ensure STM32F401 is in bootloader mode (BOOT0+RESET).")
        reply = QMessageBox.question(
            self, "Confirm",
            f"Flash {self.selected_file.split('/')[-1]}?\n\n{confirm_note}"
        )

        if reply == QMessageBox.Yes:
            self.flash_btn.setEnabled(False)
            self.progress.setVisible(True)
            self.progress.setValue(0)

            self.worker = FlashWorker(self.selected_file, self.esp32_ip, port=5000,
                                       auto_bootloader=auto_bootloader)
            self.worker.progress.connect(self.on_progress)
            self.worker.finished.connect(self.on_finished)
            self.worker.start()

    def on_progress(self, msg):
        """Update progress"""
        self.status_label.setText(msg)

        if "%" in msg:
            try:
                percent = int(msg.split("%")[0].split()[-1])
                self.progress.setValue(percent)
            except:
                pass

    def on_finished(self, success, message):
        """Flash finished"""
        self.flash_btn.setEnabled(True)

        if success:
            QMessageBox.information(self, "Success", f"✅ {message}\n\nSTM32F401 restarting...")
            self.accept()
        else:
            QMessageBox.critical(self, "Failed", f"❌ Flashing failed!\n\nReason: {message}")