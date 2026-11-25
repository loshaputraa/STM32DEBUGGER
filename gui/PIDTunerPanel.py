# ============================================================================
# PID Tuner Panel - live gain editing, step/path tests, auto-tune
# ============================================================================
# One tab that does four things:
#   1. shows / edits the three tunable loops exposed by the firmware
#      (Rotation PID, Heading PI, Position PD) and pushes them with SET:
#   2. plots the live P / I / D / total of the selected loop
#   3. runs the test sequences on the robot (rotation step, straight,
#      zig-zag, curve) with the same PATH:CLEAR / PATH:ADD / PATH:GO
#      handshake PathPanel uses
#   4. runs the closed-loop auto-tune heuristic off the GUI thread
#
# FIRMWARE CONTRACT (see Core/Src/app.c + Core/Inc/pathfinder.h):
#   SET:<gain>=<value*100>     apply a gain (clamped by the firmware too)
#   ROTATE_TO:<deg*100>        absolute rotation step, enters the ROTATE
#                              stage with the integrators cleared
#   SET:rot_lock=0|1           disable/enable the fixed-PWM direction lock so
#                              big steps are real PID responses
#   PATH:CLEAR/ADD/GO/STOP/PING   unchanged; PATH:PING keeps the 1.5 s
#                              link-loss watchdog alive while a test runs
#
# SAFETY: every test sets path_active on the firmware side (PATH:GO and
# ROTATE_TO both do), so the firmware stops the robot by itself if this app
# stops pinging. STOP aborts the worker, sends PATH:STOP and restores the
# direction lock.
# ============================================================================

import math
import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QComboBox, QDoubleSpinBox, QSpinBox, QPlainTextEdit, QMessageBox,
    QGroupBox
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal
import pyqtgraph as pg

from backend.pid_tuner import (
    TARGETS, ROTATION, HEADING, POSITION, target_for,
    to_wire, wrap_deg, step_metrics, heading_metrics, cte_metrics,
    pose_to_corrected_cm, straight_path, zigzag_path, curve_path,
    path_length_cm, suggest_gains, within_tolerance, describe,
    odom_lever_arms_round_trip, clamp_odom, wrap_encoder_delta, STAGE_DONE,
)

# ============================================================================
# Configuration
# ============================================================================
MODULE_DEFAULTS = {
    # Fallbacks used only when the device hasn't reported a value yet
    # (they mirror robot_params.h).
    "kp_hdg": 63.0, "ki_hdg": 0.0, "kd_hdg": 8.4,
    "kp_hdg_move": 80.0, "ki_hdg_move": 60.0,
    "kp_pos": 22.0, "kd_pos": 9.05,
}

STEP_ANGLES = (90.0, -90.0)      # measured steps; +-180 only stresses the lock
STEP_SETTLE_TIMEOUT_S = 8.0
STEP_HOLD_S = 0.35               # settle window captured after DONE
PATH_COMPLETE_MARGIN_S = 12.0
PATH_PER_CM_S = 0.10
GRAPH_WINDOW_S = 15.0
GRAPH_UPDATE_MS = 100
PING_INTERVAL_MS = 500

ODOM_CAL_MAX_ITERS = 4            # fallback revolution count (panel overrides)
ODOM_CAL_SEGMENTS = 4             # 90 deg steps per 360 deg revolution

CURVE_COLOURS = {
    "P": "#e74c3c",
    "I": "#f1c40f",
    "D": "#3498db",
    "Total": "#2ecc71",
}


# ============================================================================
# data_store access (lazy + defensive: this widget must also survive the
# mock-backend fallback in main_pyside6.py)
# ============================================================================

def _ds():
    try:
        from backend import data_store
        return data_store
    except ImportError:
        return None


def _var(name, default=None):
    """Read a variable's numeric value from the live store."""
    ds = _ds()
    if ds is None:
        return default
    entry = getattr(ds, "variables", {}).get(name)
    if not entry:
        return default
    try:
        return float(entry["value"])
    except (KeyError, TypeError, ValueError):
        return default


def _history(name):
    """All (timestamp, value) samples recorded for `name`."""
    ds = _ds()
    hist = getattr(ds, "history", None) if ds else None
    if not hist:
        return []
    buf = hist.get(name)
    if buf is None:
        return []
    try:
        return buf.get_data()
    except AttributeError:
        return list(buf)


# ============================================================================
# Worker - owns the robot-side of a test / auto-tune run
# ============================================================================

class TunerWorker(QThread):
    """Runs one test sequence, or the full auto-tune loop, off the GUI thread.

    Signals
        status(str)        one-line progress for the status label
        note(str)          a line for the metrics/report box
        gains(object)      {gain_name: real value} just applied to the device
        result(object)     {metrics, gains, iteration, reasons, converged}
        done(bool, str)    terminal: success flag + summary message
    """

    status = Signal(str)
    note = Signal(str)
    gains = Signal(object)
    result = Signal(object)
    done = Signal(bool, str)

    def __init__(self, bridge, target, gains, path_points=None,
                 max_iters=1, auto_tune=False, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.target = target
        self.gain_values = dict(gains)
        self.path_points = list(path_points or [])
        self.max_iters = max(1, int(max_iters))
        self.auto_tune = bool(auto_tune)
        self._abort = False
        self._last_ping = 0.0

    # ---- lifecycle ----
    def request_abort(self):
        self._abort = True

    def run(self):
        try:
            if self.auto_tune:
                self._run_auto_tune()
            else:
                self._run_single()
        except Exception as exc:                      # noqa: BLE001 - report, never crash the thread
            self.done.emit(False, f"error: {exc}")

    # ---- low-level I/O ----
    def _send(self, command):
        if self._abort:
            return
        if not self.bridge or not self.bridge.connected:
            raise RuntimeError("bridge disconnected")
        self.bridge.send_command(command)

    def _claim_motion(self):
        """Undo the ArUco tab's dry-run gates (motors_en / axis_mask) before
        driving. Reused by every motion sequence, including ROTATE_TO."""
        if not self.bridge or not self.bridge.connected:
            raise RuntimeError("bridge disconnected")
        if hasattr(self.bridge, "claim_motion"):
            self.bridge.claim_motion(7)
        else:
            self.bridge.send_command("SET:motors_en=1")
            self.bridge.send_command("SET:axis_mask=7")

    def _maybe_ping(self):
        """Keep the firmware's PATH link-loss watchdog fed during a run."""
        now = time.time()
        if now - self._last_ping >= (PING_INTERVAL_MS / 1000.0):
            self._last_ping = now
            try:
                if self.bridge and self.bridge.connected:
                    self.bridge.send_command("PATH:PING")
            except Exception:                          # noqa: BLE001
                pass

    def _sleep(self, seconds):
        """Sleep that stays abortable and keeps pinging. False if aborted."""
        end = time.time() + seconds
        while True:
            if self._abort:
                return False
            remaining = end - time.time()
            if remaining <= 0.0:
                return True
            self._maybe_ping()
            time.sleep(min(0.05, remaining))

    def _wait_for(self, predicate, timeout, poll=0.05):
        """Poll `predicate` until true or `timeout`. False on timeout/abort."""
        end = time.time() + timeout
        while time.time() < end:
            if self._abort:
                return False
            try:
                if predicate():
                    return True
            except Exception:                          # noqa: BLE001
                pass
            self._maybe_ping()
            time.sleep(poll)
        return False

    # ---- device state helpers ----
    def _stage(self):
        value = _var("stage")
        return -1 if value is None else int(value)

    def _wp(self):
        value = _var("wp_idx")
        return -1 if value is None else int(value)

    def _apply_gains(self, gains):
        for name in self.target.gain_names:
            self._send(f"SET:{name}={to_wire(gains[name])}")
            if not self._sleep(0.05):
                return False
        self.gain_values = dict(gains)
        self.gains.emit(dict(gains))
        return True

    def _samples(self, var_name, t_from, t_to, scale=1.0):
        """History window -> [(t - t_from, value * scale)]."""
        out = []
        for stamp, value in _history(var_name):
            if t_from <= stamp <= t_to:
                out.append((stamp - t_from, float(value) * scale))
        return out

    # ---- test sequences ----
    def _run_single(self):
        if not self._apply_gains(self.gain_values):
            self.done.emit(False, "aborted")
            return
        if not self._sleep(0.30):
            self.done.emit(False, "aborted")
            return

        metrics = self._run_test()

        if metrics is None:
            self.done.emit(False, "no usable data from the test run")
            return

        # A manual test is also a measurement: compute what the auto-tuner
        # would change, and show it. Nothing is sent - the user decides.
        suggested, reasons = suggest_gains(self.target, self.gain_values, metrics)
        self.result.emit({
            "metrics": metrics,
            "gains": dict(self.gain_values),
            "iteration": 1,
            "reasons": reasons,
            "suggested": suggested,
            "converged": within_tolerance(self.target, metrics),
        })
        self.done.emit(True, "test complete")

    def _run_auto_tune(self):
        gains = dict(self.gain_values)

        for iteration in range(1, self.max_iters + 1):
            if self._abort:
                self.done.emit(False, "aborted")
                return

            self.status.emit(
                f"Auto-tune {iteration}/{self.max_iters}: "
                + ", ".join(f"{k}={v:g}" for k, v in gains.items()))
            self.note.emit(
                f"--- iteration {iteration}/{self.max_iters} ---  "
                + ", ".join(f"{k}={v:g}" for k, v in gains.items()))

            if not self._apply_gains(gains):
                self.done.emit(False, "aborted")
                return
            if not self._sleep(0.40):
                self.done.emit(False, "aborted")
                return

            metrics = self._run_test()

            if self._abort:
                self.done.emit(False, "aborted")
                return

            converged = within_tolerance(self.target, metrics)
            if metrics is None:
                self.note.emit("no usable telemetry from this iteration")
                self.done.emit(
                    False,
                    f"iteration {iteration} produced no usable telemetry - "
                    f"auto-tune stopped")
                return
            for line in describe(self.target, metrics):
                self.note.emit(line)

            new_gains, reasons = suggest_gains(self.target, gains, metrics)
            for reason in reasons:
                self.note.emit(f"  -> {reason}")

            self.result.emit({
                "metrics": metrics,
                "gains": dict(gains),
                "iteration": iteration,
                "reasons": reasons,
                "suggested": new_gains,
                "converged": converged,
            })

            if converged:
                self.done.emit(True, f"converged after {iteration} iteration(s)")
                return

            if new_gains == gains:
                # Heuristics have nothing left to change (limits reached) -
                # another identical run would waste the user's floor space.
                self.done.emit(
                    False,
                    f"stopped at iteration {iteration}: gains are at their "
                    f"limits, metrics still outside tolerance")
                return

            gains = new_gains
            if not self._sleep(0.30):
                self.done.emit(False, "aborted")
                return

        self.done.emit(False, f"reached the {self.max_iters}-iteration limit")

    def _run_test(self):
        if self.target.test == "step":
            return self._run_step_test()
        return self._run_path_test()

    # ---- rotation step response ----
    def _run_step_test(self):
        """+90 deg and -90 deg steps, measured separately and combined with
        the worst case of each metric (conservative for the auto-tune loop)."""
        try:
            self._claim_motion()                  # ArUco dry run may have left gates off
            self._send("SET:rot_lock=0")          # pure PID on big steps
            self._sleep(0.20)

            # Start from the MEASURED heading, not from the stored heading
            # target: at boot that target is 0, which has nothing to do with
            # where the robot is actually pointing, so the first "step" would
            # be an arbitrary angle instead of 90 deg.
            theta_mdeg = _var("theta_mdeg")
            if theta_mdeg is not None:
                base = theta_mdeg / 1000.0
            else:
                base = (_var("heading_deg", 0.0) or 0.0) / 100.0
            base = wrap_deg(base)

            per_step = []

            for delta in STEP_ANGLES:
                if self._abort:
                    return None

                base = wrap_deg(base + delta)
                self.status.emit(f"Step {delta:+.0f} deg -> target {base:+.1f} deg")

                t_from = time.time()
                self._send(f"ROTATE_TO:{to_wire(base)}")
                self._sleep(0.15)                  # let the stage actually change

                settled = self._wait_for(
                    lambda: self._stage() == STAGE_DONE,
                    timeout=STEP_SETTLE_TIMEOUT_S)
                self._sleep(STEP_HOLD_S)           # capture the steady state

                t_to = time.time()
                samples = self._samples("hdg_err_x100", t_from, t_to, scale=0.01)
                metrics = step_metrics(samples, delta)

                if metrics is None:
                    self.note.emit(
                        f"  step {delta:+.0f}: not enough telemetry "
                        f"({len(samples)} samples) - is the link up?")
                else:
                    per_step.append(metrics)
                if not settled:
                    self.note.emit(
                        f"  step {delta:+.0f}: did not reach DONE within "
                        f"{STEP_SETTLE_TIMEOUT_S:.0f}s")

                self._send("PATH:STOP")
                self._sleep(0.30)
        finally:
            if not self._abort:
                self._send("SET:rot_lock=1")       # restore the safety lock
                self._send("PATH:STOP")

        if not per_step:
            return None
        return _combine_step_metrics(per_step)

    # ---- path following (straight / zig-zag / curve) ----
    def _run_path_test(self):
        points = self.path_points
        if len(points) < 2:
            raise RuntimeError("test path is empty")

        self._claim_motion()                  # ArUco dry run may have left gates off

        # Hold the heading the robot currently has, so the test measures the
        # heading loop during translation instead of a turn right at t=0.
        theta = (_var("theta_mdeg", 0.0) or 0.0) / 1000.0
        self._send(f"SET:heading_deg={to_wire(wrap_deg(theta))}")

        self.status.emit(f"Uploading {len(points)} waypoints...")
        self._send("PATH:CLEAR")
        self._sleep(0.12)
        for index, (x_cm, y_cm) in enumerate(points):
            if self._abort:
                return None
            self._send(f"PATH:ADD={index},{int(round(x_cm * 10))},"
                       f"{int(round(y_cm * 10))}")
            if not self._sleep(0.03):
                return None
        self._sleep(0.25)

        count = _var("path_count")
        if count is None or int(count) != len(points):
            raise RuntimeError(
                f"path upload failed (path_count={count}, expected {len(points)})")

        last = len(points) - 1
        timeout = PATH_COMPLETE_MARGIN_S + PATH_PER_CM_S * path_length_cm(points)

        self.status.emit("Running path...")
        t_from = time.time()
        self._send("PATH:GO")

        completed = self._wait_for(
            lambda: self._stage() == STAGE_DONE and self._wp() >= last,
            timeout=timeout)

        t_to = time.time()
        self._send("PATH:STOP")

        if not completed:
            # Do NOT score a run that never finished: a robot that stalled at
            # the first waypoint has ~0 heading error and ~0 CTE, which would
            # look like a perfect tune.
            self.note.emit(
                f"  path did not report DONE on the last waypoint within "
                f"{timeout:.0f}s - run discarded (no metrics)")
            return None

        if self.target.key == "heading":
            samples = self._samples("hdg_err_x100", t_from + 1.0, t_to, scale=0.01)
            return heading_metrics(samples)

        # Position PD -> cross-track error against the intended polyline.
        xs = self._samples("x_mm", t_from, t_to)
        ys = self._samples("y_mm", t_from, t_to)
        n = min(len(xs), len(ys))
        actual = [pose_to_corrected_cm(xs[i][1], ys[i][1]) for i in range(n)]
        return cte_metrics(actual, points)


def _combine_step_metrics(per_step):
    """Worst case across the measured steps (+90 and -90)."""
    def worst(key):
        values = [m.get(key) for m in per_step]
        if any(v is None for v in values):
            return None
        return max(values)

    return {
        "kind": "step",
        "step_deg": float(max(abs(m["step_deg"]) for m in per_step)),
        "overshoot_pct": worst("overshoot_pct"),
        "rise_s": worst("rise_s"),
        "settle_s": worst("settle_s"),
        "ss_err_deg": worst("ss_err_deg"),
        "n_samples": sum(m["n_samples"] for m in per_step),
        "duration_s": round(sum(m["duration_s"] for m in per_step), 2),
    }


class OdomCalibrateWorker(QThread):
    """Spin the robot +90 deg and solve odom_l / odom_b from the RAW encoder
    travel (S1 = X wheel, S3 = Y wheel) over the turn, so a pure spin produces
    zero phantom translation.

    The solve is one-shot algebra (odom_l = -S1/dtheta, odom_b = +S3/dtheta),
    not a world-pose drift iteration, so a noisy or non-settled spin cannot
    make the lever arms exponentially worse: an invalid sample is discarded and
    the values are clamped to the physical mounting range.

    Requires the firmware ROTATE re-pin fix (ROTATE stage pins the position
    target to the current spot) so the turn is pure rotation with no
    DONE->MOVE runaway. odom_l / odom_b must be live tunables (SET:odom_l=).

    Signals
        status(str)               one-line progress
        note(str)                 a line for the metrics/report box
        values(float, float)      final odom_l, odom_b (meters)
        done(bool, str)           terminal
    """

    status = Signal(str)
    note = Signal(str)
    values = Signal(float, float)
    done = Signal(bool, str)

    def __init__(self, bridge, max_iters=ODOM_CAL_MAX_ITERS, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.max_iters = max(1, int(max_iters))
        self._abort = False
        self._last_ping = 0.0

    def request_abort(self):
        self._abort = True

    def run(self):
        try:
            self._run()
        except Exception as exc:                      # noqa: BLE001
            self.done.emit(False, f"error: {exc}")

    # ---- low-level I/O (same contract as TunerWorker) ----
    def _send(self, command):
        if self._abort:
            return
        if not self.bridge or not self.bridge.connected:
            raise RuntimeError("bridge disconnected")
        self.bridge.send_command(command)

    def _claim_motion(self):
        """Undo the ArUco tab's dry-run gates before spinning the wheels."""
        if hasattr(self.bridge, "claim_motion"):
            self.bridge.claim_motion(7)
        else:
            self._send("SET:motors_en=1")
            self._send("SET:axis_mask=7")

    def _maybe_ping(self):
        now = time.time()
        if now - self._last_ping >= (PING_INTERVAL_MS / 1000.0):
            self._last_ping = now
            try:
                if self.bridge and self.bridge.connected:
                    self.bridge.send_command("PATH:PING")
            except Exception:                         # noqa: BLE001
                pass

    def _sleep(self, seconds):
        end = time.time() + seconds
        while True:
            if self._abort:
                return False
            remaining = end - time.time()
            if remaining <= 0.0:
                return True
            self._maybe_ping()
            time.sleep(min(0.05, remaining))

    def _wait_for(self, predicate, timeout, poll=0.05):
        end = time.time() + timeout
        while time.time() < end:
            if self._abort:
                return False
            try:
                if predicate():
                    return True
            except Exception:                         # noqa: BLE001
                pass
            self._maybe_ping()
            time.sleep(poll)
        return False

    # ---- device state ----
    def _stage(self):
        value = _var("stage")
        return -1 if value is None else int(value)

    def _snapshot(self):
        """(enc3, enc4, theta_deg, x_mm, y_mm) from live telemetry."""
        e3 = _var("enc3")
        e4 = _var("enc4")
        t = _var("theta_mdeg")
        x = _var("x_mm")
        y = _var("y_mm")
        if None in (e3, e4, t, x, y):
            raise RuntimeError(
                "missing telemetry (enc3/enc4/theta_mdeg/x_mm/y_mm)")
        return (int(e3), int(e4), float(t) / 1000.0, float(x), float(y))

    def _odom(self):
        """(odom_l_m, odom_b_m); wire is mm -> /1000."""
        l = _var("odom_l")
        b = _var("odom_b")
        if l is None:
            l = -69.0          # ODOM_L default -0.069 m
        if b is None:
            b = 0.0            # ODOM_B default
        return float(l) / 1000.0, float(b) / 1000.0

    def _spin_revolution(self, direction, heading, segments=ODOM_CAL_SEGMENTS):
        """Rotate direction*360 deg as `segments` absolute-target steps.

        The running heading is advanced locally (unwrapped) and each target is
        wrapped to (-180, 180], so no step is ever >= 180 deg and ROTATE_TO's
        wrap cannot fold a step onto itself. Returns True only if every step
        reached DONE.
        """
        step = float(direction) * (360.0 / segments)
        for _ in range(segments):
            heading += step
            self._send(f"ROTATE_TO:{int(round(wrap_deg(heading) * 100))}")
            # The `stage` telemetry is a round-robin variable (~95 ms refresh),
            # so immediately after ROTATE_TO it still reads DONE from the
            # PREVIOUS segment. Without this settle delay `_wait_for(DONE)`
            # returns immediately and the next ROTATE_TO is sent while the
            # robot is still turning, so every revolution stops short
            # (~90-180 deg instead of 360 deg).
            if not self._sleep(0.20):
                return False
            if not self._wait_for(
                    lambda: self._stage() == STAGE_DONE,
                    timeout=STEP_SETTLE_TIMEOUT_S):
                return False
            if not self._sleep(0.05):
                return False
        return True

    def _measure_round_trip(self):
        """One 360 deg CW revolution then one 360 deg CCW revolution.

        Returns (odom_l_m, odom_b_m) or None if a leg did not settle or did
        not actually turn a full revolution. Always parks the robot on exit so
        a half-finished revolution cannot feed a bad snapshot to the next one.
        """
        try:
            e3_0, e4_0, t0, x0, y0 = self._snapshot()

            if not self._spin_revolution(-1.0, t0):
                self.note.emit("    CW revolution did not settle - discarded")
                return None
            e3_1, e4_1, t1, x1, y1 = self._snapshot()
            dth_cw = t1 - t0

            if not self._spin_revolution(+1.0, t1):
                self.note.emit("    CCW revolution did not settle - discarded")
                return None
            e3_2, e4_2, t2, x2, y2 = self._snapshot()
            dth_ccw = t2 - t1

            denc3_cw = wrap_encoder_delta(e3_1 - e3_0)
            denc4_cw = wrap_encoder_delta(e4_1 - e4_0)
            denc3_ccw = wrap_encoder_delta(e3_2 - e3_1)
            denc4_ccw = wrap_encoder_delta(e4_2 - e4_1)

            self.note.emit(
                f"    CW  dth {dth_cw:+7.1f} deg  dEnc3 {denc3_cw:+6d}  "
                f"dEnc4 {denc4_cw:+6d}  pose {math.hypot(x1 - x0, y1 - y0):5.1f} mm")
            self.note.emit(
                f"    CCW dth {dth_ccw:+7.1f} deg  dEnc3 {denc3_ccw:+6d}  "
                f"dEnc4 {denc4_ccw:+6d}  pose {math.hypot(x2 - x1, y2 - y1):5.1f} mm")

            return odom_lever_arms_round_trip(
                denc3_cw, denc4_cw, dth_cw,
                denc3_ccw, denc4_ccw, dth_ccw)
        finally:
            try:
                self._send("PATH:STOP")
            except Exception:                        # noqa: BLE001
                pass
            self._sleep(0.10)

    def _run(self):
        self._claim_motion()                          # gate may still be off
        self._send("SET:rot_lock=0")                  # pure PID on the step
        self._sleep(0.20)

        l_m, b_m = self._odom()
        self.status.emit(
            f"Calibrating odometry (odom_l={l_m * 1000:+.0f} mm, "
            f"odom_b={b_m * 1000:+.0f} mm)")

        estimates = []
        valid = 0

        for it in range(1, self.max_iters + 1):
            if self._abort:
                self.done.emit(False, "aborted")
                return

            self.status.emit(
                f"Revolution {it}/{self.max_iters}: 360 deg CW, then 360 deg CCW")
            sample = self._measure_round_trip()
            if sample is None:
                continue
            l_est, b_est = sample
            if l_est is None:
                self.note.emit("    heading change too small - discarded")
                continue

            estimates.append((l_est, b_est))
            valid += 1

            # Running average of the per-revolution solves, clamped to the
            # physical mounting range.
            l_avg = sum(e[0] for e in estimates) / len(estimates)
            b_avg = sum(e[1] for e in estimates) / len(estimates)
            l_m, b_m = clamp_odom(l_avg, b_avg)

            spread_l = ((max(e[0] for e in estimates)
                         - min(e[0] for e in estimates)) * 1000.0)
            spread_b = ((max(e[1] for e in estimates)
                         - min(e[1] for e in estimates)) * 1000.0)
            self.note.emit(
                f"  rev {it}: odom_l={l_est * 1000:+.1f} mm, "
                f"odom_b={b_est * 1000:+.1f} mm -> avg "
                f"odom_l={l_m * 1000:+.1f} mm, odom_b={b_m * 1000:+.1f} mm "
                f"(spread {spread_l:.1f} / {spread_b:.1f} mm)")

            self._send(f"SET:odom_l={int(round(l_m * 1000))}")
            self._sleep(0.05)
            self._send(f"SET:odom_b={int(round(b_m * 1000))}")
            self._sleep(0.20)

        self._send("SET:rot_lock=1")                  # restore the safety lock
        self._send("PATH:STOP")
        self.values.emit(l_m, b_m)

        if valid == 0:
            self.done.emit(
                False,
                "no valid revolution (robot did not settle) - odometry unchanged")
        else:
            self.done.emit(
                True,
                f"odometry calibrated over {valid}/{self.max_iters} "
                f"revolution(s): odom_l={l_m * 1000:+.1f} mm, "
                f"odom_b={b_m * 1000:+.1f} mm")


# ============================================================================
# Panel
# ============================================================================

class PIDTunerPanel(QWidget):
    """PID Tuner tab.

    Usage from main_pyside6.py:
        self.pid_tuner = PIDTunerPanel(bridge=self.bridge,
                                       event_log=self.event_log)
        tabs.addTab(self.pid_tuner, "PID Tuner")
        # from update_gui():
        self.pid_tuner.set_link_state(bridge.connected, bridge.stm32_alive)
    """

    def __init__(self, bridge=None, event_log=None, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.event_log = event_log
        self.worker = None
        self._link_ok = False
        self._running = False
        self._remembered = {}         # target key -> last edited gains

        self.setup_ui()
        self._on_target_changed()

        self.graph_timer = QTimer(self)
        self.graph_timer.timeout.connect(self._update_plot)
        self.graph_timer.start(GRAPH_UPDATE_MS)

    # ---- plumbing ----
    def set_bridge(self, bridge):
        self.bridge = bridge

    def _log(self, message, level="INFO"):
        if self.event_log is not None:
            try:
                self.event_log.add_event(message, level)
            except Exception:                          # noqa: BLE001
                pass

    @property
    def target(self):
        key = self.target_combo.currentData() or "rotation"
        return target_for(key)

    # ========================================================================
    # UI
    # ========================================================================
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # ---- gains ----
        gains_box = QGroupBox("Gains")
        gains_layout = QHBoxLayout(gains_box)

        gains_layout.addWidget(QLabel("Target:"))
        self.target_combo = QComboBox()
        for key in ("rotation", "heading", "position"):
            self.target_combo.addItem(TARGETS[key].label, key)
        self.target_combo.setMinimumWidth(140)
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        gains_layout.addWidget(self.target_combo)

        self.spin_p = self._make_spin()
        self.spin_i = self._make_spin()
        self.spin_d = self._make_spin()
        for spin in (self.spin_p, self.spin_i, self.spin_d):
            spin.valueChanged.connect(self._on_spin_edited)
        self.lbl_p = QLabel("P:")
        self.lbl_i = QLabel("I:")
        self.lbl_d = QLabel("D:")
        gains_layout.addWidget(self.lbl_p)
        gains_layout.addWidget(self.spin_p)
        gains_layout.addWidget(self.lbl_i)
        gains_layout.addWidget(self.spin_i)
        gains_layout.addWidget(self.lbl_d)
        gains_layout.addWidget(self.spin_d)

        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setStyleSheet("background-color: #007acc; color: white;")
        self.apply_btn.clicked.connect(self._on_apply)
        gains_layout.addWidget(self.apply_btn)

        self.read_btn = QPushButton("Read")
        self.read_btn.setToolTip("Load the gains the device currently reports")
        self.read_btn.clicked.connect(self._on_read)
        gains_layout.addWidget(self.read_btn)

        gains_layout.addStretch()
        layout.addWidget(gains_box)

        # ---- tests ----
        test_box = QGroupBox("Tests")
        test_layout = QHBoxLayout(test_box)

        self.step_btn = QPushButton("\u00b190\u00b0 Step")
        self.step_btn.setToolTip("Rotation only: +90 and -90 deg step response")
        self.step_btn.clicked.connect(self._on_step)
        test_layout.addWidget(self.step_btn)

        self.straight_btn = QPushButton("Straight 60cm")
        self.straight_btn.clicked.connect(lambda: self._on_path_test("straight"))
        test_layout.addWidget(self.straight_btn)

        self.zigzag_btn = QPushButton("Zig-Zag")
        self.zigzag_btn.clicked.connect(lambda: self._on_path_test("zigzag"))
        test_layout.addWidget(self.zigzag_btn)

        self.curve_btn = QPushButton("Curve")
        self.curve_btn.clicked.connect(lambda: self._on_path_test("curve"))
        test_layout.addWidget(self.curve_btn)

        test_layout.addSpacing(15)

        self.odom_btn = QPushButton("Calibrate Odom")
        self.odom_btn.setToolTip(
            "Spin +90 deg and measure position drift to solve odom_l / odom_b "
            "(dead-wheel lever arms). Robot must be on the floor, free to turn.")
        self.odom_btn.setStyleSheet(
            "background-color: #1e8449; color: white; font-weight: bold;")
        self.odom_btn.clicked.connect(self._on_calibrate_odom)
        test_layout.addWidget(self.odom_btn)

        test_layout.addSpacing(15)

        self.auto_btn = QPushButton("\u2699 Auto-Tune")
        self.auto_btn.setStyleSheet(
            "background-color: #8e44ad; color: white; font-weight: bold;")
        self.auto_btn.clicked.connect(self._on_auto_tune)
        test_layout.addWidget(self.auto_btn)

        test_layout.addWidget(QLabel("Max iters:"))
        self.iter_spin = QSpinBox()
        self.iter_spin.setRange(1, 15)
        self.iter_spin.setValue(6)
        self.iter_spin.setMaximumWidth(60)
        test_layout.addWidget(self.iter_spin)

        self.stop_btn = QPushButton("\u25a0 STOP")
        self.stop_btn.setStyleSheet(
            "background-color: #c0392b; color: white; font-weight: bold;")
        self.stop_btn.clicked.connect(self._on_stop)
        test_layout.addWidget(self.stop_btn)

        test_layout.addStretch()
        layout.addWidget(test_box)

        # ---- status ----
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #a0a0a0; padding: 2px;")
        layout.addWidget(self.status_label)

        # ---- graph ----
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#252526")
        self.plot.setLabel("left", "PWM")
        self.plot.setLabel("bottom", "Time (s)")
        self.plot.getAxis("left").setPen(pg.mkPen("#d4d4d4"))
        self.plot.getAxis("bottom").setPen(pg.mkPen("#d4d4d4"))
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.addLegend(offset=(10, 10))
        self.plot.setMinimumHeight(220)

        self.curves = {}
        for label in ("P", "I", "D", "Total"):
            self.curves[label] = self.plot.plot(
                pen=pg.mkPen(CURVE_COLOURS[label], width=2), name=label)

        layout.addWidget(self.plot, 1)

        # ---- metrics / report ----
        self.metrics_box = QPlainTextEdit()
        self.metrics_box.setReadOnly(True)
        self.metrics_box.setMaximumBlockCount(400)
        self.metrics_box.setFixedHeight(120)
        self.metrics_box.setStyleSheet(
            "QPlainTextEdit { background-color: #252526; color: #d4d4d4; "
            "font-family: Consolas, Courier; font-size: 9pt; }")
        self.metrics_box.setPlainText(
            "Pick a target, then run a test.\n"
            "  Rotation PID  -> \u00b190\u00b0 Step (or Auto-Tune)\n"
            "  Heading PI    -> Straight 60cm (or Auto-Tune)\n"
            "  Position PD   -> Zig-Zag / Curve (or Auto-Tune)")
        layout.addWidget(self.metrics_box)

        self._update_buttons()

    @staticmethod
    def _make_spin():
        spin = QDoubleSpinBox()
        spin.setDecimals(2)
        spin.setSingleStep(0.5)
        spin.setMinimumWidth(90)
        spin.setKeyboardTracking(False)
        return spin

    def _on_spin_edited(self, _value):
        """Remember edits immediately, so switching target and back does not
        silently discard a value the user typed but hasn't applied yet."""
        self._remembered[self.target.key] = self._spins_to_gains()

    # ========================================================================
    # Target / gain handling
    # ========================================================================
    def _on_target_changed(self):
        target = self.target

        # Reload the remembered values for the newly selected loop.
        gains = self._remembered.get(target.key) or self._read_gains(target)
        self._set_spins(target, gains)
        self._remembered[target.key] = dict(gains)

        self.spin_i.setVisible(target.has_i)
        self.lbl_i.setVisible(target.has_i)
        self.spin_d.setVisible(target.has_d)
        self.lbl_d.setVisible(target.has_d)

        self.step_btn.setEnabled(target.test == "step" and self._can_run())
        self._update_buttons()

        self.plot.setTitle(f"PID terms - {target.label}")
        self._update_plot()

    def _limits_for(self, target):
        return {
            "p": target.limits[target.kp],
            "i": target.limits[target.ki] if target.ki else (0.0, 100.0),
            "d": target.limits[target.kd] if target.kd else (0.0, 100.0),
        }

    def _set_spins(self, target, gains):
        limits = self._limits_for(target)
        self.spin_p.setRange(*limits["p"])
        self.spin_i.setRange(*limits["i"])
        self.spin_d.setRange(*limits["d"])
        self.spin_p.setValue(gains.get(target.kp, limits["p"][0]))
        if target.ki:
            self.spin_i.setValue(gains.get(target.ki, limits["i"][0]))
        if target.kd:
            self.spin_d.setValue(gains.get(target.kd, limits["d"][0]))

    def _spins_to_gains(self):
        target = self.target
        gains = {}
        if target.kp:
            gains[target.kp] = self.spin_p.value()
        if target.ki:
            gains[target.ki] = self.spin_i.value()
        if target.kd:
            gains[target.kd] = self.spin_d.value()
        return target.clamp(gains)

    def _read_gains(self, target):
        """Current device values, falling back to the module defaults.

        Device values arrive x100 on the wire (SET:kp_hdg=6300 -> 63.00); the
        defaults are already real units, so only the device path is divided.
        """
        gains = {}
        for name in target.gain_names:
            value = _var(name)
            if value is None:
                gains[name] = round(float(MODULE_DEFAULTS.get(
                    name, target.limits[name][0])), 2)
            else:
                gains[name] = round(float(value) / 100.0, 2)   # wire is x100
        return gains

    def _on_read(self):
        target = self.target
        gains = self._read_gains(target)
        self._set_spins(target, gains)
        self._remembered[target.key] = dict(gains)
        self.status_label.setText(
            "Read from device: " + ", ".join(f"{k}={v:g}" for k, v in gains.items()))

    def _on_apply(self):
        if not self._can_run():
            self._warn_not_ready()
            return
        target = self.target
        gains = self._spins_to_gains()
        for name in target.gain_names:
            self.bridge.send_command(f"SET:{name}={to_wire(gains[name])}")
        self._remembered[target.key] = dict(gains)
        self.status_label.setText(
            "Applied: " + ", ".join(f"{k}={v:g}" for k, v in gains.items()))
        self._log(f"PID tuner: applied {target.label} "
                  + ", ".join(f"{k}={v:g}" for k, v in gains.items()), "INFO")

    # ========================================================================
    # Test runners
    # ========================================================================
    def _current_corrected_cm(self):
        x_mm = _var("x_mm")
        y_mm = _var("y_mm")
        if x_mm is None or y_mm is None:
            return None
        return pose_to_corrected_cm(x_mm, y_mm)

    def _on_step(self):
        self._start(target=ROTATION, path_points=None)

    def _on_path_test(self, kind):
        start = self._current_corrected_cm()
        if start is None:
            QMessageBox.warning(
                self, "No pose",
                "Robot pose unknown (no x_mm / y_mm). Is the debug link up?")
            return

        if kind == "straight":
            points = straight_path(start, 60.0, legs=2)
        elif kind == "zigzag":
            points = zigzag_path(start, forward_cm=60.0, amplitude_cm=12.0, legs=3)
        else:
            points = curve_path(start, radius_cm=30.0, sweep_deg=180.0)

        self._start(target=self.target, path_points=points)

    def _on_auto_tune(self):
        target = self.target
        if target.test == "step":
            path_points = None
        else:
            start = self._current_corrected_cm()
            if start is None:
                QMessageBox.warning(
                    self, "No pose",
                    "Robot pose unknown (no x_mm / y_mm). Is the debug link up?")
                return
            if target.key == "position":
                # Sharp corners are where damping shows up.
                path_points = zigzag_path(start, forward_cm=60.0,
                                          amplitude_cm=12.0, legs=3)
            else:
                path_points = straight_path(start, 60.0, legs=2)

        self._start(target=target, path_points=path_points,
                    auto_tune=True, max_iters=self.iter_spin.value())

    def _on_calibrate_odom(self):
        if not self._can_run():
            self._warn_not_ready()
            return
        if self._running:
            return

        # Honour the panel's "Max iters" value: each iteration is one full
        # 360 deg CW + 360 deg CCW revolution pair.
        self.worker = OdomCalibrateWorker(
            self.bridge, max_iters=self.iter_spin.value(), parent=self)
        self.worker.status.connect(self.status_label.setText)
        self.worker.note.connect(self.metrics_box.appendPlainText)
        self.worker.values.connect(self._on_odom_values)
        self.worker.done.connect(self._on_worker_done)

        self._running = True
        self._update_buttons()
        self.metrics_box.clear()
        self.metrics_box.appendPlainText("Odometry lever-arm calibration")
        self.status_label.setText("Calibrating odometry...")
        self._log("PID tuner: odometry calibration started", "INFO")
        self.worker.start()

    def _on_odom_values(self, l_m, b_m):
        self.metrics_box.appendPlainText(
            f"Final: odom_l = {l_m * 1000:+.0f} mm   odom_b = {b_m * 1000:+.0f} mm")
        self.metrics_box.appendPlainText(
            "Copy these into robot_params.h (ODOM_L / ODOM_B) so they persist "
            "after a reset, or re-SET:odom_l / SET:odom_b at boot.")

    def _start(self, target, path_points=None, auto_tune=False, max_iters=1):
        if not self._can_run():
            self._warn_not_ready()
            return
        if self._running:
            return

        # Make sure the selected loop is the one being measured.
        self.target_combo.setCurrentIndex(
            list(TARGETS.keys()).index(target.key))

        gains = self._spins_to_gains()
        self._remembered[target.key] = dict(gains)

        self.worker = TunerWorker(
            self.bridge, target, gains, path_points,
            max_iters=max_iters, auto_tune=auto_tune, parent=self)
        self.worker.status.connect(self.status_label.setText)
        self.worker.note.connect(self.metrics_box.appendPlainText)
        self.worker.gains.connect(self._on_gains_applied)
        self.worker.result.connect(self._on_result)
        self.worker.done.connect(self._on_worker_done)

        self._running = True
        self._update_buttons()
        self.metrics_box.clear()
        mode = "Auto-Tune" if auto_tune else "Test"
        self.status_label.setText(f"{mode}: {target.label}")
        self._log(f"PID tuner: {mode} started on {target.label}", "INFO")
        self.worker.start()

    def _on_stop(self):
        self._abort_worker()
        try:
            if self.bridge and self.bridge.connected:
                self.bridge.send_command("PATH:STOP")
                self.bridge.send_command("SET:rot_lock=1")
        except Exception:                              # noqa: BLE001
            pass
        self.status_label.setText("STOP sent")
        self._log("PID tuner: STOP pressed", "WARNING")

    def stop_all(self):
        """Abort any run and park the robot (used before flashing)."""
        self._abort_worker()
        try:
            if self.bridge and self.bridge.connected:
                self.bridge.send_command("PATH:STOP")
        except Exception:                              # noqa: BLE001
            pass

    def _abort_worker(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_abort()
            # The worker's waits are 50 ms slices, so this returns quickly;
            # without it a fast re-start could interleave with the tail of the
            # aborted run (e.g. its rot_lock restore landing after the new
            # run's rot_lock=0).
            self.worker.wait(600)
        self._running = False
        self._update_buttons()

    # ---- worker callbacks (GUI thread) ----
    def _on_gains_applied(self, gains):
        target = self.target
        self._remembered[target.key] = dict(gains)
        limits = self._limits_for(target)
        # Block signals so applying a value doesn't look like a user edit.
        for spin, name, key in ((self.spin_p, target.kp, "p"),
                                (self.spin_i, target.ki, "i"),
                                (self.spin_d, target.kd, "d")):
            if not name:
                continue
            spin.blockSignals(True)
            spin.setValue(gains.get(name, limits[key][0]))
            spin.blockSignals(False)

    def _on_result(self, payload):
        metrics = payload.get("metrics")
        self.metrics_box.appendPlainText("")
        for line in describe(self.target, metrics):
            self.metrics_box.appendPlainText(line)
        if payload.get("converged"):
            self.metrics_box.appendPlainText(
                "\u2713 within tolerance - keeping these gains")
        elif payload.get("reasons"):
            self.metrics_box.appendPlainText("Suggested next step:")
            for reason in payload["reasons"]:
                self.metrics_box.appendPlainText(f"  \u00b7 {reason}")
            suggested = payload.get("suggested")
            if suggested:
                # Load them into the boxes but do NOT send: the user presses
                # Apply (or re-runs the test) when they're happy with them.
                self.metrics_box.appendPlainText(
                    "  -> set to " + ", ".join(
                        f"{k}={v:g}" for k, v in suggested.items())
                    + " (press Apply to use)")
                self._set_spins(self.target, suggested)
        scrollbar = self.metrics_box.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_worker_done(self, success, message):
        # Ignore a late `done` from a run the user already stopped: without
        # this guard its queued signal would clear _running and the status of
        # whatever run is active now.
        worker = self.sender()
        if worker is not self.worker:
            return
        self.worker = None
        self._running = False
        self._update_buttons()
        if worker is not None:
            worker.wait(1500)

        self.status_label.setText(("\u2713 " if success else "\u2717 ") + message)
        self.metrics_box.appendPlainText("")
        self.metrics_box.appendPlainText(f"== {message} ==")
        self._log(f"PID tuner: run finished - {message}",
                  "SUCCESS" if success else "WARNING")

    # ========================================================================
    # Enablement
    # ========================================================================
    def _can_run(self):
        return bool(self._link_ok and not self._running)

    def _warn_not_ready(self):
        if self._running:
            self.status_label.setText("A test is already running - press STOP first")
        else:
            self.status_label.setText(
                "Not ready: connect to the ESP32 and wait for STM32 alive")

    def _update_buttons(self):
        can = self._can_run()
        target = self.target

        self.step_btn.setEnabled(can and target.test == "step")
        for btn in (self.straight_btn, self.zigzag_btn, self.curve_btn):
            btn.setEnabled(can and target.test == "path")
        self.auto_btn.setEnabled(can)
        self.odom_btn.setEnabled(can)
        self.apply_btn.setEnabled(self._link_ok)
        self.read_btn.setEnabled(self._link_ok)
        self.stop_btn.setEnabled(True)

    def set_link_state(self, connected, stm32_alive):
        """Called from the main window's update_gui() every 100 ms."""
        ok = bool(connected and stm32_alive)
        if ok != self._link_ok:
            self._link_ok = ok
            self._update_buttons()
            if not ok and not self._running:
                self.status_label.setText("Waiting for STM32 (link not ready)")

    # ========================================================================
    # Live graph
    # ========================================================================
    def _curve_series(self, label, target):
        """(times, values) for one plotted line, windowed to GRAPH_WINDOW_S."""
        now = time.time()
        start = now - GRAPH_WINDOW_S

        name = {
            "P": target.p_var,
            "I": target.i_var,
            "D": target.d_var,
            "Total": target.total_var,
        }[label]

        if name is None and label == "Total":
            # Position PD has no single total variable: the controller command
            # is the magnitude of the (fwd, str) output pair.
            fwd = [(t, v) for t, v in _history("fwd_out") if t >= start]
            strafe = [(t, v) for t, v in _history("str_out") if t >= start]
            n = min(len(fwd), len(strafe))
            if n == 0:
                return [], []
            fwd, strafe = fwd[-n:], strafe[-n:]
            return ([t - start for t, _ in fwd],
                    [math.hypot(a[1], b[1]) for a, b in zip(fwd, strafe)])

        if name is None:
            return [], []

        data = [(t, v) for t, v in _history(name) if t >= start]
        return [t - start for t, _ in data], [v for _, v in data]

    def _update_plot(self):
        if not self.isVisible():
            return
        target = self.target
        for label, curve in self.curves.items():
            available = {
                "P": target.p_var,
                "I": target.i_var,
                "D": target.d_var,
                "Total": target.total_var,
            }[label]
            if available is None and label != "Total":
                curve.setVisible(False)
                continue
            curve.setVisible(True)
            xs, ys = self._curve_series(label, target)
            if xs:
                curve.setData(xs, ys)
            else:
                curve.clear()
