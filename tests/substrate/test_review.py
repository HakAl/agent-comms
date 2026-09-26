import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import argparse
import json
import re
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import paths
from agent_comms import review
from agent_comms.reviewing import approval, checks, execution, store
from agent_comms.review import (
    ReviewError,
    _BRIEF_SECTION_RULES,
    _PART_HEADING_REGEX,
    validate_brief_sections,
)
from agent_comms.store import Store


briefs_dir = paths.REPO_ROOT / "local" / "dispatch" / "briefs"


class BaseReviewBriefSectionTest:
    production_surface = "## Production surface\n- touches: none; reason: test fixture\n"

    def test_check_id_vocabularies_are_closed_and_classifier_uses_evidence_enum(self) -> None:
        self.assertEqual(review.EXECUTABLE_CHECK_IDS, {"green", "fail", "unittest", "extra"})
        self.assertEqual(review.EVIDENCE_ONLY_CHECK_IDS, {"runtime-cert"})
        self.assertTrue(review.is_evidence_only({"check_id": "runtime-cert"}))
        self.assertFalse(review.is_evidence_only({"check_id": "extra"}))

    def test_workflow_state_sets_are_derived_from_authoritative_semantics(self) -> None:
        self.assertEqual(review.REVIEW_STATES, frozenset(review.STATE_SEMANTICS))
        self.assertEqual(
            review.PRE_APPROVAL_STATES,
            frozenset(
                state for state, semantics in review.STATE_SEMANTICS.items()
                if semantics.brief_revisable
            ),
        )
        self.assertEqual(
            review.EXECUTION_BOUND_STATES,
            frozenset(
                state for state, semantics in review.STATE_SEMANTICS.items()
                if semantics.execution_evidence
            ),
        )
        self.assertTrue(all(type(value) is review.StateSemantics for value in review.STATE_SEMANTICS.values()))

    def write_brief(self, body: str) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        brief_path = Path(temp.name) / "brief.md"
        brief_path.write_text(body, encoding="utf-8")
        return brief_path

    def assert_missing(self, body: str, *labels: str) -> None:
        with self.assertRaises(ReviewError) as cm:
            validate_brief_sections(self.write_brief(body))
        message = str(cm.exception)
        self.assertIn("missing required level-2 sections", message)
        for label in labels:
            self.assertIn(label, message)

    def test_brief_missing_scope_section_raises_review_error(self) -> None:
        self.assert_missing(
            "# Test brief\n\n"
            "## Anti-claims (NOT in scope)\n"
            "## Definition of Done\n"
            "## Process\n",
            "Scope",
        )

    def test_brief_missing_anti_scope_section_raises_review_error(self) -> None:
        self.assert_missing(
            "# Test brief\n\n"
            "## Surface\n"
            "## Definition of Done\n"
            "## Process\n",
            "Anti-scope",
        )

    def test_brief_missing_dod_section_raises_review_error(self) -> None:
        self.assert_missing(
            "# Test brief\n\n"
            "## Surface\n"
            "## Anti-claims\n"
            "## Process\n",
            "Definition of Done",
        )

    def test_brief_missing_process_section_raises_review_error(self) -> None:
        self.assert_missing(
            "# Test brief\n\n"
            "## Surface\n"
            "## Anti-claims\n"
            "## Definition of Done\n",
            "Process",
        )

    def test_brief_missing_multiple_sections_lists_each_in_message(self) -> None:
        self.assert_missing(
            "# Test brief\n\n"
            "## Surface\n",
            "Anti-scope",
            "Definition of Done",
            "Process",
        )

    def test_brief_with_all_five_required_sections_passes_validation(self) -> None:
        self.assertEqual(len(_BRIEF_SECTION_RULES), 5)
        self.assertIsNotNone(_PART_HEADING_REGEX.match("part 42"))
        brief_path = self.write_brief(
            "# Test brief\n\n"
            "## Surface\n"
            "## Anti-claims\n"
            "## Definition of Done\n"
            "## Process\n" + self.production_surface
        )
        self.assertIsNone(validate_brief_sections(brief_path))


class PriorBriefRevisionStateValidationTest(unittest.TestCase):
    def record(self) -> dict:
        return {
            "schema_version": review.SCHEMA_VERSION,
            "dispatch_id": "prior-state-validation",
            "state": "brief_revised",
            "repo": "/tmp/repo",
            "base_commit": None,
            "reviewed_head": None,
            "approved_head": None,
            "target_branch": "worker/test",
            "expected_producer": "gamma-architect",
            "expected_recipient": "gamma-codex-worker",
            "intended_dispatches": [],
            "worker_evidence": [],
            "blocked_dispatches": [],
            "superseded_dispatches": [],
            "blocked_redispatch_count": 0,
            "max_blocked_redispatches": 3,
            "brief_path": "/tmp/brief.md",
            "brief_sha256": "a" * 64,
            "brief_revision": 1,
            "brief_checks": [],
            "dod": [],
            "findings": [],
            "gate_runs": [],
            "gate_epoch": 0,
            "skips": [],
            "approval": None,
            "respawn_count": 0,
            "max_respawns": 3,
            "history": [],
            "created_at": "2026-07-27T00:00:00Z",
            "updated_at": "2026-07-27T00:00:00Z",
        }

    def bind_execution_evidence(self, record: dict) -> None:
        record["reviewed_head"] = "b" * 40
        record["trigger_closed"] = True
        record["intended_dispatches"] = [{
            "attempt": 1,
            "idempotency_key": "prior-state-validation",
            "recorded_at": "2026-07-27T00:00:00Z",
        }]
        record["worker_evidence"] = [{
            "worker_dispatch_id": "dispatch_20260727_000000_00000001",
            "intent_attempt": 1,
            "idempotency_key": "prior-state-validation",
            "ledger_db": "/tmp/ledger.sqlite",
            "closeout": {
                "protocol": 1,
                "recorded_by": "gamma-codex-worker",
                "reply_message_id": "msg-reply",
                "delta": None,
            },
            "verified_at": "2026-07-27T00:00:00Z",
            "producer": "gamma-architect",
            "recipient": "gamma-codex-worker",
            "status": "closed",
            "result": "satisfied",
            "delta_verification": {
                "snapshot_tree": "c" * 40,
                "reviewed_head_tree": "c" * 40,
                "manifest_sha256": "d" * 64,
                "entries": 1,
                "status_counts": {"M": 1},
            },
            "artifact_bindings": [],
        }]

    def test_absent_prior_state_validates(self) -> None:
        review.validate_record(self.record())

    def test_every_possible_prior_state_validates_with_required_evidence(self) -> None:
        for prior_state in review.PRE_APPROVAL_STATES:
            with self.subTest(prior_state=prior_state):
                record = self.record()
                record["state_before_brief_revised"] = prior_state
                if prior_state in review.EXECUTION_BOUND_STATES:
                    self.bind_execution_evidence(record)
                review.validate_record(record)

    def test_invalid_prior_state_near_misses_and_non_strings_raise_review_error(self) -> None:
        invalid_values = [
            "executed_typo", "execut3d", "EXECUTED", "Dispatched", "",
            None, 123, True, [], {},
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                record = self.record()
                record["state_before_brief_revised"] = value
                with self.assertRaisesRegex(ReviewError, "invalid state_before_brief_revised"):
                    review.validate_record(record)

    def test_every_valid_but_impossible_prior_state_rejects_despite_full_bindings(self) -> None:
        impossible_states = review.REVIEW_STATES - review.PRE_APPROVAL_STATES
        self.assertTrue(impossible_states)
        for prior_state in impossible_states:
            with self.subTest(prior_state=prior_state):
                record = self.record()
                record["state_before_brief_revised"] = prior_state
                self.bind_execution_evidence(record)
                with self.assertRaisesRegex(ReviewError, "invalid state_before_brief_revised"):
                    review.validate_record(record)

    def test_c1_execution_bound_prior_state_without_bindings_still_rejects(self) -> None:
        record = self.record()
        record["state_before_brief_revised"] = "executed"
        with self.assertRaisesRegex(ReviewError, "execution lineage requires"):
            review.validate_record(record)

    def test_c2_dispatched_prior_state_without_bindings_still_validates(self) -> None:
        record = self.record()
        record["state_before_brief_revised"] = "dispatched"
        review.validate_record(record)


class ReviewBriefSectionTest(BaseReviewBriefSectionTest, unittest.TestCase):
    def test_brief_heading_match_is_case_insensitive(self) -> None:
        brief_path = self.write_brief(
            "# Test brief\n\n"
            "## DELIVERABLE\n"
            "## CONSTRAINTS\n"
            "## DoD\n"
            "## PROCESS\n" + self.production_surface
        )
        self.assertIsNone(validate_brief_sections(brief_path))

    def test_brief_level_3_headings_do_not_satisfy_required_sections(self) -> None:
        self.assert_missing(
            "# Test brief\n\n"
            "## Detailed plan\n"
            "### Surface\n"
            "### Anti-claims\n"
            "### Definition of Done\n"
            "### Process\n",
            "Scope",
            "Anti-scope",
            "Definition of Done",
            "Process",
        )

    def test_legacy_shipped_briefs_require_production_surface(self) -> None:
        names = [
            "close-message-reply-then-close.md",
            "retry-spawn.md",
            "p1-7-actors-json-shrink.md",
            "d3-extract.md",
            "d4-cli-split.md",
            "fk-migration-fix.md",
            "p29-test-layering.md",
        ]
        missing = [name for name in names if not (briefs_dir / name).exists()]
        if missing:
            self.skipTest(f"shipped brief fixtures incomplete in this checkout: {missing}")

        for name in names:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ReviewError, "Production surface"):
                    validate_brief_sections(briefs_dir / name)

    def compliant(self, declaration: str = "- touches: none; reason: no production state") -> str:
        return (
            "# Test brief\n\n## Surface\nChange.\n\n## Anti-claims\nNone.\n\n"
            "## Definition of Done\nGreen.\n\n## Process\nReview.\n\n"
            f"## Production surface\n{declaration}\n"
        )

    def test_production_surface_unknown_item_refuses_with_vocabulary(self) -> None:
        with self.assertRaises(ReviewError) as cm:
            validate_brief_sections(self.write_brief(self.compliant("- touches: production_db; residual: x")))
        self.assertIn("production_db", str(cm.exception))
        self.assertIn("canonical_db, installed_cli, system_interpreter, launchd, seat_config, auth_config, real_runtime, none", str(cm.exception))

    def test_production_surface_canonical_multi_item_sample_passes(self) -> None:
        declaration = (
            "- touches: installed_cli; residual: shared install is exercised at the next open\n"
            "- touches: system_interpreter; gated-by: Python 3.11 compatibility suite"
        )
        self.assertIsNone(validate_brief_sections(self.write_brief(self.compliant(declaration))))

    def test_production_surface_clauseless_and_bare_none_refuse(self) -> None:
        for line in ("- touches: canonical_db", "- touches: canonical_db; residual:", "- touches: none"):
            with self.subTest(line=line), self.assertRaisesRegex(ReviewError, re.escape(line)):
                validate_brief_sections(self.write_brief(self.compliant(line)))

    def test_production_surface_none_mixed_refuses(self) -> None:
        declaration = "- touches: none; reason: no state\n- touches: canonical_db; residual: x"
        with self.assertRaisesRegex(ReviewError, "none.*only"):
            validate_brief_sections(self.write_brief(self.compliant(declaration)))

    def test_production_surface_scope_collision_refuses(self) -> None:
        body = self.compliant().replace("## Surface\nChange.\n\n", "")
        self.assert_missing(body, "Scope")

    def test_production_surface_duplicate_heading_refuses_with_both_headings(self) -> None:
        body = self.compliant() + "\n## Production surface details\n- touches: none; reason: duplicate\n"
        with self.assertRaises(ReviewError) as cm:
            validate_brief_sections(self.write_brief(body))
        self.assertIn("`## Production surface`", str(cm.exception))
        self.assertIn("`## Production surface details`", str(cm.exception))

    def test_production_surface_duplicate_item_refuses(self) -> None:
        declaration = "- touches: canonical_db; residual: x\n- touches: canonical_db; gated-by: y"
        with self.assertRaisesRegex(ReviewError, "duplicate.*canonical_db"):
            validate_brief_sections(self.write_brief(self.compliant(declaration)))

    def test_production_surface_colon_lookalike_refuses_even_with_valid_line(self) -> None:
        declaration = "- touches: canonical_db; residual: x\n- touches : canonical_db; residual: x"
        with self.assertRaisesRegex(ReviewError, re.escape("- touches : canonical_db; residual: x")):
            validate_brief_sections(self.write_brief(self.compliant(declaration)))

    def test_command_brief_check_revalidates_brief(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            brief_path = root / "brief.md"
            brief_path.write_text(
                "# Test brief\n\n"
                "## Surface\n"
                "Touch points.\n\n"
                "## Anti-claims\n"
                "Out of scope.\n\n"
                "## Definition of Done\n"
                "Passes.\n\n"
                "## Process\n"
                "Architect commits.\n\n"
                "## Production surface\n"
                "- touches: none; reason: test fixture\n",
                encoding="utf-8",
            )
            dod_path = root / "dod.json"
            dod_path.write_text(
                json.dumps([{"id": "unit", "claim": "green check", "check_id": "green"}]),
                encoding="utf-8",
            )
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            subprocess.run(["git", "checkout", "-B", "review-branch"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, stdout=subprocess.PIPE)

            ledger_path = root / "ledger.sqlite"
            fixture_store = Store(ledger_path)
            fixture_store.init()
            fixture_store.register_agent_actor(
                "gamma-architect", "agentcomms", "architect", str(repo), []
            )
            fixture_store.register_agent_actor(
                "gamma-codex-worker",
                "agentcomms",
                "worker",
                str(repo),
                [],
                owner="gamma-architect",
            )

            integration = root / "integration"
            integration.mkdir()
            subprocess.run(["git", "init"], cwd=integration, check=True, stdout=subprocess.PIPE)

            # Mirrors test_review_tool.py isolation: review records stay in the temp tree.
            with (
                mock.patch.object(store, "REVIEW_ROOT", root / "reviews"),
                mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(integration)}),
                mock.patch.object(
                    review.runtime_paths, "db_path", return_value=ledger_path
                ) as db_path,
            ):
                review.command_open(
                    argparse.Namespace(
                        dispatch_id="D-revalidate",
                        brief=str(brief_path),
                        dod=str(dod_path),
                        dod_section=None,
                        repo=str(repo),
                        max_respawns=3,
                        expected_producer="gamma-architect",
                        expected_recipient="gamma-codex-worker",
                    )
                )
                record = review.read_record("D-revalidate")
                self.assertEqual(record["state"], "drafted_brief")
                self.assertIsNone(record["brief_sha256"])

                brief_check_args = argparse.Namespace(
                    dispatch_id="D-revalidate",
                    by="codex",
                    clean=True,
                    finding=None,
                    surface_verdict="complete",
                    surface_reason="fixture declaration is complete",
                )
                review.command_brief_check(brief_check_args)
                record = review.read_record("D-revalidate")
                self.assertEqual(record["state"], "brief_reviewed")
                self.assertIsNotNone(record["brief_sha256"])

                text = brief_path.read_text(encoding="utf-8")
                brief_path.write_text(text.split("\n## Process\n", 1)[0], encoding="utf-8")

                with self.assertRaises(ReviewError) as cm:
                    review.command_brief_check(brief_check_args)
                self.assertIn("Process", str(cm.exception))

                record = review.read_record("D-revalidate")
                self.assertEqual(record["state"], "brief_revised")
                self.assertGreaterEqual(db_path.call_count, 1)
                self.assertEqual(db_path.return_value, ledger_path)


class DodDriftGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dod_path = self.root / "dod.json"
        self.write_dod(1)

    def write_dod(self, count: int) -> None:
        self.dod_path.write_text(
            json.dumps(
                {
                    "criteria": [
                        {"id": f"d{index}", "claim": "claim", "check_id": "green"}
                        for index in range(count)
                    ]
                }
            ),
            encoding="utf-8",
        )

    def binding(self) -> dict:
        return {
            "dod": review.load_dod(self.dod_path, None),
            "dod_path": str(self.dod_path),
            "dod_sha256": review.file_sha256(self.dod_path, "test DoD"),
            "history": [],
        }

    def run_update(self, command, record: dict, args: argparse.Namespace) -> None:
        # The extracted commands look up ``locked_update`` in their own owner
        # module (checks/approval/rebind), so patch the module that defines the
        # command under test rather than the facade re-export.
        owner = sys.modules[command.__module__]
        with mock.patch.object(
            owner, "locked_update", side_effect=lambda _dispatch, fn: fn(record)
        ):
            command(args)

    def test_open_binds_dod_path_and_digest(self) -> None:
        brief = self.root / "brief.md"
        brief.write_text(
            "# B\n## Surface\nx\n## Anti-claims\nx\n## Definition of Done\nx\n"
            "## Process\nx\n## Production surface\n- touches: none; reason: test\n",
            encoding="utf-8",
        )
        repo = self.root / "repo"
        repo.mkdir()
        captured = {}
        args = argparse.Namespace(
            dispatch_id="d",
            brief=str(brief),
            dod=str(self.dod_path),
            dod_section=None,
            repo=str(repo),
            max_respawns=3,
            expected_producer="a",
            expected_recipient="w",
        )
        fake_paths = argparse.Namespace(
            lock=self.root / "record.lock", json=self.root / "absent.json"
        )
        with (
            mock.patch.object(execution, "_resolved_binding_path", return_value=repo),
            mock.patch.object(
                execution,
                "integration_checkout",
                return_value=self.root / "integration",
            ),
            mock.patch.object(execution, "run_git", return_value="true"),
            mock.patch.object(execution, "git_branch", return_value="worker/test"),
            mock.patch.object(execution, "git_head", return_value="a" * 40),
            mock.patch.object(execution, "require_review_repo_worker_root"),
            mock.patch.object(execution, "review_paths", return_value=fake_paths),
            mock.patch.object(
                execution,
                "persist",
                side_effect=lambda _paths, record: captured.update(record),
            ),
        ):
            review.command_open(args)
        self.assertEqual(captured["dod_path"], str(self.dod_path.resolve()))
        self.assertEqual(
            captured["dod_sha256"], review.file_sha256(self.dod_path, "test")
        )

    def test_gates_refuses_on_dod_drift(self) -> None:
        record = self.binding() | {"state": "executed"}
        self.dod_path.write_text("changed", encoding="utf-8")
        args = argparse.Namespace(
            dispatch_id="d",
            check=["green"],
            skip=None,
            reason=None,
            risk=None,
            actor="a",
            timeout=None,
        )
        with self.assertRaises(ReviewError) as raised:
            self.run_update(review.command_gates, record, args)
        message = str(raised.exception)
        self.assertRegex(
            message,
            f"{re.escape(str(self.dod_path))}.*restore the file.*open a new governed review",
        )
        self.assertNotIn("use rebind-dod", message)

    def test_gates_drift_refusal_leaves_record_unchanged(self) -> None:
        record = self.binding() | {"state": "executed", "gate_runs": [], "skips": []}
        before = {
            name: list(record[name]) if isinstance(record[name], list) else record[name]
            for name in ("gate_runs", "skips", "state", "history")
        }
        self.dod_path.write_text("changed", encoding="utf-8")
        args = argparse.Namespace(
            dispatch_id="d",
            check=["green"],
            skip=None,
            reason=None,
            risk=None,
            actor="a",
            timeout=None,
        )
        with self.assertRaises(ReviewError):
            self.run_update(review.command_gates, record, args)
        self.assertEqual({name: record[name] for name in before}, before)

    def test_gates_refuses_when_dod_file_unreadable(self) -> None:
        record = self.binding() | {"state": "executed"}
        self.dod_path.unlink()
        drift = review.dod_drift(record)
        self.assertEqual(drift["reason"], "unreadable")
        self.assertIsNone(drift["actual_sha256"])
        args = argparse.Namespace(
            dispatch_id="d",
            check=["green"],
            skip=None,
            reason=None,
            risk=None,
            actor="a",
            timeout=None,
        )
        with self.assertRaisesRegex(ReviewError, "reason=unreadable"):
            self.run_update(review.command_gates, record, args)

    def test_approve_refuses_on_dod_drift(self) -> None:
        # Contract 18: command_approve reads reviewed_head (always present in a
        # real review_clean record) before the DoD-drift remeasure, which still
        # refuses before any TTY prompt or destination measurement.
        record = self.binding() | {"state": "review_clean", "reviewed_head": "b" * 40}
        self.dod_path.write_text("changed", encoding="utf-8")
        args = argparse.Namespace(dispatch_id="d", approver="a", key=None)
        with (
            mock.patch.object(approval, "read_tty_confirmation") as prompt,
            self.assertRaises(ReviewError) as raised,
        ):
            self.run_update(review.command_approve, record, args)
        prompt.assert_not_called()
        message = str(raised.exception)
        self.assertRegex(message, "restore the file.*open a new governed review")
        self.assertNotIn("use rebind-dod", message)

    def test_rebind_dod_updates_binding_and_history(self) -> None:
        record = self.binding() | {"state": "brief_reviewed"}
        old_digest = record["dod_sha256"]
        self.write_dod(2)
        args = argparse.Namespace(
            dispatch_id="d", dod=str(self.dod_path), reason="harden criteria"
        )
        self.run_update(review.command_rebind_dod, record, args)
        event = record["history"][-1]
        self.assertEqual(
            (event["old_sha256"], event["new_sha256"]),
            (old_digest, record["dod_sha256"]),
        )
        self.assertEqual(
            (event["old_criterion_count"], event["new_criterion_count"]), (1, 2)
        )

    def test_rebind_dod_refuses_after_dispatch(self) -> None:
        record = self.binding() | {"state": "dispatched"}
        args = argparse.Namespace(
            dispatch_id="d", dod=str(self.dod_path), reason="reason"
        )
        with self.assertRaises(ReviewError):
            self.run_update(review.command_rebind_dod, record, args)

    def test_rebind_dod_refuses_after_execution(self) -> None:
        record = self.binding() | {"state": "executed"}
        args = argparse.Namespace(
            dispatch_id="d", dod=str(self.dod_path), reason="reason"
        )
        with self.assertRaises(ReviewError):
            self.run_update(review.command_rebind_dod, record, args)

    def test_gates_allows_legacy_record_without_dod_binding(self) -> None:
        record = {
            "dod": [],
            "state": "executed",
            "gate_runs": [],
            "skips": [],
            "history": [],
            "gate_epoch": 0,
        }
        args = argparse.Namespace(
            dispatch_id="d",
            check=["green"],
            skip=None,
            reason=None,
            risk=None,
            actor="a",
            timeout=None,
        )
        with (
            mock.patch.object(checks, "registry_command"),
            mock.patch.object(
                checks,
                "run_check",
                return_value={"check_id": "green", "verdict": "pass"},
            ),
        ):
            self.run_update(review.command_gates, record, args)
        self.assertEqual(record["state"], "execution_reviewed")

    def test_gates_allows_dod_section_record(self) -> None:
        record = {
            "dod": review.load_dod(None, "criterion"),
            "dod_path": None,
            "dod_sha256": None,
            "state": "executed",
            "gate_runs": [],
            "skips": [],
            "history": [],
            "gate_epoch": 0,
        }
        args = argparse.Namespace(
            dispatch_id="d",
            check=["green"],
            skip=None,
            reason=None,
            risk=None,
            actor="a",
            timeout=None,
        )
        with (
            mock.patch.object(checks, "registry_command"),
            mock.patch.object(
                checks,
                "run_check",
                return_value={"check_id": "green", "verdict": "pass"},
            ),
        ):
            self.run_update(review.command_gates, record, args)
        self.assertEqual(record["state"], "execution_reviewed")


if __name__ == "__main__":
    unittest.main()
