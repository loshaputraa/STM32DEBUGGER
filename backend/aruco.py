# ============================================================================
# ArUco pose estimation for STM32 visual alignment (PC side, no Qt)
# ============================================================================
# Pipeline: USB camera -> ArUco detect (DICT_4X4_50, ID 0) -> solvePnP
#           (SOLVEPNP_IPPE_SQUARE) -> marker yaw -> robot-centre pose relative
#           to the goal pose -> "ARUCO:POSE=x_mm,y_mm,th_mdeg,brg_mdeg".
#
# The conversion reproduces the agreed geometry:
#   camera lens 240 mm in front of robot centre, robot drives to 50 mm
#   from the marker (lens-to-marker), facing it straight on.
#
#   tz_c = tz + CAM_FWD_MM
#   x_mm = -(cos(th)*tx - sin(th)*tz_c)
#   y_mm = -(sin(th)*tx + cos(th)*tz_c) + (CAM_FWD_MM + GOAL_MM)
#   brg  = atan2(tx, tz)          (marker bearing, + = marker to the right)
#
#   At the goal (tx=0, tz=50, th=0): x=0, y=0  -> pathfinder target (0,0).
#
# NOTE the firmware treats this as the CORRECTED frame. PATHFINDER_X_SIGN /
# PATHFINDER_Y_SIGN stay -1 (they correct encoder polarity; do not override
# them), and ArUco_Tick() divides by those signs when it writes robot.x/y, so
# the pathfinder sees exactly the numbers sent here. The final goal is (0,0)
# heading 0 (Pathfinder_SetArucoGoal), reached through the normal
# MOVE -> LATCH -> ROTATE -> DONE flow.
#
# Bench-verify list (see the plan):
#   * YAW_SIGN  - flip if the robot turns away from the marker it is facing
#   * tx sign   - +X must be to the RIGHT in the image
#   * MARKER_SIZE_MM must be the measured printed black border
# ============================================================================

import math
import os

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

# ============================================================================
# Configuration (bench-tunable)
# ============================================================================
MARKER_ID = 0
MARKER_SIZE_MM = 260.0  # measure the PRINTED black border, not the PNG
CAM_FWD_MM = 240.0  # camera lens ahead of robot centre
GOAL_MM = 50.0  # wanted lens-to-marker distance at the goal

ALPHA = 0.35  # EMA on yaw (0..1; lower = smoother)
YAW_SIGN = -1.0  # bench-verify; +1 / -1 only
MAX_YAW_JUMP_DEG = 30.0  # reject IPPE flips between frames
MIN_TZ_MM = 20.0  # ignore solves closer than this (garbage)
MAX_TZ_MM = 1500.0
POSE_LIMIT_MM = 1500.0  # matches ARUCO_POSE_LIMIT_MM on the STM32

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480

WIRE_PREFIX = "ARUCO:POSE="


# ============================================================================
# Pose conversion
# ============================================================================

def pose_from_marker(tx_mm, tz_mm, th_deg):
    """Marker observation -> robot-centre pose relative to the goal.

    tx_mm, tz_mm : marker position in the camera frame (mm), +x right, +z fwd
    th_deg       : marker yaw relative to the camera axis, 0 = straight on
    """
    tz_c = float(tz_mm) + CAM_FWD_MM
    th = math.radians(float(th_deg))

    x_mm = -(math.cos(th) * float(tx_mm) - math.sin(th) * tz_c)
    y_mm = -(math.sin(th) * float(tx_mm) + math.cos(th) * tz_c) + (CAM_FWD_MM + GOAL_MM)

    brg_deg = math.degrees(math.atan2(float(tx_mm), max(float(tz_mm), 1e-6)))

    return {
        "x_mm": int(round(x_mm)),
        "y_mm": int(round(y_mm)),
        "th_mdeg": int(round(float(th_deg) * 1000.0)),
        "brg_mdeg": int(round(brg_deg * 1000.0)),
    }


def pose_in_range(pose):
    """Reject nonsense before it reaches the firmware."""
    if pose is None:
        return False
    return (abs(pose["x_mm"]) <= POSE_LIMIT_MM and
            abs(pose["y_mm"]) <= POSE_LIMIT_MM and
            abs(pose["th_mdeg"]) <= 180000 and
            abs(pose["brg_mdeg"]) <= 180000)


def wire_command(pose):
    return (f"{WIRE_PREFIX}{pose['x_mm']},{pose['y_mm']},"
            f"{pose['th_mdeg']},{pose['brg_mdeg']}\r")


def describe(pose):
    if pose is None:
        return "no marker"
    return (f"x={pose['x_mm']:6d} mm  y={pose['y_mm']:6d} mm  "
            f"th={pose['th_mdeg'] / 1000.0:7.2f} deg  "
            f"brg={pose['brg_mdeg'] / 1000.0:7.2f} deg")


# ============================================================================
# Detector
# ============================================================================

class ArucoDetector:
    """Owns the camera + solvePnP. One instance per run."""

    def __init__(self, calib_path, marker_id=MARKER_ID,
                 camera_index=0, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
        self.calib_path = calib_path
        self.marker_id = int(marker_id)
        self.camera_index = int(camera_index)
        self.width = int(width)
        self.height = int(height)

        self._cap = None
        self._detector = None
        self._mtx = None
        self._dist = None
        self._edges = None
        self._yaw_smooth = None
        self._last_good_yaw = None
        self.frames = 0
        self.rejects = 0

    # ---- lifecycle ----
    def open(self):
        if cv2 is None:
            raise RuntimeError("OpenCV is not installed (pip install opencv-python)")
        if np is None:
            raise RuntimeError("NumPy is not installed")
        if not os.path.exists(self.calib_path):
            raise RuntimeError(f"calibration file not found: {self.calib_path}")

        with np.load(self.calib_path) as data:
            self._mtx = data["mtx"]
            self._dist = data["dist"]

        half = (MARKER_SIZE_MM / 1000.0) / 2.0
        self._edges = np.array([
            [-half, half, 0.0],
            [half, half, 0.0],
            [half, -half, 0.0],
            [-half, -half, 0.0],
        ], dtype=np.float32)

        aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        params = cv2.aruco.DetectorParameters()
        self._detector = cv2.aruco.ArucoDetector(aruco_dict, params)

        cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            raise RuntimeError(f"camera {self.camera_index} did not open")

        # MJPG is what actually delivers 640x480 at a usable frame rate on a
        # cheap 5 MP webcam; the default YUY2 mode is far slower.
        fourcc = getattr(cv2, "VideoWriter_fourcc", None)
        if fourcc is not None:
            cap.set(cv2.CAP_PROP_FOURCC, fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self._cap = cap
        return self

    def close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    # ---- per-frame ----
    def read(self):
        """Return (pose_or_None, annotated_bgr_frame_or_None, reason_or_None)."""
        if self._cap is None:
            return None, None, "Camera not open"

        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None, None, "Camera frame grab failed"

        self.frames += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids, _ = self._detector.detectMarkers(gray)

        if ids is None:
            return None, frame, "No markers found in view"

        cv2.aruco.drawDetectedMarkers(frame, corners, ids)

        try:
            matches = np.where(ids.flatten() == self.marker_id)[0]
        except Exception:
            return None, frame, "ID parsing error"

        if len(matches) == 0:
            self.rejects += 1
            return None, frame, f"Wrong ID detected: {ids.flatten().tolist()}"

        index = int(matches[0])
        ok, rvec, tvec = cv2.solvePnP(
            self._edges, corners[index][0], self._mtx, self._dist,
            flags=cv2.SOLVEPNP_IPPE_SQUARE)

        if not ok:
            self.rejects += 1
            return None, frame, "solvePnP math failed"

        tx_mm = float(tvec[0][0]) * 1000.0
        tz_mm = float(tvec[2][0]) * 1000.0

        if not (MIN_TZ_MM <= tz_mm <= MAX_TZ_MM):
            self.rejects += 1
            return None, frame, f"tz out of bounds: {tz_mm:.1f}mm"
        rmat, _ = cv2.Rodrigues(rvec)

        r22 = float(rmat[2, 2])

        # If the solver flipped the marker backwards, correct it by
        # rotating the matrix 180 degrees around the X and Y axes.
        if r22 < 0:
            rmat = -rmat
            rvec, _ = cv2.Rodrigues(rmat)
            r22 = -r22  # Now positive

        if r22 < 0.25:
            self.rejects += 1
            return None, frame, f"R22 angle fail: {r22:.2f} (needs > 0.25)"

        yaw_deg = math.degrees(math.atan2(float(rmat[0, 2]), float(rmat[2, 2])))
        yaw_deg *= YAW_SIGN

        if (self._last_good_yaw is not None and
                abs(yaw_deg - self._last_good_yaw) > MAX_YAW_JUMP_DEG):
            self.rejects += 1
            return None, frame, f"Yaw jump: {yaw_deg:.1f} vs {self._last_good_yaw:.1f} deg"

        self._last_good_yaw = yaw_deg
        if self._yaw_smooth is None:
            self._yaw_smooth = yaw_deg
        else:
            self._yaw_smooth = ALPHA * yaw_deg + (1.0 - ALPHA) * self._yaw_smooth

        pose = pose_from_marker(tx_mm, tz_mm, self._yaw_smooth)

        if not pose_in_range(pose):
            self.rejects += 1
            return None, frame, f"Pose out of range: x={pose['x_mm']}, y={pose['y_mm']}"

        # Success
        cv2.drawFrameAxes(frame, self._mtx, self._dist, rvec, tvec,
                          (MARKER_SIZE_MM / 1000.0) / 2.0)
        cv2.putText(frame, f"ID {self.marker_id}  z={tz_mm:.0f}mm  yaw={self._yaw_smooth:.1f}",
                    (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

        return pose, frame, None