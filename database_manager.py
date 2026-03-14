import sqlite3
import os
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

def retry_on_lock(max_retries=5, delay=1):
    """
    SQLite DB Locked/ReadOnly 에러 발생 시 재시도하는 데코레이터.
    - max_retries: 최대 재시도 횟수
    - delay: 재시도 사이 대기 시간(초)
    - 재시도 중에는 마우스 커서를 WaitCursor로 변경하며, UI 갱신을 수행함.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            cursor_changed = False
            try:
                while True:
                    try:
                        return func(*args, **kwargs)

                    except sqlite3.OperationalError as e:
                        error_msg = str(e).lower()
                        if "locked" in error_msg or "readonly" in error_msg or "busy" in error_msg:
                            retries += 1
                            if retries > max_retries:
                                raise

                            if not cursor_changed:
                                QApplication.setOverrideCursor(Qt.WaitCursor)
                                cursor_changed = True

                            print(f"[DB Locked] Retry {retries}/{max_retries} in {delay}s... Error: {e}")

                            # sleep을 분할하여 UI 응답성 유지
                            steps = max(int(delay * 10), 1)
                            for _ in range(steps):
                                QApplication.processEvents()
                                time.sleep(delay / steps)
                        else:
                            raise

            finally:
                if cursor_changed:
                    QApplication.restoreOverrideCursor()

        return wrapper
    return decorator

class DatabaseManager:
    """
    데이터베이스와의 모든 통신을 전담하는 클래스.
    SQL 쿼리 실행, 연결 관리 등의 역할을 수행합니다.
    """

    def __init__(self, recipe_db_path: str, process_db_dir: str):
        self.recipe_db_path = recipe_db_path
        self.process_db_dir = process_db_dir

    @contextmanager
    def get_connection(self, db_path: str):
        target_path = os.path.normpath(db_path)
        conn = sqlite3.connect(
            target_path,
            timeout=60.0,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False
        )
        try:
            cur = conn.cursor()

            cur.execute("PRAGMA journal_mode = TRUNCATE;")
            cur.execute("PRAGMA busy_timeout = 60000;")

            # 3) 동기화: NORMAL
            cur.execute("PRAGMA synchronous = NORMAL;")

            # 4) 기타 설정 (유지)
            cur.execute("PRAGMA temp_store = MEMORY;")
            cur.execute("PRAGMA cache_size = -64000;")
            cur.execute("PRAGMA foreign_keys = ON;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_process_db_path(self, process_name: str) -> str | None:
        """프로세스 이름을 기반으로 실제 DB 파일 경로를 반환합니다."""
        if not process_name:
            return None
        return os.path.join(self.process_db_dir, f"{process_name}.db")

    # --- [조회] 쿼리 메서드 ---
    def get_param_defs(self, chamber_id: str) -> list[tuple]:
        """특정 Chamber의 표시할 ParameterDefinition을 조회합니다."""
        with self.get_connection(self.recipe_db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT mapping, id, "order", unit
                  FROM ParameterDefinition
                 WHERE chamber_id=? AND hide=0
              ORDER BY "order"
            """, (chamber_id,))
            return cur.fetchall()

    def get_recipes(self, proc_db_path: str, cls_id: int,
                    code_filter: str | None, base_filter: str | None, *, _conn=None) -> list[tuple]:
        """
        조건에 맞는 Recipe 목록을 조회합니다.
        반환: (id, created_at, comment, base_recipe, recipe_code)
        _conn: 외부 연결을 재사용할 경우 전달 (단일 트랜잭션 보장용)

        base_filter 의미:
          None             → 필터 없음 (All)
          "__BASE_NONE__"  → base_recipe가 비어있는(=NULL/공백) 행만
          그 외 문자열     → 해당 base_recipe와 정확히 일치
        """
        sql = [
            "SELECT id, created_at, comment, base_recipe, recipe_code",
            "  FROM Recipe",
            " WHERE classification_id=?"
        ]
        params = [cls_id]

        # ★ [수정] Base Filter 로직 개선
        if base_filter is not None:
            if base_filter == "__BASE_NONE__":
                # Base가 없는 레시피만 조회 (기존 유지)
                sql.append("   AND (base_recipe IS NULL OR TRIM(base_recipe)='')")
            else:
                # [핵심 수정]
                # 1. 내 Base가 이 값인 경우 (자식들)
                # 2. OR 내 이름(Code)이 이 값인 경우 (부모 본인)
                sql.append("   AND (base_recipe = ? OR recipe_code = ? COLLATE NOCASE)")
                params.append(base_filter)
                params.append(base_filter)

        if code_filter:
            sql.append("   AND recipe_code LIKE ? COLLATE NOCASE")
            params.append(f"%{code_filter}%")

        # ★ 안정적 정렬: created_at 비/NULL을 뒤로 보내고 → created_at → recipe_code → id
        sql.append("""
               ORDER BY
                 CASE WHEN created_at IS NULL OR TRIM(created_at)='' THEN 1 ELSE 0 END,
                 created_at,
                 recipe_code COLLATE NOCASE,
                 id
           """)

        def _fetch(c):
            cur = c.cursor()
            cur.execute(" ".join(sql), tuple(params))
            return cur.fetchall()

        if _conn is not None:
            return _fetch(_conn)
        with self.get_connection(proc_db_path) as conn:
            return _fetch(conn)

    def get_param_values(self, proc_db_path: str, recipe_ids: list[int], id2order: dict, *, _conn=None) -> list[tuple]:
        """여러 레시피에 대한 모든 파라미터 값들을 조회합니다.
        _conn: 외부 연결을 재사용할 경우 전달 (단일 트랜잭션 보장용)"""
        if not recipe_ids: return []

        ph = ",".join("?" * len(recipe_ids))

        # [수정] id2order가 비어있는 경우를 처리하는 로직 추가
        if id2order:
            sorted_pids = sorted(id2order.keys(), key=lambda pid: id2order[pid])
            case_expr = "CASE parameter_id " + " ".join(
                f"WHEN {int(pid)} THEN {i}" for i, pid in enumerate(sorted_pids)
            ) + " ELSE 999999 END"
            order_clause = f"step_no, {case_expr}"  # ★ step_no 우선
        else:
            order_clause = "step_no, parameter_id"  # ★ 기본도 step_no 우선
        sql = f"""
               SELECT recipe_id, step, step_no, parameter_id, value, aux_value, id
                 FROM RecipeParameter
                WHERE recipe_id IN ({ph})
             ORDER BY recipe_id, {order_clause}
           """

        def _fetch(c):
            cur = c.cursor()
            cur.execute(sql, tuple(recipe_ids))
            return cur.fetchall()

        if _conn is not None:
            return _fetch(_conn)
        with self.get_connection(proc_db_path) as conn:
            return _fetch(conn)

    @retry_on_lock()
    def update_ramping_parameter(self, proc_db_path: str, recipe_id: int, step_no: int, params_to_update: dict):
        """Ramp 편집 결과를 DB에 일괄 업데이트합니다."""
        with self.get_connection(proc_db_path) as conn:
            cur = conn.cursor()
            for pid, values in params_to_update.items():
                cur.execute("""
                    UPDATE RecipeParameter SET value=?, aux_value=?
                    WHERE recipe_id=? AND step_no=? AND parameter_id=?
                """, (values.get('end'), values.get('start'), recipe_id, step_no, pid))

    def check_recipe_code_exists_in_chamber(self, proc_db_path: str, chamber_id: str, recipe_code: str,
                                              *, _conn=None) -> bool:
        """주어진 Process DB의 특정 Chamber 내에 동일한 recipe_code가 있는지 확인합니다.
        _conn: 외부 연결을 재사용할 경우 전달 (TOCTOU 방지용)"""
        if not all([proc_db_path, chamber_id, recipe_code]): return False

        def _fetch(c):
            cur = c.cursor()
            cur.execute("""
                SELECT 1 FROM Recipe r
                JOIN RecipClassification rc ON rc.id = r.classification_id
                WHERE rc.chamber_id = ? AND r.recipe_code = ? COLLATE NOCASE
                LIMIT 1
            """, (chamber_id, recipe_code.strip()))
            return cur.fetchone() is not None

        if _conn is not None:
            return _fetch(_conn)
        with self.get_connection(proc_db_path) as conn:
            return _fetch(conn)

    # --- [수정] 업데이트 메서드 ---
    @retry_on_lock()
    def update_recipe_metadata(self, proc_db_path: str, recipe_id: int, **kwargs):
        """Recipe 테이블의 특정 필드를 업데이트합니다. (comment, base_recipe, recipe_code)"""
        if not kwargs or not proc_db_path or not recipe_id: return

        fields = []
        params = []
        for key, value in kwargs.items():
            if key in ["comment", "base_recipe", "recipe_code"]:
                fields.append(f"{key}=?")
                params.append(value)

        if not fields: return

        params.append(recipe_id)
        sql = f"UPDATE Recipe SET {', '.join(fields)} WHERE id=?"

        with self.get_connection(proc_db_path) as conn:
            conn.execute(sql, tuple(params))

    @retry_on_lock()
    def update_step_name(self, proc_db_path: str, recipe_id: int, old_step: str, new_step: str):
        """특정 레시피의 Step 이름을 변경합니다."""
        with self.get_connection(proc_db_path) as conn:
            conn.execute("UPDATE RecipeParameter SET step=? WHERE recipe_id=? AND step=?",
                         (new_step, recipe_id, old_step))

    @retry_on_lock()
    def update_parameter_value(self, proc_db_path: str, new_value: float | None, recipe_id: int, param_id: int,
                               step_no: int, step_name: str):  # [수정] step_name 인자 추가
        """
        하나의 파라미터 값을 업데이트합니다.
        [수정] 해당 파라미터 행이 없으면(빈 칸이었으면) 새로 INSERT 합니다.
        """
        with self.get_connection(proc_db_path) as conn:
            cur = conn.cursor()
            # 1. 먼저 UPDATE 시도
            cur.execute("""
                        UPDATE RecipeParameter SET value=?
                        WHERE recipe_id=? AND parameter_id=? AND step_no=?
                    """, (new_value, recipe_id, param_id, step_no))

            # 2. 변경된 행이 없으면(데이터가 없었던 경우) INSERT 수행
            if cur.rowcount == 0:
                # aux_value는 NULL로 들어감 (기본값)
                cur.execute("""
                            INSERT INTO RecipeParameter (recipe_id, parameter_id, step, step_no, value)
                            VALUES (?, ?, ?, ?, ?)
                        """, (recipe_id, param_id, step_name, step_no, new_value))

    # --- [삭제] 삭제 메서드 ---
    @retry_on_lock()
    def delete_recipe(self, proc_db_path: str, recipe_id: int):
        """Recipe 및 관련 Parameter들을 삭제합니다. (ON DELETE CASCADE)"""
        with self.get_connection(proc_db_path) as conn:
            conn.execute("DELETE FROM Recipe WHERE id = ?", (recipe_id,))

    @retry_on_lock()
    def delete_step(self, proc_db_path: str, recipe_id: int, step_name: str):
        """특정 레시피의 한 스텝 전체를 삭제합니다."""
        with self.get_connection(proc_db_path) as conn:
            conn.execute("DELETE FROM RecipeParameter WHERE recipe_id=? AND step=?", (recipe_id, step_name))

    def get_param_ids_for_chamber(self, chamber_id: str, process_name: str = None) -> list[int]:
        """
        [수정] 현재 Process 설정에 맞춰 정렬된, 모든 활성(is_active=1) 파라미터 ID 목록을 반환합니다.
        숨김(hide) 여부와 무관하게 전체 반환 — 레시피 생성 시 모든 파라미터에 대해 행이 생성되어야 함.
        """
        rows = self.get_full_param_defs(chamber_id, process_name)
        # rows 구조: (id, name, mapping, hide, order, unit, is_active)
        # 모든 활성 파라미터 반환 (hide 필터 제거 — 숨긴 컬럼도 데이터는 보존)
        return [r[0] for r in rows]

    def get_values_for_base_recipe(self, proc_db_path: str, cls_id: int, base_code: str,
                                    *, _conn=None) -> dict[tuple[str, int], tuple]:
        """Base 레시피의 파라미터 값들을 (step_name, param_id) -> (value, aux_value) 형태의 딕셔너리로 반환합니다.
        _conn: 외부 연결을 재사용할 경우 전달 (Stale Read 방지용)"""
        def _fetch(c):
            base_map = {}
            cur = c.cursor()
            cur.execute("SELECT id FROM Recipe WHERE classification_id=? AND recipe_code=?", (cls_id, base_code))
            r = cur.fetchone()
            if r:
                base_id = r[0]
                cur.execute("SELECT parameter_id, step, value, aux_value FROM RecipeParameter WHERE recipe_id=?",
                            (base_id,))
                for pid, step_name, val, aux_val in cur.fetchall():
                    base_map[(step_name, pid)] = (val, aux_val)
            return base_map

        if _conn is not None:
            return _fetch(_conn)
        with self.get_connection(proc_db_path) as conn:
            return _fetch(conn)

    def get_classification_id(self, proc_db_path: str, sheet: str, chamber_id: str) -> int | None:
        """Process DB 경로, sheet, chamber ID로 classification ID를 조회합니다."""
        with self.get_connection(proc_db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM RecipClassification WHERE sheet=? AND chamber_id=?", (sheet, chamber_id))
            row = cur.fetchone()
            return row[0] if row else None

    def get_recipe_steps(self, proc_db_path: str, recipe_id: int) -> list[tuple]:
        """Recipe ID에 해당하는 스텝 정보(이름, 번호)를 순서대로 조회합니다."""
        with self.get_connection(proc_db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT step, step_no FROM RecipeParameter WHERE recipe_id=? ORDER BY step_no",
                        (recipe_id,))
            return cur.fetchall()

    def get_param_name_to_id_map(self, chamber_id: str) -> dict[str, int]:
        """Chamber ID에 해당하는 활성(is_active=1) 파라미터 이름 -> ID 맵을 반환합니다."""
        with self.get_connection(self.recipe_db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, id FROM ParameterDefinition WHERE chamber_id=? AND is_active=1", (chamber_id,))
            return {name: pid for name, pid in cur.fetchall()}

    def get_param_id_to_name_map(self, chamber_id: str) -> dict[int, str]:
        """Chamber ID에 해당하는 활성(is_active=1) 파라미터 ID -> 이름 맵을 반환합니다."""
        with self.get_connection(self.recipe_db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name FROM ParameterDefinition WHERE chamber_id=? AND is_active=1", (chamber_id,))
            return {pid: name for pid, name in cur.fetchall()}

    @retry_on_lock()
    def insert_imported_recipes(self, proc_db_path: str, recipes_to_insert: list[dict]) -> int:
        """
        가져오기 위해 파싱된 여러 레시피와 파라미터 데이터를 DB에 일괄 삽입합니다.
        하나의 트랜잭션으로 처리하여 데이터 정합성을 보장합니다.
        [수정] 각 Recipe 삽입 직후 WaferInformation 연결을 시도합니다.
        """

        total_inserted = 0
        if not recipes_to_insert:
            return 0

        with self.get_connection(proc_db_path) as conn:
            cur = conn.cursor()
            for item in recipes_to_insert:
                rec_info = item["recipe"]
                params = item["params"]
                # a) Recipe 생성
                cur.execute("""
                           INSERT INTO Recipe (classification_id, recipe_code, base_recipe, created_by, created_at, updated_at, comment)
                           VALUES (?, ?, ?, ?, ?, ?, ?)
                       """, (rec_info['cls_id'], rec_info['recipe_code'], rec_info['base_recipe'],
                             rec_info['user_id'], rec_info['timestamp'], rec_info['timestamp'], rec_info['comment']))

                new_recipe_id = cur.lastrowid  # 새로 생성된 ID (예: 126)

                # b) RecipeParameter 생성
                if params:
                    param_inserts = [
                        (new_recipe_id, pid, step_name, step_no, value)
                        for (pid, step_name, step_no), value in params.items()
                    ]
                    cur.executemany("""
                               INSERT INTO RecipeParameter (recipe_id, parameter_id, step, step_no, value)
                               VALUES (?, ?, ?, ?, ?)
                           """, param_inserts)
                    total_inserted += len(param_inserts)

                # --- [★★★ 신규 추가: WaferInformation 연결 로직 (루프 내부) ★★★] ---
                # (Service에서 전달받은 chamber_id 사용)
                try:
                    new_code = rec_info['recipe_code']
                    chamber_id = rec_info['chamber_id']  # Service에서 추가해준 chamber_id

                    cur.execute("""
                            UPDATE WaferInformation
                            SET used_recipe_id = ?  -- (새 Recipe ID)
                            WHERE UsedRecipe = ?    -- (새 Recipe Code)
                              AND chamber_id = ?    -- (새 챔버 ID)
                              AND used_recipe_id IS NULL; -- (아직 연결 안 된 칩만)
                        """, (new_recipe_id, new_code, chamber_id))
                except (sqlite3.OperationalError, KeyError) as e:
                    # Locked/ReadOnly 에러라면 상위 데코레이터가 처리하도록 던짐
                    if isinstance(e, sqlite3.OperationalError) and (
                            "locked" in str(e).lower() or "readonly" in str(e).lower()):
                        raise e
                    # 그 외 에러(테이블 없음 등)는 무시
                    print(f"WaferInformation link warning (non-fatal): {e}")

        return total_inserted

    def get_full_param_defs(self, chamber_id: str, process_name: str = None) -> list[tuple]:
        """
           [수정] 특정 Chamber의 파라미터 정의를 조회하되,
           주어진 Process에 대한 표시 설정(Mapping, Order, Hide)을 우선 적용(Overlay)합니다.
           """
        # process_name이 None이거나 빈 문자열이면 조인이 실패하여 자연스럽게 Default 값(d.*)이 사용됩니다.
        # is_active가 없으면 1로 취급 (하위 호환)
        sql = """
               SELECT d.id, 
                      d.name, 

                      -- [변경] 설정이 없으면(c가 NULL), 원본 이름(d.name)을 매핑 이름으로 사용
                      COALESCE(c.mapping, d.name) as mapping,

                      -- [변경] 설정이 없으면, 기본적으로 보임(0) 처리
                      COALESCE(c.hide, 0) as hide,

                      -- [변경] 설정이 없으면, 순서를 맨 뒤(999999) 혹은 ID순으로 보냄
                      COALESCE(c."order", 999999) as "order",

                      d.unit,
                      COALESCE(d.is_active, 1) as is_active
                 FROM ParameterDefinition d
                 LEFT JOIN ParameterDisplayConfig c
                        ON d.id = c.param_id 
                       AND c.process_name = ?
                       AND c.chamber_id = ?
                WHERE d.chamber_id = ?
                AND d.is_active = 1
             ORDER BY "order" ASC, d.id ASC
           """

        with self.get_connection(self.recipe_db_path) as conn:
            cur = conn.cursor()
            # 파라미터 순서: [process_name, chamber_id (for JOIN), chamber_id (for WHERE)]
            cur.execute(sql, (process_name, chamber_id, chamber_id))
            return cur.fetchall()

    @retry_on_lock()
    def update_param_defs_batch(self, chamber_id: str, process_name: str, definitions: list[dict]):
        """
        [수정] 표시 설정 변경 사항을 ParameterDisplayConfig 테이블에 저장합니다.
        definitions: [{'pid': int, 'mapping': str, 'hide': int, 'order': int}, ...]
        """
        if not process_name:
            # 프로세스가 선택되지 않은 상태에서는 UI 설정을 저장하지 않도록 방어
            print("Warning: Process name is missing. Settings not saved.")
            return

        sql = """
            INSERT OR REPLACE INTO ParameterDisplayConfig 
            (chamber_id, process_name, param_id, mapping, hide, "order")
            VALUES (:chamber_id, :process_name, :pid, :mapping, :hide, :order)
        """

        # 쿼리 파라미터 보정 (dict에 chamber_id, process_name 주입)
        params_to_insert = []
        for d in definitions:
            new_d = d.copy()
            new_d['chamber_id'] = chamber_id
            new_d['process_name'] = process_name
            params_to_insert.append(new_d)

        with self.get_connection(self.recipe_db_path) as conn:
            conn.executemany(sql, params_to_insert)

    def get_all_chambers(self) -> list[str]:
        """Recipe.db에 있는 모든 Chamber ID 목록을 가져옵니다."""
        with self.get_connection(self.recipe_db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT Chamber_ID FROM Chamber_Info ORDER BY Chamber_ID")
            return [row[0] for row in cur.fetchall()]

    def get_scheme_codes(self, proc_db_path: str) -> list[str]:
        """특정 Process DB의 모든 SchemeCode를 가져옵니다."""
        if not os.path.exists(proc_db_path): return []
        with self.get_connection(proc_db_path) as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT SchemeCode FROM SchemeInformation ORDER BY SchemeCode")
                return [row[0] for row in cur.fetchall()]
            except sqlite3.OperationalError:  # 테이블이 없는 경우
                return []

    def create_new_process_db(self, db_path: str) -> None:
        """
        새 프로세스 DB를 '최신 스키마'로 생성한다.
        - 전역 유니크 recipe_code(COLLATE NOCASE)
        - Recipe.chamber_id 보유 + 분류 변경 전파 트리거
        - RecipeParameter.aux_value 포함
        - WaferInformation.used_recipe_id(FK) + UsedRecipe 텍스트와 양방향 동기화 트리거
        - 필수 인덱스/PRAGMA 설정
        """

        # 경로 준비 및 존재 체크
        dirpath = os.path.dirname(db_path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)

        # 파일 중복 방지
        if os.path.exists(db_path):
            raise FileExistsError(f"DB already exists: {db_path}")

        conn = None
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.cursor()

            # ───────── 분류/레시피/파라미터 (먼저 생성: WaferInfo가 참조해야 함) ─────────
            cursor.execute('''
                    CREATE TABLE IF NOT EXISTS RecipClassification (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        chamber_id   TEXT    NOT NULL,
                        sheet        TEXT    NOT NULL,
                        schemeCode   TEXT,
                        Date         REAL,
                        UNIQUE(chamber_id, sheet)
                    );
                ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_rc_chamber ON RecipClassification(chamber_id);')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_rc_sheet   ON RecipClassification(sheet);')

            cursor.execute('''
                    CREATE TABLE IF NOT EXISTS Recipe (
                        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                        classification_id  INTEGER NOT NULL REFERENCES RecipClassification(id),
                        recipe_code        TEXT    NOT NULL,
                        base_recipe        TEXT,
                        created_by         INTEGER,
                        created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
                        comment            TEXT,
                        UNIQUE(classification_id, recipe_code)
                    );
                ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_recipe_classif ON Recipe(classification_id);')

            cursor.execute('''
                    CREATE TABLE IF NOT EXISTS RecipeParameter (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        recipe_id     INTEGER NOT NULL REFERENCES Recipe(id) ON DELETE CASCADE,
                        parameter_id  INTEGER NOT NULL,
                        step          TEXT    NOT NULL,
                        step_no       INTEGER NOT NULL,
                        value         REAL,
                        aux_value     REAL,
                        UNIQUE(recipe_id, parameter_id, step_no)
                    );
                ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_rp_recipe    ON RecipeParameter(recipe_id);')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_rp_paramstep ON RecipeParameter(parameter_id, step_no);')

            # ───────── 기존 Raw/Meta/Results/Rules/Scheme 테이블들 ─────────
            # (WaferInformation 앞에 있어도 순서 상관 없음)
            cursor.execute('''
                    CREATE TABLE IF NOT EXISTS Raw (
                        Date         INTEGER,
                        Code         TEXT,
                        Item         TEXT,
                        DataSet      BLOB,
                        RawFilePath  TEXT,
                        Category     TEXT,
                        Unit         TEXT,
                        Operator     INTEGER
                    );
                ''')
            # ... (RawMeta, Results, Rules, SchemeInformation 테이블 생성 쿼리 - 원본과 동일) ...
            cursor.execute('''
                    CREATE TABLE IF NOT EXISTS RawMeta (
                        Code       TEXT PRIMARY KEY,
                        LotID      TEXT,
                        WaferSlot  INTEGER,
                        CouponPos  TEXT,
                        Sheet      TEXT,
                        Recipe     TEXT
                    );
                ''')
            cursor.execute('''
                    CREATE TABLE IF NOT EXISTS Results (
                        Date       INTEGER    NOT NULL,
                        Sheet      TEXT       NOT NULL,
                        Recipe     TEXT       NOT NULL,
                        Base       TEXT,
                        LotID      TEXT       NOT NULL,
                        WaferSlot  INTEGER    NOT NULL,
                        CouponPos  TEXT,
                        Category   INTEGER,
                        IndexItem  TEXT,
                        Value      REAL,
                        Unit       TEXT,
                        PRIMARY KEY (Sheet, Recipe, Category, IndexItem)
                    );
                ''')
            cursor.execute('''
                    CREATE TABLE IF NOT EXISTS Rules (
                        Sheet      TEXT    NOT NULL,
                        Comment    TEXT,
                        Category   TEXT    NOT NULL,
                        Methods    TEXT    NOT NULL,
                        IndexItem  TEXT    NOT NULL,
                        Formula    TEXT,
                        Unit       TEXT,
                        PRIMARY KEY (Sheet, IndexItem, Category)
                    );
                ''')
            cursor.execute('''
                    CREATE TABLE IF NOT EXISTS SchemeInformation (
                        Date        INTEGER,
                        SchemeCode  TEXT    NOT NULL PRIMARY KEY,
                        Comment     TEXT,
                        Pitch       REAL,
                        PitchUnit   TEXT,
                        StackUnit   TEXT,
                        Stack1      TEXT, Stack1H  REAL,
                        Stack2      TEXT, Stack2H  REAL,
                        Stack3      TEXT, Stack3H  REAL,
                        Stack4      TEXT, Stack4H  REAL,
                        Stack5      TEXT, Stack5H  REAL,
                        Stack6      TEXT, Stack6H  REAL,
                        Stack7      TEXT, Stack7H  REAL,
                        Stack8      TEXT, Stack8H  REAL,
                        Stack9      TEXT, Stack9H  REAL,
                        Stack10     TEXT, Stack10H REAL,
                        Stack11     TEXT, Stack11H REAL,
                        Stack12     TEXT, Stack12H REAL,
                        Stack13     TEXT, Stack13H REAL,
                        Stack14     TEXT, Stack14H REAL,
                        Stack15     TEXT, Stack15H REAL,
                        Stack16     TEXT, Stack16H REAL,
                        Stack17     TEXT, Stack17H REAL,
                        Stack18     TEXT, Stack18H  REAL,
                        Stack19     TEXT, Stack19H  REAL,
                        Stack20     TEXT, Stack20H  REAL,
                        Stack21     TEXT, Stack21H  REAL,
                        Stack22     TEXT, Stack22H  REAL,
                        Stack23     TEXT, Stack23H  REAL,
                        Stack24     TEXT, Stack24H  REAL,
                        Stack25     TEXT, Stack25H  REAL,
                        Stack26     TEXT, Stack26H  REAL,
                        Stack27     TEXT, Stack27H  REAL,
                        Stack28     TEXT, Stack28H  REAL,
                        Stack29     TEXT, Stack29H  REAL
                    );
                ''')

            # ───────── ★★★ WaferInformation (수정됨) ★★★ ─────────
            cursor.execute('''
                    CREATE TABLE IF NOT EXISTS WaferInformation (
                        Invoicenumber   TEXT    NOT NULL,
                        LotID           TEXT    NOT NULL,
                        Slot            INTEGER NOT NULL,
                        CouponSize      TEXT,
                        CouponPos       TEXT,
                        CouponX         REAL,
                        CouponY         REAL,
                        UsedRecipe      TEXT,
                        Purpose         TEXT,
                        Status          INTEGER,
                        DICD            REAL,

                        -- [신규] 모호성 해결을 위한 챔버 ID
                        chamber_id      TEXT, 

                        -- [신규] FK 링크. 이력 보존을 위해 ON DELETE SET NULL
                        used_recipe_id  INTEGER REFERENCES Recipe(id) ON DELETE SET NULL ON UPDATE CASCADE
                    );
                ''')
            # [신규] 빠른 조회를 위한 인덱스 추가
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_wafer_recipe_link ON WaferInformation(chamber_id, UsedRecipe, used_recipe_id);')

            # ───────── 기존 Recipe 고유성 트리거 (유지) ─────────
            cursor.executescript("""
                    CREATE TRIGGER IF NOT EXISTS trg_recipe_unique_chamber_ins
                    BEFORE INSERT ON Recipe
                    BEGIN
                      SELECT CASE
                        WHEN EXISTS (
                          SELECT 1
                            FROM Recipe r2
                            JOIN RecipClassification rc2 ON rc2.id = r2.classification_id
                           WHERE rc2.chamber_id = (SELECT chamber_id FROM RecipClassification WHERE id = NEW.classification_id)
                             AND r2.recipe_code = NEW.recipe_code COLLATE NOCASE
                        )
                        THEN RAISE(ABORT, 'DUP_RECIPE_CODE_IN_CHAMBER')
                      END;
                    END;

                    CREATE TRIGGER IF NOT EXISTS trg_recipe_unique_chamber_upd
                    BEFORE UPDATE OF classification_id, recipe_code ON Recipe
                    BEGIN
                      SELECT CASE
                        WHEN EXISTS (
                          SELECT 1
                            FROM Recipe r2
                            JOIN RecipClassification rc2 ON rc2.id = r2.classification_id
                           WHERE rc2.chamber_id = (SELECT chamber_id FROM RecipClassification WHERE id = NEW.classification_id)
                             AND r2.recipe_code = NEW.recipe_code COLLATE NOCASE
                             AND r2.id <> NEW.id
                        )
                        THEN RAISE(ABORT, 'DUP_RECIPE_CODE_IN_CHAMBER')
                      END;
                    END;
                """)

            # ───────── ★★★ 신규 동기화 트리거 3개 ★★★ ─────────
            cursor.executescript("""
                    -- 트리거 1: Recipe 이름 변경 시 -> WaferInfo 텍스트(UsedRecipe) 자동 업데이트
                    CREATE TRIGGER IF NOT EXISTS trg_recipe_rename_sync_text
                    AFTER UPDATE OF recipe_code ON Recipe
                    WHEN OLD.recipe_code != NEW.recipe_code
                    BEGIN
                        UPDATE WaferInformation
                        SET UsedRecipe = NEW.recipe_code
                        WHERE used_recipe_id = NEW.id;
                    END;

                    -- 트리거 2: WaferInfo 삽입 시 -> Recipe ID 찾아 자동 연결
                    CREATE TRIGGER IF NOT EXISTS trg_wafer_sync_id_ins
                    AFTER INSERT ON WaferInformation
                    WHEN NEW.chamber_id IS NOT NULL AND NEW.UsedRecipe IS NOT NULL
                    BEGIN
                        UPDATE WaferInformation
                        SET used_recipe_id = (
                            SELECT r.id FROM Recipe r
                            JOIN RecipClassification rc ON r.classification_id = rc.id
                            WHERE rc.chamber_id = NEW.chamber_id AND r.recipe_code = NEW.UsedRecipe
                            LIMIT 1
                        )
                        WHERE rowid = NEW.rowid;
                    END;

                    -- 트리거 3: WaferInfo 수정 시 -> Recipe ID 찾아 자동 연결 (INSERT와 로직 동일)
                    CREATE TRIGGER IF NOT EXISTS trg_wafer_sync_id_upd
                    AFTER UPDATE OF UsedRecipe, chamber_id ON WaferInformation
                    WHEN NEW.chamber_id IS NOT NULL AND NEW.UsedRecipe IS NOT NULL
                         -- (링크가 없었거나, 키가 변경되었을 때만)
                         AND (OLD.used_recipe_id IS NULL OR NEW.UsedRecipe != OLD.UsedRecipe OR NEW.chamber_id != OLD.chamber_id)
                    BEGIN
                        UPDATE WaferInformation
                        SET used_recipe_id = (
                            SELECT r.id FROM Recipe r
                            JOIN RecipClassification rc ON r.classification_id = rc.id
                            WHERE rc.chamber_id = NEW.chamber_id AND r.recipe_code = NEW.UsedRecipe
                            LIMIT 1
                        )
                        WHERE rowid = NEW.rowid;
                    END;

                    -- 트리거 4: Recipe 이름 변경 시 -> 자식 Recipe의 base_recipe 자동 업데이트
                    CREATE TRIGGER IF NOT EXISTS trg_recipe_base_update_cascade
                    AFTER UPDATE OF recipe_code ON Recipe
                    WHEN OLD.recipe_code != NEW.recipe_code
                    BEGIN
                        UPDATE Recipe
                        SET base_recipe = NEW.recipe_code
                        WHERE base_recipe = OLD.recipe_code;
                    END;
                """)

            conn.commit()

        except Exception as e:
            if conn:
                conn.rollback()
            # 파일 생성 실패 시, 불완전한 파일 삭제
            if db_path and os.path.exists(db_path):
                try:
                    if conn:
                        conn.close()
                        conn = None
                except Exception:
                    pass
                try:
                    os.remove(db_path)
                except Exception:
                    pass
            raise
        finally:
            if conn:
                conn.close()

    @retry_on_lock()
    def upsert_classification(self, proc_db_path: str, data: dict):
        """RecipClassification 정보를 INSERT 또는 REPLACE 합니다."""
        with self.get_connection(proc_db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO RecipClassification (chamber_id, sheet, schemeCode, Date)
                VALUES (?, ?, ?, ?)
            """, (data['chamber'], data['sheet'], data['scheme'], data['date_int']))

    def check_param_def_exists(self, chamber_id: str) -> bool:
        """해당 Chamber ID에 대한 ParameterDefinition이 존재하는지 확인합니다."""
        with self.get_connection(self.recipe_db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM ParameterDefinition WHERE chamber_id=? LIMIT 1", (chamber_id,))
            return cur.fetchone() is not None

    @retry_on_lock()
    def replace_param_defs_from_import(self, chamber_id: str, definitions: list[dict]):
        """
        기존 ParameterDefinition을 삭제하고 CSV에서 가져온 새 정의로 교체합니다.
        하나의 트랜잭션으로 처리됩니다.
        """
        with self.get_connection(self.recipe_db_path) as conn:
            cur = conn.cursor()

            # 1. 기존 정의 비활성화 (Soft Delete)
            cur.execute("UPDATE ParameterDefinition SET is_active=0 WHERE chamber_id=?", (chamber_id,))

            if definitions:
                # [변경] mapping, order, hide 제외하고 name, unit, is_active만 저장
                cur.executemany("""
                    INSERT INTO ParameterDefinition (chamber_id, name, unit, is_active)
                    VALUES (:chamber_id, :name, :unit, 1)
                """, definitions)

    def get_chamber_id_for_recipe(self, proc_db_path: str, recipe_id: int) -> str | None:
        """Recipe ID를 통해 해당 레시피가 속한 Chamber ID를 조회합니다."""
        with self.get_connection(proc_db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT rc.chamber_id
                  FROM Recipe r
                  JOIN RecipClassification rc ON r.classification_id = rc.id
                 WHERE r.id = ?
            """, (recipe_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def get_sheets_by_chamber(self, proc_db_path: str, chamber_id: str) -> list[str]:
        if not os.path.exists(proc_db_path):
            return []
        try:
            with self.get_connection(proc_db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT sheet FROM RecipClassification WHERE chamber_id=? ORDER BY sheet",
                            (chamber_id,))
                return [r[0] for r in cur.fetchall() if r[0]]
        except Exception as e:
            print(f"Error getting sheets by chamber: {e}")
            return []

    # [신규] 특정 Sheet의 상세 정보(Scheme, Date) 조회
    def get_sheet_info(self, proc_db_path: str, chamber_id: str, sheet: str) -> tuple | None:
        if not os.path.exists(proc_db_path): return None
        try:
            with self.get_connection(proc_db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT schemeCode, Date FROM RecipClassification WHERE chamber_id=? AND sheet=?",
                            (chamber_id, sheet))
                return cur.fetchone()  # tuple or None
        except Exception:
            return None

    # [신규] Sheet 삭제
    @retry_on_lock()
    def delete_sheet(self, proc_db_path: str, chamber_id: str, sheet: str):
        with self.get_connection(proc_db_path) as conn:
            # Recipe 테이블이 RecipClassification을 FK로 참조하므로 CASCADE 설정에 따라 자동 삭제되거나
            # 수동으로 지워야 함. 여기서는 명시적으로 Classification을 지움.
            conn.execute("DELETE FROM RecipClassification WHERE chamber_id=? AND sheet=?", (chamber_id, sheet))

    # [신규] Sheet 생성
    @retry_on_lock()
    def insert_classification(self, proc_db_path: str, chamber_id: str, sheet: str, scheme: str, date_int: int):
        with self.get_connection(proc_db_path) as conn:
            conn.execute("""
                INSERT INTO RecipClassification (chamber_id, sheet, schemeCode, Date)
                VALUES (?, ?, ?, ?)
            """, (chamber_id, sheet, scheme, date_int))

    # [신규] Sheet 내 레시피 존재 여부 확인
    def has_recipes_in_sheet(self, proc_db_path: str, chamber_id: str, sheet: str) -> bool:
        try:
            with self.get_connection(proc_db_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT 1 FROM Recipe r
                    JOIN RecipClassification rc ON r.classification_id = rc.id
                    WHERE rc.chamber_id = ? AND rc.sheet = ?
                    LIMIT 1
                """, (chamber_id, sheet))
                return cur.fetchone() is not None
        except Exception:
            return False

    @retry_on_lock()
    def update_parameter_values_batch(self, proc_db_path: str, updates: list):
        """
        여러 파라미터를 한 번의 트랜잭션으로 업데이트합니다.
        updates: [(value, recipe_id, param_id, step_no, step_name), ...]
        """
        if not updates: return

        # 1. Update 쿼리 정의
        update_sql = """
                UPDATE RecipeParameter SET value=?
                WHERE recipe_id=? AND parameter_id=? AND step_no=?
            """

        # 2. Insert 쿼리 정의 (일반적인 INSERT문 사용)
        insert_sql = """
                INSERT INTO RecipeParameter (recipe_id, parameter_id, step, step_no, value)
                VALUES (?, ?, ?, ?, ?)
            """

        with self.get_connection(proc_db_path) as conn:
            cur = conn.cursor()
            try:
                for val, rid, pid, sno, sname in updates:
                    # A. 먼저 Update 시도
                    cur.execute(update_sql, (val, rid, pid, sno))

                    # B. Update된 행이 0개라면 (데이터가 없다는 뜻) Insert 수행
                    if cur.rowcount == 0:
                        cur.execute(insert_sql, (rid, pid, sname, sno, val))

            except Exception:
                raise

    @retry_on_lock()
    def shift_step_numbers(self, proc_db_path: str, recipe_id: int, start_after_step: int, shift_amount: int):
        """
        특정 스텝 이후의 모든 스텝 번호를 shift_amount 만큼 뒤로 밉니다.
        (중간에 새 스텝을 끼워넣기 위함)
        """

        if shift_amount <= 0: return

        with self.get_connection(proc_db_path) as conn:
            cur = conn.cursor()

            # 1단계: 이동 대상 스텝들을 임시로 음수로 변경하여 충돌 회피
            # 예: Step 3, 4, 5 -> Step -3, -4, -5
            # (step_no가 유니크 키의 일부이므로, 서로 다른 음수로 변환되면 충돌하지 않음)
            cur.execute("""
                    UPDATE RecipeParameter
                       SET step_no = -step_no
                     WHERE recipe_id = ? 
                       AND step_no > ?
                """, (recipe_id, start_after_step))

            # 2단계: 음수로 변한 스텝들을 최종 목적지(양수)로 이동
            # 공식: 최종값 = (원래값) + shift = (-현재음수값) + shift
            cur.execute("""
                    UPDATE RecipeParameter
                       SET step_no = (-step_no) + ?
                     WHERE recipe_id = ? 
                       AND step_no < 0  -- 음수로 바뀐 것들만 대상
                """, (shift_amount, recipe_id))

    @retry_on_lock()
    def insert_transition_params(self, proc_db_path: str, params_list: list):
        """계산된 Transition 파라미터들을 일괄 Insert (aux_value 포함)"""
        if not params_list: return

        # params_list 구조: [(recipe_id, param_id, step_name, step_no, value, aux_value), ...]
        with self.get_connection(proc_db_path) as conn:
            conn.executemany("""
                    INSERT INTO RecipeParameter (recipe_id, parameter_id, step, step_no, value, aux_value)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, params_list)

    def get_raw_param_defs(self, chamber_id: str) -> list[dict]:
        with self.get_connection(self.recipe_db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, unit, is_active FROM ParameterDefinition WHERE chamber_id=?", (chamber_id,))
            return [{'pid': r[0], 'name': r[1], 'unit': r[2], 'is_active': r[3]} for r in cur.fetchall()]

    @retry_on_lock()
    def apply_parameter_import_changes(self, chamber_id: str,
                                       matched_updates: list,
                                       mapped_pairs: list,
                                       final_new: list,
                                       final_legacy: list,
                                       process_name: str):
        """
        [수정] Master 테이블과 Config 테이블 업데이트를 '단일 트랜잭션'으로 통합하여
        네트워크 환경에서의 데이터 누락 및 동기화 문제를 해결합니다.
        """

        # Display Config 업데이트를 위한 리스트 준비
        config_inserts = []

        with self.get_connection(self.recipe_db_path) as conn:
            cur = conn.cursor()

            # 1. Matched: 활성화 (ID 유지)
            # new_d에는 UI에서 결정된 mapping, order 정보가 들어있음
            sql_match = "UPDATE ParameterDefinition SET is_active = 1 WHERE id = ?"
            for new_d, pid in matched_updates:
                cur.execute(sql_match, (pid,))
                if process_name:
                    config_inserts.append({
                        'chamber_id': chamber_id,
                        'process_name': process_name,
                        'pid': pid,
                        'mapping': new_d.get('mapping', new_d['name']),
                        'hide': 0,
                        'order': new_d['order']
                    })

            # 2. Mapped: 이름 변경 및 활성화 (ID 유지)
            sql_map = "UPDATE ParameterDefinition SET name = ?, unit = ?, is_active = 1 WHERE id = ?"
            for db_item, new_item in mapped_pairs:
                pid = db_item['pid']
                cur.execute(sql_map, (new_item['name'], new_item['unit'], pid))
                if process_name:
                    config_inserts.append({
                        'chamber_id': chamber_id,
                        'process_name': process_name,
                        'pid': pid,
                        'mapping': new_item.get('mapping', new_item['name']),
                        'hide': 0,
                        'order': new_item['order']
                    })

            # 3. New: 신규 삽입 (핵심 수정 부분)
            # INSERT 후 lastrowid를 바로 가져와서 Config에 추가합니다. (Select 불필요)
            sql_insert = "INSERT INTO ParameterDefinition (chamber_id, name, unit, is_active) VALUES (?, ?, ?, 1)"
            for item in final_new:
                cur.execute(sql_insert, (chamber_id, item['name'], item['unit']))
                new_pid = cur.lastrowid  # 방금 생성된 ID 획득

                if process_name:
                    config_inserts.append({
                        'chamber_id': chamber_id,
                        'process_name': process_name,
                        'pid': new_pid,  # 획득한 ID 사용
                        'mapping': item.get('mapping', item['name']),
                        'hide': 0,
                        'order': item['order']
                    })

            # 4. Legacy: 비활성화
            sql_delete = "UPDATE ParameterDefinition SET is_active = 0 WHERE id = ?"
            for item in final_legacy:
                cur.execute(sql_delete, (item['pid'],))

            # 5. Display Config 일괄 저장 (같은 커넥션 사용)
            # update_param_defs_batch 메서드를 호출하면 새로운 커넥션을 맺으므로,
            # 여기서는 쿼리를 직접 실행해야 합니다.
            if config_inserts:
                sql_config = """
                        INSERT OR REPLACE INTO ParameterDisplayConfig 
                        (chamber_id, process_name, param_id, mapping, hide, "order")
                        VALUES (:chamber_id, :process_name, :pid, :mapping, :hide, :order)
                    """
                cur.executemany(sql_config, config_inserts)
            # 컨텍스트 종료 시 자동 commit

    # [신규] 삭제 전 레시피 데이터 백업용
    def get_recipe_snapshot(self, proc_db_path: str, recipe_id: int) -> dict:
        """레시피의 메타데이터와 파라미터 전체를 백업합니다."""
        with self.get_connection(proc_db_path) as conn:
            cur = conn.cursor()
            # 1. Recipe Metadata
            cur.execute("""
                SELECT classification_id, recipe_code, base_recipe, created_by, created_at, comment
                  FROM Recipe WHERE id = ?
            """, (recipe_id,))
            meta = cur.fetchone()

            # 2. Parameters (aux_value 포함 — Ramp 시작값 보존)
            cur.execute("""
                SELECT parameter_id, step, step_no, value, aux_value
                  FROM RecipeParameter WHERE recipe_id = ?
            """, (recipe_id,))
            params = cur.fetchall()

        return {"meta": meta, "params": params}

    # [신규] 삭제 전 스텝 데이터 백업용
    def get_step_snapshot(self, proc_db_path: str, recipe_id: int, step_name: str) -> list:
        """특정 스텝의 파라미터 값들을 백업합니다."""
        with self.get_connection(proc_db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT parameter_id, step, step_no, value, aux_value
                  FROM RecipeParameter
                 WHERE recipe_id = ? AND step = ?
            """, (recipe_id, step_name))
            return cur.fetchall()

    # [신규] 레시피 복구 (Undo Delete Recipe)
    @retry_on_lock()
    def restore_recipe(self, proc_db_path: str, snapshot: dict):
        """백업된 데이터로 레시피를 다시 생성합니다."""
        meta = snapshot['meta']
        params = snapshot['params']

        # meta: (cls_id, code, base, creator, created_at, comment)
        with self.get_connection(proc_db_path) as conn:
            cur = conn.cursor()
            # 1. Recipe 복구 (ID는 자동생성되지만, 내용은 동일)
            cur.execute("""
                INSERT INTO Recipe (classification_id, recipe_code, base_recipe, created_by, created_at, updated_at, comment)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (meta[0], meta[1], meta[2], meta[3], meta[4], meta[4], meta[5]))  # updated_at도 created_at으로 복구

            new_rid = cur.lastrowid

            # 2. Params 복구 (aux_value 포함 — Ramp 시작값 보존)
            if params:
                inserts = [(new_rid, p[0], p[1], p[2], p[3], p[4]) for p in params]
                cur.executemany("""
                    INSERT INTO RecipeParameter (recipe_id, parameter_id, step, step_no, value, aux_value)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, inserts)

            # 트리거에 의해 WaferInfo 링크 등은 자동으로 처리됨

    # [신규] 스텝 복구 (Undo Delete Step)
    @retry_on_lock()
    def restore_step_params(self, proc_db_path: str, recipe_id: int, params: list):
        """백업된 파라미터 리스트를 다시 삽입합니다."""
        if not params: return
        with self.get_connection(proc_db_path) as conn:
            # params: (pid, step, step_no, value, aux_value)
            inserts = [(recipe_id, p[0], p[1], p[2], p[3], p[4]) for p in params]
            conn.executemany("""
                INSERT INTO RecipeParameter (recipe_id, parameter_id, step, step_no, value, aux_value)
                VALUES (?, ?, ?, ?, ?, ?)
            """, inserts)

    # [신규] Sheet 이름 변경
    @retry_on_lock()
    def rename_classification_sheet(self, proc_db_path: str, chamber_id: str, old_sheet: str, new_sheet: str):
        """특정 Sheet의 이름을 변경합니다."""
        with self.get_connection(proc_db_path) as conn:
            # 중복 체크는 Service나 Trigger, 혹은 여기서 IntegrityError로 잡을 수 있지만
            # Service에서 미리 체크하고 들어오는 것이 깔끔함.
            conn.execute("""
                UPDATE RecipClassification 
                   SET sheet = ? 
                 WHERE chamber_id = ? AND sheet = ?
            """, (new_sheet, chamber_id, old_sheet))

    # [신규] 파라미터 정의가 이미 존재하는 챔버 ID 목록 조회 (UI 색상 표시용)
    def get_chambers_with_definitions(self) -> set:
        with self.get_connection(self.recipe_db_path) as conn:
            cur = conn.cursor()
            # is_active=1인 파라미터가 하나라도 있는 챔버 조회
            cur.execute("SELECT DISTINCT chamber_id FROM ParameterDefinition WHERE is_active=1")
            return {row[0] for row in cur.fetchall()}

    # [신규] Sheet 생성 시, 해당 Process/Chamber에 대한 Display Config가 없으면 Master에서 복사
    @retry_on_lock()
    def sync_initial_display_config(self, chamber_id: str, process_name: str):
        if not chamber_id or not process_name: return

        with self.get_connection(self.recipe_db_path) as conn:
            # 이미 설정이 존재하는지 확인 (중복 초기화 방지)
            cur = conn.cursor()
            cur.execute("""
                SELECT 1 FROM ParameterDisplayConfig 
                WHERE chamber_id=? AND process_name=? LIMIT 1
            """, (chamber_id, process_name))
            if cur.fetchone():
                return  # 이미 설정이 있으면 건너뜀

            # 설정이 없으면 Master(ParameterDefinition)에서 복사하여 초기화
            # 순서(Order)는 Master의 Order를 따름
            cur.execute("""
                INSERT INTO ParameterDisplayConfig (chamber_id, process_name, param_id, mapping, hide, "order")
                SELECT chamber_id, ?, id, name, 0, "order"
                  FROM ParameterDefinition
                 WHERE chamber_id = ? AND is_active = 1
            """, (process_name, chamber_id))

    def get_configured_process_names(self) -> set:
        """
        [신규] Recipe.db에 설정(Sheet/Parameter)이 등록된 프로세스 이름 목록을 반환합니다.
        개별 파일을 열지 않고도 '유효한 프로세스'를 식별하는 인덱스 역할을 합니다.
        """
        try:
            with self.get_connection(self.recipe_db_path) as conn:
                cur = conn.cursor()
                # Config 테이블에 process_name이 있다는 것은 Sheet가 생성되었다는 의미
                cur.execute("SELECT DISTINCT process_name FROM ParameterDisplayConfig")
                return {row[0] for row in cur.fetchall() if row[0]}
        except Exception:
            return set()