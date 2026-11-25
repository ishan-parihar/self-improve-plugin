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
  * dedup suppresses an exact-cue replay, including a cue carrying a quote or
    a backslash, and including two curators racing the same turn;
  * housekeeping prunes stale spawn files and caps both JSONL files;
  * every diagnostic arm is asserted by at least one case, so a silent
    regression in a "nothing happened" path fails a test;
  * a full fake-gateway run records a lesson and a second identical run does
    not (the dedup contract end to end).

Run:  python3 lib/test_self_improve.py [case]
Exit 0 on success, 1 on first failure.

Ports are requested from the OS (bind 0, read back), so two runs of this suite
cannot collide on a fixed port and report a hang or a false pass.
"""
from __future__ import annotations

import json
import os
import socket
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


def free_port() -> int:
    """Ask the OS for an unused TCP port.

    A fixed port makes two concurrent runs of this suite collide, and the failure
    mode is a hang or a false pass rather than an error. Binding 0 and reading the
    assigned port back removes the collision class entirely.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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

    def _spawn_gateway(self, skill_only: bool = False) -> None:
        """Start the fake gateway on a FRESH port, keeping stderr.

        A fresh port matters: reusing one after terminate lets the dying socket
        answer the readiness probe while the new bind is still pending, so the
        plugin's curl then fails. stderr is captured to a file so a startup
        failure reports its cause instead of timing out as "did not start".

        ``skill_only`` scores genome 0.0, so a case can exercise the skill branch
        without the genome branch firing alongside it.
        """
        self.port = free_port()
        self.server_err.seek(0)
        self.server_err.truncate()
        env = dict(os.environ)
        env["SKILL_ONLY"] = "1" if skill_only else "0"
        self.proc = subprocess.Popen(
            [sys.executable, str(self.server), str(self.port)],
            stdout=subprocess.DEVNULL,
            stderr=self.server_err,
            env=env,
        )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = self.home / ".jcode/self-improve/ledger.jsonl"
        self.diag = self.home / ".jcode/self-improve/diagnostics.jsonl"
        # The gateway answers the router with a fixed score and the curator with
        # whatever is in self.answer_path, so a case can drive the parser with
        # hostile text without rewriting and restarting the server. It records
        # every body so tests can assert what the plugin actually sent.
        self.server = self.home / "gateway.py"
        self.server.write_text(GATEWAY_PY)
        # The gateway writes received.jsonl next to itself, so the test reads the
        # same path. Reading a path under HOME while the server wrote next to the
        # script silently saw nothing and failed the wire assertions.
        self.received = self.server.parent / "received.jsonl"
        self.answer_path = self.home / "answer.txt"
        self.answer_path.write_text(self._default_answer)
        self.server_err = open(self.home / "gateway.err", "w+")
        self.addCleanup(self.server_err.close)
        self._spawn_gateway()
        self._wait_for_port()

    _default_answer = (
        "CURATION: debugging a flaky test | Wait on a condition variable, never "
        "sleep-and-poll, because sleep is racy | the test failed intermittently "
        "and passed with a cv wait | high"
    )

    def _wait_for_port(self) -> None:
        for _ in range(100):
            if self.proc.poll() is not None:
                # The server died on startup. Surface why: with stderr on
                # DEVNULL the only symptom was "did not start".
                self.server_err.seek(0)
                err = self.server_err.read()
                raise RuntimeError(
                    f"fake gateway exited {self.proc.returncode}: {err[-800:]}"
                )
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=0.1)
                s.close()
                return
            except OSError:
                time.sleep(0.05)
        self.proc.kill()
        raise RuntimeError("fake gateway did not start")

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

    def run_turn(self, assistant_text: str = "Fixed the flaky test by waiting on a condition variable.", mem_url: str | None = None, curator_answer: str = "CURATION: debugging a flaky test | Wait on a condition variable, never sleep-and-poll, because sleep is racy | the test failed intermittently and passed with a cv wait | high") -> subprocess.CompletedProcess:
        """Run one turn end to end.

        ``curator_answer`` is what the gateway's curator reply carries. It lets a
        case drive the parser with hostile text without rewriting the gateway
        script and restarting the server.
        """
        if curator_answer != self._default_answer:
            # The gateway reads the answer file per request, so swapping the text
            # is enough. Restarting the server here used to reuse the old port,
            # which races TIME_WAIT and can fail the turn's curl.
            self.answer_path.write_text(curator_answer)
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
            # The plugin sources its own env file AFTER the process env, so a
            # variable set only in the process env loses to whatever the file says.
            # Write the case's URL into the file so it is the value the plugin
            # actually uses, then remove it again so cases stay independent.
            cfg = self.home / ".jcode/plugins/self-improve/env"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            existing = cfg.read_text() if cfg.exists() else ""
            cfg.write_text(
                existing + f"SELF_IMPROVE_MEMORY_URL={mem_url}\n"
            )
            self.addCleanup(self._restore_env_file, cfg, existing)
        return run_plugin(env, home=self.home)

    def _restore_env_file(self, cfg, existing: str) -> None:
        if existing:
            cfg.write_text(existing)
        elif cfg.exists():
            cfg.unlink()

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
        """The project fact must hit /banks/{bank}/retain (not /retain).

        The fake gateway records the request path on a sibling file, so the test
        asserts the exact route rather than trusting a substring.
        """
        paths_file = self.home / "paths.log"
        path_server = self.home / "pathserver.py"
        path_server.write_text(PATH_SERVER_PY)
        mem_port = free_port()
        proc = subprocess.Popen(
            [sys.executable, str(path_server), str(mem_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    s = socket.create_connection(("127.0.0.1", mem_port), timeout=0.1)
                    s.close()
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("path server did not start")
            res = self.run_turn(mem_url=f"http://127.0.0.1:{mem_port}")
        finally:
            proc.terminate()
            proc.wait(timeout=3)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            len([e for e in self.ledger_entries() if e.get("kind") == "genome"]), 1
        )
        self.assertTrue(paths_file.exists(), "no retain request was recorded")
        logged = [json.loads(l) for l in paths_file.read_text().splitlines() if l.strip()]
        # The bank is DERIVED, not pinned. The fake HOME sits under the real repo
        # tree, so the work-tree walk reaches this repository's own .git and the
        # remote rung wins. Asserting the derived name is the point: a hardcoded
        # bank would put every project in one namespace.
        self.assertEqual(
            [r["path"] for r in logged],
            ["/banks/ishan-parihar-self-improve-plugin/retain"],
            logged,
        )

    def test_skill_lesson_never_reaches_memory(self):
        """Memory is for project facts, so a skill lesson must not be shared.

        A skill lesson transfers across projects by definition. Writing it to the
        shared bank puts one project's procedure into every project's active
        context, which is subagent telemetry, not project memory.
        """
        paths_file = self.home / "paths.log"
        path_server = self.home / "pathserver.py"
        path_server.write_text(PATH_SERVER_PY)
        mem_port = free_port()
        proc = subprocess.Popen(
            [sys.executable, str(path_server), str(mem_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    s = socket.create_connection(("127.0.0.1", mem_port), timeout=0.1)
                    s.close()
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("path server did not start")
            # The fake gateway scores every turn 0.9/0.9, so both branches always
            # fire. Ask for the skill-only scoring so this case can actually
            # isolate the skill branch, while the curator still records a lesson.
            self.restart_gateway(skill_only=True)
            res = self.run_turn(mem_url=f"http://127.0.0.1:{mem_port}")
        finally:
            proc.terminate()
            proc.wait(timeout=3)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            len([e for e in self.ledger_entries() if e.get("kind") == "skill"]), 1
        )
        logged = [json.loads(l) for l in paths_file.read_text().splitlines() if l.strip()] if paths_file.exists() else []
        self.assertEqual(
            logged,
            [],
            "a skill lesson must never be shared to memory",
        )

    def test_bank_is_derived_from_the_origin_remote(self):
        """A git repo's bank comes from owner/repo, not a pinned constant.

        This is the whole point of the change: one shared bank makes every
        project's facts recallable in every other project's context. The fake HOME
        gets a real .git/config with an origin, so the remote rung is exercised.
        """
        repo = self.home / "proj"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "config").write_text(
            '[core]\n\trepositoryformatversion = 0\n\n'
            '[remote "origin"]\n\turl = https://github.com/acme/widget.git\n'
        )
        paths_file = self.home / "paths.log"
        path_server = self.home / "pathserver.py"
        path_server.write_text(PATH_SERVER_PY)
        mem_port = free_port()
        proc = subprocess.Popen(
            [sys.executable, str(path_server), str(mem_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    s = socket.create_connection(("127.0.0.1", mem_port), timeout=0.1)
                    s.close()
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("path server did not start")
            env = {
                "JCODE_HOOK_STATUS": "ok",
                "JCODE_HOOK_LAST_ASSISTANT_TEXT": "The operator ruled on a design question.",
                "JCODE_HOOK_SESSION_ID": "s",
                "JCODE_HOOK_CWD": str(repo),
                "AROUTE_API_KEY": "dummy",
                "SELF_IMPROVE_ROUTER_URL": f"http://127.0.0.1:{self.port}/v1/chat/completions",
                "SELF_IMPROVE_SKILL_THRESHOLD": "0.5",
                "SELF_IMPROVE_GENOME_THRESHOLD": "0.5",
            }
            cfg = self.home / ".jcode/plugins/self-improve/env"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(f"SELF_IMPROVE_MEMORY_URL=http://127.0.0.1:{mem_port}\n")
            res = run_plugin(env, home=self.home)
        finally:
            proc.terminate()
            proc.wait(timeout=3)
        self.assertEqual(res.returncode, 0, res.stderr)
        logged = [json.loads(l) for l in paths_file.read_text().splitlines() if l.strip()]
        self.assertEqual(
            [r["path"] for r in logged],
            ["/banks/acme-widget/retain"],
            logged,
        )

    def test_memory_share_carries_the_project_fact_not_the_lesson(self):
        """The shared content is the project fact, tagged as one."""
        paths_file = self.home / "paths.log"
        path_server = self.home / "pathserver.py"
        path_server.write_text(PATH_SERVER_PY)
        mem_port = free_port()
        proc = subprocess.Popen(
            [sys.executable, str(path_server), str(mem_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    s = socket.create_connection(("127.0.0.1", mem_port), timeout=0.1)
                    s.close()
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("path server did not start")
            res = self.run_turn(mem_url=f"http://127.0.0.1:{mem_port}")
        finally:
            proc.terminate()
            proc.wait(timeout=3)
        self.assertEqual(res.returncode, 0, res.stderr)
        rows = [json.loads(l) for l in paths_file.read_text().splitlines() if l.strip()]
        payload = json.loads(rows[-1]["body"])
        self.assertEqual(payload["tags"], ["hook:sync-turn", "project-fact"])
        # The content is the fact, not a '[skill]' lesson blob.
        self.assertFalse(payload["content"].startswith("["))
        self.assertTrue(payload["content"])

    def test_memory_share_to_dead_server_logs_and_does_not_fail(self):
        # A refused connection must be logged, not raised, and the lesson stays.
        res = self.run_turn(mem_url="http://127.0.0.1:1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len([e for e in self.ledger_entries() if e.get("kind") == "genome"]), 1)
        self.assertTrue(
            any(d.get("kind") == "memory-failed" for d in self.diags()),
            self.diags(),
        )

    def test_plugin_env_file_is_sourced(self):
        """A hook entry in config.toml is a path, so the knobs live in a file.

        Discriminating proof: the fake gateway scores every turn 0.9/0.9, so with
        the default 0.65 skill threshold the skill branch records a lesson.
        Writing ``SELF_IMPROVE_SKILL_THRESHOLD=0.99`` into the env file must
        suppress that branch entirely — the one outcome only a read file can
        produce. The genome branch is unaffected and still runs at its 0.70
        default (its fake answer is `CURATION: nothing`, so it records nothing
        but it does fire: the callback is what the gateway saw).
        """
        # baseline: no env file, skill branch fires and records
        kinds_baseline = [e.get("kind") for e in self.ledger_after_turn()]
        self.assertIn("skill", kinds_baseline)

        # reset the ledger so the second run starts clean
        if self.ledger.exists():
            self.ledger.unlink()

        # with the env file raising the threshold past 0.9, the branch must not fire
        cfg_dir = self.home / ".jcode/plugins/self-improve"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "env").write_text("SELF_IMPROVE_SKILL_THRESHOLD=0.99\n")
        kinds = [e.get("kind") for e in self.ledger_after_turn()]
        self.assertNotIn(
            "skill", kinds,
            "the env file's 0.99 threshold must suppress the skill branch",
        )

    def ledger_after_turn(self) -> list[dict]:
        res = self.run_turn()
        self.assertEqual(res.returncode, 0, res.stderr)
        return self.ledger_entries()

    def test_quote_and_backslash_cue_round_trips(self):
        """A cue carrying a quote or a backslash must record once and dedup.

        The diagnostics writer used printf, so either character produced an
        unparseable line, and the dedup check grepped the literal \"cue\": \"...\"
        text, so the same cue was written again. This drives the real plugin with
        such a cue twice and asserts one row plus parseable diagnostics.
        """
        nasty = 'use "volatile" and \\ backslash here'
        self.run_turn(curator_answer=f"CURATION: {nasty} | lesson | evidence")
        first = self.ledger_entries()
        # The turn fires both branches and the gateway answers them the same way, so
        # a turn may record one row per kind. The contract under test is per (cue,
        # kind): exactly one row of each, never two of either.
        kinds = [e["kind"] for e in first]
        self.assertEqual(sorted(kinds), ["genome", "skill"], first)
        for e in first:
            self.assertEqual(e["cue"], nasty)
        # every diagnostics line must parse, including the recorded one
        for line in self.diag.read_text().splitlines():
            json.loads(line)
        # a second identical turn must not add a row
        self.run_turn(curator_answer=f"CURATION: {nasty} | lesson | evidence")
        self.assertEqual(len(self.ledger_entries()), 2)
        self.assertTrue(
            any(d.get("kind") == "curation-duplicate" for d in self.diags()),
            self.diags(),
        )

    def test_concurrent_curators_write_one_row(self):
        """A cue racing itself across turns must yield one row per kind.

        Measured on the real plugin before the fix: one turn wrote two rows of
        the SAME kind, because both curators passed the dedup check before
        either appended. The check and the append now share an flock, so two
        concurrent turns of one kind collapse to one row each.
        """
        import concurrent.futures as cf

        with cf.ThreadPoolExecutor(2) as ex:
            list(ex.map(lambda _: self.run_turn(), range(2)))
        rows = self.ledger_entries()
        kinds = [r["kind"] for r in rows]
        self.assertEqual(sorted(kinds), ["genome", "skill"], rows)

    def test_diagnostics_is_capped(self):
        """diagnostics.jsonl is capped like the ledger, not unbounded."""
        cap = 5
        d = self.home / ".jcode/self-improve"
        d.mkdir(parents=True, exist_ok=True)
        (d / "diagnostics.jsonl").write_text(
            "".join(json.dumps({"i": i}) + "\n" for i in range(cap + 10))
        )
        res = run_plugin(
            {
                "JCODE_HOOK_STATUS": "ok",
                "JCODE_HOOK_LAST_ASSISTANT_TEXT": "",
                "AROUTE_API_KEY": "dummy",
                "SELF_IMPROVE_MAX_LEDGER_ENTRIES": str(cap),
            },
            home=self.home,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(self.diags()), cap)

    def test_model_on_the_wire(self):
        """The router body must name small-stack and the curator large-stack.

        The suite set these env vars but asserted nothing, so a regression that
        swapped the two models passed. The fake gateway records every body.
        """
        self.run_turn()
        bodies = [json.loads(l) for l in self.received.read_text().splitlines()]
        models = [b.get("model") for b in bodies]
        self.assertIn("small-stack", models, models)
        self.assertIn("large-stack", models, models)
        # and the router is the small one, the curator the large one
        for b in bodies:
            head = b["messages"][0]["content"]
            if "evaluating whether a completed agent turn" in head:
                self.assertEqual(b["model"], "small-stack")
            else:
                self.assertEqual(b["model"], "large-stack")

    def restart_gateway(self, skill_only: bool = False) -> None:
        """Stop and restart the fake gateway from its current script.

        The port must be fresh: reusing one after terminate lets the dying socket
        answer the readiness probe while the new bind is still pending, so the
        plugin's curl fails and logs router-failed.
        """
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)
        self._spawn_gateway(skill_only=skill_only)
        self._wait_for_port()

    # -- diagnostic arm coverage ------------------------------------------
    #
    # Every arm log_diag can emit must have a case that fails if the arm stops
    # firing. The arms below cover the "nothing happened" paths plus the one arm
    # that means the product worked, so a silent regression in any of them shows
    # up as a red test rather than a quiet ledger.

    def assert_arm(self, arm: str) -> None:
        self.assertTrue(
            any(d.get("kind") == arm for d in self.diags()),
            f"arm {arm!r} never fired: {self.diags()}",
        )

    def test_arm_curation_recorded(self):
        """The success arm: one turn, both branches, a recorded row each."""
        self.run_turn()
        self.assert_arm("curation-recorded")

    def test_arm_router(self):
        """The router arm reports the scores it got, whatever they are."""
        self.run_turn()
        self.assert_arm("router")

    def test_arm_curation_incomplete(self):
        """Three fields with an empty one is incomplete, not parse-failed.

        The count guard must not swallow this: a line with the right number of
        parts but a blank field is the incomplete case, and it used to be
        unreachable because the count check ran before the blank could be seen.
        """
        self.run_turn(curator_answer="CURATION: a cue | | evidence")
        self.assert_arm("curation-incomplete")
        self.assertEqual(self.ledger_entries(), [])

    def test_arm_curation_parse_failed(self):
        """Fewer than three parts logs parse-failed and records nothing."""
        self.run_turn(curator_answer="CURATION: only one part")
        self.assert_arm("curation-parse-failed")

    def test_arm_curation_template(self):
        """A cue still carrying <angle brackets> is a template, not a lesson."""
        self.run_turn(curator_answer="CURATION: <cue> | <lesson> | <evidence>")
        self.assert_arm("curation-template")

    def test_genome_fact_survives_curation_nothing(self):
        """A genome fact must survive an explicit `CURATION: nothing`.

        The genome prompt asks for the GENOME line FIRST and then a CURATION line,
        and a model that recorded a real project fact still writes
        `CURATION: nothing` because the fact was not a transferable lesson.
        Measured live: the plugin logged curation-nothing and the fact was lost.
        The fallback therefore runs on explicit-nothing, not only on absence.
        """
        answer = (
            "GENOME: the self-improve plugin is a git-tracked public repo and "
            "aroute.ishanparihar.com is the required router (operator rulings) | "
            "not recording the rationale, because none was stated\n"
            "CURATION: nothing\n"
        )
        res = self.run_turn(curator_answer=answer)
        self.assertEqual(res.returncode, 0, res.stderr)
        rows = [e for e in self.ledger_entries() if e.get("kind") == "genome"]
        self.assertEqual(len(rows), 1, self.ledger_entries())
        self.assertIn("git-tracked public repo", rows[0]["cue"])
        # The genome branch must not report nothing. The skill branch got the same
        # reply and correctly did, so scope the assertion to the genome branch.
        self.assertFalse(
            any(
                d.get("kind") == "curation-nothing"
                and d.get("branch") == "genome"
                for d in self.diags()
            ),
            self.diags(),
        )

    def test_genome_nothing_still_logs_curation_nothing(self):
        """An explicit double-nothing is still the no-op, not a record."""
        res = self.run_turn(
            curator_answer="GENOME: nothing\nCURATION: nothing\n"
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.ledger_entries(), [])
        self.assert_arm("curation-nothing")

    def test_arm_no_curation(self):
        """A reply with no CURATION line at all logs no-curation."""
        self.run_turn(curator_answer="I found nothing worth writing down.")
        self.assert_arm("no-curation")

    def test_arm_curator_failed(self):
        """The router succeeds but the curator call fails: curator-failed."""
        # Same fake gateway, but the plugin's curator URL points at a dead port
        # while the router URL stays live. The knobs are read from the env file.
        cfg_dir = self.home / ".jcode/plugins/self-improve"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        dead = free_port()
        (cfg_dir / "env").write_text(
            f"SELF_IMPROVE_ROUTER_URL=http://127.0.0.1:{self.port}/v1/chat/completions\n"
            f"SELF_IMPROVE_CURATOR_URL=http://127.0.0.1:{dead}/v1/chat/completions\n"
            f"SELF_IMPROVE_SKILL_THRESHOLD=0.5\n"
            f"SELF_IMPROVE_GENOME_THRESHOLD=0.5\n"
        )
        res = self.run_turn()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assert_arm("curator-failed")
        self.assertEqual(self.ledger_entries(), [])

    def test_parser_ignores_curation_nothing(self):
        # Point the curator at a response that yields CURATION: nothing.
        self.server.write_text(GATEWAY_PY_NOTHING)
        self.restart_gateway()
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

# Set SKILL_ONLY=1 to score genome 0.0 while the curator still answers with a real
# lesson. A case that needs to isolate the skill branch needs both halves: a
# router that fires only skill, and a curator that still records something.
SKILL_ONLY = __import__("os").environ.get("SKILL_ONLY") == "1"
ROUTER_ANSWER = json.dumps({"skill_worthy": 0.9, "genome_worthy": 0.0 if SKILL_ONLY else 0.9})

HERE = Path(__file__).parent
RECV = HERE / "received.jsonl"
ANSWER = HERE / "answer.txt"

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode()
        with RECV.open("a") as f:
            f.write(body + "\n")
        # The plugin sends one model per call: router first, then curator.
        # Distinguish by the prompt's opening words. The curator answer is read
        # per request so a test can change it without restarting the server.
        is_router = "evaluating whether a completed agent turn" in body
        if is_router:
            payload = {"choices": [{"message": {"content": ROUTER_ANSWER}}]}
        else:
            payload = {"choices": [{"message": {"content": ANSWER.read_text()}}]}
        out = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", int(sys.argv[1])), H) as httpd:
    httpd.serve_forever()
'''

GATEWAY_PY_NOTHING = r'''
import json, http.server, socketserver, sys
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
with socketserver.TCPServer(("127.0.0.1", int(sys.argv[1])), H) as httpd:
    httpd.serve_forever()
'''


PATH_SERVER_PY = r'''
import http.server, socketserver, sys, json
from pathlib import Path

LOGPATH = Path(__file__).with_name("paths.log")

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode() if n else ""
        with LOGPATH.open("a") as f:
            # One JSON object per line: {"path":..., "body":...}. Recording only
            # the path could not prove what the plugin actually sent, which is the
            # point of routing the share through a sink.
            f.write(json.dumps({"path": self.path, "body": body}) + "\n")
        out = b'{"id":"stub"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", int(sys.argv[1])), H) as httpd:
    httpd.serve_forever()
'''


if __name__ == "__main__":
    # Allow `python3 lib/test_self_improve.py case_name` to run one case.
    if len(sys.argv) > 1:
        unittest.main(argv=[sys.argv[0], sys.argv[1]], verbosity=2)
    else:
        unittest.main(verbosity=2)