import sys
import os
import io
import csv
import re
import traceback
from PyQt5.QtWidgets import QMessageBox

_EXC_HOOK_RUNNING = False
# ── 16비트 Order 헬퍼 ───────────────────────────────────────────
LEVEL_SHIFT = 16
LEVEL_MASK = (1 << LEVEL_SHIFT) - 1  # 0xFFFF

def parse_order(order: int) -> tuple[int, int]:
    """order를 (main_idx, sub_idx)로 분해"""
    return (order >> LEVEL_SHIFT), (order & LEVEL_MASK)

def make_order(main_idx: int, sub_idx: int) -> int:
    """
    32비트 정수: 상위 16비트(main_idx), 하위 16비트(sub_idx)
    main_idx, sub_idx 모두 0~65535 범위여야 함.
    """
    return (main_idx << LEVEL_SHIFT) | (sub_idx & LEVEL_MASK)

def excepthook(exc_type, exc_value, exc_tb):
    """
    예외를 콘솔(stderr)에도 항상 출력하고, 팝업(QMessageBox)으로도 보여준다.
    훅 내부에서 추가 예외가 나도 재귀 호출되지 않도록 가드한다.
    """
    global _EXC_HOOK_RUNNING
    if _EXC_HOOK_RUNNING:
        # 훅 내부 2차 예외 발생 시: 최소한 콘솔에는 찍고 종료
        try:
            traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
            try:
                sys.stderr.flush()
            except Exception:
                pass
        except Exception:
            pass
        return

    _EXC_HOOK_RUNNING = True
    try:
        # 트레이스 문자열 준비
        try:
            trace = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        except Exception:
            buf = io.StringIO()
            traceback.print_exception(exc_type, exc_value, exc_tb, file=buf)
            trace = buf.getvalue()

        # 1) 팝업과 무관하게 항상 콘솔에 출력
        try:
            sys.stderr.write(trace)
            try:
                sys.stderr.flush()
            except Exception:
                pass
        except Exception:
            # 콘솔 출력이 실패해도 훅 동작은 계속
            pass

        # 2) 가능하면 팝업도 띄움 (실패해도 무시)
        try:
            QMessageBox.critical(None, "Unexpected Error", trace)
        except Exception:
            # GUI 사용 불가 시엔 이미 콘솔에 출력됨
            pass
    finally:
        _EXC_HOOK_RUNNING = False

# ──────────────── CSV 파싱 유틸 함수 ────────────────
def read_csv_rows(path: str, encoding: str = 'utf-8') -> list[list[str]]:
    """CSV 파일 전체를 2차원 리스트로 읽어 반환."""
    with open(path, newline='', encoding=encoding) as f:
        return [row for row in csv.reader(f)]

def extract_block(rows: list[list[str]], marker: str) -> list[list[str]]:
    """
    marker(예: "<Step Conditions>") 바로 다음 행부터
    다음에 나오는 "<...>" 마커 전까지의 행을 반환.
    """
    start = end = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == marker:
            start = i + 1
            continue
        if start is not None and row and re.match(r"<.*>", row[0].strip()):
            end = i
            break
    if start is None:
        return []
    return rows[start:end or len(rows)]

class CsvRecipeParser:
    """CSV 파일의 파싱을 전담하는 헬퍼 클래스."""

    def parse_file(self, path: str) -> dict | None:
        try:
            rows = read_csv_rows(path, 'utf-8')
        except UnicodeDecodeError:
            try:
                rows = read_csv_rows(path, 'cp949')
            except Exception:
                return None
        except Exception:
            return None

        step_block = extract_block(rows, "<Step Conditions>")
        param_block = extract_block(rows, "<Recipe Parameters>")

        step_rows = [r for r in step_block if r]
        if len(step_rows) < 3: return None

        header = step_rows[0]
        comment_row = self._find_row_by_label(step_rows, "comment")
        complete_row = self._find_row_by_label(step_rows, "step completion cond.")
        if not comment_row or not complete_row: return None

        complete_idx = self._find_col_by_label(complete_row, "complete")
        if complete_idx is None or complete_idx <= 1: return None

        steps_info = []
        for col in range(1, complete_idx):
            step_label = self._safe_cell(header, col).strip()
            if not step_label or not step_label.lower().startswith("step"): continue
            comment = self._safe_cell(comment_row, col).strip()
            steps_info.append({"abs_col": col, "label": step_label, "comment": comment})

        recipe_name = self._extract_recipe_name(rows) or os.path.splitext(os.path.basename(path))[0]

        return {
            "path": path, "rows": rows, "step_block": step_block, "param_block": param_block,
            "steps_info": steps_info, "recipe_name": recipe_name
        }

    def _safe_cell(self, row: list[str], idx: int) -> str:
        return (row[idx] if 0 <= idx < len(row) else "") or ""

    def _find_row_by_label(self, rows: list[list[str]], label: str) -> list[str] | None:
        return next((r for r in rows if r and self._safe_cell(r, 0).strip().lower() == label), None)

    def _find_col_by_label(self, row: list[str], label: str) -> int | None:
        label = (label or "").strip().lower()
        for i, cell in enumerate(row):
            if (cell or "").strip().lower() == label:
                return i
        return None

    def _extract_recipe_name(self, rows: list[list[str]]) -> str:
        name_row = self._find_row_by_label(rows, "recipe name")
        return self._safe_cell(name_row, 1).strip() if name_row else ""