"""Main window for the UV Template Exporter."""

import os

from PySide6.QtCore import QThread, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QDragEnterEvent, QDropEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.loader import SUPPORTED_EXTENSIONS, load_mesh
from core.mesh_data import MeshParseError, UVMesh
from core.rasterizer import RenderOptions, render_uv_template, save_render
from core.selection_store import (
    clear_selection,
    load_selection,
    save_selection,
)
from core.update_checker import UpdateCheckError, UpdateInfo, check_for_update
from core.version import __version__ as APP_VERSION
from gui.widgets import ColorPickerButton, GroupListWidget, make_labeled_row

RESOLUTION_PRESETS = [512, 1024, 2048, 4096, 8192, 16384]


class LoadWorker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, path: str, uv_channel: str | None = None):
        super().__init__()
        self.path = path
        self.uv_channel = uv_channel

    def run(self):
        try:
            mesh = load_mesh(self.path, uv_channel=self.uv_channel)
            self.finished_ok.emit(mesh)
        except MeshParseError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - surface unexpected errors too
            self.failed.emit(f"Unexpected error: {e}")


class RenderWorker(QThread):
    finished_ok = Signal(object, str)
    failed = Signal(str)

    def __init__(self, mesh: UVMesh, opts: RenderOptions, out_path: str):
        super().__init__()
        self.mesh = mesh
        self.opts = opts
        self.out_path = out_path

    def run(self):
        try:
            img = render_uv_template(self.mesh, self.opts)
            ok = save_render(img, self.out_path)
            if not ok:
                self.failed.emit(f"Failed to save PNG to {self.out_path}")
                return
            self.finished_ok.emit(img, self.out_path)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"Render failed: {e}")


class UpdateCheckWorker(QThread):
    checked_ok = Signal(object)  # UpdateInfo | None
    failed = Signal(str)

    def __init__(self, current_version: str):
        super().__init__()
        self.current_version = current_version

    def run(self):
        try:
            info = check_for_update(self.current_version)
            self.checked_ok.emit(info)
        except UpdateCheckError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - surface unexpected errors too
            self.failed.emit(f"Unexpected error: {e}")


class DropZone(QFrame):
    fileDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumHeight(120)
        self._set_idle_style()

        layout = QVBoxLayout(self)
        self.label = QLabel("Drop a .obj / .dae / .cdae / .pim / .pmg / .pmd file here\nor click to browse")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("color: #999; font-size: 13px;")
        layout.addWidget(self.label)

    def _set_idle_style(self):
        self.setStyleSheet(
            "DropZone { border: 2px dashed #4a4a4a; border-radius: 10px; background: #232323; }"
        )

    def _set_hover_style(self):
        self.setStyleSheet(
            "DropZone { border: 2px dashed #6a9bd8; border-radius: 10px; background: #262b30; }"
        )

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            path = event.mimeData().urls()[0].toLocalFile()
            ext = os.path.splitext(path)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                self._set_hover_style()
                event.acceptProposedAction()
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._set_idle_style()

    def dropEvent(self, event: QDropEvent):
        self._set_idle_style()
        path = event.mimeData().urls()[0].toLocalFile()
        self.fileDropped.emit(path)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._browse()

    def _browse(self):
        exts = " ".join(f"*{e}" for e in sorted(SUPPORTED_EXTENSIONS))
        path, _ = QFileDialog.getOpenFileName(
            self, "Open mesh file", "", f"Mesh files ({exts});;All files (*)"
        )
        if path:
            self.fileDropped.emit(path)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("UV Template Exporter")
        # Left panel holds a mesh-group list, resolution/line-width/color
        # controls, and several checkboxes with sub-options -- 980x680 was
        # too small for all of that plus the preview, so the left column
        # ended up squeezed under its own content's natural width/height
        # and needed internal scrolling just to reach "Export...". Sized
        # up so the left panel's content fits without being cramped while
        # still leaving real room for the preview on typical 1080p+
        # displays.
        self.resize(1280, 860)
        # Floor the window itself too -- without this, manually shrinking
        # the window (not just dragging the splitter) can reproduce the
        # same cramped-left-panel problem the default size just fixed.
        self.setMinimumSize(900, 640)

        self.current_mesh: UVMesh | None = None
        self.current_path: str | None = None
        self.load_worker: LoadWorker | None = None
        self.render_worker: RenderWorker | None = None
        self.update_check_worker: UpdateCheckWorker | None = None
        self._update_check_is_silent = True
        self._pending_update_info: UpdateInfo | None = None
        self._reloading_for_channel_switch = False

        self._build_ui()
        self._apply_theme()

        # Silent check on launch: only surfaces anything if an update is
        # actually found (via the dismissible banner below) -- a flaky
        # connection or a rate-limited GitHub API shouldn't pop an error
        # at someone on every startup. "Help > Check for Updates…" runs
        # the same check non-silently for an explicit result either way.
        self._check_for_updates(silent=True)

    # ---------------------------------------------------------------- UI ---
    def _build_ui(self):
        help_menu = self.menuBar().addMenu("&Help")
        check_updates_action = help_menu.addAction("Check for Updates…")
        check_updates_action.triggered.connect(lambda: self._check_for_updates(silent=False))
        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self._show_about_dialog)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Dismissible strip shown only once a newer GitHub release is
        # actually found -- stays hidden the rest of the time rather than
        # taking up permanent space for something that's usually not there.
        self.update_banner = QWidget()
        self.update_banner.setStyleSheet(
            "background: #2c3e50; border-bottom: 1px solid #3d5468;"
        )
        banner_layout = QHBoxLayout(self.update_banner)
        banner_layout.setContentsMargins(12, 6, 8, 6)
        self.update_banner_label = QLabel("")
        self.update_banner_label.setStyleSheet("color: #dce8f2;")
        banner_layout.addWidget(self.update_banner_label, stretch=1)
        self.update_banner_view_btn = QPushButton("View release")
        self.update_banner_view_btn.setFixedWidth(100)
        self.update_banner_view_btn.clicked.connect(self._on_view_release_clicked)
        banner_layout.addWidget(self.update_banner_view_btn)
        self.update_banner_dismiss_btn = QPushButton("✕")
        self.update_banner_dismiss_btn.setFixedWidth(28)
        self.update_banner_dismiss_btn.setFlat(True)
        self.update_banner_dismiss_btn.clicked.connect(self.update_banner.hide)
        banner_layout.addWidget(self.update_banner_dismiss_btn)
        self.update_banner.hide()
        outer.addWidget(self.update_banner)

        root = QHBoxLayout()
        root.setContentsMargins(10, 10, 10, 10)
        outer.addLayout(root)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        # ---- Left panel: input + options ----
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setSpacing(14)

        left_layout.addWidget(QLabel("<b>1. Input model</b>"))
        self.drop_zone = DropZone()
        self.drop_zone.fileDropped.connect(self._on_file_chosen)
        left_layout.addWidget(self.drop_zone)

        self.file_label = QLabel("No file loaded")
        self.file_label.setStyleSheet("color: #aaa; font-size: 11px;")
        self.file_label.setWordWrap(True)
        left_layout.addWidget(self.file_label)

        self.load_progress = QProgressBar()
        self.load_progress.setRange(0, 0)
        self.load_progress.hide()
        left_layout.addWidget(self.load_progress)

        # ---- UV channel/layer (only shown when a loaded file actually has
        # more than one -- e.g. a paint UV layout vs. a lightmap/AO unwrap;
        # picking the wrong one silently produces a degenerate template for
        # whatever part only has data on the other channel) ----
        self.uv_channel_row = QWidget()
        uv_channel_row_layout = QHBoxLayout(self.uv_channel_row)
        uv_channel_row_layout.setContentsMargins(0, 0, 0, 0)
        self.uv_channel_combo = QComboBox()
        uv_channel_row_layout.addLayout(make_labeled_row("UV channel", self.uv_channel_combo))
        self.uv_channel_row.setVisible(False)
        left_layout.addWidget(self.uv_channel_row)

        # ---- Groups ----
        left_layout.addWidget(QLabel("<b>2. Groups to include</b>"))
        self.group_list = GroupListWidget()
        self.group_list.setMinimumHeight(160)
        self.group_list.selectionChanged.connect(self._update_export_enabled)
        left_layout.addWidget(self.group_list)

        selection_row = QHBoxLayout()
        self.save_selection_btn = QPushButton("Save selection for this file")
        self.save_selection_btn.setToolTip(
            "Remembers the checked groups, UV channel, and template options "
            "for this exact file so next time it's loaded they're applied "
            "automatically instead of needing to be picked again."
        )
        self.save_selection_btn.setEnabled(False)
        self.save_selection_btn.clicked.connect(self._on_save_selection_clicked)
        selection_row.addWidget(self.save_selection_btn)

        self.clear_selection_btn = QPushButton("Clear")
        self.clear_selection_btn.setToolTip("Forgets the saved selection for this file.")
        self.clear_selection_btn.setFixedWidth(56)
        self.clear_selection_btn.setEnabled(False)
        self.clear_selection_btn.clicked.connect(self._on_clear_selection_clicked)
        selection_row.addWidget(self.clear_selection_btn)
        left_layout.addLayout(selection_row)

        self.selection_status_label = QLabel("")
        self.selection_status_label.setStyleSheet("color: #7fae7f; font-size: 10px;")
        self.selection_status_label.setWordWrap(True)
        self.selection_status_label.hide()
        left_layout.addWidget(self.selection_status_label)

        # ---- Render options ----
        left_layout.addWidget(QLabel("<b>3. Template options</b>"))
        opts_group = QGroupBox()
        opts_layout = QVBoxLayout(opts_group)

        self.width_combo = QComboBox()
        for r in RESOLUTION_PRESETS:
            self.width_combo.addItem(str(r), r)
        self.width_combo.setCurrentIndex(RESOLUTION_PRESETS.index(4096))
        self.width_combo.currentIndexChanged.connect(self._update_resolution_hint)
        opts_layout.addLayout(make_labeled_row("Width (px)", self.width_combo))

        self.height_combo = QComboBox()
        for r in RESOLUTION_PRESETS:
            self.height_combo.addItem(str(r), r)
        self.height_combo.setCurrentIndex(RESOLUTION_PRESETS.index(4096))
        self.height_combo.currentIndexChanged.connect(self._update_resolution_hint)
        opts_layout.addLayout(make_labeled_row("Height (px)", self.height_combo))

        self.resolution_hint = QLabel("")
        self.resolution_hint.setStyleSheet("color: #888; font-size: 10px;")
        opts_layout.addWidget(self.resolution_hint)

        self.line_width_spin = QDoubleSpinBox()
        self.line_width_spin.setRange(0.1, 64.0)
        self.line_width_spin.setSingleStep(0.5)
        self.line_width_spin.setValue(1.0)
        opts_layout.addLayout(make_labeled_row("Line width (px)", self.line_width_spin))

        self.color_button = ColorPickerButton(QColor(0, 0, 0, 255))
        opts_layout.addLayout(make_labeled_row("Line color", self.color_button))

        self.color_by_group_check = QCheckBox("Color-code by material/group")
        opts_layout.addWidget(self.color_by_group_check)

        self.checker_bg_check = QCheckBox("Checker background (scale reference)")
        opts_layout.addWidget(self.checker_bg_check)

        self.island_silhouette_check = QCheckBox("Island outlines only (no triangulation)")
        self.island_silhouette_check.setToolTip(
            "Draws only each UV island's outer edge -- no interior "
            "triangle mesh -- instead of the full wireframe. Independent "
            "of the fill option below."
        )
        opts_layout.addWidget(self.island_silhouette_check)

        self.hide_quad_diagonals_check = QCheckBox("Hide quad diagonals")
        self.hide_quad_diagonals_check.setToolTip(
            "When two triangles form a rectangular/trapezoidal panel, hides "
            "the diagonal line splitting them so the result looks like a "
            "clean quad grid instead of a dense triangle mesh -- closer to "
            "the game's own shipped UV maps. This only removes diagonals; "
            "it doesn't change any line's thickness or color. Works with "
            "or without 'Island outlines only' above."
        )
        opts_layout.addWidget(self.hide_quad_diagonals_check)

        self.island_fill_check = QCheckBox("Fill islands with color")
        self.island_fill_check.setToolTip(
            "Fills each UV island's interior with a translucent color, "
            "matching how the game's own shipped UV maps look. Works with "
            "or without the interior triangle mesh still showing."
        )
        opts_layout.addWidget(self.island_fill_check)

        self.island_fill_options = QWidget()
        island_fill_options_layout = QVBoxLayout(self.island_fill_options)
        island_fill_options_layout.setContentsMargins(20, 0, 0, 0)

        self.fill_color_button = ColorPickerButton(QColor(255, 255, 255, 255))
        self.fill_color_button.setToolTip("Fill color for each island's interior.")
        island_fill_options_layout.addLayout(
            make_labeled_row("Fill color", self.fill_color_button)
        )

        fill_opacity_row = QHBoxLayout()
        fill_opacity_label = QLabel("Fill opacity")
        fill_opacity_label.setMinimumWidth(110)
        fill_opacity_row.addWidget(fill_opacity_label)
        self.fill_opacity_slider = QSlider(Qt.Horizontal)
        self.fill_opacity_slider.setRange(0, 100)
        self.fill_opacity_slider.setValue(20)
        fill_opacity_row.addWidget(self.fill_opacity_slider)
        self.fill_opacity_value_label = QLabel("20%")
        self.fill_opacity_value_label.setFixedWidth(36)
        fill_opacity_row.addWidget(self.fill_opacity_value_label)
        island_fill_options_layout.addLayout(fill_opacity_row)

        self.island_fill_options.setVisible(False)
        opts_layout.addWidget(self.island_fill_options)

        self.auto_apply_selection_check = QCheckBox("Auto-apply saved selection on load")
        self.auto_apply_selection_check.setChecked(True)
        self.auto_apply_selection_check.setToolTip(
            "When a file with a saved selection is opened, automatically "
            "check the same groups, UV channel, and template options that "
            "were saved for it last time. Turn off to always start fresh."
        )
        opts_layout.addWidget(self.auto_apply_selection_check)

        left_layout.addWidget(opts_group)
        left_layout.addStretch()

        left_scroll.setWidget(left_panel)
        # Without a floor, the splitter is free to squeeze this panel
        # below what its own controls need (resolution dropdowns, labeled
        # rows, checkboxes with indented sub-options) -- that's what left
        # it too narrow in practice. 380px comfortably fits all of that at
        # the theme's default font size without the panel wrapping/clipping.
        left_scroll.setMinimumWidth(380)
        splitter.addWidget(left_scroll)

        # ---- Right panel: preview + warnings + export ----
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        right_layout.addWidget(QLabel("<b>Preview</b> (low-res proxy; export renders at full resolution)"))

        preview_scroll = QScrollArea()
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setStyleSheet("background: #1a1a1a;")
        self.preview_label = QLabel("Load a model to preview its UV layout")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setStyleSheet("color: #777;")
        preview_scroll.setWidget(self.preview_label)
        right_layout.addWidget(preview_scroll, stretch=1)

        # Small in-place indicator shown while island fill is being (re)computed
        # for the preview. Parented to the scroll area's viewport rather than
        # the scrolled preview_label itself, so it stays fixed in the center
        # of the visible area regardless of scroll position, and is an
        # embedded child widget rather than a separate top-level window.
        self.applying_overlay = QLabel(
            "Applying…\nThis may freeze the app for a minute or two.",
            preview_scroll.viewport(),
        )
        self.applying_overlay.setAlignment(Qt.AlignCenter)
        self.applying_overlay.setWordWrap(True)
        # Fixed width so the wrapped two-line message stays compact instead
        # of stretching out to whatever the longest line would need on one
        # line -- adjustSize() in _position_applying_overlay() then only
        # has to grow the height to fit the wrapped text.
        self.applying_overlay.setFixedWidth(240)
        self.applying_overlay.setStyleSheet(
            "background: rgba(25, 25, 25, 225); color: #eee; "
            "border: 1px solid #555; border-radius: 6px; "
            "padding: 8px 16px; font-weight: 600;"
        )
        self.applying_overlay.hide()
        self._preview_scroll = preview_scroll

        self.warnings_box = QTextEdit()
        self.warnings_box.setReadOnly(True)
        self.warnings_box.setMaximumHeight(90)
        self.warnings_box.hide()
        right_layout.addWidget(self.warnings_box)

        export_row = QHBoxLayout()
        self.export_button = QPushButton("Export UV Template PNG…")
        self.export_button.setEnabled(False)
        self.export_button.setMinimumHeight(36)
        self.export_button.clicked.connect(self._on_export_clicked)
        export_row.addWidget(self.export_button)
        right_layout.addLayout(export_row)

        self.render_progress = QProgressBar()
        self.render_progress.setRange(0, 0)
        self.render_progress.hide()
        right_layout.addWidget(self.render_progress)

        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        # Matches the new 1280px-wide default window: left panel gets
        # enough for its content at a comfortable width, right panel (the
        # preview, which benefits most from extra space) takes the rest.
        splitter.setSizes([420, 860])

        self.setStatusBar(QStatusBar())

        self.line_width_spin.valueChanged.connect(self._refresh_preview)
        self.color_by_group_check.stateChanged.connect(self._refresh_preview)
        self.checker_bg_check.stateChanged.connect(self._refresh_preview)
        self.island_silhouette_check.stateChanged.connect(self._refresh_preview)
        self.hide_quad_diagonals_check.stateChanged.connect(self._refresh_preview)
        self.island_fill_check.stateChanged.connect(self._on_island_fill_toggled)
        self.fill_color_button.colorChanged.connect(self._refresh_preview_with_overlay)
        self.fill_opacity_slider.valueChanged.connect(self._on_fill_opacity_changed)
        self.fill_opacity_slider.sliderReleased.connect(self._on_fill_opacity_slider_released)
        self.color_button.colorChanged.connect(self._refresh_preview)
        self.group_list.selectionChanged.connect(self._refresh_preview)
        self.uv_channel_combo.currentIndexChanged.connect(self._on_uv_channel_changed)
        self._update_resolution_hint()

    def _apply_theme(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #1e1e1e; color: #ddd; font-size: 12px; }
            QGroupBox { border: 1px solid #3a3a3a; border-radius: 8px; margin-top: 8px; padding: 8px; }
            QPushButton { background: #2c2c2c; border: 1px solid #444; border-radius: 6px; padding: 6px 10px; }
            QPushButton:hover { background: #383838; }
            QPushButton:disabled { color: #666; }
            QComboBox, QSpinBox, QDoubleSpinBox { background: #2a2a2a; border: 1px solid #444; border-radius: 4px; padding: 3px; }
            QScrollArea { border: none; }
            """
        )

    # -------------------------------------------------------------- logic ---
    def _check_for_updates(self, silent: bool):
        if self.update_check_worker is not None and self.update_check_worker.isRunning():
            return
        self._update_check_is_silent = silent
        self.update_check_worker = UpdateCheckWorker(APP_VERSION)
        self.update_check_worker.checked_ok.connect(self._on_update_check_ok)
        self.update_check_worker.failed.connect(self._on_update_check_failed)
        self.update_check_worker.start()

    def _on_update_check_ok(self, info: object):
        if info is not None:
            self._pending_update_info = info
            self.update_banner_label.setText(
                f"A new version is available: v{info.latest_version} "
                f"(you have v{APP_VERSION})."
            )
            self.update_banner.show()
        elif not self._update_check_is_silent:
            QMessageBox.information(
                self,
                "No updates available",
                f"You're running the latest version (v{APP_VERSION}).",
            )

    def _on_update_check_failed(self, message: str):
        # A silent startup check failing (no network, GitHub rate limit,
        # etc.) shouldn't interrupt opening the app -- only a manually
        # triggered "Check for Updates…" surfaces the failure directly.
        if not self._update_check_is_silent:
            QMessageBox.warning(self, "Update check failed", message)

    def _on_view_release_clicked(self):
        if self._pending_update_info is not None:
            QDesktopServices.openUrl(QUrl(self._pending_update_info.release_url))

    def _show_about_dialog(self):
        QMessageBox.about(
            self,
            "About UV Template Exporter",
            f"UV Template Exporter\nVersion {APP_VERSION}\n\n"
            "https://github.com/Burzt-YT/UV-Exporter",
        )

    def _on_file_chosen(self, path: str):
        ext = os.path.splitext(path)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            QMessageBox.warning(self, "Unsupported file", f"'{ext}' is not a supported mesh format.")
            return

        self.current_path = path
        self.file_label.setText(path)
        self.load_progress.show()
        self.export_button.setEnabled(False)
        self.warnings_box.hide()
        self.uv_channel_row.setVisible(False)
        self._reloading_for_channel_switch = False
        self.save_selection_btn.setEnabled(False)
        self.clear_selection_btn.setEnabled(False)
        self.selection_status_label.hide()
        self.statusBar().showMessage(f"Loading {os.path.basename(path)}…")

        # If a selection was saved for this file (by path, or falling back
        # to filename) and auto-apply is on, load straight into the saved
        # UV channel instead of the format default and switching afterward
        # -- avoids a redundant reload for files whose default channel
        # differs from what was saved.
        saved = (
            load_selection(path)
            if self.auto_apply_selection_check.isChecked()
            else None
        )
        initial_channel = saved.get("uv_channel") if saved else None

        self.load_worker = LoadWorker(path, uv_channel=initial_channel)
        self.load_worker.finished_ok.connect(self._on_load_success)
        self.load_worker.failed.connect(self._on_load_failed)
        self.load_worker.start()

    def _on_load_success(self, mesh: UVMesh):
        self.load_progress.hide()
        self.current_mesh = mesh
        self.statusBar().showMessage(
            f"Loaded {len(mesh.groups)} group(s), {mesh.total_triangles} triangles.", 6000
        )

        group_infos = [(g.name, g.triangle_count) for g in mesh.groups]
        was_channel_switch = self._reloading_for_channel_switch
        if was_channel_switch:
            self.group_list.populate_preserving_checks(group_infos)
        else:
            self.group_list.populate(group_infos)
        self._reloading_for_channel_switch = False

        self._update_uv_channel_combo(mesh)

        applied_saved = False
        if not was_channel_switch and self.auto_apply_selection_check.isChecked():
            saved = load_selection(self.current_path) if self.current_path else None
            if saved:
                self._apply_saved_selection(saved, group_infos)
                applied_saved = True

        if self.current_path is not None:
            has_saved = load_selection(self.current_path) is not None
            self.save_selection_btn.setEnabled(True)
            self.clear_selection_btn.setEnabled(has_saved)
            if applied_saved:
                self.selection_status_label.setText(
                    "✓ Applied saved selection for this file."
                )
                self.selection_status_label.show()
            else:
                self.selection_status_label.hide()

        if mesh.warnings:
            self.warnings_box.setPlainText("\n".join(f"⚠ {w}" for w in mesh.warnings))
            self.warnings_box.show()
        else:
            self.warnings_box.hide()

        self._update_export_enabled()
        self._refresh_preview()

    def _apply_saved_selection(self, saved: dict, group_infos: list[tuple[str, int]]) -> None:
        """Applies a previously saved selection dict to the current UI:
        checked groups (only the ones that still exist in this load are
        checked -- a saved group name that no longer appears, e.g. after a
        model edit, is silently dropped rather than erroring) and render
        option widget values."""
        available_names = {name for name, _ in group_infos}
        checked = set(saved.get("checked_groups", [])) & available_names
        if checked:
            self.group_list.set_checked(checked)

        opts = saved.get("render_options", {})
        if "width" in opts:
            idx = self.width_combo.findData(opts["width"])
            if idx >= 0:
                self.width_combo.setCurrentIndex(idx)
        if "height" in opts:
            idx = self.height_combo.findData(opts["height"])
            if idx >= 0:
                self.height_combo.setCurrentIndex(idx)
        if "line_width" in opts:
            self.line_width_spin.setValue(opts["line_width"])
        if "line_color_rgba" in opts:
            r, g, b, a = opts["line_color_rgba"]
            self.color_button.set_color(QColor(r, g, b, a))
        if "color_by_group" in opts:
            self.color_by_group_check.setChecked(opts["color_by_group"])
        if "checker_background" in opts:
            self.checker_bg_check.setChecked(opts["checker_background"])
        if "island_silhouette_only" in opts:
            self.island_silhouette_check.setChecked(opts["island_silhouette_only"])
        if "hide_quad_diagonals" in opts:
            self.hide_quad_diagonals_check.setChecked(opts["hide_quad_diagonals"])
        if "fill_color_rgba" in opts:
            r, g, b, a = opts["fill_color_rgba"]
            self.fill_color_button.set_color(QColor(r, g, b, a))
        if "fill_opacity_pct" in opts:
            self.fill_opacity_slider.setValue(opts["fill_opacity_pct"])
        # island_fill itself is deliberately not restored here -- whether
        # fill is on is left at its default (off) each time a file is
        # loaded/selection applied, rather than persisted. Fill color and
        # opacity above are still restored so they're ready as-is whenever
        # someone turns fill on by hand.

    def _update_uv_channel_combo(self, mesh: UVMesh):
        """Populates the UV channel picker from the just-loaded mesh's own
        metadata (no extra file read needed -- each parser fills these in
        as part of its normal parse pass) and shows the row only when
        there's actually a choice to make."""
        self.uv_channel_combo.blockSignals(True)
        self.uv_channel_combo.clear()
        for channel_id in mesh.available_uv_sets:
            label = f"UV channel {channel_id}" if channel_id else "UV channel (default)"
            self.uv_channel_combo.addItem(label, channel_id)
        idx = self.uv_channel_combo.findData(mesh.active_uv_set)
        self.uv_channel_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.uv_channel_combo.blockSignals(False)
        self.uv_channel_row.setVisible(len(mesh.available_uv_sets) > 1)

    def _on_uv_channel_changed(self, _index: int):
        if self.current_path is None or self.current_mesh is None:
            return
        selected_channel = self.uv_channel_combo.currentData()
        if selected_channel == self.current_mesh.active_uv_set:
            return

        self._reloading_for_channel_switch = True
        self.load_progress.show()
        self.export_button.setEnabled(False)
        self.statusBar().showMessage(f"Reloading with UV channel {selected_channel}…")

        self.load_worker = LoadWorker(self.current_path, uv_channel=selected_channel)
        self.load_worker.finished_ok.connect(self._on_load_success)
        self.load_worker.failed.connect(self._on_load_failed)
        self.load_worker.start()

    def _on_load_failed(self, message: str):
        self.load_progress.hide()
        was_channel_switch = self._reloading_for_channel_switch
        self._reloading_for_channel_switch = False

        if not was_channel_switch:
            self.current_mesh = None
        elif self.current_mesh is not None:
            # The channel switch itself failed (e.g. no data on that
            # channel) -- the previously-loaded mesh is still valid, so
            # revert the combo to what's actually loaded instead of
            # leaving it pointing at a channel that didn't load.
            idx = self.uv_channel_combo.findData(self.current_mesh.active_uv_set)
            if idx >= 0:
                self.uv_channel_combo.blockSignals(True)
                self.uv_channel_combo.setCurrentIndex(idx)
                self.uv_channel_combo.blockSignals(False)

        self.export_button.setEnabled(
            self.current_mesh is not None and len(self.group_list.checked_names()) > 0
        )
        self.statusBar().showMessage(
            "Failed to switch UV channel." if was_channel_switch else "Failed to load file.", 6000
        )
        QMessageBox.critical(
            self,
            "Couldn't switch UV channel" if was_channel_switch else "Couldn't load model",
            message,
        )

    def _capture_current_selection(self) -> dict:
        """Builds the JSON-serializable selection dict for the currently
        loaded file: checked groups, active UV channel, and the render
        option widget values (not derived boundary/interior values, since
        those are computed from line_width/color at apply time)."""
        color = self.color_button.color()
        fill_color = self.fill_color_button.color()
        return {
            "uv_channel": (
                self.uv_channel_combo.currentData()
                if self.uv_channel_row.isVisible()
                else None
            ),
            "checked_groups": sorted(self.group_list.checked_names()),
            "render_options": {
                "width": self.width_combo.currentData(),
                "height": self.height_combo.currentData(),
                "line_width": self.line_width_spin.value(),
                "line_color_rgba": [color.red(), color.green(), color.blue(), color.alpha()],
                "color_by_group": self.color_by_group_check.isChecked(),
                "checker_background": self.checker_bg_check.isChecked(),
                "island_silhouette_only": self.island_silhouette_check.isChecked(),
                "hide_quad_diagonals": self.hide_quad_diagonals_check.isChecked(),
                # island_fill's on/off state is intentionally not saved --
                # see _apply_saved_selection() for why.
                "fill_color_rgba": [
                    fill_color.red(), fill_color.green(), fill_color.blue(), fill_color.alpha()
                ],
                "fill_opacity_pct": self.fill_opacity_slider.value(),
            },
        }

    def _on_save_selection_clicked(self):
        if self.current_path is None:
            return
        save_selection(self.current_path, self._capture_current_selection())
        self.clear_selection_btn.setEnabled(True)
        self.selection_status_label.setText(
            f"✓ Saved selection for {os.path.basename(self.current_path)}."
        )
        self.selection_status_label.show()
        self.statusBar().showMessage("Selection saved for this file.", 4000)

    def _on_clear_selection_clicked(self):
        if self.current_path is None:
            return
        clear_selection(self.current_path)
        self.clear_selection_btn.setEnabled(False)
        self.selection_status_label.setText("Saved selection cleared.")
        self.selection_status_label.show()
        self.statusBar().showMessage("Saved selection cleared for this file.", 4000)

    def _on_island_fill_toggled(self, _state):
        self.island_fill_options.setVisible(self.island_fill_check.isChecked())
        self._refresh_preview_with_overlay()

    def _on_fill_opacity_changed(self, value: int):
        # The % label always tracks the live value so it's readable while
        # dragging. The actual preview re-render is the expensive part
        # (island fill recompute), so it's deliberately skipped here while
        # the slider is being dragged with the mouse -- see
        # _on_fill_opacity_slider_released() for where that happens
        # instead. isSliderDown() is only true for an active mouse drag, so
        # keyboard/page-step/programmatic changes (e.g. applying a saved
        # selection) still refresh immediately as before.
        self.fill_opacity_value_label.setText(f"{value}%")
        if not self.fill_opacity_slider.isSliderDown():
            self._refresh_preview_with_overlay()

    def _on_fill_opacity_slider_released(self):
        self._refresh_preview_with_overlay()

    def _update_resolution_hint(self):
        width = self.width_combo.currentData()
        height = self.height_combo.currentData()
        largest = max(width, height)
        if largest >= 16384:
            self.resolution_hint.setText(
                "16K exports are large (~1GB working buffer) and can take "
                "10-30s+ depending on mesh complexity."
            )
        elif largest >= 8192:
            self.resolution_hint.setText("8K exports may take several seconds.")
        else:
            self.resolution_hint.setText("")

    def _update_export_enabled(self):
        has_mesh = self.current_mesh is not None
        has_selection = len(self.group_list.checked_names()) > 0
        self.export_button.setEnabled(has_mesh and has_selection)

    def _current_options(
        self, width: int | None = None, height: int | None = None
    ) -> RenderOptions:
        color = self.color_button.color()
        fill_color = self.fill_color_button.color()
        return RenderOptions(
            width=width or self.width_combo.currentData(),
            height=height or self.height_combo.currentData(),
            line_width=self.line_width_spin.value(),
            line_color=(color.red(), color.green(), color.blue(), color.alpha()),
            color_by_group=self.color_by_group_check.isChecked(),
            included_group_names=self.group_list.checked_names(),
            draw_checker_background=self.checker_bg_check.isChecked(),
            island_silhouette_only=self.island_silhouette_check.isChecked(),
            hide_quad_diagonals=self.hide_quad_diagonals_check.isChecked(),
            island_fill=self.island_fill_check.isChecked(),
            fill_color=(fill_color.red(), fill_color.green(), fill_color.blue(), fill_color.alpha()),
            fill_opacity=self.fill_opacity_slider.value() / 100.0,
            # Boundary/island-outline lines now match the regular line
            # width and color rather than being forced 2.5x bolder -- that
            # multiplier was the actual source of the unwanted bold-outline
            # look; RenderOptions defaults to the same behavior on its own,
            # but this is passed explicitly to keep the two in lockstep if
            # either widget's value changes.
            boundary_line_width=self.line_width_spin.value(),
            boundary_line_color=(color.red(), color.green(), color.blue(), color.alpha()),
        )

    def _position_applying_overlay(self):
        self.applying_overlay.adjustSize()
        viewport = self.applying_overlay.parentWidget()
        x = (viewport.width() - self.applying_overlay.width()) // 2
        y = (viewport.height() - self.applying_overlay.height()) // 2
        self.applying_overlay.move(max(0, x), max(0, y))

    def _refresh_preview_with_overlay(self):
        """Same as _refresh_preview(), but shows a small "Applying…"
        indicator over the preview while island fill is (re)computed --
        filling every island's interior is the one preview-affecting option
        that can take long enough to notice, and _refresh_preview() itself
        renders synchronously on the UI thread, so without this the window
        just looks like it briefly hung. Only shown when fill is actually
        on; toggling it off or changing other options stays silent since
        those redraw fast enough not to need it."""
        if self.island_fill_check.isChecked():
            self._position_applying_overlay()
            self.applying_overlay.show()
            self.applying_overlay.raise_()
            # Force the label to actually paint before the synchronous
            # render below blocks the event loop -- without this, show()
            # would just get coalesced away and never actually appear.
            QApplication.processEvents()
        try:
            self._refresh_preview()
        finally:
            self.applying_overlay.hide()

    def _refresh_preview(self):
        if self.current_mesh is None:
            return
        full_width = self.width_combo.currentData()
        full_height = self.height_combo.currentData()
        # Scale down for a fast preview while preserving the aspect ratio of
        # the selected export size.
        preview_longest = 768
        largest = max(full_width, full_height)
        scale = preview_longest / largest
        preview_width = max(1, round(full_width * scale))
        preview_height = max(1, round(full_height * scale))

        opts = self._current_options(width=preview_width, height=preview_height)
        img = render_uv_template(self.current_mesh, opts)
        pix = QPixmap.fromImage(img)
        self.preview_label.setPixmap(pix)
        self.preview_label.setFixedSize(pix.size())
        self._update_export_enabled()

    def _on_export_clicked(self):
        if self.current_mesh is None:
            return
        width = self.width_combo.currentData()
        height = self.height_combo.currentData()
        default_name = os.path.splitext(os.path.basename(self.current_path or "template"))[0]
        default_name += f"_uv_{width}x{height}.png"
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Export UV Template", default_name, "PNG image (*.png)"
        )
        if not out_path:
            return

        opts = self._current_options()
        self.export_button.setEnabled(False)
        self.render_progress.show()
        self.statusBar().showMessage(f"Rendering {width}x{height} template…")

        self.render_worker = RenderWorker(self.current_mesh, opts, out_path)
        self.render_worker.finished_ok.connect(self._on_render_success)
        self.render_worker.failed.connect(self._on_render_failed)
        self.render_worker.start()

    def _on_render_success(self, _img, out_path: str):
        self.render_progress.hide()
        self.export_button.setEnabled(True)
        self.statusBar().showMessage(f"Saved to {out_path}", 8000)
        if self.current_path is not None:
            save_selection(self.current_path, self._capture_current_selection())
            self.clear_selection_btn.setEnabled(True)
        QMessageBox.information(self, "Export complete", f"UV template saved to:\n{out_path}")

    def _on_render_failed(self, message: str):
        self.render_progress.hide()
        self.export_button.setEnabled(True)
        self.statusBar().showMessage("Export failed.", 6000)
        QMessageBox.critical(self, "Export failed", message)
