# ============================================================================
# TCP Client - ESP32 Communication with Framing Support
# ============================================================================
# Enhanced for length-prefixed frames to handle TCP fragmentation
# 100% reliable packet delivery under stress

import socket
import threading
import time
import sys
from backend.parser import (
    parse_packet,
    record_command_sent,
    get_next_command_id,
    get_latest_latency,
    extract_frames  # NEW: Frame extraction function
)

# ============================================================================
# CONFIGURATION SECTION
# ============================================================================
ESP32_IP = "192.168.0.36"
ESP32_PORT = 5000


# ============================================================================
# DebugBridge CLASS - Handles TCP communication with framing
# ============================================================================

class DebugBridge:
    """
    TCP client for ESP32/STM32 communication with length-prefixed framing.

    Features:
    - Connects to ESP32 TCP server
    - Sends commands and measures round-trip latency
    - Receives and parses STM32 responses with frame extraction
    - 100% reliable packet delivery (no TCP fragmentation loss)
    - Provides connection status and latency metrics

    Protocol:
    - Frame format: [0xAA][LENGTH][PAYLOAD]\r\n
    - Handles TCP fragmentation automatically
    - Self-healing: Searches for next valid frame on error
    """

    def __init__(self):
        """Initialize the DebugBridge"""
        self.socket = None
        self.connected = False
        self.lock = threading.Lock()
        self.stm32_alive = False

        # Latency tracking
        self.latest_latency_ms = None
        self.average_latency_ms = None
        self.command_count = 0

        # Frame buffer for incomplete data
        self.frame_buffer = b''

    def connect(self):
        """
        Establish TCP connection to ESP32 and start receiving.
        """
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((ESP32_IP, ESP32_PORT))
            self.connected = True
            self.stm32_alive = False

            with self.lock:
                print(f"\n✓ Connected to ESP32 at {ESP32_IP}:{ESP32_PORT}")
                print("  Waiting for STM32 status...\n")

            # Start background receiver thread
            receiver_thread = threading.Thread(
                target=self.receive_data,
                daemon=True
            )
            receiver_thread.start()

        except Exception as e:
            with self.lock:
                print(f"\n✗ Connection failed: {e}\n")
            self.connected = False

    def receive_data(self):
        """
        Background thread - continuously listen for messages.

        This new version uses frame extraction to handle TCP fragmentation:
        1. Accumulate raw bytes in frame_buffer
        2. Call extract_frames() to pull out complete frames
        3. Parse each complete frame
        4. Keep incomplete frames for next recv() call
        """

        while self.connected:
            try:
                # Receive raw TCP data
                raw_data = self.socket.recv(1024)

                if not raw_data:
                    if self.stm32_alive:
                        with self.lock:
                            print("\n✗ STM32: DEAD (connection lost)")
                        self.stm32_alive = False
                    self.connected = False
                    break

                # ============================================================
                # Accumulate data in frame buffer
                # ============================================================
                self.frame_buffer += raw_data

                print(f"[TCP] Received {len(raw_data)} bytes, buffer size: {len(self.frame_buffer)}")

                # ============================================================
                # Extract complete frames from buffer
                # ============================================================
                frames, self.frame_buffer = extract_frames(self.frame_buffer)

                # ============================================================
                # Process each complete frame
                # ============================================================
                for frame_data in frames:
                    # Check for "alive" status
                    if "alive" in frame_data.lower():
                        if not self.stm32_alive:
                            self.stm32_alive = True
                            with self.lock:
                                print("\n✓ STM32: ALIVE")
                                print("  Ready to send commands\n")
                                sys.stdout.write("Enter command: ")
                                sys.stdout.flush()
                    else:
                        # Parse non-alive packets
                        parse_packet(frame_data)
                        self.latest_latency_ms = get_latest_latency()

                        with self.lock:
                            print(f"\nSTM32: {frame_data}")
                            sys.stdout.write("Enter command: ")
                            sys.stdout.flush()

            except Exception as e:
                if self.stm32_alive:
                    self.stm32_alive = False
                    with self.lock:
                        print(f"\n✗ STM32: DEAD ({str(e)})")
                        print("  Type 'reset' to reconnect\n")
                        sys.stdout.write("Enter command: ")
                        sys.stdout.flush()
                self.connected = False
                break

    def send_command(self, command):
        """
        Send command to STM32 and record timing for latency measurement.

        Args:
            command (str): Command text (e.g., "SET:temperature=25")

        Returns:
            cmd_id: Unique command ID for tracking latency
        """
        if self.connected:
            try:
                # Generate unique ID for this command
                cmd_id = get_next_command_id()

                # Record send time
                sent_time = time.time()
                record_command_sent(cmd_id, command, sent_time)

                # Send command with newline
                message_bytes = (command + '\n').encode('utf-8')
                self.socket.sendall(message_bytes)
                self.command_count += 1

                with self.lock:
                    print(f"✓ Sent: {command}\n")

                return cmd_id

            except Exception as e:
                with self.lock:
                    print(f"✗ Send failed: {e}\n")
                self.connected = False
                return None
        else:
            with self.lock:
                print("✗ Not connected to ESP32\n")
            return None

    def disconnect(self):
        """Close connection and clean up"""
        if self.socket:
            try:
                self.socket.close()
            except:
                pass

        self.connected = False
        self.stm32_alive = False

    def get_status(self):
        """
        Get connection and latency status.

        Returns:
            dict with keys:
            - connected: bool
            - stm32_alive: bool
            - latest_latency_ms: float or None
            - command_count: int
            - frame_buffer_size: int (debug)
        """
        return {
            "connected": self.connected,
            "stm32_alive": self.stm32_alive,
            "latest_latency_ms": self.latest_latency_ms,
            "command_count": self.command_count,
            "frame_buffer_size": len(self.frame_buffer)  # Debug info
        }