from pathlib import Path


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "schema_migrations" in sql and "select version" in sql:
            return _Cursor([("20260101000000",)])
        return _Cursor([])

    def commit(self):
        self.calls.append(("commit", None))

    def close(self):
        self.calls.append(("close", None))


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


def test_apply_pending_migrations_in_filename_order(tmp_path, monkeypatch):
    from shared import migrations

    (tmp_path / "20260101000000_first.sql").write_text("select 1;")
    (tmp_path / "20260102000000_second.sql").write_text("select 2;")
    conn = _Connection()
    monkeypatch.setattr(migrations.psycopg, "connect", lambda _: conn)

    assert migrations.apply(tmp_path, "postgresql://migration-owner") == [
        "20260102000000"
    ]
    assert ("select 2;", None) in conn.calls
    assert any(
        params == ("20260102000000", ["select 2;"], "second")
        for _, params in conn.calls
    )
    assert conn.calls[-2:] == [("commit", None), ("close", None)]
