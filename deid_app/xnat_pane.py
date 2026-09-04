"""The "XNAT server" tab: pick a project and a study, name the session, upload.

The pane owns no network code and no upload threads. It shows what MainWindow
tells it (projects, existing session labels, the loaded studies) and asks for
things through signals; MainWindow forwards those to XnatWorker and
UploadManager. That keeps the pane testable with nothing but a QApplication.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QRegularExpression, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .model import Series
from .xnat_upload import (
    PHASE_ARCHIVING,
    StudyRow,
    UploadResult,
    default_session_labels,
    group_studies,
    sanitise_label,
)

log = logging.getLogger(__name__)

STUDY_UID_ROLE = Qt.UserRole + 1

# How often finished-but-not-archived rows are looked up on the server.
STATUS_REFRESH_MS = 60_000

# Colours for the Status column of the uploads table.
STATUS_COLORS = {
    "queued": QColor("#7f8c8d"),
    "running": QColor("#1565c0"),
    "archived": QColor("#2e7d32"),
    "in prearchive": QColor("#2e7d32"),
    "failed": QColor("#c0392b"),
    "cancelled": QColor("#c87f0a"),
}

STUDY_COLUMNS = ["Patient", "Patient ID", "Study", "Modality", "Series", "Files", "Status"]
STUDY_WIDTHS = [160, 120, 300, 70, 55, 50, 140]
UPLOAD_COLUMNS = ["Subject", "Session", "Project", "Status", "Progress", ""]
UPLOAD_WIDTHS = [150, 220, 110, 180, 260, 70]


def _interactive_columns(table: QTableWidget, widths: list[int]) -> None:
    """Every column draggable by its separator, starting at `widths`.

    The last column stretches to soak up leftover width so the table never
    shows a ragged right edge, but stays draggable like the rest.
    """
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.Interactive)
    header.setStretchLastSection(True)
    header.setMinimumSectionSize(30)
    for col, width in enumerate(widths):
        table.setColumnWidth(col, width)


class XnatPane(QWidget):
    uploadRequested = Signal(str, str, str)   # study_uid, project_id, session label
    projectChanged = Signal(str)              # project_id
    refreshProjectsRequested = Signal()
    cancelRequested = Signal(int)             # job_id
    statusCheckRequested = Signal(object)     # list[(project, label)]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._user: str | None = None
        self._rows: list[StudyRow] = []
        self._row_by_uid: dict[str, StudyRow] = {}
        # Session labels: the default for every study, the user's overrides,
        # and which studies the user has touched (those keep their text when
        # defaults are recomputed).
        self._labels: dict[str, str] = {}
        self._user_edited: set[str] = set()
        # Labels already in the current project, plus ones handed to jobs
        # this session so a second click cannot reuse one.
        self._existing: set[str] = set()
        self._existing_project: str | None = None
        # Jobs on screen: job_id -> table row; study_uid -> job_id while active.
        self._job_rows: dict[int, int] = {}
        self._active_by_study: dict[str, int] = {}
        self._job_study: dict[int, str] = {}
        self._uploaded: dict[str, str] = {}   # study_uid -> where it went
        self._finished_study: dict[int, str] = {}   # job_id -> study_uid, after it ends
        # Finished rows still waiting to be seen in the archive, refreshed
        # from the server on a timer while logged in: job_id -> (project, label).
        self._watching: dict[int, tuple[str, str]] = {}
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(STATUS_REFRESH_MS)
        self._refresh_timer.timeout.connect(self.request_status_check)
        self._build_ui()
        self._update_controls()

    # -- construction ----------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Project:"))
        # Editable so the user can type to filter: a real server can list
        # hundreds of projects. The completer matches anywhere in "name (ID)",
        # case-insensitively, and picking a match selects that item. Typed
        # text that is not exactly an item counts as no project chosen.
        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(280)
        self.project_combo.setEditable(True)
        self.project_combo.setInsertPolicy(QComboBox.NoInsert)
        self.project_combo.setToolTip(
            "XNAT projects you may upload into. Type part of a name or ID to filter."
        )
        self.project_completer = QCompleter(self.project_combo.model(), self.project_combo)
        self.project_completer.setCompletionMode(QCompleter.PopupCompletion)
        self.project_completer.setFilterMode(Qt.MatchContains)
        self.project_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.project_completer.activated[str].connect(self._on_project_typed)
        self.project_combo.setCompleter(self.project_completer)
        self.project_combo.lineEdit().setPlaceholderText("type to filter projects")
        self.project_combo.lineEdit().editingFinished.connect(self._on_project_edit_finished)
        self.project_combo.lineEdit().textEdited.connect(lambda _t: self._update_controls())
        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        top.addWidget(self.project_combo, 1)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Reload the project list from the server.")
        self.refresh_button.clicked.connect(self.refreshProjectsRequested)
        top.addWidget(self.refresh_button)
        self.status_label = QLabel("Not logged in")
        top.addWidget(self.status_label)
        layout.addLayout(top)
        # One-line notice under the project row: listing failures and the like.
        self.notice_label = QLabel("")
        self.notice_label.setWordWrap(True)
        self.notice_label.setStyleSheet("color: #c0392b;")
        self.notice_label.setVisible(False)
        layout.addWidget(self.notice_label)

        layout.addWidget(QLabel("Loaded studies - one XNAT session each:"))
        self.studies_table = QTableWidget(0, len(STUDY_COLUMNS))
        self.studies_table.setHorizontalHeaderLabels(STUDY_COLUMNS)
        self.studies_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.studies_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.studies_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.studies_table.verticalHeader().setVisible(False)
        _interactive_columns(self.studies_table, STUDY_WIDTHS)
        self.studies_table.itemSelectionChanged.connect(self._on_study_selected)
        layout.addWidget(self.studies_table, 2)

        session_row = QHBoxLayout()
        session_row.addWidget(QLabel("Session label:"))
        self.label_edit = QLineEdit()
        self.label_edit.setValidator(
            QRegularExpressionValidator(QRegularExpression(r"[A-Za-z0-9_-]{0,64}"))
        )
        self.label_edit.setToolTip(
            "The XNAT session (experiment) label. Defaults to "
            "<Patient ID>_<modality>_<n>; letters, digits, '_' and '-' only."
        )
        self.label_edit.textEdited.connect(self._on_label_edited)
        session_row.addWidget(self.label_edit, 1)
        self.default_button = QPushButton("Default")
        self.default_button.setToolTip("Put the generated label back.")
        self.default_button.clicked.connect(self._reset_label)
        session_row.addWidget(self.default_button)
        self.upload_button = QPushButton("Upload to XNAT")
        self.upload_button.clicked.connect(self._on_upload_clicked)
        session_row.addWidget(self.upload_button)
        layout.addLayout(session_row)

        uploads_head = QHBoxLayout()
        uploads_head.addWidget(QLabel("Uploads:"))
        uploads_head.addStretch(1)
        self.clear_button = QPushButton("Clear finished")
        self.clear_button.clicked.connect(self.clear_finished)
        uploads_head.addWidget(self.clear_button)
        layout.addLayout(uploads_head)

        self.uploads_table = QTableWidget(0, len(UPLOAD_COLUMNS))
        self.uploads_table.setHorizontalHeaderLabels(UPLOAD_COLUMNS)
        self.uploads_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.uploads_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.uploads_table.verticalHeader().setVisible(False)
        _interactive_columns(self.uploads_table, UPLOAD_WIDTHS)
        layout.addWidget(self.uploads_table, 1)

    # -- inputs from MainWindow ------------------------------------------
    def show_notice(self, text: str) -> None:
        self.notice_label.setText(text)
        self.notice_label.setVisible(bool(text))

    def set_logged_in(self, user: str | None) -> None:
        self._user = user
        self.show_notice("")
        self._update_refresh_timer()
        if user is None:
            self.status_label.setText("Not logged in")
            self.set_projects([])
        else:
            self.status_label.setText(f"Logged in as {user}")
        self._update_controls()

    def set_projects(self, projects: list[tuple[str, str]]) -> None:
        current = self.current_project()
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        for pid, name in projects:
            text = pid if name == pid else f"{name} ({pid})"
            self.project_combo.addItem(text, pid)
        index = self.project_combo.findData(current) if current else -1
        self.project_combo.setCurrentIndex(index if index >= 0 else (0 if projects else -1))
        self.project_combo.blockSignals(False)
        if projects:
            self.show_notice("")
            log.info("Project list shows %d project(s)", len(projects))
        self._on_project_changed(self.project_combo.currentIndex())

    def set_existing_labels(self, project_id: str, labels: set[str]) -> None:
        """Session labels already in `project_id`, from the server."""
        if project_id != self.current_project():
            return
        self._existing = set(labels)
        self._existing_project = project_id
        self._recompute_defaults()
        self._update_controls()

    def set_series_map(self, series_map: dict[str, Series]) -> None:
        """Rebuild the study rows after a scan (or a clear)."""
        self._rows = group_studies(series_map)
        self._row_by_uid = {r.study_uid: r for r in self._rows}
        self._labels = {}
        self._user_edited = set()
        self._uploaded = {}
        self._recompute_defaults()
        self._fill_studies()
        self._update_controls()

    def refresh_readiness(self) -> None:
        """Cheap update of the Status column and the buttons; no rebuild."""
        for row_index, row in enumerate(self._rows):
            self._set_study_status(row_index, row)
        self._update_controls()

    # -- study table -----------------------------------------------------
    def _fill_studies(self) -> None:
        table = self.studies_table
        table.setRowCount(0)
        table.setRowCount(len(self._rows))
        for row_index, row in enumerate(self._rows):
            values = [
                row.patient_name,
                row.patient_id,
                row.study_description or "(no study description)",
                row.modality,
                str(len(row.series)),
                str(row.file_count),
                "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(STUDY_UID_ROLE, row.study_uid)
                table.setItem(row_index, col, item)
            self._set_study_status(row_index, row)
        if self._rows:
            table.selectRow(0)

    def _set_study_status(self, row_index: int, row: StudyRow) -> None:
        item = self.studies_table.item(row_index, len(STUDY_COLUMNS) - 1)
        if item is None:
            return
        if row.study_uid in self._active_by_study:
            text, color = "uploading", STATUS_COLORS["running"]
        elif row.study_uid in self._uploaded:
            text, color = self._uploaded[row.study_uid], STATUS_COLORS["archived"]
        elif row.ready:
            text, color = "ready", STATUS_COLORS["archived"]
        else:
            n = row.not_ready_count
            text, color = f"{n} series not ready", STATUS_COLORS["failed"]
        item.setText(text)
        item.setForeground(color)

    def selected_row(self) -> StudyRow | None:
        items = self.studies_table.selectedItems()
        if not items:
            return None
        uid = self.studies_table.item(items[0].row(), 0).data(STUDY_UID_ROLE)
        return self._row_by_uid.get(uid)

    @Slot()
    def _on_study_selected(self) -> None:
        row = self.selected_row()
        self.label_edit.blockSignals(True)
        self.label_edit.setText(self._labels.get(row.study_uid, "") if row else "")
        self.label_edit.blockSignals(False)
        self._update_controls()

    # -- labels ----------------------------------------------------------
    def current_project(self) -> str | None:
        """The selected project's ID, or None while the text is a filter."""
        index = self.project_combo.currentIndex()
        if index < 0:
            return None
        if self.project_combo.lineEdit().text() != self.project_combo.itemText(index):
            return None
        data = self.project_combo.itemData(index)
        return str(data) if data else None

    @Slot(str)
    def _on_project_typed(self, text: str) -> None:
        """A completion was picked: select the matching item."""
        index = self.project_combo.findText(text, Qt.MatchFixedString)
        if index >= 0:
            self.project_combo.setCurrentIndex(index)
            self.project_combo.lineEdit().setText(text)
        self._update_controls()

    @Slot()
    def _on_project_edit_finished(self) -> None:
        """Enter/focus-out: an exact item name selects it; anything else
        falls back to the item that is already selected."""
        text = self.project_combo.lineEdit().text()
        index = self.project_combo.findText(text, Qt.MatchFixedString)
        if index >= 0:
            if index != self.project_combo.currentIndex():
                self.project_combo.setCurrentIndex(index)
            else:
                self.project_combo.lineEdit().setText(self.project_combo.itemText(index))
        else:
            current = self.project_combo.currentIndex()
            self.project_combo.lineEdit().setText(
                self.project_combo.itemText(current) if current >= 0 else ""
            )
        self._update_controls()

    def _recompute_defaults(self) -> None:
        """Fresh defaults for every study the user has not edited."""
        existing = self._existing if self._existing_project == self.current_project() else set()
        defaults = default_session_labels(self._rows, existing)
        for uid, label in defaults.items():
            if uid not in self._user_edited:
                self._labels[uid] = label
        row = self.selected_row()
        if row is not None and row.study_uid not in self._user_edited:
            self.label_edit.blockSignals(True)
            self.label_edit.setText(self._labels.get(row.study_uid, ""))
            self.label_edit.blockSignals(False)

    @Slot(str)
    def _on_label_edited(self, text: str) -> None:
        row = self.selected_row()
        if row is None:
            return
        self._labels[row.study_uid] = text
        self._user_edited.add(row.study_uid)
        self._update_controls()

    @Slot()
    def _reset_label(self) -> None:
        row = self.selected_row()
        if row is None:
            return
        self._user_edited.discard(row.study_uid)
        self._recompute_defaults()
        self._update_controls()

    @Slot(int)
    def _on_project_changed(self, index: int) -> None:
        project = self.current_project()
        if project != self._existing_project:
            # Labels from the previous project no longer apply; until the
            # new list arrives, defaults are computed against nothing.
            self._existing = set()
            self._existing_project = None
        self._recompute_defaults()
        self._update_controls()
        if project:
            self.projectChanged.emit(project)

    # -- the upload button -----------------------------------------------
    def _blocker(self) -> str | None:
        """Why Upload is disabled, or None when it may be pressed."""
        if self._user is None:
            return "Log in to XNAT first (the XNAT Login button)."
        if not self.current_project():
            if self.project_combo.lineEdit().text().strip() and self.project_combo.count():
                return "Pick a project from the filtered list (or press Enter on an exact name)."
            return "Choose a project."
        row = self.selected_row()
        if row is None:
            return "Select a study to upload." if self._rows else "Load a DICOM directory first."
        if row.study_uid in self._active_by_study:
            return "This study is already queued or uploading."
        if not row.ready:
            n = row.not_ready_count
            return (f"{n} series in this study must be reviewed, committed or "
                    "skipped first.")
        label = self.label_edit.text().strip()
        if not label:
            return "Enter a session label."
        if label in self._existing:
            return (f"A session labelled {label} already exists in project "
                    f"{self.current_project()}.")
        return None

    def _update_controls(self) -> None:
        blocker = self._blocker()
        self.upload_button.setEnabled(blocker is None)
        row = self.selected_row()
        if blocker is None and row is not None:
            self.upload_button.setToolTip(
                f"Upload {row.file_count} file(s) as {self.label_edit.text().strip()} "
                f"into project {self.current_project()}"
            )
        else:
            self.upload_button.setToolTip(blocker or "")
        self.refresh_button.setEnabled(self._user is not None)
        self.project_combo.setEnabled(self._user is not None and self.project_combo.count() > 0)
        has_row = row is not None
        self.label_edit.setEnabled(has_row)
        self.default_button.setEnabled(has_row and row.study_uid in self._user_edited)

    @Slot()
    def _on_upload_clicked(self) -> None:
        row = self.selected_row()
        project = self.current_project()
        if row is None or project is None or self._blocker() is not None:
            return
        label = sanitise_label(self.label_edit.text())
        # Taken from now on, whatever the server says: the same label must not
        # be handed to a second click before the first upload lands.
        self._existing.add(label)
        self.uploadRequested.emit(row.study_uid, project, label)

    # -- uploads table ---------------------------------------------------
    def add_job(self, job_id: int, study_uid: str, subject: str, session: str,
                project: str) -> None:
        table = self.uploads_table
        row_index = table.rowCount()
        table.insertRow(row_index)
        for col, value in enumerate([subject, session, project, "queued"]):
            item = QTableWidgetItem(value)
            table.setItem(row_index, col, item)
        bar = QProgressBar()
        bar.setRange(0, 1)
        bar.setValue(0)
        bar.setFormat("queued")
        bar.setTextVisible(True)
        table.setCellWidget(row_index, 4, bar)
        cancel = QPushButton("Cancel")
        cancel.setToolTip(
            "Remove a waiting upload, or stop a running one between files. "
            "A zip that is already being sent completes first."
        )
        cancel.clicked.connect(lambda _=False, jid=job_id: self.cancelRequested.emit(jid))
        table.setCellWidget(row_index, 5, cancel)
        self._job_rows[job_id] = row_index
        self._active_by_study[study_uid] = job_id
        self._job_study[job_id] = study_uid
        self._set_job_status(job_id, "queued")
        self.refresh_readiness()

    def _set_job_status(self, job_id: int, text: str, tooltip: str = "",
                        key: str | None = None) -> None:
        row_index = self._job_rows.get(job_id)
        if row_index is None:
            return
        item = self.uploads_table.item(row_index, 3)
        item.setText(text)
        item.setToolTip(tooltip)
        item.setForeground(STATUS_COLORS.get(key or text, QColor("black")))

    def _bar(self, job_id: int) -> QProgressBar | None:
        row_index = self._job_rows.get(job_id)
        if row_index is None:
            return None
        return self.uploads_table.cellWidget(row_index, 4)

    # -- periodic status refresh -----------------------------------------
    def _update_refresh_timer(self) -> None:
        if self._user is not None and self._watching:
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()
        else:
            self._refresh_timer.stop()

    @Slot()
    def request_status_check(self) -> None:
        """Ask for the server's view of every finished row not yet archived."""
        if self._user is None or not self._watching:
            return
        self.statusCheckRequested.emit(sorted(set(self._watching.values())))

    @Slot(object)
    def apply_session_status(self, statuses) -> None:
        """Update rows from a check_sessions answer; stop watching archived ones."""
        for job_id, key in list(self._watching.items()):
            status = statuses.get(key)
            if not status or status == "missing":
                continue
            row_index = self._job_rows.get(job_id)
            if status == "archived":
                self._set_job_status(job_id, "archived", tooltip=self._tooltip(job_id), key="archived")
                del self._watching[job_id]
                log.info("%s/%s is now archived", *key)
            elif status.startswith("prearchive:"):
                state = status.split(":", 1)[1].strip().lower() or "?"
                self._set_job_status(job_id, f"in prearchive ({state})",
                                     tooltip=self._tooltip(job_id), key="in prearchive")
            if row_index is not None:
                study_uid = self._study_of_row(row_index)
                if study_uid is not None:
                    self._uploaded[study_uid] = self.job_status_text(job_id)
        self.refresh_readiness()
        self._update_refresh_timer()

    def _tooltip(self, job_id: int) -> str:
        row_index = self._job_rows.get(job_id)
        if row_index is None:
            return ""
        return self.uploads_table.item(row_index, 3).toolTip()

    def _study_of_row(self, row_index: int) -> str | None:
        for job_id, idx in self._job_rows.items():
            if idx == row_index:
                return self._finished_study.get(job_id)
        return None

    def _finish_job(self, job_id: int, outcome: str | None) -> None:
        """Release the study, remove the Cancel button, note where it went."""
        study_uid = self._job_study.pop(job_id, None)
        if study_uid is not None:
            self._finished_study[job_id] = study_uid
            self._active_by_study.pop(study_uid, None)
            if outcome:
                self._uploaded[study_uid] = outcome
        row_index = self._job_rows.get(job_id)
        if row_index is not None:
            self.uploads_table.removeCellWidget(row_index, 5)
        self.refresh_readiness()

    @Slot(int)
    def set_job_started(self, job_id: int) -> None:
        self._set_job_status(job_id, "running")
        bar = self._bar(job_id)
        if bar is not None:
            bar.setRange(0, 0)
            bar.setFormat("starting...")

    @Slot(int, int, int, str)
    def set_job_progress(self, job_id: int, done: int, total: int, phase: str) -> None:
        bar = self._bar(job_id)
        if bar is None:
            return
        if total <= 0:
            bar.setRange(0, 0)
            bar.setFormat(f"{phase}...")
            return
        bar.setRange(0, total)
        bar.setValue(done)
        if phase == "Uploading" and total > 100000:
            bar.setFormat(f"{phase} %p% ({done // 1024 // 1024} / {total // 1024 // 1024} MB)")
        else:
            bar.setFormat(f"{phase} %v / %m")
        self._set_job_status(job_id, phase.lower(), key="running")

    @Slot(int, object)
    def set_job_finished(self, job_id: int, result: UploadResult) -> None:
        where = result.where
        self._set_job_status(job_id, where, tooltip=f"{result.describe()}\n{result.uri}",
                             key="archived")
        if not result.archived:
            row_index = self._job_rows.get(job_id)
            if row_index is not None:
                project = self.uploads_table.item(row_index, 2).text()
                label = self.uploads_table.item(row_index, 1).text()
                self._watching[job_id] = (project, label)
        bar = self._bar(job_id)
        if bar is not None:
            bar.setRange(0, 1)
            bar.setValue(1)
            bar.setFormat("done")
        self._finish_job(job_id, where)
        self._update_refresh_timer()

    @Slot(int, str)
    def set_job_failed(self, job_id: int, message: str) -> None:
        self._set_job_status(job_id, "failed", tooltip=message)
        bar = self._bar(job_id)
        if bar is not None:
            bar.setRange(0, 1)
            bar.setValue(0)
            bar.setFormat("failed")
        # The label was never used on the server; let it be tried again.
        row_index = self._job_rows.get(job_id)
        if row_index is not None:
            self._existing.discard(self.uploads_table.item(row_index, 1).text())
        self._finish_job(job_id, None)

    @Slot(int)
    def set_job_cancelled(self, job_id: int) -> None:
        self._set_job_status(job_id, "cancelled")
        bar = self._bar(job_id)
        if bar is not None:
            bar.setRange(0, 1)
            bar.setValue(0)
            bar.setFormat("cancelled")
        row_index = self._job_rows.get(job_id)
        if row_index is not None:
            self._existing.discard(self.uploads_table.item(row_index, 1).text())
        self._finish_job(job_id, None)

    @Slot()
    def clear_finished(self) -> None:
        """Drop rows of jobs that are no longer active."""
        table = self.uploads_table
        keep = set(self._job_study)   # still active
        for job_id in sorted(self._job_rows, key=self._job_rows.get, reverse=True):
            if job_id in keep:
                continue
            table.removeRow(self._job_rows.pop(job_id))
            self._watching.pop(job_id, None)
        self._update_refresh_timer()
        # Re-index the survivors.
        self._job_rows = {jid: idx for idx, jid in
                          enumerate(sorted(self._job_rows, key=self._job_rows.get))}

    def job_status_text(self, job_id: int) -> str:
        row_index = self._job_rows.get(job_id)
        if row_index is None:
            return ""
        return self.uploads_table.item(row_index, 3).text()
