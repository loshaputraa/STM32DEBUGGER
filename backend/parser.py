# ============================================================================
# Packet Parser - Enhanced with Length-Prefixed Framing
# ============================================================================
# Eliminates TCP fragmentation issues by validating frame length
# before processing payload

from datetime import datetime
from backend.data_store import variables, events, log_sample, add_to_history
import time

# ============================================================================
# Framing Constants
# ============================================================================
FRAME_HEADER = 0xAA  # Sync byte for frame detection

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

        print(f"[FRAME] 0x{FRAME_HEADER:02X} 0x{payload_length:02X} {payload}")

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

    print(f"[PARSER] Processing: {packet}")

    # ========================================================================
    # VARIABLE PACKET - Format: VAR:name=value
    # ========================================================================
    if packet.startswith("VAR:"):
        print(f"[PARSER] VAR packet detected")
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
            print(f"[PARSER] Stored: {name} = {value}")

            add_to_history(name, value)          # Day 4: record sample for graphing
            log_sample("VAR", name, value)        # Day 5: session recording
        return

    # ========================================================================
    # EVENT PACKET - Format: EVENT:message
    # ========================================================================
    if packet.startswith("EVENT:"):
        print(f"[PARSER] EVENT packet detected")
        payload = packet[6:]
        events.append({
            "event": payload.strip(),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "type": "info"
        })
        print(f"[PARSER] Event logged: {payload.strip()}")
        log_sample("EVENT", "-", payload.strip())  # Day 5: session recording
        return

    # ========================================================================
    # CONFIRMATION PACKET - Format: CONFIRM:command,status
    # ========================================================================
    if packet.startswith("CONFIRM:"):
        print(f"[PARSER] CONFIRM packet detected")
        payload = packet[8:]
        parts = payload.split(",")

        if len(parts) >= 2:
            command = parts[0].strip()
            status = parts[1].strip()

            latency = None
            for cmd_id, cmd_info in list(pending_commands.items()):
                if cmd_info["command"].startswith(command):
                    latency = calculate_latency(cmd_id)
                    del pending_commands[cmd_id]
                    break

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
        return

    # ========================================================================
    # Status Packet
    # ========================================================================
    if "alive" in packet.lower():
        print(f"[PARSER] Status packet (alive), storing")
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