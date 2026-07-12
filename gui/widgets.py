
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

class ColorPickerButton(QPushButton):
    colorChanged = Signal(QColor)

    def __init__(self, initial: QColor, parent=None):
        super().__init__(parent)
        self._color = initial
        self.setFixedSize(44, 26)
        self._update_swatch()
        self.clicked.connect(self._pick)

    def _update_swatch(self):
        pix = QPixmap(self.size())
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setBrush(self._color)
        painter.setPen(QColor(90, 90, 90))
        painter.drawRoundedRect(1, 1, pix.width() - 2, pix.height() - 2, 4, 4)
        painter.end()
        self.setIcon(pix)
        self.setIconSize(self.size())

    def color(self) -> QColor:
        return self._color

    def set_color(self, color: QColor):
        self._color = color
        self._update_swatch()

    def _pick(self):
        chosen = QColorDialog.getColor(
            self._color, self, "Choose line color", QColorDialog.ShowAlphaChannel
        )
        if chosen.isValid():
            self.set_color(chosen)
            self.colorChanged.emit(chosen)

class GroupListWidget(QWidget):

    selectionChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        title = QLabel("Mesh groups / materials")
        title.setStyleSheet("font-weight: 600;")
        header.addWidget(title)
        header.addStretch()
        self.select_all_btn = QPushButton("All")
        self.select_none_btn = QPushButton("None")
        self.select_only_shown_btn = QPushButton("Only shown")
        self.select_only_shown_btn.setToolTip(
            "Check every group matching the current search and uncheck everything else"
        )
        for b in (self.select_all_btn, self.select_none_btn):
            b.setFixedWidth(48)
            b.setFlat(True)
        self.select_only_shown_btn.setFixedWidth(80)
        self.select_only_shown_btn.setFlat(True)
        header.addWidget(self.select_all_btn)
        header.addWidget(self.select_none_btn)
        header.addWidget(self.select_only_shown_btn)
        layout.addLayout(header)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search parts…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setStyleSheet(
            "QLineEdit { background: #2a2a2a; border: 1px solid #444; "
            "border-radius: 4px; padding: 4px 6px; }"
        )
        self.search_box.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search_box)

        self.match_label = QLabel("")
        self.match_label.setStyleSheet("color: #888; font-size: 10px;")
        self.match_label.hide()
        layout.addWidget(self.match_label)

        self.hidden_checked_label = QLabel("")
        self.hidden_checked_label.setStyleSheet("color: #d8a13a; font-size: 10px;")
        self.hidden_checked_label.setWordWrap(True)
        self.hidden_checked_label.hide()
        layout.addWidget(self.hidden_checked_label)

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet(
            "QListWidget { border: 1px solid #3a3a3a; border-radius: 6px; }"
        )
        layout.addWidget(self.list_widget)

        self.select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        self.select_none_btn.clicked.connect(lambda: self._set_all_checked(False))
        self.select_only_shown_btn.clicked.connect(self._check_only_visible)
        self.list_widget.itemChanged.connect(self._on_item_changed)

    def populate(self, group_infos: list[tuple[str, int]]):
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for name, tri_count in group_infos:
            item = QListWidgetItem(f"{name}  ({tri_count} tris)")
            item.setData(Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)
        self._apply_filter(self.search_box.text())
        self.selectionChanged.emit()

    def populate_preserving_checks(self, group_infos: list[tuple[str, int]]):
        previously_checked = self.checked_names()
        self.populate(group_infos)
        self.set_checked(previously_checked)

    def set_checked(self, names: set[str]):
        self.list_widget.blockSignals(True)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in names else Qt.Unchecked)
        self.list_widget.blockSignals(False)
        self._update_hidden_checked_warning()
        self.selectionChanged.emit()

    def _on_item_changed(self, _item):
        self._update_hidden_checked_warning()
        self.selectionChanged.emit()

    def _apply_filter(self, query: str):
        query = query.strip().lower()
        visible_count = 0
        total_count = self.list_widget.count()
        for i in range(total_count):
            item = self.list_widget.item(i)
            name = item.data(Qt.UserRole) or ""
            matches = query in name.lower()
            item.setHidden(not matches)
            if matches:
                visible_count += 1

        if query:
            self.match_label.setText(f"{visible_count} of {total_count} match")
            self.match_label.show()
        else:
            self.match_label.hide()

        self._update_hidden_checked_warning()

    def _update_hidden_checked_warning(self):
        hidden_checked = 0
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.isHidden() and item.checkState() == Qt.Checked:
                hidden_checked += 1

        if hidden_checked > 0:
            group_word = "group" if hidden_checked == 1 else "groups"
            self.hidden_checked_label.setText(
                f"⚠ {hidden_checked} more {group_word} checked outside this search "
                f"and still included in the render. Use \"Only shown\" to isolate "
                f"just what's visible."
            )
            self.hidden_checked_label.show()
        else:
            self.hidden_checked_label.hide()

    def _set_all_checked(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        self.list_widget.blockSignals(True)
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(state)
        self.list_widget.blockSignals(False)
        self._update_hidden_checked_warning()
        self.selectionChanged.emit()

    def _check_only_visible(self):
        self.list_widget.blockSignals(True)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setCheckState(Qt.Unchecked if item.isHidden() else Qt.Checked)
        self.list_widget.blockSignals(False)
        self._update_hidden_checked_warning()
        self.selectionChanged.emit()

    def checked_names(self) -> set[str]:
        names = set()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.Checked:
                names.add(item.data(Qt.UserRole))
        return names

def make_labeled_row(label_text: str, widget: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    label = QLabel(label_text)
    label.setMinimumWidth(110)
    row.addWidget(label)
    row.addWidget(widget)
    row.addStretch()
    return row
