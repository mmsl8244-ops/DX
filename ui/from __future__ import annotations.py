from __future__ import annotations
import math
from typing import Any

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QSplitter, QScrollArea, QSizePolicy, QMessageBox
)
from PyQt5.QtCore import Qt

import pyqtgraph as pg


class PulseViewerDialog(QDialog):
    def __init__(self, parent=None, pulse_setting: dict | None = None,
                 recipe_data: list[dict] | None = None,
                 pid_to_mapping: dict[int, str] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Pulse Viewer")
        self.resize(1600, 900)

        self.pulse_setting = pulse_setting or {}
        self.recipe_data = recipe_data or []
        self.pid_to_mapping = pid_to_mapping or {}

        root = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal, self)
        root.addWidget(splitter)

        # -------------------------
        # Left: recipe checklist
        # -------------------------
        left = QWidget(self)
        left_l = QVBoxLayout(left)
        left_l.addWidget(QLabel("Recipes"))

        self.recipe_list = QListWidget(self)
        for r in self.recipe_data:
            item = QListWidgetItem(r["recipe_code"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, r)
            self.recipe_list.addItem(item)

        self.recipe_list.itemChanged.connect(self._render_all)
        left_l.addWidget(self.recipe_list)

        # -------------------------
        # Right: plots
        # -------------------------
        right = QWidget(self)
        right_l = QVBoxLayout(right)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)

        self.scroll_body = QWidget(self)
        self.scroll_v = QVBoxLayout(self.scroll_body)
        self.scroll.setWidget(self.scroll_body)

        right_l.addWidget(self.scroll)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([260, 1300])

        self._render_all()

    # ------------------------------------
    # Data helpers
    # ------------------------------------
    def _checked_recipes(self) -> list[dict]:
        out = []
        for i in range(self.recipe_list.count()):
            item = self.recipe_list.item(i)
            if item.checkState() == Qt.Checked:
                out.append(item.data(Qt.UserRole))
        return out

    def _pid_value_from_step(self, step_params: dict, pid: int | None) -> float | None:
        if pid is None:
            return None
        mapping = self.pid_to_mapping.get(pid)
        if not mapping:
            return None
        raw = step_params.get(mapping)
        if raw in (None, "", "NULL", "null"):
            return None
        try:
            return float(raw)
        except Exception:
            return None

    # ------------------------------------
    # Waveform builders
    # ------------------------------------
    def _build_global_mask(self, t, global_freq_hz: float):
        if global_freq_hz <= 0:
            return [0.0 for _ in t]

        period = 1.0 / global_freq_hz
        on_time = period / 2.0   # 여기선 기본 50% ON으로 표시
        out = []
        for x in t:
            phase = x % period
            out.append(1.0 if phase < on_time else 0.0)
        return out

    def _square_wave(self, t, freq_hz: float, duty_pct: float, amp_h: float = 1.0, amp_l: float = 0.0, offset_pct: float = 0.0):
        if freq_hz <= 0:
            return [amp_l for _ in t]

        period = 1.0 / freq_hz
        shift = (offset_pct / 100.0) * period
        duty_frac = max(0.0, min(1.0, duty_pct / 100.0))

        out = []
        for x in t:
            phase = (x - shift) % period
            out.append(amp_h if phase < (period * duty_frac) else amp_l)
        return out

    def _sine_wave(self, t, freq_hz: float, amp: float = 1.0):
        if freq_hz <= 0:
            return [0.0 for _ in t]
        return [amp * math.sin(2.0 * math.pi * freq_hz * x) for x in t]

    def _apply_global_on(self, local, global_mask):
        return [a * g for a, g in zip(local, global_mask)]

    def _build_viewer_wave(self, viewer_cfg: dict, step: dict):
        params = step["params"]

        # global
        g_duration = self._pid_value_from_step(params, viewer_cfg.get("pid_duration"))
        g_freq = self._pid_value_from_step(params, viewer_cfg.get("pid_pulse_freq"))

        if g_duration is None or g_duration <= 0:
            g_duration = 1000.0   # ms fallback
        if g_freq is None or g_freq <= 0:
            g_freq = 1000.0       # Hz fallback

        # duration 단위는 일단 ms로 가정
        duration_sec = g_duration / 1000.0

        # 샘플 수 적당히 제한
        sample_count = 1200
        t = [duration_sec * i / (sample_count - 1) for i in range(sample_count)]
        global_mask = self._build_global_mask(t, g_freq)

        mode_val = self._pid_value_from_step(params, viewer_cfg.get("pid_mode"))
        mode_text = None
        if mode_val is None:
            # pid_mode는 문자열 파라미터라 numeric 안 될 수 있으니 mapping raw로 직접 꺼냄
            pid_mode = viewer_cfg.get("pid_mode")
            mapping = self.pid_to_mapping.get(pid_mode)
            if mapping:
                raw = params.get(mapping)
                if raw is not None:
                    mode_text = str(raw).strip()
        else:
            mode_text = str(mode_val).strip()

        case_type = viewer_cfg.get("case_type", "")

        # local frequency
        if viewer_cfg.get("freq_input_mode") == "manual":
            local_freq_khz = float(viewer_cfg.get("manual_freq_khz", 0.0))
            local_freq_hz = local_freq_khz * 1000.0
        else:
            local_freq = self._pid_value_from_step(params, viewer_cfg.get("pid_freq"))
            local_freq_hz = (local_freq or 0.0)

        y = [0.0 for _ in t]

        # ---------------- case1 ----------------
        if case_type == "case1":
            amp = self._pid_value_from_step(params, viewer_cfg.get("pid_c1_amp")) or 0.0
            duty = self._pid_value_from_step(params, viewer_cfg.get("pid_c1_duty")) or 50.0
            offset = self._pid_value_from_step(params, viewer_cfg.get("pid_c1_offset")) or 0.0

            if mode_text == "Pulse":
                local = self._square_wave(t, local_freq_hz, duty, amp_h=amp, amp_l=0.0, offset_pct=offset)
                y = self._apply_global_on(local, global_mask)

            elif mode_text == "Cont":
                # 사용자가 말한대로 0-Amp 사각파를 주파수에 맞춰 연속 반복
                local = self._square_wave(t, local_freq_hz, 50.0, amp_h=amp, amp_l=0.0, offset_pct=offset)
                y = self._apply_global_on(local, global_mask)

        # ---------------- case2 ----------------
        elif case_type == "case2":
            m1 = self._pid_value_from_step(params, viewer_cfg.get("pid_c2_m1_amp")) or 0.0

            if mode_text == "CW":
                local = self._sine_wave(t, local_freq_hz, amp=m1)
                y = self._apply_global_on(local, global_mask)

            elif mode_text == "Pulse":
                # 1차 버전: multistate 중 M1만 우선 반영
                local = self._sine_wave(t, local_freq_hz, amp=m1)
                y = self._apply_global_on(local, global_mask)

        # ---------------- case3 ----------------
        elif case_type == "case3":
            pulse_duty = self._pid_value_from_step(params, viewer_cfg.get("pid_c3_pulse_duty")) or 50.0
            amp_h = self._pid_value_from_step(params, viewer_cfg.get("pid_c3_amp_h")) or 0.0
            amp_l = self._pid_value_from_step(params, viewer_cfg.get("pid_c3_amp_l")) or 0.0
            offset = self._pid_value_from_step(params, viewer_cfg.get("pid_c3_offset")) or 0.0

            if mode_text == "Pulse":
                local = self._square_wave(t, g_freq, pulse_duty, amp_h=amp_h, amp_l=amp_l, offset_pct=offset)
                y = local

            elif mode_text == "Cont":
                local = [amp_h for _ in t]
                y = self._apply_global_on(local, global_mask)

        return t, y, duration_sec

    # ------------------------------------
    # Render
    # ------------------------------------
    def _render_all(self):
        while self.scroll_v.count():
            item = self.scroll_v.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

        checked_recipes = self._checked_recipes()
        if not checked_recipes:
            self.scroll_v.addWidget(QLabel("No recipe selected."))
            return

        viewers = self.pulse_setting.get("viewers", [])
        if not viewers:
            self.scroll_v.addWidget(QLabel("No viewer setting found."))
            return

        for v_idx, viewer_cfg in enumerate(viewers, start=1):
            box = QGroupBox(f"Viewer {v_idx}")
            box_l = QVBoxLayout(box)

            pw = pg.PlotWidget()
            pw.showGrid(x=True, y=True)
            pw.addLegend()
            pw.setMinimumHeight(260)
            pw.setBackground("w")
            pw.getAxis("left").setTextPen("k")
            pw.getAxis("bottom").setTextPen("k")

            for recipe in checked_recipes:
                all_x = []
                all_y = []
                x_offset = 0.0

                for step in recipe["steps"]:
                    t, y, duration_sec = self._build_viewer_wave(viewer_cfg, step)
                    t_shifted = [x + x_offset for x in t]
                    all_x.extend(t_shifted)
                    all_y.extend(y)
                    x_offset += duration_sec

                pw.plot(all_x, all_y, pen=pg.mkPen(width=1.5), name=recipe["recipe_code"])

            pw.setLabel("bottom", "Time (s)")
            box_l.addWidget(pw)
            self.scroll_v.addWidget(box)

        self.scroll_v.addStretch(1)