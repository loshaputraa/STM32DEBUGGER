# ============================================================================
# PID Tuner - targets, test paths, step/path metrics, tuning heuristics
# ============================================================================
# Pure logic for the PID Tuner tab: no Qt, no sockets, no I/O. The GUI panel
# (gui/PIDTunerPanel.py) drives the robot and feeds the samples collected
# here; the Auto-Tune worker calls suggest_gains() / within_tolerance() to
# decide what to try next.
#
# ---------------------------------------------------------------------------
# WIRE CONVENTIONS (must match app.c / pathfinder.h)
# ---------------------------------------------------------------------------
#   Gains are fixed-point x100 on the wire:
#       SET:kp_hdg=6300        ->  63.00
#
#   Rotation PID   kp_hdg       / ki_hdg       / kd_hdg       -> pid_w (ROTATE)
#   Heading PI     kp_hdg_move  / ki_hdg_move                 -> HeadingHold
#   Position PD    kp_pos       / kd_pos                      -> MOVE/LATCH/DONE
#
#   PID telemetry, all in PWM-domain units (P/I/D are the individual terms,
#   total is the summed controller output):
#       rot_p, rot_i, rot_d, rot_out   ROTATE stage
#       hdg_p, hdg_i                   heading hold (total = rot_out)
#       pos_p, pos_d                   position PD (total = hypot(fwd_out,str_out))
#       hdg_err_x100                   heading error in (deg * 100)  [0.01 deg]
#
#   Round-robin rate: app.c sends TELEM_VARS_PER_CYCLE=2 of TELEM_COUNT=19
#   every 10 ms, so each variable refreshes every ~95 ms.  Every metric below
#   is therefore quantised by TELEM_PERIOD_S; the tuner compares successive
#   runs against each other, so the absolute resolution matters less than the
#   fact that it is constant.
#
#   POSE FRAME: x_mm / y_mm are the RAW odometry pose. The firmware compares
#   each waypoint against PATHFINDER_X_SIGN * robot.x, so the frame the
#   waypoints live in ("corrected") is WIRE_SIGN * raw.  Same two signs as
#   robot_params.h; if those change, change WIRE_SIGN and nothing else.
# ---------------------------------------------------------------------------

import math
import statistics

# ============================================================================
# Wire constants
# ============================================================================
WIRE_SCALE = 100.0          # gains are sent as value * 100
WIRE_SIGN = -1.0            # PATHFINDER_X_SIGN / PATHFINDER_Y_SIGN
TELEM_PERIOD_S = 0.095      # ~one refresh per variable (19 vars, 2 per cycle)

# app.c PathfinderStage_t
STAGE_IDLE, STAGE_MOVE, STAGE_LATCH, STAGE_ROTATE, STAGE_DONE, STAGE_ERROR = range(6)
STAGE_NAMES = {
    STAGE_IDLE: "IDLE", STAGE_MOVE: "MOVE", STAGE_LATCH: "LATCH",
    STAGE_ROTATE: "ROTATE", STAGE_DONE: "DONE", STAGE_ERROR: "ERROR",
}

# Acceptance bands (auto-tune stops when every metric is inside these).
ROT_OVERSHOOT_MAX_PCT = 10.0
ROT_RISE_MAX_S = 1.00
ROT_SETTLE_MAX_S = 1.60
ROT_SS_ERR_MAX_DEG = 2.0

HDG_MEAN_MAX_DEG = 1.50
HDG_STD_MAX_DEG = 1.00

POS_CTE_MAX_CM = 3.00
POS_CTE_MEAN_MAX_CM = 1.50


def wrap_deg(angle):
    """Wrap an angle in degrees to (-180, 180]."""
    a = float(angle)
    while a > 180.0:
        a -= 360.0
    while a <= -180.0:
        a += 360.0
    return a


def to_wire(value):
    """Real gain value -> integer wire value (x100)."""
    return int(round(float(value) * WIRE_SCALE))


def from_wire(wire):
    """Integer wire value -> real gain value."""
    return float(wire) / WIRE_SCALE


# ============================================================================
# Tuning targets
# ============================================================================

class TuningTarget:
    """One tunable loop: which gains it has, which telemetry it produces and
    which test drives it."""

    def __init__(self, key, label, kp, ki, kd, limits,
                 p_var, i_var, d_var, total_var, test):
        self.key = key
        self.label = label
        self.kp, self.ki, self.kd = kp, ki, kd
        self.limits = limits          # {gain_name: (min, max)} real units
        self.p_var, self.i_var, self.d_var = p_var, i_var, d_var
        self.total_var = total_var
        self.test = test              # "step" | "path"

    @property
    def gain_names(self):
        return [g for g in (self.kp, self.ki, self.kd) if g]

    @property
    def has_i(self):
        return self.ki is not None

    @property
    def has_d(self):
        return self.kd is not None

    def clamp(self, gains):
        """Clamp a gain dict to this target's wire limits. Missing gains fall
        back to their lower limit; unknown keys are dropped."""
        out = {}
        for name in self.gain_names:
            lo, hi = self.limits[name]
            value = gains.get(name, lo)
            out[name] = round(min(max(float(value), lo), hi), 2)
        return out

    def __repr__(self):
        return f"TuningTarget({self.key!r})"


ROTATION = TuningTarget(
    key="rotation",
    label="Rotation PID",
    kp="kp_hdg", ki="ki_hdg", kd="kd_hdg",
    limits={"kp_hdg": (0.0, 300.0), "ki_hdg": (0.0, 100.0), "kd_hdg": (0.0, 100.0)},
    p_var="rot_p", i_var="rot_i", d_var="rot_d", total_var="rot_out",
    test="step",
)

HEADING = TuningTarget(
    key="heading",
    label="Heading PI",
    kp="kp_hdg_move", ki="ki_hdg_move", kd=None,
    limits={"kp_hdg_move": (0.0, 300.0), "ki_hdg_move": (0.01, 300.0)},
    p_var="hdg_p", i_var="hdg_i", d_var=None, total_var="rot_out",
    test="path",
)

POSITION = TuningTarget(
    key="position",
    label="Position PD",
    kp="kp_pos", ki=None, kd="kd_pos",
    limits={"kp_pos": (0.0, 100.0), "kd_pos": (0.0, 50.0)},
    p_var="pos_p", i_var=None, d_var="pos_d", total_var=None,  # hypot(fwd,str)
    test="path",
)

TARGETS = {t.key: t for t in (ROTATION, HEADING, POSITION)}


def target_for(key):
    return TARGETS[key]


# ============================================================================
# Test path generators (all in the CORRECTED frame, centimetres)
# ============================================================================
# The first point is the robot's current position (the uploader re-snaps it
# anyway), so every path starts from wherever the robot stands. The last point
# is the only one that uses the full MOVE->LATCH->ROTATE->DONE stop; points in
# between are driven through at cruise PWM (pathfinder_continuous = 1, set by
# PATH:GO).

def straight_path(start_cm, length_cm=60.0, legs=2):
    """Straight line along +Y (the firmware's 'forward' at heading 0),
    sampled into `legs` segments between the start and the end."""
    x0, y0 = start_cm
    legs = max(1, int(legs))
    return [(x0, y0)] + [
        (x0, y0 + length_cm * i / legs) for i in range(1, legs + 1)
    ]


def zigzag_path(start_cm, forward_cm=60.0, amplitude_cm=12.0, legs=3):
    """Alternating left/right offsets while advancing `forward_cm` total.
    Sharp direction changes stress the position loop's damping - the corner
    CTE is exactly what the Position-PD tuner is scored on."""
    x0, y0 = start_cm
    legs = max(2, int(legs))
    pts = [(x0, y0)]
    for i in range(1, legs + 1):
        sign = 1.0 if i % 2 else -1.0
        pts.append((x0 + sign * amplitude_cm, y0 + forward_cm * i / legs))
    pts.append((x0, y0 + forward_cm))     # finish on the centre line
    return pts


def curve_path(start_cm, radius_cm=30.0, sweep_deg=180.0, step_deg=15.0):
    """Circular arc of `radius_cm` sweeping `sweep_deg`, starting at the
    current position. Constant curvature shows lateral lag (a steady CTE)
    rather than the transient corner error of the zig-zag."""
    x0, y0 = start_cm
    step_deg = max(5.0, float(step_deg))
    n = max(2, int(round(abs(sweep_deg) / step_deg)))
    sweep = math.radians(sweep_deg)
    # Circle centre 90 deg to the left of the start heading (+Y travel):
    # the start point sits on the circle at angle 0 from the centre.
    cx, cy = x0 - radius_cm, y0
    pts = []
    for i in range(n + 1):
        a = sweep * i / n
        pts.append((cx + radius_cm * math.cos(a), cy + radius_cm * math.sin(a)))
    pts[0] = (x0, y0)                      # exact start
    return pts


def path_length_cm(points):
    return sum(math.hypot(points[i][0] - points[i - 1][0],
                          points[i][1] - points[i - 1][1])
               for i in range(1, len(points)))


# ============================================================================
# Metrics
# ============================================================================

def _crossing(ts, ps, level):
    """First time the (non-decreasing) sequence ps crosses `level`, linearly
    interpolated. None if it never does."""
    for i in range(1, len(ps)):
        if ps[i - 1] < level <= ps[i]:
            span = ps[i] - ps[i - 1]
            if span <= 0.0:
                return ts[i]
            return ts[i - 1] + (level - ps[i - 1]) / span * (ts[i] - ts[i - 1])
    return None


def step_metrics(samples, expected_step_deg):
    """Metrics for one step response.

    samples: [(t_s, heading_error_deg), ...] in time order, starting at (or
    just after) the ROTATE_TO command. heading_error_deg is the firmware's
    `hdg_err_x100 / 100` = target - actual.

    expected_step_deg: the commanded step (e.g. +90). The response is
    normalised as p(t) = (e0 - e(t)) / e0, which is 0 before the step and 1 at
    the target, for positive AND negative steps.

    Returns None when the data cannot support the analysis.
    """
    if not samples or len(samples) < 3:
        return None

    e0 = float(expected_step_deg)
    if abs(e0) < 1.0:
        return None

    ts = [float(t) for t, _ in samples]
    es = [float(e) for _, e in samples]
    t_span = ts[-1] - ts[0]
    if t_span < 0.20:
        return None

    ps = [(e0 - e) / e0 for e in es]
    peak = max(ps)
    overshoot_pct = max(0.0, (peak - 1.0) * 100.0)

    p10 = _crossing(ts, ps, 0.10)
    p90 = _crossing(ts, ps, 0.90)
    rise_s = (p90 - p10) if (p10 is not None and p90 is not None) else None

    # Settling: find the last sample outside a +-2% band around the final
    # value; settling ends at the sample after it.
    band = 0.02
    settle_s = None
    last_out = None
    for i, p in enumerate(ps):
        if abs(1.0 - p) > band:
            last_out = i
    if last_out is None:
        settle_s = 0.0
    elif last_out + 1 < len(ps):
        settle_s = ts[last_out + 1] - ts[0]

    return {
        "kind": "step",
        "step_deg": e0,
        "overshoot_pct": round(overshoot_pct, 1),
        "rise_s": None if rise_s is None else round(rise_s, 2),
        "settle_s": None if settle_s is None else round(settle_s, 2),
        "ss_err_deg": round(abs(es[-1]), 2),
        "n_samples": len(samples),
        "duration_s": round(t_span, 2),
    }


def heading_metrics(samples):
    """Steady-state heading behaviour during a path run.

    samples: [(t_s, heading_error_deg), ...]

    mean   -> sustained drift (a left/right motor mismatch the I term must
              learn)
    stddev -> oscillation / hunting (too much P, or I windup)
    """
    if not samples or len(samples) < 3:
        return None
    es = [float(e) for _, e in samples]
    mean = sum(es) / len(es)
    std = statistics.pstdev(es) if len(es) > 1 else 0.0
    return {
        "kind": "heading",
        "err_mean_deg": round(mean, 2),
        "err_std_deg": round(std, 2),
        "err_max_deg": round(max(abs(e) for e in es), 2),
        "n_samples": len(es),
    }


def _point_segment_distance(p, a, b):
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / denom
    t = min(1.0, max(0.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def cte_metrics(actual_cm, waypoints_cm, skip_start_frac=0.15):
    """Cross-track error of the actual path against the intended polyline.

    actual_cm:    [(x_cm, y_cm), ...] in the corrected frame
    waypoints_cm: [(x_cm, y_cm), ...] the uploaded path

    The first `skip_start_frac` of the samples is ignored: the robot always
    starts at the first waypoint and does its initial LATCH/ROTATE there, so
    including it would reward doing nothing.
    """
    if len(waypoints_cm) < 2 or len(actual_cm) < 5:
        return None

    start = int(len(actual_cm) * max(0.0, min(skip_start_frac, 0.5)))
    pts = actual_cm[start:]
    if len(pts) < 3:
        return None

    errs = []
    for p in pts:
        best = min(_point_segment_distance(p, waypoints_cm[i - 1], waypoints_cm[i])
                   for i in range(1, len(waypoints_cm)))
        errs.append(best)

    return {
        "kind": "cte",
        "cte_mean_cm": round(sum(errs) / len(errs), 2),
        "cte_max_cm": round(max(errs), 2),
        "cte_rms_cm": round(math.sqrt(sum(e * e for e in errs) / len(errs)), 2),
        "n_samples": len(errs),
    }


def pose_to_corrected_cm(x_mm, y_mm):
    """Raw pose telemetry -> corrected-frame cm (the waypoint frame)."""
    return (WIRE_SIGN * float(x_mm) / 10.0,
            WIRE_SIGN * float(y_mm) / 10.0)


# ============================================================================
# Odometry lever-arm calibration
# ============================================================================
# odometry.c integrates, per cycle:
#       dx_local = S1 + odom_l * dtheta
#       dy_local = S3 - odom_b * dtheta
# For a PURE spin the dead wheels roll by exactly lever_arm * dtheta, so with
# correct odom_l / odom_b the local displacement is zero. The exact solve is
# therefore a one-shot algebra over the raw encoder counts, NOT a world-pose
# drift estimate:
#
#       odom_l = -S1 / dtheta          (cancel the X-wheel lever arm)
#       odom_b = +S3 / dtheta          (cancel the Y-wheel lever arm)
#
# with S = 2*pi*R * denc / N. This is immune to the robot's world position,
# to odometry drift accumulated so far, and to the pose -> local rotation
# approximation that made the old iteration blow up when a spin did not settle.

ODOM_R_DEFAULT = 0.030      # m, mirrors robot_params.h ODOM_R
ODOM_N_DEFAULT = 8600.0     # counts/rev, mirrors robot_params.h ODOM_N

# Physical mounting ranges the user measured on the robot.
ODOM_L_MIN_M, ODOM_L_MAX_M = -0.09, 0.09
ODOM_B_MIN_M, ODOM_B_MAX_M = -0.01, 0.01


def wrap_encoder_delta(delta):
    """Sign-extend a 16-bit encoder counter delta (same rule as odometry.c)."""
    delta = int(delta)
    if delta > 32768:
        delta -= 65536
    if delta < -32768:
        delta += 65536
    return delta


def odom_lever_arms_from_spin(denc_x, denc_y, dtheta_deg,
                              odom_r=ODOM_R_DEFAULT, odom_n=ODOM_N_DEFAULT):
    """Solve odom_l / odom_b (m) from one pure-spin measurement.

    denc_x  : encoder3 counter delta over the spin  (X / S1 wheel)
    denc_y  : encoder4 counter delta over the spin  (Y / S3 wheel)
    dtheta_deg : heading change over the spin (degrees)

    Returns (odom_l_m, odom_b_m), or (None, None) if the spin was too small to
    resolve a lever arm (|dtheta| < ~20 deg).
    """
    dtheta = math.radians(float(dtheta_deg))
    if abs(dtheta) < 0.35:                       # ~20 deg
        return None, None

    circ = 2.0 * math.pi * float(odom_r)
    S1 = circ * float(denc_x) / float(odom_n)    # m
    S3 = circ * float(denc_y) / float(odom_n)    # m

    odom_l = -S1 / dtheta
    odom_b = +S3 / dtheta
    return odom_l, odom_b


def wheel_travel_m(delta_counts, odom_r=ODOM_R_DEFAULT, odom_n=ODOM_N_DEFAULT):
    """Encoder count delta -> dead-wheel linear travel (m)."""
    return (2.0 * math.pi * float(odom_r) * float(delta_counts)
            / float(odom_n))


def odom_lever_arms_round_trip(denc_x_cw, denc_y_cw, dtheta_cw_deg,
                               denc_x_ccw, denc_y_ccw, dtheta_ccw_deg,
                               odom_r=ODOM_R_DEFAULT, odom_n=ODOM_N_DEFAULT):
    """Solve odom_l / odom_b from a full CW + CCW revolution pair.

    Each leg is solved on its own (odom_l = -S1/dtheta, odom_b = +S3/dtheta)
    and the two results are averaged. Averaging a +theta and -theta leg
    cancels a constant encoder offset and any common-mode pose bias, which is
    why a full 360 deg round trip is far less noisy than a single 90 deg
    sample. The larger travel (4x the 90 deg signal) also lifts the SNR.

    Returns (odom_l_m, odom_b_m), or (None, None) if a leg rotated too little.
    """
    dth_cw = math.radians(float(dtheta_cw_deg))
    dth_ccw = math.radians(float(dtheta_ccw_deg))
    if abs(dth_cw) < 0.35 or abs(dth_ccw) < 0.35:
        return None, None

    s1_cw = wheel_travel_m(denc_x_cw, odom_r, odom_n)
    s3_cw = wheel_travel_m(denc_y_cw, odom_r, odom_n)
    s1_ccw = wheel_travel_m(denc_x_ccw, odom_r, odom_n)
    s3_ccw = wheel_travel_m(denc_y_ccw, odom_r, odom_n)

    odom_l = -0.5 * (s1_cw / dth_cw + s1_ccw / dth_ccw)
    odom_b = +0.5 * (s3_cw / dth_cw + s3_ccw / dth_ccw)
    return odom_l, odom_b


def clamp_odom(odom_l_m, odom_b_m):
    """Clamp lever arms to the physical mounting range."""
    l = min(max(float(odom_l_m), ODOM_L_MIN_M), ODOM_L_MAX_M)
    b = min(max(float(odom_b_m), ODOM_B_MIN_M), ODOM_B_MAX_M)
    return l, b


# ============================================================================
# Heuristics
# ============================================================================
# Deliberately coarse and monotone: one or two multiplicative nudges per
# iteration, always clamped to the firmware's SET: limits. The loop stops on
# band membership; it does not try to solve for an optimum.

def _nudge(value, factor):
    return round(float(value) * factor, 2)


def suggest_gains(target, gains, metrics):
    """Next gain set for `target`, derived from `metrics`.

    Returns (new_gains, reasons) where reasons explains each change (empty
    when nothing needed changing).
    """
    g = target.clamp(dict(gains))
    reasons = []

    if metrics is None:
        return g, ["no usable metrics - re-running the same gains"]

    if target.key == "rotation":
        kp, ki, kd = g["kp_hdg"], g["ki_hdg"], g["kd_hdg"]

        if metrics["overshoot_pct"] > ROT_OVERSHOOT_MAX_PCT:
            kp = _nudge(kp, 0.90)
            kd = _nudge(kd, 1.15)
            reasons.append(
                f"overshoot {metrics['overshoot_pct']:.1f}% > "
                f"{ROT_OVERSHOOT_MAX_PCT:.0f}% -> P x0.90, D x1.15")

        rise = metrics.get("rise_s")
        if rise is None:
            # Never reached 90% of the step inside the capture window: too
            # slow to be useful, treat it as a rise-time failure.
            kp = _nudge(kp, 1.10)
            reasons.append("rise never reached 90% of the step -> P x1.10")
        elif rise > ROT_RISE_MAX_S:
            kp = _nudge(kp, 1.10)
            reasons.append(
                f"rise {rise:.2f}s > {ROT_RISE_MAX_S:.2f}s -> P x1.10")

        settle = metrics.get("settle_s")
        if settle is None:
            kd = _nudge(kd, 1.20)
            reasons.append("never settled inside 2% -> D x1.20")
        elif settle > ROT_SETTLE_MAX_S:
            kd = _nudge(kd, 1.20)
            reasons.append(
                f"settling {settle:.2f}s > {ROT_SETTLE_MAX_S:.2f}s -> D x1.20")

        if metrics["ss_err_deg"] > ROT_SS_ERR_MAX_DEG:
            # I starts at 0 in robot_params.h: bring it up from nothing.
            ki = max(_nudge(ki, 1.5) if ki > 0.0 else 0.5, 0.5)
            reasons.append(
                f"steady-state error {metrics['ss_err_deg']:.2f} deg > "
                f"{ROT_SS_ERR_MAX_DEG:.1f} deg -> I raised to {ki:g}")

        g.update(kp_hdg=kp, ki_hdg=ki, kd_hdg=kd)

    elif target.key == "heading":
        kp, ki = g["kp_hdg_move"], g["ki_hdg_move"]
        mean_err = abs(metrics["err_mean_deg"])
        std_err = metrics["err_std_deg"]

        if std_err > HDG_STD_MAX_DEG:
            # Hunting: back both terms off, I first (the integrator is what
            # keeps an oscillation going once it has wound up).
            ki = _nudge(ki, 0.85)
            kp = _nudge(kp, 0.90)
            reasons.append(
                f"heading oscillation {std_err:.2f} deg > "
                f"{HDG_STD_MAX_DEG:.2f} deg -> P x0.90, I x0.85")

        if mean_err > HDG_MEAN_MAX_DEG:
            ki = _nudge(ki, 1.30)
            reasons.append(
                f"heading drift {mean_err:.2f} deg > {HDG_MEAN_MAX_DEG:.2f} deg "
                f"-> I x1.30")

        g.update(kp_hdg_move=kp, ki_hdg_move=ki)

    elif target.key == "position":
        kp, kd = g["kp_pos"], g["kd_pos"]
        max_cte = metrics["cte_max_cm"]
        mean_cte = metrics["cte_mean_cm"]

        if max_cte > POS_CTE_MAX_CM:
            # Corner error is a damping problem, not a stiffness one: raise D
            # first, and only add P once D is near its limit.
            if kd < 0.9 * target.limits["kd_pos"][1]:
                kd = _nudge(kd, 1.15)
                reasons.append(
                    f"max CTE {max_cte:.2f} cm > {POS_CTE_MAX_CM:.2f} cm "
                    f"-> D x1.15")
            else:
                kp = _nudge(kp, 1.10)
                reasons.append(
                    f"max CTE {max_cte:.2f} cm, D near limit -> P x1.10")

        if mean_cte > POS_CTE_MEAN_MAX_CM:
            kp = _nudge(kp, 1.10)
            reasons.append(
                f"mean CTE {mean_cte:.2f} cm > {POS_CTE_MEAN_MAX_CM:.2f} cm "
                f"-> P x1.10")

        g.update(kp_pos=kp, kd_pos=kd)

    return target.clamp(g), reasons


def within_tolerance(target, metrics):
    """True when every metric for `target` is inside its acceptance band."""
    if metrics is None:
        return False

    if target.key == "rotation":
        if metrics["overshoot_pct"] > ROT_OVERSHOOT_MAX_PCT:
            return False
        rise = metrics.get("rise_s")
        if rise is None or rise > ROT_RISE_MAX_S:
            return False
        settle = metrics.get("settle_s")
        if settle is None or settle > ROT_SETTLE_MAX_S:
            return False
        return metrics["ss_err_deg"] <= ROT_SS_ERR_MAX_DEG

    if target.key == "heading":
        return (abs(metrics["err_mean_deg"]) <= HDG_MEAN_MAX_DEG and
                metrics["err_std_deg"] <= HDG_STD_MAX_DEG)

    if target.key == "position":
        return (metrics["cte_max_cm"] <= POS_CTE_MAX_CM and
                metrics["cte_mean_cm"] <= POS_CTE_MEAN_MAX_CM)

    return False


# ============================================================================
# Reporting
# ============================================================================

def _fmt(value, spec, fallback="n/a"):
    return fallback if value is None else format(value, spec)


def describe(target, metrics):
    """Human-readable metric lines for the panel's metrics box."""
    if metrics is None:
        return ["(no usable data for this run - check the robot is powered, "
                "on the floor, and the link is up)"]

    if metrics["kind"] == "step":
        ok = within_tolerance(target, metrics)
        return [
            f"Step {metrics['step_deg']:+.0f} deg   "
            f"({metrics['n_samples']} samples / {metrics['duration_s']:.1f} s)",
            f"Overshoot      : {metrics['overshoot_pct']:6.1f} %   "
            f"(limit {ROT_OVERSHOOT_MAX_PCT:.0f} %)",
            f"Rise time      : {_fmt(metrics.get('rise_s'), '6.2f'):>6} s   "
            f"(limit {ROT_RISE_MAX_S:.2f} s)",
            f"Settling time  : {_fmt(metrics.get('settle_s'), '6.2f'):>6} s   "
            f"(limit {ROT_SETTLE_MAX_S:.2f} s)",
            f"Steady-state   : {metrics['ss_err_deg']:6.2f} deg "
            f"(limit {ROT_SS_ERR_MAX_DEG:.1f} deg)",
            f"Verdict        : {'WITHIN TOLERANCE' if ok else 'outside tolerance'}",
        ]

    if metrics["kind"] == "heading":
        ok = within_tolerance(target, metrics)
        return [
            f"Heading hold   ({metrics['n_samples']} samples)",
            f"Mean error     : {metrics['err_mean_deg']:6.2f} deg "
            f"(limit {HDG_MEAN_MAX_DEG:.2f})",
            f"Std deviation  : {metrics['err_std_deg']:6.2f} deg "
            f"(limit {HDG_STD_MAX_DEG:.2f})",
            f"Peak |error|   : {metrics['err_max_deg']:6.2f} deg",
            f"Verdict        : {'WITHIN TOLERANCE' if ok else 'outside tolerance'}",
        ]

    if metrics["kind"] == "cte":
        ok = within_tolerance(target, metrics)
        return [
            f"Path following ({metrics['n_samples']} samples)",
            f"Mean CTE       : {metrics['cte_mean_cm']:6.2f} cm "
            f"(limit {POS_CTE_MEAN_MAX_CM:.2f})",
            f"Max CTE        : {metrics['cte_max_cm']:6.2f} cm "
            f"(limit {POS_CTE_MAX_CM:.2f})",
            f"RMS CTE        : {metrics['cte_rms_cm']:6.2f} cm",
            f"Verdict        : {'WITHIN TOLERANCE' if ok else 'outside tolerance'}",
        ]

    return [str(metrics)]
