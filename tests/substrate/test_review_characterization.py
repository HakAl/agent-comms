"""Behavior-preserving characterization of the review CLI.

Preparatory evidence for decomposing ``agent_comms/review.py``; adds no production seam.
Frozen hand-authored data (in review_decomposition_contract.json): the argparse contract for
all 24 verbs; the names each consumer binds; and one ``review.main(argv)`` scenario per verb
against a staged record, the scenario partition proven to cover the 24 verbs once. Success
rows bind field-normalized record/summary SHA-256 digests; refusal rows bind exact normalized
stderr and a byte-identical root but the pre-refusal lock. Normalization is scoped to named
JSON fields and summary prefixes, never a global regex, so semantic values in free text stay.
"""

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import argparse
import ast
import contextlib
import copy
import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from agent_comms import review
from agent_comms.reviewing import store as reviewing_store
from agent_comms.paths import REPO_ROOT
from agent_comms.store import Store
from tests.substrate.review_decomposition_support import require_schema_v1

# Frozen expected data is loaded from review_decomposition_contract.json and rebuilt into the
# exact objects the assertions compare against; a JSON array becomes the original tuple.
_CONTRACT = json.loads(
    (Path(__file__).parent / "review_decomposition_contract.json").read_text(
        encoding="utf-8"
    )
)
require_schema_v1(_CONTRACT)


def _tuplify(obj):
    if isinstance(obj, list):
        return tuple(_tuplify(item) for item in obj)
    return obj


EXPECTED_VERBS = {v: _tuplify(val) for v, val in _CONTRACT["argparse_verbs"].items()}
EXPECTED_CONSUMER_SYMBOLS = {
    k: set(n) for k, n in _CONTRACT["consumer_symbols"].items()
}
_RC = _CONTRACT["record_constants"]
DOD, INTENT, CLOSEOUT = _RC["DOD"], _RC["INTENT"], _RC["CLOSEOUT"]
DELTA_VERIF, WORKER_EVIDENCE = _RC["DELTA_VERIF"], _RC["WORKER_EVIDENCE"]
FINDING_OPEN, FINDING_NIT = _RC["FINDING_OPEN"], _RC["FINDING_NIT"]
_REFUSALS = {
    v: (r["is_exec"], r["state"], r["over"], r["tail"], r["stderr"])
    for v, r in _CONTRACT["refusals"].items()
}
_MUTATIONS = {
    v: (
        r["is_exec"],
        r["state"],
        r["over"],
        r["tail"],
        r["stdout"],
        r["rpin"],
        r["spin"],
    )
    for v, r in _CONTRACT["mutations"].items()
}
PINS = _CONTRACT["pins"]
_TS_KEYS = frozenset(_CONTRACT["norm_ts_keys"])
_OID_KEYS = frozenset(_CONTRACT["norm_oid_keys"])
_SIG_KEYS = frozenset(_CONTRACT["norm_sig_keys"])
_PATH_KEYS = frozenset(_CONTRACT["norm_path_keys"])
_SUMMARY_PATH_PREFIXES = tuple(_CONTRACT["summary_path_prefixes"])
_SUMMARY_OID_PREFIXES = tuple(_CONTRACT["summary_oid_prefixes"])
STATUS_MASKED = frozenset(_CONTRACT["status_masked"])
BASE_RECORD_SKELETON = _CONTRACT["base_record_skeleton"]

# --- Part A: argparse contract ---------------------------------------------
# Each option row is (option_strings, dest, required, default, choices, nargs, type_name,
# action); option_strings is the complete tuple, so any alias change fails. approve's
# --approver default is env-derived (ENV_USER). EXPECTED_VERBS (the frozen 24-verb
# contract) is loaded from review_decomposition_contract.json above.
ENV_USER = "<env:USER>"
_ACTION_TAGS = {
    "_StoreAction": "store",
    "_StoreTrueAction": "store_true",
    "_AppendAction": "append",
}


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("review parser exposes no subcommands")


def _option_row(verb: str, action: argparse.Action) -> tuple:
    flags = tuple(action.option_strings)
    default = action.default
    if verb == "approve" and "--approver" in flags:
        default = ENV_USER
    choices = tuple(action.choices) if action.choices else None
    type_name = getattr(action.type, "__name__", None)
    tag = _ACTION_TAGS.get(type(action).__name__, type(action).__name__)
    return (
        flags,
        action.dest,
        bool(action.required),
        default,
        choices,
        action.nargs,
        type_name,
        tag,
    )


class ReviewArgparseContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = review.build_parser()
        self.subs = _subparsers(self.parser)

    def test_exactly_twenty_four_verbs(self) -> None:
        self.assertEqual(sorted(self.subs.choices), sorted(EXPECTED_VERBS))
        self.assertEqual(len(self.subs.choices), 24)

    def test_subparsers_required(self) -> None:
        self.assertTrue(self.subs.required)
        self.assertEqual(self.subs.dest, "verb")

    def test_contract_rejects_non_v1_schema_version(self) -> None:
        for bad in (2, "1", 1.0, None):
            with self.assertRaises(ValueError):
                require_schema_v1({"schema_version": bad})

    def test_each_verb_matches_frozen_contract(self) -> None:
        examined = set()
        for verb, subparser in self.subs.choices.items():
            with self.subTest(verb=verb):
                handler = subparser.get_default("func")
                handler_name = getattr(handler, "__name__", None)
                verb_default = subparser.get_default("verb")
                rows = tuple(
                    _option_row(verb, a)
                    for a in subparser._actions
                    if not isinstance(a, argparse._HelpAction)
                )
                self.assertEqual(
                    (handler_name, verb_default, rows), EXPECTED_VERBS[verb]
                )
                examined.add(verb)
        self.assertEqual(examined, set(EXPECTED_VERBS))
        self.assertEqual(len(examined), 24)

    def test_approver_default_is_env_derived(self) -> None:
        approve = self.subs.choices["approve"]
        approver = next(
            a for a in approve._actions if a.option_strings == ["--approver"]
        )
        self.assertEqual(approver.default, os.environ.get("USER", "unknown"))


# --- Part B: consumer symbol binding ---------------------------------------
# Exact review names each consumer binds; 8 distinct across 8 references.
# EXPECTED_CONSUMER_SYMBOLS is loaded from the contract.
CONSUMERS = {
    "push_approval": "agent_comms/push_approval.py",
}


def _review_names(path: Path) -> set:
    """Names this module binds off ``agent_comms.review`` (attrs and imports)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "review"
        ):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "agent_comms.review" or module.split(".")[-1:] == ["review"]:
                names.update(alias.name for alias in node.names)
    return names


class ReviewConsumerBindingTest(unittest.TestCase):
    def test_consumer_symbol_mapping_frozen(self) -> None:
        for consumer, rel in CONSUMERS.items():
            with self.subTest(consumer=consumer):
                actual = _review_names(REPO_ROOT / rel)
                self.assertEqual(actual, EXPECTED_CONSUMER_SYMBOLS[consumer])

    def test_reference_totals(self) -> None:
        distinct = set().union(*EXPECTED_CONSUMER_SYMBOLS.values())
        self.assertEqual(len(distinct), 8)
        self.assertEqual(sum(len(v) for v in EXPECTED_CONSUMER_SYMBOLS.values()), 8)

    def test_every_bound_name_present_in_review(self) -> None:
        for consumer, symbols in EXPECTED_CONSUMER_SYMBOLS.items():
            for name in symbols:
                with self.subTest(consumer=consumer, name=name):
                    self.assertTrue(
                        hasattr(review, name), f"{consumer} binds absent review.{name}"
                    )


# --- Part C: raw review.main(argv) characterization ------------------------
# Every scenario drives review.main(argv) against a staged record, freezing the unassisted
# contract each verb honors past record lookup and its state gate.
BRIEF_TEXT = (
    "# Test brief\n\n## Surface\nx\n\n## Anti-claims\nx\n\n"
    "## Definition of Done\nx\n\n## Process\nx\n\n"
    "## Production surface\n- touches: none; reason: test\n"
)
WORKER = "gamma-codex-worker"
ARCHITECT = "gamma-architect"
TS0 = "2026-01-01T00:00:00Z"
FAKE_A = "a" * 40
FAKE_B = "b" * 40
WID = "dispatch_20260101_000000_0000abcd"
# Field-normalization vocabulary (contract: _TS_KEYS/_OID_KEYS/_SIG_KEYS/_PATH_KEYS and the
# summary prefixes): only these named fields/prefixes are masked, field-scoped and never by
# whole-JSON regex, so a semantic OID/timestamp/path in free text stays visible.
_HEX40 = re.compile(r"[0-9a-f]{40}")
_SUMMARY_TS_ROW = re.compile(
    r"^(- )\d{4}-\d\d-\d\dT[\d:.+\-]+Z?(: )"
)  # brief-check rows


# DOD/INTENT/CLOSEOUT/DELTA_VERIF/WORKER_EVIDENCE/FINDING_OPEN/FINDING_NIT are frozen record
# fixtures loaded from the contract (same TS0/WID/ARCHITECT/WORKER/FAKE values inlined there).
def _canon(obj, mask_path=None):
    """Mask named TS/OID/SIG/path fields by key; path fields only when mask_path is given, and values under other keys are untouched."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in _TS_KEYS and isinstance(value, str) and value:
                out[key] = "<TS>"
            elif (
                key in _OID_KEYS and isinstance(value, str) and _HEX40.fullmatch(value)
            ):
                out[key] = "<OID>"
            elif key in _SIG_KEYS and isinstance(value, str) and value:
                out[key] = "<SIG>"
            elif (
                key in _PATH_KEYS
                and isinstance(value, str)
                and value
                and mask_path is not None
            ):
                out[key] = mask_path(value)
            else:
                out[key] = _canon(value, mask_path)
        return out
    if isinstance(obj, list):
        return [_canon(item, mask_path) for item in obj]
    return obj


class ReviewEnv(unittest.TestCase):
    """Real team topology: integration checkout + review worktree (ReviewToolTest.setUp minus evidence bootstrap)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.review_root = self.root / "reviews"
        self.ledger = self.root / "ledger.sqlite"
        store = Store(self.ledger)
        store.init()
        store.register_agent_actor(
            ARCHITECT, "agentcomms", "architect", str(self.root / "architect"), []
        )
        store.register_agent_actor(
            WORKER,
            "agentcomms",
            "worker",
            str(self.root / "worker"),
            [],
            owner=ARCHITECT,
        )
        self.integration = self.root / "integration"
        self._init_repo(self.integration)
        self._git("checkout", "-B", "integration-main", self.integration)
        (self.integration / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "tracked.txt", self.integration)
        self._git("commit", "-m", "base", self.integration)
        self.repo = self.root / "repo"
        self._git(
            "worktree", "add", "-b", "work-branch", str(self.repo), self.integration
        )
        (self.repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self._git("add", "tracked.txt", self.repo)
        self._git("commit", "-m", "initial", self.repo)
        import sqlite3

        with contextlib.closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.execute(
                "update actors set project_root=? where id=?", (str(self.repo), WORKER)
            )
        self.brief = self.root / "brief.md"
        self.brief.write_text(BRIEF_TEXT, encoding="utf-8")
        self.dod = self.root / "dod.json"
        self.dod.write_text(
            json.dumps([{"id": "unit", "claim": "c", "check_id": "green"}]),
            encoding="utf-8",
        )
        self.logf = self.root / "evidence.log"
        self.logf.write_text("ok\n", encoding="utf-8")
        self._patch(
            mock.patch.object(review.runtime_paths, "db_path", return_value=self.ledger)
        )
        self._patch(mock.patch.object(reviewing_store, "REVIEW_ROOT", self.review_root))
        self._patch(mock.patch.object(review, "REVIEW_ROOT", self.review_root))
        self._patch(
            mock.patch.dict(
                os.environ, {"AGENT_COMMS_MAIN": str(self.integration)}, clear=False
            )
        )

    def _patch(self, patcher) -> None:
        patcher.start()
        self.addCleanup(patcher.stop)

    def _init_repo(self, repo: Path) -> None:
        repo.mkdir()
        self._git("init", repo)
        self._git("config", "user.email", "t@t.invalid", repo)
        self._git("config", "user.name", "T", repo)

    def _git(self, *args) -> str:
        *rest, cwd = args
        return subprocess.run(
            ["git", *rest],
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout

    def cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = review.main(list(argv))
            except SystemExit as exc:
                rc = int(exc.code) if isinstance(exc.code, int) else 1
        return rc, out.getvalue(), err.getvalue()

    def _mask_paths(self, text: str) -> str:
        text = text.replace(str(self.root.resolve()), "<ROOT>").replace(
            str(self.root), "<ROOT>"
        )
        # Longest first: the interpreter sits under the install prefix (the
        # checkout's .venv here), which sits under the checkout root, and a
        # resolved prefix (/private/var/...) contains the unresolved one.
        text = text.replace(sys.executable, "<INTERP>")
        for prefix in sorted({str(Path(sys.prefix).resolve()), sys.prefix}, key=len, reverse=True):
            text = text.replace(prefix, "<PREFIX>")
        return text.replace(str(REPO_ROOT), "<REPO>")

    def _mask_err(self, text: str) -> str:
        """Mask the volatile values an error emits: temp paths plus the exact review-worktree and integration-checkout HEADs (exact-string, not regex)."""
        head = self._git("rev-parse", "HEAD", self.repo).strip()
        integration_head = self._git("rev-parse", "HEAD", self.integration).strip()
        masked = self._mask_paths(text).replace(head, "<HEAD>")
        return masked.replace(integration_head, "<IHEAD>")

    def _sha(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def digest_record(self, did: str) -> str:
        raw = json.loads((self.review_root / f"{did}.json").read_text(encoding="utf-8"))
        return self._sha(json.dumps(_canon(raw, self._mask_paths), sort_keys=True))

    def digest_summary(self, did: str) -> str:
        return self._sha(
            self._norm_summary(
                (self.review_root / f"{did}.summary.md").read_text(encoding="utf-8")
            )
        )

    def _norm_summary(self, text: str) -> str:
        """Line-scoped: mask paths after Repo/Brief, OIDs after HEAD prefixes, and the leading timestamp of brief-check rows; other text stays visible."""
        out = []
        for line in text.splitlines(keepends=True):
            path_pre = next(
                (p for p in _SUMMARY_PATH_PREFIXES if line.startswith(p)), None
            )
            oid_pre = next(
                (p for p in _SUMMARY_OID_PREFIXES if line.startswith(p)), None
            )
            if path_pre:
                line = path_pre + self._mask_paths(line[len(path_pre) :])
            elif oid_pre:
                line = oid_pre + _HEX40.sub("<OID>", line[len(oid_pre) :])
            else:
                line = _SUMMARY_TS_ROW.sub(r"\1<TS>\2", line)
            out.append(line)
        return "".join(out)

    def snapshot_review_root(self) -> dict:
        out: dict = {}
        if self.review_root.exists():
            for p in sorted(self.review_root.rglob("*")):
                if p.is_file():
                    out[str(p.relative_to(self.review_root))] = p.read_bytes()
        return out

    def bindings_state(self) -> dict:
        """Normalized binding-registry result: each claim's field-masked JSON."""
        root = self.review_root / "bindings"
        out: dict = {}
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file() and not any(
                    part.startswith(".") for part in p.relative_to(root).parts
                ):
                    rel = str(p.relative_to(root))
                    try:
                        out[rel] = _canon(json.loads(p.read_text(encoding="utf-8")))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        out[rel] = "<opaque>"
        return out

    def write_record(self, record: dict, did: str) -> Path:
        self.review_root.mkdir(parents=True, exist_ok=True)
        path = self.review_root / f"{did}.json"
        path.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    def base_record(self, did: str, state: str, **over) -> dict:
        # Schema-2 defaults live in BASE_RECORD_SKELETON (frozen in the contract); only the
        # per-call dispatch_id, state, and brief_path are filled before scenario overrides.
        record = copy.deepcopy(BASE_RECORD_SKELETON)
        record["dispatch_id"] = did
        record["state"] = state
        record["brief_path"] = str(self.brief)
        record.update(over)
        return record

    def exec_record(self, did: str, state: str, **over) -> dict:
        record = self.base_record(
            did,
            state,
            repo=str(self.repo),
            base_commit=FAKE_B,
            reviewed_head=FAKE_A,
            approved_head=FAKE_A,
            intended_dispatches=[dict(INTENT)],
            worker_evidence=[dict(WORKER_EVIDENCE)],
            trigger_closed=True,
            history=[
                {"event": "open", "timestamp": TS0},
                {"event": "mark-executed", "timestamp": TS0},
            ],
        )
        record.update(over)
        return record


# Refusal scenarios: verb -> (exec_fixture?, state, overrides, argv_tail, stderr). Each verb
# is staged past record lookup and its state gate so it refuses at ITS OWN external-authority
# boundary; the only observable is the empty lock opened before refusing, and stderr is the
# exact normalized stderr. _REFUSALS/_MUTATIONS/PINS are frozen in the contract verbatim.
class PerVerbCharacterizationTest(ReviewEnv):
    def _stage(self, verb, is_exec, state, over):
        import shutil

        if self.review_root.exists():
            shutil.rmtree(self.review_root)
        over = {k: (str(self.repo) if v == "SELF" else v) for k, v in over.items()}
        self.write_record(
            (self.exec_record if is_exec else self.base_record)(verb, state, **over),
            verb,
        )

    def _resolve_arg(self, arg):
        return str(self.logf) if arg == "LOG" else arg

    def test_scenario_partition_covers_the_24_verbs_exactly_once(self) -> None:
        # The brief's "exactly 24 distinct verbs": refusals, mutations, and the five separate verbs are a disjoint partition whose union is EXPECTED_VERBS.
        separate = {"open", "summary", "status", "recover-binding", "rebind-dod"}
        groups = [set(_REFUSALS), set(_MUTATIONS), separate]
        union = set().union(*groups)
        self.assertEqual(sum(len(g) for g in groups), len(union))  # pairwise disjoint
        self.assertEqual(union, set(EXPECTED_VERBS))
        self.assertEqual(len(union), 24)

    def test_every_refusal_is_verb_specific_and_lock_only(self) -> None:
        for verb, (is_exec, state, over, tail, stderr) in _REFUSALS.items():
            with self.subTest(verb=verb):
                self._stage(verb, is_exec, state, over)
                before = self.snapshot_review_root()
                argv = [
                    verb,
                    "--dispatch-id",
                    verb,
                    *(self._resolve_arg(a) for a in tail),
                ]
                rc, out, err = self.cli(*argv)
                after = self.snapshot_review_root()
                self.assertEqual(rc, 1, err)
                self.assertEqual(out, "")
                self.assertEqual(
                    self._mask_err(err), stderr
                )  # complete normalized stderr
                created = sorted(set(after) - set(before))
                self.assertEqual(created, [f"{verb}.lock"])
                self.assertEqual(after[f"{verb}.lock"], b"")
                self.assertEqual(
                    {k: before[k] for k in before}, {k: after[k] for k in before}
                )
                self.assertEqual(self.bindings_state(), {})

    def test_every_mutation_binds_record_summary_and_bindings(self) -> None:
        for verb, (
            is_exec,
            state,
            over,
            tail,
            stdout,
            rpin,
            spin,
        ) in _MUTATIONS.items():
            with self.subTest(verb=verb):
                self._stage(verb, is_exec, state, over)
                rc, out, err = self.cli(verb, "--dispatch-id", verb, *tail)
                self.assertEqual(rc, 0, err)
                self.assertEqual(out, stdout)
                self.assertEqual(self.digest_record(verb), PINS[rpin])
                self.assertEqual(self.digest_summary(verb), PINS[spin])
                self.assertEqual(self.bindings_state(), {})

    def test_open_persists_normalized_record_and_summary(self) -> None:
        rc, out, err = self.cli(
            "open",
            "--dispatch-id",
            "openok",
            "--brief",
            str(self.brief),
            "--dod",
            str(self.dod),
            "--repo",
            str(self.repo),
            "--expected-producer",
            ARCHITECT,
            "--expected-recipient",
            WORKER,
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        record = json.loads(
            (self.review_root / "openok.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["state"], "drafted_brief")
        self.assertEqual(record["expected_producer"], ARCHITECT)
        self.assertEqual(record["expected_recipient"], WORKER)
        self.assertEqual([h["event"] for h in record["history"]], ["open"])
        self.assertEqual(self.digest_record("openok"), PINS["PIN_OPEN_R"])
        self.assertEqual(self.digest_summary("openok"), PINS["PIN_OPEN_S"])
        self.assertEqual(self.bindings_state(), {})

    def test_rebind_dod_replaces_binding_and_appends_history(self) -> None:
        old_dod = self.root / "old-dod.json"
        old_bytes = json.dumps(
            [{"id": "old", "claim": "old", "check_id": "green"}]
        ).encode()
        old_dod.write_bytes(old_bytes)
        self.dod.write_text(
            json.dumps(
                [
                    {"id": "new-1", "claim": "first", "check_id": "green"},
                    {"id": "new-2", "claim": "second", "check_id": "green"},
                ]
            ),
            encoding="utf-8",
        )
        old_sha256 = hashlib.sha256(old_bytes).hexdigest()
        new_sha256 = hashlib.sha256(self.dod.read_bytes()).hexdigest()
        self.write_record(
            self.base_record(
                "rebindok",
                "brief_reviewed",
                dod=[dict(DOD, id="old", claim="old")],
                dod_path=str(old_dod),
                dod_sha256=old_sha256,
            ),
            "rebindok",
        )

        rc, out, err = self.cli(
            "rebind-dod",
            "--dispatch-id",
            "rebindok",
            "--dod",
            str(self.dod),
            "--reason",
            "harden criteria",
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        record = json.loads(
            (self.review_root / "rebindok.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            _canon(
                {
                    "dod": record["dod"],
                    "dod_path": record["dod_path"],
                    "dod_sha256": record["dod_sha256"],
                    "history": record["history"],
                },
                self._mask_paths,
            ),
            {
                "dod": [
                    dict(DOD, id="new-1", claim="first"),
                    dict(DOD, id="new-2", claim="second"),
                ],
                "dod_path": "<ROOT>/dod.json",
                "dod_sha256": new_sha256,
                "history": [
                    {"event": "open", "timestamp": "<TS>"},
                    {
                        "event": "rebind-dod",
                        "timestamp": "<TS>",
                        "reason": "harden criteria",
                        "old_sha256": old_sha256,
                        "new_sha256": new_sha256,
                        "old_criterion_count": 1,
                        "new_criterion_count": 2,
                    },
                ],
            },
        )
        self.assertEqual(self.bindings_state(), {})

    def test_summary_reads_without_mutation(self) -> None:
        self.write_record(self.base_record("sumrec", "drafted_brief"), "sumrec")
        before = self.snapshot_review_root()
        rc, out, err = self.cli("summary", "--dispatch-id", "sumrec")
        after = self.snapshot_review_root()
        self.assertEqual(rc, 0, err)
        self.assertEqual(before, after)  # pure read: byte-identical, no lock
        self.assertEqual(self._sha(self._norm_summary(out)), PINS["PIN_SUM_STDOUT"])


class StatusBindingCharacterizationTest(ReviewEnv):
    # Named path/interpreter fields of the status binding; only these are masked
    # (STATUS_MASKED is frozen in the contract).
    _STATUS_MASKED = STATUS_MASKED

    def test_status_without_expected_root_is_binding_unverified(self) -> None:
        self.write_record(self.base_record("statrec", "drafted_brief"), "statrec")
        before = self.snapshot_review_root()
        rc, out, err = self.cli("status", "--dispatch-id", "statrec")
        after = self.snapshot_review_root()
        self.assertEqual(rc, 1)
        self.assertEqual(err, "")
        self.assertEqual(before, after)  # status is a pure read: byte-identical
        payload = json.loads(out)
        binding = payload["binding"]
        # Raw cross-checks that normalization would otherwise collapse: the
        # binding is to the install prefix, never to a checkout or the cwd.
        self.assertEqual(binding["actual_repo_root"], str(Path(sys.prefix).resolve()))
        self.assertNotEqual(binding["actual_repo_root"], binding["cwd"])
        self.assertEqual(binding["interpreter"], sys.executable)
        norm_binding = {
            k: (self._mask_paths(v) if k in self._STATUS_MASKED else v)
            for k, v in binding.items()
        }
        self.assertEqual(payload["diagnosis"], "binding_unverified")
        self.assertIsNone(payload["record"])
        self.assertEqual(
            norm_binding,
            {
                "actual_repo_root": "<PREFIX>",
                "cwd": "<REPO>",
                "cwd_shadow": False,
                "expected_repo_root": None,
                "expected_source": None,
                "interpreter": "<INTERP>",
                "module": "<REPO>/agent_comms/review.py",
                "record_path": None,
                "review_root": "<ROOT>/reviews",
            },
        )


class BindingRegistryCharacterizationTest(ReviewEnv):
    def test_recover_binding_quarantines_malformed_claim(self) -> None:
        bindings = self.review_root / "bindings"
        bindings.mkdir(parents=True)
        (bindings / WID).write_text("{not json", encoding="utf-8")
        before = self.snapshot_review_root()
        rc, out, err = self.cli(
            "recover-binding", "--worker-dispatch-id", WID, "--quarantine-malformed"
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, f"recover-binding {WID}: quarantined malformed claim\n")
        state = self.bindings_state()
        self.assertEqual(
            state[WID],
            {
                "worker_dispatch_id": WID,
                "state": "quarantined",
                "quarantined_at": "<TS>",
                "forensics": "<SIG>",
            },
        )
        self.assertTrue((bindings / "quarantine").is_dir())
        self.assertNotEqual(self.snapshot_review_root(), before)

    def test_recover_binding_holds_bindings_lock_before_mutation(self) -> None:
        # Bounded lock-visible proof: while the test holds bindings/.lock, recovery
        # must block (claim byte-identical) and complete only on release; daemon
        # thread + finite joins prevent a hang, and it fails if recovery stops locking.
        bindings = self.review_root / "bindings"
        bindings.mkdir(parents=True)
        (bindings / WID).write_text("{not json", encoding="utf-8")
        claim_before = (bindings / WID).read_bytes()
        result: dict = {}

        def run() -> None:
            result["out"] = self.cli(
                "recover-binding", "--worker-dispatch-id", WID, "--quarantine-malformed"
            )

        with (bindings / ".lock").open("a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            time.sleep(0.3)  # let the worker reach the blocking flock
            worker.join(timeout=0.5)
            self.assertTrue(worker.is_alive())  # blocked on bindings/.lock
            self.assertEqual(
                (bindings / WID).read_bytes(), claim_before
            )  # unmutated while blocked
            self.assertFalse((bindings / "quarantine").exists())
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())  # proceeds once the lock is released
        rc, out, err = result["out"]
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, f"recover-binding {WID}: quarantined malformed claim\n")
        self.assertEqual(self.bindings_state()[WID]["state"], "quarantined")
        self.assertTrue((bindings / "quarantine").is_dir())

    def test_recover_binding_removes_unbound_orphan(self) -> None:
        self.write_record(self.base_record("orphanrec", "drafted_brief"), "orphanrec")
        bindings = self.review_root / "bindings"
        bindings.mkdir(parents=True, exist_ok=True)
        orphan = "dispatch_20260731_210000_0000beef"
        (bindings / orphan).write_text(
            json.dumps(
                {
                    "worker_dispatch_id": orphan,
                    "record_id": "orphanrec",
                    "state": "pending",
                }
            ),
            encoding="utf-8",
        )
        rc, out, err = self.cli("recover-binding", "--worker-dispatch-id", orphan)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, f"recover-binding {orphan}: orphan removed\n")
        self.assertEqual(self.bindings_state(), {})  # claim removed: registry empty
        history = json.loads((self.review_root / "orphanrec.json").read_text())[
            "history"
        ]
        self.assertEqual(history[-1]["event"], "recover-binding-orphan-removed")


class NormalizationScopeTest(ReviewEnv):
    def test_semantic_oid_timestamp_path_survive_normalization(self) -> None:
        # Red control for over-masking: the same volatile-looking values are masked under a named field/prefix but preserved in free text.
        oid, ts = "c" * 40, "2026-01-01T00:00:00Z"
        canon = _canon(
            {
                "reviewed_head": oid,
                "problem": oid,
                "created_at": ts,
                "note": ts,
                "repo": str(self.root),
                "impact": str(self.root),
            },
            self._mask_paths,
        )
        self.assertEqual(
            canon,
            {
                "reviewed_head": "<OID>",
                "problem": oid,
                "created_at": "<TS>",
                "note": ts,
                "repo": "<ROOT>",
                "impact": str(self.root),
            },
        )
        summary = (
            f"- Repo: {self.root}\n- Reviewed HEAD: {oid}\n- {ts}: architect clean\n"
            f"- F1 [blocking/open]: {oid} at {ts} in {self.root} -> fix\n"
        )
        lines = self._norm_summary(summary).splitlines()
        self.assertEqual(
            lines[:3],
            ["- Repo: <ROOT>", "- Reviewed HEAD: <OID>", "- <TS>: architect clean"],
        )
        # Semantic OID, timestamp, and path in the finding line remain visible.
        self.assertEqual(
            lines[3], f"- F1 [blocking/open]: {oid} at {ts} in {self.root} -> fix"
        )


class SchemaOnePriorReadOnlyTest(ReviewEnv):
    def test_schema1_summary_status_read_only_and_mutation_refused(self) -> None:
        record = {
            "schema_version": 1,
            "dispatch_id": "s1",
            "state": "merge_eligible",
            "repo": str(self.repo),
            "brief_path": str(self.brief),
            "dod": [],
            "findings": [],
            "gate_runs": [],
        }
        path = self.write_record(record, "s1")
        before = path.read_bytes()
        rc, out, err = self.cli("summary", "--dispatch-id", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("diagnosis: prior_schema_read_only", out)
        rc, out, err = self.cli(
            "status",
            "--dispatch-id",
            "s1",
            "--expected-repo-root",
            sys.prefix,
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["diagnosis"], "prior_schema_read_only")
        self.assertEqual(path.read_bytes(), before)  # reads did not mutate
        rc, _out, err = self.cli(
            "brief-check",
            "--dispatch-id",
            "s1",
            "--clean",
            "--by",
            "architect",
            "--surface-verdict",
            "complete",
            "--surface-reason",
            "r",
        )
        self.assertEqual(rc, 1)
        self.assertIn("prior_schema_read_only", err)
        self.assertEqual(path.read_bytes(), before)  # mutation refused before effects


class BriefDriftCharacterizationTest(ReviewEnv):
    def test_brief_drift_persists_brief_revised_even_when_callback_refuses(
        self,
    ) -> None:
        rc, _out, err = self.cli(
            "open",
            "--dispatch-id",
            "drift",
            "--brief",
            str(self.brief),
            "--dod",
            str(self.dod),
            "--repo",
            str(self.repo),
            "--expected-producer",
            ARCHITECT,
            "--expected-recipient",
            WORKER,
        )
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.cli(
            "brief-check",
            "--dispatch-id",
            "drift",
            "--clean",
            "--by",
            "architect",
            "--surface-verdict",
            "complete",
            "--surface-reason",
            "r",
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            json.loads((self.review_root / "drift.json").read_text())["state"],
            "brief_reviewed",
        )
        self.brief.write_text(BRIEF_TEXT + "\n## Extra\ndrifted\n", encoding="utf-8")
        rc, _out, err = self.cli(
            "mark-dispatched", "--dispatch-id", "drift", "--idempotency-key", "k"
        )
        self.assertEqual(rc, 1)
        record = json.loads((self.review_root / "drift.json").read_text())
        self.assertEqual(record["state"], "brief_revised")
        self.assertEqual(record["brief_revision"], 1)
        self.assertEqual(record["state_before_brief_revised"], "brief_reviewed")


if __name__ == "__main__":
    unittest.main()
