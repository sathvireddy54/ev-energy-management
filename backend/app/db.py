"""
Database connection for the data that must survive a restart: accounts and sessions, battery readings and trips,
driver reports and the adaptive-learning readings.

    DATABASE_URL not set  -> SQLite file (data/records.db), as before - for running on your own computer.
    DATABASE_URL set      -> PostgreSQL (e.g. a free Neon database) - for hosting where local files are wiped on
                             every restart (Render free plan).

The services keep their SQLite-style SQL ("?" placeholders, AUTOINCREMENT); for PostgreSQL it is translated here,
so the same code runs on both. Short-lived data that is rebuilt on every start (the simulated charger feeds and
tariffs) stays in the local SQLite file in both cases.
"""
import os
import re
import sqlite3
import threading

URL = (os.getenv("DATABASE_URL") or "").strip()
_pool = None
_pool_lock = threading.Lock()
_ddl_done = set()
_wal = set()


def using_postgres():
    return bool(URL)


def connect(path):
    """A connection with sqlite3-style use: c.execute(sql, params).fetchone(), dict(row), row["col"], row[0],
    cur.lastrowid, cur.rowcount, `with connect(path) as c:` commits."""
    if not URL:
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        if path not in _wal:                       # write-ahead log: readers don't wait for writers (once per file)
            try:
                c.execute("PRAGMA journal_mode=WAL")
                _wal.add(path)
            except sqlite3.Error:
                pass
        return c
    return _PgConn(_get_pool())


# ------------------------------------------------------------------ PostgreSQL
def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool
            _pool = ConnectionPool(URL, min_size=1, max_size=5, kwargs={"autocommit": False},
                                   configure=_configure, open=True, timeout=30)
            import atexit
            atexit.register(_pool.close)
    return _pool


def _configure(conn):
    # send every value untyped, so PostgreSQL types it from the column - as SQLite does
    # (e.g. a station id given as a number is compared with a TEXT column)
    from psycopg.adapt import Dumper
    from psycopg.types.string import StrDumperUnknown

    class _Num(Dumper):
        oid = 0

        def dump(self, obj):
            if isinstance(obj, bool):
                return b"1" if obj else b"0"
            if isinstance(obj, float) and obj.is_integer():
                return str(int(obj)).encode()
            return repr(obj).encode() if isinstance(obj, float) else str(obj).encode()

    conn.adapters.register_dumper(str, StrDumperUnknown)
    for t in (int, float, bool):
        conn.adapters.register_dumper(t, _Num)
    conn.commit()


def _translate(sql):
    sql = sql.replace("%", "%%").replace("?", "%s")
    sql = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", sql, flags=re.I)
    return sql


class _Row(dict):
    """dict row that also allows row[0] like sqlite3.Row."""
    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return super().__getitem__(k)


class _PgCursor:
    def __init__(self, cur):
        self._cur = cur
        self.rowcount = cur.rowcount
        self._last = None

    def _row(self, r):
        return None if r is None else _Row(zip([d.name for d in self._cur.description], r))

    def fetchone(self):
        return self._row(self._cur.fetchone()) if self._cur.description else None

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()] if self._cur.description else []

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def lastrowid(self):
        if self._last is None:
            self._cur.execute("SELECT lastval()")
            self._last = self._cur.fetchone()[0]
        return self._last


class _PgConn:
    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()

    def execute(self, sql, params=()):
        ddl = sql.lstrip().upper().startswith(("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS"))
        if ddl and sql in _ddl_done:                 # tables are created once per process
            return _PgCursor(self._conn.cursor())
        cur = self._conn.cursor()
        cur.execute(_translate(sql), tuple(params))
        if ddl:
            self._conn.commit()
            _ddl_done.add(sql)
        return _PgCursor(cur)

    def executemany(self, sql, seq):
        cur = self._conn.cursor()
        cur.executemany(_translate(sql), [tuple(p) for p in seq])
        return _PgCursor(cur)

    def commit(self):
        self._conn.commit()

    def close(self):
        if self._conn is not None:
            try:
                self._conn.rollback()
            finally:
                self._pool.putconn(self._conn)
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._pool.putconn(self._conn)
            self._conn = None
        return False

    def __del__(self):
        try:
            if self._conn is not None:
                self._pool.putconn(self._conn)
        except Exception:
            pass
