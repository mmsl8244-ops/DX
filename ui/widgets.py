from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5 import QtCore


class TableViewWithCopyPaste(QTableView):
    modelChanged = pyqtSignal()
    itemsPasted = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._groups = []

    def setModel(self, model):
        super().setModel(model)
        self.modelChanged.emit()

    def inEditable(self):
        return self.editTriggers() != QAbstractItemView.NoEditTriggers

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy):
            self.copy()
        elif event.matches(QKeySequence.Paste) and self.inEditable():
            self.paste()
        else:
            super().keyPressEvent(event)

    def copy(self):
        if not self.model() or not self.selectionModel():
            return
        indexes = self.selectionModel().selectedIndexes()
        if not indexes:
            return
        rows = sorted({idx.row() for idx in indexes})
        cols = sorted({idx.column() for idx in indexes})
        r0, r1 = rows[0], rows[-1]
        c0, c1 = cols[0], cols[-1]

        lines = []
        for r in range(r0, r1 + 1):
            cells = []
            for c in range(c0, c1 + 1):
                idx = self.model().index(r, c)
                val = self.model().data(idx, Qt.DisplayRole)
                cells.append('' if val is None else str(val))
            lines.append('\t'.join(cells))
        QApplication.clipboard().setText('\n'.join(lines))

    def paste(self):
        if not self.model() or not self.selectionModel():
            return
        text = QApplication.clipboard().text()
        rows_text = [row for row in text.split('\n') if row]
        if not rows_text:
            return

        indexes = self.selectionModel().selectedIndexes()
        if not indexes:
            return

        model = self.model()

        # ★ 중요: 붙여넣기 동안 모델의 시그널을 차단하여 _on_param_edited 호출 방지
        model.blockSignals(True)

        try:
            top_left = min(indexes, key=lambda i: (i.row(), i.column()))
            start_row = top_left.row()
            start_col = top_left.column()

            pasted_changes = []  # (row, col, text_value)

            for i, row_text in enumerate(rows_text):
                cols_text = row_text.split('\t')
                for j, cell_text in enumerate(cols_text):
                    r = start_row + i
                    c = start_col + j

                    idx = model.index(r, c)
                    if idx.isValid():
                        # 모델에 데이터 설정
                        model.setData(idx, cell_text, Qt.EditRole)
                        # 변경 사항 기록
                        pasted_changes.append((r, c, cell_text))

        finally:
            model.blockSignals(False)
            # 강제 갱신
            self.viewport().update()

        # 시그널 차단 해제 후 커스텀 시그널 발송
        if pasted_changes:
            self.itemsPasted.emit(pasted_changes)

    def paintEvent(self, event):
        super().paintEvent(event)
        real = getattr(self, "_real_row_count", None)
        if real is None:
            return
        model = self.model()
        if model is None:
            return
        rows = model.rowCount()
        if real >= rows:
            return

        painter = QPainter(self.viewport())
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        base_brush = self.palette().base()
        cols = model.columnCount()

        for row in range(real, rows):
            for col in range(cols):
                idx = model.index(row, col)
                rect = self.visualRect(idx)
                painter.fillRect(rect, base_brush)
        painter.end()


class DraggableTableWidget(QTableWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # 행 선택, Drag&amp;Drop 허용 설정
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.setDefaultDropAction(Qt.MoveAction)

    def dropEvent(self, event):
        # 드래그된 원본 행
        src_row = self.currentRow()
        # 드롭 위치의 행
        dest_index = self.indexAt(event.pos())
        dest_row = dest_index.row()
        if dest_row < 0 or dest_row == src_row:
            return

        # 원본 행의 모든 셀 아이템을 꺼내둔다
        items = [self.takeItem(src_row, col) for col in range(self.columnCount())]

        # 목적지에 새 행 삽입
        self.insertRow(dest_row)
        # 꺼내둔 아이템을 새 행에 채운다
        for col, item in enumerate(items):
            self.setItem(dest_row, col, item)

        # 원본 행 삭제 (삽입 위치에 따라 인덱스 보정)
        if dest_row > src_row:
            self.removeRow(src_row)
        else:
            self.removeRow(src_row + 1)

        event.accept()

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy):
            self.copy()
        elif event.matches(QKeySequence.Paste):
            self.paste()
        else:
            super().keyPressEvent(event)

    def copy(self):
        selected_ranges = self.selectedRanges()
        if not selected_ranges:
            return

        text = ""
        # 행 단위 정렬을 위해 모음
        rows = sorted(list(set(i.row() for i in self.selectedItems())))
        if not rows: return
        cols = sorted(list(set(i.column() for i in self.selectedItems())))

        if not cols: return

        min_r, max_r = min(rows), max(rows)
        min_c, max_c = min(cols), max(cols)

        for r in range(min_r, max_r + 1):
            row_text = []
            for c in range(min_c, max_c + 1):
                item = self.item(r, c)
                # 아이템이 있고 선택된 상태일 때만 텍스트 가져옴 (혹은 범위 내 전체)
                # 엑셀처럼 동작하려면 범위 내 빈 셀도 탭으로 채워야 함
                if item:
                    row_text.append(item.text())
                else:
                    row_text.append("")
            text += "\t".join(row_text) + "\n"

        QApplication.clipboard().setText(text)

    def paste(self):
        text = QApplication.clipboard().text()
        if not text:
            return

        rows = text.split('\n')
        if rows and not rows[-1]:
            rows.pop()

        current_row = self.currentRow()
        current_col = self.currentColumn()

        if current_row < 0 or current_col < 0:
            return

        for r_idx, row_data in enumerate(rows):
            if current_row + r_idx >= self.rowCount():
                break

            cells = row_data.split('\t')
            for c_idx, cell_data in enumerate(cells):
                if current_col + c_idx >= self.columnCount():
                    break

                target_row = current_row + r_idx
                target_col = current_col + c_idx

                item = self.item(target_row, target_col)
                if not item:
                    item = QTableWidgetItem()
                    self.setItem(target_row, target_col, item)

                # [중요] 읽기 전용 컬럼(Parameter Name 등)은 덮어쓰지 않도록 보호
                if item.flags() & Qt.ItemIsEditable:
                    item.setText(cell_data)


class FilterHeaderView(QHeaderView):
    # (Index, GlobalPos)
    rightClicked = pyqtSignal(int, QPoint)

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setSectionsClickable(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            idx = self.logicalIndexAt(event.pos())
            if idx >= 0:
                self.rightClicked.emit(idx, event.globalPos())
            # super() 호출 안 함 → 우클릭 시 컬럼 전체 선택 방지
        elif event.button() == Qt.LeftButton:
            idx = self.logicalIndexAt(event.pos())
            if idx >= 0:
                self.sectionClicked.emit(idx)
            # super() 호출 안 함 → 좌클릭 시 컬럼 전체 선택 방지 (정렬만 수행)
        else:
            super().mousePressEvent(event)


class IgnoreWheelFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel:
            return True  # wheel 이벤트 무시
        return super().eventFilter(obj, event)


class UnifiedDelegate(QStyledItemDelegate):
    """
    테이블의 모든 커스텀 그리기와 편집기 생성을 담당하는 통합 Delegate.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_window = parent
        self._boundaries = set()

    def set_boundaries(self, rows: set[int]):
        self._boundaries = set(rows)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QtCore.QModelIndex):
        """Hover 효과와 Recipe 경계선을 그립니다."""
        view = option.widget
        painter.save()

        # 1. 하이라이트 여부 판단
        is_highlighted = False

        if view is self.parent_window.tableLeft:
            try:
                idx_recipe = self.parent_window.default_cols.index("Recipe")
                if index.column() == idx_recipe:
                    current_text = index.data(Qt.DisplayRole)
                    target_name = getattr(self.parent_window, '_highlight_target_recipe_code', None)
                    val_str = str(current_text).strip() if current_text else ""
                    tgt_str = str(target_name).strip() if target_name else ""
                    if tgt_str and val_str == tgt_str:
                        is_highlighted = True
            except Exception:
                pass

        elif view is self.parent_window.tableView:
            try:
                row = index.row()
                col = index.column()
                win = self.parent_window
                if row < len(win._current_rows) and col < len(win._dyn_mappings) and row < len(win._row_occidx):
                    rid = win._current_rows[row][0]
                    step = win._current_rows[row][1][4]
                    occ_idx = win._row_occidx[row]
                    mapping_key = win._dyn_mappings[col]
                    current_val = win._current_rows[row][2].get(mapping_key)
                    base_id = win._base_map.get(rid)
                    if base_id:
                        base_val = win._base_lookup.get((base_id, step, occ_idx, mapping_key))
                        if base_val is not None and current_val is not None:
                            try:
                                if abs(float(base_val) - float(current_val)) > 1e-9:
                                    is_highlighted = True
                            except ValueError:
                                if str(base_val) != str(current_val):
                                    is_highlighted = True
                        elif (base_val is None and current_val is not None) or \
                                (base_val is not None and current_val is None):
                            is_highlighted = True
            except Exception:
                pass

        # 2. 그리기 (Hover, Selection, Highlight)
        hovered_row = getattr(view, 'hovered_row', -1)
        hovered_col = getattr(view, 'hovered_col', -1)
        is_hovered = (index.row() == hovered_row) or \
                     (view is self.parent_window.tableView and index.column() == hovered_col)

        if is_hovered:
            painter.fillRect(option.rect, QColor(0, 120, 215, 20))

        opt = QStyleOptionViewItem(option)
        if opt.state & QStyle.State_Selected:
            opt.palette.setColor(QPalette.Highlight, QColor(200, 230, 201))
            opt.palette.setColor(QPalette.HighlightedText, Qt.black)
        elif is_highlighted:
            painter.fillRect(opt.rect, QColor(255, 249, 196))
            opt.palette.setColor(QPalette.Text, Qt.black)

        super().paint(painter, opt, index)

        # 3. 경계선
        if index.row() in self._boundaries:
            painter.save()
            pen = QPen(option.palette.color(QPalette.Dark))
            pen.setWidth(3)
            painter.setPen(pen)
            rect = option.rect
            painter.drawLine(rect.topLeft(), rect.topRight())
            painter.restore()

        painter.restore()

    def createEditor(self, parent, option, index):
        """테이블과 컬럼에 따라 적절한 편집기를 생성하거나 팝업을 띄웁니다."""
        view = parent.parent() if hasattr(parent, 'parent') else None
        if not isinstance(view, QTableView):
            return super().createEditor(parent, option, index)

        # --- tableLeft (왼쪽 테이블) ---
        if view is self.parent_window.tableLeft:
            # 1. Comment Column (Index 1) -> 팝업 다이얼로그 사용
            if index.column() == 1:
                from ui.dialogs_recipe import CommentEditDialog
                # 현재 값 가져오기
                current_text = index.data(Qt.EditRole) or ""

                # 팝업 다이얼로그 실행
                dlg = CommentEditDialog(self.parent_window, current_text)
                if dlg.exec_() == QDialog.Accepted:
                    new_text = dlg.get_text()

                    # 모델에 값 직접 저장 (QStandardItemModel)
                    model = index.model()
                    model.setData(index, new_text, Qt.EditRole)

                    # [중요] setData를 호출하면 itemChanged 시그널이 발생하여
                    # RecipeWindow의 _on_left_item_edited가 자동으로 호출되고 DB에 저장됩니다.

                # 인라인 에디터를 만들지 않기 위해 None 반환
                return None

            # 2. Base Recipe Column (Index 2) -> ComboBox
            base_col_index = 2
            if index.column() == base_col_index:
                codes = getattr(self.parent_window, "_current_recipe_codes", [])
                cb = QComboBox(parent)
                cb.addItems([""] + codes)
                return cb

        # --- tableView (오른쪽 테이블) ---
        elif view is self.parent_window.tableView:
            # 1. 특수 에디터 확인 (Ramp/Dynamic → None 반환 = 다이얼로그 처리됨)
            special_editor = self._create_special_editor(parent, option, index)
            if special_editor is None:
                return None  # 다이얼로그가 이미 열렸으므로 에디터 불필요

            # 2. 일반 파라미터 셀 (LineEdit)
            editor = QLineEdit(parent)
            editor.setStyleSheet("QLineEdit { color: black; font-weight: bold; }")

            col = index.column()
            dyn_units = getattr(self.parent_window, '_dyn_units', [])

            if col < len(dyn_units) and dyn_units[col] and dyn_units[col].strip():
                validator = QDoubleValidator(editor)
                validator.setNotation(QDoubleValidator.StandardNotation)
                validator.setDecimals(4)
                editor.setValidator(validator)

            return editor

        return super().createEditor(parent, option, index)

    def _create_special_editor(self, parent, option, index):
        """Ramp, Dynamic Process 등 특별한 편집기를 생성하는 로직."""
        row, col = index.row(), index.column()
        try:
            param_id = self.parent_window._param_ids[col]
            original_param_name = self.parent_window._id_to_name_map.get(param_id)

            # 메타데이터 참조
            recipe_id = self.parent_window._current_rows[row][0]
            step_no = self.parent_window._row_stepnos[row]
            # [수정] step_name 가져오기 (index 4)
            step_name = self.parent_window._current_rows[row][1][4]

            proc_db = self.parent_window._current_process_db
        except (IndexError, AttributeError, KeyError):
            return super().createEditor(parent, option, index)

        if not all([proc_db, original_param_name]):
            return super().createEditor(parent, option, index)

        # 0) Ramp 형식 값("X > Y") 셀 → 직접 편집 차단 (Ramp Times 셀을 통해 편집)
        current_display = str(index.data(Qt.DisplayRole) or "")
        if " > " in current_display and "Ramp Times" not in original_param_name:
            return None

        # 1) Ramp 편집
        if "Ramp Times" in original_param_name:
            self.parent_window._open_ramp_editor(parent, proc_db, recipe_id, step_no, original_param_name)
            return None

        # 2) Dynamic Process / Dynamic Process Step 편집
        if ("Dynamic Process Step" in original_param_name) or ("Dynamic Process" in original_param_name):
            name2id = {v: k for k, v in self.parent_window._id_to_name_map.items()}
            dps_pid = name2id.get("Dynamic Process Step")
            dp_pid = name2id.get("Dynamic Process")

            # [수정] step_name 인자 전달
            self.parent_window._open_dynamic_step_editor(parent, proc_db, recipe_id, step_no, step_name, dps_pid,
                                                         dp_pid)
            return None

        return super().createEditor(parent, option, index)


class FastLeftModel(QtCore.QAbstractTableModel):
    """
    왼쪽 테이블(메타데이터)용 고속 모델
    QStandardItem을 생성하지 않고 Python List를 직접 참조하여 메모리와 속도를 최적화합니다.
    """

    # 기존 시그널 핸들러와 연결하기 위해 row, col, value를 전달하는 시그널 정의
    dataEdited = pyqtSignal(int, int, str)

    def __init__(self, headers, rows_data, recipes_with_hidden, parent=None):
        super().__init__(parent)
        self._headers = headers
        self._data = rows_data
        # 숨겨진 스텝을 가진 레시피 ID 집합 (Bold/Underline용)
        self._recipes_with_hidden = recipes_with_hidden
        self._parent = parent  # 상위 윈도우 참조 (상수 접근용)

    def rowCount(self, parent=None):
        return len(self._data)

    def columnCount(self, parent=None):
        return len(self._headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self._headers[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid(): return None

        row, col = index.row(), index.column()
        if row >= len(self._data) or col >= len(self._data[row][1]):
            return None

        if role == Qt.DisplayRole or role == Qt.EditRole:
            return self._data[row][1][col]

        elif role == Qt.TextAlignmentRole:
            return Qt.AlignRight | Qt.AlignVCenter

        elif role == Qt.ToolTipRole:
            if col == 1: return self._data[row][1][col]

        elif role == Qt.UserRole:
            if col == 3: return self._data[row][0]  # Recipe ID

        if role == Qt.FontRole:
            if col == 3:
                rid = self._data[row][0]
                if rid in self._recipes_with_hidden:
                    font = QFont()
                    font.setBold(True)
                    font.setUnderline(True)
                    return font

        elif role == Qt.ForegroundRole:
            if col == 4 and self._parent is not None:
                txt = str(self._data[row][1][col])
                if txt.endswith(self._parent.HIDDEN_SUFFIX):
                    return QColor(Qt.gray)

        return None

    def flags(self, index):
        if not index.isValid(): return Qt.NoItemFlags
        if index.column() == 0: return Qt.ItemIsEnabled | Qt.ItemIsSelectable
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable

    def setData(self, index, value, role=Qt.EditRole):
        if index.isValid() and role == Qt.EditRole:
            self.dataEdited.emit(index.row(), index.column(), value)
            return True
        return False

    def update_data(self, row, col, new_val):
        if 0 <= row < len(self._data):
            self._data[row][1][col] = new_val
            idx = self.index(row, col)
            self.dataChanged.emit(idx, idx, [Qt.DisplayRole, Qt.EditRole])


class FastRightModel(QtCore.QAbstractTableModel):
    """오른쪽 테이블(파라미터)용 고속 모델"""

    dataEdited = pyqtSignal(int, int, str)

    def __init__(self, headers, dense_data, rows_data, parent=None):
        super().__init__(parent)
        self._headers = headers
        self._data = dense_data  # 2D List (값)
        self._rows_data = rows_data  # List (메타데이터 참조용)
        self._parent = parent  # HIDDEN_SUFFIX 접근용

    def rowCount(self, parent=None):
        return len(self._data)

    def columnCount(self, parent=None):
        return len(self._headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self._headers[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid(): return None
        row, col = index.row(), index.column()
        if row >= len(self._data) or col >= len(self._data[row]):
            return None

        if role == Qt.DisplayRole or role == Qt.EditRole:
            return self._data[row][col]

        elif role == Qt.TextAlignmentRole:
            return Qt.AlignRight | Qt.AlignVCenter

        # 1. [신규] 글자 색상 (숨겨진 스텝 행 전체를 회색으로)
        if role == Qt.ForegroundRole:
            try:
                # rows_data 구조: [rid, [Date, Comment, Base, Recipe, Step], {param}]
                # Step 이름은 index 1번 리스트의 4번 인덱스
                step_name = str(self._rows_data[row][1][4])
                if step_name.endswith(self._parent.HIDDEN_SUFFIX):
                    return QColor(Qt.gray)
            except (IndexError, AttributeError):
                pass
            return None

        return None

    def flags(self, index):
        if not index.isValid(): return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable

    def setData(self, index, value, role=Qt.EditRole):
        if index.isValid() and role == Qt.EditRole:
            self.dataEdited.emit(index.row(), index.column(), value)
            return True
        return False

    def update_data(self, row, col, new_val):
        if 0 <= row < len(self._data) and 0 <= col < len(self._data[row]):
            # [수정] 들어온 값을 Model 포맷(String)에 맞춰서 변환 후 저장
            if new_val is None:
                str_val = ""
            elif isinstance(new_val, float):
                str_val = str(int(new_val)) if new_val.is_integer() else str(new_val)
            else:
                str_val = str(new_val)

            self._data[row][col] = str_val

            idx = self.index(row, col)
            self.dataChanged.emit(idx, idx, [Qt.DisplayRole, Qt.EditRole])