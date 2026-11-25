# ============================================================================
# STM32 Wireless Flasher - UART Bootloader Protocol (AN3155)
# ============================================================================
# Talks to the STM32F401's built-in ROM bootloader over the SAME UART link
# used for debug traffic, relayed wirelessly through the ESP32's raw-byte
# passthrough (backend/tcp_client.py's DebugBridge cannot be connected at
# the same time - the ESP32 only accepts ONE TCP client).
#
# Bootloader entry is automatic by default (see request_bootloader_entry()
# below) - no physical BOOT0/RESET presses needed as long as the currently
# running firmware has the ENTER_BOOTLOADER handler (see app.c). Pass
# auto_bootloader=False to flash_firmware() to fall back to the old manual
# flow instead:
#   1. Hold BOOT0 button
#   2. Tap RESET button (while still holding BOOT0)
#   3. Release BOOT0
#
# Protocol reference: ST AN3155 "USART protocol used in the STM32 bootloader"
# ============================================================================

import socket
import time

# ----------------------------------------------------------------------------
# Protocol constants (AN3155)
# ----------------------------------------------------------------------------
ACK = 0x79
NACK = 0x1F
INIT_BYTE = 0x7F

CMD_GET = 0x00
CMD_GET_ID = 0x02
CMD_GO = 0x21
CMD_WRITE_MEMORY = 0x31
CMD_EXTENDED_ERASE = 0x44   # required on F4-series (not the legacy 0x43 Erase)

FLASH_START_ADDRESS = 0x08000000
WRITE_CHUNK_SIZE = 248       # max payload bytes per Write Memory command
                              # FIX: was 256 (AN3155's own max). Every Write
                              # Memory data phase is [N-1][data][checksum] =
                              # WRITE_CHUNK_SIZE + 2 bytes on the wire - at
                              # 256 that's 258 bytes, which straddles the
                              # ESP32 relay's per-loop() read cap of 256
                              # bytes (see esp32_bridge.ino's PC->STM32
                              # buf[256]). Every single write chunk was
                              # GUARANTEED to be split into two separate
                              # STM32Serial.write() calls ~tens of ms apart,
                              # which is what stalled the bootloader on the
                              # very first write in practice.
                              #
                              # MUST be a multiple of 4: flash_firmware()
                              # pads any chunk shorter than WRITE_CHUNK_SIZE
                              # up to the next 4-byte boundary (required by
                              # flash word alignment) - that padding logic
                              # runs unconditionally, so if WRITE_CHUNK_SIZE
                              # itself isn't already a multiple of 4, every
                              # FULL-SIZE chunk also gets padded past it
                              # (250 was tried and immediately got padded to
                              # 252, tripping the "chunk too large" check
                              # below on the very first chunk). 248 is a
                              # multiple of 4, so a full chunk is never
                              # padded, and 248+2=250 still comfortably
                              # avoids the relay's read-boundary split.
SOCKET_TIMEOUT_S = 5

# ----------------------------------------------------------------------------
# Mass erase gets its own, longer timeout.
# ----------------------------------------------------------------------------
# Measured (via [DIAG] logging) at 4.33s for a global mass erase on this
# STM32F401 - well past the normal SOCKET_TIMEOUT_S (5s) danger zone, which
# is why mass_erase() used to intermittently time out waiting for the ACK
# even though the chip was erasing fine. 4.33s + 1s buffer = 5.33s;
# rounded up to 6s for a bit more margin against run-to-run jitter.
MASS_ERASE_TIMEOUT_S = 6

# ----------------------------------------------------------------------------
# Auto bootloader entry (no physical BOOT0 needed) - a plain-text command
# sent while the link is STILL in normal 8N1 debug framing, before any
# AN3155 bytes or the ENTER_FLASH_MODE marker. The running app firmware (if
# it has the ENTER_BOOTLOADER handler - see app.c) resets itself straight
# into the ROM bootloader.
#
# Falls back gracefully if the currently-flashed firmware predates this
# feature: an unrecognized command is just logged as "Unknown command" on
# the STM32 side and ignored - the flash then fails at the normal
# init_bootloader() sync-byte timeout, exactly as if BOOT0 was never
# pressed. Clear, safe failure either way, never a hang or silent corruption.
# ----------------------------------------------------------------------------
ENTER_BOOTLOADER_CMD = b"ENTER_BOOTLOADER\r\n"
AUTO_BOOTLOADER_SETTLE_S = 0.3   # time for the STM32 to receive+parse the
                                  # command, send its confirmation, reset,
                                  # and have the ROM bootloader up and
                                  # listening - before we switch UART framing
                                  # and start the AN3155 handshake
BOOTLOADER_CONFIRM_TIMEOUT_S = 3.0  # how long to wait for the STM32's own confirmation frame
FRAME_HEADER = 0xAA                 # same length-prefixed framing normal debug traffic uses

# ----------------------------------------------------------------------------
# ESP32 relay control markers (NOT part of AN3155 - consumed by the ESP32,
# never forwarded to the STM32). The bootloader's ROM UART requires even
# parity (8E1), while normal debug traffic uses 8N1 - these tell the ESP32
# relay to reconfigure its UART framing before/after a flash.
# ----------------------------------------------------------------------------
ENTER_FLASH_MODE = bytes([0xFE, 0xED, 0xBE, 0xEF])   # switch ESP32<->STM32 UART to 8E1
EXIT_FLASH_MODE = bytes([0xFE, 0xED, 0xDE, 0xAD])    # switch back to 8N1
MODE_ENTER_CONFIRM = 0xC0   # ESP32 sends this back (over TCP, not UART) after switching to 8E1
MODE_EXIT_CONFIRM = 0xC1    # ESP32 sends this back after switching back to 8N1
MODE_SWITCH_SETTLE_S = 0.15   # let the ESP32 reconfigure before more bytes flow


class FlashError(Exception):
    """Raised on any bootloader protocol failure (no ACK, bad chip response, etc.)."""
    pass


class STM32Flasher:
    """
    Wireless STM32 flasher over the ESP32 UART relay.

    Usage:
        flasher = STM32Flasher(esp32_ip, esp32_port)
        flasher.connect()
        flasher.flash_firmware("firmware.bin", progress_callback=lambda pct: print(pct))
        flasher.disconnect()

    IMPORTANT: the normal DebugBridge (backend/tcp_client.py) must be
    disconnected first - the ESP32 only accepts one TCP client at a time.
    """

    def __init__(self, esp32_ip, esp32_port):
        self.esp32_ip = esp32_ip
        self.esp32_port = esp32_port
        self.sock = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(SOCKET_TIMEOUT_S)
        self.sock.connect((self.esp32_ip, self.esp32_port))

    def disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # ------------------------------------------------------------------
    # Low-level byte I/O helpers
    # ------------------------------------------------------------------
    def _send(self, data: bytes):
        self.sock.sendall(data)

    def _recv_exact(self, n):
        """Block until exactly n bytes are received (or timeout raises)."""
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise FlashError("Connection closed by ESP32/STM32 during flash")
            buf += chunk
        return buf

    def _expect_ack(self, context=""):
        resp = self._recv_exact(1)[0]
        if resp == ACK:
            return
        if resp == NACK:
            raise FlashError(f"NACK received from bootloader ({context})")
        raise FlashError(f"Unexpected response 0x{resp:02X} from bootloader ({context})")

    @staticmethod
    def _xor_checksum(data: bytes) -> int:
        checksum = 0
        for b in data:
            checksum ^= b
        return checksum

    # ------------------------------------------------------------------
    # Auto bootloader entry (no physical BOOT0 needed)
    # ------------------------------------------------------------------
    def _read_framed_event(self, timeout_s):
        """Read one [0xAA][len][payload]\\r\\n frame - the same framing
        normal debug traffic (VAR:/EVENT:/CONFIRM:) uses (see
        uart_handler.c's UART_SendFramed / parser.py's extract_frames).
        Only meaningful in normal 8N1 mode, before enter_flash_mode()
        switches everything to raw AN3155 bytes. Returns the decoded
        payload string. Raises FlashError on timeout or a malformed frame.
        """
        original_timeout = self.sock.gettimeout()
        self.sock.settimeout(timeout_s)
        try:
            header = self._recv_exact(1)[0]
            skipped = 0
            while header != FRAME_HEADER and skipped < 512:
                header = self._recv_exact(1)[0]
                skipped += 1
            if header != FRAME_HEADER:
                raise FlashError("Never found a frame header while waiting for a confirmation")

            length = self._recv_exact(1)[0]
            payload = self._recv_exact(length)
            terminator = self._recv_exact(2)
            if terminator != b"\r\n":
                raise FlashError(f"Malformed frame terminator: {terminator!r}")
            return payload.decode("utf-8", errors="ignore")
        except socket.timeout:
            raise FlashError(f"Timed out after {timeout_s}s waiting for a confirmation from the STM32")
        finally:
            self.sock.settimeout(original_timeout)

    def request_bootloader_entry(self):
        """
        Ask the STM32 app firmware to reset itself straight into the ROM
        bootloader, over the normal (8N1) debug UART framing - no physical
        BOOT0/RESET button presses needed. Must be called BEFORE
        enter_flash_mode(), since this only works while the app firmware
        (not yet the bootloader) is listening in normal framing.

        FIX: this used to send the command and then just blindly
        time.sleep(AUTO_BOOTLOADER_SETTLE_S), assuming the reset happened.
        If the running firmware didn't actually recognize the command (an
        older build without the ENTER_BOOTLOADER handler, a typo, whatever)
        nothing here would catch it - the flash would charge ahead into
        enter_flash_mode() and init_bootloader() against a chip that never
        left the running app, and fail confusingly much later with an
        AN3155 sync-byte timeout that gives no hint the real problem was
        "the reset request was never acted on".
        Now this blocks on app.c's own CONFIRM:ENTER_BOOTLOADER,OK /
        EVENT:Entering bootloader (auto)... reply and raises immediately,
        with a clear diagnosis, if that confirmation never arrives - the
        same hard guarantee manual BOOT0+RESET gives.
        """
        self._send(ENTER_BOOTLOADER_CMD)
        event_text = self._read_framed_event(BOOTLOADER_CONFIRM_TIMEOUT_S)

        if "bootloader" not in event_text.lower():
            raise FlashError(
                f"STM32 did not confirm bootloader entry - got {event_text!r} instead "
                f"(likely 'Unknown command: ENTER_BOOTLOADER', meaning the running "
                f"firmware's APP_ParseCommand() doesn't have that handler / doesn't "
                f"call APP_RequestBootloaderReset()). Use manual BOOT0+RESET instead, "
                f"or reflash firmware that includes it."
            )

        print(f"[FLASHER] STM32 confirmed bootloader entry: {event_text!r}")

        # app.c sends a SECOND frame right after the CONFIRM - the
        # EVENT:Entering bootloader (auto)... notice - before it actually
        # resets. Drain it here explicitly so enter_flash_mode() starts
        # from a clean socket instead of having to discard it as an
        # unexpected stray. Best-effort: if it doesn't show up (firmware
        # variant that only sends the CONFIRM, timing edge case, etc.)
        # that's fine, don't fail the flash over a notice we don't
        # actually need.
        try:
            trailing = self._read_framed_event(0.5)
            print(f"[FLASHER] Drained expected trailing event: {trailing!r}")
        except FlashError:
            pass

        time.sleep(AUTO_BOOTLOADER_SETTLE_S)

    # ------------------------------------------------------------------
    # Flash-mode UART parity switching (ESP32 relay control, not AN3155)
    # ------------------------------------------------------------------
    def _wait_for_marker_confirm(self, expected_byte, context, max_stray_bytes=32, per_byte_timeout=1.0):
        """Read bytes until `expected_byte` appears, discarding anything
        else in between.

        Why this is needed: the ESP32 relay forwards STM32 telemetry
        (STM32 alive / VAR / EVENT / STATS) to this same TCP socket on
        every loop() iteration, and that forwarding runs BEFORE its
        marker-detection code in the same iteration. The STM32 keeps
        emitting that telemetry right up until the instant flash mode is
        entered, so a telemetry byte queued in an earlier loop() call can
        land in the TCP stream ahead of the mode-switch confirmation byte
        - assuming "the very next byte is the confirmation" is fragile
        against that ordering. Bounded so a genuinely dead/outdated relay
        (one that never sends the confirmation at all) still fails fast
        instead of hanging.
        """
        original_timeout = self.sock.gettimeout()
        self.sock.settimeout(per_byte_timeout)
        discarded = []
        try:
            for _ in range(max_stray_bytes):
                b = self._recv_exact(1)[0]
                if b == expected_byte:
                    if discarded:
                        print(f"[FLASHER] Discarded {len(discarded)} stray byte(s) before "
                              f"{context} confirmation (likely in-flight STM32 telemetry): "
                              f"{[hex(x) for x in discarded]}")
                    return
                discarded.append(b)
        except socket.timeout:
            raise FlashError(
                f"Timed out waiting for the {context} confirmation (0x{expected_byte:02X}) "
                f"after {len(discarded)} stray byte(s): {[hex(x) for x in discarded]}. "
                f"The ESP32 relay may be unresponsive or running firmware without "
                f"flash-mode marker support."
            )
        finally:
            self.sock.settimeout(original_timeout)

        raise FlashError(
            f"Never saw the {context} confirmation (0x{expected_byte:02X}) within "
            f"{max_stray_bytes} bytes: {[hex(x) for x in discarded]}. Either the ESP32 "
            f"relay doesn't support flash-mode marker detection at all, or the link is "
            f"dead."
        )

    def enter_flash_mode(self):
        """Tell the ESP32 relay to switch the STM32 UART to 8E1 (even parity),
        as required by the ROM bootloader. Must be called before init_bootloader().

        Raises FlashError immediately (with a clear diagnosis) if the ESP32
        doesn't confirm the switch - this means it's running an older relay
        firmware without flash-mode marker support, NOT a bootloader issue.
        """
        self._send(ENTER_FLASH_MODE)
        self._wait_for_marker_confirm(MODE_ENTER_CONFIRM, "flash-mode enter", max_stray_bytes=300)
        time.sleep(MODE_SWITCH_SETTLE_S)

    def exit_flash_mode(self):
        """Tell the ESP32 relay to switch the STM32 UART back to 8N1 (normal
        debug traffic framing). Best-effort - does not raise, since this is
        typically called during cleanup/error handling."""
        try:
            self._send(EXIT_FLASH_MODE)
            self._wait_for_marker_confirm(MODE_EXIT_CONFIRM, "flash-mode exit", per_byte_timeout=0.5)
            time.sleep(MODE_SWITCH_SETTLE_S)
        except (OSError, FlashError):
            pass  # best-effort - connection may already be gone

    # ------------------------------------------------------------------
    # Bootloader handshake
    # ------------------------------------------------------------------
    def init_bootloader(self):
        """
        Send the 0x7F sync byte. The ROM bootloader uses this single byte
        to auto-detect the baud rate, then ACKs. Must be the very first
        byte sent after the chip enters bootloader mode.
        """
        self._send(bytes([INIT_BYTE]))
        resp = self._recv_exact(1)[0]
        if resp == ACK:
            return
        if resp == NACK:
            # Some ROM versions NACK a second init attempt if already synced -
            # treat that as "already ready" rather than a hard failure.
            return
        raise FlashError(f"Unexpected response 0x{resp:02X} from bootloader (init) - "
                          f"bootloader mode may not actually be active")

    def get_id(self):
        """Optional sanity check: confirms we're actually talking to an STM32
        bootloader and returns the raw product ID bytes."""
        self._send(bytes([CMD_GET_ID, CMD_GET_ID ^ 0xFF]))
        self._expect_ack("GET_ID command")
        length = self._recv_exact(1)[0]
        product_id = self._recv_exact(length + 1)
        self._expect_ack("GET_ID data")
        return product_id

    # ------------------------------------------------------------------
    # Erase
    # ------------------------------------------------------------------
    def mass_erase(self):
        """
        Extended Erase (0x44), global erase mode - required for F4-series.
        Erases the entire flash before writing.
        """
        self._send(bytes([CMD_EXTENDED_ERASE, CMD_EXTENDED_ERASE ^ 0xFF]))
        self._expect_ack("Extended Erase command")

        # 0xFFFF signals "global mass erase" per AN3155
        payload = bytes([0xFF, 0xFF])
        checksum = self._xor_checksum(payload)

        # Mass erase routinely takes longer than the normal SOCKET_TIMEOUT_S
        # (measured 4.33s vs a 5s default) - widen the timeout just for this
        # one ACK wait, then restore it immediately after.
        original_timeout = self.sock.gettimeout()
        self.sock.settimeout(MASS_ERASE_TIMEOUT_S)
        try:
            self._send(payload + bytes([checksum]))
            self._expect_ack("Extended Erase (mass erase)")
        finally:
            self.sock.settimeout(original_timeout)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def write_memory_chunk(self, address: int, data: bytes):
        """Write up to WRITE_CHUNK_SIZE bytes starting at `address`."""
        if len(data) > WRITE_CHUNK_SIZE:
            raise ValueError(f"Chunk too large ({len(data)} > {WRITE_CHUNK_SIZE})")

        # --- Write Memory command ---
        self._send(bytes([CMD_WRITE_MEMORY, CMD_WRITE_MEMORY ^ 0xFF]))
        self._expect_ack("Write Memory command")

        # --- Address (4 bytes, big-endian) + checksum ---
        addr_bytes = address.to_bytes(4, byteorder="big")
        addr_checksum = self._xor_checksum(addr_bytes)
        self._send(addr_bytes + bytes([addr_checksum]))
        self._expect_ack("Write Memory address")

        # --- Data: [N-1][data...][checksum of (N-1) XOR all data bytes] ---
        n_minus_1 = len(data) - 1
        payload = bytes([n_minus_1]) + data
        checksum = self._xor_checksum(payload)
        full_payload = payload + bytes([checksum])
        self._send(full_payload)
        try:
            self._expect_ack("Write Memory data")
        except FlashError:
            print(f"[FLASHER] Write Memory data ACK failed for address 0x{address:08X} "
                  f"({len(full_payload)} bytes sent: {len(data)} data + 1 N-1 + 1 checksum)")
            raise

    # ------------------------------------------------------------------
    # Go (jump to application)
    # ------------------------------------------------------------------
    def go(self, address=FLASH_START_ADDRESS):
        """Tell the bootloader to jump to and start executing the new firmware."""
        self._send(bytes([CMD_GO, CMD_GO ^ 0xFF]))
        self._expect_ack("Go command")

        addr_bytes = address.to_bytes(4, byteorder="big")
        addr_checksum = self._xor_checksum(addr_bytes)
        self._send(addr_bytes + bytes([addr_checksum]))
        self._expect_ack("Go address")

    # ------------------------------------------------------------------
    # High-level: flash a whole .bin file
    # ------------------------------------------------------------------
    def flash_firmware(self, bin_path, progress_callback=None, auto_bootloader=True):
        """
        Full flash sequence: init -> mass erase -> write all chunks -> go.

        Args:
            bin_path: path to the compiled .bin firmware file
            progress_callback: optional fn(percent: float) called after each chunk
            auto_bootloader: if True (default), sends the ENTER_BOOTLOADER
                command first so no physical BOOT0/RESET press is needed.
                Set False to skip this and rely on manual BOOT0 entry
                instead - useful if the currently-flashed firmware
                predates the ENTER_BOOTLOADER handler in app.c.

        Raises:
            FlashError on any protocol failure. The chip may be left erased
            and partially written if this raises mid-flash - do not power
            cycle until you've either fixed the issue and retried, or
            re-entered bootloader mode to try again.
        """
        with open(bin_path, "rb") as f:
            firmware = f.read()

        if len(firmware) == 0:
            raise FlashError("Firmware file is empty")

        if auto_bootloader:
            self.request_bootloader_entry()

        self.enter_flash_mode()
        try:
            self.init_bootloader()
            self.get_id()          # sanity check - raises if not talking to a real bootloader
            self.mass_erase()

            total = len(firmware)
            written = 0
            address = FLASH_START_ADDRESS

            while written < total:
                chunk = firmware[written:written + WRITE_CHUNK_SIZE]
                # Pad the final chunk to a 4-byte boundary with 0xFF (erased-flash value)
                if len(chunk) % 4 != 0:
                    chunk = chunk + bytes([0xFF] * (4 - len(chunk) % 4))

                self.write_memory_chunk(address, chunk)

                written += len(firmware[written:written + WRITE_CHUNK_SIZE])
                address += len(chunk)

                if progress_callback:
                    progress_callback(min(100.0, written / total * 100))

            self.go()
        finally:
            self.exit_flash_mode()   # always restore 8N1, even if the flash failed