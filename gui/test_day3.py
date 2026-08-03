"""
Unit tests - STM32 wireless flasher protocol (backend/stm32_flasher.py)

No hardware required. Uses a FakeSocket that plays back canned bootloader
responses (ACK/NACK/data bytes) and records exactly what bytes the flasher
sends - so we can verify the actual wire protocol matches AN3155 byte for
byte, without ever touching a real chip.

Run in PyCharm: right-click -> Run 'pytest in test_unit_stm32_flasher.py'
"""

import pytest
from backend.stm32_flasher import STM32Flasher, FlashError, ACK, NACK


class FakeSocket:
    """
    Minimal stand-in for socket.socket. `responses` is the exact byte
    stream the "bootloader" will hand back, in order; sendall() records
    everything the flasher transmits so tests can assert on it.
    """

    def __init__(self, responses: bytes = b""):
        self.sent = b""
        self._responses = responses
        self._pos = 0

    def sendall(self, data: bytes):
        self.sent += data

    def recv(self, n: int) -> bytes:
        chunk = self._responses[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk  # empty bytes if exhausted -> _recv_exact will raise FlashError

    def settimeout(self, t):
        pass

    def close(self):
        pass


def make_flasher(responses: bytes) -> STM32Flasher:
    f = STM32Flasher("127.0.0.1", 5000)
    f.sock = FakeSocket(responses)
    return f


# ============================================================================
# Checksum / framing math (pure functions, no I/O)
# ============================================================================

def test_xor_checksum_matches_an3155_example():
    # AN3155 worked example: address 0x08000000 -> checksum 0x08
    addr_bytes = (0x08000000).to_bytes(4, byteorder="big")
    f = STM32Flasher("x", 1)
    assert f._xor_checksum(addr_bytes) == 0x08


def test_command_complement_byte():
    from backend.stm32_flasher import CMD_GO
    assert (CMD_GO ^ 0xFF) == 0xDE


# ============================================================================
# init_bootloader()
# ============================================================================

def test_init_bootloader_sends_sync_byte_and_accepts_ack():
    flasher = make_flasher(bytes([ACK]))
    flasher.init_bootloader()
    assert flasher.sock.sent == bytes([0x7F])


def test_init_bootloader_tolerates_nack_on_second_sync():
    # Some ROM versions NACK if already synced - must not raise
    flasher = make_flasher(bytes([NACK]))
    flasher.init_bootloader()  # should not raise


def test_init_bootloader_raises_on_truly_unexpected_byte():
    # A garbage byte (neither ACK nor NACK) must NOT be silently swallowed -
    # this was a real bug: init previously caught and ignored ALL errors,
    # masking genuine corruption instead of just tolerating a re-sync NACK.
    flasher = make_flasher(bytes([0x3F]))
    with pytest.raises(FlashError):
        flasher.init_bootloader()


# ============================================================================
# enter_flash_mode() / exit_flash_mode() - diagnostic checkpoint
# ============================================================================

def test_enter_flash_mode_succeeds_with_correct_confirmation():
    from backend.stm32_flasher import MODE_ENTER_CONFIRM
    flasher = make_flasher(bytes([MODE_ENTER_CONFIRM]))
    flasher.enter_flash_mode()  # should not raise
    assert flasher.sock.sent == bytes([0xFE, 0xED, 0xBE, 0xEF])


def test_enter_flash_mode_raises_clear_error_if_marker_not_recognized():
    # This is exactly the real-world bug encountered: ESP32 running old
    # firmware forwards the marker as raw data instead of confirming it,
    # so Python reads back whatever garbage the STM32 happened to send.
    flasher = make_flasher(bytes([0xF1]))  # some unrelated byte, not MODE_ENTER_CONFIRM
    with pytest.raises(FlashError, match="outdated relay firmware"):
        flasher.enter_flash_mode()


def test_enter_flash_mode_raises_clear_error_if_no_response_at_all():
    flasher = make_flasher(b"")  # nothing comes back - connection produced no data
    with pytest.raises(FlashError, match="did not respond to flash-mode marker"):
        flasher.enter_flash_mode()


def test_exit_flash_mode_never_raises_even_on_garbage_or_no_response():
    # Best-effort cleanup - must not raise regardless of what comes back
    flasher = make_flasher(b"")
    flasher.exit_flash_mode()  # should not raise

    flasher2 = make_flasher(bytes([0xAB]))
    flasher2.exit_flash_mode()  # should not raise


# ============================================================================
# get_id()
# ============================================================================

def test_get_id_sends_correct_command_and_parses_response():
    # ACK, then length=1, then 2 ID bytes (F401 product ID example), then ACK
    responses = bytes([ACK, 0x01, 0x04, 0x30, ACK])
    flasher = make_flasher(responses)
    product_id = flasher.get_id()

    assert flasher.sock.sent == bytes([0x02, 0x02 ^ 0xFF])
    assert product_id == bytes([0x04, 0x30])


def test_get_id_raises_on_nack():
    flasher = make_flasher(bytes([NACK]))
    with pytest.raises(FlashError):
        flasher.get_id()


# ============================================================================
# mass_erase()
# ============================================================================

def test_mass_erase_sends_extended_erase_global_command():
    responses = bytes([ACK, ACK])  # command ACK, erase-payload ACK
    flasher = make_flasher(responses)
    flasher.mass_erase()

    expected = bytes([0x44, 0x44 ^ 0xFF]) + bytes([0xFF, 0xFF, 0xFF ^ 0xFF])
    assert flasher.sock.sent == expected


def test_mass_erase_raises_on_nack():
    flasher = make_flasher(bytes([NACK]))
    with pytest.raises(FlashError):
        flasher.mass_erase()


# ============================================================================
# write_memory_chunk()
# ============================================================================

def test_write_memory_chunk_wire_format():
    responses = bytes([ACK, ACK, ACK])  # command, address, data
    flasher = make_flasher(responses)

    data = bytes([0x01, 0x02, 0x03, 0x04])
    flasher.write_memory_chunk(0x08000000, data)

    addr_bytes = (0x08000000).to_bytes(4, byteorder="big")
    addr_checksum = flasher._xor_checksum(addr_bytes)
    n_minus_1 = len(data) - 1
    data_payload = bytes([n_minus_1]) + data
    data_checksum = flasher._xor_checksum(data_payload)

    expected = (
        bytes([0x31, 0x31 ^ 0xFF])
        + addr_bytes + bytes([addr_checksum])
        + data_payload + bytes([data_checksum])
    )
    assert flasher.sock.sent == expected


def test_write_memory_chunk_rejects_oversize_payload():
    flasher = make_flasher(bytes([ACK, ACK, ACK]))
    with pytest.raises(ValueError):
        flasher.write_memory_chunk(0x08000000, bytes(300))  # > 256 byte limit


# ============================================================================
# go()
# ============================================================================

def test_go_sends_correct_command_and_address():
    responses = bytes([ACK, ACK])
    flasher = make_flasher(responses)
    flasher.go(0x08000000)

    addr_bytes = (0x08000000).to_bytes(4, byteorder="big")
    addr_checksum = flasher._xor_checksum(addr_bytes)
    expected = bytes([0x21, 0x21 ^ 0xFF]) + addr_bytes + bytes([addr_checksum])
    assert flasher.sock.sent == expected


# ============================================================================
# flash_firmware() - full end-to-end sequence against a synthetic .bin
# ============================================================================

def test_flash_firmware_full_sequence(tmp_path):
    # 10-byte fake firmware -> gets padded to 12 bytes (4-byte aligned) in one chunk
    firmware = bytes(range(10))
    bin_path = tmp_path / "firmware.bin"
    bin_path.write_bytes(firmware)

    # Response order: enter-flash-mode confirm(1) + init(1) + get_id(ACK,len,id,ACK = 4)
    #                 + mass_erase(2) + write_chunk(3) + go(2) + exit-flash-mode confirm(1)
    from backend.stm32_flasher import MODE_ENTER_CONFIRM, MODE_EXIT_CONFIRM
    responses = bytes([
        MODE_ENTER_CONFIRM,           # enter_flash_mode
        ACK,                          # init
        ACK, 0x01, 0x04, 0x30, ACK,   # get_id
        ACK, ACK,                     # mass_erase
        ACK, ACK, ACK,                # write_memory_chunk (only chunk needed, 10 <= 256)
        ACK, ACK,                     # go
        MODE_EXIT_CONFIRM,            # exit_flash_mode
    ])
    flasher = make_flasher(responses)

    progress_calls = []
    flasher.flash_firmware(str(bin_path), progress_callback=progress_calls.append)

    assert progress_calls, "expected at least one progress callback"
    assert progress_calls[-1] == 100.0

    from backend.stm32_flasher import ENTER_FLASH_MODE, EXIT_FLASH_MODE

    # Sent stream should start with the flash-mode marker, then sync byte,
    # contain the write-memory command byte (0x31), and end with the
    # exit-flash-mode marker.
    assert flasher.sock.sent.startswith(ENTER_FLASH_MODE)
    assert flasher.sock.sent[len(ENTER_FLASH_MODE)] == 0x7F
    assert 0x31 in flasher.sock.sent
    assert flasher.sock.sent.endswith(EXIT_FLASH_MODE)


def test_flash_firmware_raises_on_empty_file(tmp_path):
    bin_path = tmp_path / "empty.bin"
    bin_path.write_bytes(b"")

    flasher = make_flasher(bytes([ACK]))
    with pytest.raises(FlashError):
        flasher.flash_firmware(str(bin_path))


def test_flash_firmware_raises_cleanly_if_bootloader_stops_responding():
    # Response stream runs out mid-sequence (simulates dropped connection)
    responses = bytes([ACK])  # only enough for init, nothing after
    firmware = bytes(range(10))

    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".bin")
    os.write(fd, firmware)
    os.close(fd)

    try:
        flasher = make_flasher(responses)
        with pytest.raises(FlashError):
            flasher.flash_firmware(path)
    finally:
        os.unlink(path)