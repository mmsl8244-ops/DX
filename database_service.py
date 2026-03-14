import os
import sqlite3
import shutil
import traceback
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

#== Local Imports ==
try:
    from .config_recipe import USER_ID
    from .database_manager import DatabaseManager
    from .utils_recipe import (
        CsvRecipeParser, extract_block, read_csv_rows, parse_order, make_order
    )
except ImportError:
    from config_recipe import USER_ID
    from database_manager import DatabaseManager
    from utils_recipe import (
        CsvRecipeParser, extract_block, read_csv_rows, parse_order, make_order
    )

class RecipeService:
    """
    UI(RecipeWindow)와 데이터(DatabaseManager) 사이의 중재자 역할.
    데이터를 가져와 UI에 필요한 형태로 가공하고, 복잡한 로직을 처리합니다.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    def _structure_data_for_view(self, recipes: list[tuple], param_rows: list[tuple],
                                    param_defs: list[tuple], dyn_mappings: list = None):
        id2map = {d[0]: d[2] for d in param_defs}

        # ═══════════════════════════════════════════════════════════════
        # [최적화] param_rows 단일 패스: 4회 반복 → 1회로 통합
        # 빌드 대상: temp_grouped, row_stepname_map, row_min_id,
        #            steps_by_recipe, proc_param_rows
        # ═══════════════════════════════════════════════════════════════
        temp_grouped = defaultdict(dict)
        row_stepname_map = {}
        row_min_id = {}
        steps_by_recipe = defaultdict(list)
        seen_steps = set()
        proc_param_rows = []

        id2map_get = id2map.get  # 로컬 참조로 메서드 룩업 비용 제거

        for rid, step, step_no, pid, val, aux_val, rpid in param_rows:
            key = (rid, step_no)

            # 1) temp_grouped
            mapping = id2map_get(pid)
            if mapping:
                temp_grouped[key][mapping] = val

            # 2) row_stepname_map
            row_stepname_map[key] = step

            # 3) row_min_id (Occurrence Index 계산용)
            cur_min = row_min_id.get(key)
            if cur_min is None or rpid < cur_min:
                row_min_id[key] = rpid

            # 4) steps_by_recipe
            if key not in seen_steps:
                steps_by_recipe[rid].append((step_no, step))
                seen_steps.add(key)

            # 5) proc_param_rows (SpecialCaseProcessor 입력)
            proc_param_rows.append((rid, step_no, pid, val, aux_val))

        # 데이터 전처리 (Processor 적용)
        processor = SpecialCaseProcessor(proc_param_rows, param_defs, row_stepname_map)

        all_data_map = {}
        for key, step_data in temp_grouped.items():
            rid, step_no = key
            all_data_map[key] = processor.process_step(rid, step_no, step_data)

        # Occurrence Index 계산
        name_occ_lists = defaultdict(list)
        for (rid, step_no), min_id in row_min_id.items():
            step_name = row_stepname_map.get((rid, step_no))
            if step_name:
                name_occ_lists[(rid, step_name)].append((min_id, step_no))

        occ_idx_map = {}
        for k in name_occ_lists:
            name_occ_lists[k].sort(key=lambda t: t[0])
        for (rid, step_name), pairs in name_occ_lists.items():
            for idx, (min_id, step_no) in enumerate(pairs):
                occ_idx_map[(rid, step_no)] = idx

        # base_lookup
        base_lookup = {}
        for (rid, step_no), data_dict in all_data_map.items():
            step_name = row_stepname_map.get((rid, step_no), "")
            occ_idx = occ_idx_map.get((rid, step_no), 0)
            for mkey, mval in data_dict.items():
                base_lookup[(rid, step_name, occ_idx, mkey)] = mval

        # ═══════════════════════════════════════════════════════════════
        # Rows + dense_right_data 동시 생성 (별도 패스 제거)
        # ═══════════════════════════════════════════════════════════════
        rows = []
        row_stepnos = []
        row_occidx = []
        dense_right_data = [] if dyn_mappings else None

        # to_disp 로컬 함수 (dense_right_data 생성용)
        def to_disp(v):
            if v is None: return ""
            if isinstance(v, float):
                return str(int(v)) if v.is_integer() else str(v)
            return str(v)

        occ_get = occ_idx_map.get
        data_get = all_data_map.get

        for r in recipes:
            rid = r[0]
            steps = sorted(steps_by_recipe.get(rid, []), key=lambda x: x[0])
            created_at = r[1] or ""
            date_disp = created_at[2:10].replace("-", "") if len(created_at) >= 10 else str(created_at)

            for step_no, step_name in steps:
                data = data_get((rid, step_no), {})
                base_vals = [date_disp, r[2] or "", r[3] or "", r[4], step_name]
                rows.append([rid, base_vals, data])
                row_stepnos.append(step_no)
                row_occidx.append(occ_get((rid, step_no), 0))

                # dense_right_data 인라인 생성 (별도 루프 제거)
                if dyn_mappings is not None:
                    param_get = data.get
                    dense_right_data.append([to_disp(param_get(m)) for m in dyn_mappings])

        # Groups 생성
        groups = []
        prev_rid = None
        for i, (rid, _, _) in enumerate(rows):
            if rid != prev_rid:
                groups.append({"start": i, "count": 1})
                prev_rid = rid
            else:
                groups[-1]["count"] += 1

        return rows, groups, base_lookup, row_stepnos, row_occidx, dense_right_data

    def load_recipe_data_for_view(self, proc_db_path, chamber_id, cls_id, process_name, code_filter=None,
                                  base_filter=None):

        if not all([proc_db_path, chamber_id, cls_id]):
            return [], [], [], [], {}, {}, [], [], [], {}, [], [], []

        # 1. 파라미터 정의 로드 (Process Name 전달 -> 순서/숨김/매핑 반영됨)
        defs = self.db_manager.get_full_param_defs(chamber_id, process_name)

        id_to_name_map = {d[0]: d[1] for d in defs}

        # 표시용(Hide=0) 필터링 및 정렬
        display_defs = [d for d in defs if d[3] == 0]
        # SQL에서 이미 정렬해오지만, 안전을 위해 한 번 더 정렬
        display_defs.sort(key=lambda x: x[4])

        id2ord = {d[0]: d[4] for d in display_defs}

        # 2~5. 단일 연결에서 레시피 목록 + Base ID + 파라미터 값 조회 (읽기 일관성 보장)
        with self.db_manager.get_connection(proc_db_path) as conn:
            # 2. 레시피 목록 조회
            recipes = self.db_manager.get_recipes(proc_db_path, cls_id, code_filter, base_filter, _conn=conn)
            visible_recipe_ids = [r[0] for r in recipes]
            recipe_codes = [r[4] for r in recipes]

            # 3. Base Recipe ID 확보
            needed_base_codes = {r[3] for r in recipes if r[3]}
            code_to_id_map = {}
            if needed_base_codes:
                cur = conn.cursor()
                placeholders = ','.join('?' for _ in needed_base_codes)
                cur.execute(
                    f"SELECT recipe_code, id FROM Recipe WHERE classification_id=? AND recipe_code IN ({placeholders})",
                    (cls_id, *needed_base_codes))
                for code, rid in cur.fetchall():
                    code_to_id_map[code] = rid

            # 4. base_map 생성 (O(n) dict 조회로 최적화)
            visible_code_to_id = {r[4]: r[0] for r in recipes}  # recipe_code → recipe_id
            base_map = {}
            for r in recipes:
                child_id = r[0]
                base_code = r[3]
                if base_code:
                    base_id = visible_code_to_id.get(base_code) or code_to_id_map.get(base_code)
                    if base_id:
                        base_map[child_id] = base_id

            all_ids_to_load = list(set(visible_recipe_ids + list(base_map.values())))

            # 5. 파라미터 값 조회
            param_rows = self.db_manager.get_param_values(proc_db_path, all_ids_to_load, id2ord, _conn=conn)

        # 6. 컬럼 정보 생성 (데이터 구조화 전에 dyn_mappings 먼저 준비)
        dyn_cols = []
        dyn_mappings = []
        dyn_units = []

        for d in display_defs:
            mapping = d[2]
            unit = d[5]
            dyn_mappings.append(mapping)
            dyn_units.append(unit)
            header_text = f"{mapping}\n({unit})" if unit else mapping
            dyn_cols.append(header_text)

        param_ids = [d[0] for d in display_defs]

        # 7. 데이터 구조화 + dense_right_data 동시 생성 (별도 패스 제거)
        rows, groups, base_lookup, row_stepnos, row_occidx, dense_right_data = \
            self._structure_data_for_view(recipes, param_rows, display_defs, dyn_mappings)

        return (
            dyn_cols, param_ids, rows, groups, base_map, base_lookup,
            recipe_codes, row_stepnos, row_occidx, id_to_name_map,
            dyn_mappings, dyn_units, dense_right_data
        )

    def create_new_recipe(self, proc_db_path: str, cls_id: int, chamber_id: str, process_name: str, new_code: str,
                          base_code: str | None,
                          comment: str, steps: list[str], created_at: str) -> tuple[bool, str]:
        """
        모든 비즈니스 로직을 포함하여 새로운 레시피를 생성합니다.
        [수정] 생성 직후, 이 레시피를 기다리던 WaferInformation의 used_recipe_id를 업데이트합니다.
        성공 여부와 메시지를 튜플로 반환합니다. (True, "Success") or (False, "Error message")
        """
        # 1. 유효성 검사 (입력값)
        if not new_code:
            return False, "Recipe name cannot be empty."
        if not steps:
            return False, "At least one step is required."

        # 2. 파라미터 정의 조회 (Recipe.db — 별도 DB이므로 별개 연결)
        param_ids = self.db_manager.get_param_ids_for_chamber(chamber_id, process_name)
        if not param_ids:
            return False, f"No ParameterDefinition found for Chamber '{chamber_id}'(Process: {process_name}."

        # 3. DB에 데이터 생성 (중복 검사 + Base값 조회 + INSERT를 단일 트랜잭션으로)
        wafer_link_warning = ""
        try:
            with self.db_manager.get_connection(proc_db_path) as conn:
                # [Fix #4] 중복 검사를 트랜잭션 내부로 이동 (TOCTOU 방지)
                if self.db_manager.check_recipe_code_exists_in_chamber(
                        proc_db_path, chamber_id, new_code, _conn=conn):
                    return False, f"Recipe code '{new_code}' already exists in Chamber '{chamber_id}'."

                base_map = {}
                if base_code:
                    base_map = self.db_manager.get_values_for_base_recipe(
                        proc_db_path, cls_id, base_code, _conn=conn)
                cur = conn.cursor()
                # a) Recipe 생성
                cur.execute("""
                        INSERT INTO Recipe (classification_id, recipe_code, base_recipe, created_by, created_at, updated_at, comment)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (cls_id, new_code, base_code, USER_ID, created_at, created_at, comment))

                new_recipe_id = cur.lastrowid

                # b) RecipeParameter 일괄 생성 (aux_value 포함 — Ramp 시작값 보존)
                inserts = []
                for step_no, step_name in enumerate(steps, start=1):
                    for pid in param_ids:
                        base_data = base_map.get((step_name, pid))
                        if isinstance(base_data, tuple):
                            val, aux_val = base_data
                        else:
                            val, aux_val = base_data, None
                        inserts.append((new_recipe_id, pid, step_name, step_no, val, aux_val))
                if inserts:
                    cur.executemany("""
                            INSERT INTO RecipeParameter (recipe_id, parameter_id, step, step_no, value, aux_value)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, inserts)

                # --- WaferInformation 연결 로직 ---
                try:
                    cur.execute("""
                            UPDATE WaferInformation
                            SET used_recipe_id = ?
                            WHERE UsedRecipe = ?
                              AND chamber_id = ?
                              AND used_recipe_id IS NULL;
                        """, (new_recipe_id, new_code, chamber_id))
                except sqlite3.OperationalError as e:
                    # [Fix #5] 사용자에게 경고 전달 (Recipe 생성 자체는 성공)
                    wafer_link_warning = f" (WaferInfo link warning: {e})"

            return True, f"Recipe created successfully.{wafer_link_warning}"
        except sqlite3.IntegrityError as e:
            # DUP_RECIPE_CODE_IN_CHAMBER 트리거 등에 의한 오류
            if "DUP_RECIPE_CODE_IN_CHAMBER" in str(e):
                return False, f"Recipe code '{new_code}' already exists in Chamber '{chamber_id}'."
            return False, f"Database integrity error: {e}"
        except Exception as e:
            traceback.print_exc()  # 상세 오류 로깅
            return False, f"An unexpected error occurred: {e}"

    def _generate_new_recipe_codes(self, base_code: str, quantity: int) -> list[str]:
        """기존 이름 생성 로직을 서비스 내부 헬퍼 함수로 분리합니다."""
        return [f"{base_code}_{i}" for i in range(1, quantity + 1)]

    def copy_recipe_from_source(self, src_info: dict, dest_info: dict, quantity: int, ignore_mismatch: bool = False) -> \
    tuple[bool, str]:

        # 1) 소스 레시피 단건 찾기(정확 매칭)
        src_code = src_info["recipe_code"].strip().lower()
        cand = self.db_manager.get_recipes(src_info["proc_db"], src_info["cls_id"], src_info["recipe_code"], None)
        src_row = next((r for r in cand if (r[4] or "").strip().lower() == src_code), None)
        if not src_row:
            return False, f"Source recipe '{src_info['recipe_code']}' not found."
        src_recipe_id = src_row[0]

        # 2) 스텝/파라미터 값
        src_steps = self.db_manager.get_recipe_steps(src_info["proc_db"], src_recipe_id)
        src_param_values = self.db_manager.get_param_values(src_info["proc_db"], [src_recipe_id], {})

        # 3) 파라미터 이름 매핑
        src_id_to_name = self.db_manager.get_param_id_to_name_map(src_info["chamber_id"])
        dest_name_to_id = self.db_manager.get_param_name_to_id_map(dest_info["chamber_id"])

        src_value_map = {}
        # get_param_values: (recipe_id, step, step_no, parameter_id, value, aux_value, id)
        for _, step, step_no, pid, val, aux, _ in src_param_values:  # ★ 언팩 순서 수정
            pname = src_id_to_name.get(pid)
            if pname:
                src_value_map[(step, pname)] = (val, aux)

        # 4) 파라미터 불일치 체크
        missing = sorted({name for _, name in src_value_map.keys() if name not in dest_name_to_id})
        if missing and not ignore_mismatch:
            return False, "PARAMETER_MISMATCH::" + ",".join(missing)

        # 5) 새 코드 생성
        base_code = src_info["recipe_code"]
        new_codes = self._generate_new_recipe_codes(base_code, quantity)

        # 6) 중복 체크 + 쓰기 (단일 트랜잭션 — TOCTOU 방지)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self.db_manager.get_connection(dest_info["proc_db"]) as conn:
                # 중복 체크를 트랜잭션 안에서 수행 (체크↔INSERT 사이 다른 사용자 개입 방지)
                conflicts = [c for c in new_codes if self.db_manager.check_recipe_code_exists_in_chamber(
                    dest_info["proc_db"], dest_info["chamber_id"], c, _conn=conn)]
                if conflicts:
                    return False, "Duplicate codes exist in destination:\n- " + "\n- ".join(conflicts)

                for code in new_codes:  # 루프 시작
                    cur = conn.cursor()
                    # a) Recipe 생성
                    cur.execute("""
                            INSERT INTO Recipe (classification_id, recipe_code, base_recipe, created_by, created_at, updated_at, comment)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (dest_info["cls_id"], code, base_code, USER_ID, now_str, now_str, ""))

                    new_id = cur.lastrowid  # 새로 생성된 ID (예: 125)

                    # b) RecipeParameter 생성 (aux_value 포함 — Ramp 시작값 보존)
                    inserts = []
                    for step_name, step_no in src_steps:  # get_recipe_steps: (step, step_no)
                        for dest_param_name, dest_param_id in dest_name_to_id.items():
                            data = src_value_map.get((step_name, dest_param_name))
                            if isinstance(data, tuple):
                                value, aux_value = data
                            else:
                                value, aux_value = data, None
                            inserts.append((new_id, dest_param_id, step_name, step_no, value, aux_value))
                    if inserts:
                        cur.executemany("""
                                INSERT INTO RecipeParameter (recipe_id, parameter_id, step, step_no, value, aux_value)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, inserts)

                    # --- [★★★ 신규 추가: WaferInformation 연결 로직 (루프 내부) ★★★] ---
                    try:
                        cur.execute("""
                                UPDATE WaferInformation
                                SET used_recipe_id = ?  -- (새 Recipe ID)
                                WHERE UsedRecipe = ?    -- (새 Recipe Code)
                                  AND chamber_id = ?    -- (새 챔버 ID)
                                  AND used_recipe_id IS NULL; -- (아직 연결 안 된 칩만)
                            """, (new_id, code, dest_info["chamber_id"]))
                    except sqlite3.OperationalError as e:
                        # WaferInformation 테이블 등이 없어도 복사 자체가 실패하지 않도록 함
                        print(f"WaferInformation link warning (non-fatal): {e}")
                    # --- [신규 로직 끝] ---

                # 루프 끝
            return True, f"Successfully copied {quantity} recipe(s) from '{base_code}'."
        except Exception as e:
            traceback.print_exc()  # 상세 오류 로깅
            return False, f"An error occurred during copy: {e}"

    def import_recipes_from_csv(self, proc_db_path: str, chamber_id: str, cls_id: int, import_configs: list[dict]) -> \
    tuple[bool, str]:
        """CSV 파일들로부터 레시피를 가져옵니다."""
        # 1. ParameterDefinition 조회
        name_to_pid = self.db_manager.get_param_name_to_id_map(chamber_id)
        if not name_to_pid:
            return False, "No ParameterDefinition found for this chamber."

        # 2. 유효성 검사
        for config in import_configs:
            if self.db_manager.check_recipe_code_exists_in_chamber(proc_db_path, chamber_id, config["recipe_code"]):
                return False, f"Recipe code '{config['recipe_code']}' already exists in this Chamber."

        # 3. 각 파일을 파싱하고 DB에 저장할 데이터로 변환
        all_inserts = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parser = CsvRecipeParser()

        for config in import_configs:
            parsed_data = parser.parse_file(config["path"])
            if not parsed_data:
                return False, f"Failed to parse file: {os.path.basename(config['path'])}"

            combined_values = self._extract_and_combine_values(parsed_data, config["selected_steps"], name_to_pid)
            if not combined_values:
                continue

            recipe_info = {
                "cls_id": cls_id,
                "recipe_code": config["recipe_code"],
                "base_recipe": config["base_recipe"],
                "user_id": USER_ID,
                "timestamp": now_str,
                "comment": "",
                "chamber_id": chamber_id  # [★★★ 신규 추가 ★★★] (DBM 메서드로 전달하기 위해)
            }
            all_inserts.append({"recipe": recipe_info, "params": combined_values})

        if not all_inserts:
            return False, "No valid data to import."

        # 4. DatabaseManager를 통해 일괄 저장
        try:
            total_inserted = self.db_manager.insert_imported_recipes(proc_db_path, all_inserts)
            return True, f"Successfully imported {len(all_inserts)} recipe(s).\nTotal parameter rows inserted: {total_inserted}"
        except Exception as e:
            traceback.print_exc()  # 상세 오류 로깅
            return False, f"Database import failed: {e}"

    # --------------------------------------------------------------------------
    #  CSV 값 추출 및 파싱을 위한 헬퍼 메서드들
    # --------------------------------------------------------------------------
    def _extract_and_combine_values(self, parsed_data: dict, selected_steps: list[dict], name_to_pid: dict) -> dict:
        step_block = parsed_data["step_block"]
        param_block = parsed_data["param_block"]

        step_param_rows = self._rows_from_block_for_params(step_block)
        recipe_param_rows = self._rows_from_block_for_params(param_block)

        # 스키마(컬럼 위치) 감지
        step_cols = [s["abs_col"] for s in selected_steps]
        name_col_s = self._detect_name_col(step_param_rows, name_to_pid)
        unit_col_s = self._detect_unit_col_generic(step_param_rows, exclude_cols={name_col_s})
        name_col_p = self._detect_name_col(recipe_param_rows, name_to_pid)
        unit_col_p = self._detect_unit_col_generic(recipe_param_rows, exclude_cols={name_col_p})
        setting_col = self._detect_setting_value_col(recipe_param_rows,
                                                     exclude_cols={name_col_p, unit_col_p} if unit_col_p else {
                                                         name_col_p})

        # 파라미터 이름 매칭 (행 길이 검증 포함)
        param_rows_s = [(name_to_pid[r[name_col_s].strip()], r) for r in step_param_rows if
                        len(r) > name_col_s and r[name_col_s].strip() in name_to_pid]
        param_rows_p = [(name_to_pid[r[name_col_p].strip()], r) for r in recipe_param_rows if
                        len(r) > name_col_p and r[name_col_p].strip() in name_to_pid]

        # Dynamic Process 로직을 위한 준비
        dynamic_proc_pid = name_to_pid.get("Dynamic Process")
        dynamic_proc_step_pid = name_to_pid.get("Dynamic Process Step")
        selected_steps_map = {s['abs_col']: s for s in selected_steps}
        selected_step_numbers = sorted(selected_steps_map.keys())

        # 최종 값 병합
        combined = {}
        for new_step_no, step_info in enumerate(selected_steps, start=1):
            abs_col = step_info["abs_col"]
            step_ui_info = {"name": step_info["comment"] or step_info["label"], "no": new_step_no}

            # 1. Step Conditions 값 처리 (우선 순위 높음)
            for pid, row_vals in param_rows_s:
                unit = self._detect_unit_from_row_using_column(row_vals, unit_col_s, step_cols)
                raw_val = row_vals[abs_col] if abs_col < len(row_vals) else ""
                val = self._parse_value_with_unit(raw_val, unit)
                key = (pid, step_ui_info['no'])

                # Step Conditions 내부에서 중복 파라미터가 있다면 마지막 값 혹은 병합 처리
                if key not in combined:
                    combined[key] = val
                else:
                    combined[key] = self._merge_values_by_rule(combined[key], val)

            # 2. Recipe Parameters 값 처리 (우선 순위 낮음)
            if setting_col is not None:
                for pid, row_vals in param_rows_p:
                    unit = self._detect_unit_from_row_using_column(row_vals, unit_col_p, [])
                    raw_val = row_vals[setting_col] if setting_col < len(row_vals) else ""
                    val = self._parse_value_with_unit(raw_val, unit)
                    key = (pid, step_ui_info['no'])

                    # [수정된 부분]
                    # Step Conditions에서 이미 값을 가져왔다면(key in combined),
                    # Recipe Parameters의 값은 무시합니다.
                    if key not in combined:
                        combined[key] = val
                    # else: pass  <-- 여기가 핵심: 이미 있으면 덮어쓰거나 합치지 않음

            # Dynamic Process 로직
            if dynamic_proc_pid and dynamic_proc_step_pid:
                repeat_count_key = (dynamic_proc_pid, step_ui_info['no'])
                repeat_count = combined.get(repeat_count_key)

                if isinstance(repeat_count, (int, float)) and repeat_count > 0:
                    start_step_key = (dynamic_proc_step_pid, step_ui_info['no'])
                    start_step_no = combined.get(start_step_key)

                    if isinstance(start_step_no, (int, float)) and start_step_no > 0:
                        # [BUG FIX] 숫자 그대로 저장 (_process_dynamic_process가 표시용 변환 담당)
                        combined[start_step_key] = int(start_step_no)

        # 최종 반환 형식 변환
        final_map = {}
        for new_step_no, step_info in enumerate(selected_steps, start=1):
            step_name = step_info["comment"] or step_info["label"]
            for pid in name_to_pid.values():
                val = combined.get((pid, new_step_no))
                if val is not None:
                    final_map[(pid, step_name, new_step_no)] = val
        return final_map

    def _time_to_seconds(self, s: str):
        s = (s or "").strip()
        if not s or s == "-----": return None
        if ":" not in s:
            try:
                return float(s)
            except ValueError:
                return None
        try:
            parts = [p.strip() for p in s.split(":")]
            secs = float(parts[-1] or 0.0)
            mins = float(parts[-2] or 0.0) if len(parts) >= 2 else 0.0
            hrs = float(parts[-3] or 0.0) if len(parts) >= 3 else 0.0
            return hrs * 3600.0 + mins * 60.0 + secs
        except ValueError:
            return None

    def _to_number(self, s: str):
        if s is None: return None
        t = str(s).strip().replace(",", "")
        if not t or t == "-----": return None
        m = re.search(r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?', t)
        return float(m.group(0)) if m else None

    def _normalize_unit_token(self, s: str) -> str:
        t = (s or "").strip().lower().replace(".", "")
        if t in {"sec", "s", "second", "seconds"}: return "sec"
        if t in {"%", "percent", "percentage"}: return "%"
        return ""

    def _parse_value_with_unit(self, raw_text: str, unit_text: str):
        return self._time_to_seconds(raw_text) if self._normalize_unit_token(unit_text) == "sec" else self._to_number(
            raw_text)

    def _rows_from_block_for_params(self, block: list[list[str]]) -> list[list[str]]:
        skip_labels = {"comment", "step completion cond.", "tolerance condition no."}
        out = []
        for r in block:
            if not r: continue
            key = (r[0] or "").strip().lower()
            if not key or key in skip_labels or (key.startswith("<") and key.endswith(">")):
                continue
            out.append(r)
        return out

    def _detect_name_col(self, rows: list[list[str]], name_to_pid: dict) -> int:
        if not rows: return 0
        names = set(name_to_pid.keys())
        best_col, best_hits = 0, -1
        col_count = max(len(r) for r in rows) if rows else 0
        for c in range(col_count):
            hits = sum(1 for r in rows if c < len(r) and (r[c] or "").strip() in names)
            if hits > best_hits:
                best_hits, best_col = hits, c
        return best_col

    def _detect_unit_col_generic(self, rows: list[list[str]], exclude_cols: set) -> int | None:
        if not rows: return None
        best_col, best_score = None, -1
        col_count = max(len(r) for r in rows) if rows else 0
        for c in range(col_count):
            if c in exclude_cols: continue
            score = sum(1 for r in rows if c < len(r) and self._normalize_unit_token(r[c]))
            if score > best_score:
                best_score, best_col = score, c
        return best_col if best_score > 0 else None

    def _detect_unit_from_row_using_column(
            self,
            row_vals: list[str],
            unit_col: int | None,
            value_cols: list[int]
    ) -> str | None:
        """
        한 행(row)에서 단위를 찾아 반환한다.
        1) unit_col이 주어지면 그 칸을 최우선 사용
        2) 없거나 비어있으면, value_cols(값이 들어있는 열)들을 제외하고
           행 전체를 훑어 단위 토큰(sec, %, …)이 보이면 사용
        3) 없으면 None(=단위 미지정) 반환
        """
        # 1) 지정된 unit_col이 있으면 우선 사용
        if unit_col is not None and 0 <= unit_col < len(row_vals):
            cell = (row_vals[unit_col] or "").strip()
            if self._normalize_unit_token(cell):
                return cell
            # 괄호/문장 안에 단위가 섞여 있을 수도 있으므로 한 번 더 추출 시도
            m = re.search(r'([A-Za-z%]+)', cell)
            if m and self._normalize_unit_token(m.group(1)):
                return m.group(1)

        # 2) fallback: 값 열(value_cols)은 건너뛰고 행을 훑어 단위 토큰 찾기
        for c, cell in enumerate(row_vals):
            if c in (value_cols or []):
                continue
            token = self._normalize_unit_token(cell)
            if token:
                # 원문 그대로 넘겨도 _parse_value_with_unit에서 다시 normalize 함
                return cell

        # 3) 못 찾으면 None
        return None

    def _is_value_like(self, s: str) -> bool:
        t = (s or "").strip()
        return bool(t and t != "-----" and (
                self._to_number(t) is not None or (":" in t and self._time_to_seconds(t) is not None)))

    def _detect_setting_value_col(self, rows: list[list[str]], exclude_cols: set) -> int | None:
        if not rows: return None
        # First, check for explicit header names
        for r in rows[:min(len(rows), 6)]:
            for c, cell in enumerate(r):
                if c in exclude_cols: continue
                if (cell or "").strip().lower() in {"setting value", "settingvalue", "set value"}:
                    return c
        # If not found, guess based on content
        best_col, best_score = None, -1
        col_count = max(len(r) for r in rows) if rows else 0
        for c in range(col_count):
            if c in exclude_cols: continue
            score = sum(1 for r in rows if c < len(r) and self._is_value_like(r[c]))
            if score > best_score:
                best_score, best_col = score, c
        return best_col if best_score > 0 else None

    def _merge_values_by_rule(self, prev_val, new_val):
        if prev_val is None: return new_val
        if new_val is None: return prev_val
        if prev_val == 0 and new_val != 0: return new_val
        if new_val == 0 and prev_val != 0: return prev_val
        return (prev_val + new_val) if (prev_val != 0 and new_val != 0) else 0.0

    def get_column_definitions(self, chamber_id: str, process_name: str) -> list[dict]:
        """[수정] Column Edit Dialog용 데이터 조회"""
        rows = self.db_manager.get_full_param_defs(chamber_id, process_name)
        # rows: (id, name, mapping, hide, order, unit)
        return [{"pid": r[0], "name": r[1], "mapping": r[2], "hide": r[3]} for r in rows]

    def save_column_definitions(self, chamber_id: str, process_name: str, updated_defs: list[dict]) -> tuple[bool, str]:
        """[수정] Column Edit Dialog 저장"""
        # Mapping 중복 검사 (기존 동일)
        mapping_counts = defaultdict(list)
        for d in updated_defs:
            if d["mapping"]:
                mapping_counts[d["mapping"]].append(d["name"])

        duplicates = {m: names for m, names in mapping_counts.items() if len(names) > 1}
        if duplicates:
            conflicts = [f"'{m}': {', '.join(names)}" for m, names in duplicates.items()]
            return False, "Duplicate mapping values are not allowed.\n\nConflicts:\n" + "\n".join(conflicts)

        # Order 부여 (Process 별로 독립적인 순서)
        for i, d in enumerate(updated_defs):
            d["order"] = make_order(i + 1, 0)

        # DB 저장 (Config 테이블에 저장됨)
        self.db_manager.update_param_defs_batch(chamber_id, process_name, updated_defs)
        return True, "Column definitions saved successfully."

    def get_steps_for_base_recipe(self, proc_db_path: str, cls_id: int, base_code: str) -> list[str]:
        """
        주어진 Base 레시피 코드에 해당하는 스텝 이름 목록을 반환합니다.
        """
        if not base_code:
            return []
        rows = self.db_manager.get_recipes(proc_db_path, cls_id, code_filter=base_code, base_filter=None)
        base_row = next((r for r in rows if (r[4] or "").strip().lower() == base_code.strip().lower()), None)
        if not base_row:
            return []
        base_id = base_row[0]
        steps = self.db_manager.get_recipe_steps(proc_db_path, base_id)
        return [name for name, _ in steps]

    def get_available_processes(self, only_with_recipes: bool = False) -> list[str]:
        """
        존재하는 모든 프로세스 DB 목록을 반환합니다.
        [수정] only_with_recipes=True 일 때, 개별 DB를 여는 대신
        Recipe.db의 설정 테이블(ParameterDisplayConfig)을 조회하여 교차 검증합니다. (속도 최적화)
        """
        try:
            # 1. 폴더 내의 실제 파일 목록 스캔 (물리적 존재 확인)
            files = [
                fn[:-3] for fn in os.listdir(self.db_manager.process_db_dir)
                if fn.lower().endswith(".db")
            ]
            file_set = set(files)

            # 2. 필터링 로직
            if only_with_recipes:
                # Recipe.db에 등록된(Sheet가 생성되어 Config가 있는) 프로세스 이름 조회
                configured_set = self.db_manager.get_configured_process_names()

                # [핵심] 교차 검증 (파일도 있고 && 설정도 있는 것)
                # 실제 레시피 Row count를 세지 않더라도, Sheet가 만들어진 프로세스만 뜨므로 충분히 유효함
                valid_processes = list(file_set.intersection(configured_set))
            else:
                # 필터링 없으면 파일 목록 전체 반환
                valid_processes = files

            valid_processes.sort(key=str.lower)
            return valid_processes
        except FileNotFoundError:
            return []

    def get_sheets_for_process(self, process_name: str) -> list[str]:
        """특정 프로세스에 포함된 모든 Sheet 목록을 반환합니다."""
        proc_db_path = self.db_manager.get_process_db_path(process_name)
        if not proc_db_path: return []

        try:
            with self.db_manager.get_connection(proc_db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT sheet FROM RecipClassification ORDER BY sheet")
                return [row[0] for row in cur.fetchall()]
        except Exception:
            return []

    def get_chambers_for_sheet(self, process_name: str, sheet: str) -> list[str]:
        """특정 프로세스의 특정 Sheet에 포함된 Chamber 목록을 반환합니다."""
        proc_db_path = self.db_manager.get_process_db_path(process_name)
        if not proc_db_path or not sheet: return []

        try:
            with self.db_manager.get_connection(proc_db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT chamber_id FROM RecipClassification WHERE sheet=? ORDER BY chamber_id",
                            (sheet,))
                return [row[0] for row in cur.fetchall()]
        except Exception:
            return []

    def get_recipes_for_chamber(self, process_name: str, sheet: str, chamber_id: str) -> list[str]:
        """특정 Process/Sheet/Chamber에 포함된 레시피 코드 목록을 반환합니다."""
        proc_db_path = self.db_manager.get_process_db_path(process_name)
        if not proc_db_path:
            return []
        cls_id = self.db_manager.get_classification_id(proc_db_path, sheet, chamber_id)
        if cls_id is None:
            return []
        rows = self.db_manager.get_recipes(proc_db_path, cls_id, None, None)  # ★ 언패킹 금지
        return [r[4] for r in rows]

    def get_pro_edit_initial_data(self) -> dict:
        # 파라미터 정의가 존재하는(is_active=1) 챔버만 필터링하여 가져옵니다.
        active_chambers_set = self.db_manager.get_chambers_with_definitions()
        # UI 표시를 위해 Set을 정렬된 List로 변환
        sorted_chambers = sorted(list(active_chambers_set))
        return {
            "chambers": sorted_chambers,
            "processes": self.get_available_processes()
        }

    def get_scheme_codes_for_process(self, process_name: str) -> list[str]:
        """특정 프로세스의 SchemeCode 목록을 반환합니다."""
        proc_db_path = self.db_manager.get_process_db_path(process_name)
        if not proc_db_path: return []
        return self.db_manager.get_scheme_codes(proc_db_path)

    def create_new_process(self, process_name: str) -> tuple[bool, str]:
        """새로운 프로세스 DB를 생성합니다."""
        if not process_name:
            return False, "Process name cannot be empty."

        new_db_path = self.db_manager.get_process_db_path(process_name)
        if os.path.exists(new_db_path):
            return False, f"Process '{process_name}' already exists."

        try:
            self.db_manager.create_new_process_db(new_db_path)
            return True, f"Process '{process_name}' created successfully."
        except Exception as e:
            return False, f"Failed to create process DB: {e}"

    def delete_process(self, process_name: str) -> tuple[bool, str]:
        """프로세스 DB 파일을 'removed' 폴더로 이동합니다."""
        if not process_name:
            return False, "No process selected."

        src_path = self.db_manager.get_process_db_path(process_name)
        if not os.path.exists(src_path):
            return False, f"Process DB file for '{process_name}' not found."

        try:
            removed_dir = os.path.join(self.db_manager.process_db_dir, "removed")
            os.makedirs(removed_dir, exist_ok=True)
            shutil.move(src_path, removed_dir)
            return True, f"Process '{process_name}' moved to 'removed' folder."
        except Exception as e:
            return False, f"Failed to delete process: {e}"

    def save_classification(self, process_name: str, data: dict) -> tuple[bool, str]:
        """Classification 메타데이터를 저장합니다."""
        proc_db_path = self.db_manager.get_process_db_path(process_name)
        if not proc_db_path:
            return False, "Selected process not found."

        self.db_manager.upsert_classification(proc_db_path, data)
        return True, "Classification metadata saved."

    def prepare_param_import(self, chamber_id: str, new_definitions: list[dict],
                             process_name: str) -> dict:
        """
        파라미터 정의 Import 1단계: 분류만 수행하고 결과를 dict로 반환한다.
        UI 의존성 없음 (순수 데이터 처리).
        """

        # 1. 현재 DB 파라미터 조회
        current_defs = self.db_manager.get_raw_param_defs(chamber_id)
        db_map = {(d['name'], d['unit']): d for d in current_defs}

        matched_updates = []
        import_only = []

        # 2. 분류 작업
        for new_def in new_definitions:
            key = (new_def['name'], new_def['unit'])
            if key in db_map:
                pid = db_map[key]['pid']
                matched_updates.append((new_def, pid))
                del db_map[key]
            else:
                import_only.append(new_def)

        db_only = list(db_map.values())

        # 3. Silent Mode (매핑할 게 없을 때)
        if not db_only and not import_only:
            self.db_manager.apply_parameter_import_changes(
                chamber_id, matched_updates, [], [], [], process_name
            )
            return {"needs_dialog": False, "message": "All parameters matched perfectly."}

        # 4. Dialog 필요 — UI 레이어에서 처리할 데이터 반환
        return {
            "needs_dialog": True,
            "matched_updates": matched_updates,
            "db_only": db_only,
            "import_only": import_only,
        }

    def apply_param_import_result(self, chamber_id: str, matched_updates: list,
                                  mapped_pairs: list, final_new: list,
                                  final_legacy: list, process_name: str) -> tuple[bool, str]:
        """
        파라미터 정의 Import 2단계: 사용자가 매핑 다이얼로그에서 결정한 결과를 DB에 반영한다.
        UI 의존성 없음.
        """
        self.db_manager.apply_parameter_import_changes(
            chamber_id, matched_updates, mapped_pairs, final_new, final_legacy, process_name
        )
        return True, "Parameter definitions updated with manual mapping."

    # ---- 파서들 ----
    def _parse_defs_from_manual_defs(self, defs: list[dict]) -> list[dict]:
        """
        defs: [{"name": str, "unit": str}, ...]
        - name 공백/빈칸 스킵
        - name 중복 제거(첫 등장만)
        """
        out, seen = [], set()
        for d in defs or []:
            name = (d.get("name", "") or "").strip()
            unit = (d.get("unit", "") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "unit": unit})
        return out

    def _parse_defs_from_manual(self, names: list[str]) -> list[dict]:
        out, seen = [], set()
        for n in names or []:
            s = (n or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append({"name": s, "unit": ""})
        return out

    def _parse_defs_from_csv(self, csv_path: str) -> list[dict]:
        try:
            rows = read_csv_rows(csv_path, 'utf-8')
        except UnicodeDecodeError:
            try:
                rows = read_csv_rows(csv_path, 'cp949')
            except Exception:
                return []
        except Exception:
            return []

        step_block = extract_block(rows, "<Step Conditions>")
        param_block = extract_block(rows, "<Recipe Parameters>")

        exclude = {"Comment", "Step Completion Cond.", "Tolerance Condition No.",
                   "Process Data", "Matcher Relay", "Gas Control"}

        # Unit 컬럼 동적 감지 (known unit tokens으로 판별)
        unit_tokens = {"sec", "s", "sccm", "torr", "mtorr", "w", "degc", "deg",
                       "%", "mm", "v", "a", "pa", "rpm", "l/min"}

        def detect_unit_col(block: list[list[str]]) -> int:
            """블록에서 unit 값이 가장 많이 나타나는 컬럼 인덱스 반환 (0은 name이므로 제외)"""
            if not block: return 1
            best_col, best_score = 1, 0
            col_count = max((len(r) for r in block), default=2)
            for c in range(1, min(col_count, 4)):  # 1~3 컬럼만 탐색
                score = sum(1 for r in block if c < len(r) and r[c].strip().lower() in unit_tokens)
                if score > best_score:
                    best_score, best_col = score, c
            return best_col

        step_unit_col = detect_unit_col(step_block)
        param_unit_col = detect_unit_col(param_block)

        names = set()
        step_defs = []
        for r in step_block:
            if len(r) < 1:
                continue
            name = r[0].strip()
            unit = r[step_unit_col].strip() if step_unit_col < len(r) else ""
            if name and name not in exclude and name not in names:
                names.add(name)
                step_defs.append({"name": name, "unit": unit})

        param_defs = []
        for r in param_block:
            if len(r) < 1:
                continue
            name = r[0].strip()
            unit = r[param_unit_col].strip() if param_unit_col < len(r) else ""
            if unit.lower() == "degc" and name not in names:
                names.add(name)
                param_defs.append({"name": name, "unit": unit})

        return step_defs + param_defs

    def _normalize_defs_for_db(self, chamber_id: str, raw_defs: list[dict]) -> list[dict]:
        defs = []
        for idx, d in enumerate(raw_defs, 1):
            name = (d.get("name", "") or "").strip()
            unit = (d.get("unit", "") or "").strip()
            if not name:
                continue
            order_val = make_order(idx, 0)
            defs.append({
                "chamber_id": chamber_id,
                "name": name,
                "unit": unit,
                "mapping": name,
                "order": order_val
            })
        return defs

    def get_ramp_edit_data(self, proc_db_path: str, recipe_id: int, step_no: int,
                           ramp_param_name: str, process_name: str) -> dict:  # [인자 추가]
        """Ramp 편집 다이얼로그에 필요한 데이터를 조회하고 구성합니다."""

        # 1. Chamber ID 조회
        chamber_id = self.get_chamber_id_for_recipe(proc_db_path, recipe_id)
        if not chamber_id:
            return {}

        # 2. Ramp 유형에 따른 대상 Unit 결정
        target_unit = None
        if "Gas Ramp Times" in ramp_param_name:
            target_unit = "sccm"
        elif "Temp Ramp Times" in ramp_param_name:
            target_unit = "deg"
        if not target_unit: return {}

        # 3. [수정] 파라미터 정의 조회 시 process_name 전달!
        all_defs = self.db_manager.get_full_param_defs(chamber_id, process_name)
        # full_param_defs: (id, name, mapping, hide, order, unit, is_active)
        target_pids = {pid for pid, name, mapping, hide, order, unit, _ in all_defs if
                       unit and target_unit in unit.lower()}

        id_to_mapping = {d[0]: d[2] for d in all_defs}  # pid → mapping (표시용)

        # 4. 값 조회 (기존 동일)
        all_params_in_step = self.db_manager.get_param_values(proc_db_path, [recipe_id], {})
        current_step_params = {p[3]: {'value': p[4], 'aux_value': p[5]} for p in all_params_in_step if p[2] == step_no}

        # 5. 데이터 구성
        # [BUG FIX] 원본 이름(d[1])으로 PID 조회 (mapping과 원본 이름이 다를 수 있음)
        ramp_times_pid = next((d[0] for d in all_defs if d[1] == ramp_param_name), None)

        ramp_times_value = current_step_params.get(ramp_times_pid, {}).get('value', 0)

        target_params_for_dialog = []
        for pid in target_pids:
            name = id_to_mapping.get(pid, f"PID_{pid}")
            value_info = current_step_params.get(pid)
            display_value = ""
            if value_info:
                if value_info.get('aux_value') is not None:
                    start_val = value_info['aux_value']
                    end_val = value_info.get('value')
                    try:
                        start_str = f"{float(start_val):.2f}".rstrip('0').rstrip('.')
                        end_str = f"{float(end_val):.2f}".rstrip('0').rstrip('.')
                        display_value = f"{start_str} > {end_str}"
                    except (TypeError, ValueError):
                        display_value = str(end_val or "")
                else:
                    display_value = str(value_info.get('value', ''))
            target_params_for_dialog.append({'pid': pid, 'name': name, 'value': display_value})

        return {"ramp_times": ramp_times_value, "target_params": target_params_for_dialog}

    def save_ramp_data(self, proc_db_path: str, recipe_id: int, step_no: int,
                       ramp_param_name: str, ramp_data: dict, process_name: str):  # [인자 추가]
        """Ramp 편집 다이얼로그의 결과를 DB에 저장합니다."""

        chamber_id = self.get_chamber_id_for_recipe(proc_db_path, recipe_id)
        if not chamber_id:
            return

        # [수정] process_name을 전달하여 매핑된 이름을 기준으로 PID를 찾습니다.
        all_defs = self.db_manager.get_full_param_defs(chamber_id, process_name)

        # [BUG FIX] 원본 이름(d[1])으로 PID 조회 (mapping과 다를 수 있음)
        ramp_times_pid = next((d[0] for d in all_defs if d[1] == ramp_param_name), None)

        updates = {}
        if ramp_times_pid:
            updates[ramp_times_pid] = {'start': None, 'end': ramp_data.get('ramp_times')}

        for pid, values in ramp_data.get("params", {}).items():
            updates[pid] = values

        if updates:
            self.db_manager.update_ramping_parameter(proc_db_path, recipe_id, step_no, updates)

    def get_chamber_id_for_recipe(self, proc_db_path: str, recipe_id: int) -> str | None:
        """Recipe ID로 Chamber ID를 조회하는 로직을 Manager에 위임합니다."""
        return self.db_manager.get_chamber_id_for_recipe(proc_db_path, recipe_id)

    def get_all_steps_for_recipe(self, proc_db_path: str, recipe_id: int) -> list[dict]:
        """특정 레시피의 모든 스텝 목록을 (번호, 이름) 딕셔너리 리스트로 반환합니다."""
        steps_data = self.db_manager.get_recipe_steps(proc_db_path, recipe_id)
        return [{"step_no": s_no, "step_name": s_name} for s_name, s_no in steps_data]

    def save_dynamic_step_data(self, proc_db_path: str, recipe_id: int, step_no: int, step_name: str,
                               dps_pid: int | None, start_step_no: int | None,
                               *, dp_pid: int | None = None, repeat_count: int | None = None):
        """Dynamic Process Step / Dynamic Process 값을 저장한다."""
        # Dynamic Process Step (시작 스텝 번호 저장)
        if dps_pid is not None and start_step_no is not None:
            # [수정] step_name 전달
            self.db_manager.update_parameter_value(proc_db_path, start_step_no, recipe_id, dps_pid, step_no, step_name)

        # Dynamic Process (반복 횟수 저장)
        if dp_pid is not None and repeat_count is not None:
            # [수정] step_name 전달
            self.db_manager.update_parameter_value(proc_db_path, repeat_count, recipe_id, dp_pid, step_no, step_name)

    def reorder_columns(self, chamber_id: str, process_name: str, ordered_mappings: list[str]):
        """[수정] Drag & Drop 순서 변경 저장"""
        # 현재 설정 조회 (Config 포함)
        defs = self.db_manager.get_full_param_defs(chamber_id, process_name)
        old_orders = {d[2]: d[4] for d in defs}  # mapping -> order

        updates = []
        for new_main_idx, mapping in enumerate(ordered_mappings, 1):
            _, sub_idx = parse_order(old_orders.get(mapping, 0))
            new_order = make_order(new_main_idx, sub_idx)

            # 해당 mapping을 가진 def 찾기
            target_def = next((d for d in defs if d[2] == mapping), None)
            if target_def:
                updates.append({
                    'pid': target_def[0],
                    'mapping': mapping,
                    'hide': 0,  # 드래그 앤 드롭은 보이는 컬럼끼리 하므로 0
                    'order': new_order
                })

        if updates:
            self.db_manager.update_param_defs_batch(chamber_id, process_name, updates)

    # [신규] Sheet 목록 가져오기
    def get_sheets_for_chamber(self, process_name: str, chamber_id: str) -> list[str]:
        path = self.db_manager.get_process_db_path(process_name)
        if not path: return []
        return self.db_manager.get_sheets_by_chamber(path, chamber_id)

    # [신규] Sheet 정보 가져오기
    def get_sheet_details(self, process_name: str, chamber_id: str, sheet: str) -> tuple | None:
        path = self.db_manager.get_process_db_path(process_name)
        if not path: return None
        return self.db_manager.get_sheet_info(path, chamber_id, sheet)

    def create_new_sheet(self, process_name: str, chamber_id: str, sheet: str, scheme: str, date_int: int) -> tuple[
        bool, str]:
        path = self.db_manager.get_process_db_path(process_name)

        if not path: return False, "Process DB not found."

        # 중복 검사
        sheets = self.db_manager.get_sheets_by_chamber(path, chamber_id)
        if sheet in sheets:
            return False, f"Sheet '{sheet}' already exists in this chamber."

        try:
            # 1. Sheet 생성 (기존 로직)
            self.db_manager.insert_classification(path, chamber_id, sheet, scheme, date_int)

            # 2. [신규] 이 Process-Chamber 조합에 대한 파라미터 Config 초기화
            self.db_manager.sync_initial_display_config(chamber_id, process_name)

            return True, "Sheet created successfully."
        except Exception as e:
            return False, f"Failed to create sheet: {e}"

    # [신규] Sheet 삭제
    def delete_sheet(self, process_name: str, chamber_id: str, sheet: str) -> tuple[bool, str, bool]:
        """반환값: (성공여부, 메시지, 확인필요여부)"""
        path = self.db_manager.get_process_db_path(process_name)
        if not path: return False, "Process DB not found.", False

        # 레시피 존재 확인
        has_recipes = self.db_manager.has_recipes_in_sheet(path, chamber_id, sheet)
        if has_recipes:
            return False, "Recipes exist", True  # True means confirmation needed

        try:
            self.db_manager.delete_sheet(path, chamber_id, sheet)
            return True, "Sheet deleted.", False
        except Exception as e:
            return False, f"Error: {e}", False

    # [신규] Sheet 강제 삭제 (확인 후)
    def force_delete_sheet(self, process_name: str, chamber_id: str, sheet: str):
        path = self.db_manager.get_process_db_path(process_name)
        try:
            self.db_manager.delete_sheet(path, chamber_id, sheet)
            return True, "Sheet and related recipes deleted."
        except Exception as e:
            return False, f"Error: {e}"

    def create_transition_steps(self, proc_db_path: str, recipe_id: int,
                                start_step_no: int, end_step_no: int,
                                start_step_name: str, end_step_name: str,
                                num_steps: int) -> tuple[bool, str]:
        """
        두 스텝 사이에 Transition 스텝들을 생성하고 DB에 삽입합니다.
        """

        # 1. 시작 스텝과 끝 스텝의 파라미터 값 조회
        # (recipe_id, step, step_no, parameter_id, value, aux_value, id)
        rows = self.db_manager.get_param_values(proc_db_path, [recipe_id], {})

        start_vals = {}  # {param_id: (value, aux_value)}
        end_vals = {}  # {param_id: (value, aux_value)}

        for r in rows:
            pid = r[3]
            val = r[4]
            aux_val = r[5]
            s_no = r[2]

            if s_no == start_step_no:
                start_vals[pid] = (val, aux_val)
            elif s_no == end_step_no:
                end_vals[pid] = (val, aux_val)

        # 2. 기존 스텝들 뒤로 밀기 (공간 확보)
        # start_step_no 바로 뒤부터 num_steps 만큼 자리가 필요함
        # 주의: 사용자가 1번과 5번을 선택했더라도, 로직은 1번 뒤에 삽입하고
        # 기존 2번(선택된 5번 포함)부터 뒤로 밀리는 구조가 자연스러움.
        # 하지만 요구사항은 "Transition"이므로 A와 B 사이에 채우는 것.
        # 여기서는 "A 바로 뒤에 삽입"하고 "A 뒤에 있던 모든 것(B 포함)을 뒤로 미루는" 방식으로 구현

        try:
            self.db_manager.shift_step_numbers(proc_db_path, recipe_id, start_step_no, num_steps)
        except Exception as e:
            return False, f"Failed to shift steps: {e}"

        # 3. 중간값 계산 및 Insert 데이터 준비
        new_params = []

        # 모든 파라미터 ID 집합 (시작 혹은 끝에 존재하는 모든 파라미터)
        all_pids = set(start_vals.keys()) | set(end_vals.keys())

        for i in range(1, num_steps + 1):
            # 새 스텝 이름 생성 (예: TranAtoB_1Step)
            new_step_name = f"T_{start_step_name}_{end_step_name}_{i}"
            new_step_no = start_step_no + i

            # 비율 (1단계면 0.5, 2단계면 0.33, 0.66 ...)
            # 공식: Start + (End - Start) * (current_step / (total_steps + 1))
            ratio = i / (num_steps + 1)

            for pid in all_pids:
                s_data = start_vals.get(pid, (None, None))
                e_data = end_vals.get(pid, (None, None))
                s_val, s_aux = s_data
                e_val, e_aux = e_data

                final_val = None
                final_aux = None

                # 둘 다 숫자일 경우에만 보간 계산
                if isinstance(s_val, (int, float)) and isinstance(e_val, (int, float)):
                    final_val = s_val + (e_val - s_val) * ratio
                else:
                    final_val = s_val

                # aux_value(Ramp 시작값)도 보간
                if isinstance(s_aux, (int, float)) and isinstance(e_aux, (int, float)):
                    final_aux = s_aux + (e_aux - s_aux) * ratio
                else:
                    final_aux = s_aux

                if final_val is not None:
                    new_params.append((recipe_id, pid, new_step_name, new_step_no, final_val, final_aux))

        # 4. DB 저장
        try:
            self.db_manager.insert_transition_params(proc_db_path, new_params)
            return True, "Transition steps created successfully."
        except Exception as e:
            return False, f"Failed to insert transition parameters: {e}"

    # [신규] Sheet 이름 변경 서비스
    def rename_sheet(self, process_name: str, chamber_id: str, old_sheet: str, new_sheet: str) -> tuple[bool, str]:
        path = self.db_manager.get_process_db_path(process_name)
        if not path:
            return False, "Process DB not found."

        # 1. 유효성 검사
        if not new_sheet:
            return False, "New sheet name cannot be empty."
        if old_sheet == new_sheet:
            return False, "New name is same as old name."

        # 2. 중복 검사
        existing_sheets = self.db_manager.get_sheets_by_chamber(path, chamber_id)
        if new_sheet in existing_sheets:
            return False, f"Sheet '{new_sheet}' already exists in this chamber."

        # 3. 업데이트 실행
        try:
            self.db_manager.rename_classification_sheet(path, chamber_id, old_sheet, new_sheet)
            return True, "Sheet renamed successfully."
        except Exception as e:
            return False, f"Failed to rename sheet: {e}"

class SpecialCaseProcessor:

    def __init__(self, all_param_rows: list[tuple], param_defs: list[tuple], row_stepname_map: dict):
        # param_defs 튜플의 구조: (0:id, 1:name, 2:mapping, 3:hide, 4:order, 5:unit)

        self.id2map = {d[0]: d[2] for d in param_defs}  # id -> mapping
        self.row_stepname_map = row_stepname_map

        # 파라미터 이름(Name)을 기준으로 ID를 찾습니다.
        name2id = {d[1]: d[0] for d in param_defs}
        self.dynamic_proc_step_pid = name2id.get("Dynamic Process Step")

        self.param_values_map = defaultdict(dict)
        for r_id, s_no, p_id, val, aux_val in all_param_rows:
            self.param_values_map[(r_id, s_no)][p_id] = {'value': val, 'aux_value': aux_val}

    def process_step(self, recipe_id: int, step_no: int, step_data: dict) -> dict:
        # copy() 대신 원본 수정이 불가능하면, 얕은 복사 사용.
        # deepcopy는 너무 느리므로 dict() 생성자 사용
        modified_data = dict(step_data)

        modified_data = self._process_ramp_times(recipe_id, step_no, modified_data)
        modified_data = self._process_dynamic_process(recipe_id, step_no, modified_data)
        return modified_data

    @staticmethod
    def _fmt_num(x) -> str:
        """숫자/문자 섞여도 안전하게 '12.34' 같은 포맷으로 반환. 실패 시 빈 문자열."""
        try:
            f = float(x)
        except (TypeError, ValueError):
            return ""
        s = f"{f:.2f}".rstrip('0').rstrip('.')
        return s

    def _process_ramp_times(self, recipe_id: int, step_no: int, data: dict) -> dict:
        """DB에 저장된 aux_value(시작값)를 사용하여 Ramp 표시를 처리합니다."""
        step_params = self.param_values_map.get((recipe_id, step_no), {})

        for pid, values in step_params.items():
            start_val = values.get('aux_value')
            end_val = values.get('value')

            # aux_value(시작값)가 있고, 둘 다 숫자형으로 해석 가능할 때 Ramp 텍스트로
            if start_val is not None and end_val is not None:
                param_mapping = self.id2map.get(pid)
                if param_mapping and param_mapping in data:
                    start_str = self._fmt_num(start_val)
                    end_str = self._fmt_num(end_val)
                    if start_str and end_str:
                        data[param_mapping] = f"{start_str} > {end_str}"
        return data

    def _process_dynamic_process(self, recipe_id: int, step_no: int, data: dict) -> dict:
        """DB에 저장된 시작 스텝 번호를 이름으로 변환하여 표시합니다."""
        if self.dynamic_proc_step_pid is None: return data

        # "Dynamic Process Step" 파라미터의 mapping 이름을 찾음
        dps_mapping = self.id2map.get(self.dynamic_proc_step_pid)
        if not dps_mapping or dps_mapping not in data: return data

        start_step_no = data.get(dps_mapping)
        if not isinstance(start_step_no, (int, float)) or start_step_no <= 0:
            return data
        start_step_no = int(start_step_no)

        # 시작 스텝의 이름을 맵에서 조회
        start_step_name = self.row_stepname_map.get((recipe_id, start_step_no), f"Step {start_step_no}")

        # UI 표시용 텍스트로 변환
        data[dps_mapping] = f"{start_step_name} (Step {start_step_no})"
        return data