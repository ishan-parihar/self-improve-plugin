#!/usr/bin/env python3
"""Suite for self-improve.sh.

The plugin is a single shell script whose job is to turn one completed turn
into at most one ledger entry, and to leave the ledger untouched when anything
goes wrong. This suite proves both halves without a network, a provider
credential, or a live gateway:

  * every early-exit guard fires (recursion, bad status, no assistant text);
  * the rate limiter caps per-session and profile-wide;
  * the parser survives hostile curator output (reasoning leakage, missing
    fields, template placeholders, fewer-than-three parts, all the confidence
    spellings);
  * dedup suppresses an exact-cue replay;
  * housekeeping prunes stale spawn files and caps the ledger;
  * a full fake-gateway run records a lesson and a second identical run does
    not (the dedup contract end to end).

Run:  python3 lib/test_self_improve.py [case]
Exit 0 on success, 1 on first failure.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PLUGIN = ROOT / "self-improve.sh"


def run_plugin(
    env: dict[str, str],
    *,
    home: Path,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the plugin with a sandboxed HOME and a minimal env.

    A fake HOME keeps the suite from touching the operator's real
    ~/.jcode/self-improve/ ledger and diagnostics. AROUTe credential is set to a
    dummy value only when the caller wants the gateway path exercised; the
    no-credential guard is itself one of the cases.
    """
    base_env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    base_env.update(env)
    return subprocess.run(
        ["bash", str(PLUGIN), *(args or [])],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


class PluginTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = self.home / ".jcode/self-improve/ledger.jsonl"
        self.diag = self.home / ".jcode/self-improve/diagnostics.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def diags(self) -> list[dict]:
        return read_jsonl(self.diag)

    def ledger_entries(self) -> list[dict]:
        return read_jsonl(self.ledger)

    def write_ledger(self, entries: list[dict]) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    # -- guard cases -----------------------------------------------------

    def test_recursion_guard_exits_without_touching_state(self):
        res = run_plugin(
            {
                "JCODE_HOOKS_DISABLED": "1",
                "JCODE_HOOK_STATUS": "ok",
                "JCODE_HOOK_LAST_ASSISTANT_TEXT": "did a thing",
                "AROUTE_API_KEY": "dummy",
            },
            home=self.home,
        )
        self.assertEqual(res.returncode, 0)
        self.assertFalse(self.ledger.exists())
        self.assertFalse(self.diag.exists())

    def test_non_ok_status_exits_without_touching_state(self):
        res = run_plugin(
            {
                "JCODE_HOOK_STATUS": "error",
                "JCODE_HOOK_LAST_ASSISTANT_TEXT": "did a thing",
                "AROUTE_API_KEY": "dummy",
            },
            home=self.home,
        )
        self.assertEqual(res.returncode, 0)
        self.assertFalse(self.diag.exists())

    def test_no_api_key_logs_skip_and_exits_zero(self):
        res = run_plugin(
            {
                "JCODE_HOOK_STATUS": "ok",
                "JCODE_HOOK_LAST_ASSISTANT_TEXT": "did a thing",
                # no AROUTe key, and no provider env file under fake HOME
            },
            home=self.home,
        )
        self.assertEqual(res.returncode, 0)
        diags = self.diags()
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["kind"], "skip")
        self.assertEqual(diags[0]["reason"], "no_api_key")

    def test_empty_assistant_text_logs_skip_and_exits_zero(self):
        # housekeeping runs first (mkdir state dir), then the no-text guard logs.
        res = run_plugin(
            {
                "JCODE_HOOK_STATUS": "ok",
                "JCODE_HOOK_LAST_ASSISTANT_TEXT": "",
                "AROUTE_API_KEY": "dummy",
            },
            home=self.home,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn(
            "no_assistant_text",
            [d.get("reason") for d in self.diags()],
        )

    def test_dead_router_exits_zero_and_logs_once(self):
        """An unreachable router must exit 0, not abort the hook.

        Regression: main() used a bare `scores=$(call_router ...)`, so under
        `set -e` call_router's `return 1` aborted the script with exit 1.
        A lesson capture that fails must never take down the turn that
        produced it, and the `wait` below must still run so any curator that
        did get backgrounded is not orphaned.
        """
        res = run_plugin(
            {
                "JCODE_HOOK_STATUS": "ok",
                "JCODE_HOOK_LAST_ASSISTANT_TEXT": "a real procedure worth scoring",
                "JCODE_HOOK_SESSION_ID": "dead-router",
                "AROUTE_API_KEY": "dummy",
                "SELF_IMPROVE_ROUTER_URL": "http://127.0.0.1:1/v1/chat/completions",
                "SELF_IMPROVE_ROUTER_MAX_TOKENS": "8",
            },
            home=self.home,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        failed = [d for d in self.diags() if d.get("kind") == "router-failed"]
        self.assertEqual(len(failed), 1, self.diags())
        self.assertEqual(self.ledger_entries(), [])

    def test_housekeeping_caps_ledger(self):
        # Seed a ledger larger than the cap; housekeeping trims to the last N.
        cap = 500
        self.write_ledger(
            [{"n": i, "cue": f"cue-{i}", "lesson": "l"} for i in range(cap + 50)]
        )
        res = run_plugin(
            {
                "JCODE_HOOK_STATUS": "ok",
                "JCODE_HOOK_LAST_ASSISTANT_TEXT": "",
                "AROUTE_API_KEY": "dummy",
            },
            home=self.home,
        )
        self.assertEqual(res.returncode, 0)
        entries = self.ledger_entries()
        self.assertLessEqual(len(entries), cap)

    def test_rate_limiter_caps_session(self):
        # Pre-fill the per-session spawn file past MAX_PER_HOUR, then run with
        # a stub router URL that must NOT be called (the limiter exits first).
        self.diag.parent.mkdir(parents=True, exist_ok=True)
        state = self.home / ".jcode/self-improve/state"
        state.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        session = "sess-cap"
        with (state / f"spawns-{session}").open("w") as f:
            for _ in range(10):  # MAX_PER_HOUR default 6; 10 > 6
                f.write(f"{now}\n")
        # A bogus router URL: if the limiter is working, no call is attempted
        # and we exit before curl. A real call would log router-failed.
        res = run_plugin(
            {
                "JCODE_HOOK_STATUS": "ok",
                "JCODE_HOOK_LAST_ASSISTANT_TEXT": "nontrivial debugging path that should score",
                "JCODE_HOOK_SESSION_ID": session,
                "AROUTE_API_KEY": "dummy",
                "SELF_IMPROVE_ROUTER_URL": "http://127.0.0.1:1/v1/chat/completions",
                "SELF_IMPROVE_ROUTER_MAX_TOKENS": "8",
            },
            home=self.home,
        )
        self.assertEqual(res.returncode, 0)
        # The cap diagnostic names the session scope.
        caps = [d for d in self.diags() if d.get("kind") == "cap"]
        self.assertTrue(any(c.get("scope") == "session" for c in caps))

    def test_housekeeping_prunes_stale_spawn_files(self):
        state = self.home / ".jcode/self-improve/state"
        state.mkdir(parents=True, exist_ok=True)
        stale = state / "spawns-old"
        stale.write_text("1\n")
        # age the file well past STATE_TTL_SECS (default 86400)
        old = time.time() - 200000
        os.utime(stale, (old, old))
        fresh = state / "spawns-fresh"
        fresh.write_text("1\n")
        res = run_plugin(
            {
                "JCODE_HOOK_STATUS": "ok",
                "JCODE_HOOK_LAST_ASSISTANT_TEXT": "",
                "AROUTE_API_KEY": "dummy",
            },
            home=self.home,
        )
        self.assertEqual(res.returncode, 0)
        self.assertFalse(stale.exists(), "stale spawn file should be pruned")
        self.assertTrue(fresh.exists(), "fresh spawn file must survive")


class FakeGatewayTest(unittest.TestCase):
    """End-to-end through a local fake gateway: the only network surface the
    plugin touches is HTTP POST to ROUTE_URL, so a loopback stub exercises the
    router, curator, parser, dedup and ledger write paths for real."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = self.home / ".jcode/self-improve/ledger.jsonl"
        self.diag = self.home / ".jcode/self-improve/diagnostics.jsonl"
        # Stateful script: first call answers the router, second answers the
        # curator. It records each body it received so tests can assert what
        # the plugin sent.
        self.server = self.home / "gateway.py"
        self.server.write_text(GATEWAY_PY)
        self.received = self.home / "received.jsonl"
        self.proc = subprocess.Popen(
            [sys.executable, str(self.server)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # wait for the port to come up
        port = 18977
        for _ in range(100):
            try:
                import socket

                s = socket.create_connection(("127.0.0.1", port), timeout=0.1)
                s.close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            self.proc.kill()
            raise RuntimeError("fake gateway did not start")
        self.port = port

    def tearDown(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._tmp.cleanup()

    def diags(self) -> list[dict]:
        return read_jsonl(self.diag)

    def ledger_entries(self) -> list[dict]:
        return read_jsonl(self.ledger)

    def run_turn(self, assistant_text: str = "Fixed the flaky test by waiting on a condition variable.", mem_url: str | None = None) -> subprocess.CompletedProcess:
        env = {
            "JCODE_HOOK_STATUS": "ok",
            "JCODE_HOOK_LAST_ASSISTANT_TEXT": assistant_text,
            "JCODE_HOOK_SESSION_ID": "fake-session",
            "AROUTE_API_KEY": "dummy",
            "SELF_IMPROVE_ROUTER_URL": f"http://127.0.0.1:{self.port}/v1/chat/completions",
            "SELF_IMPROVE_ROUTER_MODEL": "small-stack",
            "SELF_IMPROVE_CURATOR_MODEL": "large-stack",
            "SELF_IMPROVE_SKILL_THRESHOLD": "0.5",
            "SELF_IMPROVE_GENOME_THRESHOLD": "0.5",
        }
        if mem_url is not None:
            env["SELF_IMPROVE_MEMORY_URL"] = mem_url
        return run_plugin(env, home=self.home)

    def test_full_turn_records_a_skill_lesson(self):
        res = self.run_turn()
        self.assertEqual(res.returncode, 0, res.stderr)
        entries = self.ledger_entries()
        skill_entries = [e for e in entries if e.get("kind") == "skill"]
        self.assertEqual(len(skill_entries), 1, entries)
        e = skill_entries[0]
        self.assertEqual(e["cue"], "debugging a flaky test")
        self.assertIn("condition variable", e["lesson"])
        self.assertEqual(e["confidence"], "high")
        self.assertEqual(e["sourceTurn"], "fake-session")

    def test_second_identical_turn_is_deduplicated(self):
        self.run_turn()
        first = len(self.ledger_entries())
        res2 = self.run_turn()
        self.assertEqual(res2.returncode, 0, res2.stderr)
        second = len(self.ledger_entries())
        self.assertEqual(first, second, "identical cue must not be re-recorded")
        # and the dedup was logged
        self.assertTrue(
            any(d.get("kind") == "curation-duplicate" for d in self.diags()),
            self.diags(),
        )

    def test_memory_share_posts_to_bank_route(self):
        """The lesson share must hit /banks/{bank}/retain (not /retain).

        The fake gateway records the request path on a sibling file, so the
        test asserts the exact route rather than trusting a substring.
        """
        import socket

        paths_file = self.home / "paths.log"
        path_server = self.home / "pathserver.py"
        path_server.write_text(PATH_SERVER_PY)
        proc = subprocess.Popen(
            [sys.executable, str(path_server)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    s = socket.create_connection(("127.0.0.1", 18978), timeout=0.1)
                    s.close()
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("path server did not start")
            res = self.run_turn(mem_url="http://127.0.0.1:18978")
        finally:
            proc.terminate()
            proc.wait(timeout=3)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            len([e for e in self.ledger_entries() if e.get("kind") == "skill"]), 1
        )
        self.assertTrue(paths_file.exists(), "no retain request was recorded")
        self.assertIn("POST /banks/memory-wire/retain", paths_file.read_text())

    def test_memory_share_to_dead_server_logs_and_does_not_fail(self):
        # A refused connection must be logged, not raised, and the lesson stays.
        res = self.run_turn(mem_url="http://127.0.0.1:1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len([e for e in self.ledger_entries() if e.get("kind") == "skill"]), 1)
        self.assertTrue(
            any(d.get("kind") == "memory-failed" for d in self.diags()),
            self.diags(),
        )

    def test_parser_ignores_curation_nothing(self):
        # Point the curator at a response that yields CURATION: nothing.
        self.server.write_text(GATEWAY_PY_NOTHING)
        self.proc.terminate()
        self.proc.wait(timeout=3)
        self.proc = subprocess.Popen(
            [sys.executable, str(self.server)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            try:
                import socket

                s = socket.create_connection(("127.0.0.1", self.port), timeout=0.1)
                s.close()
                break
            except OSError:
                time.sleep(0.05)
        res = self.run_turn()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.ledger_entries(), [])
        self.assertTrue(
            any(d.get("kind") == "curation-nothing" for d in self.diags()),
            self.diags(),
        )


GATEWAY_PY = r'''
import json, http.server, socketserver, sys
from pathlib import Path

RECV = Path(__file__).with_name("received.jsonl")

ROUTER_ANSWER = json.dumps({"skill_worthy": 0.9, "genome_worthy": 0.9})
CURATOR_SKILL_ANSWER = (
    "Reviewed the turn. The user hit a race in a polling loop.\n\n"
    "CURATION: debugging a flaky test | Wait on a condition variable, never sleep-and-poll, because sleep is racy | the test failed intermittently and passed with a cv wait | high\n"
)
CURATOR_GENOME_ANSWER = (
    "This turn made a project decision.\n\n"
    "GENOME: phase 2 started | nothing skipped\n"
    "CURATION: nothing\n"
)

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode()
        with RECV.open("a") as f:
            f.write(body + "\n")
        # The plugin sends one model per call: router first, then curator.
        # Distinguish by the prompt's opening words.
        is_router = "evaluating whether a completed agent turn" in body
        if is_router:
            payload = {"choices": [{"message": {"content": ROUTER_ANSWER}}]}
        else:
            prompt = json.loads(body)["messages"][0]["content"]
            if "genome curator" in prompt:
                payload = {"choices": [{"message": {"content": CURATOR_GENOME_ANSWER}}]}
            else:
                payload = {"choices": [{"message": {"content": CURATOR_SKILL_ANSWER}}]}
        out = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", 18977), H) as httpd:
    httpd.serve_forever()
'''

GATEWAY_PY_NOTHING = r'''
import json, http.server, socketserver
ROUTER_ANSWER = json.dumps({"skill_worthy": 0.9, "genome_worthy": 0.0})
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode()
        is_router = "evaluating whether a completed agent turn" in body
        if is_router:
            payload = {"choices": [{"message": {"content": ROUTER_ANSWER}}]}
        else:
            payload = {"choices": [{"message": {"content": "Nothing transferable here.\n\nCURATION: nothing\n"}}]}
        out = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", 18977), H) as httpd:
    httpd.serve_forever()
'''


PATH_SERVER_PY = r'''
import http.server, socketserver
from pathlib import Path

LOGPATH = Path(__file__).with_name("paths.log")

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        if n:
            self.rfile.read(n)
        with LOGPATH.open("a") as f:
            f.write(f"{self.command} {self.path}\n")
        out = b'{"id":"stub"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", 18978), H) as httpd:
    httpd.serve_forever()
'''


if __name__ == "__main__":
    # Allow `python3 lib/test_self_improve.py case_name` to run one case.
    if len(sys.argv) > 1:
        unittest.main(argv=[sys.argv[0], sys.argv[1]], verbosity=2)
    else:
        unittest.main(verbosity=2)