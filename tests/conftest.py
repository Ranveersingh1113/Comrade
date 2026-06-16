import psycopg
import pytest

from shared.config import settings
from tests._seed import cleanup, seed


@pytest.fixture
def seeded():
    """Fresh seed per test; cleaned up after (committed worker writes included)."""
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cleanup(cur)   # idempotent
            seed(cur)
        yield
        with conn.cursor() as cur:
            cleanup(cur)
    finally:
        conn.close()
