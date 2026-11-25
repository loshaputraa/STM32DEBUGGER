# ============================================================================
# Packet Parser - Enhanced with Length-Prefixed Framing
# ============================================================================
# Eliminates TCP fragmentation issues by validating frame length
# before processing payload

from datetime import datetime
from backend.data_store import (
    variables, events, log_sample, add_to_history,
    note_list_variable, finish_list_snapshot
)
import time

# ============================================================================
# Framing Constants
# ============================================================================
FRAME_HEADER = 0xAA  # Sync byte for frame detection

# ============================================================================
# STATS Tracking - only report when counters actually increase
# ============================================================================
# These counters are cumulative totals since STM32 boot - they never reset
# on their own. Reporting "nonzero" meant a single early drop got re-logged
# every second forever. Track the last-seen value per counter and only log
# when something has genuinely gone up since the last packet.
_last_stats_counters = {}
_stats_baseline_set = False

# ============================================================================
# Latency Tracking
# ============================================================================
pending_commands = {}
command_counter = 0


def get_next_command_id():
    global command_counter
    command_counter += 1
    return command_counter


def record_command_sent(cmd_id, command, sent_time):
    pending_commands[cmd_id] = {
        "sent_time": sent_time,
        "command": command,
        "latency": None
    }


def calculate_latency(cmd_id):
    if cmd_id in pending_commands:
        sent_time = pending_commands[cmd_id]["sent_time"]
        latency_ms = (time.time() - sent_time) * 1000
        pending_commands[cmd_id]["latency"] = latency_ms
        return latency_ms
    return None


# ============================================================================
# FIX: Clear pending commands on reconnect
# ============================================================================
# The latency readings were bogus (100,000+ms) because:
#   1. pending_commands dict was never cleared on disconnect/reconnect
#   2. A stale entry from before disconnect would hang around
#   3. CONFIRM matching was by prefix ("SET" matches "SET:var1" AND "SET:var8")
#      so an old command could get matched to a new confirmation
#   4. This produced fake latencies of "time since the old command was sent"
#
# Fix: Call this function whenever the connection is re-established
# (from tcp_client.py's _connect_worker after socket.connect succeeds)
def reset_latency_tracking():
    """Clear pending commands on reconnect - prevents stale entries from
    producing fake latencies."""
    global pending_commands
    pending_commands = {}


# ============================================================================
# Frame Parsing - Length-Prefixed Protocol
# ============================================================================

def extract_frames(buffer):
    """
    Extract complete frames from buffer.
    Returns: (list of complete frames, remaining incomplete buffer)

    Frame format: [0xAA][LENGTH][PAYLOAD...]\r\n

    Example:
    Input buffer: b'\\xaa\\x13VAR:temperature=25\\r\\n\\xaa\\x0f...'
    Output: (['VAR:temperature=25'], b'\\xaa\\x0f...')

    Handles:
    - Multiple frames in one buffer
    - Fragmented frames (incomplete)
    - Invalid headers (skipped)
    """

    frames = []
    pos = 0

    while pos < len(buffer):
        # ====================================================================
        # Search for frame header (0xAA)
        # ====================================================================
        header_pos = buffer.find(FRAME_HEADER, pos)

        if header_pos == -1:
            # No more headers found
            remaining = buffer[pos:]
            return frames, remaining

        # ====================================================================
        # Check if we have at least header + length byte
        # ====================================================================
        if header_pos + 1 >= len(buffer):
            # Not enough data yet - wait for more
            remaining = buffer[header_pos:]
            return frames, remaining

        # Read length byte (position after header)
        payload_length = buffer[header_pos + 1]

        # ====================================================================
        # Calculate total frame size needed
        # ====================================================================
        # Frame = [HEADER] [LENGTH] [PAYLOAD...] [\r\n]
        #         1 byte   1 byte   N bytes      2 bytes
        frame_start = header_pos
        payload_start = header_pos + 2
        payload_end = payload_start + payload_length
        frame_end = payload_end + 2  # Account for \r\n

        # ====================================================================
        # Check if we have complete frame
        # ====================================================================
        if frame_end > len(buffer):
            # Incomplete frame - wait for more data
            remaining = buffer[frame_start:]
            return frames, remaining

        # ====================================================================
        # Verify terminator (\r\n)
        # ====================================================================
        if (buffer[payload_end] != ord('\r') or
                buffer[payload_end + 1] != ord('\n')):
            # Invalid terminator - skip this header and search again
            print(f"[PARSER] Frame validation failed at pos {frame_start}: "
                  f"expected \\r\\n, got {buffer[payload_end:payload_end + 2]}")
            pos = header_pos + 1
            continue

        # ====================================================================
        # Valid frame - extract payload
        # ====================================================================
        payload = buffer[payload_start:payload_end].decode('utf-8', errors='ignore')
        frames.append(payload)

        # Move position past this frame
        pos = frame_end

    # All frames processed, no remaining data
    return frames, b''


# ============================================================================
# Packet Parser
# ============================================================================

def parse_packet(packet):
    """
    Parse a complete, de-framed packet.
    At this point, TCP fragmentation is already handled by frame extraction.

    Packet types:
    - VAR:name=value
    - CONFIRM:command,status
    - EVENT:message
    - STM32 alive
    """

    packet = packet.strip()

    if not packet:
        print(f"[PARSER] Empty packet, skipping")
        return

    # ========================================================================
    # VARIABLE PACKET - Format: VAR:name=value
    # ========================================================================
    if packet.startswith("VAR:"):
        payload = packet[4:]
        if "=" in payload:
            name, value = payload.split("=", 1)
            name = name.strip()
            value = value.strip()
            variables[name] = {
                "value": value,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "status": "confirmed"
            }

            add_to_history(name, value)          # Day 4: record sample for graphing
            log_sample("VAR", name, value)        # Day 5: session recording
            note_list_variable(name)             # LIST snapshot tracking (no-op unless a LIST is in flight)
        return

    # ========================================================================
    # EVENT PACKET - Format: EVENT:message
    # ========================================================================
    if packet.startswith("EVENT:"):
        payload = packet[6:]
        events.append({
            "event": payload.strip(),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "type": "info"
        })
        log_sample("EVENT", "-", payload.strip())  # Day 5: session recording
        return

    # ========================================================================
    # CONFIRMATION PACKET - Format: CONFIRM:command,status
    # ========================================================================
    if packet.startswith("CONFIRM:"):
        payload = packet[8:]
        parts = payload.split(",")

        if len(parts) >= 2:
            command = parts[0].strip()
            status = parts[1].strip()

            latency = None
            # FIX: Match by EXACT sent command, not prefix.
            # Before: "SET" prefix would match both "SET:var1" and "SET:var8",
            # causing a stale entry to match a new confirmation and produce
            # fake 100,000+ ms latencies.
            # Now: Extract the full command we sent (including the variable),
            # and only match exact strings.
            matched_cmd_id = None
            for cmd_id, cmd_info in list(pending_commands.items()):
                if cmd_info["command"] == command:
                    latency = calculate_latency(cmd_id)
                    matched_cmd_id = cmd_id
                    break

            if matched_cmd_id is not None:
                del pending_commands[matched_cmd_id]

            event_msg = f"Command confirmed: {command} → {status}"
            if latency is not None:
                event_msg += f" (Latency: {latency:.1f}ms)"

            events.append({
                "event": event_msg,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "type": "confirm",
                "command": command,
                "status": status,
                "latency_ms": latency
            })

            if latency is not None:
                print(f"[PARSER] Confirmation with latency: {latency:.1f}ms")
            else:
                print(f"[PARSER] Confirmation received (no matching pending command)")

            # Day 5: session recording
            confirm_value = status if latency is None else f"{status} ({latency:.1f}ms)"
            log_sample("CONFIRM", command, confirm_value)

            # LIST completion: this is the device's full, authoritative
            # variable set - drop anything we had that wasn't in it.
            if command == "LIST":
                removed = finish_list_snapshot()
                if removed:
                    removed_list = ", ".join(sorted(removed))
                    events.append({
                        "event": f"Removed stale variable(s) no longer on device: {removed_list}",
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                        "type": "info"
                    })
        return

    # ========================================================================
    # STATS PACKET - Format: STATS:cmd_dropped=N,rx_overflow=N,uart_ore=N,uart_fe=N,uart_ne=N
    # ========================================================================
    # Periodic health counters from uart_handler.c. These are cumulative
    # totals since STM32 boot, so "nonzero" isn't the right test - a single
    # early error would then get re-logged every second forever. Only log
    # when a counter has actually gone UP since the last packet.
    if packet.startswith("STATS:"):
        global _stats_baseline_set
        payload = packet[6:]
        counters = {}
        for field in payload.split(","):
            if "=" in field:
                key, val = field.split("=", 1)
                try:
                    counters[key.strip()] = int(val.strip())
                except ValueError:
                    pass

        if not _stats_baseline_set:
            # First STATS packet this session - just establish a baseline,
            # don't alarm on whatever total happened to already exist.
            _last_stats_counters.update(counters)
            _stats_baseline_set = True
            return

        increased = {}
        for key, val in counters.items():
            prev = _last_stats_counters.get(key, val)
            if val > prev:
                increased[key] = (prev, val)
            _last_stats_counters[key] = val

        if increased:
            summary = ", ".join(f"{k} {p}→{v} (+{v - p})" for k, (p, v) in increased.items())
            print(f"[PARSER] STATS counter increase: {summary}")
            events.append({
                "event": f"UART/command error counter(s) increased: {summary}",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "type": "warning"
            })
        return

    # ========================================================================
    # Status Packet
    # ========================================================================
    if "alive" in packet.lower():
        return

    # Unknown packet
    print(f"[PARSER] Unknown packet: {packet}")
    events.append({
        "event": f"Unknown packet: {packet}",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "type": "unknown"
    })


def get_average_latency():
    latencies = []
    for event in reversed(events[-20:]):
        if event.get("type") == "confirm" and event.get("latency_ms") is not None:
            latencies.append(event["latency_ms"])

    if latencies:
        return sum(latencies) / len(latencies)
    return None


def get_latest_latency():
    for event in reversed(events):
        if event.get("type") == "confirm" and event.get("latency_ms") is not None:
            return event["latency_ms"]
    return None