# ============================================================================
# Main Window - Tkinter GUI Interface
# ============================================================================
# Live monitoring dashboard for STM32 variables and events

import tkinter as tk  # Tkinter for GUI
from tkinter import ttk  # Tkinter themed widgets (TreeView)
from datetime import datetime  # For timestamps
from backend.tcp_client import DebugBridge  # TCP communication
from backend.data_store import variables, events  # Data storage
from backend.parser import parse_packet  # Packet parsing


# ============================================================================
# MainWindow CLASS - Tkinter GUI
# ============================================================================

class MainWindow:
    """
    Tkinter GUI window for live monitoring of STM32 data.

    Features:
    - Live variable table (ID, Name, Value, Last Updated)
    - Event log (scrollable list with timestamps)
    - Connection status indicator (Connected/Disconnected)
    - Refresh rate: 100ms updates
    """

    def __init__(self, root):
        """
        Initialize the main window and create all GUI components.

        Args:
            root: Tkinter root window object
        """
        self.root = root
        self.root.title("STM32 Live Monitor")
        self.root.geometry("900x700")
        self.root.resizable(True, True)

        # ====================================================================
        # Initialize TCP connection
        # ====================================================================
        self.bridge = DebugBridge()
        self.bridge.connect()

        # ====================================================================
        # Create main frames
        # ====================================================================

        # Top frame - Connection status and title
        top_frame = tk.Frame(root, bg="#2c3e50", height=60)
        top_frame.pack(fill=tk.X, padx=0, pady=0)
        top_frame.pack_propagate(False)

        # Title label
        title_label = tk.Label(
            top_frame,
            text="STM32 Live Monitor",
            font=("Arial", 16, "bold"),
            bg="#2c3e50",
            fg="white"
        )
        title_label.pack(side=tk.LEFT, padx=20, pady=10)

        # Connection status indicator (will update every 100ms)
        self.status_label = tk.Label(
            top_frame,
            text="● Disconnected",
            font=("Arial", 12),
            bg="#2c3e50",
            fg="red"
        )
        self.status_label.pack(side=tk.RIGHT, padx=20, pady=10)

        # ====================================================================
        # Middle frame - Variable table
        # ====================================================================

        var_frame = tk.LabelFrame(
            root,
            text="Variables",
            font=("Arial", 12, "bold"),
            padx=10,
            pady=10
        )
        var_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Create TreeView for variables
        # Columns: ID, Name, Value, Last Updated
        self.var_tree = ttk.Treeview(
            var_frame,
            columns=("Name", "Value", "Updated"),
            height=8,
            show="tree headings"
        )

        # Define column headings and widths
        self.var_tree.heading("#0", text="ID")
        self.var_tree.heading("Name", text="Name")
        self.var_tree.heading("Value", text="Value")
        self.var_tree.heading("Updated", text="Last Updated")

        self.var_tree.column("#0", width=50)
        self.var_tree.column("Name", width=150)
        self.var_tree.column("Value", width=150)
        self.var_tree.column("Updated", width=200)

        # Scrollbar for variable table
        var_scrollbar = ttk.Scrollbar(var_frame, orient=tk.VERTICAL, command=self.var_tree.yview)
        self.var_tree.configure(yscroll=var_scrollbar.set)

        # Pack table and scrollbar
        self.var_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        var_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # ====================================================================
        # Bottom frame - Event log
        # ====================================================================

        event_frame = tk.LabelFrame(
            root,
            text="Event Log",
            font=("Arial", 12, "bold"),
            padx=10,
            pady=10
        )
        event_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Create Text widget for events (with scrollbar)
        self.event_text = tk.Text(
            event_frame,
            height=8,
            width=80,
            font=("Courier", 10),
            bg="#f5f5f5",
            state=tk.DISABLED  # Read-only
        )

        # Scrollbar for event log
        event_scrollbar = ttk.Scrollbar(event_frame, orient=tk.VERTICAL, command=self.event_text.yview)
        self.event_text.configure(yscroll=event_scrollbar.set)

        # Pack event log and scrollbar
        self.event_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        event_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # ====================================================================
        # Initialize variable counter for IDs
        # ====================================================================
        self.var_counter = 0

        # ====================================================================
        # Start refresh timer (100ms updates)
        # ====================================================================
        self.refresh_gui()

    def refresh_gui(self):
        """
        Refresh GUI every 100ms with latest data from data_store.
        Updates:
        1. Connection status
        2. Variable table
        3. Event log
        """

        # ====================================================================
        # Update connection status
        # ====================================================================
        if self.bridge.connected:
            self.status_label.config(text="● Connected", fg="green")
        else:
            self.status_label.config(text="● Disconnected", fg="red")

        # ====================================================================
        # Update variable table from data_store
        # ====================================================================
        # Clear existing items
        for item in self.var_tree.get_children():
            self.var_tree.delete(item)

        # Add variables from data_store.variables dict
        for idx, (name, data) in enumerate(variables.items(), 1):
            value = data.get("value", "N/A")
            timestamp = data.get("timestamp", "N/A")

            # Insert row: ID | Name | Value | Timestamp
            self.var_tree.insert("", "end", text=str(idx), values=(name, value, timestamp))

        # ====================================================================
        # Update event log from data_store
        # ====================================================================
        # Clear existing text
        self.event_text.config(state=tk.NORMAL)
        self.event_text.delete("1.0", tk.END)

        # Add events from data_store.events list
        for event_data in events:
            event_msg = event_data.get("event", "N/A")
            event_time = event_data.get("timestamp", "N/A")

            # Format: [TIMESTAMP] EVENT
            log_line = f"[{event_time}] {event_msg}\n"
            self.event_text.insert(tk.END, log_line)

        # Scroll to bottom to show latest events
        self.event_text.see(tk.END)
        self.event_text.config(state=tk.DISABLED)

        # ====================================================================
        # Schedule next refresh in 100ms
        # ====================================================================
        self.root.after(100, self.refresh_gui)


if __name__ == "__main__":
    """
    Main program - create and run GUI
    """
    root = tk.Tk()
    app = MainWindow(root)
    root.mainloop()