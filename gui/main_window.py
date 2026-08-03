# ============================================================================
# Main Window - Tkinter GUI (Day 3 + Day 4 Visualization + Day 5 Logging)
# ============================================================================
# Two-way communication with variable editor, latency display, event log,
# session recording/CSV export, and embedded real-time graphs.

import tkinter as tk
from tkinter import ttk, simpledialog, messagebox, filedialog
from datetime import datetime
from backend.tcp_client import DebugBridge, ESP32_IP
from backend.data_store import variables, events
from backend import data_store
from backend import csv_logger
from backend.parser import parse_packet, get_latest_latency, get_average_latency
from gui.visualization import VisualizationPanel
from gui.flash_dialog import FlashDialog


# ============================================================================
# MainWindow CLASS
# ============================================================================

class MainWindow:
    """
    Tkinter GUI for STM32 monitoring, variable control, live graphing,
    and session recording.

    Day 3 (intact):
    - Live variable table (double-click to edit), event log, latency
      indicator, command entry/send, connection status.

    Day 4:
    - "📊 View Graphs" toggle button embeds the real-time graph panel
      (individual + combined variable graphs) directly in the main window.

    Day 5:
    - Session Recording frame: Start/Stop recording, Export to CSV.
    """

    def __init__(self, root):
        """Initialize the main window"""
        self.root = root
        self.root.title("STM32 Debug Bridge - Day 3+4+5")
        self.root.geometry("1100x900")
        self.root.resizable(True, True)

        self.graph_panel = None
        self.graph_visible = False

        # Initialize TCP connection
        self.bridge = DebugBridge()
        self.bridge.connect()

        # ====================================================================
        # TOP FRAME - Connection status and title
        # ====================================================================
        top_frame = tk.Frame(root, bg="#2c3e50", height=70)
        top_frame.pack(fill=tk.X, padx=0, pady=0)
        top_frame.pack_propagate(False)

        title_label = tk.Label(
            top_frame,
            text="STM32 Debug Bridge - Day 3+4+5",
            font=("Arial", 16, "bold"),
            bg="#2c3e50",
            fg="white"
        )
        title_label.pack(side=tk.LEFT, padx=20, pady=10)

        self.status_label = tk.Label(
            top_frame,
            text="● Disconnected",
            font=("Arial", 12),
            bg="#2c3e50",
            fg="red"
        )
        self.status_label.pack(side=tk.RIGHT, padx=20, pady=10)

        self.latency_label = tk.Label(
            top_frame,
            text="Latency: -- ms",
            font=("Arial", 11),
            bg="#2c3e50",
            fg="yellow"
        )
        self.latency_label.pack(side=tk.RIGHT, padx=20, pady=10)

        # ====================================================================
        # COMMAND FRAME - Text input + Send button + View Graphs button
        # ====================================================================
        cmd_frame = tk.LabelFrame(
            root,
            text="Send Command",
            font=("Arial", 11, "bold"),
            padx=10,
            pady=10
        )
        cmd_frame.pack(fill=tk.X, padx=10, pady=10)

        self.cmd_entry = tk.Entry(cmd_frame, font=("Courier", 10), width=50)
        self.cmd_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.cmd_entry.bind("<Return>", self.on_command_enter)

        send_btn = tk.Button(
            cmd_frame,
            text="Send Command",
            font=("Arial", 10, "bold"),
            bg="#27ae60",
            fg="white",
            padx=15,
            command=self.send_command
        )
        send_btn.pack(side=tk.LEFT, padx=5)

        self.graph_btn = tk.Button(
            cmd_frame,
            text="📊 View Graphs",
            font=("Arial", 10, "bold"),
            bg="#8e44ad",
            fg="white",
            padx=15,
            command=self.toggle_graphs
        )
        self.graph_btn.pack(side=tk.LEFT, padx=5)

        self.demo_btn = tk.Button(
            cmd_frame,
            text="▶ Run Demo",
            font=("Arial", 10, "bold"),
            bg="#2980b9",
            fg="white",
            padx=15,
            command=self.run_demo_sequence
        )
        self.demo_btn.pack(side=tk.LEFT, padx=5)

        self.flash_btn = tk.Button(
            cmd_frame,
            text="⚡ Flash Firmware",
            font=("Arial", 10, "bold"),
            bg="#e67e22",
            fg="white",
            padx=15,
            command=self.open_flash_dialog
        )
        self.flash_btn.pack(side=tk.LEFT, padx=5)

        help_label = tk.Label(
            cmd_frame,
            text="Examples: SET:temp=25  |  GET:temp  |  LIST",
            font=("Arial", 9),
            fg="#7f8c8d"
        )
        help_label.pack(side=tk.LEFT, padx=10)

        # ====================================================================
        # RECORDING FRAME - Day 5: Start/Stop recording + Export to CSV
        # ====================================================================
        rec_frame = tk.LabelFrame(
            root,
            text="Session Recording",
            font=("Arial", 11, "bold"),
            padx=10,
            pady=10
        )
        rec_frame.pack(fill=tk.X, padx=10, pady=10)

        self.record_btn = tk.Button(
            rec_frame,
            text="⏺ Start Recording",
            font=("Arial", 10, "bold"),
            bg="#c0392b",
            fg="white",
            padx=15,
            command=self.toggle_recording
        )
        self.record_btn.pack(side=tk.LEFT, padx=5)

        self.export_btn = tk.Button(
            rec_frame,
            text="⬇ Export to CSV",
            font=("Arial", 10, "bold"),
            bg="#8e44ad",
            fg="white",
            padx=15,
            command=self.export_csv
        )
        self.export_btn.pack(side=tk.LEFT, padx=5)

        self.record_status_label = tk.Label(
            rec_frame,
            text="⚪ Not Recording   |   Samples: 0",
            font=("Arial", 10),
            fg="#7f8c8d"
        )
        self.record_status_label.pack(side=tk.LEFT, padx=15)

        # ====================================================================
        # GRAPH FRAME - Day 4: embedded, toggled visible/hidden (not packed yet)
        # ====================================================================
        self.graph_frame = tk.Frame(root)
        # Not packed here - toggle_graphs() packs/unpacks it on demand,
        # inserted before the variable table so layout order stays sensible.

        # ====================================================================
        # VARIABLE TABLE FRAME
        # ====================================================================
        self.var_frame = tk.LabelFrame(
            root,
            text="Variables (Double-click to edit)",
            font=("Arial", 12, "bold"),
            padx=10,
            pady=10
        )
        self.var_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.var_tree = ttk.Treeview(
            self.var_frame,
            columns=("Name", "Value", "Status", "Updated"),
            height=8,
            show="tree headings"
        )

        self.var_tree.heading("#0", text="ID")
        self.var_tree.heading("Name", text="Name")
        self.var_tree.heading("Value", text="Value")
        self.var_tree.heading("Status", text="Status")
        self.var_tree.heading("Updated", text="Last Updated")

        self.var_tree.column("#0", width=40)
        self.var_tree.column("Name", width=120)
        self.var_tree.column("Value", width=100)
        self.var_tree.column("Status", width=100)
        self.var_tree.column("Updated", width=180)

        var_scrollbar = ttk.Scrollbar(self.var_frame, orient=tk.VERTICAL, command=self.var_tree.yview)
        self.var_tree.configure(yscroll=var_scrollbar.set)

        self.var_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        var_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.var_tree.bind("<Double-1>", self.on_variable_double_click)

        # ====================================================================
        # EVENT LOG FRAME
        # ====================================================================
        event_frame = tk.LabelFrame(
            root,
            text="Event Log",
            font=("Arial", 12, "bold"),
            padx=10,
            pady=10
        )
        event_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.event_text = tk.Text(
            event_frame,
            height=6,
            width=80,
            font=("Courier", 9),
            bg="#f5f5f5",
            state=tk.DISABLED
        )

        event_scrollbar = ttk.Scrollbar(event_frame, orient=tk.VERTICAL, command=self.event_text.yview)
        self.event_text.configure(yscroll=event_scrollbar.set)

        self.event_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        event_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # ====================================================================
        # STATUS BAR
        # ====================================================================
        self.status_bar = tk.Label(
            root, text="Ready", bd=1, relief=tk.SUNKEN, anchor=tk.W,
            font=("Arial", 9), padx=5
        )
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        # ====================================================================
        # Start GUI refresh timer
        # ====================================================================
        self.refresh_gui()

    # ========================================================================
    # GUI REFRESH - Update display every 100ms
    # ========================================================================
    def refresh_gui(self):
        """Refresh GUI with latest data"""

        if self.bridge.connected and self.bridge.stm32_alive:
            self.status_label.config(text="● Connected", fg="green")
            self.status_bar.config(text="Connected to ESP32 | STM32 alive")
        else:
            self.status_label.config(text="● Disconnected", fg="red")
            self.status_bar.config(text="Disconnected - waiting for ESP32/STM32...")

        if self.bridge.latest_latency_ms is not None:
            latency_text = f"Latency: {self.bridge.latest_latency_ms:.1f} ms"
            self.latency_label.config(text=latency_text, fg="#f39c12")
        else:
            self.latency_label.config(text="Latency: -- ms", fg="yellow")

        self.update_variable_table()
        self.update_event_log()
        self.update_recording_status()

        self.root.after(100, self.refresh_gui)

    def update_variable_table(self):
        """Refresh variable table from data_store"""
        for item in self.var_tree.get_children():
            self.var_tree.delete(item)

        for idx, (name, data) in enumerate(variables.items(), 1):
            value = data.get("value", "N/A")
            status = data.get("status", "pending")
            timestamp = data.get("timestamp", "N/A")
            self.var_tree.insert("", "end", text=str(idx), values=(name, value, status, timestamp))

    def update_event_log(self):
        """Refresh event log from data_store"""
        self.event_text.config(state=tk.NORMAL)
        self.event_text.delete("1.0", tk.END)

        for event_data in events:
            event_msg = event_data.get("event", "N/A")
            event_time = event_data.get("timestamp", "N/A")
            self.event_text.insert(tk.END, f"[{event_time}] {event_msg}\n")

        self.event_text.see(tk.END)
        self.event_text.config(state=tk.DISABLED)

    # ========================================================================
    # COMMAND HANDLING
    # ========================================================================
    def send_command(self):
        """Send command from text entry"""
        command = self.cmd_entry.get().strip()
        if command:
            self.bridge.send_command(command)
            self.cmd_entry.delete(0, tk.END)
        else:
            messagebox.showwarning("Empty Command", "Please enter a command")

    def on_command_enter(self, event):
        """Handle Enter key in command entry"""
        self.send_command()

    # ========================================================================
    # DAY 4 - EMBEDDED GRAPH TOGGLE
    # ========================================================================
    def toggle_graphs(self):
        """Show/hide the embedded live graph panel (pauses updates when hidden)."""
        if not self.graph_visible:
            if self.graph_panel is None:
                self.graph_panel = VisualizationPanel(self.graph_frame)
                self.graph_panel.pack(fill=tk.BOTH, expand=True)
            else:
                self.graph_panel.start()

            self.graph_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10, before=self.var_frame)
            self.graph_btn.config(text="📉 Hide Graphs")
            self.graph_visible = True
        else:
            self.graph_panel.stop()
            self.graph_frame.pack_forget()
            self.graph_btn.config(text="📊 View Graphs")
            self.graph_visible = False

    # ========================================================================
    # WIRELESS FLASHING - opens the flash dialog (handles disconnect/reconnect)
    # ========================================================================
    def open_flash_dialog(self):
        FlashDialog(self.root, self.bridge)

    # ========================================================================
    # DEMO SEQUENCE - fires real commands through the live connection so you
    # can watch the table/graph/event log populate, without a separate script
    # ========================================================================
    DEMO_SEQUENCE = [
        ("SET:temperature=25", 400),
        ("SET:humidity=65", 400),
        ("SET:pressure=1013", 400),
        ("GET:temperature", 800),
        ("LIST", 1200),
        ("SET:graph_probe=0", 300),
        ("SET:graph_probe=10", 300),
        ("SET:graph_probe=20", 300),
        ("SET:graph_probe=30", 300),
        ("SET:graph_probe=40", 800),
    ]

    def run_demo_sequence(self):
        """Send a real command sequence through the live bridge, one at a
        time, so the table/event log/graph visibly fill in as it runs."""
        if not (self.bridge.connected and self.bridge.stm32_alive):
            messagebox.showwarning(
                "Not Ready",
                "Not connected/alive yet. Wait for '● Connected' at the top, then try again."
            )
            return

        if not self.graph_visible:
            self.toggle_graphs()

        self.demo_btn.config(state=tk.DISABLED, text="Running Demo...")

        def send_next(i=0):
            if i >= len(self.DEMO_SEQUENCE):
                self.status_bar.config(text="Demo complete. Table/log/graph above are live data from your STM32.")
                self.demo_btn.config(state=tk.NORMAL, text="▶ Run Demo")
                return

            command, delay = self.DEMO_SEQUENCE[i]
            self.bridge.send_command(command)
            self.status_bar.config(
                text=f"Demo: sent '{command}'  ({i + 1}/{len(self.DEMO_SEQUENCE)})"
            )
            self.root.after(delay, send_next, i + 1)

        send_next()

    # ========================================================================
    # SESSION RECORDING - Day 5: Start/Stop + Export to CSV
    # ========================================================================
    def toggle_recording(self):
        """Start or stop a recording session."""
        if not data_store.recording:
            data_store.start_recording(ip_address=ESP32_IP)
            self.record_btn.config(text="⏹ Stop Recording", bg="#27ae60")
        else:
            data_store.stop_recording()
            self.record_btn.config(text="⏺ Start Recording", bg="#c0392b")
            messagebox.showinfo(
                "Recording Stopped",
                f"Recorded {data_store.sample_count} samples.\n"
                f"Click 'Export to CSV' to save this session."
            )

    def update_recording_status(self):
        """Refresh the recording status label (called every GUI tick)."""
        status = data_store.get_recording_status()
        if status["recording"]:
            self.record_status_label.config(
                text=f"🔴 Recording   |   Samples: {status['sample_count']}   "
                     f"|   Started: {status['session_start']}",
                fg="#c0392b"
            )
        else:
            samples = status["sample_count"]
            self.record_status_label.config(
                text=f"⚪ Not Recording   |   Samples: {samples}",
                fg="#7f8c8d"
            )

    def export_csv(self):
        """Export the current/last session log to a CSV file chosen by the user."""
        if data_store.sample_count == 0:
            messagebox.showwarning("No Data", "No recorded samples to export yet. "
                                                "Start a recording first.")
            return

        filepath = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"stm32_session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            title="Export Session to CSV"
        )
        if not filepath:
            return  # user cancelled

        try:
            row_count = csv_logger.export_to_csv(filepath)
            messagebox.showinfo("Export Complete", f"Exported {row_count} samples to:\n{filepath}")
        except Exception as e:
            messagebox.showerror("Export Failed", f"Could not export CSV:\n{e}")

    # ========================================================================
    # VARIABLE EDITOR - Double-click to edit
    # ========================================================================
    def on_variable_double_click(self, event):
        """Handle double-click on variable row"""
        item = self.var_tree.selection()[0] if self.var_tree.selection() else None
        if not item:
            return

        values = self.var_tree.item(item, "values")
        if not values:
            return

        var_name = values[0]
        current_value = values[1]

        dialog = VariableEditorDialog(self.root, var_name, current_value)

        if dialog.result is not None:
            command = f"SET:{var_name}={dialog.result}"
            self.bridge.send_command(command)
            variables[var_name]["value"] = str(dialog.result)
            variables[var_name]["status"] = "pending"


# ============================================================================
# Variable Editor Dialog - Popup for editing variables
# ============================================================================

class VariableEditorDialog(tk.simpledialog.Dialog):
    """Dialog for editing variable values"""

    def __init__(self, parent, var_name, current_value):
        self.var_name = var_name
        self.current_value = current_value
        self.result = None
        super().__init__(parent, title=f"Edit Variable: {var_name}")

    def body(self, master):
        tk.Label(master, text="Variable Name:").grid(row=0, column=0, sticky=tk.W, padx=10, pady=5)
        tk.Label(master, text=self.var_name, font=("Arial", 11, "bold")).grid(
            row=0, column=1, sticky=tk.W, padx=10, pady=5)

        tk.Label(master, text="Current Value:").grid(row=1, column=0, sticky=tk.W, padx=10, pady=5)
        tk.Label(master, text=str(self.current_value), font=("Courier", 10)).grid(
            row=1, column=1, sticky=tk.W, padx=10, pady=5)

        tk.Label(master, text="New Value:").grid(row=2, column=0, sticky=tk.W, padx=10, pady=5)
        self.entry = tk.Entry(master, font=("Arial", 11), width=20)
        self.entry.grid(row=2, column=1, sticky=tk.EW, padx=10, pady=5)
        self.entry.insert(0, str(self.current_value))
        self.entry.focus()

        return self.entry

    def buttonbox(self):
        box = tk.Frame(self)
        box.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=5)

        ok_btn = tk.Button(box, text="Send to STM32", command=self.ok, bg="#27ae60", fg="white")
        ok_btn.pack(side=tk.LEFT, padx=5)

        cancel_btn = tk.Button(box, text="Cancel", command=self.cancel, bg="#e74c3c", fg="white")
        cancel_btn.pack(side=tk.LEFT, padx=5)

    def ok(self, event=None):
        try:
            value = self.entry.get().strip()
            if not value:
                messagebox.showerror("Empty Value", "Please enter a value")
                return
            int(value)
            self.result = value
            self.destroy()
        except ValueError:
            messagebox.showerror("Invalid Value", "Please enter a valid number")


# ============================================================================
# Main Program
# ============================================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = MainWindow(root)
    root.mainloop()