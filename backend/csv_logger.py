# ============================================================================
# CSV Logger - Day 5: Export recorded debug sessions to CSV
# ============================================================================
# Reads the session log accumulated in backend.data_store while recording
# was active, and writes it to a CSV file with a small metadata header.
#
# Output format:
#   # Session Metadata
#   Start Time, <start>
#   End Time,   <end>
#   Device IP,  <ip>
#   Total Samples, <n>
#   <blank line>
#   Timestamp, Type, ID, Name, Value
#   <data rows...>
# ============================================================================

import csv
from backend import data_store


def export_to_csv(filepath):
    """
    Write the current session_log (from data_store) to a CSV file.

    Args:
        filepath: destination .csv path (str)

    Returns:
        int: number of data rows written

    Raises:
        ValueError: if there is nothing to export (empty session log)
        OSError: if the file can't be written
    """
    if not data_store.session_log:
        raise ValueError("No recorded samples to export. Start/stop a recording first.")

    status = data_store.get_recording_status()

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # ------------------------------------------------------------------
        # Session metadata header
        # ------------------------------------------------------------------
        writer.writerow(["# Session Metadata"])
        writer.writerow(["Start Time", status["session_start"] or "N/A"])
        writer.writerow(["End Time", status["session_end"] or "N/A (still recording)"])
        writer.writerow(["Device IP", status["device_ip"] or "N/A"])
        writer.writerow(["Total Samples", status["sample_count"]])
        writer.writerow([])  # blank separator line

        # ------------------------------------------------------------------
        # Data rows: Timestamp, Type, ID, Name, Value
        # ------------------------------------------------------------------
        writer.writerow(["Timestamp", "Type", "ID", "Name", "Value"])

        for entry in data_store.session_log:
            writer.writerow([
                entry["timestamp"],
                entry["type"],
                entry["id"],
                entry["name"],
                entry["value"],
            ])

    return len(data_store.session_log)