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
    extract_frames,  # NEW: Frame extraction function
    reset_latency_tracking  # NEW: Clear pending commands on reconnect
)
from backend.config import ESP32_IP, ESP32_PORT, SOCKET_TIMEOUT_S

# ============================================================================
# CONFIGURATION SECTION
# ============================================================================
# ESP32_IP / ESP32_PORT / SOCKET_TIMEOUT_S now live in backend/config.py -
# this is the single place to change them. They're re-imported here (rather
# than referenced as backend.config.X) so `from backend.tcp_client import
# ESP32_IP` keeps working for any existing callers.
#
# FIX (kept for context): without a timeout, self.socket.recv() in
# receive_data() blocks forever if the ESP32 hangs without closing the TCP
# connection - the receiver thread never notices, `connected` stays True,
# and the GUI shows a stale "Connected" status indefinitely.

# [DEBUG] Toggle verbose lifecycle tracing for connect/disconnect. Leave True
# while chasing the flash-timeout / WinError 10038 issue, flip to False once
# confirmed fixed.
DEBUG_LIFECYCLE = True


def _dbg(tag, msg):
    if DEBUG_LIFECYCLE:
        print(f"[DEBUG:{tag}] {msg}")


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

    def __init__(self, ip=None, port=None):
        """
        Initialize the DebugBridge.

        Args:
            ip: ESP32 IP address. Defaults to backend.config.ESP32_IP.
            port: ESP32 TCP port. Defaults to backend.config.ESP32_PORT.
        """
        self.ip = ip if ip is not None else ESP32_IP
        self.port = port if port is not None else ESP32_PORT

        self.socket = None
        self.connected = False
        self.connecting = False  # True while a connect attempt is in flight (background thread)
        self.lock = threading.Lock()
        self.stm32_alive = False

        # Latency tracking
        self.latest_latency_ms = None
        self.average_latency_ms = None
        self.command_count = 0

        # Frame buffer for incomplete data
        self.frame_buffer = b''

        # [DEBUG] Receiver thread handle + explicit shutdown flag, needed so
        # disconnect() can wait for the thread to actually exit instead of
        # racing socket.close() against a blocked recv() call.
        self._receiver_thread = None
        self._shutdown_requested = threading.Event()
        self._connect_thread = None

    def connect(self):
        """
        Kick off a TCP connection attempt to the ESP32 in a background
        thread and return immediately.

        FIX: previously the actual blocking socket.connect() (up to
        SOCKET_TIMEOUT_S) ran directly on whatever thread called connect() -
        which for both the initial connect and the GUI's auto-reconnect
        timer is the Qt main thread. If the ESP32 was unreachable, every
        connect/retry attempt froze the entire GUI (no button worked) for
        the full timeout, repeatedly. connect() now only launches a worker
        thread that does the real work; callers can poll `.connecting` /
        `.connected` (already how the GUI reads status) without blocking.
        """
        if self.connecting or self.connected:
            _dbg("CONNECT", "connect() called while already connecting/connected - ignoring")
            return

        self.connecting = True
        self._connect_thread = threading.Thread(target=self._connect_worker, daemon=True)
        self._connect_thread.start()

    def _connect_worker(self):
        """Actual blocking connect logic - always runs on a background thread."""
        try:
            _dbg("CONNECT", f"Opening socket to {self.ip}:{self.port} ...")
            self._shutdown_requested.clear()
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(SOCKET_TIMEOUT_S)  # FIX: applies to connect() and recv()
            self.socket.connect((self.ip, self.port))
            self.connected = True
            self.stm32_alive = False

            # FIX: Clear pending commands on reconnect to prevent stale entries
            # from producing fake latencies
            reset_latency_tracking()

            with self.lock:
                print(f"\n✓ Connected to ESP32 at {self.ip}:{self.port}")
                print("  Waiting for STM32 status...\n")
            _dbg("CONNECT", "Socket connected, starting receiver thread")

            # Start background receiver thread
            self._receiver_thread = threading.Thread(
                target=self.receive_data,
                daemon=True
            )
            self._receiver_thread.start()

        except Exception as e:
            with self.lock:
                print(f"\n✗ Connection failed: {e}\n")
            _dbg("CONNECT", f"Connect failed: {e!r}")
            self.connected = False
        finally:
            self.connecting = False

    def receive_data(self):
        """
        Background thread - continuously listen for messages.

        This new version uses frame extraction to handle TCP fragmentation:
        1. Accumulate raw bytes in frame_buffer
        2. Call extract_frames() to pull out complete frames
        3. Parse each complete frame
        4. Keep incomplete frames for next recv() call
        """
        _dbg("RECV_THREAD", "Receiver thread started")

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
                    else:
                        # Parse non-alive packets
                        parse_packet(frame_data)
                        self.latest_latency_ms = get_latest_latency()

            except OSError as e:
                # [DEBUG] Distinguish an *intentional* shutdown (we closed the
                # socket ourselves from disconnect()) from a genuine link
                # failure. This is what used to surface as a bare
                # "WinError 10038" with no context - now it's labeled.
                if self._shutdown_requested.is_set():
                    _dbg("RECV_THREAD", f"recv() aborted due to intentional disconnect() ({e!r}) - expected, ignoring")
                    self.connected = False
                    break

                # FIX: the STM32 is silent for ~20-30 s at boot while
                # MPU6050_Init() calibrates. A recv() timeout before the first
                # "STM32 alive" is that boot silence, NOT a dead link - tearing
                # the socket down here made the GUI bounce the connection over
                # and over while the robot was still booting. Keep waiting
                # until the STM32 reports alive (a real drop surfaces as b''
                # above, or as a post-alive timeout below).
                if isinstance(e, socket.timeout) and not self.stm32_alive:
                    _dbg("RECV_THREAD", f"recv() timeout while STM32 still booting - keep waiting ({e!r})")
                    continue

                _dbg("RECV_THREAD", f"recv() failed unexpectedly: {e!r}")
                if self.stm32_alive:
                    self.stm32_alive = False
                    with self.lock:
                        print(f"\n✗ STM32: DEAD ({str(e)})")
                        print("  Type 'reset' to reconnect\n")
                        sys.stdout.write("Enter command: ")
                        sys.stdout.flush()
                self.connected = False
                break

            except Exception as e:
                _dbg("RECV_THREAD", f"Unexpected error: {e!r}")
                if self.stm32_alive:
                    self.stm32_alive = False
                    with self.lock:
                        print(f"\n✗ STM32: DEAD ({str(e)})")
                        print("  Type 'reset' to reconnect\n")
                        sys.stdout.write("Enter command: ")
                        sys.stdout.flush()
                self.connected = False
                break

        _dbg("RECV_THREAD", "Receiver thread exiting")

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

    def send_raw(self, command):
        """Send a command WITHOUT latency tracking or console output.

        High-rate commands that the firmware deliberately answers with
        silence (ARUCO:POSE at ~25 Hz) must not go through send_command():
        every entry would sit in pending_commands forever waiting for a
        CONFIRM that never comes, leaking memory and poisoning the latency
        averages. Returns True on success.
        """
        if not self.connected:
            return False
        try:
            self.socket.sendall((command + '\n').encode('utf-8'))
            self.command_count += 1
            return True
        except Exception:
            self.connected = False
            return False

    def claim_motion(self, axes=7):
        """Assert the drive gates before a path/rotation command.

        The ArUco tab arms in dry-run mode (SET:motors_en=0) and can leave
        axis_mask at a bench value. Any panel that is about to command the
        robot must claim the motors back, otherwise the firmware computes a
        path but Pathfinder_Drive() holds the wheels off. PATH:GO and
        ROTATE_TO also release the firmware's PARKED latch.
        """
        if not self.connected:
            return False
        self.send_command("SET:motors_en=1")
        self.send_command(f"SET:axis_mask={int(axes)}")
        return True

    def disconnect(self):
        """
        Close connection and clean up.

        FIX: previously this called self.socket.close() from the GUI thread
        while the receiver thread could still be blocked inside
        self.socket.recv(). On Windows that races and surfaces as
        "WinError 10038: An operation was attempted on something that is
        not a socket" - and worse, the caller (open_flash_dialog) would
        immediately open a new connection to the ESP32 before the OS/ESP32
        had actually finished tearing down the old TCP connection. The
        ESP32 sketch only accepts a *new* client once it notices the old
        one is gone (`if(!client || !client.connected())`), so a flasher
        connection opened too early can succeed at the socket level while
        the ESP32 firmware never reads from it - producing exactly the
        bootloader-sync timeout you're seeing.

        Now: signal intentional shutdown, shutdown(SHUT_RDWR) to force the
        blocked recv() to return immediately, join the receiver thread so
        we know it has actually exited, THEN clear the socket. This makes
        disconnect() synchronous and safe to call right before opening a
        new connection (e.g. for flashing).
        """
        _dbg("DISCONNECT", "disconnect() called")
        self._shutdown_requested.set()
        self.connected = False

        if self.socket:
            try:
                # Forces any thread blocked in recv() to wake up immediately
                # with a clean error, instead of leaving close() to race it.
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                _dbg("DISCONNECT", f"shutdown() ignored: {e!r}")
            try:
                self.socket.close()
            except OSError as e:
                _dbg("DISCONNECT", f"close() ignored: {e!r}")

        # Wait for the receiver thread to actually finish before returning,
        # so callers (like open_flash_dialog) can rely on the socket/port
        # being fully released before opening a new connection.
        if self._receiver_thread and self._receiver_thread.is_alive():
            _dbg("DISCONNECT", "Waiting for receiver thread to exit...")
            self._receiver_thread.join(timeout=SOCKET_TIMEOUT_S + 1)
            if self._receiver_thread.is_alive():
                _dbg("DISCONNECT", "WARNING: receiver thread did not exit in time")
            else:
                _dbg("DISCONNECT", "Receiver thread exited cleanly")

        # FIX: connect() now runs the real socket.connect() on a background
        # thread (_connect_worker). If disconnect() is called while that
        # thread is still mid-attempt (e.g. auto-reconnect firing again too
        # soon, or a manual disconnect click during connect), both threads
        # end up touching self.socket at once - closing it here can race
        # _connect_worker replacing/using it, surfacing as WinError 10038.
        # The shutdown()/close() above should make its blocked connect()
        # call fail quickly; wait for that worker to actually exit before
        # this method (and thus self.socket) is considered settled.
        if self._connect_thread and self._connect_thread.is_alive():
            _dbg("DISCONNECT", "Waiting for in-flight connect attempt to abort...")
            self._connect_thread.join(timeout=SOCKET_TIMEOUT_S + 1)
            if self._connect_thread.is_alive():
                _dbg("DISCONNECT", "WARNING: connect thread did not exit in time")
            else:
                _dbg("DISCONNECT", "Connect thread exited cleanly")

        self.socket = None
        self.stm32_alive = False
        _dbg("DISCONNECT", "disconnect() complete")

    def get_status(self):
        """
        Get connection and latency status.

        Returns:
            dict with keys:
            - connected: bool
            - connecting: bool (True while a background connect attempt is in flight)
            - stm32_alive: bool
            - latest_latency_ms: float or None
            - command_count: int
            - frame_buffer_size: int (debug)
        """
        return {
            "connected": self.connected,
            "connecting": self.connecting,
            "stm32_alive": self.stm32_alive,
            "latest_latency_ms": self.latest_latency_ms,
            "command_count": self.command_count,
            "frame_buffer_size": len(self.frame_buffer)  # Debug info
        }