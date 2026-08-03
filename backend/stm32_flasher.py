# ============================================================================
# STM32 Wireless Flasher - UART Bootloader Protocol (AN3155)
# ============================================================================
# Talks to the STM32F401's built-in ROM bootloader over the SAME UART link
# used for debug traffic, relayed wirelessly through the ESP32's raw-byte
# passthrough (backend/tcp_client.py's DebugBridge cannot be connected at
# the same time - the ESP32 only accepts ONE TCP client).
#
# PREREQUISITE (manual, physical): put the STM32 in bootloader mode before
# calling flash_firmware():
#   1. Hold BOOT0 button
#   2. Tap RESET button (while still holding BOOT0)
#   3. Release BOOT0
# The chip is now running its ROM bootloader and listening on USART1,
# waiting for the 0x7F sync byte below.
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
WRITE_CHUNK_SIZE = 256       # max payload bytes per Write Memory command
SOCKET_TIMEOUT_S = 5

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
    # Flash-mode UART parity switching (ESP32 relay control, not AN3155)
    # ------------------------------------------------------------------
    def enter_flash_mode(self):
        """Tell the ESP32 relay to switch the STM32 UART to 8E1 (even parity),
        as required by the ROM bootloader. Must be called before init_bootloader().

        Raises FlashError immediately (with a clear diagnosis) if the ESP32
        doesn't confirm the switch - this means it's running an older relay
        firmware without flash-mode marker support, NOT a bootloader issue.
        """
        self._send(ENTER_FLASH_MODE)
        try:
            resp = self._recv_exact(1)[0]
        except FlashError:
            raise FlashError(
                "ESP32 did not respond to flash-mode marker. "
                "Check the ESP32 is running the latest relay firmware "
                "with ENTER_FLASH_MODE marker detection."
            )
        if resp != MODE_ENTER_CONFIRM:
            raise FlashError(
                f"ESP32 sent 0x{resp:02X} instead of the flash-mode confirmation "
                f"(0x{MODE_ENTER_CONFIRM:02X}). The marker leaked through to the "
                f"STM32 instead of being intercepted - the ESP32 is likely running "
                f"outdated relay firmware without marker detection."
            )
        time.sleep(MODE_SWITCH_SETTLE_S)

    def exit_flash_mode(self):
        """Tell the ESP32 relay to switch the STM32 UART back to 8N1 (normal
        debug traffic framing). Best-effort - does not raise, since this is
        typically called during cleanup/error handling."""
        try:
            self._send(EXIT_FLASH_MODE)
            self._recv_exact(1)  # drain the confirmation byte if present, ignore its value
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
        self._send(payload + bytes([checksum]))
        self._expect_ack("Extended Erase (mass erase)")

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
        self._send(payload + bytes([checksum]))
        self._expect_ack("Write Memory data")

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
    def flash_firmware(self, bin_path, progress_callback=None):
        """
        Full flash sequence: init -> mass erase -> write all chunks -> go.

        Args:
            bin_path: path to the compiled .bin firmware file
            progress_callback: optional fn(percent: float) called after each chunk

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