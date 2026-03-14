"""EditingMixin: 셀 편집, 붙여넣기, 배치 연산, 언두"""
import logging
import traceback
from collections import defaultdict, namedtuple

logger = logging.getLogger(__name__)

from PyQt5.QtWidgets import QMessageBox, QApplication, QDialog
from PyQt5.QtCore import Qt

from ui.dialogs_recipe import RampEditDialog, DynamicStepEditDialog, RowBatchUpdateDialog

# Undo 액션 저장용 구조체
UndoAction = namedtuple("UndoAction", [
    "row",  # 편집 당시의 테이블 행 (참고용)
    "col",  # 편집 당시의 테이블 열
    "recipe_id",  # Key 1
    "param_id",  # Key 2
    "step_no",  # Key 3 (이게 없어서 문제였음)
    "mapping",  # 캐시 갱신용 키
    "old_value",
    "new_value"
])


class EditingMixin:

    def _open_ramp_editor(self, parent, proc_db, recipe_id, step_no, ramp_param_name):
        """RampEditDialog를 열고 결과를 처리합니다."""

        # [수정] 현재 Process Name 가져오기
        process_name = self.process_combo.currentText().strip()

        # [수정] get_ramp_edit_data 호출 시 process_name 전달
        ramp_data = self.recipe_service.get_ramp_edit_data(
            proc_db, recipe_id, step_no, ramp_param_name, process_name
        )

        if not ramp_data or "target_params" not in ramp_data:
            QMessageBox.warning(self, "Error", f"Could not load data for Ramp Editor: {ramp_param_name}")
            return

        dlg = RampEditDialog(parent, ramp_param_name, ramp_data['ramp_times'], ramp_data['target_params'])
        if dlg.exec_():
            # [수정] save_ramp_data 호출 시 process_name 전달
            self.recipe_service.save_ramp_data(
                proc_db, recipe_id, step_no, ramp_param_name, dlg.get_ramp_data(), process_name
            )
            self.update_recipe_table()

    def _open_dynamic_step_editor(self, parent, proc_db, recipe_id, step_no, step_name, dps_pid, dp_pid=None):
        """DynamicStepEditDialog 열고, 시작 스텝/반복횟수를 저장."""

        # 1) 전체 스텝 목록
        all_steps = self.recipe_service.get_all_steps_for_recipe(proc_db, recipe_id)
        if not all_steps:
            QMessageBox.warning(self, "Error", "Could not load step list for this recipe.")
            return

        # 2) 현재 스텝의 기존 값 읽기
        initial_start = None
        initial_repeat = None
        try:
            rows = self.db_manager.get_param_values(proc_db, [recipe_id], {})
            for rid, _step, s_no, pid, val, _aux, _rpid in rows:
                if s_no != step_no:
                    continue
                if dp_pid is not None and pid == dp_pid and isinstance(val, (int, float)):
                    initial_repeat = int(val)
                if dps_pid is not None and pid == dps_pid and isinstance(val, (int, float)):
                    initial_start = int(val)
        except Exception as e:
            logger.warning("Failed to read dynamic step values: %s", e)

        # 3) 다이얼로그
        dlg = DynamicStepEditDialog(parent, all_steps, step_no,
                                    initial_repeat=initial_repeat,
                                    initial_start=initial_start)
        if dlg.exec_():
            start_no, repeat = dlg.get_results()
            # 4) 저장: [수정] step_name을 전달
            self.recipe_service.save_dynamic_step_data(
                proc_db, recipe_id, step_no, step_name,
                dps_pid, start_no,
                dp_pid=dp_pid, repeat_count=repeat
            )
            self.update_recipe_table()

    def _on_left_item_edited(self, row, col, new_text):
        """
        왼쪽 tableLeft에서 값 수정 시 호출됩니다. (FastLeftModel.dataEdited 시그널)
        인자: row(int), col(int), new_text(str)
        """
        if row >= len(self._current_rows):
            return
        rid = self._current_rows[row][0]

        idx_comment = self.default_cols.index("Comment")
        idx_base = self.default_cols.index("Base")
        idx_step = self.default_cols.index("Step")
        idx_recipe = self.default_cols.index("Recipe")

        proc_db = getattr(self, "_current_process_db", None)
        if not proc_db:
            QMessageBox.warning(self, "Error", "No process database selected.")
            return

        # 1. Comment 수정
        if col == idx_comment:
            self.db_manager.update_recipe_metadata(proc_db, rid, comment=new_text)
            self._current_rows[row][1][idx_comment] = new_text
            self.left_model.update_data(row, col, new_text) # 모델 갱신

        # 2. Base 수정 (콤보박스)
        elif col == idx_base:
            new_base = new_text.strip()
            # 1. DB 업데이트
            self.db_manager.update_recipe_metadata(proc_db, rid, base_recipe=new_base if new_base else None)

            # 2. UI 데이터(캐시) 및 모델 갱신
            self._current_rows[row][1][idx_base] = new_base
            self.left_model.update_data(row, col, new_base)

            # 3. _base_map 갱신 (Diff View 로직을 위해 필수)
            if not new_base:
                # Base가 지워졌으면 맵에서 삭제
                if rid in self._base_map:
                    del self._base_map[rid]
            else:
                # 새로운 Base의 ID(base_rid)를 찾아야 함
                # (1) 현재 로드된 행들 중에서 찾기 (가장 빠름)
                found_base_rid = None
                for r_data in self._current_rows:
                    # r_data[1][idx_recipe]는 Recipe Code
                    if r_data[1][idx_recipe] == new_base:
                        found_base_rid = r_data[0]
                        break

                # (2) 로드된 행에 없다면 DB에서 조회 (필터링 등으로 안 보일 수 있음)
                if found_base_rid is None:
                    # 간단한 조회 쿼리 (이미 연결된 DB가 있으므로 빠르게 수행)
                    try:
                        with self.db_manager.get_connection(proc_db) as conn:
                            cur = conn.cursor()
                            # classification_id는 현재와 동일하다고 가정
                            cls_id = self._current_cls_id()
                            cur.execute("SELECT id FROM Recipe WHERE classification_id=? AND recipe_code=?",
                                        (cls_id, new_base))
                            res = cur.fetchone()
                            if res:
                                found_base_rid = res[0]
                    except Exception as e:
                        logger.warning("Failed to lookup base recipe '%s': %s", new_base, e)

                # 맵 업데이트
                if found_base_rid:
                    self._base_map[rid] = found_base_rid

        # 3. Step 수정
        elif col == idx_step:
            old_step = self._current_rows[row][1][idx_step]
            if old_step != new_text:
                self.db_manager.update_step_name(proc_db, rid, old_step, new_text)
                self._current_rows[row][1][idx_step] = new_text
                self.left_model.update_data(row, col, new_text)

        # 4. Recipe 코드 수정
        elif col == idx_recipe:
            new_code = new_text.strip()
            old_code = self._current_rows[row][1][idx_recipe]
            chamber = (self.chamber_id_combo.currentText() or "").strip()

            if not new_code:
                QMessageBox.warning(self, "Validation Error", "Recipe code cannot be empty.")
                # 원복 로직은 Model이 자동으로 처리하므로 별도 처리 불필요 (DB업데이트 안함)
                return

            if new_code == old_code:
                return

            # 중복 검사
            if new_code.casefold() != old_code.casefold():
                if self.db_manager.check_recipe_code_exists_in_chamber(proc_db, chamber, new_code):
                    QMessageBox.warning(self, "Duplicate", f"'{new_code}' already exists in Chamber '{chamber}'.")
                    return

            # DB 반영
            self.db_manager.update_recipe_metadata(proc_db, rid, recipe_code=new_code)

            # 현재 행 캐시 갱신
            self._current_rows[row][1][idx_recipe] = new_code
            self.left_model.update_data(row, col, new_code)

            # UI 상의 Base Recipe 컬럼 동기화
            # 현재 테이블에 로드된 모든 행 중, Base가 old_code였던 것들을 new_code로 변경
            for r_idx, (r_rid, r_base_vals, _) in enumerate(self._current_rows):
                if r_base_vals[idx_base] == old_code:
                    r_base_vals[idx_base] = new_code
                    self.left_model.update_data(r_idx, idx_base, new_code)

            # Base 콤보박스 리스트 갱신
            try:
                codes = list(self._current_recipe_codes)
                if old_code in codes:
                    k = codes.index(old_code)
                    codes[k] = new_code
                self._current_recipe_codes = codes
            except Exception as e:
                print(f"Error updating base combo: {e}")

    def _on_param_edited(self, row, col, new_text):
        """
        오른쪽 tableView에서 값 수정 시 호출됩니다. (FastRightModel.dataEdited 시그널)
        인자: row(int), col(int), new_text(str)
        """
        # 1. 정보 수집 (범위 검증)
        if row >= len(self._current_rows) or row >= len(self._row_stepnos):
            return
        if not hasattr(self, '_dyn_mappings') or col >= len(self._dyn_mappings) or col >= len(self._param_ids):
            return

        rid = self._current_rows[row][0]
        step_name = self._current_rows[row][1][4]
        mapping = self._dyn_mappings[col]
        param_id = self._param_ids[col]
        step_no = self._row_stepnos[row]
        current_unit = self._dyn_units[col] if hasattr(self, '_dyn_units') and col < len(self._dyn_units) else None

        # 2. 값 파싱
        #    [안전장치] Ramp("X > Y") / Dynamic Process("StepName (Step N)") 직접 편집 차단
        old_val_raw = self._current_rows[row][2].get(mapping)
        if isinstance(old_val_raw, str) and (" > " in old_val_raw or "(Step " in old_val_raw):
            return

        new_val = None
        if current_unit and current_unit.strip():
            # Unit 있음 -> 숫자만 허용
            try:
                if new_text.strip() == "":
                    new_val = None
                else:
                    new_val = float(new_text)
            except (ValueError, TypeError):
                new_val = None
        else:
            # Unit 없음 -> 문자열 허용
            new_val = new_text if new_text else None

        # 3. 값 비교 및 Undo Action 저장
        old_val = self._current_rows[row][2].get(mapping)
        is_same = False

        if old_val is None and new_val is None:
            is_same = True
        elif old_val is not None and new_val is not None:
            try:
                # 숫자 비교
                if abs(float(old_val) - float(new_val)) < 1e-9:
                    is_same = True
            except (ValueError, TypeError):
                # 문자열 비교
                if str(old_val) == str(new_val):
                    is_same = True

        if not is_same:
            # 4. DB Update
            proc_db = getattr(self, "_current_process_db", None)
            if proc_db:
                self.db_manager.update_parameter_value(proc_db, new_val, rid, param_id, step_no, step_name)

                # 4-1. DB 성공 후 Undo 기록
                action = UndoAction(row, col, rid, param_id, step_no, mapping, old_val, new_val)
                self._undo_stack.append(action)

                # 5. 캐시 갱신
                self._current_rows[row][2][mapping] = new_val

                # 6. Base Lookup 갱신
                occ_idx = self._row_occidx[row] if 0 <= row < len(self._row_occidx) else 0
                self._base_lookup[(rid, step_name, occ_idx, mapping)] = new_val

                # 7. 모델 갱신 (화면에 즉시 반영)
                self.right_model.update_data(row, col, new_val)

    def _on_batch_paste(self, changes: list):
        """
        changes: [(row, col, text_value), ...]
        [수정] 캐시/모델 업데이트를 DB 트랜잭션 성공 이후로 이동 (데이터 일관성 보장)
        [수정] INSERT 시에도 undo 기록 추가 (Ctrl+Z로 되돌리기 가능)
        """
        proc_db = getattr(self, "_current_process_db", None)
        if not proc_db: return

        db_updates = []
        cache_updates = []  # DB 성공 후 적용할 캐시 변경 목록

        self.tableView.setUpdatesEnabled(False)

        try:
            for row, col, text in changes:
                if row >= len(self._current_rows): continue
                if col >= len(self._param_ids) or col >= len(self._dyn_mappings): continue

                # 값 파싱 (_on_param_edited와 동일한 unit 기반 로직)
                current_unit = self._dyn_units[col] if hasattr(self, '_dyn_units') and col < len(self._dyn_units) else None
                if current_unit and current_unit.strip():
                    try:
                        val = float(text) if text.strip() else None
                    except (ValueError, TypeError):
                        val = None
                else:
                    val = text if text else None

                rid = self._current_rows[row][0]
                mapping_key = self._dyn_mappings[col]

                # [안전장치] Ramp("X > Y") / Dynamic Process("StepName (Step N)") 셀 보호
                existing_val = self._current_rows[row][2].get(mapping_key)
                if isinstance(existing_val, str) and (" > " in existing_val or "(Step " in existing_val):
                    continue
                step_no = self._row_stepnos[row]
                step_name = self._current_rows[row][1][4]
                param_id = self._param_ids[col]

                old_val = self._current_rows[row][2].get(mapping_key)

                # 값 비교
                is_changed = False
                if old_val is None and val is None:
                    is_changed = False
                elif old_val is not None and val is not None:
                    try:
                        if abs(float(old_val) - float(val)) > 1e-9: is_changed = True
                    except (ValueError, TypeError):
                        if str(old_val) != str(val): is_changed = True
                else:
                    is_changed = True

                if not is_changed: continue

                # 캐시 업데이트 정보를 모아둠 (DB 성공 후 적용)
                occ_idx = self._row_occidx[row]
                cache_updates.append((row, col, mapping_key, val, rid, step_name, occ_idx))

                # DB 업데이트 리스트
                db_updates.append((val, rid, param_id, step_no, step_name))

            if not db_updates: return

            # DB 트랜잭션 및 Undo 처리
            undo_data = []
            with self.db_manager.get_connection(proc_db) as conn:
                cur = conn.cursor()
                final_updates = []

                for (new_val, rid, pid, sno, sname) in db_updates:
                    cur.execute(
                        "SELECT id, value FROM RecipeParameter WHERE recipe_id=? AND parameter_id=? AND step_no=?",
                        (rid, pid, sno))
                    row_db = cur.fetchone()
                    if row_db:
                        pk_id, db_old_val = row_db
                        final_updates.append((new_val, pk_id))
                        undo_data.append({'id': pk_id, 'val': db_old_val, 'rid': rid, 'pid': pid, 'sno': sno})
                    else:
                        if new_val is not None:
                            cur.execute(
                                "INSERT INTO RecipeParameter (recipe_id, parameter_id, step, step_no, value) VALUES (?,?,?,?,?)",
                                (rid, pid, sname, sno, new_val))
                            # INSERT도 undo 기록 (삭제로 되돌림)
                            undo_data.append({'id': cur.lastrowid, 'val': None, 'rid': rid, 'pid': pid,
                                              'sno': sno, 'action': 'insert'})

                if final_updates:
                    cur.executemany("UPDATE RecipeParameter SET value=? WHERE id=?", final_updates)

            # DB 트랜잭션 성공 후에만 캐시/모델 업데이트
            for row, col, mapping_key, val, rid, step_name, occ_idx in cache_updates:
                self._current_rows[row][2][mapping_key] = val
                self._base_lookup[(rid, step_name, occ_idx, mapping_key)] = val
                self.right_model.update_data(row, col, val)

            if undo_data:
                self._undo_stack.append({"type": "BATCH_RESTORE", "data": undo_data})

        except Exception as e:
            QMessageBox.warning(self, "Error", f"Paste failed: {e}")
        finally:
            self.tableView.setUpdatesEnabled(True)
            self.tableView.viewport().update()

    def paste_combined_data(self):
        """
        [신규] 왼쪽 테이블에 붙여넣기(Ctrl+V) 시 호출됩니다.
        클립보드 데이터를 파싱하여, 왼쪽 테이블 영역을 벗어나는 데이터는
        자동으로 오른쪽 테이블(파라미터)에 붙여넣습니다.
        """
        text = QApplication.clipboard().text()
        if not text:
            return

        rows_text = [row for row in text.split('\n') if row]
        if not rows_text:
            return

        # 1. 붙여넣기 시작 위치 확인
        selection = self.tableLeft.selectionModel()
        if not selection.hasSelection():
            return

        # 가장 왼쪽 위 셀을 기준점으로 잡음
        indexes = selection.selectedIndexes()
        top_left = min(indexes, key=lambda i: (i.row(), i.column()))
        start_row = top_left.row()
        start_col = top_left.column()

        left_model = self.tableLeft.model()
        left_col_count = left_model.columnCount()

        right_model = self.tableView.model()
        if not right_model:
            return
        right_col_count = right_model.columnCount()

        # 오른쪽 테이블 일괄 업데이트를 위한 데이터 저장소
        # 형식: [(row, col, text_value), ...]
        right_changes = []

        # 2. 데이터 순회 및 분배
        for r_idx, row_data in enumerate(rows_text):
            cells = row_data.split('\t')
            target_r = start_row + r_idx

            # 행 범위를 벗어나면 중단 (혹은 추가 로직 가능)
            if target_r >= left_model.rowCount():
                break

            for c_idx, cell_value in enumerate(cells):
                target_c = start_col + c_idx

                # (A) 왼쪽 테이블 영역인 경우
                if target_c < left_col_count:
                    # 직접 setData 호출 (왼쪽은 기존 시그널 로직을 태움)
                    idx = left_model.index(target_r, target_c)
                    if idx.isValid():
                        # [주의] Recipe 컬럼(3)이나 Base 컬럼(2) 변경 시
                        # 테이블 리로드가 발생할 수 있어 주의 필요.
                        # 여기서는 단순 값 입력만 처리
                        left_model.setData(idx, cell_value, Qt.EditRole)

                # (B) 오른쪽 테이블 영역으로 넘어간 경우
                else:
                    # 오른쪽 테이블 기준 컬럼 인덱스 계산
                    right_c = target_c - left_col_count

                    if 0 <= right_c < right_col_count:
                        # 일괄 처리를 위해 리스트에 모음
                        right_changes.append((target_r, right_c, cell_value))

        # 3. 오른쪽 데이터 일괄 적용 (기존 Batch Paste 로직 활용)
        if right_changes:
            # 뷰포트 갱신 잠시 중단 (속도 향상)
            self.tableView.setUpdatesEnabled(False)
            try:
                # 모델에 값 설정 (UI 갱신)
                for r, c, val in right_changes:
                    idx = right_model.index(r, c)
                    pass

                # [최적화 로직]
                # 1) 오른쪽 모델 시그널 차단 (개별 DB 업데이트 방지)
                right_model.blockSignals(True)

                # 2) 모델에 값 입력 (화면 갱신용)
                real_changes = []
                for r, c, val in right_changes:
                    idx = right_model.index(r, c)
                    if idx.isValid():
                        right_model.setData(idx, val, Qt.EditRole)
                        real_changes.append((r, c, val))

                # 3) 시그널 차단 해제
                right_model.blockSignals(False)

                # 4) 일괄 DB 업데이트 및 Undo 등록 (배치 함수 호출)
                if real_changes:
                    self._on_batch_paste(real_changes)

            finally:
                self.tableView.setUpdatesEnabled(True)
                self.tableView.viewport().update()

    def copy_combined_data(self):
        """
        [신규] 왼쪽 테이블에서 복사(Ctrl+C) 시 호출됩니다.
        왼쪽 테이블의 선택된 행에 해당하는 '오른쪽 테이블 데이터'까지 합쳐서 클립보드에 복사합니다.
        """
        # 1. 왼쪽 테이블의 선택된 행 번호 수집
        selection = self.tableLeft.selectionModel()
        if not selection.hasSelection(): return

        selected_map = defaultdict(list)
        for idx in selection.selectedIndexes():
            selected_map[idx.row()].append(idx.column())

        rows = sorted(selected_map.keys())
        if not rows: return

        text_lines = []
        # 모델 직접 참조 (FastLeftModel, FastRightModel)
        model_left = self.tableLeft.model()
        model_right = self.tableView.model()
        if not model_right:
            return
        cols_right = model_right.columnCount()

        for r in rows:
            row_texts = []
            # 왼쪽 테이블
            cols_in_row = sorted(selected_map[r])
            for c in cols_in_row:
                idx = model_left.index(r, c)
                # DisplayRole로 문자열 가져오기
                val = model_left.data(idx, Qt.DisplayRole)
                row_texts.append(val if val else "")

            # 오른쪽 테이블 (전체 컬럼)
            if r < model_right.rowCount():
                for c in range(cols_right):
                    idx = model_right.index(r, c)
                    val = model_right.data(idx, Qt.DisplayRole)
                    row_texts.append(val if val else "")

            text_lines.append("\t".join(row_texts))

        QApplication.clipboard().setText("\n".join(text_lines))

    def apply_recipe_batch_change(self):
        """
        [신규] 선택된 Recipe들에 포함된 '모든 Step'의 특정 파라미터 값을 일괄 변경합니다.
        """

        # 1. 선택된 레시피 ID 수집
        selection_model = self.tableLeft.selectionModel()
        if not selection_model.hasSelection():
            return

        selected_rows = list(set(index.row() for index in selection_model.selectedIndexes()))
        target_recipe_ids = set()

        for r in selected_rows:
            if r < len(self._current_rows):
                rid = self._current_rows[r][0]
                target_recipe_ids.add(rid)

        if not target_recipe_ids:
            return

        # 2. 다이얼로그 띄우기
        if not hasattr(self, '_dyn_cols') or not self._dyn_cols:
            QMessageBox.warning(self, "Warning", "No parameter columns to update.")
            return

        dlg = RowBatchUpdateDialog(self, self._dyn_cols)
        if dlg.exec_() != QDialog.Accepted:
            return

        col_indices, multiplier, precision = dlg.get_data()

        target_param_ids = []
        for c_idx in col_indices:
            if c_idx < len(self._param_ids):
                target_param_ids.append(self._param_ids[c_idx])

        if not target_param_ids:
            return

        proc_db = getattr(self, "_current_process_db", None)
        if not proc_db:
            return

        # 3. DB 업데이트 및 Undo 데이터 생성
        updated_count = 0

        try:
            with self.db_manager.get_connection(proc_db) as conn:
                cur = conn.cursor()

                recipe_ph = ",".join("?" for _ in target_recipe_ids)
                param_ph = ",".join("?" for _ in target_param_ids)

                # [중요] 복구를 위해 id, value 뿐만 아니라 식별 정보(recipe_id, parameter_id, step_no)도 조회
                query = f"""
                        SELECT id, value, recipe_id, parameter_id, step_no
                          FROM RecipeParameter
                         WHERE recipe_id IN ({recipe_ph})
                           AND parameter_id IN ({param_ph})
                    """
                query_args = list(target_recipe_ids) + target_param_ids

                cur.execute(query, query_args)
                rows_to_update = cur.fetchall()

                if not rows_to_update:
                    QMessageBox.information(self, "Info", "No matching data found to update.")
                    return

                batch_args = []  # DB 업데이트용: (new_val, row_id)
                undo_snapshot = []  # Undo용 백업: (row_id, old_val, recipe_id, param_id, step_no)

                for row_id, old_val, rid, pid, step_no in rows_to_update:
                    # 값이 없으면(None) 계산 불가하므로 스킵 (혹은 0 취급 정책에 따라 수정 가능)
                    if old_val is None:
                        continue

                    try:
                        val_float = float(old_val)
                        new_val = round(val_float * multiplier, precision)

                        # 값이 실제로 변하는 경우만 처리
                        if abs(val_float - new_val) > 1e-9:
                            batch_args.append((new_val, row_id))
                            # 백업 데이터 저장 (기존 값 보존)
                            undo_snapshot.append({
                                'id': row_id,
                                'val': old_val,  # 기존 값 (복구용)
                                'rid': rid,
                                'pid': pid,
                                'sno': step_no
                            })
                            updated_count += 1
                    except (ValueError, TypeError):
                        continue

                if batch_args:
                    # A. 실제 DB 업데이트
                    cur.executemany("UPDATE RecipeParameter SET value = ? WHERE id = ?", batch_args)

                    # B. Undo 스택에 푸시 (새로운 타입: BATCH_RESTORE)
                    self._undo_stack.append({
                        "type": "BATCH_RESTORE",
                        "data": undo_snapshot
                    })

            # 4. UI 갱신
            self.update_recipe_table()
            QMessageBox.information(self, "Success",
                                    f"Updated {updated_count} values across {len(target_recipe_ids)} recipe(s).")

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to batch update recipes: {e}")
            traceback.print_exc()

    def apply_row_batch_change(self):
        """
        선택된 행(Row)들에 대하여,
        사용자가 체크박스로 선택한 컬럼(Para)들의 값을 일괄 변경합니다.
        """
        # 1. 선택된 행 추출
        indexes = self.tableView.selectionModel().selectedIndexes()
        if not indexes: return
        target_rows = sorted(list(set(idx.row() for idx in indexes)))

        # 2. 다이얼로그
        if not hasattr(self, '_dyn_cols') or not self._dyn_cols: return
        dlg = RowBatchUpdateDialog(self, self._dyn_cols)
        if dlg.exec_() != QDialog.Accepted: return
        target_col_indices, multiplier, precision = dlg.get_data()

        proc_db = getattr(self, "_current_process_db", None)
        if not proc_db: return

        updated_count = 0
        self.tableView.setUpdatesEnabled(False)  # 렌더링 잠금

        try:
            undo_snapshot = []
            db_updates = []

            with self.db_manager.get_connection(proc_db) as conn:
                cur = conn.cursor()

                for r in target_rows:
                    if r >= len(self._current_rows): continue
                    rid = self._current_rows[r][0]
                    step_no = self._row_stepnos[r]
                    step_name = self._current_rows[r][1][4]
                    row_data_map = self._current_rows[r][2]

                    for c in target_col_indices:
                        if c >= len(self._param_ids) or c >= len(self._dyn_mappings): continue
                        param_id = self._param_ids[c]
                        mapping_key = self._dyn_mappings[c]

                        cur.execute(
                            "SELECT id, value FROM RecipeParameter WHERE recipe_id=? AND parameter_id=? AND step_no=?",
                            (rid, param_id, step_no))
                        row_db = cur.fetchone()

                        if not row_db: continue
                        rp_id, old_val_db = row_db
                        if old_val_db is None: continue

                        try:
                            val_float = float(old_val_db)
                            new_val = round(val_float * multiplier, precision)

                            if abs(val_float - new_val) > 1e-9:
                                db_updates.append((new_val, rp_id))
                                undo_snapshot.append(
                                    {'id': rp_id, 'val': old_val_db, 'rid': rid, 'pid': param_id, 'sno': step_no})
                                updated_count += 1
                        except (ValueError, TypeError):
                            continue

                if db_updates:
                    cur.executemany("UPDATE RecipeParameter SET value = ? WHERE id = ?", db_updates)
                    self._undo_stack.append({"type": "BATCH_RESTORE", "data": undo_snapshot})

            # DB 커밋 성공 후 캐시/UI 갱신
            if db_updates:
                for r in target_rows:
                    if r >= len(self._current_rows): continue
                    rid = self._current_rows[r][0]
                    step_name = self._current_rows[r][1][4]
                    row_data_map = self._current_rows[r][2]
                    for c in target_col_indices:
                        if c >= len(self._param_ids) or c >= len(self._dyn_mappings): continue
                        param_id = self._param_ids[c]
                        mapping_key = self._dyn_mappings[c]
                        old_val_db = row_data_map.get(mapping_key)
                        if old_val_db is None: continue
                        try:
                            val_float = float(old_val_db)
                            new_val = round(val_float * multiplier, precision)
                            if abs(val_float - new_val) > 1e-9:
                                row_data_map[mapping_key] = new_val
                                self.right_model.update_data(r, c, new_val)
                                occ_idx = self._row_occidx[r]
                                self._base_lookup[(rid, step_name, occ_idx, mapping_key)] = new_val
                        except (ValueError, TypeError):
                            continue

            if updated_count > 0:
                QMessageBox.information(self, "Success", f"Updated {updated_count} cells.")
            else:
                QMessageBox.information(self, "Info", "No changes made.")

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Batch update failed: {e}")
            traceback.print_exc()
        finally:
            self.tableView.setUpdatesEnabled(True)
            self.tableView.viewport().update()

    def _undo(self):
        """
        Ctrl+Z: 마지막 작업을 되돌립니다. (값 수정, 레시피 삭제, 스텝 삭제)
        """

        if not self._undo_stack: return
        action = self._undo_stack.pop()
        proc_db = getattr(self, "_current_process_db", None)
        if not proc_db: return

        # Case 1: 단일 셀 수정
        if isinstance(action, UndoAction):
            row, col, rid, param_id, step_no, mapping, old_val, new_val = action
            with self.db_manager.get_connection(proc_db) as conn:
                conn.execute("UPDATE RecipeParameter SET value=? WHERE recipe_id=? AND parameter_id=? AND step_no=?",
                             (old_val, rid, param_id, step_no))

            # UI 복구
            # [Fix #2] col이 현재 param_id와 일치하는지 검증 (컬럼 변경 시 stale 방지)
            col_valid = (col < len(self._param_ids) and self._param_ids[col] == param_id)
            if row < len(self._current_rows) and col_valid:
                if self._current_rows[row][0] == rid and self._row_stepnos[row] == step_no:
                    self._current_rows[row][2][mapping] = old_val
                    self.right_model.update_data(row, col, old_val)

                    step_name = self._current_rows[row][1][4]
                    occ_idx = self._row_occidx[row]
                    self._base_lookup[(rid, step_name, occ_idx, mapping)] = old_val
                    self.tableView.viewport().update()
                else:
                    self.update_recipe_table()
            else:
                self.update_recipe_table()

        # Case 2: 레시피 삭제 복구
        elif isinstance(action, dict) and action["type"] == "RESTORE_RECIPES":
            try:
                for snapshot in action["data"]:
                    self.db_manager.restore_recipe(proc_db, snapshot)
                self.update_recipe_table()
            except Exception as e:
                QMessageBox.critical(self, "Undo Error", f"Failed: {e}")

        # Case 3: 스텝 삭제 복구
        elif isinstance(action, dict) and action["type"] == "RESTORE_STEPS":
            try:
                for item in action["data"]:
                    self.db_manager.restore_step_params(proc_db, item['rid'], item['params'])
                self.update_recipe_table()
            except Exception as e:
                QMessageBox.critical(self, "Undo Error", f"Failed: {e}")

        # Case 4: 일괄 변경 복구 (UPDATE 원복 + INSERT 삭제)
        elif isinstance(action, dict) and action["type"] == "BATCH_RESTORE":
            restore_data = action["data"]
            if not restore_data: return
            try:
                updates = [(d['val'], d['id']) for d in restore_data if d.get('action') != 'insert']
                deletes = [(d['id'],) for d in restore_data if d.get('action') == 'insert']
                with self.db_manager.get_connection(proc_db) as conn:
                    if updates:
                        conn.executemany("UPDATE RecipeParameter SET value = ? WHERE id = ?", updates)
                    if deletes:
                        conn.executemany("DELETE FROM RecipeParameter WHERE id = ?", deletes)

                # UI 효율적 갱신
                visible_map = defaultdict(list)
                for r_idx, (rid, _, _) in enumerate(self._current_rows):
                    sno = self._row_stepnos[r_idx]
                    visible_map[(rid, sno)].append(r_idx)

                pid_to_col = {pid: i for i, pid in enumerate(self._param_ids)}
                self.tableView.setUpdatesEnabled(False)

                for item in restore_data:
                    rid, sno, pid, old_val = item['rid'], item['sno'], item['pid'], item['val']
                    if (rid, sno) in visible_map and pid in pid_to_col:
                        col_idx = pid_to_col[pid]
                        mapping_key = self._dyn_mappings[col_idx]
                        for r_idx in visible_map[(rid, sno)]:
                            self._current_rows[r_idx][2][mapping_key] = old_val
                            self.right_model.update_data(r_idx, col_idx, old_val)  # 모델 업데이트

                            step_name = self._current_rows[r_idx][1][4]
                            occ_idx = self._row_occidx[r_idx]
                            self._base_lookup[(rid, step_name, occ_idx, mapping_key)] = old_val

                self.tableView.setUpdatesEnabled(True)
                self.tableView.viewport().update()
            except Exception as e:
                QMessageBox.critical(self, "Undo Error", f"Batch restore failed: {e}")