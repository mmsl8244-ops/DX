"""ColumnMixin: 컬럼 숨기기/순서/관리, ProEdit 다이얼로그"""
from PyQt5.QtWidgets import QMessageBox, QInputDialog, QDialog
from PyQt5.QtCore import Qt, QTimer

from ui.dialogs_recipe import ColumnEditDialog, ProEditDialog
from utils_recipe import make_order


class ColumnMixin:

    def open_column_dialog(self):
        chamber_id = self.chamber_id_combo.currentText()
        process_name = self.process_combo.currentText()  # [추가]

        if not chamber_id or not process_name:
            QMessageBox.warning(self, "Warning", "Please select Process and Chamber first.")
            return

        # [수정] process_name 전달
        definitions = self.recipe_service.get_column_definitions(chamber_id, process_name)
        if not definitions:
            QMessageBox.information(self, "Info", "No column definitions found.")
            return

        dlg = ColumnEditDialog(self, chamber_id, definitions)
        if dlg.exec_():
            updated_defs = dlg.get_updated_definitions()
            # [수정] 저장 시 process_name 전달
            success, message = self.recipe_service.save_column_definitions(chamber_id, process_name, updated_defs)

            if success:
                # 숨김 컬럼 재정렬도 Process Name 필요
                self._reorder_hidden_columns_to_end(chamber_id, process_name)
                QMessageBox.information(self, "Success", message)
                self.update_recipe_table()
            else:
                QMessageBox.warning(self, "Error", message)

    def open_proedit_dialog(self):
        # 1. Service를 통해 다이얼로그 초기 데이터 가져오기
        initial_data = self.recipe_service.get_pro_edit_initial_data()
        dlg = ProEditDialog(self, initial_data, self.recipe_service)

        # 2. 다이얼로그 시그널에 대한 슬롯(핸들러) 정의 및 연결
        def on_process_changed(process_name):
            schemes = self.recipe_service.get_scheme_codes_for_process(process_name)
            dlg.update_scheme_codes(schemes)

        def on_new_process():
            new_name, ok = QInputDialog.getText(dlg, "New Process", "Enter new process name:")
            if ok and new_name:
                success, message = self.recipe_service.create_new_process(new_name)
                QMessageBox.information(dlg, "Result", message)
                if success:
                    processes = self.recipe_service.get_available_processes()
                    dlg.update_processes(processes, select_process=new_name)

        def on_delete_process(process_name):
            if not process_name:
                QMessageBox.warning(dlg, "Warning", "No process selected.")
                return
            reply = QMessageBox.question(dlg, "Confirm Delete",
                                         f"Are you sure you want to delete process '{process_name}'?",
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                success, message = self.recipe_service.delete_process(process_name)
                QMessageBox.information(dlg, "Result", message)
                if success:
                    processes = self.recipe_service.get_available_processes()
                    dlg.update_processes(processes)

        dlg.process_changed.connect(on_process_changed)
        dlg.new_process_requested.connect(on_new_process)
        dlg.delete_process_requested.connect(on_delete_process)

        # 초기 SchemeCode 로드를 위해 수동으로 시그널 발생
        on_process_changed(dlg.process_combo.currentText())

        # 3. 다이얼로그 실행
        result = dlg.exec_()

        # [★★★ 수정됨 ★★★]
        # 다이얼로그가 닫히면 (OK든 Cancel이든) 무조건 메인 화면의 Process 목록을 갱신합니다.
        # 이렇게 해야 New/Delete로 변경된 DB 목록이 바로 반영됩니다.
        self.refresh_process_combo()

    def _hide_column(self, visual_col):
        # 1. 정확한 Mapping 키 찾기
        if not hasattr(self, '_dyn_mappings') or visual_col >= len(self._dyn_mappings):
            return

        mapping = self._dyn_mappings[visual_col]
        chamber = self.chamber_id_combo.currentText().strip()
        process_name = self.process_combo.currentText().strip()  # [추가]

        if not chamber or not mapping or not process_name: return

        # DB에서 현재 정보를 가져와야 PID와 Order를 알 수 있음
        defs = self.db_manager.get_full_param_defs(chamber, process_name)
        target = next((d for d in defs if d[2] == mapping), None)  # mapping 일치

        if target:
            # pid=target[0], order=target[4]
            update_item = {
                'pid': target[0],
                'mapping': mapping,
                'hide': 1,
                'order': target[4]
            }
            # [수정] batch update 사용
            self.db_manager.update_param_defs_batch(chamber, process_name, [update_item])

            self._reorder_hidden_columns_to_end(chamber, process_name)
            self.update_recipe_table()

    def _hide_selected_columns(self):
        """
        오른쪽 테이블에서 선택된 셀들이 포함된 모든 '열(Column)'을 숨깁니다.
        """

        # 1. 선택된 열 인덱스 수집 (중복 제거)
        selection = self.tableView.selectionModel()
        if not selection.hasSelection():
            return

        selected_cols = sorted(list(set(idx.column() for idx in selection.selectedIndexes())))
        if not selected_cols:
            return

        # 2. 매핑 키 수집
        mappings_to_hide = []
        if hasattr(self, '_dyn_mappings'):
            for col_idx in selected_cols:
                if 0 <= col_idx < len(self._dyn_mappings):
                    mappings_to_hide.append(self._dyn_mappings[col_idx])

        if not mappings_to_hide:
            return

        chamber = self.chamber_id_combo.currentText().strip()
        process_name = self.process_combo.currentText().strip()  # [추가]

        if not chamber or not mappings_to_hide or not process_name: return

        # 3. DB 업데이트 (일괄 처리)
        # 전체 정의를 가져와서 대상만 hide=1로 변경
        defs = self.db_manager.get_full_param_defs(chamber, process_name)
        updates = []

        for d in defs:
            if d[2] in mappings_to_hide and d[3] == 0:
                updates.append({
                    'pid': d[0],
                    'mapping': d[2],
                    'hide': 1,
                    'order': d[4]
                })

        if updates:
            # [수정] batch update 사용
            self.db_manager.update_param_defs_batch(chamber, process_name, updates)
            self._reorder_hidden_columns_to_end(chamber, process_name)
            self.update_recipe_table()

    def _reorder_hidden_columns_to_end(self, chamber_id, process_name):
        """[수정] process_name 인자 추가"""
        defs = self.db_manager.get_full_param_defs(chamber_id, process_name)

        visible_defs = [d for d in defs if d[3] == 0]
        hidden_defs = [d for d in defs if d[3] == 1]

        new_order_list = visible_defs + hidden_defs
        updates = []

        for i, d in enumerate(new_order_list):
            new_order_val = make_order(i + 1, 0)
            if d[4] != new_order_val:
                updates.append({
                    'pid': d[0],
                    'mapping': d[2],
                    'hide': d[3],
                    'order': new_order_val
                })

        if updates:
            self.db_manager.update_param_defs_batch(chamber_id, process_name, updates)

    def auto_hide_empty_columns(self):
        """
        [신규] 현재 로드된 레시피 데이터 중, 값이 하나도 없는 컬럼들을 자동으로 숨깁니다.
        """
        chamber = self.chamber_id_combo.currentText()
        process_name = self.process_combo.currentText().strip()  # [추가]
        if not chamber:
            return

        # 레시피 데이터가 없으면 동작 안 함 (파라미터만 있는 상태 방지)
        if not self._current_rows:
            QMessageBox.information(self, "Info", "No recipes loaded to check.")
            return

        # 사용자 확인
        reply = QMessageBox.question(
            self, "Auto Hide",
            "This will hide all columns that have no values in the currently loaded recipes.\nProceed?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        # 1. 각 매핑 키별로 값 존재 여부 확인
        # _dyn_mappings: 현재 화면에 보이는 컬럼들의 키 리스트
        columns_to_hide = []

        for mapping in self._dyn_mappings:
            has_value = False
            # 모든 행 검사
            for row_data in self._current_rows:
                # row_data[2] 가 파라미터 맵 {mapping: value}
                val = row_data[2].get(mapping)

                # 값이 있고(None 아님), 빈 문자열이 아니면 유효
                if val is not None and str(val).strip() != "":
                    has_value = True
                    break

            if not has_value:
                columns_to_hide.append(mapping)

        if not columns_to_hide:
            QMessageBox.information(self, "Info", "All columns have values. Nothing to hide.")
            return

        # 2. DB 업데이트 (일괄 Hide 처리)
        defs = self.db_manager.get_full_param_defs(chamber, process_name)
        updates = []

        for d in defs:
            if d[2] in columns_to_hide and d[3] == 0:
                updates.append({
                    'pid': d[0],
                    'mapping': d[2],
                    'hide': 1,
                    'order': d[4]
                })

        if updates:
            self.db_manager.update_param_defs_batch(chamber, process_name, updates)
            self._reorder_hidden_columns_to_end(chamber, process_name)
            self.update_recipe_table()
            QMessageBox.information(self, "Success", f"Hidden {len(updates)} empty columns.")

    def _on_section_moved(self, logicalIndex, oldVisualIndex, newVisualIndex):
        """
        컬럼 드래그 앤 드롭 이동 시 호출됩니다.
        [수정] 이동 직후 즉시 리로드하면 Qt 내부 상태와 충돌하므로,
        DB 저장 및 리로드를 QTimer.singleShot을 이용해 비동기로 처리합니다.
        """
        # 1. 제자리 이동이면 무시
        if oldVisualIndex == newVisualIndex:
            return
        chamber = self.chamber_id_combo.currentText().strip()
        process = self.process_combo.currentText().strip()
        # 필수 정보 확인
        if not chamber or not process or not hasattr(self, '_dyn_mappings'):
            return

        # 2. 현재 매핑 리스트 복사
        # (이 리스트는 현재 화면의 순서와 100% 일치한다고 가정할 수 있음. 매번 리로드하므로.)
        current_order = list(self._dyn_mappings)

        # 인덱스 범위 체크 (안전장치)
        if oldVisualIndex >= len(current_order) or newVisualIndex > len(current_order):
            return

        # 3. [핵심] 리스트 직접 조작 (Simulation)
        # 헤더의 상태를 물어보는 대신, "A를 빼서 B 자리에 넣었다"는 사실 자체를 수행
        try:
            # 이동할 아이템 꺼내기
            moved_item = current_order.pop(oldVisualIndex)
            # 새 위치에 넣기
            current_order.insert(newVisualIndex, moved_item)

        except Exception as e:
            return

        # 4. 지연 저장 및 리로드
        # (UI 애니메이션이 끝날 시간을 벌어주고, 충돌을 방지함)
        def save_and_refresh():
            # DB에 저장
            self.recipe_service.reorder_columns(chamber, process, current_order)
            # 테이블 리로드
            self.update_recipe_table()

        # 50ms 후 실행
        QTimer.singleShot(50, save_and_refresh)

    def _temp_hide_columns(self, selection, clicked_col):
        # 1. 선택된 영역이 있으면 선택된 컬럼들 숨김
        if selection.hasSelection():
            selected_cols = sorted(list(set(idx.column() for idx in selection.selectedIndexes())))
            for c in selected_cols:
                self.tableView.setColumnHidden(c, True)

        # 2. 선택 영역이 없으면 우클릭한 컬럼만 숨김
        elif clicked_col >= 0:
            self.tableView.setColumnHidden(clicked_col, True)

    def _temp_show_all_columns(self):
        model = self.tableView.model()
        if not model: return

        # 전체 컬럼을 순회하며 숨김 해제
        for c in range(model.columnCount()):
            self.tableView.setColumnHidden(c, False)

        # Diff View가 켜져있다면 다시 계산해서 숨겨야 함 (Sync 유지)
        if self.diff_view_chk.isChecked():
            self._update_diff_view()