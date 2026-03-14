"""ComboMixin: 콤보박스 핸들러, 프로세스 맵, 설정 저장/로드"""
import os
import sys
import json
import re

from PyQt5.QtWidgets import QMessageBox, QComboBox
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QStandardItemModel
from typing import Any

from config_recipe import db_path2


class ComboMixin:

    def _build_process_map(self):
        """
        [최적화] 기존: 모든 .db 파일을 열어 스캔 (1-5초 소요)
        현재: _process_map은 사용되지 않으므로 스캔을 생략합니다.
        프로세스 목록은 refresh_process_combo() → recipe_service.get_available_processes()가 담당.
        """
        pass

    def _build_process_index(self):
        """현재 Process DB의 인덱스를 구성합니다. (DB 접근은 Manager 사용)"""
        self._rc_rows = []
        self._sheet_to_entries = {}
        self._all_chambers_in_process = []

        if not self._current_process_db:
            return

        # DatabaseManager를 통해 데이터를 한 번에 가져옵니다.
        try:
            with self.db_manager.get_connection(self._current_process_db) as conn:
                cur = conn.cursor()
                cur.execute("SELECT sheet, id, chamber_id FROM RecipClassification ORDER BY sheet")
                self._rc_rows = cur.fetchall()

                cur.execute("SELECT DISTINCT chamber_id FROM RecipClassification ORDER BY chamber_id;")
                self._all_chambers_in_process = [r[0] for r in cur.fetchall()]
        except Exception as e:
            self._rc_rows = []
            self._all_chambers_in_process = []

        d = {}
        for s, cid, ch in self._rc_rows:
            d.setdefault(s, []).append({"cls_id": cid, "chamber": ch})
        self._sheet_to_entries = d

    def _current_cls_id(self):
        """
        현재 Sheet(이름)와 Chamber 선택에 기반해 cls_id를 찾는다.
        - Sheet 콤보는 '이름'만 저장 (중복 가능)
        - Chamber 콤보는 실제 chamber_id
        둘 다 선택되어야 유효한 cls_id가 나온다. 없으면 None.
        """
        sheet_name = (self.sheet_combo.currentText() or "").strip()
        chamber_id = (self.chamber_id_combo.currentText() or "").strip()
        if not sheet_name or not chamber_id:
            return None
        entries = self._sheet_to_entries.get(sheet_name, [])
        for e in entries:
            if e["chamber"] == chamber_id:
                return e["cls_id"]
        return None

    def refresh_process_combo(self):
        """
        Process 콤보박스를 갱신합니다.
        [수정] Service를 호출하여 'Recipe가 존재하는 프로세스'만 가져옵니다. (Main UI 필터링 적용)
        """
        # Service를 통해 필터링된 목록 가져오기 (True = 레시피 있는 것만)
        names = self.recipe_service.get_available_processes(only_with_recipes=True)

        # 콤보 채움 (맨 앞 빈칸 포함)
        self._populate_combo(self.process_combo, names, include_blank=True)

        # ▶ Process 선택 전에는 Chamber/Sheet 비활성 + 내용 리셋
        self._enable_secondary_filters(False)
        self._reset_secondary_filters()

    def _enable_secondary_filters(self, enabled: bool):
        """
        Process 선택 여부에 따라 2차 필터(Chamber, Sheet) 활성/비활성 전환.
        비활성 시 툴팁으로 이유를 표시.
        """
        for cb in (self.chamber_id_combo, self.sheet_combo):
            cb.setEnabled(enabled)
            cb.setToolTip("" if enabled else "Please select a Process first.")

    def _reset_secondary_filters(self):
        """2차 필터(Chamber/Sheet) 내용과 선택을 비우고 테이블도 초기화"""
        # 콤보 클리어
        self._populate_combo(self.chamber_id_combo, [], include_blank=True)
        self._populate_combo(self.sheet_combo, [], include_blank=True)
        # 테이블/라벨 리셋
        self._reset_table_and_label()

    def _reset_table_and_label(self):
        """테이블과 최신 레이블, 모든 필터/정렬 상태를 초기화"""
        self.clear_recipe_table()
        self._current_base_filter = None
        self._current_code_filter = None
        self._active_filters.clear()
        self._filter_universe.clear()
        self._sort_col_idx = None
        self._sort_order = Qt.AscendingOrder
        self.label.clear()
        self.latest_recipe_name = None
        self._temp_shown_recipes.clear()
        # 검색창 텍스트도 동기화
        self.search_rcp.blockSignals(True)
        self.search_rcp.clear()
        self.search_rcp.blockSignals(False)

    def _populate_combo(self, combo: QComboBox,
                        items: list[str],
                        data: list[Any] = None,
                        include_blank: bool = True):
        """
        콤보박스를 초기화하고, 리스트로 채웁니다.
        items: 표시할 텍스트 리스트
        data: 각 아이템의 userData 리스트 (없으면 items 그대로)
        include_blank: 맨 앞에 빈 항목 추가 여부
        """
        combo.blockSignals(True)
        combo.clear()
        if include_blank:
            combo.addItem("", None)
        if data is None:
            for txt in items:
                combo.addItem(txt)
        else:
            for txt, d in zip(items, data):
                combo.addItem(txt, d)
        combo.blockSignals(False)

    def clear_recipe_table(self):
        """tableLeft/tableView 전부 비우기"""
        self.tableLeft.clearSpans()
        self.tableView.clearSpans()

        # 더미/배경 처리용 실제 행 수 초기화
        self.tableLeft._real_row_count = 0
        self.tableView._real_row_count = 0

        empty_left = QStandardItemModel(self)
        empty_left.setColumnCount(len(self.default_cols))
        empty_left.setHorizontalHeaderLabels(self.default_cols)
        self.tableLeft.setModel(empty_left)
        self.tableView.setModel(QStandardItemModel(self))

    def _on_process_selected(self, index):
        # 테이블/라벨 초기화
        self._reset_table_and_label()

        process = (self.process_combo.currentText() or "").strip()
        self._hidden_recipe_ids.clear()
        self._current_process_db = (os.path.join(db_path2, f"{process}.db") if process else None)

        if not self._current_process_db:
            self._enable_secondary_filters(False)
            self._populate_combo(self.sheet_combo, [], include_blank=True)
            self._populate_combo(self.chamber_id_combo, [], include_blank=True)
            return

        # 2차 필터 활성
        self._enable_secondary_filters(True)

        # 인덱스 구성
        self._build_process_index()

        # Sheet: 이름만 유일화해서 채우기
        sheet_names = sorted(self._sheet_to_entries.keys(), key=str.lower)
        self._populate_combo(self.sheet_combo, sheet_names, include_blank=True)

        # Chamber: 아직 Sheet 미선택이므로 프로세스 내 전체 챔버를 보여줌
        self._populate_combo(self.chamber_id_combo, self._all_chambers_in_process, include_blank=True)

        # ▶ Sheet 후보가 딱 1개라면 자동 선택
        if len(sheet_names) == 1:
            self.sheet_combo.blockSignals(True)
            self.sheet_combo.setCurrentIndex(1)
            self.sheet_combo.blockSignals(False)
            # blockSignals 중 시그널이 발생하지 않으므로 직접 호출
            self._on_sheet_selected(1)

    def _on_sheet_selected(self, index):
        # 테이블/라벨 초기화
        self._reset_table_and_label()

        process_selected = bool(getattr(self, "_current_process_db", None))
        if not process_selected:
            return

        sheet_name = (self.sheet_combo.currentText() or "").strip()
        if not sheet_name:
            # Sheet 해제: Chamber 목록은 '프로세스 전체 챔버'로 복귀
            self._populate_combo(self.chamber_id_combo, self._all_chambers_in_process, include_blank=True)
            return

        # 선택된 Sheet 이름에 존재하는 Chamber 목록만 노출
        entries = self._sheet_to_entries.get(sheet_name, [])
        chambers_for_sheet = sorted({e["chamber"] for e in entries}, key=str.lower)
        self._populate_combo(self.chamber_id_combo, chambers_for_sheet, include_blank=True)

        # Auto-select 규칙(선택사항): 후보가 1개면 자동 선택
        if len(chambers_for_sheet) == 1:
            idx = self.chamber_id_combo.findText(chambers_for_sheet[0])
            if idx >= 0:
                self.chamber_id_combo.setCurrentIndex(idx)

    def _on_chamber_selected(self, index):
        # Sheet가 선택되지 않았다면, 테이블을 그릴 수 없으니 종료
        sheet_name = (self.sheet_combo.currentText() or "").strip()
        self._hidden_recipe_ids.clear()
        if not sheet_name:
            self._reset_table_and_label()
            return

        # 여기서는 어떤 콤보도 변경하지 않음(요구사항 1)
        # 단지 현재 조합(Sheet 이름 + Chamber)로 cls_id를 찾아 테이블을 갱신
        cls_id = self._current_cls_id()
        if cls_id is None:
            # 유효하지 않은 조합 → 테이블 비움
            self._reset_table_and_label()
            return

        # Chamber 변경 시 필터/정렬/상태 초기화
        self._current_code_filter = None
        self._active_filters.clear()
        self._filter_universe.clear()
        self._sort_col_idx = None
        self._sort_order = Qt.AscendingOrder
        self._temp_shown_recipes.clear()
        self.search_rcp.blockSignals(True)
        self.search_rcp.clear()
        self.search_rcp.blockSignals(False)
        # 테이블 갱신
        self._refresh_recipe_table(
            cls_id,
            code_filter=None,
            base_filter=self._current_base_filter
        )
        self._update_diff_view()
        self.update_latest_recipe_label()

    def save_default_settings(self):
        """현재 선택된 Process, Sheet, Chamber를 JSON 파일로 저장합니다."""
        proc = self.process_combo.currentText()
        sheet = self.sheet_combo.currentText()
        chamber = self.chamber_id_combo.currentText()

        if not all([proc, sheet, chamber]):
            QMessageBox.warning(self, "Warning", "Please select Process, Sheet, and Chamber first.")
            return

        config = {
            "process": proc,
            "sheet": sheet,
            "chamber": chamber
        }

        config_path = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(sys.argv[0])), "config.json")

        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            QMessageBox.information(self, "Success", f"Startup settings saved.\n\n{config}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save settings: {e}")

    def load_default_settings(self):
        """프로그램 시작 시 config.json을 읽어 설정을 복원합니다."""
        config_path = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(sys.argv[0])), "config.json")
        if not os.path.exists(config_path):
            return

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            target_proc = config.get("process")
            target_sheet = config.get("sheet")
            target_chamber = config.get("chamber")

            if not all([target_proc, target_sheet, target_chamber]):
                return

            # 1. Process 설정
            idx_proc = self.process_combo.findText(target_proc)
            if idx_proc < 0:
                raise ValueError(f"Process '{target_proc}' not found in database.")
            self.process_combo.setCurrentIndex(idx_proc)
            # setCurrentIndex가 _on_process_selected를 트리거하여 Sheet 콤보를 채움

            # 2. Sheet 설정 (Process 선택에 의해 콤보가 채워진 상태여야 함)
            idx_sheet = self.sheet_combo.findText(target_sheet)
            if idx_sheet < 0:
                raise ValueError(f"Sheet '{target_sheet}' not found in Process '{target_proc}'.")
            self.sheet_combo.setCurrentIndex(idx_sheet)
            # setCurrentIndex가 _on_sheet_selected를 트리거하여 Chamber 콤보를 채움

            # 3. Chamber 설정
            idx_chamber = self.chamber_id_combo.findText(target_chamber)
            if idx_chamber < 0:
                raise ValueError(f"Chamber '{target_chamber}' not found.")
            self.chamber_id_combo.setCurrentIndex(idx_chamber)

            # 성공적으로 로드됨 (테이블은 _on_chamber_selected에 의해 자동 로드됨)

        except Exception as e:
            # 설정 로드 실패 시 초기화
            self.refresh_process_combo()
            QMessageBox.warning(self, "Setting Load Error",
                                f"Saved settings are invalid or data is missing.\n\nError: {e}\n\nThe program will start with default state.")

    def remove_default_settings(self):
        """저장된 config.json 파일을 삭제하여 설정을 초기화합니다."""

        config_path = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(sys.argv[0])), "config.json")

        if not os.path.exists(config_path):
            QMessageBox.information(self, "Info", "No saved settings found.")
            return

        # 사용자 확인 (선택 사항이나 안전을 위해 추가)
        reply = QMessageBox.question(
            self, "Confirm Remove",
            "Are you sure you want to remove the startup settings?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        try:
            os.remove(config_path)
            QMessageBox.information(self, "Success",
                                    "Startup settings have been removed.\nThe program will start with a blank state next time.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to remove settings: {e}")

    def update_latest_recipe_label(self):
        """최신 Recipe 코드를 Label에 표시합니다."""
        cls_id = self._current_cls_id()
        if cls_id is None:
            self.label.clear()
            self.latest_recipe_name = None
            return

        proc_db = getattr(self, "_current_process_db", None)
        if not proc_db:
            QMessageBox.warning(self, "Error", "No process database selected.")
            return

        # [최적화] ORDER BY + LIMIT으로 후보를 줄인 뒤 Python에서 최종 선택
        try:
            with self.db_manager.get_connection(proc_db) as conn:
                cur = conn.cursor()
                # 최신 날짜의 레시피만 조회 (서브쿼리로 최신 날짜 필터)
                cur.execute("""
                    SELECT recipe_code, created_at, id FROM Recipe
                    WHERE classification_id=?
                      AND DATE(created_at) = (
                          SELECT MAX(DATE(created_at)) FROM Recipe WHERE classification_id=?
                      )
                    ORDER BY id DESC
                """, (cls_id, cls_id))
                candidates = cur.fetchall()
        except Exception as e:
            print(f"Error fetching latest recipe: {e}")
            candidates = []

        if not candidates:
            self.label.clear()
            self.latest_recipe_name = None
            return

        # 숫자가 있는 코드부터 필터
        numeric = []
        for code, _, rid in candidates:
            nums = re.findall(r"\d+", code)
            if nums:
                numeric.append((code, rid, int(nums[-1])))
        if numeric:
            max_n = max(n for _, _, n in numeric)
            top = [(code, rid) for code, rid, n in numeric if n == max_n]
            chosen = max(top, key=lambda x: x[1])[0] if len(top) > 1 else top[0][0]
        else:
            chosen = candidates[0][0]  # 이미 id DESC 정렬

        # Label 업데이트 및 저장
        self.label.setText(f"Latest Recipe : {chosen}")
        self.latest_recipe_name = chosen

    def on_search_rcp(self):
        cls_id = self._current_cls_id()
        if cls_id is None:
            QMessageBox.warning(self, "Warning", "Please select Process → Sheet → Chamber first.")
            return
        kw = self.search_rcp.text().strip() or None
        self._current_code_filter = kw

        # 검색 컨텍스트 변경 시 컬럼 필터 초기화 (DB 레벨 검색과 UI 레벨 필터 충돌 방지)
        self._active_filters.clear()
        self._filter_universe.clear()

        # 테이블 클리어 후 리프레시(더미 잔상 방지)
        self.clear_recipe_table()
        self._refresh_recipe_table(
            cls_id,
            code_filter=kw,
            base_filter=self._current_base_filter
        )
        self._update_diff_view()
        self.update_latest_recipe_label()