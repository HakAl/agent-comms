"""The Codex launch settings that keep operator shell startup files out of a
worker's command shells, and the stored options that could undo them."""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import tomllib
import unittest

from agent_comms import codex_binding, codex_startup


class ConfigOverridesTest(unittest.TestCase):
    def test_pins_each_setting_as_a_config_pair(self) -> None:
        argv = codex_startup.config_overrides()
        self.assertEqual(argv[0::2], ["-c"] * (len(argv) // 2))
        merged: dict = {}
        for value in argv[1::2]:
            key, _, raw = value.partition("=")
            parsed = tomllib.loads(f"{key} = {raw}")
            for top, inner in parsed.items():
                if isinstance(inner, dict):
                    node = merged.setdefault(top, {})
                    for name, leaf in inner.items():
                        if isinstance(leaf, dict):
                            node.setdefault(name, {}).update(leaf)
                        else:
                            node[name] = leaf
                else:
                    merged[top] = inner
        self.assertEqual(
            merged,
            {
                "allow_login_shell": False,
                "features": {"shell_snapshot": False, "shell_snapshot_v2": False},
                "shell_environment_policy": {"set": {"BASH_ENV": "", "ENV": ""}},
            },
        )

    def test_every_pin_is_itself_a_startup_override(self) -> None:
        for value in codex_startup.config_overrides()[1::2]:
            with self.subTest(value):
                self.assertTrue(codex_startup.touches_startup(value))


class TouchesStartupTest(unittest.TestCase):
    def test_overrides_that_reach_a_pinned_setting(self) -> None:
        for value in (
            "allow_login_shell=true",
            " allow_login_shell = true",
            '"allow_login_shell"=true',
            "features.shell_snapshot=true",
            'features."shell_snapshot"=true',
            '"features".shell_snapshot_v2=true',
            "features={shell_snapshot=true}",
            "features={}",
            "shell_environment_policy.set.BASH_ENV='/x'",
            'shell_environment_policy.set={BASH_ENV="/x"}',
            'shell_environment_policy={inherit="all"}',
            "shell_environment_policy.inherit='all'",
            # A key that does not parse is read as ours when it names one.
            "features.shell_snapshot.=true",
            "allow_login_shell..x=1",
        ):
            with self.subTest(value):
                self.assertTrue(codex_startup.touches_startup(value))

    def test_overrides_that_do_not(self) -> None:
        for value in (
            'model="o3"',
            "features.web_search=true",
            'mcp_servers.other.command="/y"',
            'notes="allow_login_shell"',
            "sandbox_workspace_write.exclude_slash_tmp=true",
        ):
            with self.subTest(value):
                self.assertFalse(codex_startup.touches_startup(value))


class FeatureToggleTest(unittest.TestCase):
    def test_launch_options_keep_feature_toggles_in_every_spelling(self) -> None:
        options = codex_binding.launch_options(
            ["--enable", "shell_snapshot", "--disable=web_search", "--enable=shell_snapshot_v2", "-c", "x=1"]
        )
        self.assertEqual(
            options.feature_toggles,
            [("--enable", "shell_snapshot"), ("--disable", "web_search"), ("--enable", "shell_snapshot_v2")],
        )
        self.assertEqual(options.overrides, ["x=1"])

    def test_startup_toggles_are_the_pinned_features_only(self) -> None:
        toggles = [("--enable", "shell_snapshot"), ("--disable", "web_search"), ("--disable", "shell_snapshot_v2")]
        self.assertEqual(
            codex_startup.startup_toggles(toggles),
            ["--enable shell_snapshot", "--disable shell_snapshot_v2"],
        )


class SpawnEnvTest(unittest.TestCase):
    def test_startup_variables_are_named(self) -> None:
        self.assertEqual(codex_startup.STARTUP_ENV_NAMES, frozenset({"BASH_ENV", "ENV"}))


if __name__ == "__main__":
    unittest.main()
