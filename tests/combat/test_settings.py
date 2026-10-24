"""选项合并优先级与载体节点读取测试。"""

import unittest

from ._loader import submodule

settings = submodule("settings")


class MergeTests(unittest.TestCase):
    def test_defaults(self):
        options, warnings = settings.merge_options([])
        self.assertEqual(options, settings.CombatOptions())
        self.assertEqual(options.scheme, "team_rotation")
        self.assertEqual(warnings, [])

    def test_later_layers_win_and_values_are_coerced(self):
        options, warnings = settings.merge_options(
            [
                {"scheme": "main_dps", "max_duration": "120"},
                {"scheme": " Custom ", "custom_scheme": " mine.json "},
                {"wait_timeout": 0, "max_duration": 90},
            ]
        )
        self.assertEqual(
            (options.scheme, options.custom_scheme, options.max_duration, options.wait_timeout),
            ("custom", "mine.json", 90.0, 0.0),
        )
        self.assertEqual(warnings, [])

    def test_empty_values_do_not_override(self):
        options, _ = settings.merge_options([{"custom_scheme": "a.json", "max_duration": 60}, {"custom_scheme": "", "max_duration": None}])
        self.assertEqual((options.custom_scheme, options.max_duration), ("a.json", 60.0))

    def test_invalid_values_keep_previous_and_warn(self):
        options, warnings = settings.merge_options(
            [
                {"max_duration": 120},
                {
                    "max_duration": "abc",
                    "scheme": "nope",
                    "clear_confirm": 0.1,
                    "wait_timeout": True,
                    "hud_lost_timeout": float("nan"),
                    "speed": 3,
                },
            ]
        )
        self.assertEqual(options.max_duration, 120.0)
        self.assertEqual(options.scheme, "team_rotation")
        self.assertEqual(options.clear_confirm, 3.0)
        self.assertEqual(len(warnings), 6)

    def test_collect_layers_order_and_failures(self):
        data = {
            "CombatSetting_Scheme": {"attach": {"scheme": "main_dps"}},
            "CombatSetting_CustomScheme": {"attach": {"custom_scheme": "x.json"}},
            "Caller_Option": {"attach": {"max_duration": 42}},
        }

        def get_node_data(name):
            if name == "Broken":
                raise RuntimeError("boom")
            return data.get(name)

        layers = settings.collect_layers(
            get_node_data, {"scheme": "solo_skill", "option_nodes": ["x"]}, ["Caller_Option", "Broken", "Missing"]
        )
        count = len(settings.GLOBAL_SETTING_NODES)
        self.assertEqual(layers[:2], [{"scheme": "main_dps"}, {"custom_scheme": "x.json"}])
        self.assertEqual(layers[2:count], [{}] * (count - 2))  # 读不到的全局载体视为未设置
        self.assertEqual(layers[count:], [{"scheme": "solo_skill"}, {"max_duration": 42}, {}, {}])
        options, warnings = settings.merge_options(layers)
        self.assertEqual((options.scheme, options.max_duration), ("solo_skill", 42.0))
        self.assertEqual(warnings, [])


class StrategyOptionTests(unittest.TestCase):
    def test_bool_options_accept_common_spellings(self):
        for raw, expected in [(True, True), (False, False), ("true", True), (" No ", False), ("off", False), ("1", True), (0, False), (1, True)]:
            options, warnings = settings.merge_options([{"lock_target": raw}])
            self.assertEqual(options.lock_target, expected, raw)
            self.assertEqual(warnings, [], raw)

    def test_team_size_coercion(self):
        for raw in (2, "2", 2.0, " 2 "):
            self.assertEqual(settings.merge_options([{"team_size": raw}])[0].team_size, 2, raw)

    def test_invalid_bool_and_team_size_keep_previous(self):
        options, warnings = settings.merge_options(
            [
                {"team_size": 3, "use_skill": False},
                {"team_size": "five", "use_skill": "maybe", "search_turn": 2},
                {"team_size": 0},
                {"team_size": 2.5},
                {"team_size": float("inf")},
            ]
        )
        self.assertEqual((options.team_size, options.use_skill, options.search_turn), (3, False, False))
        self.assertEqual(len(warnings), 6)

    def test_disabled_actions_follow_toggle_order(self):
        options, _ = settings.merge_options([{"use_switch": False, "use_ultimate": "false", "use_jump": True}])
        self.assertEqual(options.disabled_actions, ("ultimate", "switch"))
        self.assertEqual(settings.CombatOptions().disabled_actions, ())

    def test_every_toggle_has_an_option_and_a_carrier(self):
        for action in submodule("model").TOGGLE_ACTIONS:
            self.assertIn("use_" + action, settings.OPTION_KEYS)
            self.assertIn("CombatSetting_Disable%s" % action.capitalize(), settings.GLOBAL_SETTING_NODES)


if __name__ == "__main__":
    unittest.main()
