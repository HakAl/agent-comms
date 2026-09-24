import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import os
import tempfile
import unittest
from functools import cache
from pathlib import Path

from agent_comms import actors
from agent_comms.schema import ValidationError, validate_refs
from agent_comms.store import Store


@cache
def resolve_raises_on_symlink_loop() -> bool:
    with tempfile.TemporaryDirectory() as temp_dir:
        loop = Path(temp_dir) / "loop"
        os.symlink(loop, loop)
        try:
            (loop / "x").resolve()
        except (RuntimeError, OSError):
            return True
        return False


class RefValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "project"
        self.root.mkdir()
        self.store = Store(Path(self.temp_dir.name) / "agent-comms.sqlite")
        self.store.register_agent(
            "team-architect", "team", "architect", str(self.root), []
        )
        self.store.register_agent(
            "team-worker", "team", "worker", str(self.root), [], owner="team-architect"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def assert_refused_on_both_doors(self, path: str, *fragments: str) -> None:
        calls = (
            lambda: self.store.send_message(
                "team-architect",
                ["team-worker"],
                "subject",
                "body",
                [{"path": path, "summary": "summary"}],
            ),
            lambda: self.store.post_handoff(
                "team-worker",
                "body",
                [{"path": path, "summary": "summary"}],
                created_by_actor_id="team-architect",
            ),
        )
        messages = []
        for call in calls:
            with self.assertRaises(ValidationError) as raised:
                call()
            messages.append(str(raised.exception))
            for fragment in fragments:
                self.assertIn(fragment, messages[-1])
        self.assertEqual(messages[0], messages[1])

    def test_relative_ref_is_refused_with_remedy_on_both_doors(self) -> None:
        self.assert_refused_on_both_doors(
            "relative/file.txt", "relative/file.txt", "absolute", "inline"
        )

    def test_tilde_ref_is_refused_with_remedy_on_both_doors(self) -> None:
        self.assert_refused_on_both_doors(
            "~/file.txt", "~/file.txt", "absolute", "inline"
        )

    def test_embedded_nul_ref_is_refused_with_remedy_on_both_doors(self) -> None:
        path = str(self.root / "artifact.txt") + "\0suffix"
        self.assert_refused_on_both_doors(
            path, "refs[0].path", path, "absolute", "inline"
        )

    def test_symlink_loop_ref_is_refused_or_validates_by_interpreter(self) -> None:
        loop = self.root / "loop"
        os.symlink(loop, loop)
        path = str(loop / "artifact.txt")
        if resolve_raises_on_symlink_loop():
            self.assert_refused_on_both_doors(
                path, "refs[0].path", "absolute", "inline"
            )
        else:
            resolved = str(Path(path).resolve())
            send = self.store.send_message(
                "team-architect",
                ["team-worker"],
                "subject",
                "body",
                [{"path": path, "summary": "summary"}],
            )
            message = self.store.read_message("team-worker", send["id"])
            handoff = self.store.post_handoff(
                "team-worker",
                "body",
                [{"path": path, "summary": "summary"}],
                created_by_actor_id="team-architect",
            )
            self.assertEqual(message["refs"][0]["path"], resolved)
            self.assertEqual(handoff["refs"][0]["path"], resolved)

    def test_symlink_loop_project_root_is_refused_or_validates_by_interpreter(
        self,
    ) -> None:
        self.root.rmdir()
        os.symlink(self.root, self.root)
        path = str(self.root / "artifact.txt")
        if resolve_raises_on_symlink_loop():
            self.assert_refused_on_both_doors(path, str(self.root), "inline")
        else:
            # Do not drive store doors: agent-comms-semaphore-write-broken-root-cp0.
            expected = [{"path": str(Path(path).resolve()), "summary": "summary"}]
            self.assertEqual(
                validate_refs([{"path": path, "summary": "summary"}], [self.root]),
                expected,
            )

    def test_helper_deduplicates_and_sorts_roots_and_refusal_lists_them_once(
        self,
    ) -> None:
        other = Path(self.temp_dir.name) / "aaa"
        other.mkdir()
        self.store.register_agent(
            "other-architect", "other", "architect", str(other), []
        )
        with self.store.connection() as conn:
            self.assertEqual(
                actors.agent_project_roots(conn), [other.resolve(), self.root.resolve()]
            )
        outside = str(Path(self.temp_dir.name) / "outside.txt")
        with self.assertRaises(ValidationError) as raised:
            self.store.send_message(
                "team-architect",
                ["team-worker"],
                "subject",
                "body",
                [{"path": outside, "summary": ""}],
            )
        message = str(raised.exception)
        self.assertEqual(message.count(str(self.root.resolve())), 1)
        self.assertLess(
            message.index(str(other.resolve())), message.index(str(self.root.resolve()))
        )

    def test_both_doors_use_actors_roots_when_legacy_agents_diverges(self) -> None:
        actors_root = Path(self.temp_dir.name) / "actors-only"
        actors_root.mkdir()
        ref = actors_root / "artifact.txt"
        with self.store.connection() as conn:
            conn.execute(
                "update actors set project_root = ? where id = ?",
                (str(actors_root), "team-worker"),
            )
        send = self.store.send_message(
            "team-architect",
            ["team-worker"],
            "subject",
            "body",
            [{"path": str(ref), "summary": ""}],
        )
        handoff = self.store.post_handoff(
            "team-worker",
            "body",
            [{"path": str(ref), "summary": ""}],
            created_by_actor_id="team-architect",
        )
        sent_message = self.store.read_message("team-worker", send["id"])
        self.assertEqual(sent_message["refs"][0]["path"], str(ref.resolve()))
        self.assertEqual(handoff["refs"][0]["path"], str(ref.resolve()))

    def test_out_of_root_absolute_ref_has_path_remedy_and_roots(self) -> None:
        outside = str(Path(self.temp_dir.name) / "outside.txt")
        self.assert_refused_on_both_doors(
            outside, outside, "inline", str(self.root.resolve())
        )

    def test_traversal_and_symlink_escapes_are_refused(self) -> None:
        traversal = str(self.root / ".." / "outside.txt")
        self.assert_refused_on_both_doors(
            traversal, str(Path(traversal).resolve()), "inline"
        )
        outside = Path(self.temp_dir.name) / "outside"
        outside.mkdir()
        link = self.root / "escape"
        os.symlink(outside, link)
        self.assert_refused_on_both_doors(
            str(link / "file.txt"), str(outside / "file.txt"), "inline"
        )

    def test_valid_absolute_in_root_ref_is_unchanged(self) -> None:
        ref = self.root / "artifact.txt"
        expected = [{"path": str(ref.resolve()), "summary": "summary"}]
        self.assertEqual(
            validate_refs([{"path": str(ref), "summary": " summary "}], [self.root]),
            expected,
        )


if __name__ == "__main__":
    unittest.main()
