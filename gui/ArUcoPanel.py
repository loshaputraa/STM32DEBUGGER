# ============================================================================
# ArUco Align Panel - camera preview, pose readout, arm/disarm the STM32
# ============================================================================
# Runs marker detection on the PC (camera is mounted on the robot, USB-cabled
# to the PC) and streams the robot-centre pose to the STM32 through the
# existing ESP32 bridge:
#
#     ARUCO:POSE=x_mm,y_mm,th_mdeg,brg_mdeg      (~10 Hz, silent command)
#
# The STM32 then runs its unchanged pathfinder against target (0,0) heading 0.
#
# Arming sequence (deliberately explicit):
#     1. camera opens and the first frame decodes
#     2. SET:motors_en=0, SET:axis_mask=<selected>, SET:aruco_en=1
#     3. pose streaming starts; the robot stays still until "Motors ON"
# Stopping sends SET:motors_en=0 then SET:aruco_en=0, which makes the
# firmware forget the pose and park (ArUco_Reset()).
# ============================================================================

import os
import time

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QImage, QPixmap, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton, QLabel,
    QGroupBox, QComboBox, QSpinBox, QLineEdit, QMessageBox, QFileDialog,
    QSizePolicy
)

from backend.aruco import ArucoDetector, wire_command, MARKER_ID

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

# ============================================================================
# Configuration
# ============================================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CALIB = os.path.join(PROJECT_ROOT, "calib_data.npz")
DEFAULT_CAMERA = 0
CAMERA_CHOICES = [
    ("Laptop webcam (index 0)", 0),
    ("USB camera (index 1)", 1),
]

# FIX: Lowered to 10.0 Hz.
# The STM32 UART only holds ONE command at a time. If TCP buffers poses,
# the STM32 drops them and the watchdog trips. 10Hz is safe for the current ESP32 config.
STREAM_HZ = 10.0
PREVIEW_EVERY = 2  # draw ~5 preview frames/s

AXIS_CHOICES = [
    ("All axes (forward + strafe + rotate)", 7),
    ("Rotate only", 4),
    ("Strafe only", 2),
    ("Forward only", 1),
]

STATE_NAMES = {
    0: "off", 1: "waiting for pose", 2: "tracking",
    3: "marker lost (stopped)", 4: "searching", 5: "gave up",
}


# ============================================================================
# Worker
# ============================================================================

class ArucoWorker(QThread):
    """Detection loop. Opens the camera in-thread so the GUI never blocks."""

    pose = Signal(object)
    frame = Signal(object)  # QImage
    ready = Signal()
    failed = Signal(str)
    status = Signal(str)
    pose_failed = Signal(str)

    def __init__(self, detector, bridge, stream_hz=STREAM_HZ, parent=None):
        super().__init__(parent)
        self.detector = detector
        self.bridge = bridge
        self.period = 1.0 / max(1.0, float(stream_hz))
        self._abort = False
        self._sending = False
        self._preview_count = 0

    # ---- control from the GUI thread ----
    def request_abort(self):
        self._abort = True

    def set_sending(self, enabled):
        self._sending = bool(enabled)

    # ---- helpers ----
    def _to_qimage(self, frame_bgr):
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        return QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888).copy()

    def _send_pose(self, pose):
        if not self._sending:
            return
        if not (self.bridge and self.bridge.connected):
            return
        try:
            if hasattr(self.bridge, "send_raw"):
                self.bridge.send_raw(wire_command(pose))
            else:
                self.bridge.send_command(wire_command(pose))
        except Exception:  # noqa: BLE001
            pass

    # ---- thread body ----
    def run(self):
        try:
            self.detector.open()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return

        self.ready.emit()
        self.status.emit("Camera ready")

        try:
            while not self._abort:
                pose, frame, reason = self.detector.read()

                # 1. SEND POSE FIRST (Priority)
                if pose is not None:
                    self._send_pose(pose)
                    self.pose.emit(pose)
                    self.status.emit("Streaming pose")
                else:
                    self.status.emit(f"No marker - {reason}")
                    if reason and not reason.startswith("No markers"):
                        self.pose_failed.emit(reason)

                # 2. UPDATE GUI PREVIEW (Secondary)
                if frame is not None:
                    self._preview_count += 1
                    if self._preview_count % PREVIEW_EVERY == 0:
                        self.frame.emit(self._to_qimage(frame))

                # 3. STRICT DELAY
                # Always sleep a fixed amount at the end of the loop.
                # If the camera took 100ms to read, this doesn't skip the sleep.
                time.sleep(self.period)

        finally:
            self.detector.close()
            self.status.emit("Camera closed")


# ============================================================================
# Panel
# ============================================================================

class ArUcoPanel(QWidget):
    """ArUco alignment tab."""

    def __init__(self, bridge=None, event_log=None, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.event_log = event_log
        self.worker = None
        self._link_ok = False
        self._running = False
        self._last_pose = None
        self._last_pose_time = 0.0
        self._frames = 0
        self._fps_time = time.time()

        self._last_fail_reason = ""
        self._last_fail_time = 0.0

        self.setup_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(200)

    # ---- plumbing ----
    def set_bridge(self, bridge):
        self.bridge = bridge

    def _log(self, message, level="INFO"):
        if self.event_log is not None:
            try:
                self.event_log.add_event(message, level)
            except Exception:  # noqa: BLE001
                pass

    def _send(self, command):
        try:
            if self.bridge and self.bridge.connected:
                self.bridge.send_command(command + "\r")
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _var_int(self, name, default=0):
        try:
            from backend.data_store import variables
            entry = variables.get(name)
            if entry:
                return int(float(entry["value"]))
        except Exception:  # noqa: BLE001
            pass
        return default

    def _path_active(self):
        return self._var_int("path_active", 0)

    # ========================================================================
    # UI
    # ========================================================================
    def setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        row = QHBoxLayout()
        outer.addLayout(row, 1)

        # ---- camera preview ----
        preview_box = QGroupBox("Camera")
        preview_layout = QVBoxLayout(preview_box)
        self.preview = QLabel("Camera not started")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(480, 360)
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview.setStyleSheet(
            "background-color: #101010; color: #808080; border: 1px solid #555555;")
        preview_layout.addWidget(self.preview)
        row.addWidget(preview_box, 3)

        # ---- right column ----
        side = QVBoxLayout()
        row.addLayout(side, 2)

        pose_box = QGroupBox("Pose (robot centre vs goal)")
        pose_grid = QGridLayout(pose_box)
        posture = QFont("Consolas")
        posture.setStyleHint(QFont.Monospace)
        self.lbl_x = QLabel("--")
        self.lbl_y = QLabel("--")
        self.lbl_th = QLabel("--")
        self.lbl_brg = QLabel("--")
        self.lbl_age = QLabel("--")
        for i, (name, widget) in enumerate((
                ("x", self.lbl_x), ("y", self.lbl_y),
                ("theta", self.lbl_th), ("bearing", self.lbl_brg),
                ("frame age", self.lbl_age))):
            key = QLabel(name + ":")
            widget.setFont(posture)
            pose_grid.addWidget(key, i, 0)
            pose_grid.addWidget(widget, i, 1)
        side.addWidget(pose_box)

        self.state_label = QLabel("off")
        self.state_label.setStyleSheet("color: #a0a0a0;")
        side.addWidget(self.state_label)

        ctl_box = QGroupBox("Control")
        ctl = QVBoxLayout(ctl_box)

        self.start_btn = QPushButton("Start camera (dry run)")
        self.start_btn.setStyleSheet("background-color: #007acc; color: white;")
        self.start_btn.clicked.connect(self._on_start)
        ctl.addWidget(self.start_btn)

        self.motors_btn = QPushButton("Motors ON")
        self.motors_btn.setStyleSheet("background-color: #27ae60; color: white;")
        self.motors_btn.clicked.connect(self._on_motors_on)
        ctl.addWidget(self.motors_btn)

        self.stop_btn = QPushButton("\u25a0 STOP and disarm")
        self.stop_btn.setStyleSheet(
            "background-color: #c0392b; color: white; font-weight: bold;")
        self.stop_btn.clicked.connect(self._on_stop)
        ctl.addWidget(self.stop_btn)

        ctl.addWidget(QLabel("Axes for bench tests:"))
        self.axis_combo = QComboBox()
        for label, mask in AXIS_CHOICES:
            self.axis_combo.addItem(label, mask)
        ctl.addWidget(self.axis_combo)

        cam_row = QHBoxLayout()
        cam_row.addWidget(QLabel("Camera:"))
        self.camera_combo = QComboBox()
        for label, index in CAMERA_CHOICES:
            self.camera_combo.addItem(label, index)
        self.camera_combo.setCurrentIndex(
            max(self.camera_combo.findData(DEFAULT_CAMERA), 0))
        cam_row.addWidget(self.camera_combo, 1)
        ctl.addLayout(cam_row)

        ctl.addWidget(QLabel("Calibration (.npz):"))
        calib_row = QHBoxLayout()
        self.calib_edit = QLineEdit(DEFAULT_CALIB)
        calib_row.addWidget(self.calib_edit, 1)
        browse_btn = QPushButton("...")
        browse_btn.setMaximumWidth(40)
        browse_btn.clicked.connect(self._on_browse_calib)
        calib_row.addWidget(browse_btn)
        ctl.addLayout(calib_row)

        side.addWidget(ctl_box)
        side.addStretch()

        self.status_label = QLabel("Idle - start the camera to preview")
        self.status_label.setStyleSheet("color: #a0a0a0; padding: 2px;")
        outer.addWidget(self.status_label)

        self._update_buttons()

    # ========================================================================
    # Handlers
    # ========================================================================
    def _on_browse_calib(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Camera calibration", self.calib_edit.text(),
            "NumPy archive (*.npz);;All files (*)")
        if path:
            self.calib_edit.setText(path)

    def _on_start(self):
        if self._running:
            return
        if not self._link_ok:
            QMessageBox.warning(
                self, "Not connected",
                "Connect to the ESP32 and wait for STM32 alive first.")
            return

        calib = self.calib_edit.text().strip()
        if not os.path.exists(calib):
            QMessageBox.warning(
                self, "Calibration missing",
                f"No calibration file at:\n{calib}\n\n"
                "Run calibration.py with THIS camera at the runtime resolution.")
            return

        detector = ArucoDetector(calib, marker_id=MARKER_ID,
                                 camera_index=self.camera_combo.currentData())
        self.worker = ArucoWorker(detector, self.bridge, STREAM_HZ, parent=self)
        self.worker.frame.connect(self._on_frame)
        self.worker.pose.connect(self._on_pose)
        self.worker.ready.connect(self._on_worker_ready)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.status.connect(self._on_worker_status)
        self.worker.pose_failed.connect(self._on_pose_failed)
        self.worker.start()

        self._running = True
        self._update_buttons()
        self.status_label.setText("Opening camera...")
        self._log("ArUco: camera starting", "INFO")

    def _on_worker_ready(self):
        self._send("SET:aruco_en=1")
        self.status_label.setText("Armed. Press 'Motors ON' to drive.")
        self._log("ArUco: armed", "INFO")

    def _on_motors_on(self):
        if not self._running or self.worker is None:
            return
        reply = QMessageBox.question(
            self, "Enable motors",
            "The robot will start driving toward the marker.\n"
            "Make sure the area is clear and the marker is visible.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        mask = self.axis_combo.currentData() or 7

        if self._path_active() != 1:
            self._send(f"SET:axis_mask={mask}")
            time.sleep(0.05)

        self._send("SET:aruco_en=1")
        time.sleep(0.05)
        self._send("SET:motors_en=1")

        if self.worker:
            self.worker.set_sending(True)

        self.status_label.setText("Motors ON - aligning")
        self._log("ArUco: motors enabled", "SUCCESS")

    def _on_stop(self):
        self._disarm()
        self.status_label.setText("Stopped and disarmed")

    def stop_all(self):
        self._disarm()

    def _disarm(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.set_sending(False)
            self.worker.request_abort()
            self.worker.wait(2000)
        self.worker = None
        self._running = False

        if self._path_active() != 1:
            self._send("SET:motors_en=0")
        self._send("SET:aruco_en=0")

        self._last_pose = None
        self._update_buttons()
        self._log("ArUco: disarmed", "WARNING")

    def _on_worker_failed(self, message):
        self.worker = None
        self._running = False
        self._update_buttons()
        self.status_label.setText(f"Camera failed: {message}")
        self._log(f"ArUco: camera failed - {message}", "ERROR")
        QMessageBox.critical(self, "Camera error", message)

    def _on_worker_status(self, message):
        self.status_label.setText(message)

    def _on_frame(self, image):
        pixmap = QPixmap.fromImage(image)
        self.preview.setPixmap(pixmap.scaled(
            self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self._frames += 1

    def _on_pose(self, pose):
        self._last_pose = pose
        self._last_pose_time = time.time()
        self.lbl_x.setText(f"{pose['x_mm']:6d} mm")
        self.lbl_y.setText(f"{pose['y_mm']:6d} mm")
        self.lbl_th.setText(f"{pose['th_mdeg'] / 1000.0:7.2f} deg")
        self.lbl_brg.setText(f"{pose['brg_mdeg'] / 1000.0:7.2f} deg")

    def _on_pose_failed(self, reason):
        now = time.time()
        if reason != self._last_fail_reason or now - self._last_fail_time > 2.0:
            self._log(f"ArUco Reject: {reason}", "WARNING")
            self._last_fail_reason = reason
            self._last_fail_time = now

    # ========================================================================
    # Periodic UI refresh
    # ========================================================================
    def _tick(self):
        if self._last_pose is not None:
            age = (time.time() - self._last_pose_time) * 1000.0
            self.lbl_age.setText(f"{age:6.0f} ms")
        else:
            self.lbl_age.setText("--")

        self._refresh_state()

    def _refresh_state(self):
        try:
            from backend.data_store import variables
        except ImportError:
            return
        state = variables.get("aruco_state")
        age = variables.get("aruco_age_ms")
        if not state:
            return
        try:
            code = int(float(state["value"]))
            name = STATE_NAMES.get(code, f"state {code}")
            if age:
                name += f"   (frame age {int(float(age['value']))} ms)"

            mode = variables.get("ctrl_mode")
            owner = ""
            if mode:
                owner = {0: "PARKED", 1: "HOLD",
                         2: "PATH", 3: "ARUCO"}.get(
                    int(float(mode["value"])), "")
            self.state_label.setText(
                f"STM32: {name}" + (f"  | owner {owner}" if owner else ""))
        except (KeyError, TypeError, ValueError):
            pass

    # ========================================================================
    # Enablement
    # ========================================================================
    def _update_buttons(self):
        self.start_btn.setEnabled(self._link_ok and not self._running)
        self.motors_btn.setEnabled(self._link_ok and self._running)
        self.stop_btn.setEnabled(True)
        self.axis_combo.setEnabled(self._link_ok)
        self.camera_combo.setEnabled(not self._running)
        self.calib_edit.setEnabled(not self._running)

    def set_link_state(self, connected, stm32_alive):
        ok = bool(connected and stm32_alive)
        if ok != self._link_ok:
            self._link_ok = ok
            self._update_buttons()
            if not ok and self._running:
                self.status_label.setText("Link lost - disarming")
                self._disarm()