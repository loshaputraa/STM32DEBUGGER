# ============================================================================
# Theme - PySide6 Dark Theme Stylesheet
# ============================================================================

class Theme:
    """Dark theme for PySide6 applications"""

    @staticmethod
    def get_stylesheet():
        """Return complete stylesheet"""
        return """
        QMainWindow {
            background-color: #1e1e1e;
            color: #d4d4d4;
        }

        QWidget {
            background-color: #1e1e1e;
            color: #d4d4d4;
        }

        QLabel {
            color: #d4d4d4;
        }

        QPushButton {
            background-color: #007acc;
            color: white;
            border: none;
            border-radius: 4px;
            padding: 6px 12px;
            font-weight: bold;
            font-size: 10pt;
        }

        QPushButton:hover {
            background-color: #005a9e;
        }

        QPushButton:pressed {
            background-color: #004578;
        }

        QPushButton:disabled {
            background-color: #555555;
            color: #999999;
        }

        QLineEdit {
            background-color: #3c3c3c;
            color: #d4d4d4;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 4px;
            selection-background-color: #007acc;
        }

        QLineEdit:focus {
            border: 1px solid #007acc;
        }

        QTextEdit {
            background-color: #3c3c3c;
            color: #d4d4d4;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 4px;
            selection-background-color: #007acc;
        }

        QTextEdit:focus {
            border: 1px solid #007acc;
        }

        QTableWidget {
            background-color: #3c3c3c;
            color: #d4d4d4;
            border: 1px solid #555555;
            gridline-color: #555555;
        }

        QTableWidget::item:selected {
            background-color: #007acc;
        }

        QHeaderView::section {
            background-color: #252526;
            color: #d4d4d4;
            padding: 4px;
            border: none;
            border-right: 1px solid #555555;
            border-bottom: 1px solid #555555;
        }

        QTabWidget::pane {
            border: 1px solid #555555;
        }

        QTabBar::tab {
            background-color: #2d2d30;
            color: #d4d4d4;
            padding: 8px 20px;
            border: 1px solid #555555;
            border-bottom: none;
        }

        QTabBar::tab:selected {
            background-color: #3c3c3c;
            border-bottom: 2px solid #007acc;
        }

        QTabBar::tab:hover {
            background-color: #3c3c3c;
        }

        QScrollBar:vertical {
            background-color: #3c3c3c;
            width: 12px;
            border: none;
        }

        QScrollBar::handle:vertical {
            background-color: #555555;
            border-radius: 6px;
            min-height: 20px;
        }

        QScrollBar::handle:vertical:hover {
            background-color: #707070;
        }

        QScrollBar::add-line:vertical {
            border: none;
            background: none;
        }

        QScrollBar::sub-line:vertical {
            border: none;
            background: none;
        }

        QStatusBar {
            background-color: #252526;
            color: #d4d4d4;
            border-top: 1px solid #555555;
        }

        QComboBox {
            background-color: #3c3c3c;
            color: #d4d4d4;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 4px;
        }

        QComboBox:focus {
            border: 1px solid #007acc;
        }

        QComboBox::drop-down {
            border: none;
        }

        QComboBox::down-arrow {
            image: none;
            width: 0;
        }

        QDialog {
            background-color: #1e1e1e;
            color: #d4d4d4;
        }

        QMessageBox {
            background-color: #1e1e1e;
        }

        QMessageBox QLabel {
            color: #d4d4d4;
        }

        QProgressBar {
            background-color: #3c3c3c;
            color: #d4d4d4;
            border: 1px solid #555555;
            border-radius: 4px;
            height: 20px;
        }

        QProgressBar::chunk {
            background-color: #007acc;
            border-radius: 2px;
        }
        """

    # Color scheme
    COLORS = {
        "primary": "#007acc",
        "success": "#27ae60",
        "warning": "#f39c12",
        "danger": "#e74c3c",
        "dark": "#1e1e1e",
        "light": "#d4d4d4",
        "border": "#555555",
        "bg_secondary": "#3c3c3c",
        "bg_tertiary": "#2d2d30",
    }

    @staticmethod
    def apply(app):
        """Apply theme to QApplication"""
        app.setStyle('Fusion')
        app.setStyleSheet(Theme.get_stylesheet())