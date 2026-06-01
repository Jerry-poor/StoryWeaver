from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import app as sw


class TestInstructionRegistry(unittest.TestCase):
    def test_default_instruction_registry(self):
        reg = sw.default_instruction_registry()
        self.assertIn("global_constraints", reg)
        self.assertIn("chapter_constraints", reg)
        self.assertIn("style_preferences", reg)
        self.assertIn("banned_patterns", reg)
        self.assertEqual(reg["global_constraints"], [])

    def test_load_save_roundtrip(self):
        reg = sw.default_instruction_registry()
        reg["global_constraints"] = ["不要写战斗"]
        reg["banned_patterns"] = ["男主冷笑"]
        with patch.object(sw, "save_json") as mock_save:
            sw.save_instruction_registry(reg)
            mock_save.assert_called_once()
            saved_data = mock_save.call_args[0][1]
            self.assertEqual(saved_data["global_constraints"], ["不要写战斗"])
            self.assertEqual(saved_data["banned_patterns"], ["男主冷笑"])


class TestContinuity(unittest.TestCase):
    def test_default_ending_state(self):
        es = sw.default_ending_state()
        self.assertIn("location", es)
        self.assertIn("time", es)
        self.assertIn("characters_present", es)
        self.assertIn("last_action", es)
        self.assertIn("last_dialogue", es)
        self.assertIn("emotional_tone", es)
        self.assertIn("immediate_unresolved_question", es)
        self.assertIn("scene_continues", es)
        self.assertIn("recommended_next_opening", es)
        self.assertFalse(es["scene_continues"])

    def test_default_continuity_contract(self):
        cc = sw.default_continuity_contract()
        self.assertIn("must_start_from_previous_ending", cc)
        self.assertIn("must_not_jump_time", cc)
        self.assertIn("must_not_change_location_immediately", cc)
        self.assertIn("opening_requirement", cc)
        self.assertIn("allowed_transition_after_words", cc)
        self.assertFalse(cc["must_start_from_previous_ending"])

    def test_update_continuity_from_updates_scene_continues(self):
        updates = {
            "ending_state": {
                "location": "密室",
                "time": "深夜",
                "characters_present": ["李明"],
                "last_action": "脚步声越来越近",
                "last_dialogue": "",
                "emotional_tone": "紧张",
                "immediate_unresolved_question": "谁在门外？",
                "scene_continues": True,
                "recommended_next_opening": "从脚步声继续",
            },
        }
        with patch.object(sw, "save_json"):
            sw.update_continuity_from_updates(updates)
            call_args = sw.save_json.call_args[0]
            saved = call_args[1]
            contract = saved["continuity_contract"]
            self.assertTrue(contract["must_start_from_previous_ending"])
            self.assertTrue(contract["must_not_jump_time"])
            self.assertTrue(contract["must_not_change_location_immediately"])
            self.assertEqual(contract["allowed_transition_after_words"], 0)

    def test_update_continuity_from_updates_scene_ends(self):
        updates = {
            "ending_state": {
                "location": "城镇",
                "time": "白天",
                "characters_present": ["李明"],
                "last_action": "他转身离去",
                "last_dialogue": "",
                "emotional_tone": "平静",
                "immediate_unresolved_question": "",
                "scene_continues": False,
                "recommended_next_opening": "",
            },
        }
        with patch.object(sw, "save_json"):
            sw.update_continuity_from_updates(updates)
            call_args = sw.save_json.call_args[0]
            saved = call_args[1]
            contract = saved["continuity_contract"]
            self.assertFalse(contract["must_start_from_previous_ending"])
            self.assertFalse(contract["must_not_jump_time"])
            self.assertFalse(contract["must_not_change_location_immediately"])
            self.assertEqual(contract["allowed_transition_after_words"], 200)


class TestBuildGenerationContext(unittest.TestCase):
    def _make_state(self):
        return {
            "outline": sw.default_outline(),
            "characters": sw.default_characters(),
            "storyline": sw.default_storyline(),
            "conversation_memory": sw.default_conversation_memory(),
            "instruction_registry": {
                "global_constraints": ["不要写战斗"],
                "chapter_constraints": [],
                "style_preferences": [],
                "banned_patterns": ["男主冷笑"],
            },
            "continuity": {
                "ending_state": sw.default_ending_state(),
                "continuity_contract": sw.default_continuity_contract(),
                "updated_at": "",
            },
        }

    def test_priority_order(self):
        state = self._make_state()
        ctx = sw.build_generation_context(state, 1, "测试指令", "", 1800)
        order = ctx["priority_order"]
        self.assertEqual(order[0], "current_user_directives")
        self.assertEqual(order[1], "active_instruction_constraints")
        self.assertEqual(order[2], "continuity_contract")
        self.assertEqual(order[3], "confirmed_story_facts")
        self.assertEqual(order[4], "characters")
        self.assertEqual(order[5], "outline")
        self.assertEqual(order[-1], "writing_optimization_goals")

    def test_user_directives_present(self):
        state = self._make_state()
        ctx = sw.build_generation_context(state, 1, "不要写战斗", "", 2000)
        self.assertEqual(ctx["current_user_directives"]["extra_instruction"], "不要写战斗")
        self.assertEqual(ctx["current_user_directives"]["length_target"], 2000)

    def test_instruction_constraints_injected(self):
        state = self._make_state()
        ctx = sw.build_generation_context(state, 1, "", "", 1800)
        self.assertEqual(ctx["active_instruction_constraints"]["global_constraints"], ["不要写战斗"])
        self.assertEqual(ctx["active_instruction_constraints"]["banned_patterns"], ["男主冷笑"])

    def test_continuity_contract_injected(self):
        state = self._make_state()
        ctx = sw.build_generation_context(state, 1, "", "", 1800)
        self.assertIn("continuity_contract", ctx)
        self.assertIn("previous_ending_state", ctx)


class TestCheckInstructionCompliance(unittest.TestCase):
    def test_no_constraints_always_compliant(self):
        ctx = {
            "current_user_directives": {"extra_instruction": ""},
            "active_instruction_constraints": {
                "global_constraints": [],
                "chapter_constraints": [],
                "banned_patterns": [],
                "style_preferences": [],
            },
        }
        result = sw.check_instruction_compliance("一些文本", ctx)
        self.assertTrue(result["compliant"])

    @patch.object(sw, "deepseek_chat")
    def test_banned_pattern_detected(self, mock_chat):
        mock_chat.return_value = json.dumps({
            "compliant": False,
            "violations": [
                {
                    "type": "banned_pattern",
                    "rule": "男主冷笑",
                    "evidence": "他冷笑了一声",
                    "severity": "hard",
                }
            ],
        })
        ctx = {
            "current_user_directives": {"extra_instruction": "不要写战斗"},
            "active_instruction_constraints": {
                "global_constraints": ["不要写战斗"],
                "chapter_constraints": [],
                "banned_patterns": ["男主冷笑"],
                "style_preferences": [],
            },
        }
        result = sw.check_instruction_compliance("他冷笑了一声，拔出了剑。", ctx)
        self.assertFalse(result["compliant"])
        self.assertTrue(any(v.get("severity") == "hard" for v in result["violations"]))


class TestCheckChapterContinuity(unittest.TestCase):
    def test_no_contract_always_continuous(self):
        ctx = {
            "continuity_contract": sw.default_continuity_contract(),
            "previous_ending_state": sw.default_ending_state(),
        }
        result = sw.check_chapter_continuity("一些文本", ctx)
        self.assertTrue(result["continuous"])

    def test_time_jump_regex(self):
        self.assertIsNotNone(sw.TIME_JUMP_PATTERNS.search("三日后，他回来了"))
        self.assertIsNotNone(sw.TIME_JUMP_PATTERNS.search("第二天清晨"))
        self.assertIsNotNone(sw.TIME_JUMP_PATTERNS.search("数日后"))
        self.assertIsNotNone(sw.TIME_JUMP_PATTERNS.search("翌日"))
        self.assertIsNone(sw.TIME_JUMP_PATTERNS.search("他缓缓睁开眼睛"))

    @patch.object(sw, "deepseek_chat")
    def test_time_jump_violation_detected(self, mock_chat):
        mock_chat.return_value = json.dumps({
            "continuous": True,
            "violations": [],
            "revision_instruction": "",
        })
        ctx = {
            "continuity_contract": {
                "must_start_from_previous_ending": True,
                "must_not_jump_time": True,
                "must_not_change_location_immediately": True,
                "opening_requirement": "从脚步声继续",
                "allowed_transition_after_words": 0,
            },
            "previous_ending_state": {
                "location": "密室",
                "time": "深夜",
                "characters_present": ["李明"],
                "last_action": "脚步声越来越近",
                "last_dialogue": "",
                "emotional_tone": "紧张",
                "immediate_unresolved_question": "谁在门外？",
                "scene_continues": True,
                "recommended_next_opening": "从脚步声继续",
            },
        }
        result = sw.check_chapter_continuity("三日后，李明已经回到了城镇。他望着远方……", ctx)
        self.assertFalse(result["continuous"])
        time_violations = [v for v in result["violations"] if v.get("type") == "time_jump"]
        self.assertTrue(len(time_violations) > 0)
        self.assertEqual(time_violations[0]["severity"], "hard")


class TestRunChapterQualityPipeline(unittest.TestCase):
    @patch.object(sw, "check_chapter_continuity")
    @patch.object(sw, "check_instruction_compliance")
    def test_passes_on_no_violations(self, mock_compliance, mock_continuity):
        mock_compliance.return_value = {"compliant": True, "violations": []}
        mock_continuity.return_value = {"continuous": True, "violations": [], "revision_instruction": ""}
        text, reports, needs_review = sw.run_chapter_quality_pipeline("测试文本", {})
        self.assertEqual(text, "测试文本")
        self.assertFalse(needs_review)

    @patch.object(sw, "revise_chapter_with_feedback")
    @patch.object(sw, "check_chapter_continuity")
    @patch.object(sw, "check_instruction_compliance")
    def test_revises_on_hard_violation(self, mock_compliance, mock_continuity, mock_revise):
        mock_compliance.side_effect = [
            {"compliant": False, "violations": [{"type": "banned_pattern", "rule": "男主冷笑", "evidence": "他冷笑", "severity": "hard"}]},
            {"compliant": True, "violations": []},
        ]
        mock_continuity.return_value = {"continuous": True, "violations": [], "revision_instruction": ""}
        mock_revise.return_value = "修订后文本"
        text, reports, needs_review = sw.run_chapter_quality_pipeline("原始文本", {})
        self.assertEqual(text, "修订后文本")
        self.assertFalse(needs_review)

    @patch.object(sw, "revise_chapter_with_feedback")
    @patch.object(sw, "check_chapter_continuity")
    @patch.object(sw, "check_instruction_compliance")
    def test_needs_user_review_after_3_failures(self, mock_compliance, mock_continuity, mock_revise):
        mock_compliance.return_value = {"compliant": False, "violations": [{"type": "banned_pattern", "rule": "男主冷笑", "evidence": "他冷笑", "severity": "hard"}]}
        mock_continuity.return_value = {"continuous": True, "violations": [], "revision_instruction": ""}
        mock_revise.return_value = "仍然有冷笑的文本"
        text, reports, needs_review = sw.run_chapter_quality_pipeline("原始文本", {})
        self.assertTrue(needs_review)
        self.assertEqual(len(reports), 3)


class TestSceneContinuesFlow(unittest.TestCase):
    def test_scene_continues_true_enforces_contract(self):
        updates = {
            "ending_state": {
                "location": "密室",
                "time": "深夜",
                "characters_present": ["李明"],
                "last_action": "脚步声越来越近",
                "last_dialogue": "",
                "emotional_tone": "紧张",
                "immediate_unresolved_question": "谁在门外？",
                "scene_continues": True,
                "recommended_next_opening": "从脚步声继续",
            },
        }
        with patch.object(sw, "save_json"):
            sw.update_continuity_from_updates(updates)
            saved = sw.save_json.call_args[0][1]
            contract = saved["continuity_contract"]
            self.assertTrue(contract["must_start_from_previous_ending"])
            self.assertTrue(contract["must_not_jump_time"])
            self.assertTrue(contract["must_not_change_location_immediately"])

    def test_scene_continues_false_allows_transition(self):
        updates = {
            "ending_state": {
                "location": "城镇",
                "time": "白天",
                "characters_present": ["李明"],
                "last_action": "他转身离去",
                "last_dialogue": "",
                "emotional_tone": "平静",
                "immediate_unresolved_question": "",
                "scene_continues": False,
                "recommended_next_opening": "",
            },
        }
        with patch.object(sw, "save_json"):
            sw.update_continuity_from_updates(updates)
            saved = sw.save_json.call_args[0][1]
            contract = saved["continuity_contract"]
            self.assertFalse(contract["must_start_from_previous_ending"])
            self.assertFalse(contract["must_not_jump_time"])
            self.assertFalse(contract["must_not_change_location_immediately"])
            self.assertEqual(contract["allowed_transition_after_words"], 200)


class TestExtractChapterUpdates(unittest.TestCase):
    @patch.object(sw, "deepseek_chat")
    def test_ending_state_and_thread_priority_extracted(self, mock_chat):
        mock_chat.return_value = json.dumps({
            "chapter_title": "密室惊魂",
            "chapter_summary": ["李明被困密室"],
            "new_events": ["脚步声逼近"],
            "open_threads": ["谁在门外"],
            "resolved_threads": [],
            "character_updates": [],
            "new_characters": [],
            "storyline_summary": "李明被困密室",
            "ending_state": {
                "location": "密室",
                "time": "深夜",
                "characters_present": ["李明"],
                "last_action": "脚步声越来越近",
                "last_dialogue": "",
                "emotional_tone": "紧张",
                "immediate_unresolved_question": "谁在门外？",
                "scene_continues": True,
                "recommended_next_opening": "从脚步声继续",
            },
            "thread_priority": {
                "immediate_threads": ["谁在门外"],
                "chapter_threads": [],
                "long_arc_threads": [],
            },
        })
        state = {
            "outline": sw.default_outline(),
            "characters": sw.default_characters(),
            "storyline": sw.default_storyline(),
            "continuity": sw.default_continuity(),
        }
        result = sw.extract_chapter_updates(state, 1, "李明站在密室中央，脚步声越来越近……")
        self.assertIn("ending_state", result)
        self.assertTrue(result["ending_state"]["scene_continues"])
        self.assertIn("thread_priority", result)
        self.assertIn("谁在门外", result["thread_priority"]["immediate_threads"])


class TestParseUserConstraints(unittest.TestCase):
    @patch.object(sw, "deepseek_chat")
    def test_extracts_banned_pattern(self, mock_chat):
        mock_chat.return_value = json.dumps({
            "global_constraints": [],
            "chapter_constraints": [],
            "style_preferences": [],
            "banned_patterns": ["男主冷笑"],
        })
        registry = sw.default_instruction_registry()
        result = sw.parse_user_constraints_from_message("以后不要让男主冷笑", registry)
        self.assertIn("男主冷笑", result["banned_patterns"])

    @patch.object(sw, "deepseek_chat")
    def test_extracts_global_constraint(self, mock_chat):
        mock_chat.return_value = json.dumps({
            "global_constraints": ["不要写战斗"],
            "chapter_constraints": [],
            "style_preferences": [],
            "banned_patterns": [],
        })
        registry = sw.default_instruction_registry()
        result = sw.parse_user_constraints_from_message("不要写战斗场景", registry)
        self.assertIn("不要写战斗", result["global_constraints"])


class TestSafeParseInt(unittest.TestCase):
    def test_safe_parse_int_valid(self):
        self.assertEqual(sw.safe_parse_int(1800, 1000), 1800)
        self.assertEqual(sw.safe_parse_int("2500", 1000), 2500)

    def test_safe_parse_int_with_characters(self):
        self.assertEqual(sw.safe_parse_int("1800字", 1000), 1800)
        self.assertEqual(sw.safe_parse_int("约 2000 左右", 1000), 2000)

    def test_safe_parse_int_fallback(self):
        self.assertEqual(sw.safe_parse_int(None, 1000), 1000)
        self.assertEqual(sw.safe_parse_int("abc", 1000), 1000)
        self.assertEqual(sw.safe_parse_int([], 1000), 1000)


class TestRelaxedJsonParsing(unittest.TestCase):
    def test_parse_json_relaxed_keeps_top_level_array(self):
        text = """```json
[
  {"chapter_no": 1, "goal": "铺垫谜团"},
  {"chapter_no": 2, "goal": "发现线索"}
]
```"""
        result = sw.parse_json_relaxed(text)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["chapter_no"], 1)
        self.assertEqual(result[1]["goal"], "发现线索")

    @patch.object(sw, "deepseek_chat")
    def test_plan_auto_chapters_accepts_array_response(self, mock_chat):
        mock_chat.return_value = """```json
[
  {
    "chapter_no": 1,
    "title_hint": "手稿现身",
    "goal": "侦探发现手稿并产生疑虑",
    "target_words": 1800,
    "tone": "悬疑",
    "instruction": "聚焦发现过程"
  }
]
```"""
        state = {
            "outline": {
                **sw.default_outline(),
                "status": "confirmed",
                "title": "预言手稿",
                "chapter_plan": [{"chapter_no": 1, "goal": "侦探发现手稿"}],
            },
            "characters": sw.default_characters(),
            "storyline": sw.default_storyline(),
            "continuity": sw.default_continuity(),
        }
        result = sw.plan_auto_chapters(state, 1, 1, "以悬疑色彩为主")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["chapter_no"], 1)
        self.assertEqual(result[0]["title_hint"], "手稿现身")
        self.assertEqual(result[0]["goal"], "侦探发现手稿并产生疑虑")


if __name__ == "__main__":
    unittest.main()
