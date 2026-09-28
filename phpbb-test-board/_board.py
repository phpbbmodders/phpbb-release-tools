"""
Shared helpers for the phpBB test board scripts.

The board is created by setup-board.sh, which writes DIR/board.env. The
scripts find it through --board-dir or the PHPBB_TEST_BOARD environment
variable. Everything here works on a local development board only.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import requests


class Board:
    """A local phpBB test board described by DIR/board.env."""

    def __init__(self, board_dir: Optional[str], port: int = 8083) -> None:
        base = board_dir or os.environ.get("PHPBB_TEST_BOARD")
        if not base:
            raise SystemExit("error: no board: pass --board-dir or set PHPBB_TEST_BOARD (see setup-board.sh)")
        env_file = Path(base).expanduser().resolve() / "board.env"
        if not env_file.is_file():
            raise SystemExit(f"error: {env_file} not found; create the board with setup-board.sh first")
        env = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
        self.root = Path(env["PHPBB_BOARD"])
        self.db = Path(env["PHPBB_DB"])
        self.clean_db = Path(env["PHPBB_CLEAN_DB"])
        self.admin_user = env["PHPBB_ADMIN_USER"]
        self.admin_password = env["PHPBB_ADMIN_PASSWORD"]
        self.port = port
        self.base = f"http://localhost:{port}"
        self.php_log = Path(base).expanduser().resolve() / "php-errors.log"

    # -- board state -------------------------------------------------------

    def cli(self, *args: str) -> str:
        """Run phpBB's CLI and return its combined output."""
        r = subprocess.run(["php", "-d", "opcache.enable_cli=0", "bin/phpbbcli.php", *args],
                           cwd=self.root, capture_output=True, text=True)
        return r.stdout + r.stderr

    def purge_cache(self) -> None:
        """Delete the board's compiled cache."""
        for p in (self.root / "cache/production").glob("*"):
            if p.is_file():
                p.unlink()
            else:
                shutil.rmtree(p, ignore_errors=True)

    def sql(self, query: str, args: tuple = ()) -> list:
        """Run one SQL statement on the board's SQLite database."""
        con = sqlite3.connect(self.db)
        try:
            rows = con.execute(query, args).fetchall()
            con.commit()
            return rows
        finally:
            con.close()

    def php(self, code: str) -> str:
        """Run PHP inside the board (common.php and functions_user.php loaded)."""
        boot = ("define('IN_PHPBB',1);$phpbb_root_path='./';$phpEx='php';require 'common.php';"
                "require 'includes/functions_user.php';$user->session_begin();$auth->acl($user->data);$user->setup();")
        return subprocess.run(["php", "-d", "opcache.enable_cli=0", "-r", boot + code],
                              cwd=self.root, capture_output=True, text=True).stdout

    def reset(self) -> None:
        """Restore the clean database and cache."""
        shutil.copy(self.clean_db, self.db)
        self.purge_cache()

    # -- extensions --------------------------------------------------------

    def install_ext(self, repo: Path, ref: str) -> tuple:
        """Copy an extension from git into a clean board and enable it.

        Returns (extension name, installed path, output of the enable command);
        the caller decides what a failed enable means.
        """
        ext = json.loads(subprocess.run(["git", "-C", str(repo), "show", f"{ref}:composer.json"],
                                        capture_output=True, text=True, check=True).stdout)["name"]
        vendor, name = ext.split("/")
        target = self.root / "ext" / vendor / name
        self.reset()
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        tar = subprocess.run(["git", "-C", str(repo), "archive", ref], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(target)], input=tar, check=True)
        self.purge_cache()
        out = self.cli("extension:enable", ext)
        self.purge_cache()
        return ext, target, out

    def remove_ext(self, target: Path) -> None:
        """Remove an installed extension's files and restore the clean board."""
        shutil.rmtree(target, ignore_errors=True)
        parent = target.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        self.reset()

    # -- web server and sessions -------------------------------------------

    @contextmanager
    def serve(self) -> Iterator[None]:
        """Serve the board with PHP's built-in server and error logging on."""
        self.php_log.write_text("")
        server = subprocess.Popen(["php", "-d", "opcache.enable=0", "-d", "log_errors=1",
                                   "-d", f"error_log={self.php_log}", "-S", f"localhost:{self.port}",
                                   "-t", str(self.root)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # Wait for the server with a static file, so the check runs no
            # phpBB code (a first page load can have side effects, such as an
            # extension's deferred setup running before a test has prepared).
            for _ in range(50):
                try:
                    requests.head(f"{self.base}/images/index.htm", timeout=2)
                    break
                except requests.ConnectionError:
                    time.sleep(0.1)
            yield
        finally:
            server.terminate()
            server.wait()

    def php_errors(self) -> list:
        """Distinct PHP error-log entries from the last serve()."""
        log = self.php_log.read_text() if self.php_log.exists() else ""
        return sorted({re.sub(r"^\[[^\]]+\] ", "", line) for line in log.splitlines() if "PHP " in line})

    def login(self, session: requests.Session, username: Optional[str] = None,
              password: Optional[str] = None, acp: bool = False) -> str:
        """Log a session in (the board admin by default); return its session id.

        With acp=True the session is also marked as ACP-authenticated directly
        in the database, which is only acceptable on a disposable test board.
        """
        username = username or self.admin_user
        password = password or self.admin_password
        page = session.get(f"{self.base}/ucp.php?mode=login").text
        time.sleep(1.1)  # phpBB rejects forms submitted faster than its minimum form time
        fields = dict(re.findall(r'name="(form_token|creation_time|sid)" value="([^"]*)"', page))
        session.post(f"{self.base}/ucp.php?mode=login",
                     data={"username": username, "password": password, "login": "Login",
                           "redirect": "./index.php", **fields})
        sid = next((c.value for c in session.cookies if c.name.endswith("_sid")), "")
        if acp:
            self.sql("UPDATE phpbb_sessions SET session_admin = 1 WHERE session_id = ?", (sid,))
        return sid

    def add_user(self, name: str, password: str) -> int:
        """Create an active registered user; return its user_id."""
        out = self.php("$h=$phpbb_container->get('passwords.manager')->hash(" + json.dumps(password) + ");"
                       "echo user_add(['username'=>" + json.dumps(name) + ",'user_password'=>$h,"
                       "'user_email'=>" + json.dumps(f"{name}@example.com") + ",'group_id'=>2,"
                       "'user_type'=>USER_NORMAL,'user_regdate'=>time()]);")
        return int(out.strip().splitlines()[-1])
