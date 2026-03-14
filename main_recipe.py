import sys
import os
import json
import ctypes
from collections import deque
from PyQt5.QtWidgets import *
from PyQt5 import uic
from PyQt5.QtCore import *
from PyQt5.QtGui import *

#== Local Imports ==
from version import FULL_NAME
from config_recipe import db_path, db_path2 # 설정 파일
from utils_recipe import excepthook
from database_manager import DatabaseManager
from database_service import RecipeService

#UI 컴포넌트
from ui.widgets import (
TableViewWithCopyPaste, FilterHeaderView, IgnoreWheelFilter, UnifiedDelegate
)

# Mixin imports
from mixins.mixin_selection import SelectionMixin
from mixins.mixin_column import ColumnMixin
from mixins.mixin_combo import ComboMixin
from mixins.mixin_editing import EditingMixin
from mixins.mixin_recipe_crud import RecipeCrudMixin
from mixins.mixin_table import TableMixin

sys.excepthook = excepthook




"""Recipe Main GUi"""
class RecipeWindow(ComboMixin, TableMixin, RecipeCrudMixin,
                   EditingMixin, ColumnMixin, SelectionMixin,
                   QMainWindow):
    # Custom role definition
    RecipeIdRole = Qt.UserRole + 1
    default_cols = ["Date", "Comment", "Base", "Recipe", "Step"]

    # ★ Base 필터용 특수 토큰
    BASE_FILTER_ALL = "__BASE_ALL__"
    BASE_FILTER_NONE = "__BASE_NONE__"

    #숨긴 식별자 정의
    HIDDEN_SUFFIX = "$$"

    # UI 레이아웃 상수 (_render_recipe_table, _apply_zoom 공용)
    LAYOUT = {
        "row_padding": 8,
        "row_min_height": 30,
        "date_padding": 10,
        "comment_width": 90,
        "col_padding": 10,
        "col_min_width": 30,
        "right_header_pad": 10,
        "right_min_width": 30,
        "header_padding": 8,
    }

    def __init__(self, parent=None):
        super(RecipeWindow, self).__init__(parent)

        ################################################################
        ########exe 파일 내에 Ui를 포함하기 위해 ui의 절대 위치를 파악#########
        def resource_path(relative_path):
            """Get absolute path to resource, works for dev and for Pyinstaller"""
            base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
            return os.path.join(base_path, relative_path)

        form = resource_path("Recipe Editor.ui")
        uic.loadUi(form, self)
        ################################################################
        # DatabaseManager 인스턴스 생성
        self.db_manager = DatabaseManager(db_path, db_path2)
        # RecipeService 인스턴스 생성(db_manager 주입)
        self.recipe_service = RecipeService(self.db_manager)

        self.setWindowIcon(QIcon(resource_path('RecipeMN.ico')))
        self.setWindowTitle(FULL_NAME)

        self._wheel_filter = IgnoreWheelFilter(self)
        for cb in (self.process_combo, self.sheet_combo, self.chamber_id_combo):
            cb.installEventFilter(self._wheel_filter)

        # ▷ Undo 스택: 최대 50개
        self._undo_stack = deque(maxlen=50)

        # ▷ Mixin 공유 상태 초기화 (_refresh_recipe_table에서 채워짐)
        self._current_rows = []
        self._dyn_cols = []
        self._param_ids = []
        self._dyn_mappings = []
        self._dyn_units = []
        self._row_stepnos = []
        self._row_occidx = []
        self._groups = []
        self._base_map = {}
        self._base_lookup = {}
        self._id_to_name_map = {}
        self._current_recipe_codes = []
        # 필터/정렬 상태 (mixin_table, mixin_selection, mixin_combo에서 사용)
        self._active_filters = {}
        self._filter_universe = {}
        self._sort_col_idx = None
        self._sort_order = Qt.AscendingOrder
        # 모델 참조 (_apply_zoom 등에서 getattr 없이 참조하므로 반드시 초기화)
        self.left_model = None
        self.right_model = None
        # 하이라이트/숨김 상태
        self._highlight_target_recipe_code = None
        self._hidden_recipe_ids = set()

        # ▷ Ctrl+Z 단축키 연결
        undo_sc = QShortcut(QKeySequence("Ctrl+Z"), self)
        undo_sc.activated.connect(self._undo)

        # 프로세스 맵 초기화
        self._process_map: dict[str, list[str]] = {}
        self._build_process_map()
        # 프로세스 DB
        self._current_process_db: str | None = None
        # 초기 상태: View Mode
        self.mode_btn = self.findChild(QPushButton, "mode")
        # 초기 상태: View Mode
        self.mode_btn.setText("View Mode")
        self._is_edit_mode = False
        # Base 필터 초기값
        self._current_base_filter = None  # 삭제했지만 만약을 대비해서 냅둠.
        # 검색 필터 초기값
        self._current_code_filter = None

        #임시로 Show하고 있는 Step 목록
        self._temp_shown_recipes = set()

        self._setup_ui()

        # 기타 시그널 연결
        self._setup_signals()
        self._setup_shortcuts()

        # __init__ 끝부분 어딘가에 추가 (UI 로딩 이후)
        self._ensure_pulse_setting_menu()

        # UI가 모두 준비된 후 지연 실행 (안전한 시그널 처리를 위해)
        QTimer.singleShot(0, self.load_default_settings)

    def _setup_ui(self):
        # 1) 기존 tableView 제거
        old_tv = self.tableView
        self.gridLayout_2.removeWidget(old_tv)
        old_tv.deleteLater()

        # 2) 왼쪽/오른쪽 TableViewWithCopyPaste 생성
        self.leftView = TableViewWithCopyPaste(self)
        self.rightView = TableViewWithCopyPaste(self)

        # 3) SizePolicy 조정
        self.leftView.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Expanding)
        self.rightView.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # 4) 폰트 설정
        self.current_font_size = 13
        font = QFont()
        font.setPixelSize(self.current_font_size)
        font2 = QFont()
        font2.setPixelSize(15)
        self.leftView.setFont(font)
        self.rightView.setFont(font)
        self.label.setFont(font2)
        self.Pulse_B.setEnabled(True)
        self.Query_B.setEnabled(False)

        # -------------------------------------------------------------
        # [수정] TableLeft 헤더 교체 (self.leftView 사용)
        # -------------------------------------------------------------
        self.filterHeader = FilterHeaderView(Qt.Horizontal, self.leftView)
        self.filterHeader.sectionClicked.connect(self._on_header_clicked)  # 좌클릭(정렬)
        self.filterHeader.rightClicked.connect(self._on_header_right_clicked)  # 우클릭(필터)

        header_style = """
                   QHeaderView::section {
                       background-color: rgb(52, 171, 252);
                       color: black;
                       font-weight: bold;
                       border: 1px solid #6c6c6c;
                       padding: 2px;
                   }
               """
        self.filterHeader.setStyleSheet(header_style)
        self.leftView.setHorizontalHeader(self.filterHeader)
        self.rightView.horizontalHeader().setStyleSheet(header_style)
        # -------------------------------------------------------------

        # =========================================================================
        # [★핵심 수정] 수직 헤더(행 높이 제어)를 왼쪽 테이블 기준으로 통합
        # 오른쪽 테이블의 수직 헤더를 왼쪽 테이블의 수직 헤더로 교체합니다.
        # 이로써 왼쪽 행 높이가 변하면 오른쪽도 무조건 같이 변합니다.
        # =========================================================================
        self.rightView.setVerticalHeader(self.leftView.verticalHeader())
        self.leftView.verticalHeader().setDefaultSectionSize(30)  # 기본 높이 설정

        # 5) 스크롤바 정책 및 헤더 설정
        self.leftView.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.rightView.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.leftView.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.rightView.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.leftView.verticalHeader().setVisible(False)
        self.rightView.verticalHeader().setVisible(False)

        # 6) 수직 스크롤 동기화
        lv = self.leftView.verticalScrollBar()
        rv = self.rightView.verticalScrollBar()
        lv.valueChanged.connect(rv.setValue)
        rv.valueChanged.connect(lv.setValue)

        # 7) 두 뷰를 레이아웃에 담기
        container = QWidget(self)
        h = QHBoxLayout(container)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.leftView, 0)
        h.addWidget(self.rightView, 1)
        self.gridLayout_2.addWidget(container, 1, 0, 1, 1)

        # 8) 참조 업데이트 (여기서부터 tableLeft와 leftView는 같습니다)
        self.tableLeft = self.leftView
        self.tableView = self.rightView
        # 이제 왼쪽 테이블에서 Ctrl+C를 누르면 오른쪽 데이터까지 합쳐서 복사됩니다.
        self.tableLeft.copy = self.copy_combined_data
        # 2. 붙여넣기: 데이터를 파싱해서 왼쪽/오른쪽으로 분배
        self.tableLeft.paste = self.paste_combined_data

        # 9) 컬럼 이동 설정 (tableView = rightView)
        header = self.tableView.horizontalHeader()
        header.setSectionsMovable(True)
        header.setSectionsClickable(True)
        header.sectionMoved.connect(self._on_section_moved)

        # 10) Process 초기 로딩
        self.refresh_process_combo()

        # 11) 셀 선택 모드 설정
        for tbl in (self.leftView, self.rightView):
            tbl.setSelectionBehavior(QAbstractItemView.SelectItems)
            tbl.setSelectionMode(QAbstractItemView.ExtendedSelection)
            tbl.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        # 12) 단일 통합 Delegate 생성 및 적용
        delegate = UnifiedDelegate(self)
        self._row_delegate_left = delegate
        self._row_delegate_right = delegate

        for tbl in (self.leftView, self.tableView):
            tbl.setMouseTracking(True)
            tbl.viewport().setMouseTracking(True)
            tbl.hovered_row = -1
            tbl.hovered_col = -1
            tbl.setItemDelegate(delegate)
            tbl.viewport().installEventFilter(self)

        # 13) View Mode 설정
        for tbl in (self.leftView, self.tableView):
            tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)

        # 14) tableLeft 너비 조정 시그널 연결
        self.leftView.horizontalHeader().sectionResized.connect(self._adjust_left_width)

        # 15) 초기 너비 계산
        self._adjust_left_width()
        self._enable_secondary_filters(False)

        # [신규] 붙여넣기 시그널 연결
        self.tableView.itemsPasted.connect(self._on_batch_paste)

        # [신규] 단축키 설명 라벨 설정
        if hasattr(self, 'label_3'):
            font_shortcut = QFont()
            font_shortcut.setPixelSize(15)

            self.label_3.setFont(font_shortcut)
            self.label_3.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            shortcut_info = """
                <html>
                <head/>
                <body>
                    <p style="margin-bottom: 3px;"><b><u>Short Cut</u></b></p>
                    <p style="margin-top: 0px; margin-bottom: 1px;">New Recipe : Ctrl + N</p>
                    <p style="margin-top: 0px; margin-bottom: 1px;">Import Recipe : Ctrl + I</p>
                    <p style="margin-top: 0px; margin-bottom: 1px;">Copy Recipe : Ctrl + Shift + C</p>
                    <p style="margin-top: 0px; margin-bottom: 1px;">Delete Recipe : Ctrl + Delete</p>
                    <p style="margin-top: 0px; margin-bottom: 1px;">Delete Step : Shift + Delete</p>
                    <p style="margin-top: 0px; margin-bottom: 1px;">Hide Recipe : Ctrl + H</p>
                </body>
                </html>
                """
            self.label_3.setText(shortcut_info)

        # [신규] Diff View 체크박스 연결
        self.diff_view_chk = self.findChild(QCheckBox, "diff_view_chk")
        if self.diff_view_chk:
            # 상태 변경 시 바로 UI 갱신 함수 호출
            self.diff_view_chk.stateChanged.connect(self._update_diff_view)

        # [수정] Selection 시그널 연결 (기존 exclusive 로직 제거 -> sync 로직 적용)
        # _on_selection_sync 메서드 하나로 통합 처리
        self.tableLeft.selectionModel().selectionChanged.connect(
            lambda s, d: self._on_selection_sync(is_left=True, selected=s, deselected=d)
        )
        self.tableView.selectionModel().selectionChanged.connect(
            lambda s, d: self._on_selection_sync(is_left=False, selected=s, deselected=d)
        )

        self.Column_B.clicked.connect(self.open_column_dialog)
        self.ProEdit_B.clicked.connect(self.open_proedit_dialog)
        


    def _collect_parameters_for_pulse(self) -> list[dict]:
        """
        현재 화면/컨텍스트에서 파라미터 목록을 PulseSettingDialog에 넘길 형태로 만든다.
        dyn_cols / param_ids / dyn_units / dyn_mappings 기반.
        """
        params = []
        # 방어: 아직 로딩 전이면 빈 리스트
        if not getattr(self, "_dyn_cols", None) or not getattr(self, "_param_ids", None):
            return params

        n = min(len(self._dyn_cols), len(self._param_ids), len(self._dyn_units), len(self._dyn_mappings))
        for i in range(n):
            pid = self._param_ids[i]
            col = str(self._dyn_cols[i] or "")
            unit = str(self._dyn_units[i] or "")
            mapping = str(self._dyn_mappings[i] or "")

            # name은 id_to_name_map이 있으면 그걸 우선
            name = ""
            try:
                name = str(self._id_to_name_map.get(pid, "")) if getattr(self, "_id_to_name_map", None) else ""
            except Exception:
                name = ""

            display_name = name if name else col.replace("\n", " ")
            display = f"{display_name} [{unit}]" if unit else display_name

            params.append({
                "pid": pid,
                "name": display_name,
                "unit": unit,
                "mapping": mapping,
                "display": display
            })
        return params
    
    ##vicky_add nethod
    def _ensure_pulse_setting_menu(self):
        """
        UI에 menuSetting이 있으면 거기에 Pulse setting QAction 추가.
        없으면 menubar에 Setting 메뉴 생성 후 추가.
        """
        # menuSetting이 ui에 없을 수도 있으니 방어
        menu_setting = getattr(self, "menuSetting", None)
        if menu_setting is None:
            # menubar에서 찾거나 생성
            mb = self.menuBar()
            menu_setting = mb.addMenu("Setting")
            self.menuSetting = menu_setting

        # QAction 생성
        self.actionPulseSetting = QAction("Pulse setting", self)
        menu_setting.addAction(self.actionPulseSetting)
        self.actionPulseSetting.triggered.connect(self.open_pulse_setting_dialog)


    def open_pulse_setting_dialog(self):
        """
        Pulse setting 다이얼로그 오픈 (중복 오픈 방지 + 저장/불러오기)
        """
        if getattr(self, "_pulse_dlg", None) is not None and self._pulse_dlg.isVisible():
            self._pulse_dlg.raise_()
            self._pulse_dlg.activateWindow()
            return

        from ui.dialogs_pulse import PulseSettingDialog

        params = self._collect_parameters_for_pulse()
        if not params:
            QMessageBox.information(self, "Info", "No parameters loaded yet.\nLoad a recipe/sheet first.")
            return

        dlg = PulseSettingDialog(self, parameters=params, default_viewers=3)
        self._pulse_dlg = dlg

        # 저장된 값 불러오기
        saved = self._load_pulse_settings()
        if saved:
            dlg.apply_persist_data(saved)

        def _cleanup(_result=None):
            self._pulse_dlg = None

        dlg.finished.connect(_cleanup)

        if dlg.exec_() == QDialog.Accepted:
            # 시뮬레이션용 pid 구조
            configs = dlg.get_all_configs()

            # UI 복원용 상세 저장 구조
            persist_data = dlg.export_persist_data()
            self._save_pulse_settings(persist_data)

            QMessageBox.information(self, "Saved", f"Pulse setting saved.\nViewers: {len(configs)}")


    def _pulse_settings_scope_key(self) -> str:
        process = self.process_combo.currentText().strip() or "_NO_PROCESS_"
        sheet = self.sheet_combo.currentText().strip() or "_NO_SHEET_"
        chamber = self.chamber_id_combo.currentText().strip() or "_NO_CHAMBER_"
        return f"PulseSetting/{process}/{sheet}/{chamber}"
    


    def _get_pid_meta_for_pulse(self) -> dict[int, dict]:
        out = {}
        n = min(len(self._param_ids), len(self._dyn_mappings), len(self._dyn_units), len(self._dyn_cols))
        for i in range(n):
            try:
                pid = int(self._param_ids[i])
            except Exception:
                continue

            out[pid] = {
                "mapping": str(self._dyn_mappings[i] or ""),
                "unit": str(self._dyn_units[i] or ""),
                "name": str(self._dyn_cols[i] or "").replace("\n", " "),
            }
        return out


    def _get_visible_recipe_data_for_pulse(self) -> list[dict]:
        """
        현재 화면에 보이는 recipe/step 데이터를 pulse viewer에서 쓰기 좋은 구조로 변환
        반환 예:
        [
            {
                "recipe_code": "ABC123",
                "steps": [
                    {"step_no": 1, "step_name": "STEP1", "params": {...}},
                    ...
                ]
            },
            ...
        ]
        """
        recipe_map = {}
        row_count = min(len(self._current_rows), len(self._row_stepnos))

        for i in range(row_count):
            row = self._current_rows[i]
            if not row or len(row) < 3:
                continue

            rid, base_vals, param_dict = row
            recipe_code = str(base_vals[3]) if len(base_vals) > 3 else ""
            step_name = str(base_vals[4]) if len(base_vals) > 4 else ""
            step_no = self._row_stepnos[i] if i < len(self._row_stepnos) else (i + 1)

            if recipe_code not in recipe_map:
                recipe_map[recipe_code] = {
                    "recipe_code": recipe_code,
                    "steps": []
                }

            recipe_map[recipe_code]["steps"].append({
                "step_no": step_no,
                "step_name": step_name,
                "params": dict(param_dict or {})
            })

        # step 순서 정렬
        result = list(recipe_map.values())
        for r in result:
            r["steps"].sort(key=lambda x: x["step_no"])
        return result
    
    def open_pulse_viewer_dialog(self):
        """
        Pulse Viewer 열기
        - 저장된 pulse setting 불러옴
        - 현재 화면의 recipe/step 데이터로 viewer 그림
        """
        from ui.dialogs_pulse_viewer import PulseViewerDialog

        saved = self._load_pulse_settings()
        if not saved:
            QMessageBox.information(self, "Info", "No pulse setting saved yet.\nPlease configure Pulse setting first.")
            return

        recipe_data = self._get_visible_recipe_data_for_pulse()
        if not recipe_data:
            QMessageBox.information(self, "Info", "No visible recipe data to draw.")
            return

        pid_meta = self._get_pid_meta_for_pulse()

        dlg = PulseViewerDialog(
            self,
            pulse_setting=saved,
            recipe_data=recipe_data,
            pid_meta=pid_meta
        )
        dlg.exec_()

    def _save_pulse_settings(self, persist_data: dict):
        settings = QSettings("RecipeMN", "PulseView")
        key = self._pulse_settings_scope_key()
        settings.setValue(key, json.dumps(persist_data, ensure_ascii=False))

    def _load_pulse_settings(self) -> dict:
        settings = QSettings("RecipeMN", "PulseView")
        key = self._pulse_settings_scope_key()
        raw = settings.value(key, "")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _setup_signals(self):
        self.improt_db.triggered.connect(self.open_import_dialog)
        self.menuFile.addAction(self.export_excel)
        self.export_excel.triggered.connect(self._open_export_excel_dialog)

        self.actionSetting.triggered.connect(self.save_default_settings)
        self.actionSettingRemove.triggered.connect(self.remove_default_settings)

        self.Column_B.clicked.connect(self.open_column_dialog)
        self.ProEdit_B.clicked.connect(self.open_proedit_dialog)
        self.Pulse_B.clicked.connect(self.open_pulse_viewer_dialog)   # ✅ 변경

        self.chamber_id_combo.currentIndexChanged.connect(self._on_chamber_selected)
        self.process_combo.currentIndexChanged.connect(self._on_process_selected)
        self.sheet_combo.currentIndexChanged.connect(self._on_sheet_selected)

        self.search_rcp.returnPressed.connect(self.on_search_rcp)
        self.mode_btn.clicked.connect(self._toggle_mode)

        self.tableView.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tableView.customContextMenuRequested.connect(self._on_header_context_menu)
        self.tableLeft.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tableLeft.customContextMenuRequested.connect(self._on_left_context_menu)

        # [Fix #10] selectionChanged 시그널은 _render_recipe_table() finally에서 관리됨
        # 여기서 중복 연결하지 않음 (_setup_ui에서 _on_selection_sync만 초기 연결)

    def _setup_shortcuts(self):
        """키보드 단축키를 설정합니다."""
        # 1. New Recipe (Ctrl + N)
        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(self.open_new_recipe_dialog)

        # 2. Import Recipe (Ctrl + I)
        QShortcut(QKeySequence("Ctrl+I"), self).activated.connect(self.open_recipe_import)

        # 3. Copy Recipe (Ctrl + Shift + C) -> 소스 선택 창 띄우기
        QShortcut(QKeySequence("Ctrl+Shift+C"), self).activated.connect(self.copy_recipe)

        # 4. Delete Recipe (Ctrl + Delete) -> 현재 선택된 셀의 레시피 삭제
        QShortcut(QKeySequence("Ctrl+Delete"), self).activated.connect(self._action_delete_recipe_key)

        # 5. Delete Step (Shift + Delete) -> 현재 선택된 셀의 스텝 삭제
        QShortcut(QKeySequence("Shift+Delete"), self).activated.connect(self._action_delete_step_key)

        # 6. Hide Recipe (Ctrl + H) -> 현재 선택된 레시피 숨김
        QShortcut(QKeySequence("Ctrl+H"), self).activated.connect(self._action_hide_recipe_key)

    def _toggle_mode(self):
        """
        View Mode ↔ Edit Mode 토글
        """
        self._is_edit_mode = not self._is_edit_mode

        if self._is_edit_mode:
            self.mode_btn.setText("Edit Mode")
            # [수정] AnyKeyPressed 추가 (키 입력 시 바로 편집 시작)
            triggers = (QAbstractItemView.DoubleClicked |
                        QAbstractItemView.SelectedClicked |
                        QAbstractItemView.AnyKeyPressed)
        else:
            self.mode_btn.setText("View Mode")
            triggers = QAbstractItemView.NoEditTriggers

            # 두 테이블에 모두 적용
        for tbl in (self.tableLeft, self.tableView):
            tbl.setEditTriggers(triggers)

    def _open_export_excel_dialog(self):
        """Excel Export 다이얼로그를 엽니다."""
        from ui.dialogs_recipe import ExportExcelDialog
        current_ctx = {
            "process": self.process_combo.currentText().strip(),
            "sheet": self.sheet_combo.currentText().strip(),
            "chamber": self.chamber_id_combo.currentText().strip(),
        }
        dlg = ExportExcelDialog(self, self.recipe_service, current_context=current_ctx)
        dlg.exec_()

    def eventFilter(self, source, event):
        left_vp = self.tableLeft.viewport()
        right_vp = self.tableView.viewport()

        # [신규] 3. Ctrl + Wheel Zoom 구현
        if event.type() == QEvent.Wheel and (event.modifiers() & Qt.ControlModifier):
            # source가 viewport이거나 테이블 자체일 때 모두 처리
            if source in (left_vp, right_vp, self.tableLeft, self.tableView):
                delta = event.angleDelta().y()
                if delta > 0:
                    self._apply_zoom(1)  # Zoom In
                else:
                    self._apply_zoom(-1)  # Zoom Out
                return True  # 이벤트 소비 (스크롤 방지)

        # 기존 마우스 무브/리브 처리
        if source in (left_vp, right_vp):
            is_left = (source is left_vp)
            view = self.tableLeft if is_left else self.tableView

            if event.type() == QEvent.MouseMove:
                idx = view.indexAt(event.pos())
                new_row = idx.row() if idx.isValid() else -1
                new_col = idx.column() if idx.isValid() else -1

                self.tableLeft.hovered_row = new_row
                self.tableView.hovered_row = new_row

                if is_left:
                    self.tableLeft.hovered_col = new_col
                else:
                    self.tableView.hovered_col = new_col

                self.tableLeft.viewport().update()
                self.tableView.viewport().update()
                return False

            elif event.type() == QEvent.Leave:
                for tbl in (self.tableLeft, self.tableView):
                    tbl.hovered_row = -1
                    tbl.hovered_col = -1
                    tbl.viewport().update()
                return False

        return super().eventFilter(source, event)


if __name__ == "__main__":
    # 1. Qt 자동 스케일링 비활성화
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"
    os.environ["QT_SCALE_FACTOR"] = "1"

    # 2. Qt HighDpi 속성 끄기
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, False)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, False)  # 이미지 확대도 방지

    # 3. Windows OS에게 "DPI를 직접 관리하겠다"고 선언 (Per-Monitor Aware)
    # 이 설정이 없으면, Qt가 100%로 그려도 윈도우가 창 자체를 1.5배로 '이미지 늘리듯' 키워버려서 뿌옇게 됩니다.
    # 이를 2(Per Monitor Aware)로 설정하면 윈도우가 건드리지 않아 100% 크기로 선명하게 나옵니다.
    if sys.platform == 'win32':
        try:
            # 2 = PROCESS_PER_MONITOR_DPI_AWARE
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    app = QApplication(sys.argv)
    # 4. 폰트 설정 (픽셀 단위 고정 권장)
    # 100% 고정이므로 Point 단위보다 Pixel 단위가 디자인 유지에 유리할 수 있습니다.
    font = QFont()
    font.setPixelSize(12)  # 12px (약 9pt) 정도로 고정
    app.setFont(font)

    # 5. 아이콘 및 윈도우 설정
    if sys.platform == 'win32':
        myappid = FULL_NAME.replace(" ", ".")
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

    if hasattr(sys, '_MEIPASS'):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))

    icon_path = os.path.join(base_path, "RecipeMN.ico")
    app.setWindowIcon(QIcon(icon_path))

    # version.txt 생성 (exe 옆에 생성)
    if getattr(sys, 'frozen', False):
        version_dir = os.path.dirname(sys.executable)
    else:
        version_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(version_dir, "version.txt"), "w", encoding="utf-8") as f:
            f.write(FULL_NAME)
    except OSError:
        pass

    window = RecipeWindow()
    window.setWindowIcon(QIcon(icon_path))
    window.showMaximized()

    sys.exit(app.exec_())
