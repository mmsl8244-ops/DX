"""SelectionMixin: 선택 동기화, 하이라이트, 검색, 헤더 클릭/필터"""
from PyQt5.QtCore import Qt, QItemSelection, QItemSelectionModel

from ui.dialogs_recipe import ExcelFilterDialog


class SelectionMixin:

    def _on_header_clicked(self, logicalIndex):
        """헤더 좌클릭: 정렬"""
        # Step 컬럼은 정렬 비활성화
        try:
            if logicalIndex == self.default_cols.index("Step"):
                return
        except ValueError:
            pass

        # 현재 정렬 상태 확인 후 토글
        if self._sort_col_idx == logicalIndex:
            if self._sort_order == Qt.AscendingOrder:
                self._sort_order = Qt.DescendingOrder
            else:
                self._sort_order = Qt.AscendingOrder
        else:
            self._sort_col_idx = logicalIndex
            self._sort_order = Qt.AscendingOrder

        # 테이블 다시 그리기 (이때 정렬 로직 수행됨)
        self.update_recipe_table()

    def _on_header_right_clicked(self, logicalIndex, globalPos):
        """헤더 우클릭: 필터 다이얼로그 (수정됨)"""
        # Step 컬럼은 필터 비활성화
        try:
            if logicalIndex == self.default_cols.index("Step"):
                return
        except ValueError:
            pass

        # [수정] 필터링 전 원본 데이터에서 고유값 수집 (필터 적용 후에도 전체 항목 표시)
        unique_values = set()
        unfiltered = getattr(self, '_unfiltered_rows', None)
        if unfiltered:
            for row in unfiltered:
                if logicalIndex < len(row[1]):
                    unique_values.add(row[1][logicalIndex])
        else:
            model = self.tableLeft.model()
            if model:
                for r in range(model.rowCount()):
                    idx = model.index(r, logicalIndex)
                    unique_values.add(model.data(idx, Qt.DisplayRole))

        # 현재 적용된 필터 가져오기
        current_filter = self._active_filters.get(logicalIndex)
        col_name = self.default_cols[logicalIndex]

        dlg = ExcelFilterDialog(self, col_name, unique_values, current_filter)
        if dlg.exec_():
            selected = dlg.get_selected_values()

            if selected is None:
                # 필터 해제
                if logicalIndex in self._active_filters:
                    del self._active_filters[logicalIndex]
                if logicalIndex in self._filter_universe:
                    del self._filter_universe[logicalIndex]
            else:
                # 필터 적용 + 설정 시점의 전체 값 저장 (신규 값 자동 포함용)
                self._active_filters[logicalIndex] = selected
                self._filter_universe[logicalIndex] = set(
                    str(v) if v is not None else "" for v in unique_values
                )

            # 테이블 다시 그리기
            self.update_recipe_table()

    def _on_selection_sync(self, is_left, selected, deselected):
        """
        [수정] 테이블 간 선택 영역 동기화 로직
        - Left 선택 시 -> Right 테이블의 같은 행 선택 (동기화)
        - Right 선택 시 -> Left 테이블 선택 '해제' (Clear)
        """
        # 무한 루프 방지
        if getattr(self, "_is_syncing_selection", False):
            return

        self._is_syncing_selection = True

        try:
            # -------------------------------------------------------
            # CASE A: 왼쪽 테이블(Step)을 선택한 경우
            # -------------------------------------------------------
            if is_left:
                sender_view = self.tableLeft
                target_view = self.tableView

                # 1. Recipe 컬럼 자동 확장 로직 (기존 유지)
                selection_model = sender_view.selectionModel()
                indexes = selection_model.selectedIndexes()

                try:
                    idx_recipe = self.default_cols.index("Recipe")
                except ValueError:
                    idx_recipe = -1

                selected_recipes = set()
                needs_expansion = False

                if idx_recipe != -1:
                    for idx in indexes:
                        if idx.column() == idx_recipe:
                            r = idx.row()
                            if r < len(self._current_rows):
                                rid = self._current_rows[r][0]
                                selected_recipes.add(rid)
                                needs_expansion = True

                if needs_expansion and selected_recipes:
                    new_selection = QItemSelection()
                    model = sender_view.model()
                    cols = model.columnCount()

                    for r, row_data in enumerate(self._current_rows):
                        if row_data[0] in selected_recipes:
                            top_left = model.index(r, 0)
                            bottom_right = model.index(r, cols - 1)
                            new_selection.select(top_left, bottom_right)

                    selection_model.select(new_selection, QItemSelectionModel.Select | QItemSelectionModel.Rows)

                # 2. 오른쪽 테이블(Right) 동기화 (기존 유지)
                current_selection = sender_view.selectionModel().selection()
                selected_rows = []

                for range_ in current_selection:
                    selected_rows.extend(range(range_.top(), range_.bottom() + 1))

                target_model = target_view.model()
                if not target_model: return
                target_selection = QItemSelection()
                target_cols = target_model.columnCount()

                if target_cols > 0:
                    for r in selected_rows:
                        if r < target_model.rowCount():
                            top_left = target_model.index(r, 0)
                            bottom_right = target_model.index(r, target_cols - 1)
                            target_selection.select(top_left, bottom_right)

                target_view.selectionModel().blockSignals(True)
                target_view.selectionModel().select(
                    target_selection,
                    QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows
                )
                target_view.selectionModel().blockSignals(False)

                # 3. 하이라이트 갱신 (왼쪽 기준이므로 수행)
                self._update_highlight_from_selection()

            # -------------------------------------------------------
            # CASE B: 오른쪽 테이블(Para)을 선택한 경우
            # -------------------------------------------------------
            else:
                # [핵심 수정] 오른쪽을 선택하면, 왼쪽 테이블의 선택을 모두 해제합니다.
                # 시그널을 막고 해제해야 다시 왼쪽 이벤트가 발생하여 무한루프 도는 것을 방지합니다.
                self.tableLeft.selectionModel().blockSignals(True)
                self.tableLeft.clearSelection()
                self.tableLeft.selectionModel().blockSignals(False)

                # 하이라이트(Diff View)도 선택이 없으므로 초기화
                self._highlight_target_recipe_code = None

        finally:
            self._is_syncing_selection = False

            # 잔상 제거를 위해 양쪽 뷰포트 강제 갱신
            self.tableLeft.viewport().repaint()
            self.tableView.viewport().repaint()

    def _update_highlight_from_selection(self):
        """
        [기존 _on_selection_changed 로직 분리]
        선택된 행의 Base Recipe 정보를 기반으로 하이라이트 타겟 업데이트
        """
        self._highlight_target_recipe_code = None

        # 왼쪽 테이블 기준 (동기화되었으므로 왼쪽만 봐도 됨)
        indexes = self.tableLeft.selectionModel().selectedIndexes()

        if indexes:
            row = indexes[0].row()
            if row < len(self._current_rows):
                try:
                    idx_base = self.default_cols.index("Base")
                    base_vals = self._current_rows[row][1]
                    base_recipe_name = base_vals[idx_base]

                    if base_recipe_name and str(base_recipe_name).strip():
                        self._highlight_target_recipe_code = str(base_recipe_name).strip()
                except ValueError:
                    pass

        # 뷰포트 갱신 (배경색 다시 그리기)
        self.tableLeft.viewport().update()

    def _on_selection_changed(self, selected, deselected):
        """
        행 선택이 변경될 때 호출됩니다.
        선택된 행(왼쪽 또는 오른쪽)의 Recipe 정보를 바탕으로 하이라이트를 갱신합니다.
        """

        self._highlight_target_recipe_code = None

        # 1. 왼쪽 테이블에서 선택된 행 확인
        indexes = self.tableLeft.selectionModel().selectedIndexes()

        # 2. 왼쪽이 비어있으면 오른쪽 테이블 확인 (상호 배타적이므로 둘 중 하나만 선택됨)
        if not indexes:
            indexes = self.tableView.selectionModel().selectedIndexes()

        if indexes:
            # 첫 번째 선택된 셀의 행 번호
            row = indexes[0].row()

            # 데이터 범위 체크
            if row < len(self._current_rows):
                # _current_rows[row][1] : base_vals -> [date, comment, base, recipe, step]
                # 인덱스 2가 Base Recipe Code
                base_vals = self._current_rows[row][1]
                idx_base = self.default_cols.index("Base")  # 보통 2

                base_recipe_name = base_vals[idx_base]

                # Base Recipe가 존재하면 타겟으로 설정
                if base_recipe_name and str(base_recipe_name).strip():
                    self._highlight_target_recipe_code = str(base_recipe_name).strip()

        # 3. 테이블 다시 그리기 요청 (배경색 갱신)
        self.tableLeft.viewport().update()