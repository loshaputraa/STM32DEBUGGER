# ============================================================================
# Packet Parser - Parses data packets from STM32
# ============================================================================
# Handles VAR (variable), EVENT, and CMD (command) packet types

from datetime import datetime  # For timestamping
from backend.data_store import variables, events

def parse_packet(packet):
    """
    Parse incoming packet from STM32 and store data appropriately.

    Packet formats:
    - VAR:name=value       → Stores variable with timestamp
    - EVENT:message        → Stores event with timestamp
    - Other               → Ignored (or could be logged)

    Args:
        packet (str): Raw packet string from STM32
    """

    # Remove leading/trailing whitespace from packet
    packet = packet.strip()

    # ========================================================================
    # VARIABLE PACKET - Format: VAR:name=value
    # ========================================================================
    if packet.startswith("VAR:"):
        # Extract the payload after "VAR:" prefix
        payload = packet[4:]

        # Split on "=" to get variable name and value
        if "=" in payload:
            name, value = payload.split("=", 1)  # split only on first "="

            # Store in variables dict with timestamp
            variables[name.strip()] = {
                "value": value.strip(),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
        return

    # ========================================================================
    # EVENT PACKET - Format: EVENT:message
    # ========================================================================
    if packet.startswith("EVENT:"):
        # Extract the message after "EVENT:" prefix
        payload = packet[6:]

        # Add to events list with timestamp
        events.append({
            "event": payload.strip(),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        return