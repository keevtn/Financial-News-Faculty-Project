"""
check_redis.py
==============
One-command Redis connectivity diagnostic (Phase 3 prerequisite).

Run from the project root:
    .\\.venv\\Scripts\\python.exe backend\\check_redis.py

Reads REDIS_URI from .env, attempts AUTH+PING with the URI as written, and if
the *transport* fails on a plain redis:// URI, retries once over TLS
(rediss://) — Redis Cloud databases with TLS enabled reset plaintext clients,
and the one-character scheme fix is invisible unless something tries it.

Prints an ASCII-only verdict (Windows-console safe) and exits 0 on PONG.
Read-only: PING + INFO, writes nothing.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path


def load_env_uri() -> str:
    for base in (Path(__file__).resolve().parents[1], Path.cwd()):
        env = base / ".env"
        if env.is_file():
            for ln in env.read_text(encoding="utf-8-sig").splitlines():
                if ln.strip().startswith("REDIS_URI") and "=" in ln:
                    return ln.partition("=")[2].strip().strip('"').strip("'")
    sys.exit("FAIL: no REDIS_URI found in .env")


def attempt(uri: str, label: str) -> tuple[bool, str]:
    import redis  # local import so a missing package reads as a clear verdict

    try:
        r = redis.from_url(uri, socket_timeout=6, socket_connect_timeout=6)
        t0 = time.perf_counter()
        pong = r.ping()
        ms = (time.perf_counter() - t0) * 1000
        info = r.info(section="server")
        mem = r.info(section="memory").get("used_memory_human", "?")
        r.close()
        return True, (f"OK   [{label}] PING -> {pong} in {ms:.0f} ms | "
                      f"redis {info.get('redis_version', '?')} | mem {mem}")
    except redis.exceptions.AuthenticationError as e:
        return False, f"FAIL [{label}] auth rejected: {e} (re-copy password from console)"
    except redis.exceptions.ConnectionError as e:
        return False, f"FAIL [{label}] transport: {type(e).__name__}: {e}"
    except redis.exceptions.TimeoutError:
        return False, f"FAIL [{label}] timed out (firewall/VPN blocking outbound {uri.rsplit(':', 1)[-1]}?)"
    except Exception as e:  # noqa: BLE001
        return False, f"FAIL [{label}] {type(e).__name__}: {e}"


def main() -> None:
    uri = load_env_uri()
    ok, msg = attempt(uri, "as-written")
    print(msg)
    if ok:
        sys.exit(0)
    # Transport-level failure on a plain URI -> try TLS before giving up.
    if uri.startswith("redis://") and "auth rejected" not in msg:
        ok2, msg2 = attempt("rediss://" + uri[len("redis://"):], "tls-retry")
        print(msg2)
        if ok2:
            print("NOTE: your database requires TLS - change redis:// to rediss://")
            print("      in REDIS_URI and REDIS_URL in .env, then rerun this check.")
            sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
