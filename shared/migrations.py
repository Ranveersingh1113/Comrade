"""Apply committed SQL migrations before a production release is declared ready."""
from pathlib import Path

import psycopg

from shared.config import settings
from shared.db import allow_table_owner


def apply(root: Path, database_url: str) -> list[str]:
    """Apply every missing migration in filename order, atomically."""
    files = sorted(root.glob("*.sql"))
    conn = psycopg.connect(database_url)
    try:
        applied = {
            row[0]
            for row in conn.execute(
                "select version from supabase_migrations.schema_migrations"
            ).fetchall()
        }
        ran: list[str] = []
        for path in files:
            version, _, name = path.stem.partition("_")
            if not version or version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            conn.execute(sql)
            conn.execute(
                "insert into supabase_migrations.schema_migrations"
                " (version, statements, name) values (%s, %s, %s)",
                (version, [sql], name),
            )
            ran.append(version)
        conn.commit()
        return ran
    finally:
        conn.close()


def main() -> None:
    """The one-off migration service.

    This is the only entrypoint that borrows the table owner, and it says so
    out loud. Everything that serves a request leaves `allow_table_owner`
    uncalled and cannot reach the credential through shared.db even if the
    variable is set in its environment.
    """
    allow_table_owner()
    root = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
    url = settings.comrade_db_url_admin
    if not url:
        raise SystemExit(
            "COMRADE_DB_URL_ADMIN is not set. Migrations run as the table"
            " owner; give it to this one-off job and to nothing else."
        )
    ran = apply(root, url)
    print("applied migrations: " + (", ".join(ran) if ran else "none"))


if __name__ == "__main__":
    main()
