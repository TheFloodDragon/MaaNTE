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
        self.assertEqual(
            layers,
            [{"scheme": "main_dps"}, {"custom_scheme": "x.json"}, {"scheme": "solo_skill"}, {"max_duration": 42}, {}, {}],
        )
        options, warnings = settings.merge_options(layers)
        self.assertEqual((options.scheme, options.max_duration), ("solo_skill", 42.0))
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
