"""RecipeCrudMixin: 레시피 CRUD, 스텝 관리, 컨텍스트 메뉴, import"""
import traceback

from PyQt5.QtWidgets import (
    QMessageBox, QDialog, QMenu, QInputDialog, QFileDialog,
    QApplication
)
from PyQt5.QtCore import Qt, QItemSelection, QItemSelectionModel

from ui.dialogs_recipe import (
    NewRecipeDialog, CopyRecipeDialog, RecipeImportDialog,
    ImportDBDialog, ParamMappingDialog, CommentEditDialog
)
from utils_recipe import CsvRecipeParser


class RecipeCrudMixin:

    def _get_active_selected_rows(self) -> list[int]:
        """
        현재 포커스가 있는 테이블(왼쪽 or 오른쪽)의 선택된 행 번호들을 반환합니다.
        포커스가 없으면 빈 리스트를 반환합니다.
        """

        # 1. 왼쪽 테이블 포커스 확인
        if self.tableLeft.hasFocus():
            selection = self.tableLeft.selectionModel()
        # 2. 오른쪽 테이블 포커스 확인
        elif self.tableView.hasFocus():
            selection = self.tableView.selectionModel()
        else:
            # 둘 다 포커스가 없으면 (혹은 다른 위젯) 처리 안 함
            return []

        if not selection.hasSelection():
            return []

        # 선택된 인덱스에서 행 번호만 추출하여 중복 제거 및 정렬
        return sorted(list(set(idx.row() for idx in selection.selectedIndexes())))

    def _action_delete_recipe_key(self):
        """단축키(Ctrl+Delete)로 레시피 삭제 시 호출"""

        rows = self._get_active_selected_rows()
        if not rows: return

        # 기존 로직을 재사용하기 위해, 왼쪽 테이블의 선택 영역을 동기화시킵니다.
        # (기존 _delete_selected_recipes 함수가 tableLeft.selectionModel()을 쓰기 때문)
        self._sync_selection_to_left(rows)

        # 기존 삭제 로직 호출
        self._delete_selected_recipes()

    def _action_delete_step_key(self):
        """단축키(Shift+Delete)로 스텝 삭제 시 호출"""
        rows = self._get_active_selected_rows()
        if not rows: return

        self._sync_selection_to_left(rows)
        self._delete_selected_steps()

    def _action_hide_recipe_key(self):
        """단축키(Ctrl+H)로 레시피 숨김 시 호출"""
        rows = self._get_active_selected_rows()
        if not rows: return

        self._sync_selection_to_left(rows)
        self._hide_selected_recipes()

    def _sync_selection_to_left(self, rows: list[int]):
        """
        오른쪽 테이블에서 작업했더라도, 로직 처리를 위해
        왼쪽 테이블의 해당 행들을 선택 상태로 만듭니다.
        """
        # 이미 왼쪽이 포커스면 할 필요 없음
        if self.tableLeft.hasFocus(): return

        # 오른쪽에서 선택된 행을 왼쪽 테이블에도 선택 적용
        mode = QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows
        selection = QItemSelection()
        model = self.tableLeft.model()

        for r in rows:
            # 행 전체 선택을 위한 Range 생성
            top_left = model.index(r, 0)
            bottom_right = model.index(r, model.columnCount() - 1)
            selection.select(top_left, bottom_right)

        self.tableLeft.selectionModel().select(selection, mode)

    def _on_header_context_menu(self, pos):
        hdr = self.tableView.horizontalHeader()
        col = hdr.logicalIndexAt(pos.x()) if hdr else self.tableView.columnAt(pos.x())

        menu = QMenu(self)
        copy_act = menu.addAction("Copy Recipe")
        new_act = menu.addAction("New Recipe")
        import_act = menu.addAction("Import Recipe")
        menu.addSeparator()
        temp_hide_act = menu.addAction("Hide Columns")
        temp_show_all_act = menu.addAction("Show Hide Columns")
        menu.addSeparator()
        not_use_act = menu.addAction("Not Use Column")
        auto_not_use_act = menu.addAction("Auto Not Use Empty Columns")
        menu.addSeparator()
        batch_act = None
        selection = self.tableView.selectionModel()
        if selection.hasSelection():
            batch_act = menu.addAction("Batch Change (Selected step)")

        act = menu.exec_(self.tableView.mapToGlobal(pos))
        if act is None:
            return

        # ─── New/Copy/Import 실행 전 컨텍스트 검증 ─────────────────────
        if act in (new_act, copy_act, import_act):
            # 1) Process 필수
            process = (self.process_combo.currentText() or "").strip()
            if not process or not getattr(self, "_current_process_db", None):
                QMessageBox.warning(self, "Warning", "Please select a Process first.")
                return

            # 2) cls_id 확인(이제는 currentData가 아니라 _current_cls_id)
            cls_id = self._current_cls_id()
            if cls_id is None:
                sheet = (self.sheet_combo.currentText() or "").strip()
                chamber = (self.chamber_id_combo.currentText() or "").strip()

                if not sheet:
                    QMessageBox.warning(self, "Warning", "Please select a Sheet first.")
                elif len(getattr(self, "_all_chambers_in_process", [])) > 1 and not chamber:
                    QMessageBox.warning(self, "Warning", "Multiple chambers found. Please select a Chamber.")
                else:
                    # 조합이 유효하지 않은 경우
                    QMessageBox.warning(self, "Warning", "Invalid Process/Sheet/Chamber combination.")
                return

        # ─── 액션 분기 ────────────────────────────────────────────────
        if act is new_act:
            self.open_new_recipe_dialog()
        elif act is copy_act:
            self.copy_recipe()
        elif act is import_act:
            self.open_recipe_import()
        elif act is not_use_act:
            if selection.hasSelection():
                self._hide_selected_columns()
            else:
                col = self.tableView.columnAt(pos.x())
                if col >= 0:
                    self._hide_column(col)
        elif act is batch_act:
            self.apply_row_batch_change()
        elif act is auto_not_use_act:
            self.auto_hide_empty_columns()
        elif act is temp_hide_act:
            self._temp_hide_columns(selection, col)
        elif act is temp_show_all_act:
            self._temp_show_all_columns()

    def _on_left_context_menu(self, pos):
        idx = self.tableLeft.indexAt(pos)
        if not idx.isValid():
            return

        col = idx.column()
        row = idx.row()
        menu = QMenu(self)

        # 현재 행의 데이터 파악
        if row < len(self._current_rows):
            rid = self._current_rows[row][0]
            idx_step = self.default_cols.index("Step")
            # 원본 데이터(SUFFIX 포함) 가져오기
            raw_step_name = str(self._current_rows[row][1][idx_step])
            is_hidden_step = raw_step_name.endswith(self.HIDDEN_SUFFIX)
        else:
            is_hidden_step = False
            rid = -1

        # 액션 변수 초기화
        del_recipe_act = None
        del_step_act = None
        hide_recipe_act = None
        show_all_act = None
        batch_recipe_act = None
        hide_step_act = None
        restore_step_act = None  # [신규] Restore
        show_hidden_act = None  # [신규] Temp Show

        # "Recipe" 컬럼인가?
        if col == self.default_cols.index("Recipe"):
            del_recipe_act = menu.addAction("Delete Selected Recipe(s)")
            menu.addSeparator()
            hide_recipe_act = menu.addAction("Hide Selected Recipe(s)")
            show_all_act = menu.addAction("Show All Hidden Recipes")
            menu.addSeparator()
            # 이미 "임시 보기 모드"인 레시피라면 -> "다시 숨기기" 메뉴 표시
            if rid in self._temp_shown_recipes:
                show_hidden_act = menu.addAction("Hide Hidden Steps (Close)")
            # 아니라면 -> "숨겨진 스텝 보기" 메뉴 표시
            else:
                show_hidden_act = menu.addAction("Show Hidden Steps")
            menu.addSeparator()
            batch_recipe_act = menu.addAction("Batch Change (All Steps)")

        # "Step" 컬럼인가?
        elif col == self.default_cols.index("Step"):
            del_step_act = menu.addAction("Delete Selected Step(s)")
            menu.addSeparator()
            if is_hidden_step:
                restore_step_act = menu.addAction("Restore Step (Unhide)")
            else:
                hide_step_act = menu.addAction("Hide Selected Step(s)")

        # 조건: 정확히 2개의 행이 선택되었을 때만 표시
        selection = self.tableLeft.selectionModel()
        selected_rows = sorted(list(set(i.row() for i in selection.selectedIndexes())))
        transition_act = None
        if len(selected_rows) == 2 and all(r < len(self._current_rows) for r in selected_rows):
            # 같은 Recipe 내의 Step인지 확인 (Recipe ID 비교)
            rid1 = self._current_rows[selected_rows[0]][0]
            rid2 = self._current_rows[selected_rows[1]][0]

            if rid1 == rid2:  # 같은 레시피일 때만
                menu.addSeparator()
                transition_act = menu.addAction("Transition")

        action = menu.exec_(self.tableLeft.viewport().mapToGlobal(pos))

        if action is None:
            return

        if action == del_recipe_act:
            self._delete_selected_recipes()
        elif action == del_step_act:
            self._delete_selected_steps()
        elif action == hide_recipe_act:
            self._hide_selected_recipes()
        elif action == show_all_act:
            self._show_all_recipes()
        elif action == transition_act:
            self._on_transition_action(selected_rows)
        elif action == batch_recipe_act:
            self.apply_recipe_batch_change()
        elif action == hide_step_act:
            self._action_hide_step()
        elif action == restore_step_act:
            self._action_restore_step()
        elif action == show_hidden_act:
            self._action_toggle_temp_show(rid)

    def copy_recipe(self):
        """'레시피 복사' 과정을 총괄 지휘합니다."""
        # 1. 목적지 정보 확인
        dest_cls_id = self._current_cls_id()
        dest_proc_db = self._current_process_db
        dest_chamber = self.chamber_id_combo.currentText().strip()
        if not all([dest_cls_id, dest_proc_db, dest_chamber]):
            QMessageBox.warning(self, "Warning", "Please select a destination (Process, Sheet, Chamber) first.")
            return

        # 2. 다이얼로그 생성 및 초기 데이터 제공
        all_processes = self.recipe_service.get_available_processes()
        initial_selection = {
            "process": self.process_combo.currentText(),
            "sheet": self.sheet_combo.currentText(),
            "chamber": dest_chamber
        }
        dlg = CopyRecipeDialog(self, all_processes, initial_selection)

        # 3. 다이얼로그의 시그널과 컨트롤러 로직(슬롯) 연결
        def on_process_changed(process):
            sheets = self.recipe_service.get_sheets_for_process(process)
            dlg.update_sheets(sheets,
                              initial_selection.get("sheet") if process == initial_selection.get("process") else None)

        def on_sheet_changed(sheet):
            process = dlg.process_combo.currentText()
            chambers = self.recipe_service.get_chambers_for_sheet(process, sheet)
            dlg.update_chambers(chambers,
                                initial_selection.get("chamber") if sheet == initial_selection.get("sheet") else None)

        def on_chamber_changed(chamber):
            process = dlg.process_combo.currentText()
            sheet = dlg.sheet_combo.currentText()
            recipes = self.recipe_service.get_recipes_for_chamber(process, sheet, chamber)
            dlg.update_base_recipes(recipes)

        dlg.process_changed.connect(on_process_changed)
        dlg.sheet_changed.connect(on_sheet_changed)
        dlg.chamber_changed.connect(on_chamber_changed)

        # 4. 초기 연쇄 반응을 수동으로 트리거
        on_process_changed(dlg.process_combo.currentText())

        # 5. 다이얼로그 실행 및 결과 처리
        if dlg.exec_() != QDialog.Accepted:
            return

        source_selection = dlg.get_source_selection()
        if not source_selection:
            QMessageBox.warning(self, "Warning", "All source fields must be selected.")
            return

        # 6. 서비스에 전달할 정보 정리
        src_proc_db = self.db_manager.get_process_db_path(source_selection["process"])
        src_cls_id = self.db_manager.get_classification_id(src_proc_db, source_selection["sheet"],
                                                           source_selection["chamber"])
        if src_cls_id is None:
            QMessageBox.warning(self, "Error", "Could not find the source classification (Sheet/Chamber mismatch).")
            return

        src_info = {
            "proc_db": src_proc_db, "cls_id": src_cls_id,
            "chamber_id": source_selection["chamber"], "recipe_code": source_selection["base_code"]
        }
        dest_info = {"proc_db": dest_proc_db, "cls_id": dest_cls_id, "chamber_id": dest_chamber}
        quantity = source_selection["quantity"]

        # 7. 서비스 호출 (1차 시도)
        success, message = self.recipe_service.copy_recipe_from_source(src_info, dest_info, quantity)

        # 8. 결과에 따른 UI 피드백
        if not success and message.startswith("PARAMETER_MISMATCH::"):
            # 8a. 파라미터 불일치 시 사용자에게 확인 질문
            missing_names_str = message.split("::")[1]
            missing_names = missing_names_str.split(',')
            msg = "The following parameters from the source recipe do not exist in the destination and will be skipped:\n\n- " + \
                  "\n- ".join(missing_names) + "\n\nDo you want to proceed with the copy?"
            reply = QMessageBox.question(self, "Parameter Mismatch", msg, QMessageBox.Yes | QMessageBox.No,
                                         QMessageBox.No)

            if reply == QMessageBox.Yes:
                # 8b. 사용자가 'Yes'를 누르면, 불일치 무시 옵션으로 다시 시도
                success, message = self.recipe_service.copy_recipe_from_source(src_info, dest_info, quantity,
                                                                               ignore_mismatch=True)
                if success:
                    QMessageBox.information(self, "Success", message)
                    self.update_recipe_table()
                else:
                    QMessageBox.warning(self, "Error", f"Copy failed on second attempt:\n{message}")

        elif success:
            # 8c. 첫 시도에 성공한 경우
            QMessageBox.information(self, "Success", message)
            self.update_recipe_table()
        else:
            # 8d. 그 외 다른 에러 발생 시
            QMessageBox.warning(self, "Error", message)

    def open_new_recipe_dialog(self):
        # 0. 컨텍스트 확인 (기존과 동일)
        cls_id = self._current_cls_id()
        chamber_id = self.chamber_id_combo.currentText()
        proc_db = self._current_process_db
        process_name = self.process_combo.currentText().strip()

        if not all([cls_id, chamber_id, proc_db, process_name]):
            QMessageBox.warning(self, "Warning", "Please select Process, Sheet, and Chamber first.")
            return

        # 1. 다이얼로그 생성 시 필요한 데이터(베이스 레시피 목록)를 전달
        dlg = NewRecipeDialog(self, self._current_recipe_codes)

        # 2. 다이얼로그의 시그널을 처리할 '슬롯' 메서드 연결
        def on_base_recipe_selected(base_code):
            # Service를 통해 스텝 목록을 가져옴
            steps = self.recipe_service.get_steps_for_base_recipe(proc_db, cls_id, base_code)
            # 다이얼로그의 테이블을 채움
            dlg.populate_steps(steps)

        dlg.base_recipe_changed.connect(on_base_recipe_selected)

        # 3. 다이얼로그 실행
        if dlg.exec_() != QDialog.Accepted:
            return

        # 4. 다이얼로그가 닫히면, 입력 데이터를 가져옴
        data = dlg.get_data()

        # 5. 서비스 계층에 레시피 생성을 요청
        success, message = self.recipe_service.create_new_recipe(
            proc_db, cls_id, chamber_id, process_name,
            data["new_code"], data["base_code"], data["comment"],
            data["steps"], data["created_at"]
        )

        # 6. 결과 처리
        if success:
            QMessageBox.information(self, "Success", message)
            self.update_recipe_table()
        else:
            QMessageBox.warning(self, "Error", message)

    def open_recipe_import(self):
        chamber = self.chamber_id_combo.currentText().strip()
        proc_db = self._current_process_db
        cls_id = self._current_cls_id()

        if not all([chamber, proc_db, cls_id]):
            QMessageBox.warning(self, "Warning", "Please select Process, Sheet, and Chamber first.")
            return

        paths, _ = QFileDialog.getOpenFileNames(self, "Select CSV files", "", "CSV Files (*.csv)")
        if not paths: return

        # 1. 단순화된 다이얼로그를 띄워 사용자 설정 수집
        dlg = RecipeImportDialog(self, chamber, paths)
        if dlg.exec_() != QDialog.Accepted:
            return

        import_configs = dlg.get_import_configs()
        if not import_configs:
            QMessageBox.warning(self, "Warning", "No recipes were configured for import.")
            return

        # 2. 서비스에 작업 위임
        success, message = self.recipe_service.import_recipes_from_csv(proc_db, chamber, cls_id, import_configs)

        # 3. 결과 처리
        if success:
            QMessageBox.information(self, "Success", message)
            self.update_recipe_table()
        else:
            QMessageBox.warning(self, "Error", message)

    def _delete_selected_recipes(self):
        """
        선택된 레시피(들)를 일괄 삭제합니다.
        [수정사항] FastModel 적용을 위해 item() 대신 data() 메서드를 사용하여 값을 읽어옵니다.
        """

        # 1. 선택된 행 인덱스 가져오기
        selection_model = self.tableLeft.selectionModel()
        if not selection_model.hasSelection():
            return

        # 중복되지 않는 행 번호 리스트 추출
        selected_rows = sorted(list(set(idx.row() for idx in selection_model.selectedIndexes())))
        if not selected_rows:
            return

        idx_recipe = self.default_cols.index("Recipe")

        # 2. 삭제 대상 수집 (Recipe ID와 Code)
        # targets = { (recipe_id, recipe_code), ... }
        targets = set()
        model = self.tableLeft.model()

        for r in selected_rows:
            if r >= len(self._current_rows): continue

            # _current_rows[r][0] -> recipe_id
            rid = self._current_rows[r][0]

            # [수정] 모델에서 텍스트 가져오기 (item() 제거 -> index().data() 사용)
            # FastLeftModel은 data() 메서드를 통해 값을 반환합니다.
            idx = model.index(r, idx_recipe)
            val = model.data(idx, Qt.DisplayRole)
            rcode = str(val).strip() if val is not None else ""

            if rcode:
                targets.add((rid, rcode))

        if not targets:
            return

        proc_db = getattr(self, "_current_process_db", None)
        if not proc_db:
            QMessageBox.warning(self, "Error", "No process database selected.")
            return

        # 3. 안전장치: Base Recipe 사용 여부 검사 (일괄 검사)
        # 삭제하려는 레시피 중 하나라도 누군가의 Base라면 삭제를 막습니다.
        target_codes = [t[1] for t in targets]

        try:
            with self.db_manager.get_connection(proc_db) as conn:
                cur = conn.cursor()
                # 내가 지우려는 코드들이 누군가의 base_recipe로 쓰이고 있는지 확인
                # 단, 지우려는 레시피들끼리 서로 참조하는 건 상관없을 수 있으나 복잡하므로,
                # "DB 전체에서 참조되면 삭제 불가"로 안전하게 처리.

                placeholders = ','.join('?' for _ in target_codes)
                # 쿼리: Base로 쓰이고 있으면서 AND 그 레시피가 삭제 대상 목록에 없는 경우
                sql = f"""
                        SELECT DISTINCT base_recipe
                          FROM Recipe
                         WHERE base_recipe IN ({placeholders})
                           AND recipe_code NOT IN ({placeholders})
                    """
                # 인자: IN절 2개에 대해 target_codes를 두 번 전달
                cur.execute(sql, target_codes * 2)

                used_bases = [r[0] for r in cur.fetchall()]

            if used_bases:
                msg = (
                    f"Cannot delete selected recipes.\n"
                    f"The following recipes are currently used as Base Recipes by others:\n\n"
                    f"- {', '.join(used_bases)}\n\n"
                    f"Please change the Base Recipe of the child recipes first."
                )
                QMessageBox.warning(self, "Deletion Denied", msg)
                return

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to check base recipe usage:\n{e}")
            return

        # 4. 사용자 확인
        msg = f"Are you sure you want to delete {len(targets)} recipe(s)?"
        if len(targets) <= 5:
            msg += "\n\n" + "\n".join([f"- {t[1]}" for t in targets])

        reply = QMessageBox.question(self, "Confirm Delete", msg,
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)

        if reply != QMessageBox.Yes:
            return

        # 5. 삭제 실행 및 Undo 스택 저장
        #    [M1] snapshot+delete를 단일 루프로 병합 — 부분 실패 시 undo 정합성 보장
        undo_data_list = []
        try:
            for rid, rcode in targets:
                snapshot = self.db_manager.get_recipe_snapshot(proc_db, rid)
                if snapshot['meta']:
                    self.db_manager.delete_recipe(proc_db, rid)
                    undo_data_list.append(snapshot)

        except Exception as e:
            QMessageBox.critical(self, "Error", f"An error occurred during deletion: {e}")

        finally:
            if undo_data_list:
                self._undo_stack.append({
                    "type": "RESTORE_RECIPES",
                    "data": undo_data_list
                })
            self.update_recipe_table()

    def _delete_selected_steps(self):
        """
        선택된 스텝들을 일괄 삭제합니다. (여러 레시피에 걸쳐 있어도 처리 가능)
        [수정사항] FastModel 적용을 위해 item() 대신 data() 메서드를 사용하여 값을 읽어옵니다.
        """
        # 1. 선택된 행 인덱스 수집 (중복 제거)
        selection = self.tableLeft.selectionModel()
        if not selection.hasSelection(): return

        # 선택된 셀들의 Row 번호
        selected_rows = sorted(list(set(idx.row() for idx in selection.selectedIndexes())))
        if not selected_rows:
            return

        # 2. 삭제 대상 수집: { (recipe_id, step_name) }
        #    Set을 사용하여 중복 삭제 방지
        targets = set()
        idx_step = self.default_cols.index("Step")
        model = self.tableLeft.model()

        for r in selected_rows:
            # 데이터 범위 체크
            if r >= len(self._current_rows):
                continue

            # Recipe ID 가져오기
            # (행이 병합되어 있어도 _current_rows에는 각 행별 데이터가 다 있음)
            rid = self._current_rows[r][0]

            # [수정] Step 이름 가져오기 (item() 제거 -> index().data() 사용)
            idx = model.index(r, idx_step)
            val = model.data(idx, Qt.DisplayRole)
            step_name = str(val).strip() if val is not None else ""

            if not step_name: continue  # 빈 행 무시

            targets.add((rid, step_name))

        if not targets:
            return

        proc_db = getattr(self, "_current_process_db", None)
        if not proc_db: return

        # 3. 사용자 확인
        # (삭제 대상이 많으면 개수만 표시)
        msg = f"Are you sure you want to delete {len(targets)} selected step(s)?"
        if len(targets) <= 5:
            # 소수면 상세 표시
            details = []
            for rid, sname in list(targets)[:5]:
                details.append(f"Step '{sname}'")
            msg += "\n\n" + "\n".join(details)

        reply = QMessageBox.question(self, "Confirm Delete Steps",
                                     msg,
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # 4. DB 삭제 및 Undo 저장
        #    [M2] snapshot+delete를 단일 루프로 병합 — 부분 실패 시 undo 정합성 보장
        undo_data_list = []
        try:
            for rid, step_name in targets:
                params = self.db_manager.get_step_snapshot(proc_db, rid, step_name)
                if params:
                    self.db_manager.delete_step(proc_db, rid, step_name)
                    undo_data_list.append({
                        "rid": rid,
                        "params": params
                    })

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete steps: {e}")

        finally:
            if undo_data_list:
                self._undo_stack.append({
                    "type": "RESTORE_STEPS",
                    "data": undo_data_list
                })
            self.update_recipe_table()

    def _on_transition_action(self, selected_rows):
        """
        selected_rows: 정렬된 행 인덱스 리스트 [row1, row2]
        """
        r1, r2 = selected_rows[0], selected_rows[1]

        # Bounds check
        if r1 >= len(self._row_stepnos) or r2 >= len(self._row_stepnos):
            return

        # 데이터 추출
        recipe_id = self._current_rows[r1][0]
        idx_step = self.default_cols.index("Step")

        # Step 번호와 이름 추출
        step_no_1 = self._row_stepnos[r1]
        step_no_2 = self._row_stepnos[r2]

        step_name_1 = self._current_rows[r1][1][idx_step]
        step_name_2 = self._current_rows[r2][1][idx_step]

        # 1. 단계 수 입력 받기
        num, ok = QInputDialog.getInt(self, "Transition",
                                      "How many transition steps?",
                                      value=1, min=1, max=100)
        if not ok: return

        proc_db = getattr(self, "_current_process_db", None)
        if not proc_db: return

        # 2. Service 호출
        # 주의: 항상 Step No가 작은 쪽이 Start가 되도록 정렬해서 넘김 (선택 순서 무관)
        if step_no_1 > step_no_2:
            start_no, end_no = step_no_2, step_no_1
            start_name, end_name = step_name_2, step_name_1
        else:
            start_no, end_no = step_no_1, step_no_2
            start_name, end_name = step_name_1, step_name_2

        success, msg = self.recipe_service.create_transition_steps(
            proc_db, recipe_id,
            start_no, end_no,
            start_name, end_name,
            num
        )

        if success:
            QMessageBox.information(self, "Success", msg)
            self.update_recipe_table()
        else:
            QMessageBox.warning(self, "Error", msg)

    def _action_hide_step(self):
        """Step을 숨김처리 (DB에 SUFFIX 추가)"""
        selection = self.tableLeft.selectionModel()
        if not selection.hasSelection(): return

        selected_rows = sorted(list(set(idx.row() for idx in selection.selectedIndexes())))
        if not selected_rows: return

        proc_db = getattr(self, "_current_process_db", None)
        if not proc_db: return

        idx_step = self.default_cols.index("Step")
        targets = []

        for r in selected_rows:
            if r >= len(self._current_rows): continue
            rid = self._current_rows[r][0]
            step_name = str(self._current_rows[r][1][idx_step])

            # 이미 숨겨진 것은 패스
            if not step_name.endswith(self.HIDDEN_SUFFIX):
                targets.append((rid, step_name, r))

        if not targets: return

        try:
            for rid, old_name, r in targets:
                new_name = old_name + self.HIDDEN_SUFFIX
                # 1. DB Update
                self.db_manager.update_step_name(proc_db, rid, old_name, new_name)
                # 2. 캐시 Update
                self._current_rows[r][1][idx_step] = new_name

            # 3. 화면 갱신 (리로드를 해야 굵게/밑줄 스타일 등이 일괄 적용됨)
            self.update_recipe_table()

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to hide steps: {e}")

    def _action_restore_step(self):
        """임시로 보이는 숨겨진 Step을 복구 (DB에서 SUFFIX 제거)"""
        selection = self.tableLeft.selectionModel()
        if not selection.hasSelection(): return

        selected_rows = sorted(list(set(idx.row() for idx in selection.selectedIndexes())))
        proc_db = getattr(self, "_current_process_db", None)
        idx_step = self.default_cols.index("Step")

        targets = []
        for r in selected_rows:
            if r >= len(self._current_rows): continue
            rid = self._current_rows[r][0]
            step_name = str(self._current_rows[r][1][idx_step])

            # 숨겨진 스텝만 복구 대상
            if step_name.endswith(self.HIDDEN_SUFFIX):
                targets.append((rid, step_name, r))

        if not targets: return
        if not proc_db: return

        try:
            for rid, old_name, r in targets:
                new_name = old_name.removesuffix(self.HIDDEN_SUFFIX)
                self.db_manager.update_step_name(proc_db, rid, old_name, new_name)
                self._current_rows[r][1][idx_step] = new_name

            self.update_recipe_table()

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to restore steps: {e}")

    def _action_toggle_temp_show(self, rid):
        """Recipe의 숨겨진 Step을 임시로 보이게 하거나 다시 숨김"""
        # 이미 보고 있는 상태라면 -> 목록에서 제거 (다시 숨김)
        if rid in self._temp_shown_recipes:
            self._temp_shown_recipes.remove(rid)
        # 안 보고 있는 상태라면 -> 목록에 추가 (보임)
        else:
            self._temp_shown_recipes.add(rid)

        # 화면 갱신 (FastLeftModel/FastRightModel의 data()와 setRowHidden()이 다시 호출됨)
        self.update_recipe_table()

    def _hide_selected_recipes(self):
        """선택된 행에 해당하는 '모든 Recipe'를 숨깁니다."""

        selection_model = self.tableLeft.selectionModel()
        if not selection_model.hasSelection():
            return

        selected_rows = list(set(index.row() for index in selection_model.selectedIndexes()))
        if not selected_rows:
            return

        target_recipe_ids = set()
        for r in selected_rows:
            if 0 <= r < len(self._current_rows):
                rid = self._current_rows[r][0]
                target_recipe_ids.add(rid)

        if not target_recipe_ids:
            return

        # [★수정] 숨김 목록에 ID 추가
        self._hidden_recipe_ids.update(target_recipe_ids)

        model = self.tableLeft.model()
        if not model: return
        row_count = model.rowCount()

        # 화면 숨김 처리
        for r in range(row_count):
            if r >= len(self._current_rows): continue

            current_rid = self._current_rows[r][0]
            if current_rid in target_recipe_ids:
                self.tableLeft.setRowHidden(r, True)
                self.tableView.setRowHidden(r, True)

        self.tableLeft.clearSelection()
        self.tableView.clearSelection()

        self._update_diff_view()

    def _show_all_recipes(self):
        """숨겨진 모든 레시피(행)를 다시 보이게 합니다."""
        model = self.tableLeft.model()
        if not model:
            return
        self._hidden_recipe_ids.clear()
        row_count = model.rowCount()
        idx_step = self.default_cols.index("Step")

        for r in range(row_count):
            # HIDDEN_SUFFIX("$$") 스텝은 _temp_shown_recipes에 없으면 숨김 유지
            if r < len(self._current_rows):
                step_val = str(self._current_rows[r][1][idx_step])
                if step_val.endswith(self.HIDDEN_SUFFIX):
                    rid = self._current_rows[r][0]
                    if rid not in self._temp_shown_recipes:
                        continue
            self.tableLeft.setRowHidden(r, False)
            self.tableView.setRowHidden(r, False)

        self._update_diff_view()

    def open_import_dialog(self):
        # 1. 초기 데이터 로드 (여기서는 커서 변경 금지)
        chambers = self.db_manager.get_all_chambers()
        if not chambers:
            QMessageBox.warning(self, "Error", "No chambers found.")
            return

        existing_chambers = self.db_manager.get_chambers_with_definitions()

        # 2. 다이얼로그 띄우기 (existing_chambers 전달)
        dlg = ImportDBDialog(self, chambers, existing_chambers)
        if dlg.exec_() != QDialog.Accepted:
            return

        # 3. 사용자 확인 후 실제 작업 시작
        info = dlg.get_result()
        mode = info.get("mode")
        chamber_id = info.get("chamber", "")
        path = info.get("path", "")
        manual_params = info.get("manual_params", [])

        if not chamber_id:
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)

        try:
            if mode == "csv":
                parser = CsvRecipeParser()
                parsed = parser.parse_file(path)
                if parsed:
                    new_definitions = self.recipe_service._parse_defs_from_csv(path)
                else:
                    new_definitions = []
            elif mode == "manual":
                new_definitions = self.recipe_service._parse_defs_from_manual_defs(manual_params)
            else:
                new_definitions = []

            # 정규화
            new_definitions = self.recipe_service._normalize_defs_for_db(chamber_id, new_definitions)

            # [수정] process_name에 None 전달 -> Config 업데이트 생략하고 Master만 업데이트
            result = self.recipe_service.prepare_param_import(
                chamber_id, new_definitions, process_name=None
            )
            if result["needs_dialog"]:
                QApplication.restoreOverrideCursor()
                dlg = ParamMappingDialog(self, result["db_only"], result["import_only"])
                if dlg.exec_() != QDialog.Accepted:
                    success, message = False, "Import canceled by user."
                else:
                    mapped_pairs, final_new, final_legacy = dlg.get_results()
                    QApplication.setOverrideCursor(Qt.WaitCursor)
                    success, message = self.recipe_service.apply_param_import_result(
                        chamber_id, result["matched_updates"],
                        mapped_pairs, final_new, final_legacy, process_name=None)
            else:
                success, message = True, result["message"]

        except Exception as e:
            success = False
            message = f"Error: {e}"
            traceback.print_exc()

        finally:
            QApplication.restoreOverrideCursor()

        # 4. 결과 메시지 표시
        if success:
            self.update_recipe_table()
        else:
            QMessageBox.warning(self, "Operation Failed", message)