"""出招表 DSL 编译测试。"""

import unittest

from ._loader import submodule

dsl = submodule("dsl")


def names(text):
    return [c.name for c in dsl.compile_combo(text)]


class CompileTests(unittest.TestCase):
    def test_ok_nte_example_compiles(self):
        cmds = dsl.compile_combo("l_click(0.5), jump\nskill(0.5), l_click(2), wait(0.1)\narc, wait(0.2)")
        self.assertEqual(
            [c.name for c in cmds], ["l_click", "jump", "skill", "l_click", "wait", "arc", "wait"]
        )
        self.assertEqual(cmds[0].args, (0.5,))
        self.assertEqual((cmds[2].line, cmds[2].col), (2, 1))

    def test_defaults_aliases_and_keywords(self):
        cmds = dsl.compile_combo("skill, l_hold, r_hold, attack, heavy(1), walk(direction=d, seconds=0.2)")
        self.assertEqual([c.args for c in cmds], [(0.0,), (0.6,), (0.3,), (0.0,), (1.0,), ("d", 0.2)])
        self.assertEqual(cmds[3].name, "l_click")
        self.assertEqual(cmds[4].name, "l_hold")

    def test_bare_identifiers_and_case(self):
        cmds = dsl.compile_combo("walk(W, 0.3), keydown(LShift), keyup('lshift'), click(Middle), keypress(1)")
        self.assertEqual([c.args[0] for c in cmds], ["w", "lshift", "lshift", "middle", "1"])

    def test_switch_slot(self):
        self.assertEqual(dsl.compile_combo("switch(2), switch('4')")[1].args, (4,))

    def test_lines_list_comments_indent_and_fullwidth(self):
        cmds = dsl.compile_combo(["  ultimate  # 先放大招", "", "# 注释行", "    skill（0.2），jump"])
        self.assertEqual([c.name for c in cmds], ["ultimate", "skill", "jump"])
        self.assertEqual(cmds[1].args, (0.2,))

    def test_command_text_kept_for_logs(self):
        self.assertEqual(dsl.compile_combo("walk(w, 0.3)")[0].text, "walk(w, 0.3)")


class ErrorTests(unittest.TestCase):
    def assertCode(self, source, code, line=None):
        with self.assertRaises(dsl.ComboError) as ctx:
            dsl.compile_combo(source)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        if line is not None:
            self.assertEqual(ctx.exception.line, line)
        return ctx.exception

    def test_syntax_error_has_position(self):
        self.assertCode("skill,\nl_click(", "syntax")

    def test_control_flow_not_supported_yet(self):
        self.assertCode("skill\nif skill: l_click(2)", "unsupported_statement", line=2)
        self.assertCode("return", "unsupported_statement")
        self.assertCode("x = 1", "unsupported_statement")
        self.assertCode("for i in range(3): skill", "unsupported_statement")

    def test_unknown_command_has_no_attribute_fallback(self):
        err = self.assertCode("skill, perform", "unknown_command")
        self.assertEqual(err.params, ("perform",))
        self.assertCode("switch_next_char()", "unknown_command")
        self.assertCode("__import__('os')", "unknown_command")

    def test_unsupported_expressions(self):
        self.assertCode("self.click()", "unsupported_syntax")
        self.assertCode("skill + 1", "unsupported_syntax")
        self.assertCode("skill, (jump, dodge)", "unsupported_syntax")  # 嵌套序列
        self.assertEqual(names("(skill, jump)"), ["skill", "jump"])  # 整行加括号等价于逗号序列

    def test_argument_errors(self):
        self.assertCode("jump(1)", "too_many_args")
        self.assertCode("wait", "missing_arg")
        self.assertCode("walk(w)", "missing_arg")
        self.assertCode("skill(speed=1)", "unknown_param")
        self.assertCode("skill(**x)", "unknown_param")
        self.assertCode("skill(0.1, seconds=0.2)", "duplicate_param")
        self.assertCode("wait(0.5*2)", "bad_arg")

    def test_value_errors(self):
        self.assertCode("wait(0)", "out_of_range")
        self.assertCode("wait(11)", "out_of_range")
        self.assertCode("l_click(-1)", "out_of_range")
        self.assertCode("skill(True)", "bad_value")
        self.assertCode("skill(fast)", "bad_value")
        self.assertCode("walk(up, 1)", "bad_value")
        self.assertCode("keydown(esc)", "bad_value")
        self.assertCode("keypress(f1)", "bad_value")
        self.assertCode("click(side)", "bad_value")
        self.assertCode("switch(5)", "bad_value")
        self.assertCode("switch(0)", "bad_value")

    def test_empty_too_long_and_not_text(self):
        self.assertCode("", "empty")
        self.assertCode("# 只有注释", "empty")
        self.assertCode(", ".join(["jump"] * (dsl.MAX_COMMANDS + 1)), "too_many_commands")
        self.assertCode(123, "not_text")
        self.assertCode(["skill", 1], "not_text")


if __name__ == "__main__":
    unittest.main()
