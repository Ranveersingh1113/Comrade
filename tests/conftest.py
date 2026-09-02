import os

import psycopg
import pytest

from shared.config import settings
from tests._seed import cleanup, seed

# Git for Windows ships `core.fsmonitor = true` in its SYSTEM gitconfig, so
# every git command in every repository starts a `git fsmonitor--daemon
# --detach`: a background process that outlives the command by design and
# holds ~37MB of commit charge.
#
# The repo tests build a fresh git repository per test in a fresh tmp_path, so
# that is one abandoned daemon per test, and the directory they were watching
# is deleted out from under them. 418 accumulated here in two hours -- 15GB of
# commit charge -- until the machine sat half a gigabyte under its commit
# limit and an unrelated pytest run died with a MemoryError that pointed
# nowhere near git.
#
# The GIT_CONFIG_* environment form reaches EVERY git subprocess, including
# the plain `git init` calls in fixtures. Production code carries the same
# setting explicitly as repo_sync.GIT_FLAGS, since Comrade's process must not
# depend on an environment variable for this.
os.environ["GIT_CONFIG_COUNT"] = "1"
os.environ["GIT_CONFIG_KEY_0"] = "core.fsmonitor"
os.environ["GIT_CONFIG_VALUE_0"] = "false"


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
