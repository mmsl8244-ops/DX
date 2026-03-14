# ui/dialogs_pulse.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

from PyQt5.QtWidgets import (
    QDialog, QListWidget, QListWidgetItem, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSplitter, QFrame, QMessageBox, QComboBox, QFormLayout,
    QGroupBox, QScrollArea, QSizePolicy, QCheckBox, QDoubleSpinBox,
    QAbstractItemView
)
from PyQt5.QtCore import Qt, pyqtSignal, QMimeData
from PyQt5.QtGui import QDrag

# -----------------------------
# Drag source: Parameter list
# -----------------------------
class ParamListWidget(QListWidget):
    """오른쪽 파라미터 목록. Drag payload에 pid/name/unit/mapping을 담아 보냄."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QListWidget.SingleSelection)
        self.setDragEnabled(True)

    def startDrag(self, supportedActions):
        item = self.currentItem()
        if not item:
            return

        payload = item.data(Qt.UserRole) or {}

        pid = str(payload.get("pid", ""))
        name = str(payload.get("name", "") or payload.get("display", ""))
        unit = str(payload.get("unit", ""))
        mapping = str(payload.get("mapping", ""))

        text = f"{pid}|{name}|{unit}|{mapping}"

        mime = QMimeData()
        mime.setText(text)

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec_(Qt.CopyAction)


# -----------------------------
# Drop target: single field
# -----------------------------
class DropField(QLabel):
    """하나의 파라미터(pid)를 drop으로 받는 필드(표시+저장)."""
    changed = pyqtSignal()

    def __init__(self, title: str, parent=None, allow_empty=True):
        super().__init__(parent)
        self._title = title
        self._allow_empty = allow_empty

        self.pid: Optional[int] = None
        self.name: str = ""
        self.unit: str = ""
        self.mapping: str = ""

        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Box)
        self.setMinimumHeight(28)
        self.setText(self._render())

    def _render(self) -> str:
        if self.pid is None:
            return f"{self._title}: (drop here)"
        u = f" [{self.unit}]" if self.unit else ""
        return f"{self._title}: {self.name}{u}"

    def clear_value(self):
        if not self._allow_empty:
            return
        self.pid = None
        self.name = ""
        self.unit = ""
        self.mapping = ""
        self.setText(self._render())
        self.changed.emit()

    def dragEnterEvent(self, e):
        if e.mimeData().hasText():
            e.setDropAction(Qt.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasText():
            e.setDropAction(Qt.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dropEvent(self, e):
        if not e.mimeData().hasText():
            e.ignore()
            return

        txt = e.mimeData().text()
        parts = txt.split("|")
        if len(parts) < 4:
            e.ignore()
            return

        pid_s, name, unit, mapping = parts[0], parts[1], parts[2], parts[3]

        try:
            self.pid = int(pid_s)
        except Exception:
            self.pid = None

        self.name = (name or "").strip()
        self.unit = (unit or "").strip()
        self.mapping = (mapping or "").strip()

        self.setText(self._render())
        self.changed.emit()

        e.setDropAction(Qt.CopyAction)
        e.accept()

    

    def mouseDoubleClickEvent(self, e):
        # 더블클릭하면 비움 (편의)
        self.clear_value()
        super().mouseDoubleClickEvent(e)




    def to_payload_dict(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "unit": self.unit,
            "mapping": self.mapping,
        }

    def apply_payload_dict(self, payload: dict | None):
        payload = payload or {}
        self.pid = payload.get("pid")
        self.name = payload.get("name", "") or ""
        self.unit = payload.get("unit", "") or ""
        self.mapping = payload.get("mapping", "") or ""
        self.setText(self._render())
        self.changed.emit()


# -----------------------------
# Drop target: multi list (Duty/Amplitude segments)
# -----------------------------

@dataclass
class ViewerConfig:
    # Global (공통값은 Dialog에서 주입)
    pid_duration: Optional[int] = None
    pid_pulse_freq: Optional[int] = None

    # Common local
    pid_mode: Optional[int] = None
    pid_freq: Optional[int] = None              # drag&drop frequency pid
    freq_input_mode: str = "drag"               # "drag" or "manual"
    manual_freq_khz: float = 0.0                # manual 값 (kHz)
    case_type: str = ""                         # case1 / case2 / case3

    # case1
    pid_c1_pulse_duty: Optional[int] = None
    pid_c1_duty: Optional[int] = None
    pid_c1_offset: Optional[int] = None
    pid_c1_amp: Optional[int] = None
    pid_c1_hsp_offset: Optional[int] = None
    pid_c1_interval_amp_m: Optional[int] = None
    pid_c1_interval_freq: Optional[int] = None
    pid_c1_interval_duty: Optional[int] = None

    # case2
    pid_c2_m1_amp: Optional[int] = None
    pid_c2_m2_amp: Optional[int] = None
    pid_c2_m3_amp: Optional[int] = None
    pid_c2_m4_amp: Optional[int] = None
    pid_c2_m1_duty: Optional[int] = None
    pid_c2_m2_duty: Optional[int] = None
    pid_c2_m3_duty: Optional[int] = None
    pid_c2_hsp_duty: Optional[int] = None
    pid_c2_hsp_freq: Optional[int] = None

    # case3
    pid_c3_pulse_duty: Optional[int] = None
    pid_c3_amp_h: Optional[int] = None
    pid_c3_offset: Optional[int] = None
    pid_c3_amp_l: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)
class ViewerCard(QGroupBox):
    changed = pyqtSignal()

    def __init__(self, title: str, parent=None):
        super().__init__(title, parent)

        # ---------------------------
        # 공통 필드
        # ---------------------------
        self.f_mode = DropField("Local Mode", self)
        self.f_freq_drop = DropField("Local Frequency", self)

        self.chk_manual_freq = QCheckBox("Manual", self)
        self.spin_manual_freq = QDoubleSpinBox(self)
        self.spin_manual_freq.setDecimals(3)
        self.spin_manual_freq.setRange(0.0, 1_000_000_000.0)
        self.spin_manual_freq.setSuffix(" kHz")
        self.spin_manual_freq.setEnabled(False)

        # ---------------------------
        # case1 fields
        # ---------------------------
        self.c1_pulse_duty = DropField("Pulse Duty", self)
        self.c1_duty = DropField("Duty", self)
        self.c1_offset = DropField("Offset", self)
        self.c1_amp = DropField("Amp.", self)
        self.c1_hsp_offset = DropField("HSP Offset", self)
        self.c1_interval_amp_m = DropField("Interval Amp.(M)", self)
        self.c1_interval_freq = DropField("Interval Frequency", self)
        self.c1_interval_duty = DropField("Interval Duty", self)

        # ---------------------------
        # case2 fields
        # ---------------------------
        self.c2_m1_amp = DropField("M1 Amp.", self)
        self.c2_m2_amp = DropField("M2 Amp.", self)
        self.c2_m3_amp = DropField("M3 Amp.", self)
        self.c2_m4_amp = DropField("M4 Amp.", self)

        self.c2_m1_duty = DropField("M1 Duty", self)
        self.c2_m2_duty = DropField("M2 Duty", self)
        self.c2_m3_duty = DropField("M3 Duty", self)

        self.c2_hsp_duty = DropField("HSP Duty", self)
        self.c2_hsp_freq = DropField("HSP Local Freq", self)

        # ---------------------------
        # case3 fields
        # ---------------------------
        self.c3_pulse_duty = DropField("Pulse Duty", self)
        self.c3_amp_h = DropField("Amp.", self)
        self.c3_offset = DropField("Offset", self)
        self.c3_amp_l = DropField("Amp.(L)", self)

        # ---------------------------
        # 메인 레이아웃
        # ---------------------------
        self.main_layout = QVBoxLayout(self)

        self.common_form = QFormLayout()
        self.common_form.addRow(self.f_mode)

        freq_wrap = QWidget(self)
        freq_h = QHBoxLayout(freq_wrap)
        freq_h.setContentsMargins(0, 0, 0, 0)
        freq_h.setSpacing(6)
        freq_h.addWidget(self.f_freq_drop, 1)
        freq_h.addWidget(self.chk_manual_freq, 0)
        freq_h.addWidget(self.spin_manual_freq, 0)

        self.common_form.addRow("Local Frequency:", freq_wrap)
        self.main_layout.addLayout(self.common_form)

        # ---------------------------
        # case1 group
        # ---------------------------
        self.case1_box = QWidget(self)
        self.case1_form = QFormLayout(self.case1_box)
        self.case1_form.setContentsMargins(0, 0, 0, 0)
        self.case1_form.setSpacing(6)

        self.case1_form.addRow(self.c1_pulse_duty)
        self.case1_form.addRow(self.c1_duty)
        self.case1_form.addRow(self.c1_offset)
        self.case1_form.addRow(self.c1_amp)
        self.case1_form.addRow(self.c1_hsp_offset)
        self.case1_form.addRow(self.c1_interval_amp_m)
        self.case1_form.addRow(self.c1_interval_freq)
        self.case1_form.addRow(self.c1_interval_duty)

        self.main_layout.addWidget(self.case1_box)

        # ---------------------------
        # case2 group
        # ---------------------------
        self.case2_box = QWidget(self)
        self.case2_layout = QVBoxLayout(self.case2_box)
        self.case2_layout.setContentsMargins(0, 0, 0, 0)
        self.case2_layout.setSpacing(6)

        self.case2_title = QLabel("Multistate Mode", self.case2_box)
        self.case2_title.setStyleSheet("font-weight: bold;")
        self.case2_layout.addWidget(self.case2_title)

        self.case2_form = QFormLayout()
        self.case2_form.addRow(self.c2_m1_amp)
        self.case2_form.addRow(self.c2_m2_amp)
        self.case2_form.addRow(self.c2_m3_amp)
        self.case2_form.addRow(self.c2_m4_amp)
        self.case2_form.addRow(self.c2_m1_duty)
        self.case2_form.addRow(self.c2_m2_duty)
        self.case2_form.addRow(self.c2_m3_duty)
        self.case2_form.addRow(self.c2_hsp_duty)
        self.case2_form.addRow(self.c2_hsp_freq)

        self.case2_layout.addLayout(self.case2_form)
        self.main_layout.addWidget(self.case2_box)

        # ---------------------------
        # case3 group
        # ---------------------------
        self.case3_box = QWidget(self)
        self.case3_form = QFormLayout(self.case3_box)
        self.case3_form.setContentsMargins(0, 0, 0, 0)
        self.case3_form.setSpacing(6)

        self.case3_form.addRow(self.c3_pulse_duty)
        self.case3_form.addRow(self.c3_amp_h)
        self.case3_form.addRow(self.c3_offset)
        self.case3_form.addRow(self.c3_amp_l)

        self.main_layout.addWidget(self.case3_box)

        self.setLayout(self.main_layout)
        self.setMinimumHeight(360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        # ---------------------------
        # 시그널
        # ---------------------------
        self.f_mode.changed.connect(self.changed.emit)

        self.f_freq_drop.changed.connect(self._apply_case_visibility)
        self.f_freq_drop.changed.connect(self.changed.emit)

        self.chk_manual_freq.toggled.connect(self._on_manual_toggled)
        self.spin_manual_freq.valueChanged.connect(self._apply_case_visibility)
        self.spin_manual_freq.valueChanged.connect(lambda *_: self.changed.emit())

        all_fields = [
            self.c1_pulse_duty, self.c1_duty, self.c1_offset, self.c1_amp,
            self.c1_hsp_offset, self.c1_interval_amp_m,
            self.c1_interval_freq, self.c1_interval_duty,

            self.c2_m1_amp, self.c2_m2_amp, self.c2_m3_amp, self.c2_m4_amp,
            self.c2_m1_duty, self.c2_m2_duty, self.c2_m3_duty, self.c2_hsp_duty, self.c2_hsp_freq,

            self.c3_pulse_duty, self.c3_amp_h, self.c3_offset, self.c3_amp_l
        ]
        for w in all_fields:
            w.changed.connect(self.changed.emit)

        self._apply_case_visibility()

    def _on_manual_toggled(self, checked: bool):
        self.spin_manual_freq.setEnabled(checked)
        self.f_freq_drop.setEnabled(not checked)

        if checked:
            self.f_freq_drop.clear_value()

        self._apply_case_visibility()
        self.changed.emit()

    def _current_case_type(self) -> str:
        if self.chk_manual_freq.isChecked():
            return "case2" if float(self.spin_manual_freq.value()) != 0.0 else "case3"
        else:
            return "case1" if self.f_freq_drop.pid is not None else ""

    def _apply_case_visibility(self):
        case_type = self._current_case_type()

        self.case1_box.setVisible(case_type == "case1")
        self.case2_box.setVisible(case_type == "case2")
        self.case3_box.setVisible(case_type == "case3")

    def get_config(self) -> ViewerConfig:
        return ViewerConfig(
            pid_duration=None,
            pid_pulse_freq=None,

            pid_mode=self.f_mode.pid,
            pid_freq=self.f_freq_drop.pid,
            freq_input_mode="manual" if self.chk_manual_freq.isChecked() else "drag",
            manual_freq_khz=float(self.spin_manual_freq.value()),
            case_type=self._current_case_type(),

            # case1
            pid_c1_pulse_duty=self.c1_pulse_duty.pid,
            pid_c1_duty=self.c1_duty.pid,
            pid_c1_offset=self.c1_offset.pid,
            pid_c1_amp=self.c1_amp.pid,
            pid_c1_hsp_offset=self.c1_hsp_offset.pid,
            pid_c1_interval_amp_m=self.c1_interval_amp_m.pid,
            pid_c1_interval_freq=self.c1_interval_freq.pid,
            pid_c1_interval_duty=self.c1_interval_duty.pid,

            # case2
            pid_c2_m1_amp=self.c2_m1_amp.pid,
            pid_c2_m2_amp=self.c2_m2_amp.pid,
            pid_c2_m3_amp=self.c2_m3_amp.pid,
            pid_c2_m4_amp=self.c2_m4_amp.pid,
            pid_c2_m1_duty=self.c2_m1_duty.pid,
            pid_c2_m2_duty=self.c2_m2_duty.pid,
            pid_c2_m3_duty=self.c2_m3_duty.pid,
            pid_c2_hsp_duty=self.c2_hsp_duty.pid,
            pid_c2_hsp_freq=self.c2_hsp_freq.pid,

            # case3
            pid_c3_pulse_duty=self.c3_pulse_duty.pid,
            pid_c3_amp_h=self.c3_amp_h.pid,
            pid_c3_offset=self.c3_offset.pid,
            pid_c3_amp_l=self.c3_amp_l.pid,
        )
    

    def export_persist_data(self) -> dict:
        return {
            "mode": self.f_mode.to_payload_dict(),
            "freq_drop": self.f_freq_drop.to_payload_dict(),
            "manual_enabled": self.chk_manual_freq.isChecked(),
            "manual_freq_khz": float(self.spin_manual_freq.value()),

            "case1": {
                "pulse_duty": self.c1_pulse_duty.to_payload_dict(),
                "duty": self.c1_duty.to_payload_dict(),
                "offset": self.c1_offset.to_payload_dict(),
                "amp": self.c1_amp.to_payload_dict(),
                "hsp_offset": self.c1_hsp_offset.to_payload_dict(),
                "interval_amp_m": self.c1_interval_amp_m.to_payload_dict(),
                "interval_freq": self.c1_interval_freq.to_payload_dict(),
                "interval_duty": self.c1_interval_duty.to_payload_dict(),
            },

            "case2": {
                "m1_amp": self.c2_m1_amp.to_payload_dict(),
                "m2_amp": self.c2_m2_amp.to_payload_dict(),
                "m3_amp": self.c2_m3_amp.to_payload_dict(),
                "m4_amp": self.c2_m4_amp.to_payload_dict(),
                "m1_duty": self.c2_m1_duty.to_payload_dict(),
                "m2_duty": self.c2_m2_duty.to_payload_dict(),
                "m3_duty": self.c2_m3_duty.to_payload_dict(),
                "hsp_duty": self.c2_hsp_duty.to_payload_dict(),
                "hsp_freq": self.c2_hsp_freq.to_payload_dict(),
            },

            "case3": {
                "pulse_duty": self.c3_pulse_duty.to_payload_dict(),
                "amp_h": self.c3_amp_h.to_payload_dict(),
                "offset": self.c3_offset.to_payload_dict(),
                "amp_l": self.c3_amp_l.to_payload_dict(),
            }
        }

    def apply_persist_data(self, data: dict):
        data = data or {}

        # common
        self.f_mode.apply_payload_dict(data.get("mode"))
        self.f_freq_drop.apply_payload_dict(data.get("freq_drop"))

        manual_enabled = bool(data.get("manual_enabled", False))
        manual_freq_khz = float(data.get("manual_freq_khz", 0.0))

        self.chk_manual_freq.blockSignals(True)
        self.spin_manual_freq.blockSignals(True)

        self.chk_manual_freq.setChecked(manual_enabled)
        self.spin_manual_freq.setValue(manual_freq_khz)
        self.spin_manual_freq.setEnabled(manual_enabled)
        self.f_freq_drop.setEnabled(not manual_enabled)

        self.chk_manual_freq.blockSignals(False)
        self.spin_manual_freq.blockSignals(False)

        # case1
        c1 = data.get("case1", {}) or {}
        self.c1_pulse_duty.apply_payload_dict(c1.get("pulse_duty"))
        self.c1_duty.apply_payload_dict(c1.get("duty"))
        self.c1_offset.apply_payload_dict(c1.get("offset"))
        self.c1_amp.apply_payload_dict(c1.get("amp"))
        self.c1_hsp_offset.apply_payload_dict(c1.get("hsp_offset"))
        self.c1_interval_amp_m.apply_payload_dict(c1.get("interval_amp_m"))
        self.c1_interval_freq.apply_payload_dict(c1.get("interval_freq"))
        self.c1_interval_duty.apply_payload_dict(c1.get("interval_duty"))

        # case2
        c2 = data.get("case2", {}) or {}
        self.c2_m1_amp.apply_payload_dict(c2.get("m1_amp"))
        self.c2_m2_amp.apply_payload_dict(c2.get("m2_amp"))
        self.c2_m3_amp.apply_payload_dict(c2.get("m3_amp"))
        self.c2_m4_amp.apply_payload_dict(c2.get("m4_amp"))
        self.c2_m1_duty.apply_payload_dict(c2.get("m1_duty"))
        self.c2_m2_duty.apply_payload_dict(c2.get("m2_duty"))
        self.c2_m3_duty.apply_payload_dict(c2.get("m3_duty"))
        self.c2_hsp_duty.apply_payload_dict(c2.get("hsp_duty"))
        self.c2_hsp_freq.apply_payload_dict(c2.get("hsp_freq"))

        # case3
        c3 = data.get("case3", {}) or {}
        self.c3_pulse_duty.apply_payload_dict(c3.get("pulse_duty"))
        self.c3_amp_h.apply_payload_dict(c3.get("amp_h"))
        self.c3_offset.apply_payload_dict(c3.get("offset"))
        self.c3_amp_l.apply_payload_dict(c3.get("amp_l"))

        self._apply_case_visibility()
        self.changed.emit()
class PulseSettingDialog(QDialog):
    """
    Pulse Setting UI
    - 오른쪽: 파라미터 목록(드래그 소스)
    - 왼쪽: Viewer 카드들(드롭 타겟) + 추가/삭제
    """

    def __init__(self, parent=None, parameters: List[dict] | None = None, default_viewers: int = 3):
        super().__init__(parent)
        self.setWindowTitle("Pulse Setting")
        self.resize(1400, 850)
        self.setMinimumSize(1200, 780)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._parameters = parameters or []
        self._viewer_cards: List[ViewerCard] = []

        root = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal, self)
        root.addWidget(splitter)

        # --- Left: viewer area (scroll) ---
        left_wrap = QWidget()
        left_layout = QVBoxLayout(left_wrap)

        # ✅ Global 공통 설정 박스 (상단 1번만)
        global_box = QGroupBox("Global (Common for all viewers)")
        global_form = QFormLayout(global_box)

        self.f_global_duration = DropField("Global Duration (E/T)", global_box, allow_empty=False)
        self.f_global_pf = DropField("Global Pulse Frequency", global_box, allow_empty=False)

        global_form.addRow(self.f_global_duration)
        global_form.addRow(self.f_global_pf)

        left_layout.addWidget(global_box, 0)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("+ Viewer")
        self.btn_del = QPushButton("- Viewer")
        self.btn_del.setEnabled(False)
        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_del)
        btn_row.addStretch(1)

        left_layout.addLayout(btn_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_body = QWidget()
        self.scroll_v = QVBoxLayout(self.scroll_body)
        self.scroll_v.setContentsMargins(6, 6, 6, 6)
        self.scroll_v.setSpacing(16)
        self.scroll.setWidget(self.scroll_body)

        left_layout.addWidget(self.scroll, 1)

        # --- Right: parameter list ---
        right_wrap = QWidget()
        right_layout = QVBoxLayout(right_wrap)
        right_layout.addWidget(QLabel("Parameters (Drag & Drop):"))

        self.param_list = ParamListWidget()
        self.param_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        for p in self._parameters:
            display = p.get("display") or f"{p.get('name','')} [{p.get('unit','')}]"
            it = QListWidgetItem(display)
            it.setData(Qt.UserRole, p)
            self.param_list.addItem(it)

        right_layout.addWidget(self.param_list)

        # --- Bottom buttons ---
        bottom = QHBoxLayout()
        self.btn_ok = QPushButton("Save")
        self.btn_cancel = QPushButton("Cancel")
        bottom.addStretch(1)
        bottom.addWidget(self.btn_ok)
        bottom.addWidget(self.btn_cancel)

        left_outer = QWidget()
        left_outer_l = QVBoxLayout(left_outer)
        left_outer_l.addWidget(left_wrap, 1)
        left_outer_l.addLayout(bottom, 0)

        splitter.addWidget(left_outer)
        splitter.addWidget(right_wrap)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([900, 400])

        # signals
        self.btn_add.clicked.connect(self._add_viewer)
        self.btn_del.clicked.connect(self._remove_viewer)
        self.btn_ok.clicked.connect(self._on_save)
        self.btn_cancel.clicked.connect(self.reject)

        # init viewers
        for _ in range(max(1, int(default_viewers))):
            self._add_viewer()

        self._update_btn_state()

    def _update_btn_state(self):
        self.btn_del.setEnabled(len(self._viewer_cards) > 1)

    def _add_viewer(self):
        idx = len(self._viewer_cards) + 1

        # ✅ 구분선 먼저 추가 (첫 카드 제외)
        if idx > 1:
            line = QFrame(self.scroll_body)
            line.setFrameShape(QFrame.HLine)
            line.setFrameShadow(QFrame.Sunken)  # 또는 Plain
            line.setLineWidth(3)                # 굵기
            line.setStyleSheet("color: #444;")  # 라인 색
            self.scroll_v.addWidget(line)

        card = ViewerCard(f"Viewer {idx}", self.scroll_body)
        self._viewer_cards.append(card)
        self.scroll_v.addWidget(card)
        self._update_btn_state()
        
        

    def _remove_viewer(self):
        if len(self._viewer_cards) <= 1:
            return
        card = self._viewer_cards.pop()
        card.setParent(None)
        card.deleteLater()

        # re-title
        for i, c in enumerate(self._viewer_cards, start=1):
            c.setTitle(f"Viewer {i}")

        self._update_btn_state()

    def _on_save(self):
        if self.f_global_duration.pid is None or self.f_global_pf.pid is None:
            QMessageBox.warning(self, "Validation", "Please set Global Duration and Global Pulse Frequency.")
            return

        for i, card in enumerate(self._viewer_cards, start=1):
            cfg = card.get_config()

            if cfg.pid_mode is None:
                QMessageBox.warning(self, "Validation", f"Viewer {i}: Please set Local Mode.")
                return

            if cfg.case_type == "":
                QMessageBox.warning(self, "Validation", f"Viewer {i}: Please set Local Frequency.")
                return

        self.accept()

    def get_all_configs(self) -> List[dict]:
        g_duration = self.f_global_duration.pid
        g_pf = self.f_global_pf.pid

        out = []
        for c in self._viewer_cards:
            d = c.get_config().to_dict()
            # ✅ 공통값 주입 (기존 main 로직 유지용)
            d["pid_duration"] = g_duration
            d["pid_pulse_freq"] = g_pf
            out.append(d)

        return out
    
    def _rebuild_viewers(self, count: int):
        # 기존 레이아웃 비우기
        while self.scroll_v.count():
            item = self.scroll_v.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        self._viewer_cards = []

        for _ in range(max(1, int(count))):
            self._add_viewer()

        self._update_btn_state()

    def export_persist_data(self) -> dict:
        return {
            "global": {
                "duration": self.f_global_duration.to_payload_dict(),
                "pulse_freq": self.f_global_pf.to_payload_dict(),
            },
            "viewers": [c.export_persist_data() for c in self._viewer_cards]
        }

    def apply_persist_data(self, data: dict):
        data = data or {}

        g = data.get("global", {}) or {}
        self.f_global_duration.apply_payload_dict(g.get("duration"))
        self.f_global_pf.apply_payload_dict(g.get("pulse_freq"))

        viewers = data.get("viewers", []) or []
        if viewers:
            self._rebuild_viewers(len(viewers))
            for card, saved in zip(self._viewer_cards, viewers):
                card.apply_persist_data(saved)