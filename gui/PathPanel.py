# ============================================================================
# Path Panel - PySide6 Path Drawing & Upload
# ============================================================================
# Lets the user draw a path on a field-sized canvas, simplifies it with
# Ramer-Douglas-Peucker, uploads one waypoint at a time (PATH:ADD=i,x_mm,y_mm)
# pacing 25 ms per point (matches uart_handler's single-command queue),
# verifies path_count, then issues PATH:GO.
#
# FRAME (read this before tweaking the flip checkboxes):
#   robot_params.h sets PATHFINDER_X_SIGN = PATHFINDER_Y_SIGN = -1, so the
#   firmware compares each target against rx = PATHFINDER_X_SIGN * robot.x_m
#   (the "corrected" frame). The pose stream (`x_mm`, `y_mm`) sends the RAW
#   ODOMETRY position (robot_dbg.x_m * 1000) in every control mode, including
#   while ArUco owns the robot. That keeps the canvas, the first-waypoint
#   anchor and PATH:GO in the same frame even if a new path is started right
#   after an ArUco alignment.
#
#   flip_x = flip_y = True (default): canvas displays -raw, i.e. the same
#     corrected frame the firmware works in. Upload = drawn_mm × 10 as-is.
#   flip_x / flip_y = False: canvas displays +raw. Upload = -drawn_mm × 10,
#     so the firmware still receives the corrected-frame value.
#
#   See `_on_send_go()` for the two-line derivation. If robot_params.h ever
#   changes PATHFINDER_X_SIGN back to +1, change PATHFINDER_X_SIGN here and
#   flip the checkboxes to match — nothing else needs editing.
#
# ANCHORING: on Send & Go, upload_mm[0] is replaced with the robot's current
# firmware-frame position. This makes the first waypoint (which uses the
# precise MOVE→LATCH→ROTATE stop) land at the robot's current spot, then
# cruise through the rest of the drawn path.
#
# LINK-LOSS: while running, PATH:PING goes out every 500 ms. Firmware stops
# itself if none arrives within 1.5 s (see app.c PATH_LINK_TIMEOUT_MS).
# ============================================================================

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QSpinBox, QDoubleSpinBox, QCheckBox, QMessageBox
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QPointF
from PySide6.QtGui import QPainter, QPen, QColor, QBrush

import math
import time
from collections import deque


# ============================================================================
# Configuration
# ============================================================================
DEFAULT_TOLERANCE_CM     = 2.0    # default RDP epsilon
MAX_UPLOAD_POINTS        = 48     # soft limit; tolerance auto-raises above this
HARD_MAX_POINTS          = 100    # firmware PATHFINDER_RT_MAX_WP
MAX_AUTO_TOLERANCE_CM    = 20.0   # stop raising tolerance past this
UPLOAD_PACE_S            = 0.025  # 25 ms per PATH:ADD
POSE_UPDATE_MS           = 100    # live canvas refresh
PING_INTERVAL_MS         = 500    # PATH:PING cadence while running

# Must match robot_params.h
PATHFINDER_X_SIGN = -1
PATHFINDER_Y_SIGN = -1


# ============================================================================
# Helpers
# ============================================================================

def _get_variables():
    """Fetch the live data_store.variables dict (defensive - returns {} if
    backend isn't importable, so this widget can be opened in isolation)."""
    try:
        from backend.data_store import variables
        return variables
    except ImportError:
        return {}


def _var_int(name, default=None):
    """Read a variable as an int, tolerating missing/stale/odd-shaped entries."""
    v = _get_variables().get(name)
    if not v:
        return default
    try:
        return int(float(v["value"]))
    except (ValueError, KeyError, TypeError):
        return default


def rdp_simplify(points, epsilon):
    """Ramer-Douglas-Peucker path simplification.

    points:  [(x, y), ...]  (any unit; epsilon must be in the same unit)
    epsilon: max perpendicular distance a point may be from the simplified
             segment and still be dropped.
    Returns a list of (x, y) tuples including the first and last point.
    """
    if len(points) < 3:
        return list(points)

    def _perp_dist(pt, a, b):
        ax, ay = a
        bx, by = b
        px, py = pt
        dx = bx - ax
        dy = by - ay
        denom = math.hypot(dx, dy)
        if denom < 1e-9:
            return math.hypot(px - ax, py - ay)
        return abs(dy * px - dx * py + bx * ay - by * ax) / denom

    dmax = 0.0
    index = 0
    for i in range(1, len(points) - 1):
        d = _perp_dist(points[i], points[0], points[-1])
        if d > dmax:
            index = i
            dmax = d

    if dmax > epsilon:
        left = rdp_simplify(points[:index + 1], epsilon)
        right = rdp_simplify(points[index:], epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]


# ============================================================================
# Path Canvas
# ============================================================================

class PathCanvas(QWidget):
    """Freehand drawing canvas with grid, live robot marker, and trail.

    Left-click / drag to draw. Right-click to undo the last point.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.field_w_cm = 100.0
        self.field_h_cm = 100.0
        self.drawn_points = []       # [(x_cm, y_cm), ...] canvas (display) frame
        self.drawing = False
        self.robot_pos_mm = (0, 0)   # raw
        self.robot_theta_mdeg = 0    # raw
        self.trail = deque(maxlen=500)  # raw (x_mm, y_mm)
        self.flip_x = True
        self.flip_y = True
        self.setMinimumSize(400, 400)

    # ---- Configuration ----
    def set_field(self, w_cm, h_cm):
        self.field_w_cm = max(20.0, float(w_cm))
        self.field_h_cm = max(20.0, float(h_cm))
        self.update()

    def set_flip(self, fx, fy):
        self.flip_x = bool(fx)
        self.flip_y = bool(fy)
        self.update()

    def set_robot(self, x_mm, y_mm, theta_mdeg):
        new_pos = (int(x_mm), int(y_mm))
        if new_pos != self.robot_pos_mm:
            self.trail.append(new_pos)
            self.robot_pos_mm = new_pos
        self.robot_theta_mdeg = int(theta_mdeg)
        self.update()

    def clear_drawn(self):
        self.drawn_points = []
        self.update()

    def clear_trail(self):
        self.trail.clear()
        self.update()

    # ---- Coordinate transforms ----
    def _px_per_cm(self):
        return max(min(self.width() / self.field_w_cm,
                       self.height() / self.field_h_cm), 0.1)

    def _canvas_cm_to_pixel(self, fx_cm, fy_cm):
        s = self._px_per_cm()
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        return (cx + fx_cm * s, cy - fy_cm * s)  # screen Y inverted

    def _pixel_to_canvas_cm(self, px, py):
        s = self._px_per_cm()
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        return ((px - cx) / s, (cy - py) / s)

    def _raw_to_canvas_cm(self, x_mm, y_mm):
        fx = -x_mm if self.flip_x else x_mm
        fy = -y_mm if self.flip_y else y_mm
        return (fx / 10.0, fy / 10.0)

    # ---- Painting ----
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#1e1e1e"))

        cw, ch = self.width(), self.height()
        s = self._px_per_cm()
        cx, cy = cw / 2.0, ch / 2.0

        # Grid: minor 10 cm, major 50 cm
        minor_pen = QPen(QColor("#333333"), 1)
        major_pen = QPen(QColor("#555555"), 1)
        step = 10.0
        x = -self.field_w_cm / 2.0
        while x <= self.field_w_cm / 2.0 + 0.01:
            p.setPen(major_pen if abs(x % 50.0) < 0.01 else minor_pen)
            px = cx + x * s
            p.drawLine(int(px), 0, int(px), ch)
            x += step
        y = -self.field_h_cm / 2.0
        while y <= self.field_h_cm / 2.0 + 0.01:
            p.setPen(major_pen if abs(y % 50.0) < 0.01 else minor_pen)
            py = cy - y * s
            p.drawLine(0, int(py), cw, int(py))
            y += step

        # Origin crosshair
        p.setPen(QPen(QColor("#707070"), 1, Qt.DashLine))
        p.drawLine(int(cx), 0, int(cx), ch)
        p.drawLine(0, int(cy), cw, int(cy))

        # Trail (dim amber)
        if len(self.trail) >= 2:
            p.setPen(QPen(QColor("#a06000"), 2))
            prev = None
            for x_mm, y_mm in self.trail:
                fx_cm, fy_cm = self._raw_to_canvas_cm(x_mm, y_mm)
                pt = self._canvas_cm_to_pixel(fx_cm, fy_cm)
                if prev is not None:
                    p.drawLine(QPointF(prev[0], prev[1]),
                               QPointF(pt[0], pt[1]))
                prev = pt

        # Drawn path (cyan)
        if len(self.drawn_points) >= 2:
            p.setPen(QPen(QColor("#00d4ff"), 2))
            for i in range(len(self.drawn_points) - 1):
                p1 = self._canvas_cm_to_pixel(*self.drawn_points[i])
                p2 = self._canvas_cm_to_pixel(*self.drawn_points[i + 1])
                p.drawLine(QPointF(p1[0], p1[1]), QPointF(p2[0], p2[1]))

        # Drawn points (first = gold, rest = cyan dots)
        for i, (fx_cm, fy_cm) in enumerate(self.drawn_points):
            px, py = self._canvas_cm_to_pixel(fx_cm, fy_cm)
            if i == 0:
                p.setPen(QPen(QColor("#ffd700"), 2))
                p.setBrush(QBrush(QColor("#ffd700")))
                p.drawEllipse(QPointF(px, py), 5, 5)
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(QColor("#00d4ff")))
                p.drawEllipse(QPointF(px, py), 2.5, 2.5)

        # Robot marker (red triangle, points along heading)
        rx_mm, ry_mm = self.robot_pos_mm
        fx_cm, fy_cm = self._raw_to_canvas_cm(rx_mm, ry_mm)
        px, py = self._canvas_cm_to_pixel(fx_cm, fy_cm)

        theta_raw = self.robot_theta_mdeg * math.pi / 180000.0
        dx_can = -math.cos(theta_raw) if self.flip_x else math.cos(theta_raw)
        dy_can = -math.sin(theta_raw) if self.flip_y else math.sin(theta_raw)
        theta_can = math.atan2(dy_can, dx_can)

        size = 12
        tip   = QPointF(px + size * math.cos(theta_can),
                        py - size * math.sin(theta_can))
        left  = QPointF(px + size * 0.7 * math.cos(theta_can + 2.5),
                        py - size * 0.7 * math.sin(theta_can + 2.5))
        right = QPointF(px + size * 0.7 * math.cos(theta_can - 2.5),
                        py - size * 0.7 * math.sin(theta_can - 2.5))
        p.setPen(QPen(QColor("#ff5555"), 2))
        p.setBrush(QBrush(QColor("#ff5555")))
        p.drawPolygon([tip, left, right])

        # Info overlay
        p.setPen(QColor("#a0a0a0"))
        p.drawText(8, 18, f"Field: {self.field_w_cm:.0f} x "
                           f"{self.field_h_cm:.0f} cm")
        p.drawText(8, 34, f"Robot: ({rx_mm}, {ry_mm}) mm  "
                           f"θ={self.robot_theta_mdeg / 1000:.1f}°  "
                           f"flip=({int(self.flip_x)},{int(self.flip_y)})")

        # Frame labels
        p.drawText(int(cw - 60), int(cy - 6), "+X")
        p.drawText(int(cx + 6), 14, "+Y")

    # ---- Mouse ----
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drawing = True
            self._add_point(event.position())
        elif event.button() == Qt.RightButton:
            if self.drawn_points:
                self.drawn_points.pop()
                self.update()

    def mouseMoveEvent(self, event):
        if self.drawing:
            self._add_point(event.position())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drawing = False

    def _add_point(self, pos):
        fx_cm, fy_cm = self._pixel_to_canvas_cm(pos.x(), pos.y())
        if self.drawn_points:
            lx, ly = self.drawn_points[-1]
            if (fx_cm - lx) ** 2 + (fy_cm - ly) ** 2 < 0.25:  # < 0.5 cm
                return
        self.drawn_points.append((fx_cm, fy_cm))
        self.update()


# ============================================================================
# Upload Worker
# ============================================================================

class UploadWorker(QThread):
    """Paced upload of a path to the firmware, with path_count verification.

    Emits:
        progress(cur, total) after each ADD
        status(message)      for intermediate UI updates
        finished(success, message)
    """

    progress = Signal(int, int)
    status   = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, bridge, points_mm, max_retries=1, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        # points_mm is already in the firmware's wire frame (see _on_send_go).
        self.points_mm = list(points_mm)
        self.max_retries = max_retries
        self._abort = False

    def request_abort(self):
        self._abort = True

    def _send(self, cmd):
        try:
            return self.bridge.send_command(cmd)
        except Exception:
            return None

    def run(self):
        n = len(self.points_mm)
        if n == 0:
            self.finished.emit(False, "Empty path")
            return

        for attempt in range(1 + self.max_retries):
            if self._abort:
                self.finished.emit(False, "Cancelled")
                return

            self.status.emit(f"Uploading ({attempt + 1}/{1 + self.max_retries})...")

            self._send("PATH:CLEAR")
            time.sleep(0.10)

            for i, (x_mm, y_mm) in enumerate(self.points_mm):
                if self._abort:
                    self.finished.emit(False, "Cancelled")
                    return
                self._send(f"PATH:ADD={i},{int(x_mm)},{int(y_mm)}")
                self.progress.emit(i + 1, n)
                time.sleep(UPLOAD_PACE_S)

            # Let remaining confirms drain and path_count variable update.
            time.sleep(0.20)

            # Abort check before GO (covers "STOP clicked during drain").
            if self._abort:
                self.finished.emit(False, "Cancelled before GO")
                return

            pc = _var_int("path_count", -1)
            if pc == n:
                self.status.emit(f"Verified path_count={pc}. Starting...")
                self._send("PATH:GO")
                self.finished.emit(True, f"Path started ({pc} points)")
                return

            self.status.emit(f"Verification failed "
                             f"(path_count={pc} != {n})")

        self.finished.emit(
            False, f"Upload failed after {1 + self.max_retries} attempts")


# ============================================================================
# Path Panel
# ============================================================================

class PathPanel(QWidget):
    """Path drawing + upload panel.

    Usage from main_pyside6.py:
        self.path_panel = PathPanel(bridge=self.bridge)
        tabs.addTab(self.path_panel, "Path")
        # on connect/disconnect:
        self.path_panel.set_bridge(self.bridge)  # (or pass None to detach)
    """

    def __init__(self, bridge=None, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.upload_worker = None
        self._running = False
        self._stop_seen_since = None   # debounce: path_active/done -> stop pinging

        self.setup_ui()

        self.ping_timer = QTimer(self)
        self.ping_timer.timeout.connect(self._on_ping)

        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self._on_live_update)
        self.update_timer.start(POSE_UPDATE_MS)

    def set_bridge(self, bridge):
        self.bridge = bridge

    # ---- UI ----
    def setup_ui(self):
        layout = QVBoxLayout(self)

        # Row 1: field size, tolerance, flips
        row1 = QHBoxLayout()

        row1.addWidget(QLabel("Field:"))
        self.field_w_spin = QSpinBox()
        self.field_w_spin.setRange(20, 500)
        self.field_w_spin.setValue(100)
        self.field_w_spin.setSuffix(" cm")
        row1.addWidget(self.field_w_spin)

        row1.addWidget(QLabel("x"))
        self.field_h_spin = QSpinBox()
        self.field_h_spin.setRange(20, 500)
        self.field_h_spin.setValue(100)
        self.field_h_spin.setSuffix(" cm")
        row1.addWidget(self.field_h_spin)

        row1.addSpacing(20)
        row1.addWidget(QLabel("Tolerance:"))
        self.tolerance_spin = QDoubleSpinBox()
        self.tolerance_spin.setRange(0.1, 20.0)
        self.tolerance_spin.setDecimals(1)
        self.tolerance_spin.setSingleStep(0.5)
        self.tolerance_spin.setValue(DEFAULT_TOLERANCE_CM)
        self.tolerance_spin.setSuffix(" cm")
        row1.addWidget(self.tolerance_spin)

        row1.addSpacing(20)
        self.flip_x_cb = QCheckBox("Flip X")
        self.flip_x_cb.setChecked(True)
        self.flip_x_cb.setToolTip("Match PATHFINDER_X_SIGN = -1")
        row1.addWidget(self.flip_x_cb)

        self.flip_y_cb = QCheckBox("Flip Y")
        self.flip_y_cb.setChecked(True)
        self.flip_y_cb.setToolTip("Match PATHFINDER_Y_SIGN = -1")
        row1.addWidget(self.flip_y_cb)

        row1.addStretch()
        layout.addLayout(row1)

        # Row 2: heading + actions
        row2 = QHBoxLayout()

        row2.addWidget(QLabel("Heading:"))
        self.heading_spin = QDoubleSpinBox()
        self.heading_spin.setRange(-180.0, 180.0)
        self.heading_spin.setDecimals(1)
        self.heading_spin.setSingleStep(5.0)
        self.heading_spin.setValue(0.0)
        self.heading_spin.setSuffix(" °")
        row2.addWidget(self.heading_spin)

        self.apply_heading_btn = QPushButton("Apply")
        row2.addWidget(self.apply_heading_btn)

        row2.addSpacing(20)

        self.clear_btn = QPushButton("Clear")
        row2.addWidget(self.clear_btn)

        self.send_btn = QPushButton("▶ Send & Go")
        self.send_btn.setStyleSheet(
            "background-color: #27ae60; color: white; font-weight: bold;"
            " padding: 4px 12px;")
        row2.addWidget(self.send_btn)

        self.stop_btn = QPushButton("■ STOP")
        self.stop_btn.setStyleSheet(
            "background-color: #c0392b; color: white; font-weight: bold;"
            " padding: 4px 12px;")
        row2.addWidget(self.stop_btn)

        row2.addStretch()
        self.points_label = QLabel("Points: 0")
        row2.addWidget(self.points_label)
        layout.addLayout(row2)

        # Status
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #a0a0a0; padding: 2px;")
        layout.addWidget(self.status_label)

        # Canvas
        self.canvas = PathCanvas()
        layout.addWidget(self.canvas, 1)

        # Wire signals
        self.field_w_spin.valueChanged.connect(self._on_field_changed)
        self.field_h_spin.valueChanged.connect(self._on_field_changed)
        self.flip_x_cb.toggled.connect(self._on_flip_changed)
        self.flip_y_cb.toggled.connect(self._on_flip_changed)
        self.clear_btn.clicked.connect(self._on_clear)
        self.send_btn.clicked.connect(self._on_send_go)
        self.stop_btn.clicked.connect(self._on_stop)
        self.apply_heading_btn.clicked.connect(self._on_apply_heading)

    # ---- Handlers ----
    def _on_field_changed(self):
        self.canvas.set_field(self.field_w_spin.value(),
                              self.field_h_spin.value())

    def _on_flip_changed(self):
        # The frame the user drew in just changed; clearing is safer than
        # silently re-interpreting every point.
        if self.canvas.drawn_points:
            reply = QMessageBox.question(
                self, "Flip changed",
                "Changing the axis flip will clear the drawn path. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                self.flip_x_cb.blockSignals(True)
                self.flip_y_cb.blockSignals(True)
                self.flip_x_cb.setChecked(self.canvas.flip_x)
                self.flip_y_cb.setChecked(self.canvas.flip_y)
                self.flip_x_cb.blockSignals(False)
                self.flip_y_cb.blockSignals(False)
                return
        self.canvas.clear_drawn()
        self.canvas.set_flip(self.flip_x_cb.isChecked(),
                             self.flip_y_cb.isChecked())
        self._refresh_point_count()

    def _on_clear(self):
        self.canvas.clear_drawn()
        self._refresh_point_count()

    def _refresh_point_count(self):
        self.points_label.setText(f"Points: {len(self.canvas.drawn_points)}")

    def _on_apply_heading(self):
        if not self.bridge or not self.bridge.connected:
            self.status_label.setText("Not connected")
            return
        canvas_hdg = self.heading_spin.value()
        raw_hdg = self._canvas_heading_to_raw(canvas_hdg)
        wire = int(round(raw_hdg * 100))     # firmware expects x100
        self.bridge.send_command(f"SET:heading_deg={wire}")
        self.status_label.setText(
            f"Heading: canvas={canvas_hdg:.1f}° raw={raw_hdg:.1f}°")

    def _canvas_heading_to_raw(self, canvas_deg):
        """Invert the canvas flip transform on the heading angle (one
        reflection or one rotation, depending on which axes are flipped)."""
        fx = self.flip_x_cb.isChecked()
        fy = self.flip_y_cb.isChecked()
        if fx and not fy:
            return 180.0 - canvas_deg   # reflection across Y axis
        if not fx and fy:
            return -canvas_deg          # reflection across X axis
        if fx and fy:
            return canvas_deg - 180.0   # rotation by 180°
        return canvas_deg

    def _on_stop(self):
        self._running = False
        self._stop_seen_since = None
        self.ping_timer.stop()
        if self.upload_worker and self.upload_worker.isRunning():
            self.upload_worker.request_abort()
        if self.bridge and self.bridge.connected:
            self.bridge.send_command("PATH:STOP")
        self.status_label.setText("STOP sent - parked")

    def stop_all(self):
        """Used before flashing: send PATH:STOP without dialogs."""
        self._on_stop()

    def _on_send_go(self):
        if not self.bridge or not self.bridge.connected:
            QMessageBox.warning(self, "Not connected",
                                "Connect to the ESP32 first.")
            return

        if len(self.canvas.drawn_points) < 2:
            QMessageBox.warning(self, "Path too short",
                                "Draw at least 2 points on the canvas.")
            return

        rx = _var_int("x_mm")
        ry = _var_int("y_mm")
        if rx is None or ry is None:
            QMessageBox.warning(self, "No pose",
                                "Robot pose unknown. Is the debug link up?")
            return

        # Simplify, auto-raising tolerance above the soft point limit.
        tol = self.tolerance_spin.value()
        pts = rdp_simplify(self.canvas.drawn_points, tol)
        while len(pts) > MAX_UPLOAD_POINTS and tol < MAX_AUTO_TOLERANCE_CM:
            tol *= 2.0
            pts = rdp_simplify(self.canvas.drawn_points, tol)

        if len(pts) > HARD_MAX_POINTS:
            QMessageBox.critical(self, "Path too complex",
                                 f"Even at {tol:.1f} cm tolerance, path has "
                                 f"{len(pts)} points (max {HARD_MAX_POINTS}).")
            return
        if len(pts) > MAX_UPLOAD_POINTS:
            QMessageBox.warning(self, "Path long",
                                f"Using {len(pts)} points (target ≤ "
                                f"{MAX_UPLOAD_POINTS}). Upload may be slow.")

        # Convert canvas (cm) → firmware wire frame (mm).
        #   flip=ON  : canvas = corrected frame → send drawn * 10
        #   flip=OFF : canvas = raw frame → send -drawn * 10
        sx = 1 if self.flip_x_cb.isChecked() else -1
        sy = 1 if self.flip_y_cb.isChecked() else -1
        upload_mm = [(int(round(sx * fx_cm * 10)),
                      int(round(sy * fy_cm * 10)))
                     for (fx_cm, fy_cm) in pts]

        # Snap the first waypoint to the robot's current firmware position.
        # Firmware wants target_cm = PATHFINDER_X_SIGN * robot.x_cm, i.e.
        # send_mm = PATHFINDER_X_SIGN * robot.x_mm (same for Y).
        upload_mm[0] = (PATHFINDER_X_SIGN * rx,
                        PATHFINDER_Y_SIGN * ry)

        # Claim the drive gates. The ArUco tab arms in dry-run mode
        # (motors_en=0) and can leave axis_mask at a bench value; without
        # this, PATH:GO is accepted but Pathfinder_Drive() holds the wheels
        # off -- the "ArUco overrides the path" symptom.
        if hasattr(self.bridge, "claim_motion"):
            self.bridge.claim_motion(7)
        else:
            self.bridge.send_command("SET:motors_en=1")
            self.bridge.send_command("SET:axis_mask=7")

        # Start worker
        self.upload_worker = UploadWorker(self.bridge, upload_mm,
                                          max_retries=1)
        self.upload_worker.progress.connect(self._on_upload_progress)
        self.upload_worker.status.connect(self.status_label.setText)
        self.upload_worker.finished.connect(self._on_upload_finished)

        self.send_btn.setEnabled(False)
        self.canvas.clear_trail()
        self.upload_worker.start()

    def _on_upload_progress(self, cur, total):
        self.status_label.setText(f"Uploading {cur}/{total}...")

    def _on_upload_finished(self, success, message):
        self.send_btn.setEnabled(True)
        self.status_label.setText(("✓ " if success else "✗ ") + message)
        if success:
            self._running = True
            self.ping_timer.start(PING_INTERVAL_MS)

    def _on_ping(self):
        if self._running and self.bridge and self.bridge.connected:
            self.bridge.send_command("PATH:PING")
        elif not (self.bridge and self.bridge.connected):
            self._running = False
            self.ping_timer.stop()

    def _on_live_update(self):
        rx = _var_int("x_mm")
        ry = _var_int("y_mm")
        th = _var_int("theta_mdeg")
        if rx is not None and ry is not None and th is not None:
            self.canvas.set_robot(rx, ry, th)

        # Stop pinging if link dropped
        if self._running and not (self.bridge and self.bridge.connected):
            self._running = False
            self._stop_seen_since = None
            self.ping_timer.stop()

        # Firmware releases path_active when the final waypoint reaches DONE
        # (or PATH:STOP clears it). Debounce, because PATH:ADD batch upload
        # briefly has path_active=0 before PATH:GO.
        if self._running:
            done = _var_int("path_done", 0)
            active = _var_int("path_active", 1)
            if done == 1 or active == 0:
                if self._stop_seen_since is None:
                    self._stop_seen_since = time.time()
                elif time.time() - self._stop_seen_since > 0.5:
                    self._running = False
                    self._stop_seen_since = None
                    self.ping_timer.stop()
                    self.status_label.setText(
                        "Path complete" if done == 1 else "Path stopped")
            else:
                self._stop_seen_since = None