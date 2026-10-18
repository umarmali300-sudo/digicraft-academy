"""MegaService against a fake bridge script. No network, no real MEGA account."""
import sys
import time
from pathlib import Path

from app.mega_service import MegaError, MegaService

FAKE_BRIDGE = r"""
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
mode = open(os.path.join(here, "mode.txt")).read().strip()
open(os.path.join(here, "seen.json"), "w").write(json.dumps(
    {"argv": sys.argv, "email": os.environ.get("MEGA_EMAIL"), "password": os.environ.get("MEGA_PASSWORD")}))
def die(code):
    sys.stderr.write(json.dumps({"error": code}) + "\n"); sys.exit(1)
cmd = sys.argv[1]
if mode.startswith("fail:"):
    die(mode.split(":", 1)[1])
if cmd == "upload":
    if mode == "badjson":
        print("not json"); sys.exit(0)
    if mode == "badid":
        print(json.dumps({"nodeId": "../../x y"})); sys.exit(0)
    if mode == "hang":
        time.sleep(30)
    print(json.dumps({"nodeId": "AbCd1234"}))
else:
    out = sys.stdout.buffer
    if mode == "hang":
        time.sleep(30)
    out.write(b"A" * 70000); out.flush()
    if mode == "midfail":
        die("network")
    if mode == "slow":
        out.write(b"B" * 10); out.flush(); time.sleep(30)
    out.write(b"C" * 100); out.flush()
"""


def make(tmp_path: Path, mode: str, **kw) -> MegaService:
    (tmp_path / "bridge.py").write_text(FAKE_BRIDGE)
    (tmp_path / "mode.txt").write_text(mode)
    return MegaService("owner@example.com", "s3cret-pass", node_bin=sys.executable, bridge_path=tmp_path / "bridge.py", **kw)


def code_of(func, *args):
    try:
        func(*args)
    except MegaError as error:
        return error.code
    raise AssertionError("expected MegaError")


def test_upload_returns_node_id_and_keeps_secrets_out_of_argv(tmp_path: Path):
    service = make(tmp_path, "ok")
    local = tmp_path / "f.pdf"
    local.write_bytes(b"%PDF-1.4")
    node = service.upload(local, ["DigicraftAcademy", "course-1-x", "lesson-2-y"], "mega-abc.pdf", 8)
    assert node == "AbCd1234"
    import json
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert seen["email"] == "owner@example.com" and seen["password"] == "s3cret-pass"
    assert "s3cret-pass" not in " ".join(seen["argv"]) and "owner@example.com" not in " ".join(seen["argv"])
    assert seen["argv"][2:6] == [str(local), "8", "mega-abc.pdf", "DigicraftAcademy"]


def test_upload_failures_map_to_safe_codes(tmp_path: Path):
    for code in ("auth", "network", "failed"):
        service = make(tmp_path, f"fail:{code}")
        assert code_of(service.upload, tmp_path / "f", ["a"], "n.pdf", 1) == code
    assert code_of(make(tmp_path, "badjson").upload, tmp_path / "f", ["a"], "n.pdf", 1) == "failed"
    assert code_of(make(tmp_path, "badid").upload, tmp_path / "f", ["a"], "n.pdf", 1) == "failed"


def test_upload_timeout_kills_the_helper(tmp_path: Path):
    service = make(tmp_path, "hang", upload_timeout=1)
    started = time.time()
    assert code_of(service.upload, tmp_path / "f", ["a"], "n.pdf", 1) == "timeout"
    assert time.time() - started < 10


def test_unconfigured_and_missing_node_are_reported_safely(tmp_path: Path):
    assert code_of(MegaService("", "").upload, tmp_path / "f", ["a"], "n.pdf", 1) == "not_configured"
    assert code_of(MegaService("", "").open_download, "AbCd1234") == "not_configured"
    missing = MegaService("a@b.c", "pw", node_bin=str(tmp_path / "no-such-node"))
    assert code_of(missing.upload, tmp_path / "f", ["a"], "n.pdf", 1) == "bridge_missing"
    assert code_of(missing.open_download, "AbCd1234") == "bridge_missing"


def test_repr_and_errors_never_contain_credentials(tmp_path: Path):
    service = make(tmp_path, "fail:auth")
    assert "s3cret-pass" not in repr(service) and "owner@example.com" not in repr(service)
    try:
        service.upload(tmp_path / "f", ["a"], "n.pdf", 1)
    except MegaError as error:
        assert "s3cret-pass" not in str(error) and "owner@example.com" not in str(error)


def test_download_streams_all_bytes(tmp_path: Path):
    download = make(tmp_path, "ok").open_download("AbCd1234")
    data = b"".join(download.chunks())
    assert data == b"A" * 70000 + b"C" * 100


def test_download_failure_before_first_byte_raises_without_streaming(tmp_path: Path):
    for code in ("not_found", "auth", "network"):
        assert code_of(make(tmp_path, f"fail:{code}").open_download, "AbCd1234") == code
    assert code_of(make(tmp_path, "ok").open_download, "../etc/passwd") == "not_found"


def test_download_first_byte_timeout(tmp_path: Path):
    service = make(tmp_path, "hang", first_byte_timeout=1)
    started = time.time()
    assert code_of(service.open_download, "AbCd1234") == "timeout"
    assert time.time() - started < 10


def test_download_failure_midstream_raises_and_cleans_up(tmp_path: Path):
    download = make(tmp_path, "midfail").open_download("AbCd1234")
    try:
        b"".join(download.chunks())
        raise AssertionError("expected MegaError")
    except MegaError as error:
        assert error.code == "network"
    assert download._process.poll() is not None


def test_closing_a_download_stops_the_helper(tmp_path: Path):
    download = make(tmp_path, "slow").open_download("AbCd1234")
    stream = download.chunks()
    next(stream)
    process = download._process
    assert process.poll() is None
    stream.close()
    assert process.poll() is not None
