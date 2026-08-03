# ============================================================================
# Firmware Flash Dialog - Wireless STM32 Flashing UI
# ============================================================================
# Standalone Toplevel dialog: pick a .bin, confirm the manual BOOT0/RESET
# step, then flashes over the ESP32 relay using backend/stm32_flasher.py.
#
# IMPORTANT: this dialog needs the ESP32's TCP port to itself. If the main
# window's DebugBridge is connected, this dialog disconnects it first and
# reconnects it afterward automatically.

import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from backend.stm32_flasher import STM32Flasher, FlashError
from backend.tcp_client import ESP32_IP, ESP32_PORT


class FlashDialog(tk.Toplevel):
    """
    Usage from main_window.py:
        FlashDialog(self.root, self.bridge)

    `bridge` is the app's live DebugBridge - passed in so this dialog can
    disconnect it before flashing (ESP32 only accepts one TCP client) and
    reconnect it afterward.
    """

    def __init__(self, parent, bridge):
        super().__init__(parent)
        self.bridge = bridge
        self.bin_path = None

        self.title("Wireless Firmware Flash")
        self.geometry("480x360")
        self.resizable(False, False)

        # ------------------------------------------------------------------
        # Step 1: file picker
        # ------------------------------------------------------------------
        step1 = tk.LabelFrame(self, text="1. Select firmware", font=("Arial", 10, "bold"),
                               padx=10, pady=10)
        step1.pack(fill=tk.X, padx=10, pady=(10, 5))

        self.file_label = tk.Label(step1, text="No file selected", fg="#7f8c8d", anchor="w")
        self.file_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Button(step1, text="Browse .bin...", command=self._pick_file).pack(side=tk.RIGHT)

        # ------------------------------------------------------------------
        # Step 2: manual bootloader entry reminder
        # ------------------------------------------------------------------
        step2 = tk.LabelFrame(self, text="2. Enter bootloader mode (manual, on the board)",
                               font=("Arial", 10, "bold"), padx=10, pady=10)
        step2.pack(fill=tk.X, padx=10, pady=5)

        tk.Label(
            step2, justify=tk.LEFT, font=("Arial", 9),
            text="  1. Hold BOOT0\n  2. Tap RESET (while still holding BOOT0)\n"
                 "  3. Release BOOT0\n"
                 "The board is now running its bootloader and will NOT respond\n"
                 "to normal debug commands until it's flashed and reset again."
        ).pack(anchor="w")

        # ------------------------------------------------------------------
        # Step 3: flash
        # ------------------------------------------------------------------
        step3 = tk.LabelFrame(self, text="3. Flash", font=("Arial", 10, "bold"), padx=10, pady=10)
        step3.pack(fill=tk.X, padx=10, pady=5)

        self.flash_btn = tk.Button(
            step3, text="⚡ Flash Firmware", font=("Arial", 10, "bold"),
            bg="#e67e22", fg="white", command=self._start_flash
        )
        self.flash_btn.pack(fill=tk.X)

        self.progress = ttk.Progressbar(step3, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X, pady=(10, 0))

        self.status_label = tk.Label(step3, text="Idle", fg="#7f8c8d", anchor="w")
        self.status_label.pack(fill=tk.X, pady=(5, 0))

    # ------------------------------------------------------------------
    def _pick_file(self):
        path = filedialog.askopenfilename(
            title="Select firmware .bin",
            filetypes=[("Binary firmware", "*.bin"), ("All files", "*.*")]
        )
        if path:
            self.bin_path = path
            self.file_label.config(text=path.split("/")[-1].split("\\")[-1], fg="black")

    def _set_status(self, text, color="#7f8c8d"):
        self.status_label.config(text=text, fg=color)

    def _start_flash(self):
        if not self.bin_path:
            messagebox.showwarning("No File", "Select a .bin file first.")
            return

        confirmed = messagebox.askyesno(
            "Confirm",
            "Have you already put the board in bootloader mode "
            "(BOOT0 held + RESET tapped + BOOT0 released)?\n\n"
            "If not, do that now before clicking Yes."
        )
        if not confirmed:
            return

        self.flash_btn.config(state=tk.DISABLED)
        self.progress["value"] = 0
        self._set_status("Disconnecting debug session...")

        thread = threading.Thread(target=self._flash_worker, daemon=True)
        thread.start()

    def _flash_worker(self):
        # The ESP32 only accepts one TCP client - free up the port first.
        was_connected = self.bridge.connected
        if was_connected:
            self.bridge.disconnect()

        flasher = STM32Flasher(ESP32_IP, ESP32_PORT)
        try:
            self.after(0, lambda: self._set_status("Connecting to bootloader..."))
            flasher.connect()

            def on_progress(pct):
                self.after(0, lambda: self._update_progress(pct))

            self.after(0, lambda: self._set_status("Erasing and flashing..."))
            flasher.flash_firmware(self.bin_path, progress_callback=on_progress)

            self.after(0, self._flash_success)

        except (FlashError, OSError, TimeoutError) as e:
            error_msg = str(e)
            self.after(0, lambda: self._flash_failed(error_msg))

        finally:
            flasher.disconnect()
            if was_connected:
                self.after(500, self.bridge.connect)  # reconnect debug session

    def _update_progress(self, pct):
        self.progress["value"] = pct
        self._set_status(f"Writing... {pct:.0f}%")

    def _flash_success(self):
        self.progress["value"] = 100
        self._set_status("✅ Flash complete - board reset and running new firmware", "#27ae60")
        self.flash_btn.config(state=tk.NORMAL)
        messagebox.showinfo("Success", "Firmware flashed successfully.")

    def _flash_failed(self, error_msg):
        self._set_status(f"❌ Flash failed: {error_msg}", "#e74c3c")
        self.flash_btn.config(state=tk.NORMAL)
        messagebox.showerror(
            "Flash Failed",
            f"{error_msg}\n\n"
            "The board may be left partially erased. Re-enter bootloader mode "
            "(BOOT0 + RESET) and try again before power-cycling."
        )