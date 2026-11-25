from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QPushButton, QMessageBox
)
from PySide6.QtCore import Qt


class VariableEditorDialog(QDialog):
    """
    Modal dialog for editing an STM32 variable value.
    Validates that the entered value is a valid integer before accepting.
    """

    def __init__(self, var_name: str, current_value: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit Variable: {var_name}")
        self.setMinimumWidth(350)
        self.setModal(True)
        self.result_value: str | None = None

        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setContentsMargins(8, 8, 8, 8)

        # Variable name (read-only)
        name_label = QLabel(var_name)
        name_label.setStyleSheet("font-weight: bold; color: #9cdcfe;")
        form.addRow("Variable:", name_label)

        # Current value (read-only)
        current_label = QLabel(str(current_value))
        current_label.setStyleSheet("font-family: Consolas, monospace; color: #ce9178;")
        form.addRow("Current Value:", current_label)

        # New value input
        self.value_input = QLineEdit(str(current_value))
        self.value_input.selectAll()
        self.value_input.setPlaceholderText("Enter integer value...")
        self.value_input.returnPressed.connect(self._accept)
        form.addRow("New Value:", self.value_input)

        layout.addLayout(form)

        # Buttons
        btn_row = QHBoxLayout()
        send_btn = QPushButton("Send to STM32")
        send_btn.clicked.connect(self._accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setProperty("class", "secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(send_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self.value_input.setFocus()

    def _accept(self):
        raw = self.value_input.text().strip()
        if not raw:
            QMessageBox.warning(self, "Empty Value", "Please enter a value.")
            return
        try:
            int(raw)
        except ValueError:
            QMessageBox.warning(self, "Invalid Value", "Please enter a valid integer.")
            return
        self.result_value = raw
        self.accept()
