"""UV Template Exporter - entry point."""

import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("UV Template Exporter")
    # Explicitly set a point-sized default font before any widget or native
    # dialog (e.g. the color picker) is created. Without this, on some
    # Windows setups the inherited default font ends up defined by pixel
    # size rather than point size, and Qt logs a harmless but noisy
    # "QFont::setPointSize: Point size <= 0 (-1)" warning the first time
    # something queries its point size.
    app.setFont(QFont("Segoe UI", 9))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
