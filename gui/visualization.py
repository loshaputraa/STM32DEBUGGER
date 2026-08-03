# ============================================================================
# Visualization Panel - Day 4 Real-Time Graphing
# ============================================================================
# Dual-graph approach:
#   1. Individual Variable Graph (top)    - dropdown-selected single variable
#   2. Combined Multi-Variable Graph      - toggleable, normalized 0-1 overlay
#
# Both graphs read from backend.data_store's rolling TimeSeriesBuffer history
# and refresh on a 50ms throttle using efficient line.set_data() updates.
#
# start()/stop() let the host window pause the refresh loop while the panel
# is hidden (e.g. toggled off), rather than tearing it down and rebuilding.

import time
import tkinter as tk
from tkinter import ttk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from backend.data_store import (
    get_history_numpy,
    get_variable_names,
    get_history_size,
    HISTORY_WINDOW_S,
)

# Color-coded variables, cycled if more variables than colors
VAR_COLORS = ["#e74c3c", "#3498db", "#2ecc71", "#f39c12", "#9b59b6", "#1abc9c", "#e67e22", "#34495e"]

UPDATE_MS = 50  # 50ms throttled updates per Day 4 spec


class VisualizationPanel(tk.Frame):
    """
    Embeddable Tkinter Frame containing both the individual and combined
    real-time variable graphs. Runs its own after()-based refresh loop.
    """

    def __init__(self, parent, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)

        self._var_colors = {}          # var_name -> color, assigned on first sight
        self._toggle_vars = {}         # var_name -> tk.BooleanVar (combined graph)
        self._toggle_checkbuttons = {} # var_name -> Checkbutton widget
        self._known_vars = set()
        self._running = False

        self._build_individual_graph()
        self._build_combined_graph()

        self.start()

    # ========================================================================
    # INDIVIDUAL VARIABLE GRAPH (Top)
    # ========================================================================
    def _build_individual_graph(self):
        ind_frame = tk.LabelFrame(
            self, text="Individual Variable Monitor",
            font=("Arial", 11, "bold"), padx=8, pady=8
        )
        ind_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        selector_row = tk.Frame(ind_frame)
        selector_row.pack(fill=tk.X, pady=(0, 5))

        tk.Label(selector_row, text="Variable:", font=("Arial", 10)).pack(side=tk.LEFT, padx=(0, 5))

        self.individual_var = tk.StringVar()
        self.individual_dropdown = ttk.Combobox(
            selector_row, textvariable=self.individual_var,
            state="readonly", width=25, values=[]
        )
        self.individual_dropdown.pack(side=tk.LEFT)

        self.individual_recording_label = tk.Label(
            selector_row, text="", font=("Arial", 9), fg="#27ae60"
        )
        self.individual_recording_label.pack(side=tk.RIGHT, padx=5)

        self.fig_individual = Figure(figsize=(7, 2.4), dpi=100)
        self.ax_individual = self.fig_individual.add_subplot(111)
        (self.line_individual,) = self.ax_individual.plot([], [], color=VAR_COLORS[0], linewidth=1.5)
        self.ax_individual.set_xlabel("Time (s ago)")
        self.ax_individual.set_ylabel("Value")
        self.ax_individual.grid(True, alpha=0.3)

        self.canvas_individual = FigureCanvasTkAgg(self.fig_individual, master=ind_frame)
        self.canvas_individual.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # ========================================================================
    # COMBINED MULTI-VARIABLE GRAPH (Bottom)
    # ========================================================================
    def _build_combined_graph(self):
        combined_frame = tk.LabelFrame(
            self, text="Combined View (Normalized, All Variables)",
            font=("Arial", 11, "bold"), padx=8, pady=8
        )
        combined_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.toggle_row = tk.Frame(combined_frame)
        self.toggle_row.pack(fill=tk.X, pady=(0, 5))

        self.fig_combined = Figure(figsize=(7, 2.4), dpi=100)
        self.ax_combined = self.fig_combined.add_subplot(111)
        self.ax_combined.set_xlabel("Time (s ago)")
        self.ax_combined.set_ylabel("Normalized Value (0-1)")
        self.ax_combined.set_ylim(-0.05, 1.05)
        self.ax_combined.grid(True, alpha=0.3)
        self._combined_lines = {}  # var_name -> Line2D

        self.canvas_combined = FigureCanvasTkAgg(self.fig_combined, master=combined_frame)
        self.canvas_combined.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # ========================================================================
    # Dynamic variable discovery - new variables appear as data arrives
    # ========================================================================
    def _sync_known_variables(self):
        current_vars = set(get_variable_names())
        new_vars = current_vars - self._known_vars

        for var_name in sorted(new_vars):
            color = VAR_COLORS[len(self._var_colors) % len(VAR_COLORS)]
            self._var_colors[var_name] = color

            # Add checkbutton for combined graph (default ON)
            bvar = tk.BooleanVar(value=True)
            self._toggle_vars[var_name] = bvar
            cb = tk.Checkbutton(
                self.toggle_row, text=var_name, variable=bvar,
                fg=color, font=("Arial", 9, "bold")
            )
            cb.pack(side=tk.LEFT, padx=4)
            self._toggle_checkbuttons[var_name] = cb

            # Add line to combined graph
            (line,) = self.ax_combined.plot([], [], color=color, linewidth=1.5, label=var_name)
            self._combined_lines[var_name] = line

        self._known_vars |= new_vars

        # Refresh individual dropdown options
        if new_vars:
            values = sorted(self._known_vars)
            self.individual_dropdown["values"] = values
            if not self.individual_var.get() and values:
                self.individual_var.set(values[0])

    # ========================================================================
    # Refresh loop - 50ms throttled per Day 4 spec
    # ========================================================================
    def _refresh_loop(self):
        if not self._running:
            return

        self._sync_known_variables()
        self._update_individual_graph()
        self._update_combined_graph()

        self.after(UPDATE_MS, self._refresh_loop)

    def _update_individual_graph(self):
        var_name = self.individual_var.get()
        if not var_name:
            self.individual_recording_label.config(text="")
            return

        times, values = get_history_numpy(var_name)
        sample_count = get_history_size(var_name)

        if not times:
            self.individual_recording_label.config(text="● No data", fg="#e74c3c")
            return

        self.individual_recording_label.config(text="● Recording", fg="#27ae60")

        now = time.time()
        rel_times = [t - now for t in times]  # negative seconds-ago, 0 = now

        self.line_individual.set_data(rel_times, values)
        self.line_individual.set_color(self._var_colors.get(var_name, VAR_COLORS[0]))

        # X-axis: rolling 60s window
        self.ax_individual.set_xlim(-HISTORY_WINDOW_S, 0)

        # Y-axis: auto-scale with 10% margin
        v_min, v_max = min(values), max(values)
        if v_min == v_max:
            margin = abs(v_min) * 0.1 if v_min != 0 else 1.0
        else:
            margin = (v_max - v_min) * 0.1
        self.ax_individual.set_ylim(v_min - margin, v_max + margin)

        current_value = values[-1]
        self.ax_individual.set_title(
            f"{var_name} (Current: {current_value:.2f} | Samples: {sample_count})",
            fontsize=10
        )

        self.canvas_individual.draw_idle()

    def _update_combined_graph(self):
        now = time.time()
        any_visible = False

        for var_name, line in self._combined_lines.items():
            bvar = self._toggle_vars.get(var_name)
            visible = bvar.get() if bvar else False
            line.set_visible(visible)

            if not visible:
                continue

            times, values = get_history_numpy(var_name)
            if not times:
                continue

            any_visible = True
            rel_times = [t - now for t in times]

            v_min, v_max = min(values), max(values)
            if v_max > v_min:
                norm_values = [(v - v_min) / (v_max - v_min) for v in values]
            else:
                norm_values = [0.5 for _ in values]

            line.set_data(rel_times, norm_values)
            line.set_label(f"{var_name} (n={len(values)})")

        self.ax_combined.set_xlim(-HISTORY_WINDOW_S, 0)

        if any_visible:
            self.ax_combined.legend(loc="upper left", fontsize=8, ncol=2)
        else:
            legend = self.ax_combined.get_legend()
            if legend:
                legend.remove()

        var_count = sum(1 for v in self._toggle_vars.values() if v.get())
        self.ax_combined.set_title(
            f"Multi-Variable View ({var_count} variable{'s' if var_count != 1 else ''}, max {HISTORY_WINDOW_S}s)",
            fontsize=10
        )

        self.canvas_combined.draw_idle()

    # ========================================================================
    # Lifecycle - pause/resume the refresh loop without tearing the panel down
    # ========================================================================
    def start(self):
        """(Re)start the 50ms refresh loop."""
        if not self._running:
            self._running = True
            self._refresh_loop()

    def stop(self):
        """Pause the refresh loop (panel widgets stay intact, e.g. while hidden)."""
        self._running = False