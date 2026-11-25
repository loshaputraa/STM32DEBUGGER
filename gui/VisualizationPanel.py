# ============================================================================
# Visualization Panel - PySide6 with pyqtgraph (Unbounded History)
# ============================================================================

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QFileDialog, QMessageBox, QScrollArea, QSizePolicy
)
from PySide6.QtCore import Qt, QTimer
import pyqtgraph as pg
import pyqtgraph.exporters
import numpy as np
from collections import deque
from datetime import datetime


class VisualizationPanel(QWidget):
    """Real-time variable visualization using pyqtgraph"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: #1e1e1e;")

        # Unbounded data storage (keeps all historical samples)
        self.data_history = {}
        self.timestamps = deque()  # No maxlen cap
        self.sample_counter = 0

        # Color scheme - palette of 20 distinct colors
        self.color_palette = [
            (86, 205, 214), (78, 201, 176), (206, 145, 120), (214, 112, 214),
            (120, 160, 214), (214, 180, 120), (176, 214, 120), (214, 120, 180),
            (120, 214, 200), (214, 150, 120), (150, 120, 214), (120, 180, 214),
            (214, 200, 120), (180, 214, 120), (214, 120, 150), (120, 214, 180),
            (200, 120, 214), (214, 120, 120), (120, 120, 214), (170, 214, 120),
        ]
        self.colors = {}

        # Variable show/hide state - new variables default to visible.
        self.visible_vars = set()
        self.chip_buttons = {}   # var_name -> QPushButton (checkable toggle)

        # Independent "Clear Graph" state per graph. Both graphs read from
        # the same shared self.data_history/self.timestamps, so clearing
        # can't delete the underlying data (that would blank the other
        # graph too). Instead each graph tracks a time cutoff - only
        # samples at/after that x-value get plotted on that specific graph.
        # None = no cutoff (show full history), the default.
        self.individual_clear_after_time = None
        self.combined_clear_after_time = None

        self.setup_ui()

        # Update timer
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_plots)
        self.update_timer.start(100)  # 100ms updates

    def setup_ui(self):
        """Build the visualization UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        # ------------------------------------------------------------------
        # Control Bar - Individual Graph
        # ------------------------------------------------------------------
        ctrl_layout = QHBoxLayout()

        self.var_combo = QComboBox()
        self.var_combo.currentTextChanged.connect(self.on_var_selected)
        ctrl_layout.addWidget(QLabel("Variable:"))
        ctrl_layout.addWidget(self.var_combo)

        # Freeze / Unfreeze Toggle Button
        self.freeze_btn = QPushButton("🔒 Freeze")
        self.freeze_btn.setCheckable(True)
        self.freeze_btn.setStyleSheet("background-color: #8e44ad; color: white;")
        self.freeze_btn.toggled.connect(lambda checked: self.on_freeze_toggled(self.freeze_btn, checked))
        ctrl_layout.addWidget(self.freeze_btn)

        # Reset View Button
        reset_btn = QPushButton("↺ Reset View")
        reset_btn.setStyleSheet("background-color: #2980b9; color: white;")
        reset_btn.clicked.connect(self.reset_view_individual)
        ctrl_layout.addWidget(reset_btn)

        # Clear Graph Button
        clear_btn = QPushButton("🗑 Clear Graph")
        clear_btn.setStyleSheet("background-color: #c0392b; color: white;")
        clear_btn.clicked.connect(self.clear_history_individual)
        ctrl_layout.addWidget(clear_btn)

        # Export PNG Button
        export_btn = QPushButton("⬇ Export PNG")
        export_btn.setStyleSheet("background-color: #27ae60; color: white;")
        export_btn.clicked.connect(self.export_plot_individual)
        ctrl_layout.addWidget(export_btn)

        ctrl_layout.addStretch()
        layout.addLayout(ctrl_layout)

        # Plot Area - Individual Variable
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel('left', 'Value')
        self.plot_widget.setLabel('bottom', 'Time (s)')
        self.plot_widget.setTitle('Live Variable Graph')
        self.plot_widget.setBackground('#252526')

        self.plot_widget.getAxis('left').setPen(pg.mkPen('#d4d4d4'))
        self.plot_widget.getAxis('bottom').setPen(pg.mkPen('#d4d4d4'))
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)

        self.curve = self.plot_widget.plot(pen=pg.mkPen('#569cd6', width=2))
        layout.addWidget(self.plot_widget)

        # Variable Toggle Chips - show/hide individual variables on the
        # combined graph (dropdown above is independent of these)
        chips_scroll = QScrollArea()
        chips_scroll.setWidgetResizable(True)
        chips_scroll.setFixedHeight(50)
        chips_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        chips_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        chips_scroll.setStyleSheet("background-color: #1e1e1e; border: none;")

        chips_container = QWidget()
        chips_container.setStyleSheet("background-color: #1e1e1e;")
        self.chips_layout = QHBoxLayout(chips_container)
        self.chips_layout.setContentsMargins(4, 4, 4, 4)
        self.chips_layout.setSpacing(6)
        self.chips_layout.addStretch()  # keep chips left-aligned as they're added

        chips_scroll.setWidget(chips_container)
        layout.addWidget(chips_scroll)

        # ------------------------------------------------------------------
        # Control Bar - Combined (Normalized) Graph
        # ------------------------------------------------------------------
        ctrl_layout_combined = QHBoxLayout()

        combined_label = QLabel("All Variables:")
        combined_label.setStyleSheet("font-weight: bold;")
        ctrl_layout_combined.addWidget(combined_label)

        self.freeze_btn_combined = QPushButton("🔒 Freeze")
        self.freeze_btn_combined.setCheckable(True)
        self.freeze_btn_combined.setStyleSheet("background-color: #8e44ad; color: white;")
        self.freeze_btn_combined.toggled.connect(
            lambda checked: self.on_freeze_toggled(self.freeze_btn_combined, checked)
        )
        ctrl_layout_combined.addWidget(self.freeze_btn_combined)

        reset_btn_combined = QPushButton("↺ Reset View")
        reset_btn_combined.setStyleSheet("background-color: #2980b9; color: white;")
        reset_btn_combined.clicked.connect(self.reset_view_combined)
        ctrl_layout_combined.addWidget(reset_btn_combined)

        clear_btn_combined = QPushButton("🗑 Clear Graph")
        clear_btn_combined.setStyleSheet("background-color: #c0392b; color: white;")
        clear_btn_combined.clicked.connect(self.clear_history_combined)
        ctrl_layout_combined.addWidget(clear_btn_combined)

        export_btn_combined = QPushButton("⬇ Export PNG")
        export_btn_combined.setStyleSheet("background-color: #27ae60; color: white;")
        export_btn_combined.clicked.connect(self.export_plot_combined)
        ctrl_layout_combined.addWidget(export_btn_combined)

        ctrl_layout_combined.addStretch()
        layout.addLayout(ctrl_layout_combined)

        # Combined Plot (Normalized)
        self.combined_widget = pg.PlotWidget()
        self.combined_widget.setLabel('left', 'Normalized Value')
        self.combined_widget.setLabel('bottom', 'Time (s)')
        self.combined_widget.setTitle('All Variables (Normalized 0-1)')
        self.combined_widget.setBackground('#252526')

        self.combined_widget.getAxis('left').setPen(pg.mkPen('#d4d4d4'))
        self.combined_widget.getAxis('bottom').setPen(pg.mkPen('#d4d4d4'))
        self.combined_widget.showGrid(x=True, y=True, alpha=0.3)

        self.combined_curves = {}
        self.combined_widget.addLegend()
        layout.addWidget(self.combined_widget)

    def add_variable(self, var_name, min_val=0, max_val=100):
        """Dynamically add a variable to track"""
        if var_name not in self.data_history:
            self.data_history[var_name] = deque()  # Unbounded buffer

            color_index = len(self.colors) % len(self.color_palette)
            self.colors[var_name] = self.color_palette[color_index]

            color = self.colors[var_name]
            pen = pg.mkPen(color=color, width=2)
            self.combined_curves[var_name] = self.combined_widget.plot(pen=pen, name=var_name)

            # New variables default to visible on both plots
            self.visible_vars.add(var_name)
            self.var_combo.addItem(var_name)
            self._create_chip(var_name)

    def _create_chip(self, var_name):
        """Build a checkable colored toggle button for one variable"""
        btn = QPushButton(var_name)
        btn.setCheckable(True)
        btn.setChecked(True)  # visible by default
        btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        btn.toggled.connect(lambda checked, name=var_name: self.on_chip_toggled(name, checked))

        self.chip_buttons[var_name] = btn
        self._style_chip(var_name, True)

        # Insert before the trailing stretch so chips stay left-aligned
        self.chips_layout.insertWidget(self.chips_layout.count() - 1, btn)

    def _style_chip(self, var_name, checked):
        """Color a chip to match its plot line; dim it when toggled off"""
        color = self.colors.get(var_name, (100, 100, 100))
        color_str = f"rgb({color[0]}, {color[1]}, {color[2]})"
        btn = self.chip_buttons.get(var_name)
        if not btn:
            return

        if checked:
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {color_str}; color: #1e1e1e; "
                f"font-weight: bold; border-radius: 10px; padding: 4px 10px; border: none; }}"
            )
        else:
            btn.setStyleSheet(
                f"QPushButton {{ background-color: #2d2d2d; color: #777777; "
                f"border-radius: 10px; padding: 4px 10px; border: 1px solid {color_str}; }}"
            )

    def on_chip_toggled(self, var_name, checked):
        """Show/hide a variable's line on the combined (normalized) graph only.
        The dropdown/individual graph is independent of this and always
        lists every variable currently being measured."""
        self._style_chip(var_name, checked)

        # Combined (normalized) graph - just toggle curve visibility
        if var_name in self.combined_curves:
            self.combined_curves[var_name].setVisible(checked)

        if checked:
            self.visible_vars.add(var_name)
        else:
            self.visible_vars.discard(var_name)

    def add_sample(self, variables_dict):
        """Add sample to history without discarding old data"""
        for var_name, value in variables_dict.items():
            if var_name not in self.data_history:
                self.add_variable(var_name, 0, 100)

            if var_name in self.data_history:
                try:
                    self.data_history[var_name].append(float(value))
                except (ValueError, TypeError):
                    self.data_history[var_name].append(0)

        self.sample_counter += 1
        self.timestamps.append(self.sample_counter * 0.1)

    def on_freeze_toggled(self, button, checked):
        """Handle Freeze / Unfreeze toggle button (shared by both graphs' buttons)"""
        if checked:
            button.setText("🔓 Unfreeze")
            button.setStyleSheet("background-color: #27ae60; color: white;")
        else:
            button.setText("🔒 Freeze")
            button.setStyleSheet("background-color: #8e44ad; color: white;")

    def update_plots(self):
        """Update plot displays. Each graph now freezes/updates independently."""
        if not self.timestamps or not self.data_history:
            return

        x_data = np.array(list(self.timestamps))

        # --------------------------------------------------------------
        # Update individual plot (independent freeze + clear-after cutoff)
        # --------------------------------------------------------------
        if not self.freeze_btn.isChecked():
            current_var = self.var_combo.currentText()
            if current_var and current_var in self.data_history:
                y_data = np.array(list(self.data_history[current_var]))
                if len(y_data) > 0:
                    # FIX: a variable discovered after the app started has a
                    # shorter history deque than the global `timestamps`
                    # (which tracks every tick since startup). setData()
                    # requires equal length x/y arrays and throws otherwise -
                    # use only the most recent len(y_data) timestamps so the
                    # two always line up.
                    x_slice = x_data[-len(y_data):]

                    if self.individual_clear_after_time is not None:
                        mask = x_slice >= self.individual_clear_after_time
                        x_slice = x_slice[mask]
                        y_data = y_data[mask]

                    if len(y_data) > 0:
                        self.curve.setData(x_slice, y_data)
                    else:
                        self.curve.clear()

        # --------------------------------------------------------------
        # Update combined plot (independent freeze + clear-after cutoff)
        # --------------------------------------------------------------
        if not self.freeze_btn_combined.isChecked():
            for var_name in self.data_history:
                y_raw = np.array(list(self.data_history[var_name]))
                if len(y_raw) == 0:
                    continue

                # FIX (same root cause as above): align x to this variable's
                # own history length. Also wrapped in try/except so a single
                # malformed/edge-case variable (e.g. corrupted name from a
                # dropped UART byte) can't raise and abort every variable
                # that comes after it in this loop for the rest of the
                # session - previously that silently froze the combined
                # graph after the first mismatch, which is why only the
                # first couple of variables ever showed a line.
                try:
                    x_slice = x_data[-len(y_raw):]
                    min_v = np.min(y_raw)
                    max_v = np.max(y_raw)

                    if max_v - min_v < 0.001:
                        y_norm = np.zeros_like(y_raw)
                    else:
                        y_norm = (y_raw - min_v) / (max_v - min_v)
                        y_norm = np.clip(y_norm, 0, 1)

                    if self.combined_clear_after_time is not None:
                        mask = x_slice >= self.combined_clear_after_time
                        x_slice = x_slice[mask]
                        y_norm = y_norm[mask]

                    if var_name in self.combined_curves:
                        if len(y_norm) > 0:
                            self.combined_curves[var_name].setData(x_slice, y_norm)
                        else:
                            self.combined_curves[var_name].clear()
                except Exception as e:
                    print(f"[VIZ] Skipping combined-plot update for '{var_name}': {e!r}")
                    continue

    def on_var_selected(self, var_name):
        pass

    def reset_view_individual(self):
        """Restore autoscale on the individual graph only, without erasing data or freezing"""
        # FIX: autoRange() does a one-time fit-to-data, but as a side effect
        # it leaves the view in pyqtgraph's "manual range" state - so if
        # it's called *after* enableAutoRange(), auto-follow gets silently
        # switched back off and the graph stops tracking new incoming data.
        # Snap to the current data first, then enable continuous auto-range
        # last so it's guaranteed to stay on afterward.
        self.plot_widget.autoRange()
        self.plot_widget.enableAutoRange()

    def reset_view_combined(self):
        """Restore autoscale on the combined graph only, without erasing data or freezing"""
        self.combined_widget.autoRange()
        self.combined_widget.enableAutoRange()

    def _export_widget_png(self, plot_widget):
        """Shared PNG export logic for either graph's PlotWidget"""
        default_filename = f"plot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

        filepath, _ = QFileDialog.getSaveFileName(
            self,
            "Export Plot PNG",
            default_filename,
            "PNG Images (*.png);;All Files (*)"
        )

        if filepath:
            try:
                exporter = pg.exporters.ImageExporter(plot_widget.plotItem)
                exporter.export(filepath)
                QMessageBox.information(self, "Success", f"Plot exported successfully to:\n{filepath}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export plot:\n{str(e)}")

    def export_plot_individual(self):
        """Export the individual variable graph to PNG"""
        self._export_widget_png(self.plot_widget)

    def export_plot_combined(self):
        """Export the combined (normalized) graph to PNG"""
        self._export_widget_png(self.combined_widget)

    def clear_history_individual(self):
        """Clear the individual graph's display only. Underlying shared
        data is left intact (the combined graph keeps its full history) -
        a time cutoff is recorded so update_plots() only draws samples
        from this point forward on this graph."""
        self.individual_clear_after_time = self.sample_counter * 0.1
        self.curve.clear()

    def clear_history_combined(self):
        """Clear the combined graph's display only. Underlying shared data
        is left intact (the individual graph keeps its full history) - a
        time cutoff is recorded so update_plots() only draws samples from
        this point forward on this graph."""
        self.combined_clear_after_time = self.sample_counter * 0.1
        for curve in self.combined_curves.values():
            curve.clear()