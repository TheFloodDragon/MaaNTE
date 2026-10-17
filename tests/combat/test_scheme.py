"""方案解析、内置方案与用户方案查找测试。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ._loader import RESOURCE_BASE, submodule

scheme = submodule("scheme")
locate = submodule("locate")

COMBOS = {"default": "skill"}


def base(**extra):
    data = {"version": 1, "combos": dict(COMBOS)}
    data.update(extra)
    return data


class ParseTests(unittest.TestCase):
    def assertCode(self, data, code):
        with self.assertRaises(scheme.SchemeError) as ctx:
            scheme.parse_scheme(data, "test.json")
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        return ctx.exception

    def test_minimal_defaults(self):
        s = scheme.parse_scheme(base(), "dir/my_scheme.json")
        self.assertEqual(s.name, "my_scheme")
        self.assertEqual((s.mode, s.order, s.min_field_time), ("cycle", [1, 2, 3, 4], 1.0))
        self.assertEqual(s.combo_for(3)[0].name, "skill")
        self.assertEqual(s.warnings, [])

    def test_slot_combo_overrides_default(self):
        s = scheme.parse_scheme(base(combos={"1": "jump", "default": "skill"}))
        self.assertEqual(s.combo_for(1)[0].name, "jump")
        self.assertEqual(s.combo_for(2)[0].name, "skill")

    def test_structure_errors(self):
        self.assertCode([], "not_object")
        self.assertCode({"combos": COMBOS}, "bad_version")
        self.assertCode(base(version=True), "bad_version")
        self.assertCode(base(version=2), "bad_version")
        self.assertCode({"version": 1}, "no_combos")
        self.assertCode(base(combos={"5": "skill"}), "bad_combo_key")
        self.assertCode(base(rotation={"mode": "random"}), "bad_mode")
        self.assertCode(base(rotation={"order": []}), "bad_order")
        self.assertCode(base(rotation={"order": [1, 5]}), "bad_order")
        self.assertCode(base(rotation={"order": [True]}), "bad_order")
        self.assertCode(base(rotation={"min_field_time": 11}), "bad_field_time")
        self.assertCode(base(rotation=[]), "bad_rotation")
        self.assertCode(base(team={"slot": 1}), "bad_team")
        self.assertCode(base(team=5), "bad_team")
        self.assertCode(base(team=[{"slot": 9}]), "bad_team")
        self.assertCode(base(team=[{"slot": 1}, {"slot": 1}]), "duplicate_team_slot")

    def test_combo_error_is_wrapped_with_slot(self):
        err = self.assertCode(base(combos={"2": "skill\nif skill: jump"}), "combo")
        self.assertEqual(err.params, ("2",))
        self.assertEqual(err.combo_error.code, "unsupported_statement")
        self.assertEqual(err.combo_error.line, 2)

    def test_missing_combo_coverage(self):
        err = self.assertCode({"version": 1, "combos": {"1": "skill"}, "rotation": {"order": [1, 3]}}, "missing_combo")
        self.assertEqual(err.params, ("3",))
        # stay 模式当前角色可能在任意槽位，需要 default 或 1~4 全部
        self.assertCode({"version": 1, "combos": {"1": "skill"}, "rotation": {"mode": "stay"}}, "missing_combo")

    def test_unknown_fields_only_warn(self):
        s = scheme.parse_scheme(base(extra=1, team=[{"slot": 2, "role": "x"}], rotation={"speed": 1}))
        self.assertEqual(len(s.warnings), 3)
        self.assertEqual(s.team[0].slot, 2)

    def test_json_comments_and_escapes(self):
        text = '// head\n{"version": 1, /* block\n */ "name": "a//b \\" /*x*/",\n "combos": {"default": "skill"} // tail\n}'
        s = scheme.parse_scheme_text(text)
        self.assertEqual(s.name, 'a//b " /*x*/')

    def test_bad_json_reports_line(self):
        with self.assertRaises(scheme.SchemeError) as ctx:
            scheme.parse_scheme_text('{\n"version": 1,\n"combos": }')
        self.assertEqual(ctx.exception.code, "bad_json")
        self.assertEqual(ctx.exception.params[0], 3)


class BuiltinTests(unittest.TestCase):
    def test_all_builtins_and_example_load_without_warnings(self):
        for name in scheme.BUILTIN_SCHEMES:
            s = locate.load_builtin(name)
            self.assertTrue(s.name.startswith("$combat.scheme_name."), name)
            self.assertEqual(s.warnings, [], name)
        example = scheme.read_scheme_file(RESOURCE_BASE / "combat" / "examples" / "custom_example.json")
        self.assertEqual(example.warnings, [])
        self.assertEqual(example.order, [1, 2])

    def test_default_scheme_is_builtin(self):
        self.assertIn(scheme.DEFAULT_SCHEME, scheme.BUILTIN_SCHEMES)
        self.assertEqual(locate.load_builtin("main_dps").order, [1, 2, 1, 3, 1, 4])

    def test_unknown_builtin(self):
        with self.assertRaises(scheme.SchemeError) as ctx:
            locate.load_scheme("nope")
        self.assertEqual(ctx.exception.code, "unknown_builtin")


class CustomLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patch = mock.patch.object(locate, "user_dirs", lambda: [self.dir / "config" / "combat"])
        self.patch.start()
        (self.dir / "config" / "combat").mkdir(parents=True)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def write(self, name, data, raw=None):
        path = self.dir / "config" / "combat" / name
        path.write_bytes(raw if raw is not None else json.dumps(data, ensure_ascii=False).encode("utf-8"))
        return path

    def assertCode(self, value, code):
        with self.assertRaises(scheme.SchemeError) as ctx:
            locate.load_scheme("custom", value)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        return ctx.exception

    def test_name_with_or_without_suffix_and_absolute_path(self):
        path = self.write("mine.json", base(name="我的"))
        self.assertEqual(locate.load_scheme("custom", "mine").name, "我的")
        self.assertEqual(locate.load_scheme("custom", " mine.json ").name, "我的")
        self.assertEqual(locate.load_scheme("custom", '"%s"' % path).name, "我的")

    def test_bom_is_accepted(self):
        self.write("bom.json", None, raw=b"\xef\xbb\xbf" + json.dumps(base()).encode())
        self.assertEqual(locate.load_scheme("custom", "bom").name, "bom")

    def test_falls_back_to_example_dir(self):
        self.assertEqual(locate.load_scheme("custom", "custom_example").order, [1, 2])

    def test_errors(self):
        self.assertCode("", "custom_empty")
        err = self.assertCode("missing", "custom_not_found")
        self.assertIn(str(self.dir / "config" / "combat"), err.params[1])
        self.write("mine.txt", base())
        self.assertCode("mine.txt", "custom_not_json")
        self.write("big.json", None, raw=b" " * (scheme.MAX_FILE_BYTES + 1))
        self.assertCode("big", "too_large")
        self.write("gbk.json", None, raw="{\"name\": \"中文\"}".encode("gbk"))
        self.assertCode("gbk", "not_utf8")
        self.write("broken.json", None, raw=b"{")
        self.assertCode("broken", "bad_json")

    def test_user_dirs_cover_install_root(self):
        self.patch.stop()
        try:
            dirs = [str(d) for d in locate.user_dirs()]
            self.assertIn(str(Path.cwd() / "config" / "combat"), dirs)
        finally:
            self.patch.start()


if __name__ == "__main__":
    unittest.main()
