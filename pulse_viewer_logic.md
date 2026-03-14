# Pulse Viewer Logic

## Overview

`dialogs_pulse_viewer.py`는 레시피 조건별 펄스 파형을 미리보기 위한 뷰어 다이얼로그입니다.
각 뷰어(Viewer)는 `Local Frequency` 입력 방식에 따라 3가지 케이스로 구분됩니다.

---

## 케이스 분류 기준

| 조건 | 케이스 |
|------|--------|
| Local Frequency = drag-and-drop (manual 체크박스 OFF) | **Case 1** |
| manual 체크박스 ON, 주파수 ≠ 0 | **Case 2** |
| manual 체크박스 ON, 주파수 = 0 | **Case 3** |

---

## 공통 파라미터

- **Global Frequency** (`pulse_setting.global.pulse_freq`): 전역 주기 tg = 1/global_freq
- **Duration** (`pulse_setting.global.duration`): 스텝 총 시간
- **Local Mode** (drag-and-drop): CW / Pulse / Cont 중 하나 (레시피 스텝 값)

---

## Case 1 — Drag-and-drop 주파수 사각 펄스

### 파라미터 목록

| 번호 | 파라미터 | 설명 |
|------|----------|------|
| 1 | Mode | Pulse / Cont (drag-and-drop) |
| 2 | Frequency | drag-and-drop (local_freq_hz) |
| 3 | Pulse Duty | % of global period tg → 글로벌 ON 창 크기 |
| 4 | Duty | % of local period tl → 로컬 펄스 ON 시간 |
| 5 | Offset | % of global period → 글로벌 게이트 시간 shift |
| 6 | Amp. | 출력 amplitude |
| 7 | HSP Offset | % of local period → 로컬 펄스 시간 shift |
| 8 | Interval Amp.(M) | (선택) 인터벌 구간 amplitude |
| 9 | Interval Frequency | (선택) 인터벌 주파수 |
| 10 | Interval Duty | (선택) 인터벌 duty % |

### 파형 로직

```
tg = 1 / global_freq_hz
tl = 1 / local_freq_hz
global_on   = tg × (Pulse Duty % / 100)
global_shift= tg × (Offset % / 100)
local_on    = tl × (Duty % / 100)
local_shift = tl × (HSP Offset % / 100)
```

**CONT 모드**:
- 로컬 주파수 사각펄스가 전체 duration 동안 연속으로 켜짐
- 글로벌 게이트 없음
- `y = where(local_pulse, amp_env, 0)`

**PULSE 모드**:
- 각 global period(tg) 안에서, `[global_shift, global_shift + global_on)` 구간만 글로벌 게이트 ON
- 그 안에서 로컬 주파수 사각펄스 생성
- `y = where(global_gate AND local_pulse, amp_env, 0)`

**Interval 모드** (Interval Amp/Freq/Duty 모두 입력된 경우):
```
interval_period = 1 / interval_freq_hz
interval_on     = interval_period × (Interval Duty % / 100)
amp_env = where(interval_mask, Amp., Interval Amp.(M))
```
- Interval ON 구간: Amp. 적용
- Interval OFF 구간: Interval Amp.(M) 적용

### 시각화 (lazy visible-range 렌더링)

| 줌 레벨 | 조건 | 표시 방식 |
|---------|------|----------|
| 줌인 | local_cycles_per_pixel ≤ 0.5 | 로컬+글로벌 edge 기반 정확한 stair |
| 중간 | global_cycles_per_pixel ≤ 0.5 | 글로벌 gate envelope stair (로컬 펄스 생략) |
| 줌아웃 | 위 조건 모두 초과 | 포인트 샘플링으로 envelope |

---

## Case 2 — Manual 주파수 멀티스테이트 Sine

### 파라미터 목록

| 번호 | 파라미터 | 설명 |
|------|----------|------|
| 1 | Mode | CW / Pulse (drag-and-drop) |
| 2 | Frequency | manual 입력 (kHz 단위 고정, ≠ 0) |
| 3 | M1~M4 Amp. | 드래그앤드랍, 연속 입력만 유효 |
| 4 | M1~M3 Duty | 드래그앤드랍, M4 duty = 100% - (M1+M2+M3) 자동계산 |
| 5 | HSP Duty | 드래그앤드랍, NULL이면 100% |

### 파형 로직

```
tg = 1 / global_freq_hz          (global period)
tl = 1 / local_freq_hz           (local period = 1 / manual_freq_hz)
carrier = sin(2π × local_freq × t)

HSP Duty → active_len = tl × (HSP Duty % / 100)
           Null이면 100% → active_len = tl
```

**CW 모드**:
```
y = M1_amp × sin(2π × local_freq × t)
```
- 전체 duration 동안 M1 amplitude로 sine 파형 연속 출력

**PULSE 모드**:
- Global period(tg) 기준으로 M1~M4 state가 반복
- 각 state의 ON 구간 길이 = `tg × (Mx_Duty% / 100)`
- Mx_Duty 입력 개수: 유효 Amp 수 - 1 (마지막 state duty는 나머지 자동계산)
- **HSP Duty**: 각 global period 내에서 추가로 local period(tl) 기준 active window 적용

```
M-state amplitude envelope (per global period):
  phase_g = mod(t, tg)
  state 0: phase_g in [0,              tg×M1_duty%)    → M1_amp
  state 1: phase_g in [tg×M1_duty%,   tg×(M1+M2)%)   → M2_amp
  state 2: ...
  state N-1: remainder                                  → MN_amp

HSP Duty masking (per local period):
  phase_l = mod(t, tl)
  if phase_l >= active_len → signal = 0 (HSP OFF)
  else                     → signal = M-state amp × carrier
```

**최종 신호**:
```
y = M_state_amp(t) × hsp_mask(t) × sin(2π × local_freq × t)
```

### 시각화

| 줌 레벨 | 조건 | 표시 방식 |
|---------|------|----------|
| 줌인 | local_cycles_per_pixel ≤ 0.35 | 실제 sine 샘플링 |
| 줌아웃 | 위 초과 | 각 bin별 min/max band (envelope) |

---

## Case 3 — Manual 주파수 0 (DC 신호)

### 파라미터 목록

| 번호 | 파라미터 | 설명 |
|------|----------|------|
| 1 | Mode | Pulse / Cont (drag-and-drop) |
| 2 | Frequency | manual 0 입력 |
| 3 | Pulse Duty | % of global period → ON 구간 비율 |
| 4 | Amp. (H) | ON 구간 DC amplitude |
| 5 | Offset | % of global period → 시간 shift |
| 6 | Amp. (L) | OFF 구간 DC amplitude |

### 파형 로직

```
tg        = 1 / global_freq_hz
on_time   = tg × (Pulse Duty % / 100)
shift     = tg × (Offset % / 100)
```

**CONT 모드**:
```
y = Amp.(H)   ← 전체 duration 동안 flat DC
```

**PULSE 모드**:
```
phase_g = mod(t - shift, tg)
gate    = (phase_g < on_time)
y = where(gate, Amp.(H), Amp.(L))
```
- Gate ON 구간: Amp.(H) DC
- Gate OFF 구간: Amp.(L) DC

### 시각화

| 줌 레벨 | 조건 | 표시 방식 |
|---------|------|----------|
| 줌인 | global_cycles_per_pixel ≤ 0.5 | global edge 기반 stair |
| 줌아웃 | 위 초과 | 포인트 샘플링 |

---

## 아키텍처 요약

### 렌더링 파이프라인

```
_render_all()
  ├── case2: _make_case2_runtime_for_recipe()
  │          → sigXRangeChanged → _update_case2_visible_curves()
  │             → _sample_case2_visible()
  │
  └── case1/3: _make_case13_runtime_for_recipe()
               → sigXRangeChanged → _update_case13_visible_curves()
                  ├── case1: _sample_case1_visible()
                  └── case3: _sample_case3_visible()
```

### 성능 최적화

- **Debounce**: recipe 체크박스 변경 시 500ms QTimer 후 렌더링 (400+ 레시피 대응)
- **Lazy rendering**: 현재 보이는 X 범위만 계산 (전체 duration 배열 생성 없음)
- **3-level zoom**: case1 로컬 주파수 높을 때 앨리어싱 방지
- **X-axis sync**: 모든 viewer 동시 pan/zoom (Sync checkbox)
- **Color coding**: 체크된 레시피별 고유 색상 자동 할당
