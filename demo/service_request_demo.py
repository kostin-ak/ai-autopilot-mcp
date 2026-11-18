"""Small local form used for an end-to-end Class B automation demonstration.

Run directly; no browser, files, network or user data are touched. The form's only
side effect is its own status label. Every control has an explicit accessible name
so the semantic UIA path is visible in get_snapshot and addressable by act_on_element.
"""
from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPushButton,
    QSlider,
    QTextEdit,
    QWidget,
)


class ServiceRequestWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Autopilot Demo — Service Request")
        self.resize(620, 520)

        central = QWidget(self)
        layout = QFormLayout(central)
        layout.setSpacing(12)

        self.name = QLineEdit()
        self.name.setAccessibleName("Full name")
        self.email = QLineEdit()
        self.email.setAccessibleName("Email")
        self.priority = QListWidget()
        self.priority.addItems(["Low", "Normal", "High"])
        self.priority.setCurrentRow(1)
        self.priority.setMaximumHeight(78)
        self.priority.setAccessibleName("Priority")
        self.urgency = QSlider(Qt.Orientation.Horizontal)
        self.urgency.setRange(0, 10)
        self.urgency.setValue(3)
        self.urgency.setAccessibleName("Urgency")
        self.notes = QTextEdit()
        self.notes.setAccessibleName("Notes")
        self.notify = QCheckBox("Notify me by email")
        self.notify.setAccessibleName("Notify me by email")
        self.status = QLabel("Status: draft")
        self.status.setAccessibleName("Status: draft")

        layout.addRow("Full name", self.name)
        layout.addRow("Email", self.email)
        layout.addRow("Priority", self.priority)
        layout.addRow("Urgency", self.urgency)
        layout.addRow("Notes", self.notes)
        layout.addRow(self.notify)

        buttons = QHBoxLayout()
        self.submit = QPushButton("Submit request")
        self.submit.setAccessibleName("Submit request")
        self.reset = QPushButton("Reset")
        self.reset.setAccessibleName("Reset")
        buttons.addWidget(self.submit)
        buttons.addWidget(self.reset)
        layout.addRow(buttons)
        layout.addRow(self.status)

        self.submit.clicked.connect(self._submit)
        self.reset.clicked.connect(self._reset)
        self.setCentralWidget(central)

    def _submit(self):
        if not self.name.text().strip():
            self._set_status("Status: error · full name is required")
            return
        selected = self.priority.selectedItems()
        priority = selected[0].text() if selected else "none"
        notify = "yes" if self.notify.isChecked() else "no"
        self._set_status(
            f"Status: submitted · {self.name.text()} · {priority} · "
            f"urgency {self.urgency.value()} · notify {notify}"
        )

    def _reset(self):
        self.name.clear()
        self.email.clear()
        self.priority.setCurrentRow(1)
        self.urgency.setValue(3)
        self.notes.clear()
        self.notify.setChecked(False)
        self._set_status("Status: draft")

    def _set_status(self, text: str):
        # Keep the visible label and its UIA name identical: the final verification
        # must observe the same result a human sees, not a hidden implementation field.
        self.status.setText(text)
        self.status.setAccessibleName(text)


def main():
    app = QApplication(sys.argv)
    window = ServiceRequestWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
