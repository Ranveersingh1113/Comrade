"""Apply committed SQL migrations before a production release is declared ready."""
from pathlib import Path

import psycopg

from shared.config import settings


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
    root = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
    ran = apply(root, settings.comrade_db_url_admin)
    print("applied migrations: " + (", ".join(ran) if ran else "none"))


if __name__ == "__main__":
    main()
