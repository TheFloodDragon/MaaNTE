"""独立活动领奖任务的配置与五语契约，不替代实机 Pipeline 验收。"""

import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
JSONC_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|/\*[\s\S]*?\*/|//[^\r\n]*')
LANGUAGES = ("zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr")
STATUS_KEYS = (
    "claim_event_rewards_started",
    "claim_event_rewards_claimed",
    "claim_event_rewards_no_reward",
    "claim_event_rewards_no_event",
    "claim_event_rewards_complete",
    "claim_event_rewards_failed",
)


def unique_mapping(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path):
    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=unique_mapping
    )


def load_jsonc(path):
    text = path.read_text(encoding="utf-8")
    stripped = JSONC_TOKEN.sub(
        lambda match: match[0] if match[0].startswith('"') else "", text
    )
    return json.loads(stripped, object_pairs_hook=unique_mapping)


class ClaimEventRewardsConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_json(ROOT / "assets/resource/tasks/ClaimEventRewards.json")
        cls.locales = {
            language: load_json(
                ROOT / "assets/resource/locales/interface" / (language + ".json")
            )
            for language in LANGUAGES
        }
        cls.task = cls.config["task"][0]

    def test_single_standalone_task(self):
        self.assertEqual(set(self.config), {"task"})
        self.assertEqual(len(self.config["task"]), 1)
        self.assertEqual(self.task["name"], "ClaimEventRewards")
        self.assertEqual(self.task["entry"], "ClaimEventRewardsEntrance")
        interface = load_jsonc(ROOT / "assets/interface.json")
        self.assertIn("resource/tasks/ClaimEventRewards.json", interface["import"])
        pipeline = load_jsonc(
            ROOT / "assets/resource/base/pipeline/ClaimEventRewards.json"
        )
        self.assertIn(self.task["entry"], pipeline)

    def test_task_is_opt_in_and_in_daily_group(self):
        self.assertIs(self.task["default_check"], False)
        self.assertEqual(self.task["group"], ["Daily"])
        self.assertNotIn("pipeline_override", self.task)

    def test_controller_is_explicit(self):
        self.assertEqual(self.task["controller"], ["Win32-Front"])

    def test_label_and_description_exist_in_all_locales(self):
        self.assertEqual(self.task["label"], "$task_claim_event_rewards_label")
        self.assertEqual(self.task["description"], "$task_claim_event_rewards_desc")
        for language, locale in self.locales.items():
            for field in ("label", "description"):
                with self.subTest(language=language, field=field):
                    value = locale[self.task[field][1:]]
                    self.assertIsInstance(value, str)
                    self.assertTrue(value.strip())

    def test_status_messages_exist_in_all_locales(self):
        for language, locale in self.locales.items():
            for key in STATUS_KEYS:
                with self.subTest(language=language, key=key):
                    value = locale[key]
                    self.assertIsInstance(value, str)
                    self.assertTrue(value.strip())

    def test_titles_are_localized(self):
        titles = {
            locale["task_claim_event_rewards_label"] for locale in self.locales.values()
        }
        self.assertEqual(len(titles), len(LANGUAGES))
        self.assertEqual(
            self.locales["zh_cn"]["task_claim_event_rewards_label"], "领取活动奖励"
        )

    def test_description_explains_daily_claims_and_no_summoning(self):
        desc = self.locales["zh_cn"]["task_claim_event_rewards_desc"]
        for phrase in ("每天运行", "七天", "十抽", "已解锁", "不抽卡", "不购买"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, desc)

    def test_unavailable_and_claimed_messages_are_distinct(self):
        for language, locale in self.locales.items():
            with self.subTest(language=language):
                values = [locale[key] for key in STATUS_KEYS]
                self.assertEqual(len(set(values)), len(STATUS_KEYS))


if __name__ == "__main__":
    unittest.main()
