"""Private MEGA storage for lesson files.

Python talks to MEGA through ``tools/mega_bridge.mjs`` (the installed ``megajs`` package runs under
Node.js). The bridge logs in fresh for every operation, so there is no long-lived session that can
expire. Credentials are passed through environment variables only and never appear in command
lines, logs, exceptions or ``repr``. Callers only ever see opaque node ids; they are stored in
SQLite and never sent to a browser.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Iterator

BRIDGE_PATH = Path(__file__).resolve().parent.parent / "tools" / "mega_bridge.mjs"
CHUNK = 64 * 1024
NODE_ID = re.compile(r"[A-Za-z0-9_-]{4,32}")
KNOWN_CODES = {"not_configured", "auth", "network", "not_found", "timeout", "bridge_missing", "failed"}

MESSAGES = {
    "not_configured": "MEGA storage is not configured. Set MEGA_EMAIL and MEGA_PASSWORD on the server.",
    "auth": "MEGA rejected the stored login. Check MEGA_EMAIL and MEGA_PASSWORD.",
    "network": "Could not reach MEGA. Try again shortly.",
    "not_found": "That file is missing from MEGA storage.",
    "timeout": "MEGA took too long to respond. Try again shortly.",
    "bridge_missing": "The MEGA helper could not start. Check that Node.js and the megajs package are installed.",
    "failed": "MEGA storage returned an error. Try again shortly.",
}


class MegaError(Exception):
    """A safe, secret-free failure. ``code`` is one of KNOWN_CODES."""

    def __init__(self, code: str = "failed"):
        self.code = code if code in KNOWN_CODES else "failed"
        super().__init__(MESSAGES[self.code])


def _code_from_stderr(raw: bytes | str) -> str:
    text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
    for line in reversed(text.strip().splitlines()):
        try:
            code = json.loads(line).get("error")
        except (ValueError, AttributeError):
            continue
        if code in KNOWN_CODES:
            return code
    if "ERR_MODULE_NOT_FOUND" in text or "Cannot find package" in text:
        return "bridge_missing"
    return "failed"


class MegaDownload:
    """An open download. Iterate ``chunks()`` and always call ``close()``."""

    def __init__(self, process: subprocess.Popen, first: bytes):
        self._process = process
        self._first = first
        self._closed = False

    def chunks(self) -> Iterator[bytes]:
        try:
            yield self._first
            while True:
                data = self._process.stdout.read(CHUNK)
                if not data:
                    break
                yield data
            self._process.wait()
            if self._process.returncode != 0:
                raise MegaError(_code_from_stderr(self._process.stderr.read()))
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.poll() is None:
                self._process.kill()
            self._process.wait()
        finally:
            for pipe in (self._process.stdout, self._process.stderr):
                try:
                    pipe.close()
                except Exception:
                    pass


class MegaService:
    def __init__(
        self,
        email: str,
        password: str,
        *,
        node_bin: str = "node",
        bridge_path: Path | str = BRIDGE_PATH,
        first_byte_timeout: int = 60,
        upload_timeout: int | None = None,
    ):
        self._email = email
        self._password = password
        self.node_bin = node_bin or "node"
        self.bridge_path = str(bridge_path)
        self.first_byte_timeout = first_byte_timeout
        self.upload_timeout = upload_timeout

    def __repr__(self) -> str:  # never include credentials
        return f"MegaService(configured={self.configured})"

    @classmethod
    def from_settings(cls, settings) -> "MegaService":
        return cls(settings.mega_email, settings.mega_password, node_bin=settings.node_bin)

    @property
    def configured(self) -> bool:
        return bool(self._email and self._password)

    def _command(self, *args: str) -> list[str]:
        return [self.node_bin, self.bridge_path, *args]

    def _env(self) -> dict[str, str]:
        return {**os.environ, "MEGA_EMAIL": self._email, "MEGA_PASSWORD": self._password}

    def upload(self, path: str | Path, folders: list[str], name: str, size: int) -> str:
        """Upload a local file into nested folders (created or reused). Returns the private node id."""
        if not self.configured:
            raise MegaError("not_configured")
        timeout = self.upload_timeout or min(3 * 3600, max(120, int(size) // 200_000))
        try:
            process = subprocess.Popen(
                self._command("upload", str(path), str(int(size)), name, *folders),
                env=self._env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError:
            raise MegaError("bridge_missing") from None
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise MegaError("timeout") from None
        if process.returncode != 0:
            raise MegaError(_code_from_stderr(err))
        try:
            node_id = json.loads(out.decode("utf-8", "ignore").strip().splitlines()[-1])["nodeId"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise MegaError("failed") from None
        if not isinstance(node_id, str) or not NODE_ID.fullmatch(node_id):
            raise MegaError("failed")
        return node_id

    def open_download(self, node_id: str) -> MegaDownload:
        """Start a download and wait for the first bytes, so failures surface before any response."""
        if not self.configured:
            raise MegaError("not_configured")
        if not NODE_ID.fullmatch(node_id or ""):
            raise MegaError("not_found")
        try:
            process = subprocess.Popen(
                self._command("download", node_id),
                env=self._env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError:
            raise MegaError("bridge_missing") from None
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(self.first_byte_timeout, expire)
        timer.start()
        try:
            first = process.stdout.read(CHUNK)
        finally:
            timer.cancel()
        if not first:
            process.wait()
            code = "timeout" if timed_out.is_set() else _code_from_stderr(process.stderr.read())
            MegaDownload(process, b"").close()
            raise MegaError(code)
        return MegaDownload(process, first)
