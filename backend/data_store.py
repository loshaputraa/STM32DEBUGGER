# ============================================================================
# Data Store - Central storage for variables, events, rolling history,
# and recording sessions
# ============================================================================
# Day 3: `variables` (dict) and `events` (list) - live command/response state
# Day 4: rolling time-series history per variable, for real-time graphing
# Day 5: recording session state, for CSV export

from datetime import datetime
import time
from collections import deque

# ============================================================================
# Live variable / event state (Day 3)
# ============================================================================
variables = {}
events = []

# ============================================================================
# ROLLING HISTORY - Time-series data for graphing (Day 4)
# ============================================================================
# Maintains 1200 samples @ 50ms = 60 seconds of history per variable

HISTORY_MAX_SAMPLES = 1200
HISTORY_WINDOW_S = 60


class TimeSeriesBuffer:
    """
    Fixed-size rolling buffer for time-series data.
    Automatically trims old data when max_samples exceeded (deque maxlen).
    """

    def __init__(self, max_samples=HISTORY_MAX_SAMPLES):
        self.max_samples = max_samples
        self.data = deque(maxlen=max_samples)  # Auto-trim oldest when full

    def add(self, value):
        """Add value, timestamped now. Non-numeric values are ignored."""
        try:
            val = float(value)
        except (ValueError, TypeError):
            return  # Ignore non-numeric values
        self.data.append((time.time(), val))

    def get_data(self):
        """Return list of (timestamp, value) tuples"""
        return list(self.data)

    def get_data_numpy(self):
        """Return separate (times, values) lists for efficient plotting"""
        if not self.data:
            return [], []
        times, values = zip(*self.data)
        return list(times), list(values)

    def clear(self):
        self.data.clear()

    def size(self):
        return len(self.data)

    def latest_value(self):
        return self.data[-1][1] if self.data else None


# Dictionary to store time-series history per variable: {var_name: TimeSeriesBuffer}
history = {}


def add_to_history(var_name, value):
    """Add a data point to var_name's rolling history buffer."""
    if var_name not in history:
        history[var_name] = TimeSeriesBuffer()
    history[var_name].add(value)


def get_history(var_name):
    """Get time-series data for a variable as [(timestamp, value), ...]"""
    if var_name in history:
        return history[var_name].get_data()
    return []


def get_history_numpy(var_name):
    """Get time-series data for a variable as (times, values) lists"""
    if var_name in history:
        return history[var_name].get_data_numpy()
    return [], []


def get_variable_names():
    """Return list of all variable names currently tracked in history (for graph selectors)."""
    return list(history.keys())


def get_history_size(var_name):
    if var_name in history:
        return history[var_name].size()
    return 0


def clear_history(var_name=None):
    """Clear history for one variable, or all variables if var_name is omitted."""
    if var_name:
        if var_name in history:
            history[var_name].clear()
    else:
        history.clear()


# ============================================================================
# RECORDING SESSION STATE (Day 5 - Logging)
# ============================================================================
# Tracks an active "recording" session: while active, every VAR/EVENT/CONFIRM
# packet processed by the parser is appended to session_log. This log is
# what gets written out to CSV by backend/csv_logger.py.

recording = False          # True while a session is actively recording
session_start = None       # datetime: when Start Recording was pressed
session_end = None         # datetime: when Stop Recording was pressed
device_ip = None           # str: ESP32 IP address for this session
session_log = []           # list of dicts: {timestamp, type, id, name, value}
sample_count = 0           # convenience counter (== len(session_log))

_log_id_counter = 0        # internal auto-increment ID for log rows


def start_recording(ip_address=None):
    """
    Begin a new recording session.
    Clears any previous session log and resets counters.
    """
    global recording, session_start, session_end, device_ip
    global session_log, sample_count, _log_id_counter

    recording = True
    session_start = datetime.now()
    session_end = None
    device_ip = ip_address
    session_log = []
    sample_count = 0
    _log_id_counter = 0


def stop_recording():
    """Stop the current recording session (log_sample() calls become no-ops)."""
    global recording, session_end
    recording = False
    session_end = datetime.now()


def log_sample(entry_type, name, value):
    """
    Append one row to the session log, if recording is currently active.

    Args:
        entry_type: "VAR", "EVENT", or "CONFIRM"
        name: variable name / command name / "-" for plain events
        value: the value / message / status text

    No-op when recording is False, so this is safe to call unconditionally
    from the parser on every packet.
    """
    global sample_count, _log_id_counter

    if not recording:
        return

    _log_id_counter += 1
    session_log.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "type": entry_type,
        "id": _log_id_counter,
        "name": name,
        "value": value,
    })
    sample_count += 1


def get_recording_status():
    """Return a snapshot dict of the current recording session state."""
    return {
        "recording": recording,
        "session_start": session_start.strftime("%Y-%m-%d %H:%M:%S") if session_start else None,
        "session_end": session_end.strftime("%Y-%m-%d %H:%M:%S") if session_end else None,
        "device_ip": device_ip,
        "sample_count": sample_count,
    }