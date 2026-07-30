# ============================================================================
# Packet Parser - Enhanced for Day 3 (with latency tracking + DEBUG)
# ============================================================================
from datetime import datetime
from backend.data_store import variables, events
import time

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
# Packet Parser with DEBUG
# ============================================================================

def parse_packet(packet):
    """Parse incoming packet from STM32"""

    packet = packet.strip()

    if not packet:
        print(f"[PARSER] Empty packet, skipping")  # DEBUG
        return

    print(f"[PARSER] Received: {packet}")  # DEBUG - ALWAYS SHOW INCOMING

    # ========================================================================
    # VARIABLE PACKET - Format: VAR:name=value
    # ========================================================================
    if packet.startswith("VAR:"):
        print(f"[PARSER] Processing VAR packet")  # DEBUG
        payload = packet[4:]
        if "=" in payload:
            name, value = payload.split("=", 1)
            variables[name.strip()] = {
                "value": value.strip(),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "status": "confirmed"
            }
            print(f"[PARSER] Stored: {name.strip()} = {value.strip()}")  # DEBUG
        return

    # ========================================================================
    # EVENT PACKET - Format: EVENT:message
    # ========================================================================
    if packet.startswith("EVENT:"):
        print(f"[PARSER] Processing EVENT packet")  # DEBUG
        payload = packet[6:]
        events.append({
            "event": payload.strip(),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "type": "info"
        })
        print(f"[PARSER] Event logged: {payload.strip()}")  # DEBUG
        return

    # ========================================================================
    # CONFIRMATION PACKET - Format: CONFIRM:command,status
    # ========================================================================
    if packet.startswith("CONFIRM:"):
        print(f"[PARSER] Processing CONFIRM packet")  # DEBUG
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
            print(f"[PARSER] Confirmation logged with latency: {latency}")  # DEBUG
        return

    # ========================================================================
    # Status Packet
    # ========================================================================
    if "alive" in packet.lower():
        print(f"[PARSER] Status packet (alive), ignoring")  # DEBUG
        return

    # Unknown packet
    print(f"[PARSER] Unknown packet: {packet}")  # DEBUG
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