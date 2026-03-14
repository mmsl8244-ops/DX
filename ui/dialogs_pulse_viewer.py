from __future__ import annotations
import math
import numpy as np
import pyqtgraph as pg

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QSplitter, QScrollArea, QGroupBox, QCheckBox
)
from PyQt5.QtCore import Qt, QTimer


_RECIPE_COLORS = [
    (31, 119, 180),   # blue
    (214, 39, 40),    # red
    (44, 160, 44),    # green
    (255, 127, 14),   # orange
    (148, 103, 189),  # purple
    (140, 86, 75),    # brown
    (227, 119, 194),  # pink
    (23, 190, 207),   # cyan
    (188, 189, 34),   # yellow-green
    (127, 127, 127),  # gray
]


class PulseViewerDialog(QDialog):
    def __init__(self, parent=None, pulse_setting: dict | None = None,
                 recipe_data: list[dict] | None = None,
                 pid_meta: dict[int, dict] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Pulse Viewer")
        self.resize(1750, 1200)

        self.pulse_setting = pulse_setting or {}
        self.recipe_data = recipe_data or []
        self.pid_meta = pid_meta or {}

        self._plot_widgets: list[pg.PlotWidget] = []
        self._syncing = False

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(500)
        self._render_timer.timeout.connect(self._render_all)

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
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, r)
            self.recipe_list.addItem(item)

        self.recipe_list.itemChanged.connect(lambda _: self._render_timer.start())
        left_l.addWidget(self.recipe_list)

        # -------------------------
        # Right: plots
        # -------------------------
        right = QWidget(self)
        right_l = QVBoxLayout(right)

        sync_row = QHBoxLayout()
        self.chk_sync = QCheckBox("Sync X-axis (all viewers)")
        sync_row.addWidget(self.chk_sync)
        sync_row.addStretch(1)
        right_l.addLayout(sync_row)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)

        self.scroll_body = QWidget(self)
        self.scroll_v = QVBoxLayout(self.scroll_body)
        self.scroll.setWidget(self.scroll_body)

        right_l.addWidget(self.scroll)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([260, 1450])

        self._render_all()

    # =========================================================
    # Helpers
    # =========================================================
    def _checked_recipes(self) -> list[dict]:
        out = []
        for i in range(self.recipe_list.count()):
            item = self.recipe_list.item(i)
            if item.checkState() == Qt.Checked:
                out.append(item.data(Qt.UserRole))
        return out

    def _payload_pid(self, payload: dict | None) -> int | None:
        if not payload:
            return None
        pid = payload.get("pid")
        try:
            return int(pid) if pid is not None else None
        except Exception:
            return None

    def _viewer_case_type(self, viewer_data: dict) -> str:
        manual_enabled = bool(viewer_data.get("manual_enabled", False))
        manual_freq = float(viewer_data.get("manual_freq_khz", 0.0) or 0.0)
        freq_drop_pid = self._payload_pid(viewer_data.get("freq_drop"))

        if manual_enabled:
            return "case2" if manual_freq != 0.0 else "case3"

        if freq_drop_pid is not None:
            return "case1"

        return ""

    def _pid_mapping(self, pid: int | None) -> str:
        if pid is None:
            return ""
        return str(self.pid_meta.get(pid, {}).get("mapping", "") or "")

    def _pid_unit(self, pid: int | None) -> str:
        if pid is None:
            return ""
        unit = self.pid_meta.get(pid, {}).get("unit", "")
        return str(unit or "").strip()

    def _raw_from_pid(self, step_params: dict, pid: int | None):
        if pid is None:
            return None
        mapping = self._pid_mapping(pid)
        if not mapping:
            return None
        return step_params.get(mapping)

    def _num_from_pid(self, step_params: dict, pid: int | None) -> float | None:
        raw = self._raw_from_pid(step_params, pid)
        if raw in (None, "", "NULL", "null"):
            return None

        try:
            value = float(raw)
        except Exception:
            return None

        unit = self._pid_unit(pid)
        unit_norm = str(unit or "").replace(" ", "").lower()

        # 음수 방향 unit 판정 강화
        # 예:
        # [-V], [-A], [ -V ], -V, -A, [-kV], [-mA]
        is_negative_unit = (
            unit_norm.startswith("[-")
            or unit_norm.startswith("-")
            or "[-v" in unit_norm
            or "[-a" in unit_norm
            or "[-kv" in unit_norm
            or "[-mv" in unit_norm
            or "[-ma" in unit_norm
            or "[-ua" in unit_norm
        )

        if is_negative_unit:
            value = -abs(value)

        return value
    
    def _normalize_unit(self, unit: str) -> str:
        return str(unit or "").replace(" ", "").lower().replace("[", "").replace("]", "")


    def _is_negative_direction_unit(self, pid: int | None) -> bool:
        unit = self._normalize_unit(self._pid_unit(pid))
        return unit.startswith("-") or unit.startswith("(-") or unit.startswith("[-")


    def _merge_boundaries(self, *arrays) -> np.ndarray:
        vals = []
        for arr in arrays:
            if arr is None:
                continue
            if isinstance(arr, (list, tuple, set)):
                vals.extend(arr)
            else:
                vals.extend(np.asarray(arr, dtype=float).tolist())

        if not vals:
            return np.array([0.0, 1.0], dtype=float)

        out = np.array(sorted(set(float(v) for v in vals)), dtype=float)
        if len(out) < 2:
            out = np.array([out[0], out[0] + 1e-12], dtype=float)
        return out


    def _period_edges_in_range(self, x_min: float, x_max: float, period: float, on_time: float, shift_sec: float = 0.0) -> np.ndarray:
        """[x_min, x_max] 범위 내 edges만 생성 - O(visible_cycles), 전체 duration 불필요"""
        edges = {float(x_min), float(x_max)}
        if period <= 0:
            return np.array(sorted(edges), dtype=float)
        k_min = int(np.floor((x_min - shift_sec) / period)) - 1
        k_max = int(np.ceil((x_max - shift_sec) / period)) + 1
        for k in range(k_min, k_max + 1):
            s = shift_sec + k * period
            e = s + on_time
            if x_min < s < x_max:
                edges.add(float(s))
            if x_min < e < x_max:
                edges.add(float(e))
        return np.array(sorted(edges), dtype=float)

    def _period_edges_in_window(self, duration_sec: float, period: float, on_time: float, shift_sec: float = 0.0) -> np.ndarray:
        """
        duration_sec 구간 안에서 반복 사각파의 모든 경계(start/end) 반환
        """
        if duration_sec <= 0 or period <= 0:
            return np.array([0.0, max(duration_sec, 1e-12)], dtype=float)

        edges = {0.0, float(duration_sec)}

        k_min = int(np.floor((-shift_sec) / period)) - 2
        k_max = int(np.ceil((duration_sec - shift_sec) / period)) + 2

        for k in range(k_min, k_max + 1):
            start = shift_sec + k * period
            end = start + on_time

            if 0.0 < start < duration_sec:
                edges.add(float(start))
            if 0.0 < end < duration_sec:
                edges.add(float(end))

        return np.array(sorted(edges), dtype=float)


    def _segment_midpoints(self, x_edges: np.ndarray) -> np.ndarray:
        if x_edges is None or len(x_edges) < 2:
            return np.array([], dtype=float)
        return (x_edges[:-1] + x_edges[1:]) / 2.0


    def _evaluate_square_mask(self, mids: np.ndarray, period: float, on_time: float, shift_sec: float = 0.0) -> np.ndarray:
        if len(mids) == 0 or period <= 0:
            return np.zeros_like(mids, dtype=bool)

        phase = np.mod(mids - shift_sec, period)
        return phase < on_time


    def _safe_num_or_zero(self, step_params: dict, pid: int | None) -> float:
        v = self._num_from_pid(step_params, pid)
        return 0.0 if v is None else float(v)

    def _str_from_pid(self, step_params: dict, pid: int | None) -> str | None:
        raw = self._raw_from_pid(step_params, pid)
        if raw in (None, "", "NULL", "null"):
            return None
        return str(raw).strip()

    def _convert_freq_to_hz(self, value: float | None, pid: int | None) -> float:
        if value is None:
            return 0.0

        unit = self._pid_unit(pid)
        unit_norm = unit.replace(" ", "").lower()
        unit_norm = unit_norm.replace("[", "").replace("]", "")

        if "ghz" in unit_norm:
            return value * 1_000_000_000.0
        if "mhz" in unit_norm:
            return value * 1_000_000.0
        if "khz" in unit_norm:
            return value * 1_000.0
        if unit_norm == "hz":
            return value

        # 단위 없으면 Hz로 간주
        return value

    def _convert_time_to_sec(self, value: float | None, pid: int | None) -> float:
        if value is None:
            return 0.0

        unit = self._pid_unit(pid).lower().strip()
        if unit in ("s", "sec", "second", "seconds"):
            return value
        if unit == "ms":
            return value / 1_000.0
        if unit in ("us", "μs"):
            return value / 1_000_000.0
        if unit == "ns":
            return value / 1_000_000_000.0

        # 기본은 ms 가정
        return value / 1_000.0

    def _safe_pct(self, value: float | None, default: float = 0.0) -> float:
        if value is None:
            return default
        return max(0.0, min(100.0, float(value)))

    def _adaptive_sample_count(self, duration_sec: float, ref_freq_hz: float, *, sine: bool = False) -> int:
        if duration_sec <= 0:
            return 2000

        if sine:
            # sine는 더 촘촘히
            # period당 최소 64포인트 정도 확보
            est = int(duration_sec * max(ref_freq_hz, 1.0) * 64)
            return max(8000, min(est, 400000))

        est = int(duration_sec * max(ref_freq_hz, 1.0) * 12)
        return max(2500, min(est, 100000))

    def _phase(self, t: np.ndarray, period: float, shift_sec: float = 0.0) -> np.ndarray:
        if period <= 0:
            return np.zeros_like(t)
        return np.mod(t - shift_sec, period)

    def _global_period_and_axis(self, step_params: dict):
        global_data = self.pulse_setting.get("global", {}) or {}
        pid_duration = self._payload_pid(global_data.get("duration"))
        pid_gfreq = self._payload_pid(global_data.get("pulse_freq"))

        duration_raw = self._num_from_pid(step_params, pid_duration)
        gfreq_raw = self._num_from_pid(step_params, pid_gfreq)

        duration_sec = self._convert_time_to_sec(duration_raw, pid_duration)
        global_freq_hz = self._convert_freq_to_hz(gfreq_raw, pid_gfreq)

        if duration_sec <= 0:
            duration_sec = 0.001
        if global_freq_hz <= 0:
            global_freq_hz = 1000.0

        tg = 1.0 / global_freq_hz
        return duration_sec, global_freq_hz, tg
    
    def _to_stair_xy(self, x: np.ndarray, y: np.ndarray):
        """
        구간 경계 x(N+1), 구간값 y(N) -> 일반 plot 가능한 stair x/y(같은 길이)로 변환
        """
        if x is None or y is None or len(x) < 2 or len(y) < 1:
            return np.array([]), np.array([])

        # x는 경계점, y는 각 구간값
        # 결과 길이: 2*N
        xs = np.empty(len(y) * 2, dtype=float)
        ys = np.empty(len(y) * 2, dtype=float)

        xs[0::2] = x[:-1]
        xs[1::2] = x[1:]

        ys[0::2] = y
        ys[1::2] = y

        return xs, ys

    # =========================================================
    # Case 1
    # =========================================================
    def _case1_wave(self, step_params: dict, viewer_data: dict, duration_sec: float, tg: float):
        """
        case1:
        - global: 사각 gate
        - local : 사각 pulse
        - interval optional
        - 결과는 x(N+1), y(N) 형태의 step segment
        """
        mode_pid = self._payload_pid(viewer_data.get("mode"))
        mode_text = (self._str_from_pid(step_params, mode_pid) or "").strip().upper()

        freq_pid = self._payload_pid(viewer_data.get("freq_drop"))
        local_freq_raw = self._num_from_pid(step_params, freq_pid)
        local_freq_hz = self._convert_freq_to_hz(local_freq_raw, freq_pid)

        if local_freq_hz <= 0 or duration_sec <= 0 or tg <= 0:
            return np.array([0.0, max(duration_sec, 1e-12)], dtype=float), np.array([0.0], dtype=float)

        c1 = viewer_data.get("case1", {}) or {}

        pulse_duty_pid = self._payload_pid(c1.get("pulse_duty"))
        duty_pid = self._payload_pid(c1.get("duty"))
        offset_pid = self._payload_pid(c1.get("offset"))
        amp_pid = self._payload_pid(c1.get("amp"))
        hsp_offset_pid = self._payload_pid(c1.get("hsp_offset"))

        interval_amp_pid = self._payload_pid(c1.get("interval_amp_m"))
        interval_freq_pid = self._payload_pid(c1.get("interval_freq"))
        interval_duty_pid = self._payload_pid(c1.get("interval_duty"))

        amp = self._safe_num_or_zero(step_params, amp_pid)
        local_duty_pct = self._safe_pct(self._num_from_pid(step_params, duty_pid), 50.0)
        pulse_duty_pct = self._safe_pct(self._num_from_pid(step_params, pulse_duty_pid), 100.0)
        offset_pct = self._safe_pct(self._num_from_pid(step_params, offset_pid), 0.0)
        hsp_offset_pct = self._safe_pct(self._num_from_pid(step_params, hsp_offset_pid), 0.0)

        tl = 1.0 / local_freq_hz

        global_on = tg * (pulse_duty_pct / 100.0)
        local_on = tl * (local_duty_pct / 100.0)

        global_shift = tg * (offset_pct / 100.0)
        local_shift = tl * (hsp_offset_pct / 100.0)

        # interval optional
        interval_amp = self._num_from_pid(step_params, interval_amp_pid)
        interval_freq_raw = self._num_from_pid(step_params, interval_freq_pid)
        interval_duty_raw = self._num_from_pid(step_params, interval_duty_pid)

        use_interval = (
            interval_amp is not None and
            interval_freq_raw is not None and
            interval_duty_raw is not None
        )

        interval_edges = None
        interval_period = None
        interval_on = None

        if use_interval:
            interval_freq_hz = self._convert_freq_to_hz(interval_freq_raw, interval_freq_pid)
            interval_duty_pct = self._safe_pct(interval_duty_raw, 50.0)

            if interval_freq_hz > 0:
                interval_period = 1.0 / interval_freq_hz
                interval_on = interval_period * (interval_duty_pct / 100.0)
                interval_edges = self._period_edges_in_window(duration_sec, interval_period, interval_on, 0.0)
            else:
                use_interval = False

        global_edges = self._period_edges_in_window(duration_sec, tg, global_on, global_shift)
        local_edges = self._period_edges_in_window(duration_sec, tl, local_on, local_shift)

        x = self._merge_boundaries([0.0, duration_sec], global_edges, local_edges, interval_edges)
        mids = self._segment_midpoints(x)

        if len(mids) == 0:
            return np.array([0.0, max(duration_sec, 1e-12)], dtype=float), np.array([0.0], dtype=float)

        # local mask
        local_mask = self._evaluate_square_mask(mids, tl, local_on, local_shift)

        # global mask
        if mode_text == "PULSE":
            global_mask = self._evaluate_square_mask(mids, tg, global_on, global_shift)
        elif mode_text == "CONT":
            global_mask = np.ones_like(mids, dtype=bool)
        else:
            return np.array([0.0, duration_sec], dtype=float), np.array([0.0], dtype=float)

        amp_env = np.full_like(mids, amp, dtype=float)

        if use_interval and interval_period is not None and interval_on is not None:
            interval_mask = self._evaluate_square_mask(mids, interval_period, interval_on, 0.0)
            amp_env = np.where(interval_mask, amp, float(interval_amp))

        y = np.where(global_mask & local_mask, amp_env, 0.0).astype(float)
        return x, y
    # =========================================================
    # Case 2
    # =========================================================
    def _case2_wave(self, t: np.ndarray, step_params: dict, viewer_data: dict, tg: float) -> np.ndarray:
        mode_pid = self._payload_pid(viewer_data.get("mode"))
        mode_text = (self._str_from_pid(step_params, mode_pid) or "").strip().upper()

        local_freq_hz = float(viewer_data.get("manual_freq_khz", 0.0) or 0.0) * 1000.0
        if local_freq_hz <= 0 or tg <= 0 or len(t) == 0:
            return np.zeros_like(t, dtype=float)

        c2 = viewer_data.get("case2", {}) or {}

        amp_pids = [
            self._payload_pid(c2.get("m1_amp")),
            self._payload_pid(c2.get("m2_amp")),
            self._payload_pid(c2.get("m3_amp")),
            self._payload_pid(c2.get("m4_amp")),
        ]
        duty_pids = [
            self._payload_pid(c2.get("m1_duty")),
            self._payload_pid(c2.get("m2_duty")),
            self._payload_pid(c2.get("m3_duty")),
        ]
        hsp_duty_pid = self._payload_pid(c2.get("hsp_duty"))

        amp_vals_raw = [self._num_from_pid(step_params, p) for p in amp_pids]

        carrier = np.sin(2.0 * np.pi * local_freq_hz * t)

        # CW: M1 amplitude 고정 sine
        if mode_text == "CW":
            m1_amp = amp_vals_raw[0] if amp_vals_raw[0] is not None else 0.0
            return (m1_amp * carrier).astype(float)

        if mode_text != "PULSE":
            return np.zeros_like(t, dtype=float)

        # 연속으로 입력된 amplitude state만 사용
        valid_amp_count = 0
        for v in amp_vals_raw:
            if v is None:
                break
            valid_amp_count += 1

        if valid_amp_count == 0:
            return np.zeros_like(t, dtype=float)

        state_amps = [float(v if v is not None else 0.0) for v in amp_vals_raw[:valid_amp_count]]

        # HSP Duty 없으면 global period 전체 사용
        hsp_duty_raw = self._num_from_pid(step_params, hsp_duty_pid)
        hsp_duty_pct = self._safe_pct(hsp_duty_raw, 100.0)

        active_len = tg * (hsp_duty_pct / 100.0)
        if active_len <= 0:
            return np.zeros_like(t, dtype=float)

        # state duty 계산
        state_duties = []
        used = 0.0
        for i in range(valid_amp_count):
            if i < valid_amp_count - 1:
                pid = duty_pids[i] if i < len(duty_pids) else None
                dv = self._num_from_pid(step_params, pid)
                dv = self._safe_pct(dv, 0.0)
                state_duties.append(dv)
                used += dv
            else:
                state_duties.append(max(0.0, 100.0 - used))

        phase_g = np.mod(t, tg)
        active_mask = phase_g < active_len

        # active window 안에서 0~100%
        phase_pct = np.zeros_like(t, dtype=float)
        phase_pct[active_mask] = (phase_g[active_mask] / active_len) * 100.0

        amp_env = np.zeros_like(t, dtype=float)

        start_pct = 0.0
        for amp, duty_pct in zip(state_amps, state_duties):
            end_pct = start_pct + duty_pct
            mask = active_mask & (phase_pct >= start_pct) & (phase_pct < end_pct)
            amp_env[mask] = amp
            start_pct = end_pct

        # rounding으로 마지막 state 누락되는 경우 보정
        assigned = np.zeros_like(t, dtype=bool)
        start_pct = 0.0
        for duty_pct in state_duties:
            end_pct = start_pct + duty_pct
            assigned |= active_mask & (phase_pct >= start_pct) & (phase_pct < end_pct)
            start_pct = end_pct
        leftover = active_mask & (~assigned)
        if np.any(leftover):
            amp_env[leftover] = state_amps[-1]

        y = amp_env * carrier
        return y.astype(float)

    # =========================================================
    # Case 3  (가장 중요: 네 설명 기준으로 우선 반영)
    # =========================================================
    def _case3_wave(self, t: np.ndarray, step_params: dict, viewer_data: dict, tg: float) -> np.ndarray:
        mode_pid = self._payload_pid(viewer_data.get("mode"))
        mode_text = (self._str_from_pid(step_params, mode_pid) or "").strip().upper()

        c3 = viewer_data.get("case3", {}) or {}

        pulse_duty_pid = self._payload_pid(c3.get("pulse_duty"))
        amp_h_pid = self._payload_pid(c3.get("amp_h"))
        offset_pid = self._payload_pid(c3.get("offset"))
        amp_l_pid = self._payload_pid(c3.get("amp_l"))

        pulse_duty_pct = self._safe_pct(self._num_from_pid(step_params, pulse_duty_pid), 50.0)
        amp_h = self._num_from_pid(step_params, amp_h_pid)
        amp_l = self._num_from_pid(step_params, amp_l_pid)
        offset_pct = self._safe_pct(self._num_from_pid(step_params, offset_pid), 0.0)

        if amp_h is None:
            amp_h = 0.0
        if amp_l is None:
            amp_l = 0.0

        # global period 기준 shift
        shift_sec = (offset_pct / 100.0) * tg
        phase_g = self._phase(t, tg, shift_sec=shift_sec)

        # global period 내 duty 구간
        duty_on_time = (pulse_duty_pct / 100.0) * tg
        gate = phase_g < duty_on_time

        if mode_text == "PULSE":
            # duty 구간은 Amp., 나머지는 Amp.(L)
            return np.where(gate, amp_h, amp_l).astype(float)

        if mode_text == "CONT":
            # Cont는 평평한 DC
            return np.full_like(t, amp_h, dtype=float)

        return np.zeros_like(t, dtype=float)
    

    def _build_case3_step_segments(self, step_params: dict, viewer_data: dict, duration_sec: float, tg: float):
        mode_pid = self._payload_pid(viewer_data.get("mode"))
        mode_text = (self._str_from_pid(step_params, mode_pid) or "").strip().upper()

        c3 = viewer_data.get("case3", {}) or {}

        pulse_duty_pid = self._payload_pid(c3.get("pulse_duty"))
        amp_h_pid = self._payload_pid(c3.get("amp_h"))
        offset_pid = self._payload_pid(c3.get("offset"))
        amp_l_pid = self._payload_pid(c3.get("amp_l"))

        pulse_duty_pct = self._safe_pct(self._num_from_pid(step_params, pulse_duty_pid), 50.0)
        amp_h = self._safe_num_or_zero(step_params, amp_h_pid)
        amp_l = self._safe_num_or_zero(step_params, amp_l_pid)
        offset_pct = self._safe_pct(self._num_from_pid(step_params, offset_pid), 0.0)

        if duration_sec <= 0 or tg <= 0:
            return np.array([0.0, 1e-12], dtype=float), np.array([0.0], dtype=float)

        if mode_text == "CONT":
            x = np.array([0.0, duration_sec], dtype=float)
            y = np.array([amp_h], dtype=float)
            return x, y

        if mode_text != "PULSE":
            x = np.array([0.0, duration_sec], dtype=float)
            y = np.array([0.0], dtype=float)
            return x, y

        on_time = tg * (pulse_duty_pct / 100.0)
        shift = tg * (offset_pct / 100.0)

        x = self._period_edges_in_window(duration_sec, tg, on_time, shift)
        mids = self._segment_midpoints(x)
        gate = self._evaluate_square_mask(mids, tg, on_time, shift)

        y = np.where(gate, amp_h, amp_l).astype(float)
        return x, y
    # =========================================================
    # Step build
    # =========================================================
    def _build_viewer_wave(self, viewer_data: dict, step: dict):
        params = step["params"]
        duration_sec, global_freq_hz, tg = self._global_period_and_axis(params)

        case_type = self._viewer_case_type(viewer_data)
        if not case_type:
            return np.array([]), np.array([]), 0.0, False, None

        if case_type == "case1":
            x, y = self._case1_wave(params, viewer_data, duration_sec, tg)
            return x, y, duration_sec, True, None

        elif case_type == "case2":
            # case2는 이제 여기서 전체 샘플링하지 않음
            runtime = self._calc_case2_step_runtime(params, viewer_data, duration_sec, tg)
            return np.array([]), np.array([]), duration_sec, False, runtime

        elif case_type == "case3":
            x, y = self._build_case3_step_segments(params, viewer_data, duration_sec, tg)
            return x, y, duration_sec, True, None

        return np.array([]), np.array([]), 0.0, False, None
    




    def _calc_case2_step_runtime(self, step_params: dict, viewer_data: dict, duration_sec: float, tg: float):
        """
        case2:
        - M-state(M1~M4) 패턴은 global period(tg) 기준으로 반복
        - HSP Duty: local period(tl) 기준 active window (Null=100%)
          각 local period 내에서 active_len 이후 구간은 신호=0
        - 마지막 state duty는 remainder 자동 계산
        """
        mode_pid = self._payload_pid(viewer_data.get("mode"))
        mode_text = (self._str_from_pid(step_params, mode_pid) or "").strip().upper()
        if not mode_text:
            mode_text = "PULSE"

        local_freq_hz = float(viewer_data.get("manual_freq_khz", 0.0) or 0.0) * 1000.0
        if local_freq_hz <= 0 or tg <= 0 or duration_sec <= 0:
            return None

        tl = 1.0 / local_freq_hz

        c2 = viewer_data.get("case2", {}) or {}

        amp_pids = [
            self._payload_pid(c2.get("m1_amp")),
            self._payload_pid(c2.get("m2_amp")),
            self._payload_pid(c2.get("m3_amp")),
            self._payload_pid(c2.get("m4_amp")),
        ]
        duty_pids = [
            self._payload_pid(c2.get("m1_duty")),
            self._payload_pid(c2.get("m2_duty")),
            self._payload_pid(c2.get("m3_duty")),
        ]
        hsp_duty_pid = self._payload_pid(c2.get("hsp_duty"))

        amp_vals = [self._num_from_pid(step_params, p) for p in amp_pids]

        # HSP Duty: case1 local period(tl_hsp) 기준 active window
        # case1의 freq_drop PID에서 로컬 주파수를 읽어 tl_hsp 계산
        # Null → 100% (tl_hsp 전체 사용)
        freq_drop_pid = self._payload_pid(viewer_data.get("freq_drop"))
        case1_freq_raw = self._num_from_pid(step_params, freq_drop_pid)
        case1_freq_hz = self._convert_freq_to_hz(case1_freq_raw, freq_drop_pid)
        tl_hsp = (1.0 / case1_freq_hz) if case1_freq_hz > 0 else 0.0

        hsp_duty_raw = self._num_from_pid(step_params, hsp_duty_pid)
        hsp_duty_pct = self._safe_pct(hsp_duty_raw, 100.0)
        active_len = tl_hsp * (hsp_duty_pct / 100.0)

        # CW
        if mode_text == "CW":
            m1_amp = amp_vals[0] if amp_vals[0] is not None else 0.0
            return {
                "mode": "CW",
                "duration_sec": float(duration_sec),
                "tg": float(tg),
                "tl": float(tl),
                "tl_hsp": float(tl_hsp),
                "active_len": float(tl_hsp),  # CW는 항상 full local period
                "local_freq_hz": float(local_freq_hz),
                "segments": [(0.0, float(tg), float(m1_amp))]
            }

        if mode_text != "PULSE":
            return None

        # 연속으로 입력된 amplitude state만 사용
        valid_amp_count = 0
        for v in amp_vals:
            if v is None:
                break
            valid_amp_count += 1

        if valid_amp_count == 0:
            return None

        state_amps = [float(v) for v in amp_vals[:valid_amp_count]]

        # duty 계산: M-state는 global period(tg) 기준, 마지막 state는 remainder 자동
        state_duties = []
        used = 0.0
        for i in range(valid_amp_count):
            if i < valid_amp_count - 1:
                pid = duty_pids[i] if i < len(duty_pids) else None
                dv = self._num_from_pid(step_params, pid)
                dv = self._safe_pct(dv, 0.0)
                state_duties.append(float(dv))
                used += float(dv)
            else:
                state_duties.append(max(0.0, 100.0 - used))

        duty_sum = sum(state_duties)
        if duty_sum <= 0:
            return None

        if duty_sum > 100.0:
            scale = 100.0 / duty_sum
            state_duties = [d * scale for d in state_duties]

        # segments는 global period(tg) 기준 [start_t, end_t]
        segments = []
        start_t = 0.0

        for amp, duty_pct in zip(state_amps, state_duties):
            seg_len = float(tg) * (float(duty_pct) / 100.0)
            end_t = start_t + seg_len

            if seg_len > 0:
                segments.append((float(start_t), float(end_t), float(amp)))

            start_t = end_t

        # 마지막 segment 끝을 tg로 강제 보정
        if segments:
            s0, _e0, a0 = segments[-1]
            segments[-1] = (float(s0), float(tg), float(a0))

        return {
            "mode": "PULSE",
            "duration_sec": float(duration_sec),
            "tg": float(tg),
            "tl": float(tl),
            "tl_hsp": float(tl_hsp),
            "active_len": float(active_len),
            "local_freq_hz": float(local_freq_hz),
            "segments": segments
        }



    
    def _case2_amp_env_at_times(self, runtime: dict, t: np.ndarray) -> np.ndarray:
        """
        runtime["segments"]에 정의된 1 global period 내부 state를
        모든 global period마다 반복 적용한 amplitude envelope 반환
        """
        if runtime is None or len(t) == 0:
            return np.array([], dtype=float)

        tg = float(runtime.get("tg", 0.0) or 0.0)
        tl = float(runtime.get("tl", 0.0) or 0.0)
        _tl_hsp_val = runtime.get("tl_hsp")
        tl_hsp = float(_tl_hsp_val) if _tl_hsp_val is not None else tl
        active_len = float(runtime.get("active_len") or 0.0)
        mode = str(runtime.get("mode", "") or "").upper()
        segments = runtime.get("segments", []) or []

        if tg <= 0:
            return np.zeros_like(t, dtype=float)

        phase_g = np.mod(t, tg)
        amp_env = np.zeros_like(t, dtype=float)

        if mode == "CW":
            amp = float(segments[0][2]) if segments else 0.0
            amp_env[:] = amp
            return amp_env

        if mode != "PULSE" or not segments:
            return amp_env

        tol = max(tg * 1e-12, 1e-15)

        for idx, (seg_start, seg_end, amp) in enumerate(segments):
            seg_start = float(seg_start)
            seg_end = float(seg_end)
            amp = float(amp)

            if seg_end <= seg_start:
                continue

            # 마지막 segment는 오른쪽 끝 포함
            if idx == len(segments) - 1:
                mask = (phase_g >= seg_start - tol) & (phase_g <= seg_end + tol)
            else:
                mask = (phase_g >= seg_start - tol) & (phase_g < seg_end - tol)

            amp_env[mask] = amp

        # HSP Duty: M1 state(segments[0])에만 case1 local period(tl_hsp) 기준 마스킹 적용
        # active_len == 0 (hsp_duty=0) 또는 active_len == tl_hsp (null/100%) → 마스킹 없음
        if tl_hsp > 0 and 0 < active_len < tl_hsp * (1.0 - 1e-9) and segments:
            m1_start = float(segments[0][0])
            m1_end = float(segments[0][1])
            m1_region = (phase_g >= m1_start - tol) & (phase_g <= m1_end + tol)
            phase_l = np.mod(t, tl_hsp)
            hsp_active = phase_l < active_len
            # M1 영역에서만 HSP 비활성 구간을 0으로
            amp_env = np.where(m1_region & ~hsp_active, 0.0, amp_env)

        return amp_env





    def _sample_case2_visible(self, runtime: dict, x_min: float, x_max: float,
                            view_pixel_width: int | None = None,
                            max_points: int = 8000):
        """
        case2 visible-range 샘플링
        - 확대 시: 실제 sine 파형
        - 축소 시: 같은 curve로 min/max band 형태 표시
        """
        if runtime is None:
            return np.array([]), np.array([])

        duration_sec = float(runtime.get("duration_sec", 0.0) or 0.0)
        tg = float(runtime.get("tg", 0.0) or 0.0)
        local_freq_hz = float(runtime.get("local_freq_hz", 0.0) or 0.0)

        if duration_sec <= 0 or tg <= 0 or local_freq_hz <= 0:
            return np.array([]), np.array([])

        x_min = max(0.0, float(x_min))
        x_max = min(duration_sec, float(x_max))
        if x_max <= x_min:
            return np.array([]), np.array([])

        visible_len = x_max - x_min
        if view_pixel_width is None or view_pixel_width <= 0:
            view_pixel_width = 1200

        cycles_visible = visible_len * local_freq_hz
        cycles_per_pixel = cycles_visible / max(float(view_pixel_width), 1.0)

        # -------------------------------------------------
        # 1) 충분히 확대된 상태 -> 실제 sine 샘플링
        # -------------------------------------------------
        if cycles_per_pixel <= 0.35:
            n = int(min(max_points, max(1200, cycles_visible * 48)))
            t = np.linspace(x_min, x_max, n, endpoint=False, dtype=float)

            amp_env = self._case2_amp_env_at_times(runtime, t)
            y = amp_env * np.sin(2.0 * np.pi * local_freq_hz * t)
            return t, y

        # -------------------------------------------------
        # 2) 멀리서 보는 상태 -> fake sign 파형 대신
        #    각 화면 bin마다 min/max band 생성
        # -------------------------------------------------
        bin_count = int(min(max_points // 3, max(300, view_pixel_width)))
        edges = np.linspace(x_min, x_max, bin_count + 1, endpoint=True, dtype=float)
        centers = (edges[:-1] + edges[1:]) / 2.0

        xs = np.empty(bin_count * 3, dtype=float)
        ys = np.empty(bin_count * 3, dtype=float)

        for i in range(bin_count):
            amax = self._case2_max_abs_amp_in_window(runtime, edges[i], edges[i + 1])

            xs[i * 3 + 0] = centers[i]
            xs[i * 3 + 1] = centers[i]
            xs[i * 3 + 2] = np.nan

            ys[i * 3 + 0] = -amax
            ys[i * 3 + 1] = +amax
            ys[i * 3 + 2] = np.nan

        return xs, ys
    
    def _case2_max_abs_amp_in_window(self, runtime: dict, t0: float, t1: float) -> float:
        """
        [t0, t1] 구간에서 case2 amplitude envelope의 최대 절대값을 계산.
        runtime["segments"]는 1 global period 내부 정의이고,
        실제 시간축에서는 tg 주기로 반복된다고 가정.
        """
        if runtime is None:
            return 0.0

        tg = float(runtime.get("tg", 0.0) or 0.0)
        segments = runtime.get("segments", []) or []

        if tg <= 0 or t1 <= t0 or not segments:
            return 0.0

        seg_abs_max = max(abs(float(seg[2])) for seg in segments) if segments else 0.0
        if seg_abs_max <= 0:
            return 0.0

        width = t1 - t0
        tol = max(tg * 1e-12, 1e-15)

        # 구간이 global period 하나 이상이면 무조건 전체 state를 다 포함할 수 있으므로 최대값 반환
        if width >= tg - tol:
            return seg_abs_max

        def overlap(a0, a1, b0, b1):
            return min(a1, b1) > max(a0, b0) + tol

        p0 = float(np.mod(t0, tg))
        p1 = float(np.mod(t1, tg))

        # 한 period 내부에서 안 끊기면
        if (t0 // tg) == ((t1 - tol) // tg):
            hit = 0.0
            for s0, s1, amp in segments:
                s0 = float(s0)
                s1 = float(s1)
                if overlap(p0, p1, s0, s1):
                    hit = max(hit, abs(float(amp)))
            return hit

        # period 경계 넘는 경우: [p0, tg] + [0, p1]
        hit = 0.0
        for s0, s1, amp in segments:
            s0 = float(s0)
            s1 = float(s1)
            a = abs(float(amp))
            if overlap(p0, tg, s0, s1) or overlap(0.0, p1, s0, s1):
                hit = max(hit, a)
        return hit






    # =========================================================
    # Case1 / Case3 lazy visible-range 방식
    # =========================================================
    def _build_case1_runtime(self, step_params: dict, viewer_data: dict, duration_sec: float, tg: float):
        """case1 파라미터를 경량 dict로 변환 (배열 생성 없음)."""
        mode_pid = self._payload_pid(viewer_data.get("mode"))
        mode_text = (self._str_from_pid(step_params, mode_pid) or "").strip().upper()
        if not mode_text:
            mode_text = "PULSE"

        freq_pid = self._payload_pid(viewer_data.get("freq_drop"))
        local_freq_hz = self._convert_freq_to_hz(
            self._num_from_pid(step_params, freq_pid), freq_pid)

        if local_freq_hz <= 0 or duration_sec <= 0 or tg <= 0:
            return None

        c1 = viewer_data.get("case1", {}) or {}
        amp       = self._safe_num_or_zero(step_params, self._payload_pid(c1.get("amp")))
        l_duty    = self._safe_pct(self._num_from_pid(step_params, self._payload_pid(c1.get("duty"))), 50.0)
        p_duty    = self._safe_pct(self._num_from_pid(step_params, self._payload_pid(c1.get("pulse_duty"))), 100.0)
        offset    = self._safe_pct(self._num_from_pid(step_params, self._payload_pid(c1.get("offset"))), 0.0)
        hsp_off   = self._safe_pct(self._num_from_pid(step_params, self._payload_pid(c1.get("hsp_offset"))), 0.0)

        tl = 1.0 / local_freq_hz

        ia_pid  = self._payload_pid(c1.get("interval_amp_m"))
        if_pid  = self._payload_pid(c1.get("interval_freq"))
        id_pid  = self._payload_pid(c1.get("interval_duty"))
        ia_v    = self._num_from_pid(step_params, ia_pid)
        if_raw  = self._num_from_pid(step_params, if_pid)
        id_raw  = self._num_from_pid(step_params, id_pid)

        use_interval = all(v is not None for v in [ia_v, if_raw, id_raw])
        interval_period = interval_on = None
        if use_interval:
            if_hz = self._convert_freq_to_hz(if_raw, if_pid)
            if if_hz > 0:
                interval_period = 1.0 / if_hz
                interval_on = interval_period * (self._safe_pct(id_raw, 50.0) / 100.0)
            else:
                use_interval = False

        global_shift_val = float(tg * (offset / 100.0))
        global_on_val   = float(tg * (p_duty / 100.0))
        # offset + pulse_duty가 tg를 넘으면 wrap-around 방지: 창 끝을 tg로 클램프
        global_on_val = max(0.0, min(global_on_val, tg - global_shift_val))

        return {
            "type": "case1",
            "mode_text": mode_text,
            "duration_sec": float(duration_sec),
            "tg": float(tg),
            "global_on": global_on_val,
            "global_shift": global_shift_val,
            "tl": float(tl),
            "local_on": float(max(0.0, min(tl * (l_duty / 100.0), tl - tl * (hsp_off / 100.0)))),
            "local_shift": float(tl * (hsp_off / 100.0)),
            "amp": float(amp),
            "use_interval": use_interval,
            "interval_period": float(interval_period) if interval_period else None,
            "interval_on": float(interval_on) if interval_on else None,
            "interval_amp": float(ia_v) if ia_v is not None else 0.0,
        }

    def _build_case3_runtime(self, step_params: dict, viewer_data: dict, duration_sec: float, tg: float):
        """case3 파라미터를 경량 dict로 변환 (배열 생성 없음)."""
        mode_pid = self._payload_pid(viewer_data.get("mode"))
        mode_text = (self._str_from_pid(step_params, mode_pid) or "").strip().upper()
        if not mode_text:
            mode_text = "PULSE"

        c3 = viewer_data.get("case3", {}) or {}
        p_duty  = self._safe_pct(self._num_from_pid(step_params, self._payload_pid(c3.get("pulse_duty"))), 50.0)
        amp_h   = self._safe_num_or_zero(step_params, self._payload_pid(c3.get("amp_h")))
        amp_l   = self._safe_num_or_zero(step_params, self._payload_pid(c3.get("amp_l")))
        offset  = self._safe_pct(self._num_from_pid(step_params, self._payload_pid(c3.get("offset"))), 0.0)

        return {
            "type": "case3",
            "mode_text": mode_text,
            "duration_sec": float(duration_sec),
            "tg": float(tg),
            "on_time": float(tg * (p_duty / 100.0)),
            "shift": float(tg * (offset / 100.0)),
            "amp_h": float(amp_h),
            "amp_l": float(amp_l),
        }

    def _sample_case1_visible(self, rt: dict, x_min: float, x_max: float, view_pixel_width: int = 1200):
        """
        Case1 visible-range 샘플링 (3-level zoom).

        CONT 모드: 로컬 사각펄스가 전체 duration 동안 연속 (글로벌 게이트 없음)
        PULSE 모드: global gate(Pulse Duty 창) 안에서만 로컬 사각펄스 생성

        줌 레벨:
          1) local_cpp <= 0.5 : stair (로컬+글로벌 edge 모두 사용, 정확한 파형)
          2) global_cpp <= 0.5: global gate stair envelope (로컬 펄스 생략, 앨리어싱 방지)
          3) 그 외            : point-sample envelope
        """
        x_min = max(0.0, float(x_min))
        x_max = min(rt["duration_sec"], float(x_max))
        mode  = rt["mode_text"]
        if x_max <= x_min or mode not in ("PULSE", "CONT"):
            return np.array([]), np.array([])

        tl  = float(rt["tl"])
        tg  = float(rt["tg"])
        gon = float(rt["global_on"])
        gsh = float(rt["global_shift"])
        lon = float(rt["local_on"])
        lsh = float(rt["local_shift"])
        amp = float(rt["amp"])
        use_iv = bool(rt["use_interval"])
        ip  = rt.get("interval_period")
        io  = rt.get("interval_on")
        ia  = float(rt.get("interval_amp", 0.0) or 0.0)

        visible_len = x_max - x_min
        # 픽셀당 로컬 / 글로벌 사이클 수
        local_cpp  = (visible_len / tl) / max(view_pixel_width, 1)
        global_cpp = (visible_len / tg) / max(view_pixel_width, 1)

        # ── 진폭 envelope (interval 반영) ──
        def amp_env(mids: np.ndarray) -> np.ndarray:
            env = np.full(len(mids), amp, dtype=float)
            if use_iv and ip and io:
                imask = self._evaluate_square_mask(mids, float(ip), float(io), 0.0)
                env = np.where(imask, amp, ia)
            return env

        # ── 완전 신호: global_gate AND local_pulse × amp_env ──
        def full_signal(mids: np.ndarray) -> np.ndarray:
            lm = self._evaluate_square_mask(mids, tl, lon, lsh)
            if mode == "PULSE":
                gm = self._evaluate_square_mask(mids, tg, gon, gsh)
                active = gm & lm
            else:  # CONT: 글로벌 게이트 없음, 로컬 펄스만
                active = lm
            return np.where(active, amp_env(mids), 0.0).astype(float)

        # ── Envelope 신호: global gate on/off만 표시 (로컬 펄스 생략) ──
        def envelope_signal(mids: np.ndarray) -> np.ndarray:
            if mode == "PULSE":
                gm = self._evaluate_square_mask(mids, tg, gon, gsh)
                return np.where(gm, amp_env(mids), 0.0).astype(float)
            else:  # CONT: 항상 on → amp_env 그대로
                return amp_env(mids).astype(float)

        # ═══════════════════════════════════════════════════════
        # 1) 줌-인: 로컬 펄스가 픽셀에 충분히 표현 가능
        # ═══════════════════════════════════════════════════════
        if local_cpp <= 0.5:
            le = self._period_edges_in_range(x_min, x_max, tl, lon, lsh)
            if mode == "PULSE":
                ge = self._period_edges_in_range(x_min, x_max, tg, gon, gsh)
                edges = self._merge_boundaries(ge, le)
            else:
                edges = le  # CONT: 글로벌 edge 불필요
            if use_iv and ip and io:
                ie = self._period_edges_in_range(x_min, x_max, float(ip), float(io), 0.0)
                edges = self._merge_boundaries(edges, ie)
            mids = self._segment_midpoints(edges)
            if len(mids) == 0:
                return np.array([]), np.array([])
            return self._to_stair_xy(edges, full_signal(mids))

        # ═══════════════════════════════════════════════════════
        # 2) 중간 줌: 글로벌 gate는 표현 가능, 로컬은 앨리어싱 위험
        #    → envelope(글로벌 gate 형태)만 stair로 표시
        # ═══════════════════════════════════════════════════════
        if global_cpp <= 0.5:
            if mode == "PULSE":
                edges = self._period_edges_in_range(x_min, x_max, tg, gon, gsh)
            else:
                # CONT + interval: interval 경계가 있으면 그것으로, 없으면 flat 선
                if use_iv and ip and io:
                    edges = self._period_edges_in_range(x_min, x_max, float(ip), float(io), 0.0)
                else:
                    # 완전한 flat → 단순 두 점
                    return np.array([x_min, x_max]), np.array([amp, amp])
            if use_iv and ip and io:
                ie = self._period_edges_in_range(x_min, x_max, float(ip), float(io), 0.0)
                edges = self._merge_boundaries(edges, ie)
            mids = self._segment_midpoints(edges)
            if len(mids) == 0:
                return np.array([]), np.array([])
            return self._to_stair_xy(edges, envelope_signal(mids))

        # ═══════════════════════════════════════════════════════
        # 3) 완전 줌-아웃: point-sample envelope
        # ═══════════════════════════════════════════════════════
        n    = max(300, min(view_pixel_width, 2000))
        mids = np.linspace(x_min, x_max, n, dtype=float)
        return mids, envelope_signal(mids)

    def _sample_case3_visible(self, rt: dict, x_min: float, x_max: float, view_pixel_width: int = 1200):
        x_min = max(0.0, float(x_min))
        x_max = min(rt["duration_sec"], float(x_max))
        if x_max <= x_min:
            return np.array([]), np.array([])

        mode_text = rt["mode_text"]
        if mode_text == "CONT":
            return np.array([x_min, x_max]), np.array([rt["amp_h"], rt["amp_h"]])
        if mode_text != "PULSE":
            return np.array([x_min, x_max]), np.array([0.0, 0.0])

        tg, on_time, shift = rt["tg"], rt["on_time"], rt["shift"]
        amp_h, amp_l = rt["amp_h"], rt["amp_l"]
        cycles_per_pixel = ((x_max - x_min) / tg) / max(view_pixel_width, 1)

        if cycles_per_pixel <= 0.5:
            x    = self._period_edges_in_range(x_min, x_max, tg, on_time, shift)
            mids = self._segment_midpoints(x)
            if len(mids) == 0:
                return np.array([]), np.array([])
            y = np.where(self._evaluate_square_mask(mids, tg, on_time, shift), amp_h, amp_l).astype(float)
            return self._to_stair_xy(x, y)
        else:
            n    = max(300, min(view_pixel_width, 2000))
            mids = np.linspace(x_min, x_max, n, dtype=float)
            y    = np.where(self._evaluate_square_mask(mids, tg, on_time, shift), amp_h, amp_l).astype(float)
            return mids, y

    def _make_case13_runtime_for_recipe(self, viewer_data: dict, recipe: dict, case_type: str):
        blocks = []
        x_offset = 0.0
        for step in recipe["steps"]:
            params = step["params"]
            duration_sec, _, tg = self._global_period_and_axis(params)
            rt = (self._build_case1_runtime(params, viewer_data, duration_sec, tg)
                  if case_type == "case1"
                  else self._build_case3_runtime(params, viewer_data, duration_sec, tg))
            blocks.append({
                "x0": float(x_offset),
                "x1": float(x_offset + duration_sec),
                "step_name": step["step_name"],
                "runtime": rt,
            })
            x_offset += duration_sec
        return {
            "recipe_code": recipe["recipe_code"],
            "case_type": case_type,
            "blocks": blocks,
            "total_duration": float(x_offset),
        }

    def _update_case13_visible_curves(self, pw: pg.PlotWidget, case13_items: list):
        vb = pw.getPlotItem().vb
        (x_min, x_max), _ = vb.viewRange()
        pixel_width = max(400, int(pw.viewport().width()))

        for item in case13_items:
            rr        = item["runtime"]
            case_type = rr["case_type"]
            all_x, all_y = [], []

            for block in rr["blocks"]:
                bx0, bx1, rt = block["x0"], block["x1"], block["runtime"]
                if rt is None or bx1 <= x_min or bx0 >= x_max:
                    continue
                lx_min = max(0.0, x_min - bx0)
                lx_max = min(rt["duration_sec"], x_max - bx0)
                tx, ty = (self._sample_case1_visible(rt, lx_min, lx_max, pixel_width)
                          if case_type == "case1"
                          else self._sample_case3_visible(rt, lx_min, lx_max, pixel_width))
                if len(tx) > 0:
                    all_x.append(tx + bx0)
                    all_y.append(ty)

            if all_x:
                item["curve"].setData(np.concatenate(all_x), np.concatenate(all_y))
            else:
                item["curve"].setData([], [])

    def _make_case2_runtime_for_recipe(self, viewer_data: dict, recipe: dict):
        """
        recipe 전체 step에 대해 case2 runtime 정보 생성.
        """
        blocks = []
        x_offset = 0.0

        for step in recipe["steps"]:
            params = step["params"]
            duration_sec, _, tg = self._global_period_and_axis(params)
            runtime = self._calc_case2_step_runtime(params, viewer_data, duration_sec, tg)

            blocks.append({
                "x0": x_offset,
                "x1": x_offset + duration_sec,
                "step_name": step["step_name"],
                "runtime": runtime,
            })
            x_offset += duration_sec

        return {
            "recipe_code": recipe["recipe_code"],
            "blocks": blocks,
            "total_duration": x_offset,
        }


    def _render_case2_recipe_visible(self, recipe_runtime: dict, x_min: float, x_max: float):
        """
        recipe 전체에서 현재 보이는 범위만 이어붙여서 case2 파형 생성.
        """
        all_x = []
        all_y = []

        for block in recipe_runtime["blocks"]:
            bx0 = block["x0"]
            bx1 = block["x1"]
            runtime = block["runtime"]

            if runtime is None:
                continue
            if bx1 <= x_min or bx0 >= x_max:
                continue

            local_x_min = max(0.0, x_min - bx0)
            local_x_max = min(runtime["duration_sec"], x_max - bx0)

            tx, ty = self._sample_case2_visible(runtime, local_x_min, local_x_max)
            if len(tx) == 0:
                continue

            all_x.append(tx + bx0)
            all_y.append(ty)

        if not all_x:
            return np.array([]), np.array([])

        return np.concatenate(all_x), np.concatenate(all_y)


    def _update_case2_visible_curves(self, pw: pg.PlotWidget, case2_items: list[dict]):
        """
        현재 plot의 visible x-range 기준으로 case2 curve만 다시 그림.
        zoom 상태에 따라 실제 sine / envelope 자동 전환.
        """
        vb = pw.getPlotItem().vb
        (x_min, x_max), _ = vb.viewRange()
        pixel_width = max(400, int(pw.viewport().width()))

        for item in case2_items:
            recipe_runtime = item["runtime"]

            all_x = []
            all_y = []

            for block in recipe_runtime["blocks"]:
                bx0 = block["x0"]
                bx1 = block["x1"]
                runtime = block["runtime"]

                if runtime is None:
                    continue
                if bx1 <= x_min or bx0 >= x_max:
                    continue

                local_x_min = max(0.0, x_min - bx0)
                local_x_max = min(runtime["duration_sec"], x_max - bx0)

                tx, ty = self._sample_case2_visible(
                    runtime,
                    local_x_min,
                    local_x_max,
                    view_pixel_width=pixel_width,
                    max_points=8000
                )
                if len(tx) == 0:
                    continue

                all_x.append(tx + bx0)
                all_y.append(ty)

            if all_x:
                x = np.concatenate(all_x)
                y = np.concatenate(all_y)
                item["curve"].setData(x, y)
            else:
                item["curve"].setData([], [])

    # =========================================================
    # Rendering helpers
    # =========================================================
    def _add_step_guides(self, pw: pg.PlotWidget, step_boundaries: list[tuple[float, float, str]], y_top: float):
        for start_x, end_x, step_name in step_boundaries:
            line = pg.InfiniteLine(pos=start_x, angle=90, pen=pg.mkPen((180, 180, 180), width=0.8))
            pw.addItem(line)

            center_x = (start_x + end_x) / 2.0
            text = pg.TextItem(step_name, anchor=(0.5, 0))
            text.setColor((80, 80, 80))
            text.setPos(center_x, y_top)
            pw.addItem(text)

        if step_boundaries:
            last_end = step_boundaries[-1][1]
            line = pg.InfiniteLine(pos=last_end, angle=90, pen=pg.mkPen((180, 180, 180), width=0.8))
            pw.addItem(line)

    # =========================================================
    # Render
    # =========================================================
    def _render_all(self):
        self._plot_widgets.clear()

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

        for v_idx, viewer_data in enumerate(viewers, start=1):
            box = QGroupBox(f"Viewer {v_idx}")
            box_l = QVBoxLayout(box)

            pw = pg.PlotWidget()
            pw.showGrid(x=True, y=True)
            pw.addLegend()
            pw.setMinimumHeight(300)
            pw.setBackground("w")
            pw.getAxis("left").setTextPen("k")
            pw.getAxis("bottom").setTextPen("k")
            pw.setLabel("bottom", "Time (s)")
            pw.setLabel("left", "Amplitude")
            pw.setMouseEnabled(x=True, y=False)

            plotted_any = False
            global_ymax = 1.0
            all_step_guides = []
            case2_items = []
            case13_items = []
            max_total_duration = 0.0

            for r_idx, recipe in enumerate(checked_recipes):
                color = _RECIPE_COLORS[r_idx % len(_RECIPE_COLORS)]
                case_type = self._viewer_case_type(viewer_data)

                # -----------------------------
                # case2: visible-range 방식
                # -----------------------------
                if case_type == "case2":
                    recipe_runtime = self._make_case2_runtime_for_recipe(viewer_data, recipe)
                    if recipe_runtime["total_duration"] <= 0:
                        continue

                    max_total_duration = max(max_total_duration, recipe_runtime["total_duration"])
                    curve = pw.plot(
                        [], [],
                        pen=pg.mkPen(color=color, width=1.2),
                        name=recipe["recipe_code"]
                    )
                    case2_items.append({
                        "curve": curve,
                        "runtime": recipe_runtime,
                    })

                    for block in recipe_runtime["blocks"]:
                        all_step_guides.append((block["x0"], block["x1"], block["step_name"]))

                    plotted_any = True
                    continue

                # -----------------------------
                # case1 / case3: lazy visible-range 방식
                # -----------------------------
                if case_type in ("case1", "case3"):
                    recipe_runtime = self._make_case13_runtime_for_recipe(viewer_data, recipe, case_type)
                    if recipe_runtime["total_duration"] <= 0:
                        continue

                    max_total_duration = max(max_total_duration, recipe_runtime["total_duration"])
                    curve = pw.plot(
                        [], [],
                        pen=pg.mkPen(color=color, width=1.5),
                        name=recipe["recipe_code"]
                    )
                    case13_items.append({
                        "curve": curve,
                        "runtime": recipe_runtime,
                    })

                    for block in recipe_runtime["blocks"]:
                        all_step_guides.append((block["x0"], block["x1"], block["step_name"]))
                        rt = block["runtime"]
                        if rt is None:
                            continue
                        if case_type == "case1":
                            global_ymax = max(global_ymax, abs(rt["amp"]))
                            if rt["use_interval"]:
                                global_ymax = max(global_ymax, abs(rt.get("interval_amp", 0.0)))
                        else:
                            global_ymax = max(global_ymax, abs(rt["amp_h"]), abs(rt["amp_l"]))

                    plotted_any = True
                    continue

            if not plotted_any:
                msg = QLabel("No waveform matched current setting / selected recipes.")
                box_l.addWidget(msg)
            else:
                self._add_step_guides(pw, all_step_guides, y_top=global_ymax * 1.05)
                pw.setYRange(-global_ymax * 1.15, global_ymax * 1.18)
                box_l.addWidget(pw)

                self._plot_widgets.append(pw)

                # 초기 x-range를 데이터 범위로 설정 (기본값 [-1,1] 이면 아무것도 안 그려짐)
                if max_total_duration > 0:
                    pw.setXRange(0.0, max_total_duration, padding=0.02)

                def _on_xrange_for_sync(_vb, x_range, source_pw=pw):
                    if not self.chk_sync.isChecked() or self._syncing:
                        return
                    self._syncing = True
                    try:
                        for other_pw in self._plot_widgets:
                            if other_pw is source_pw:
                                continue
                            other_pw.getPlotItem().vb.setXRange(x_range[0], x_range[1], padding=0)
                    finally:
                        self._syncing = False

                pw.getPlotItem().vb.sigXRangeChanged.connect(_on_xrange_for_sync)

                # case1/case3 visible-range 동적 업데이트 연결
                if case13_items:
                    def _on_xrange_changed_13(_vb, _range, plot_widget=pw, items=case13_items):
                        self._update_case13_visible_curves(plot_widget, items)

                    pw.getPlotItem().vb.sigXRangeChanged.connect(_on_xrange_changed_13)
                    self._update_case13_visible_curves(pw, case13_items)

                # case2 visible-range 동적 업데이트 연결
                if case2_items:
                    def _on_xrange_changed(_vb, _range, plot_widget=pw, items=case2_items):
                        self._update_case2_visible_curves(plot_widget, items)

                    pw.getPlotItem().vb.sigXRangeChanged.connect(_on_xrange_changed)

                    # 최초 1회 렌더
                    self._update_case2_visible_curves(pw, case2_items)

                    # case2 amplitude 범위 대충 계산
                    ymax_case2 = 1.0
                    for item in case2_items:
                        for block in item["runtime"]["blocks"]:
                            rt = block["runtime"]
                            if not rt:
                                continue
                            for _, _, amp in rt["segments"]:
                                ymax_case2 = max(ymax_case2, abs(float(amp)))
                    pw.setYRange(-ymax_case2 * 1.15, ymax_case2 * 1.18)

            self.scroll_v.addWidget(box)

        self.scroll_v.addStretch(1)