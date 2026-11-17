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
                "shell_environment_policy": {"set": {"BASH_ENV": "", "ENV": "", "SHLVL": "1"}},
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


def effective(**policy) -> dict:
    """What ``config/read`` reports when only the launch pins apply, with
    ``policy`` laid over the shell environment policy."""
    base = {"set": dict(codex_startup.PINNED_ENV), "include_only": None, "exclude": None, "inherit": None,
            "ignore_default_excludes": None, "experimental_use_profile": None, "filters": None}
    return {
        "allow_login_shell": False,
        "features": {"shell_snapshot": False, "web_search": True},
        "shell_environment_policy": {**base, **policy},
    }


class EffectiveProblemsTest(unittest.TestCase):
    """Lookout LINUX-PINS-003 R1: a config layer (worker home, profile,
    project) merges into the policy the pins only partly set, so the check
    reads the policy Codex resolved."""

    def test_the_pinned_launch_has_none(self) -> None:
        self.assertEqual(codex_startup.effective_problems(effective()), [])

    def test_restrictions_that_keep_the_protected_variables_have_none(self) -> None:
        for policy in (
            {"include_only": ["HOME", "PATH", "SHLVL", "ZDOTDIR", "AGENT_COMMS_*"]},
            {"exclude": ["AWS_*", "*TOKEN*", "FOO?"], "ignore_default_excludes": True},
            # Lookout LINUX-PINS-003 C1: Codex's matcher knows only * and ?;
            # brackets are literal, so these cannot match ZDOTDIR.
            {"exclude": ["ZDOTDI[R]", "[Z]DOTDIR", "ZDOTDIR[!x]"]},
            {"inherit": "all", "include_only": []},
            {"set": {**codex_startup.PINNED_ENV, "EDITOR": "vi"}},
        ):
            with self.subTest(policy):
                self.assertEqual(codex_startup.effective_problems(effective(**policy)), [])

    def test_each_route_that_drops_or_replaces_a_protected_variable(self) -> None:
        pinned = codex_startup.PINNED_ENV
        for policy, expected in (
            # Codex applies include_only after set, so it removes the pins.
            ({"include_only": ["HOME", "PATH", "ZDOTDIR", "LANG"]}, "include_only .* omits SHLVL"),
            ({"include_only": ["HOME", "PATH", "SHLVL"]}, "include_only .* omits ZDOTDIR"),
            ({"include_only": ["SH*", "ZDOT*"]}, "include_only .* omits SHLVL, ZDOTDIR"),
            ({"set": {**pinned, "ZDOTDIR": "/operator-home"}}, "set.ZDOTDIR replaces"),
            ({"set": {**pinned, "SHLVL": "0"}}, "set.SHLVL is '0'"),
            ({"set": {**pinned, "BASH_ENV": "/operator-home/.bashrc"}}, "set.BASH_ENV is '/operator-home/.bashrc'"),
            ({"set": {"BASH_ENV": "", "SHLVL": "1"}}, "set.ENV is None"),
            ({"exclude": ["ZDOTDIR"]}, "exclude 'ZDOTDIR' removes ZDOTDIR"),
            ({"exclude": ["AWS_*", "zdot*"]}, "exclude 'zdot\\*' removes ZDOTDIR"),
            ({"exclude": ["*"]}, "removes ZDOTDIR"),
            ({"exclude": ["ZDOT???"]}, "removes ZDOTDIR"),
            ({"exclude": ["z*d?r"]}, "removes ZDOTDIR"),
            ({"inherit": "core"}, "inherit is 'core'"),
            ({"inherit": "none"}, "inherit is 'none'"),
            ({"experimental_use_profile": True}, "experimental_use_profile"),
            ({"filters": ["x"]}, "filters is not a setting this check knows"),
        ):
            with self.subTest(policy):
                problems = codex_startup.effective_problems(effective(**policy))
                self.assertEqual(len(problems), 1, problems)
                self.assertRegex(problems[0], expected)

    def test_settings_outside_the_policy(self) -> None:
        for change, expected in (
            ({"allow_login_shell": True}, "allow_login_shell is True"),
            ({"allow_login_shell": None}, "allow_login_shell is None"),
            ({"features": {"shell_snapshot": True}}, "features.shell_snapshot is True"),
            ({"features": {}}, "features.shell_snapshot is None"),
            ({"features": {"shell_snapshot": False, "shell_snapshot_v2": True}}, "features.shell_snapshot_v2 is True"),
            ({"shell_environment_policy": None}, "set.BASH_ENV is None"),
            ({"shell_environment_policy": {"set": None}}, "set.SHLVL is None"),
        ):
            with self.subTest(change):
                problems = codex_startup.effective_problems({**effective(), **change})
                self.assertTrue(any(expected in problem for problem in problems), problems)

    def test_read_config_reports_why_it_has_no_answer(self) -> None:
        def no_answer(codex, method, params, **kwargs):
            return None, "codex app-server exited before answering config/read"

        def an_error(codex, method, params, **kwargs):
            return {"error": {"code": -1}}, None

        def no_config(codex, method, params, **kwargs):
            return {"result": {"layers": []}}, None

        def answered(codex, method, params, **kwargs):
            self.assertEqual((method, params), ("config/read", {"cwd": "/w", "includeLayers": False}))
            self.assertEqual(kwargs["overrides"], ["-c", "x=1"])
            return {"result": {"config": {"allow_login_shell": False}}}, None

        read = codex_startup.read_config
        self.assertEqual(read("codex", cwd="/w", env={}, overrides=[], request=no_answer),
                         (None, "codex app-server exited before answering config/read"))
        self.assertIn("config/read failed", read("codex", cwd="/w", env={}, overrides=[], request=an_error)[1])
        self.assertIn("without a config", read("codex", cwd="/w", env={}, overrides=[], request=no_config)[1])
        self.assertEqual(read("codex", cwd="/w", env={}, overrides=["-c", "x=1"], request=answered),
                         ({"allow_login_shell": False}, None))


class SpawnEnvTest(unittest.TestCase):
    def test_startup_variables_are_named(self) -> None:
        self.assertEqual(codex_startup.STARTUP_ENV_NAMES, frozenset({"BASH_ENV", "ENV"}))


if __name__ == "__main__":
    unittest.main()
