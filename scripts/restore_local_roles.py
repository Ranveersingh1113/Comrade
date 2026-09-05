"""Re-create the local worker LOGIN roles after `supabase db reset`.

🔴 `supabase db reset` drops `comrade_authenticator` and the passwords on the
other three. The local setup instructions say "after a db reset" next to this
step, but the merge
gate did not, so `--with-reset` reported a wall of failures that looked like 45
migrations had broken the schema. They had not: the migrations applied cleanly
and the suite could simply no longer log in.

A documented manual step that an automated check forgets is the same class of
gap as a handler registered by import that `main()` never imports. Both are a
second place that has to agree, and neither fails where it is caused.

The passwords are read from the role URLs already in .env — they are the same
secrets, and asking for them twice is how the two drift apart. Nothing here
prints one.
"""
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
CONTAINER = "supabase_db_Comrade"

#: psql variable name -> the setting holding a URL with that password in it.
_ROLES = {
    "agent_pwd": "comrade_agent_db_url",
    "executor_pwd": "comrade_executor_db_url",
    "pipeline_pwd": "comrade_pipeline_db_url",
    "control_pwd": "comrade_control_db_url",
    "authenticator_pwd": "comrade_authenticator_db_url",
}


def _password(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.password:
        raise SystemExit(
            f"no password in {parsed.scheme}://{parsed.username or '?'}@… —"
            " the local role URLs in .env must carry one for this to run."
        )
    return unquote(parsed.password)


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from shared.config import settings

    args: list[str] = []
    for var, field in _ROLES.items():
        url = getattr(settings, field, "")
        if not url:
            raise SystemExit(
                f"{field.upper()} is not set; cannot restore the local roles."
            )
        args += ["-v", f"{var}={_password(url)}"]

    sql = (ROOT / "scripts" / "setup_local_roles.sql").read_text(encoding="utf-8")
    proc = subprocess.run(  # noqa: S603 - fixed argv, never a shell string
        ["docker", "exec", "-i", CONTAINER,
         "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres",
         *args, "-f", "-"],
        input=sql.encode("utf-8"), capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        # Redacted: psql echoes the failing statement, and these statements
        # contain the passwords we just passed in.
        err = proc.stderr.decode("utf-8", "replace")
        for var, field in _ROLES.items():
            err = err.replace(_password(getattr(settings, field)), "<redacted>")
        sys.stderr.write(err[:800] + "\n")
        return proc.returncode
    print("local worker LOGIN roles restored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
