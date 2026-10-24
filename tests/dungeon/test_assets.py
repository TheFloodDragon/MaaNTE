"""资源一致性：Pipeline 引用、custom 注册、选项载体、任务注册、5 语言文案与占位符。"""

import json
import re
import unittest

from ._loader import ACTION_DIR, RESOURCE_BASE, ROOT, submodule

flow = submodule("flow")
budget = submodule("budget")

ASSETS = ROOT / "assets"
PIPELINE_DIR = RESOURCE_BASE / "pipeline"
LANGS = ("zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr")
PLACEHOLDER = re.compile(r"%(?!%)[-#0 +]*\d*(?:\.\d+)?[sdif]")
TASK_FILE = ASSETS / "resource" / "tasks" / "DungeonFarm.json"


def strip_comments(text):
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))  # 允许尾逗号（与 MaaFramework 一致）


def load_json(path):
    return json.loads(strip_comments(path.read_text(encoding="utf-8-sig")))


def all_pipeline():
    nodes = {}
    for path in PIPELINE_DIR.rglob("*.json"):
        data = load_json(path)
        if isinstance(data, dict):
            nodes.update(data)
    return nodes


ALL = all_pipeline()
OWN = {}
for _name in ("DungeonFarm.json", "DungeonStatus.json"):
    OWN.update(load_json(PIPELINE_DIR / "Dungeon" / _name))
TASK = load_json(TASK_FILE)
INTERFACE = load_json(ASSETS / "interface.json")


def locale(kind, lang):
    return load_json(ASSETS / "resource" / "locales" / kind / ("%s.json" % lang))


def strip_prefix(name):
    return re.sub(r"^(\[[A-Za-z]+\])+", "", name)


def recognition(node):
    reco = node.get("recognition")
    return reco if isinstance(reco, dict) else {"type": reco or "DirectHit", "param": node}


def custom_param(node):
    action = node.get("action")
    if isinstance(action, dict) and action.get("type") == "Custom":
        return action["param"].get("custom_action"), action["param"].get("custom_action_param") or {}
    return None, {}


class PipelineTests(unittest.TestCase):
    def test_references_exist_and_no_private_scene_nodes(self):
        for name, node in OWN.items():
            refs = [strip_prefix(n) for n in node.get("next", []) + node.get("on_error", [])]
            reco = recognition(node)
            refs += [r for r in reco.get("param", {}).get("all_of", []) + reco.get("param", {}).get("any_of", [])
                     if isinstance(r, str)]
            _, param = custom_param(node)
            for key in ("continue", "finish"):
                refs += param.get(key, [])
            for key in ("until", "balance_node"):
                if param.get(key):
                    refs.append(param[key])
            for ref in refs:
                self.assertIn(ref, ALL, "%s -> %s" % (name, ref))
                self.assertFalse(ref.startswith("__ScenePrivate"), "%s -> %s" % (name, ref))

    def test_custom_actions_registered(self):
        source = (ACTION_DIR / "Dungeon" / "actions.py").read_text(encoding="utf-8")
        registered = set(re.findall(r'custom_action\("([^"]+)"\)', source))
        used = {custom_param(node)[0] for node in OWN.values()} - {None}
        self.assertTrue(used)
        self.assertLessEqual(used - {"map_teleport_to_point"}, registered)
        registry = (ACTION_DIR / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("from .Dungeon.actions import *", registry)
        for cls in re.findall(r"class (\w+)\(CustomAction\)", source):
            self.assertIn('"%s"' % cls, registry)

    def test_teleport_point_exists(self):
        points = load_json(RESOURCE_BASE / "map_teleport" / "teleport_points.json")
        ids = {p["id"] for p in points["teleport_points"]}
        self.assertIn(OWN["DungeonFarmTeleport"]["action"]["param"]["custom_action_param"]["teleport_point_id"], ids)

    def test_explicit_zero_delays(self):
        for name, node in OWN.items():
            if node.get("enabled") is False:
                continue
            for key in ("rate_limit", "pre_delay", "post_delay"):
                self.assertEqual(node.get(key), 0, "%s.%s" % (name, key))
            self.assertNotIn("timeout", node, name)  # 不用节点超时推进流程

    def test_option_carriers_disabled_and_match_flow(self):
        for name in (flow.RUNS_NODE, flow.STAGE_NODE):
            self.assertIs(OWN[name]["enabled"], False)
        self.assertEqual(budget.parse_runs(OWN[flow.RUNS_NODE]["attach"]["runs"]), 0)

    def test_gate_routes_only_to_claim_or_close(self):
        _, gate = custom_param(OWN["DungeonFarmClaimGate"])
        self.assertEqual(gate["continue"], ["DungeonFarmClaim"])
        self.assertEqual(OWN["DungeonFarmClaimGate"]["next"], [])  # next 只由闸门改写
        _, confirm = custom_param(OWN["DungeonFarmSettlement"])
        self.assertEqual(confirm["continue"], ["DungeonFarmAgain"])
        self.assertEqual(OWN["DungeonFarmSettlement"]["next"], [])
        self.assertNotIn("DungeonFarmAgain", confirm["finish"])  # 最后一轮不点再次挑战

    def test_claim_roi_excludes_double_claim(self):
        claim = OWN["DungeonClaimButton"]["recognition"]["param"]
        popup = OWN["DungeonRewardPopup"]["recognition"]["param"]["roi"]
        x, _, w, _ = claim["roi"]
        self.assertLessEqual(x + w, popup[0] + popup[2] // 2 + 4)  # 只覆盖弹窗左半
        self.assertEqual(claim["expected"], ["领取"])
        self.assertTrue(all("双倍" not in t for t in claim["expected"]))

    def test_claim_only_reachable_via_gate(self):
        sources = [n for n, node in OWN.items()
                   if "DungeonFarmClaim" in [strip_prefix(x) for x in node.get("next", [])]
                   or "DungeonFarmClaim" in custom_param(node)[1].get("continue", [])]
        self.assertEqual(sources, ["DungeonFarmClaimGate"])


class TaskTests(unittest.TestCase):
    def test_registered_in_interface(self):
        self.assertIn("resource/tasks/DungeonFarm.json", INTERFACE["import"])
        task = TASK["task"][0]
        self.assertIn(task["entry"], OWN)
        self.assertEqual(task["controller"], ["Win32-Front"])
        self.assertIn(task["group"][0], {g["name"] for g in INTERFACE["group"]})

    def test_each_option_overrides_its_own_carrier(self):
        targets = []
        for option in TASK["option"].values():
            overrides = [option.get("pipeline_override", {})] + [c.get("pipeline_override", {}) for c in option.get("cases", [])]
            names = {n for o in overrides for n in o}
            self.assertEqual(len(names), 1, names)
            targets.append(names.pop())
        self.assertEqual(sorted(targets), sorted([flow.RUNS_NODE, flow.STAGE_NODE]))
        for name in targets:
            self.assertIs(OWN[name]["enabled"], False)

    def test_stage_cases_valid(self):
        stage = TASK["option"]["DungeonFarmStage"]
        self.assertIn(stage["default_case"], [c["name"] for c in stage["cases"]])
        for case in stage["cases"]:
            attach = case["pipeline_override"][flow.STAGE_NODE]["attach"]
            self.assertTrue(re.fullmatch(r"[a-z_]+", attach["name"]))
            self.assertGreater(attach["cost"], 0)

    def test_runs_verify_matches_parser(self):
        item = TASK["option"]["DungeonFarmRuns"]["inputs"][0]
        pattern = re.compile(item["verify"])
        for text in ("0", "1", "42", "999", "", "00", "01", "1000", "-1", "a", " 1"):
            self.assertEqual(bool(pattern.fullmatch(text)), budget.parse_runs(text) is not None and text.strip() == text
                             and (text == "0" or not text.startswith("0")), text)
        self.assertEqual(item["default"], "0")


class LocaleTests(unittest.TestCase):
    def test_interface_labels_in_all_languages(self):
        keys = set(re.findall(r'"\$([A-Za-z0-9_.]+)"', TASK_FILE.read_text(encoding="utf-8")))
        self.assertGreater(len(keys), 5)
        for lang in LANGS:
            missing = keys - set(locale("interface", lang))
            self.assertFalse(missing, "%s missing %s" % (lang, sorted(missing)))

    def test_agent_keys_identical_and_format(self):
        reference = locale("agent", "zh_cn")
        keys = {k for k in reference if k.startswith("dungeon.")}
        source = (ACTION_DIR / "Dungeon" / "flow.py").read_text(encoding="utf-8")
        used = set(re.findall(r'"(dungeon\.[a-z_.]+)"', source)) - {"dungeon.stage.", "dungeon.finished."}
        used |= {"dungeon.finished.%s" % r for r in budget.FINISHED}
        stages = {c["pipeline_override"][flow.STAGE_NODE]["attach"]["name"]
                  for c in TASK["option"]["DungeonFarmStage"]["cases"]}
        used |= {"dungeon.stage.%s" % s for s in stages}
        self.assertLessEqual(used, keys)
        for lang in LANGS:
            data = locale("agent", lang)
            self.assertEqual({k for k in data if k.startswith("dungeon.")}, keys, lang)
            for key in keys:
                self.assertEqual(len(PLACEHOLDER.findall(data[key])), len(PLACEHOLDER.findall(reference[key])),
                                 (lang, key))


if __name__ == "__main__":
    unittest.main()
