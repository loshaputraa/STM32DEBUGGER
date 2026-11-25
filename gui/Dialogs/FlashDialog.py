import threading
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QFileDialog, QMessageBox, QPlainTextEdit
)
from PySide6.QtCore import Qt, Signal, QThread, QObject

from backend.stm32_flasher import STM32Flasher, FlashError
from backend.tcp_client import DebugBridge, ESP32_IP, ESP32_PORT


class FlashWorker(QObject):
    sig_progress = Signal(float)        # 0 – 100 %
    sig_log = Signal(str)               # status messages
    sig_finished = Signal(bool, str)    # success, message

    def __init__(self, bin_path: str, esp32_ip: str, esp32_port: int):
        super().__init__()
        self.bin_path = bin_path
        self.esp32_ip = esp32_ip
        self.esp32_port = esp32_port

    def run(self):
        flasher = STM32Flasher(self.esp32_ip, self.esp32_port)
        try:
            self.sig_log.emit("Connecting to ESP32...")
            flasher.connect()

            self.sig_log.emit("Entering flash mode (8E1 parity)...")
            self.sig_log.emit("Erasing flash memory...")
            self.sig_log.emit("Flashing firmware...")

            flasher.flash_firmware(
                self.bin_path,
                progress_callback=lambda pct: self.sig_progress.emit(pct)
            )
            self.sig_log.emit("Jumping to application...")
            self.sig_finished.emit(True, "Firmware flashed successfully!")
        except FlashError as e:
            self.sig_finished.emit(False, str(e))
        except Exception as e:
            self.sig_finished.emit(False, f"Unexpected error: {e}")
        finally:
            flasher.disconnect()


class FlashDialog(QDialog):
    """
    QDialog for wireless STM32 firmware flashing.
    Disconnects the normal DebugBridge first, runs the flasher in a
    background QThread (never blocks the GUI), then reconnects.
    """

    def __init__(self, bridge: DebugBridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.setWindowTitle("Flash STM32 Firmware")
        self.setMinimumWidth(500)
        self.setModal(True)

        layout = QVBoxLayout(self)

        # File selector
        file_row = QHBoxLayout()
        self.file_label = QLabel("No file selected")
        self.file_label.setStyleSheet("color: #717171;")
        file_row.addWidget(self.file_label, 1)
        browse_btn = QPushButton("Browse .bin...")
        browse_btn.clicked.connect(self._browse_file)
        file_row.addWidget(browse_btn)
        layout.addLayout(file_row)

        # Instructions
        instructions = QLabel(
            "⚠  Before flashing:\n"
            "1. Hold BOOT0 button   2. Tap RESET   3. Release BOOT0\n"
            "The chip is now in bootloader mode and waiting."
        )
        instructions.setStyleSheet("color: #d7ba7d; padding: 8px;")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        # Progress bar
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        # Log output
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        self.log.setPlaceholderText("Flash log will appear here...")
        layout.addWidget(self.log)

        # Buttons
        btn_row = QHBoxLayout()
        self.flash_btn = QPushButton("⚡ Flash Firmware")
        self.flash_btn.clicked.connect(self._start_flash)
        self.flash_btn.setEnabled(False)
        btn_row.addWidget(self.flash_btn)
        cancel_btn = QPushButton("Close")
        cancel_btn.setProperty("class", "secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self.bin_path: str = ""
        self._thread: QThread | None = None
        self._worker: FlashWorker | None = None

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Firmware Binary", "", "Binary Files (*.bin);;All Files (*)"
        )
        if path:
            self.bin_path = path
            self.file_label.setText(path)
            self.file_label.setStyleSheet("color: #d4d4d4;")
            self.flash_btn.setEnabled(True)

    def _start_flash(self):
        if not self.bin_path:
            return

        self.flash_btn.setEnabled(False)
        self.progress.setValue(0)
        self.log.clear()
        self.log.appendPlainText("Disconnecting DebugBridge...")

        # Disconnect the normal debug connection
        self.bridge.disconnect()

        esp32_ip = ESP32_IP
        esp32_port = ESP32_PORT

        self._worker = FlashWorker(self.bin_path, esp32_ip, esp32_port)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.sig_progress.connect(lambda v: self.progress.setValue(int(v)))
        self._worker.sig_log.connect(self.log.appendPlainText)
        self._worker.sig_finished.connect(self._on_finished)

        self._thread.start()

    def _on_finished(self, success: bool, message: str):
        self._thread.quit()
        self._thread.wait()

        if success:
            self.progress.setValue(100)
            self.log.appendPlainText("Reconnecting DebugBridge...")
            self.bridge.connect()
            QMessageBox.information(self, "Flash Success", message)
        else:
            self.log.appendPlainText(f"ERROR: {message}")
            self.bridge.connect()
            QMessageBox.critical(self, "Flash Failed", message)

        self.flash_btn.setEnabled(True)
