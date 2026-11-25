# ============================================================================
# Event Log - PySide6 Event Display Component (FIXED)
# ============================================================================

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton, QLabel
from PySide6.QtGui import QTextCursor
from PySide6.QtCore import Qt
from datetime import datetime

# How many entries are kept visible in the log. Previously capped to the
# last 50, which is barely more than fits on screen at once - there was
# nothing meaningful to scroll through. Internal storage still caps at
# 1000 (see add_event); this only controls how much of that is rendered.
DISPLAY_LIMIT = 500


class EventLog(QWidget):
    """Event log display component"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.events = []
        self.setup_ui()

    def setup_ui(self):
        """Build UI"""
        layout = QVBoxLayout(self)

        # Header
        header_layout = QHBoxLayout()
        title = QLabel("Event Log")
        title.setStyleSheet("font-weight: bold; font-size: 11pt;")
        header_layout.addWidget(title)

        clear_btn = QPushButton("Clear")
        clear_btn.setMaximumWidth(80)
        clear_btn.setStyleSheet("background-color: #555555; color: white; padding: 2px 8px;")
        clear_btn.clicked.connect(self.clear)
        header_layout.addWidget(clear_btn)

        header_layout.addStretch()
        layout.addLayout(header_layout)

        # Text display
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        # Scrollbar was technically there but nearly invisible under the
        # app's dark theme - force it always-on and style it so it's
        # actually visible and grabbable.
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.text_edit.setStyleSheet("""
            QTextEdit {
                background-color: #252526;
                color: #d4d4d4;
                font-family: Courier;
            }
            QScrollBar:vertical {
                background: #2d2d2d;
                width: 14px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #5a5a5a;
                min-height: 24px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical:hover {
                background: #787878;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
                border: none;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }
        """)
        layout.addWidget(self.text_edit)

    def add_event(self, message, event_type="INFO"):
        """Add event to log"""
        timestamp = datetime.now().strftime("%H:%M:%S")

        # Format
        log_entry = f"[{timestamp}] {event_type}: {message}"
        self.events.append(log_entry)

        # Limit to 1000 entries
        if len(self.events) > 1000:
            self.events.pop(0)

        self.refresh()

    def refresh(self):
        """Refresh display, preserving the user's scroll position.

        Previously this always forced the cursor to the end, which yanked
        the view back to the bottom on every new event even if the user
        had deliberately scrolled up to read something - making it
        impossible to review history while events kept arriving.

        Fix: only auto-follow to the bottom if the user was ALREADY at (or
        very near) the bottom before this refresh. Otherwise, restore
        their exact scroll position after the text is replaced.
        """
        scrollbar = self.text_edit.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
        old_value = scrollbar.value()

        self.text_edit.setPlainText("\n".join(self.events[-DISPLAY_LIMIT:]))

        if was_at_bottom:
            cursor = self.text_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.text_edit.setTextCursor(cursor)
            scrollbar.setValue(scrollbar.maximum())
        else:
            # User scrolled up on purpose - leave them exactly where they were.
            scrollbar.setValue(old_value)

    def clear(self):
        """Clear all events"""
        self.events.clear()
        self.refresh()

    def get_all(self):
        """Get all events"""
        return self.events