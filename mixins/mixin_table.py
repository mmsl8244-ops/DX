"""TableMixin: 테이블 렌더링, 리프레시, 줌, 너비 계산, diff view"""
import traceback
from collections import defaultdict

from PyQt5.QtWidgets import QHeaderView
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont, QFontMetrics, QStandardItemModel

from ui.widgets import FastLeftModel, FastRightModel


class TableMixin:

    # ─── (A) DB 쿼리 헬퍼 ─────────────────────────────────────────
    def _query_param_defs(self, chamber_id):
        """ParameterDefinition 조회를 DB Manager에 위임합니다."""
        return self.db_manager.get_param_defs(chamber_id)

    def _query_distinct_bases(self, cls_id: int) -> list[str]:
        if not getattr(self, "_current_process_db", None) or cls_id is None:
            return []
        with self.db_manager.get_connection(self._current_process_db) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT TRIM(base_recipe) AS b
                  FROM Recipe
                 WHERE classification_id = ?
                   AND TRIM(IFNULL(base_recipe, '')) <> ''
              ORDER BY b COLLATE NOCASE
            """, (cls_id,))
            return [r[0] for r in cur.fetchall()]

    def _query_recipes(self, cls_id, code_filter=None, base_filter=None):
        """Recipe 목록 조회를 DB Manager에 위임합니다."""
        if not self._current_process_db:
            return [], []

        recs = self.db_manager.get_recipes(self._current_process_db, cls_id, code_filter, base_filter)
        codes = [r[4] for r in recs]
        return recs, codes

    def _query_param_rows(self, recipe_ids, id2order):
        """Parameter 값 조회를 DB Manager에 위임합니다."""
        if not self._current_process_db:
            return []
        return self.db_manager.get_param_values(self._current_process_db, recipe_ids, id2order)

    # ─── (D) UI 업데이트 메서드 ─────────────────────────────────────
    def _refresh_recipe_table(self, cls_id, code_filter=None, base_filter=None):
        """
        RecipeService를 통해 데이터를 가져와 UI 렌더링을 준비합니다.
        [최적화] 인덱스 기반 필터/정렬로 튜플 생성 비용 제거
        """
        chamber = self.chamber_id_combo.currentText().strip()
        process_name = self.process_combo.currentText().strip()

        if cls_id is None or not self._current_process_db or not chamber:
            self._render_recipe_table([], [], [], {}, {}, [])
            return

        # 1. DB에서 원본 데이터 가져오기
        result = self.recipe_service.load_recipe_data_for_view(
            self._current_process_db, chamber, cls_id,
            process_name, code_filter, base_filter
        )

        (dyn_cols, param_ids, raw_rows, groups, base_map, base_lookup,
         recipe_codes, _, _, id_to_name_map, dyn_mappings,
         self._dyn_units, dense_right_data) = result

        original_stepnos = result[7]
        original_occidx = result[8]

        # ─── [최적화] 인덱스 기반 필터/정렬 ─────────────────────────
        # 튜플 생성 없이 정수 인덱스만으로 동기화 유지
        self._unfiltered_rows = raw_rows  # 필터 다이얼로그용 원본 보존
        n = len(raw_rows)
        indices = list(range(n))

        # 2. 필터링 (Filtering) — 인덱스만 걸러냄
        if self._active_filters:
            # [신규] 필터 설정 이후 추가된 새 값을 자동으로 allowed에 포함
            filter_universe = getattr(self, '_filter_universe', {})
            for col_idx, allowed_values in self._active_filters.items():
                universe = filter_universe.get(col_idx)
                if universe is None:
                    continue
                for row in raw_rows:
                    if col_idx < len(row[1]):
                        val = str(row[1][col_idx]) if row[1][col_idx] is not None else ""
                        if val not in universe:
                            allowed_values.add(val)
                            universe.add(val)

            try:
                idx_base = self.default_cols.index("Base")
                idx_recipe = self.default_cols.index("Recipe")
            except ValueError:
                idx_base = idx_recipe = -1

            default_cols_len = len(self.default_cols)
            new_indices = []
            for i in indices:
                row = raw_rows[i]
                is_pass = True
                for col_idx, allowed_values in self._active_filters.items():
                    if col_idx == idx_base and idx_base != -1 and idx_recipe != -1:
                        if (str(row[1][idx_base]) not in allowed_values) and \
                                (str(row[1][idx_recipe]) not in allowed_values):
                            is_pass = False
                            break
                    elif col_idx < default_cols_len:
                        if str(row[1][col_idx]) not in allowed_values:
                            is_pass = False
                            break
                if is_pass:
                    new_indices.append(i)
            indices = new_indices

        # 3. 정렬 (Sorting) — 인덱스만 정렬
        if self._sort_col_idx is not None:
            col_idx = self._sort_col_idx
            is_asc = (self._sort_order == Qt.AscendingOrder)
            default_cols_len = len(self.default_cols)

            def sort_key(i):
                row_data = raw_rows[i]
                val = row_data[1][col_idx] if col_idx < default_cols_len else ""
                try:
                    return (0, float(val), row_data[0])
                except (ValueError, TypeError):
                    return (1, str(val).lower(), row_data[0])

            indices.sort(key=sort_key, reverse=not is_asc)

        # 4. 최종 데이터 추출 (인덱스로 한 번에 슬라이싱)
        final_rows = [raw_rows[i] for i in indices]
        final_stepnos = [original_stepnos[i] for i in indices]
        final_occidx = [original_occidx[i] for i in indices]
        final_dense = [dense_right_data[i] for i in indices]

        # 5. 그룹(Row Span) 재계산
        new_groups = []
        if final_rows:
            current_rid = final_rows[0][0]
            count = 1
            start_index = 0

            for i in range(1, len(final_rows)):
                rid = final_rows[i][0]
                if rid == current_rid:
                    count += 1
                else:
                    new_groups.append({"start": start_index, "count": count})
                    current_rid = rid
                    count = 1
                    start_index = i
            new_groups.append({"start": start_index, "count": count})

        # 6. 멤버 변수 업데이트
        self._dyn_cols = dyn_cols
        self._param_ids = param_ids
        self._current_rows = final_rows
        self._groups = new_groups
        self._base_map = base_map
        self._base_lookup = base_lookup
        self._current_recipe_codes = recipe_codes
        self._row_stepnos = final_stepnos
        self._row_occidx = final_occidx
        self._id_to_name_map = id_to_name_map
        self._dyn_mappings = dyn_mappings

        # 7. UI 렌더링
        self._render_recipe_table(
            self._dyn_cols, self._current_rows, self._groups,
            self._base_map, self._base_lookup,
            self._dyn_mappings, final_dense
        )

    def update_recipe_table(self):
        # 2. 현재 스크롤 위치 저장
        v_bar = self.tableLeft.verticalScrollBar()
        h_bar = self.tableView.horizontalScrollBar()

        # 저장할 때 시그널 차단은 필요 없습니다. 값만 읽어옵니다.
        current_v_pos = v_bar.value()
        current_h_pos = h_bar.value()

        try:
            # 3. 데이터 초기화 및 리로드 (여기서 모델이 교체됨)
            self.clear_recipe_table()

            cls_id = self._current_cls_id()
            if cls_id is None:
                self.label.clear()
                self.latest_recipe_name = None
                return  # finally에서 잠금 해제됨

            self._refresh_recipe_table(
                cls_id,
                code_filter=self._current_code_filter,
                base_filter=self._current_base_filter
            )
            self.update_latest_recipe_label()

            # 4. Diff View 및 레이아웃 정리 (즉시 수행)
            self._update_diff_view()
            self.tableLeft.updateGeometry()
            self.tableView.updateGeometry()

        finally:
            # 5. 렌더링 잠금 해제 (이 시점에 Qt가 내부적으로 사이즈 계산을 예약함)
            self.tableLeft.setUpdatesEnabled(True)
            self.tableView.setUpdatesEnabled(True)

            # 6. [★핵심 해결책] 스크롤 복구를 이벤트 루프의 맨 마지막으로 미룸
            # Qt가 테이블 크기 계산을 완전히 끝낸 뒤에 스크롤을 이동시킵니다.
            def restore_scroll_position():
                self.tableLeft.verticalScrollBar().setValue(current_v_pos)
                self.tableView.horizontalScrollBar().setValue(current_h_pos)

            QTimer.singleShot(0, restore_scroll_position)

    # ─── (E) 렌더링 헬퍼 ───────────────────────────────────────────
    def _adjust_left_width(self):
        """
        tableLeft의 고정 너비를
        (세로헤더 폭 + 프레임 폭*2 + 모든 컬럼 폭 + 스크롤바 폭)으로 설정
        """
        hdr = self.tableLeft.horizontalHeader()
        total = self.tableLeft.verticalHeader().width() + self.tableLeft.frameWidth() * 2
        for col in range(hdr.count()):
            total += hdr.sectionSize(col)
        vsb = self.tableLeft.verticalScrollBar()
        if vsb.isVisible():
            total += vsb.width()
        # minimumWidth 대신 fixedWidth로 설정하여
        # 레이아웃이 자동으로 축소하지 못하게 함
        self.tableLeft.setFixedWidth(total)

    def _measure_column_width_sample(self, view, model, col, sample_count=50, padding=5):
        """단일 컬럼 너비 측정 (하위 호환용, _apply_zoom에서 사용)"""
        fm_data = view.fontMetrics()
        bold_font = QFont(view.font())
        bold_font.setBold(True)
        fm_header = QFontMetrics(bold_font)

        header_val = str(model.headerData(col, Qt.Horizontal, Qt.DisplayRole) or "")
        if '\n' in header_val:
            header_w = max(fm_header.horizontalAdvance(line) for line in header_val.split('\n'))
        else:
            header_w = fm_header.horizontalAdvance(header_val)

        final_w = header_w + padding
        rows_to_scan = min(model.rowCount(), sample_count)
        is_left = isinstance(model, FastLeftModel)

        for r in range(rows_to_scan):
            if is_left:
                val = model._data[r][1][col]
            else:
                val = model._data[r][col]

            text = str(val) if val is not None else ""
            if text:
                text_w = fm_data.horizontalAdvance(text)
                if text_w + padding > final_w:
                    final_w = text_w + padding

        return final_w

    def _measure_all_columns_batch(self, view, model, sample_count=50, padding=5):
        """
        [최적화] 전체 컬럼 너비를 단일 패스로 계산.
        unique string 캐싱으로 동일 텍스트의 horizontalAdvance 중복 호출 제거.
        """
        fm_data = view.fontMetrics()
        bold_font = QFont(view.font())
        bold_font.setBold(True)
        fm_header = QFontMetrics(bold_font)

        col_count = model.columnCount()
        widths = [0] * col_count

        # 1. 헤더 너비 (전체 컬럼)
        for c in range(col_count):
            header_val = str(model.headerData(c, Qt.Horizontal, Qt.DisplayRole) or "")
            if '\n' in header_val:
                w = max(fm_header.horizontalAdvance(line) for line in header_val.split('\n'))
            else:
                w = fm_header.horizontalAdvance(header_val)
            widths[c] = w + padding

        # 2. 데이터 너비 — unique string 캐싱
        rows_to_scan = min(model.rowCount(), sample_count)
        is_left = isinstance(model, FastLeftModel)
        data = model._data
        ha = fm_data.horizontalAdvance  # 로컬 참조
        width_cache = {}  # string → pixel width

        if is_left:
            for r in range(rows_to_scan):
                row_vals = data[r][1]
                for c in range(col_count):
                    val = row_vals[c]
                    if val is None:
                        continue
                    text = str(val)
                    if not text:
                        continue
                    # 캐시에서 먼저 조회
                    cached = width_cache.get(text)
                    if cached is None:
                        cached = ha(text) + padding
                        width_cache[text] = cached
                    if cached > widths[c]:
                        widths[c] = cached
        else:
            for r in range(rows_to_scan):
                row_data = data[r]
                for c in range(col_count):
                    val = row_data[c]
                    if val is None:
                        continue
                    text = str(val)
                    if not text:
                        continue
                    cached = width_cache.get(text)
                    if cached is None:
                        cached = ha(text) + padding
                        width_cache[text] = cached
                    if cached > widths[c]:
                        widths[c] = cached

        return widths

    def _render_recipe_table(self, dyn_cols, rows, groups, base_map, base_lookup, dyn_mappings,
                             precomputed_dense=None):

        LAYOUT = self.LAYOUT

        # [Step 0] 헤더 텍스트 줄바꿈 처리 (유저 입력 \n -> 실제 엔터)
        # ---------------------------------------------------------
        # Service에서 넘어온 dyn_cols는 "이름\n(단위)" 형태일 수도 있고,
        # 유저가 Mapping에 "AB\nCD"라고 쓴 것일 수도 있습니다.
        # 문자열 "\n"을 실제 개행문자로 치환합니다.
        final_cols = [str(col).replace("\\n", "\n") for col in dyn_cols]

        # ---------------------------------------------------------
        # [Step 1] 렌더링 잠금
        # ---------------------------------------------------------
        self.tableLeft.setUpdatesEnabled(False)
        self.tableView.setUpdatesEnabled(False)

        if getattr(self, 'left_model', None): self.left_model.blockSignals(True)
        if getattr(self, 'right_model', None): self.right_model.blockSignals(True)

        try:
            self.tableLeft.clearSpans()
            self.tableView.clearSpans()

            actual_rows = len(rows)
            self.tableLeft._real_row_count = actual_rows
            self.tableView._real_row_count = actual_rows

            # --- [데이터 모델 설정] ---
            idx_step = self.default_cols.index("Step")
            recipes_with_hidden_steps = set()

            for r_idx, row_data in enumerate(rows):
                step_val = str(row_data[1][idx_step])
                if step_val.endswith(self.HIDDEN_SUFFIX):
                    recipes_with_hidden_steps.add(row_data[0])

            # [최적화] 서비스에서 미리 생성한 dense_right_data 사용, 없을 때만 생성
            if precomputed_dense and len(precomputed_dense) == len(rows):
                dense_right_data = precomputed_dense
            else:
                def fast_str(v):
                    if v is None: return ""
                    if isinstance(v, float):
                        return str(int(v)) if v.is_integer() else str(v)
                    return str(v)

                dense_right_data = [
                    [fast_str(row_data[2].get(m)) for m in dyn_mappings]
                    for row_data in rows
                ]

            # ---------------------------------------------------------
            # [Step 3] 모델 연결 (final_cols 사용)
            # ---------------------------------------------------------
            # [Fix #1/#8] 이전 모델 정리 (메모리 누수 방지)
            old_left = getattr(self, 'left_model', None)
            old_right = getattr(self, 'right_model', None)

            self.left_model = FastLeftModel(self.default_cols, rows, recipes_with_hidden_steps, self)
            self.left_model.dataEdited.connect(self._on_left_item_edited)
            self.tableLeft.setModel(self.left_model)

            # [수정] dyn_cols 대신 처리된 final_cols를 전달
            self.right_model = FastRightModel(final_cols, dense_right_data, rows, self)
            self.right_model.dataEdited.connect(self._on_param_edited)
            self.tableView.setModel(self.right_model)

            if old_left is not None:
                old_left.deleteLater()
            if old_right is not None:
                old_right.deleteLater()

            # --- [Span 설정] ---
            boundaries = {g["start"] for g in groups if g.get("start", 0) > 0}
            self._row_delegate_left.set_boundaries(boundaries)
            self._row_delegate_right.set_boundaries(boundaries)

            for g in groups:
                if g["count"] > 1:
                    start, count = g["start"], g["count"]
                    for c in range(len(self.default_cols) - 1):
                        self.tableLeft.setSpan(start, c, count, 1)

            # ---------------------------------------------------------
            # [Step 5] 행 높이
            # ---------------------------------------------------------
            self.tableLeft.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
            self.tableView.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)

            current_font = self.tableLeft.font()
            fm_row = QFontMetrics(current_font)
            calc_height = max(fm_row.height() + LAYOUT["row_padding"], LAYOUT["row_min_height"])

            self.tableLeft.verticalHeader().setDefaultSectionSize(calc_height)
            self.tableView.verticalHeader().setDefaultSectionSize(calc_height)

            self.tableLeft.setWordWrap(False)
            self.tableView.setWordWrap(False)

            # ---------------------------------------------------------
            # [Step 6] 컬럼 헤더(Width/Height) 설정 - [줄바꿈 반영]
            # ---------------------------------------------------------
            fm_left = QFontMetrics(self.tableLeft.font())
            bold_font = QFont(self.tableView.font())
            bold_font.setBold(True)
            fm_bold = QFontMetrics(bold_font)

            # 1) 헤더 높이 계산 (줄바꿈이 가장 많은 컬럼 기준)
            h_left = fm_left.height() + LAYOUT["header_padding"]
            max_lines = 1

            # [수정] final_cols(줄바꿈 처리됨)를 기준으로 줄 수 계산
            for col_name in final_cols:
                # count('\n')은 줄바꿈 문자 개수이므로 줄 수는 +1
                max_lines = max(max_lines, str(col_name).count('\n') + 1)

            # 줄 수 * 줄 높이 + 패딩
            h_right = (fm_bold.height() * max_lines) + LAYOUT["header_padding"]

            final_header_height = max(h_left, h_right, LAYOUT["row_min_height"])

            self.tableLeft.horizontalHeader().setFixedHeight(final_header_height)
            self.tableView.horizontalHeader().setFixedHeight(final_header_height)

            # 2) 왼쪽 테이블 컬럼 너비 — 배치 계산
            hdrL = self.tableLeft.horizontalHeader()
            hdrL.setSectionResizeMode(QHeaderView.Fixed)

            idx_date = self.default_cols.index("Date")
            idx_comment = self.default_cols.index("Comment")

            # [최적화] 전체 컬럼 너비를 단일 패스로 계산
            left_widths = self._measure_all_columns_batch(
                self.tableLeft, self.left_model,
                sample_count=50, padding=LAYOUT["col_padding"]
            )

            # Base, Recipe 컬럼: 유니크 값 중 가장 긴 상위 5개만 픽셀 측정 (가시성 + 속도)
            idx_base_col = self.default_cols.index("Base")
            idx_recipe_col = self.default_cols.index("Recipe")
            fm_data = self.tableLeft.fontMetrics()
            ha = fm_data.horizontalAdvance
            pad = LAYOUT["col_padding"]
            data = self.left_model._data
            for special_col in (idx_base_col, idx_recipe_col):
                # 1) 유니크 문자열 수집
                unique_texts = {str(data[r][1][special_col])
                                for r in range(len(data))
                                if data[r][1][special_col] is not None}
                # 2) len() 기준 상위 5개만 추출 (Python len은 O(1))
                top_candidates = sorted(unique_texts, key=len, reverse=True)[:5]
                # 3) 상위 후보만 horizontalAdvance 측정
                max_w = left_widths[special_col]
                for text in top_candidates:
                    tw = ha(text) + pad
                    if tw > max_w:
                        max_w = tw
                left_widths[special_col] = max_w

            # Date, Comment 컬럼은 고정 너비 오버라이드
            date_w = fm_left.horizontalAdvance("999999") + LAYOUT["date_padding"]
            left_widths[idx_date] = date_w
            left_widths[idx_comment] = LAYOUT["comment_width"]

            for c in range(len(self.default_cols)):
                hdrL.resizeSection(c, max(left_widths[c], LAYOUT["col_min_width"]))

            self._adjust_left_width()

            # 3) 오른쪽 테이블 컬럼 너비 (파라미터)
            hdrR = self.tableView.horizontalHeader()
            hdrR.setSectionResizeMode(QHeaderView.Interactive)

            for c, col_name in enumerate(final_cols):
                lines = str(col_name).split('\n')
                text_w = max(fm_bold.horizontalAdvance(line) for line in lines) if lines else 0
                final_right_w = max(text_w + LAYOUT["right_header_pad"], LAYOUT["right_min_width"])
                hdrR.resizeSection(c, final_right_w)

            # ---------------------------------------------------------
            # [Step 7] 숨김 처리
            # ---------------------------------------------------------
            if self._hidden_recipe_ids:
                for i, row_data in enumerate(rows):
                    if row_data[0] in self._hidden_recipe_ids:
                        self.tableLeft.setRowHidden(i, True)
                        self.tableView.setRowHidden(i, True)

            for i, row_data in enumerate(rows):
                step_val = str(row_data[1][idx_step])
                if step_val.endswith(self.HIDDEN_SUFFIX):
                    rid = row_data[0]
                    if rid not in self._temp_shown_recipes:
                        self.tableLeft.setRowHidden(i, True)
                        self.tableView.setRowHidden(i, True)

        except Exception as e:
            print(f"Render Error: {e}")
            traceback.print_exc()

        finally:
            if getattr(self, 'left_model', None): self.left_model.blockSignals(False)
            if getattr(self, 'right_model', None): self.right_model.blockSignals(False)

            self.tableLeft.setUpdatesEnabled(True)
            self.tableView.setUpdatesEnabled(True)

            try:
                self.tableLeft.selectionModel().selectionChanged.disconnect()
                self.tableView.selectionModel().selectionChanged.disconnect()
            except (TypeError, RuntimeError):
                pass

            self.tableLeft.selectionModel().selectionChanged.connect(self._on_selection_changed)
            self.tableLeft.selectionModel().selectionChanged.connect(
                lambda s, d: self._on_selection_sync(is_left=True, selected=s, deselected=d))

            self.tableView.selectionModel().selectionChanged.connect(self._on_selection_changed)
            self.tableView.selectionModel().selectionChanged.connect(
                lambda s, d: self._on_selection_sync(is_left=False, selected=s, deselected=d))

            self.tableLeft.viewport().update()
            self.tableView.viewport().update()

    def _apply_zoom(self, delta_step):
        """
        [Ultra Fast] 폰트 크기 조절 및 레이아웃 즉시 재계산 (Scan 방식 제거)
        _render_recipe_table의 LAYOUT 설정과 동기화됨.
        """
        # 1. 폰트 크기 계산 (최소 8px ~ 최대 40px)
        new_size = self.current_font_size + delta_step
        if new_size < 8 or new_size > 40:
            return

        self.current_font_size = new_size
        new_font = QFont()
        new_font.setPixelSize(new_size)

        LAYOUT = self.LAYOUT

        # -----------------------------------------------------
        # 2. 폰트 적용
        # -----------------------------------------------------
        self.tableLeft.setFont(new_font)
        self.tableView.setFont(new_font)

        # 헤더 폰트도 명시적 적용
        self.tableLeft.horizontalHeader().setFont(new_font)
        self.tableView.horizontalHeader().setFont(new_font)

        # -----------------------------------------------------
        # 3. 폰트 메트릭 준비
        # -----------------------------------------------------
        fm_left = QFontMetrics(new_font)
        bold_font = QFont(new_font);
        bold_font.setBold(True)
        fm_bold = QFontMetrics(bold_font)

        # -----------------------------------------------------
        # 4. 행 높이 (Row Height) 재계산
        # -----------------------------------------------------
        # 폰트 높이 + 설정된 여백
        new_row_height = max(fm_left.height() + LAYOUT["row_padding"], LAYOUT["row_min_height"])

        self.tableLeft.verticalHeader().setDefaultSectionSize(new_row_height)
        self.tableView.verticalHeader().setDefaultSectionSize(new_row_height)

        # -----------------------------------------------------
        # 5. 헤더 높이 재계산 (수학적 계산)
        # -----------------------------------------------------
        h_left = fm_left.height() + LAYOUT["header_padding"]

        # 오른쪽 테이블 헤더의 최대 줄 수 계산
        max_lines = 1
        modelR = self.tableView.model()
        if modelR:
            for c in range(modelR.columnCount()):
                # 헤더 텍스트를 가져와서 줄바꿈 개수 확인
                header_text = str(modelR.headerData(c, Qt.Horizontal))
                max_lines = max(max_lines, header_text.count('\n') + 1)

        h_right = (fm_bold.height() * max_lines) + LAYOUT["header_padding"]
        final_header_height = max(h_left, h_right, LAYOUT["row_min_height"])

        self.tableLeft.horizontalHeader().setFixedHeight(final_header_height)
        self.tableView.horizontalHeader().setFixedHeight(final_header_height)

        # -----------------------------------------------------
        # 6. 왼쪽 테이블 컬럼 너비 재계산
        # -----------------------------------------------------
        hdrL = self.tableLeft.horizontalHeader()
        idx_date = self.default_cols.index("Date")
        idx_comment = self.default_cols.index("Comment")

        # Date: 6자리 숫자 기준 + 패딩
        date_w = fm_left.horizontalAdvance("999999") + LAYOUT["date_padding"]
        hdrL.resizeSection(idx_date, date_w)

        # Comment: 폰트 크기 비율에 맞춰 스케일링 (기준 12px)
        scale_ratio = new_size / 12.0
        comment_w = int(LAYOUT["comment_width"] * scale_ratio)
        hdrL.resizeSection(idx_comment, comment_w)

        # Recipe, Step 등: 샘플링 기반 너비 계산
        if self.left_model:
            idx_base_col = self.default_cols.index("Base")
            idx_recipe_col = self.default_cols.index("Recipe")

            for c in range(len(self.default_cols)):
                if c in (idx_date, idx_comment): continue
                # _render_recipe_table과 동일한 샘플링 함수 사용 (패딩 값 일치)
                w = self._measure_column_width_sample(
                    self.tableLeft, self.left_model, c,
                    sample_count=50,
                    padding=LAYOUT["col_padding"]
                )
                hdrL.resizeSection(c, max(w, LAYOUT["col_min_width"]))

            # Base, Recipe 컬럼: 유니크 값 중 상위 5개 픽셀 측정 (가시성 보장)
            fm_zoom = QFontMetrics(new_font)
            ha_zoom = fm_zoom.horizontalAdvance
            pad_zoom = LAYOUT["col_padding"]
            data = self.left_model._data
            for special_col in (idx_base_col, idx_recipe_col):
                unique_texts = {str(data[r][1][special_col])
                                for r in range(len(data))
                                if data[r][1][special_col] is not None}
                top_candidates = sorted(unique_texts, key=len, reverse=True)[:5]
                max_w = hdrL.sectionSize(special_col)
                for text in top_candidates:
                    tw = ha_zoom(text) + pad_zoom
                    if tw > max_w:
                        max_w = tw
                hdrL.resizeSection(special_col, max(max_w, LAYOUT["col_min_width"]))

        # 왼쪽 테이블 전체 폭 맞춤
        self._adjust_left_width()

        # -----------------------------------------------------
        # 7. 오른쪽 테이블 컬럼 너비 재계산
        # -----------------------------------------------------
        hdrR = self.tableView.horizontalHeader()
        if modelR:
            for c in range(modelR.columnCount()):
                header_text = str(modelR.headerData(c, Qt.Horizontal))
                lines = header_text.split('\n')
                if lines:
                    text_w = max(fm_bold.horizontalAdvance(line) for line in lines)
                else:
                    text_w = 0

                # 텍스트 너비 + 패딩 vs 최소 너비
                final_w = max(text_w + LAYOUT["right_header_pad"], LAYOUT["right_min_width"])
                hdrR.resizeSection(c, final_w)

    def _update_diff_view(self):
        """
        [UI Only] Diff View 체크박스 상태에 따라,
        '현재 화면에 보이는 행'들의 값이 모두 동일한 컬럼을 숨깁니다.
        (필터링된 행은 _current_rows에 없고, Hide된 행은 _hidden_recipe_ids로 체크)
        """
        # 필수 데이터 확인
        if not hasattr(self, 'diff_view_chk') or not hasattr(self, '_dyn_mappings'):
            return
        if not hasattr(self, '_row_occidx') or not hasattr(self, '_base_map') or not hasattr(self, '_base_lookup'):
            return

        if not self.tableView.model() or not self._current_rows:
            return

        is_diff_mode = self.diff_view_chk.isChecked()

        # 오른쪽 테이블의 각 컬럼(파라미터) 순회
        for col_idx, mapping in enumerate(self._dyn_mappings):

            # 1. 체크박스 꺼짐: 무조건 보이기 (초기화)
            if not is_diff_mode:
                self.tableView.setColumnHidden(col_idx, False)
                continue

            # 2. 체크박스 켜짐: "Base와 다른 값이 있는가?" 검사
            has_diff_from_base = False

            for row_idx, row_data in enumerate(self._current_rows):
                # A. 화면에서 숨겨진 행(Hide/Filter)은 검사 제외
                if self.tableLeft.isRowHidden(row_idx) or self.tableView.isRowHidden(row_idx):
                    continue

                # B. 데이터 식별 정보 추출
                rid = row_data[0]  # Recipe ID

                # Base가 없으면 비교 불가 -> 차이 없음으로 간주하고 넘어감
                base_id = self._base_map.get(rid)
                if not base_id:
                    continue

                # Bounds check for _row_occidx
                if row_idx >= len(self._row_occidx):
                    continue

                # Mapping Key로 값 가져오기
                step_name = row_data[1][4]  # Step Name
                occ_idx = self._row_occidx[row_idx]  # Occurrence Index

                child_val = row_data[2].get(mapping)
                base_val = self._base_lookup.get((base_id, step_name, occ_idx, mapping))

                # C. 값 비교 (Delegate의 노란색 판정 로직과 동일)
                is_different = False

                if base_val is not None and child_val is not None:
                    try:
                        # 숫자 비교 (오차 범위 1e-9)
                        if abs(float(base_val) - float(child_val)) > 1e-9:
                            is_different = True
                    except ValueError:
                        # 문자열 비교
                        if str(base_val) != str(child_val):
                            is_different = True
                elif (base_val is None and child_val is not None) or \
                        (base_val is not None and child_val is None):
                    # 둘 중 하나만 None이면 다름
                    is_different = True

                # D. 하나라도 다른게 발견되면, 이 컬럼은 '의미 있는 컬럼'임
                if is_different:
                    has_diff_from_base = True
                    break  # 더 이상 이 컬럼의 다른 행을 볼 필요 없음

            # 하나라도 노란색(차이)이 있으면 보이고(False), 없으면 숨김(True)
            self.tableView.setColumnHidden(col_idx, not has_diff_from_base)