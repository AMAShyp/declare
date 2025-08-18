# db_handler.py
import uuid
import time
import traceback
from typing import Any, Iterable, Optional, List

import streamlit as st
import pandas as pd
import psycopg2
from psycopg2 import OperationalError, InterfaceError, DatabaseError

from google.cloud.sql.connector import Connector, IPTypes
from google.oauth2 import service_account


# ───────────────────────────────────────────────────────────────
# 0) Helpers: session identity + cached resources
# ───────────────────────────────────────────────────────────────
def _session_key() -> str:
    """Unique key for current Streamlit user session."""
    if "_session_key" not in st.session_state:
        st.session_state["_session_key"] = uuid.uuid4().hex
    return st.session_state["_session_key"]


@st.cache_resource(show_spinner=False)
def _get_credentials():
    """Build Google service account credentials from Streamlit secrets."""
    if "gcp_service_account" not in st.secrets:
        raise RuntimeError("Missing [gcp_service_account] in secrets.toml")
    return service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"])
    )


@st.cache_resource(show_spinner=False)
def _get_connector():
    """Create a single Cloud SQL Connector instance for the process."""
    creds = _get_credentials()
    return Connector(credentials=creds)


def _ip_type_from_secret(val: Optional[str]) -> IPTypes:
    if (val or "").strip().upper() == "PRIVATE":
        return IPTypes.PRIVATE
    return IPTypes.PUBLIC


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
    One psycopg2 connection per Streamlit session, created via Cloud SQL Connector.
    """
    connector = _get_connector()

    conn = connector.connect(
        instance_connection_name,
        "psycopg2",
        user=user,
        password=password,
        db=db,
        ip_type=ip_type,
    )

    # Graceful cleanup when the Streamlit session ends.
    try:
        st.on_session_end(lambda: _safe_close(conn, connector))
    except Exception:
        pass

    # psycopg2 default is autocommit = False → keep it explicit
    conn.autocommit = False
    return conn


def _safe_close(conn, connector: Connector):
    try:
        if conn and getattr(conn, "closed", 1) == 0:
            conn.close()
    except Exception:
        pass
    try:
        connector.close()
    except Exception:
        pass


# ───────────────────────────────────────────────────────────────
# 1) Database Manager (Cloud SQL + psycopg2) with auto-reconnect
# ───────────────────────────────────────────────────────────────
class DatabaseManager:
    """
    Rewritten to use Cloud SQL (PostgreSQL) via the Cloud SQL Python Connector.
    Reads config from:
      [cloudsql]
        instance_connection_name, user, password, db, ip_type (PUBLIC|PRIVATE)
      [gcp_service_account]
        ... full service account json fields ...

    Public API unchanged:
      - fetch_data(query, params=None) -> DataFrame
      - execute_command(query, params=None) -> None
      - execute_command_returning(query, params=None) -> tuple | None
      - get_all_sections(), get_dropdown_values(section), get_suppliers(), add_inventory(data)
      - check_foreign_key_references(referenced_table, referenced_column, value) -> list[str]
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

    # ────────── internal helpers ──────────
    def _ensure_live_conn(self):
        """
        Ensure connection is open; if not, rebuild it for this session.
        """
        try:
            if getattr(self.conn, "closed", 1) != 0:
                _get_conn_for_session.clear()  # clear cached resource for this session
                self.conn = _get_conn_for_session(
                    self._sess_key,
                    self._instance_connection_name,
                    self._user,
                    self._password,
                    self._db,
                    self._ip_type,
                )
            else:
                # Cheap liveness check: cursor() and NOOP
                with self.conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    cur.fetchone()
        except (OperationalError, InterfaceError):
            _get_conn_for_session.clear()
            self.conn = _get_conn_for_session(
                self._sess_key,
                self._instance_connection_name,
                self._user,
                self._password,
                self._db,
                self._ip_type,
            )

    def _fetch_df(self, query: str, params: Optional[Iterable[Any]] = None) -> pd.DataFrame:
        """
        Execute a SELECT and return DataFrame.
        Auto-reconnects on transient connection errors. Rolls back on failure.
        """
        self._ensure_live_conn()
        try:
            with self.conn.cursor() as cur:
                cur.execute(query, params or ())
                rows = cur.fetchall()
                cols = [c[0] for c in cur.description]
            return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame()
        except (OperationalError, InterfaceError):
            # Reconnect and retry once
            _get_conn_for_session.clear()
            self.conn = _get_conn_for_session(
                self._sess_key,
                self._instance_connection_name,
                self._user,
                self._password,
                self._db,
                self._ip_type,
            )
            with self.conn.cursor() as cur:
                cur.execute(query, params or ())
                rows = cur.fetchall()
                cols = [c[0] for c in cur.description]
            return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame()
        except Exception:
            self.conn.rollback()
            raise

    def _execute(
        self,
        query: str,
        params: Optional[Iterable[Any]] = None,
        returning: bool = False,
    ):
        """
        Execute INSERT/UPDATE/DELETE. If returning=True, fetchone() is returned.
        """
        self._ensure_live_conn()
        try:
            with self.conn.cursor() as cur:
                cur.execute(query, params or ())
                res = cur.fetchone() if returning else None
            self.conn.commit()
            return res
        except (OperationalError, InterfaceError):
            # Reconnect and retry once
            _get_conn_for_session.clear()
            self.conn = _get_conn_for_session(
                self._sess_key,
                self._instance_connection_name,
                self._user,
                self._password,
                self._db,
                self._ip_type,
            )
            with self.conn.cursor() as cur:
                cur.execute(query, params or ())
                res = cur.fetchone() if returning else None
            self.conn.commit()
            return res
        except Exception:
            self.conn.rollback()
            raise

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
        """
        Return a list of tables that still reference the given value
        through a FOREIGN KEY constraint.
        Empty list → safe to delete.
        """
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
