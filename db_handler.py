# db_handler.py
import uuid
from typing import Any, Iterable, Optional, List

import streamlit as st
import pandas as pd

from google.cloud.sql.connector import Connector, IPTypes
from google.oauth2 import service_account


# ───────────────────────────────────────────────────────────────
# Session + cached resources
# ───────────────────────────────────────────────────────────────
def _session_key() -> str:
    if "_session_key" not in st.session_state:
        st.session_state["_session_key"] = uuid.uuid4().hex
    return st.session_state["_session_key"]


@st.cache_resource(show_spinner=False)
def _get_credentials():
    if "gcp_service_account" not in st.secrets:
        raise RuntimeError("Missing [gcp_service_account] in secrets.toml")
    return service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"])
    )


@st.cache_resource(show_spinner=False)
def _get_connector():
    return Connector(credentials=_get_credentials())


def _ip_type_from_secret(val: Optional[str]) -> IPTypes:
    return IPTypes.PRIVATE if (val or "").strip().upper() == "PRIVATE" else IPTypes.PUBLIC


@st.cache_resource(show_spinner=False)
def _get_conn_for_session(
    session_key: str,
    instance_connection_name: str,
    user: str,
    password: str,
    db: str,
    ip_type: IPTypes,
):
    """
    One DB-API connection per Streamlit session, created via Cloud SQL Connector using pg8000.
    """
    connector = _get_connector()
    conn = connector.connect(
        instance_connection_name,
        "pg8000",  # IMPORTANT: pg8000 (psycopg2 isn't supported by the connector)
        user=user,
        password=password,
        db=db,
        ip_type=ip_type,
    )

    try:
        st.on_session_end(lambda: _safe_close(conn, connector))
    except Exception:
        pass

    return conn  # pg8000 autocommit is False by default


def _safe_close(conn, connector: Connector):
    try:
        if conn and not getattr(conn, "closed", False):
            conn.close()
    except Exception:
        pass
    try:
        connector.close()
    except Exception:
        pass


# ───────────────────────────────────────────────────────────────
# Database Manager (Cloud SQL + pg8000)
# ───────────────────────────────────────────────────────────────
class DatabaseManager:
    """
    Uses Cloud SQL Connector (driver=pg8000). Reads:
      [cloudsql] instance_connection_name, user, password, db, ip_type
      [gcp_service_account] full service account JSON
    """

    def __init__(self):
        if "cloudsql" not in st.secrets:
            raise RuntimeError("Missing [cloudsql] in secrets.toml")

        cfg = st.secrets["cloudsql"]
        self._instance_connection_name = cfg.get("instance_connection_name")
        self._user = cfg.get("user")
        self._password = cfg.get("password")
        self._db = cfg.get("db")
        self._ip_type = _ip_type_from_secret(cfg.get("ip_type", "PUBLIC"))

        if not all([self._instance_connection_name, self._user, self._password, self._db]):
            raise RuntimeError(
                "cloudsql secrets must include instance_connection_name, user, password, db"
            )

        self._sess_key = _session_key()
        self.conn = _get_conn_for_session(
            self._sess_key,
            self._instance_connection_name,
            self._user,
            self._password,
            self._db,
            self._ip_type,
        )

    # ────────── internals ──────────
    def _reconnect(self):
        _get_conn_for_session.clear()
        self.conn = _get_conn_for_session(
            self._sess_key,
            self._instance_connection_name,
            self._user,
            self._password,
            self._db,
            self._ip_type,
        )

    def _ensure_live_conn(self):
        # Cheap liveness check without context manager
        try:
            cur = self.conn.cursor()
            try:
                cur.execute("SELECT 1")
                cur.fetchone()
            finally:
                cur.close()
        except Exception:
            self._reconnect()

    def _fetch_df(self, query: str, params: Optional[Iterable[Any]] = None) -> pd.DataFrame:
        self._ensure_live_conn()
        try:
            cur = self.conn.cursor()
            try:
                cur.execute(query, params or ())
                rows = cur.fetchall()
                cols = [c[0] for c in cur.description]
            finally:
                cur.close()
            return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame()
        except Exception:
            # rollback & retry once on any dbapi error
            try:
                self.conn.rollback()
            except Exception:
                pass
            self._reconnect()
            cur = self.conn.cursor()
            try:
                cur.execute(query, params or ())
                rows = cur.fetchall()
                cols = [c[0] for c in cur.description]
            finally:
                cur.close()
            return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame()

    def _execute(
        self,
        query: str,
        params: Optional[Iterable[Any]] = None,
        returning: bool = False,
    ):
        self._ensure_live_conn()
        try:
            cur = self.conn.cursor()
            try:
                cur.execute(query, params or ())
                res = cur.fetchone() if returning else None
            finally:
                cur.close()
            self.conn.commit()
            return res
        except Exception:
            # rollback & retry once
            try:
                self.conn.rollback()
            except Exception:
                pass
            self._reconnect()
            cur = self.conn.cursor()
            try:
                cur.execute(query, params or ())
                res = cur.fetchone() if returning else None
            finally:
                cur.close()
            self.conn.commit()
            return res

    # ────────── public API ──────────
    def fetch_data(self, query: str, params: Optional[Iterable[Any]] = None) -> pd.DataFrame:
        return self._fetch_df(query, params)

    def execute_command(self, query: str, params: Optional[Iterable[Any]] = None) -> None:
        self._execute(query, params, returning=False)

    def execute_command_returning(
        self, query: str, params: Optional[Iterable[Any]] = None
    ):
        return self._execute(query, params, returning=True)

    # ─────────── Dropdown Management ───────────
    def get_all_sections(self) -> List[str]:
        df = self.fetch_data("SELECT DISTINCT section FROM dropdowns")
        return df["section"].tolist() if not df.empty else []

    def get_dropdown_values(self, section: str) -> List[str]:
        q = "SELECT value FROM dropdowns WHERE section = %s"
        df = self.fetch_data(q, (section,))
        return df["value"].tolist() if not df.empty else []

    # ─────────── Supplier Management ───────────
    def get_suppliers(self) -> pd.DataFrame:
        return self.fetch_data("SELECT supplierid, suppliername FROM supplier")

    # ─────────── Inventory Management ───────────
    def add_inventory(self, data: dict):
        cols = ", ".join(data.keys())
        ph   = ", ".join(["%s"] * len(data))
        q = f"INSERT INTO inventory ({cols}) VALUES ({ph})"
        self.execute_command(q, list(data.values()))

    # ─────────── Foreign Key Helper ───────────
    def check_foreign_key_references(
        self,
        referenced_table: str,
        referenced_column: str,
        value: Any,
    ) -> List[str]:
        fk_sql = """
            SELECT tc.table_schema,
                   tc.table_name
            FROM   information_schema.table_constraints AS tc
            JOIN   information_schema.key_column_usage AS kcu
                   ON tc.constraint_name = kcu.constraint_name
            JOIN   information_schema.constraint_column_usage AS ccu
                   ON ccu.constraint_name = tc.constraint_name
            WHERE  tc.constraint_type = 'FOREIGN KEY'
              AND  ccu.table_name      = %s
              AND  ccu.column_name     = %s;
        """
        fks = self.fetch_data(fk_sql, (referenced_table, referenced_column))

        conflicts: List[str] = []
        for _, row in fks.iterrows():
            schema = row["table_schema"]
            table  = row["table_name"]

            exists_sql = f"""
                SELECT EXISTS(
                    SELECT 1
                    FROM   {schema}.{table}
                    WHERE  {referenced_column} = %s
                );
            """
            exists_df = self.fetch_data(exists_sql, (value,))
            exists = bool(exists_df.iat[0, 0]) if not exists_df.empty else False
            if exists:
                conflicts.append(f"{schema}.{table}")

        return sorted(set(conflicts))
