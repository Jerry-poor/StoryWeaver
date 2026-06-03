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


class TestExtractPlotPoints(unittest.TestCase):
    def test_empty_instruction(self):
        result = sw._extract_plot_points("")
        self.assertEqual(result, [])

    def test_single_sentence(self):
        result = sw._extract_plot_points("主角发现神秘手稿")
        self.assertEqual(result, ["主角发现神秘手稿"])

    def test_multiple_chinese_sentences(self):
        result = sw._extract_plot_points("主角发现神秘手稿。他打开手稿阅读。手稿中预言了他的死亡")
        self.assertEqual(len(result), 3)
        self.assertIn("主角发现神秘手稿", result)
        self.assertIn("他打开手稿阅读", result)
        self.assertIn("手稿中预言了他的死亡", result)

    def test_semicolon_separated(self):
        result = sw._extract_plot_points("战斗场景要激烈；对话要有张力；结尾留悬念")
        self.assertEqual(len(result), 3)

    def test_filters_short_fragments(self):
        result = sw._extract_plot_points("主角发现手稿。嗯。然后他去调查真相")
        # "嗯" is too short (len <= 2), should be filtered
        self.assertEqual(len(result), 2)


class TestMergeInstruction(unittest.TestCase):
    def test_empty_user_instruction(self):
        result = sw._merge_instruction("", "AI生成的指引")
        self.assertEqual(result, "AI生成的指引")

    def test_empty_planned_instruction(self):
        result = sw._merge_instruction("用户要求写战斗", "")
        self.assertEqual(result, "用户要求写战斗")

    def test_both_empty(self):
        result = sw._merge_instruction("", "")
        self.assertEqual(result, "")

    def test_planned_is_subset_of_user(self):
        result = sw._merge_instruction("以悬疑色彩为主，每章约1000字左右", "以悬疑色彩为主")
        self.assertEqual(result, "以悬疑色彩为主，每章约1000字左右")

    def test_merge_different_instructions(self):
        result = sw._merge_instruction("用户原始指令", "AI补充指引")
        self.assertIn("用户原始指令", result)
        self.assertIn("AI补充指引", result)
        self.assertIn("【用户要求（必须遵循）】", result)
        self.assertIn("【补充指引】", result)

    def test_user_instruction_always_preserved(self):
        detailed = "主角在图书馆发现手稿；手稿预言了他的死亡；他决定调查真相"
        planned = "聚焦发现过程"
        result = sw._merge_instruction(detailed, planned)
        self.assertIn(detailed, result)


class TestBuildGenerationContextWithPlotPoints(unittest.TestCase):
    def _make_state(self):
        return {
            "outline": sw.default_outline(),
            "characters": sw.default_characters(),
            "storyline": sw.default_storyline(),
            "conversation_memory": sw.default_conversation_memory(),
            "instruction_registry": sw.default_instruction_registry(),
            "continuity": sw.default_continuity(),
        }

    def test_instruction_plot_points_present(self):
        state = self._make_state()
        ctx = sw.build_generation_context(
            state, 1, "主角发现手稿。手稿预言了死亡。他决定调查", "", 1800,
        )
        points = ctx["current_user_directives"]["instruction_plot_points"]
        self.assertIsInstance(points, list)
        self.assertEqual(len(points), 3)

    def test_empty_instruction_no_plot_points(self):
        state = self._make_state()
        ctx = sw.build_generation_context(state, 1, "", "", 1800)
        points = ctx["current_user_directives"]["instruction_plot_points"]
        self.assertEqual(points, [])


class TestComplianceWithPlotPoints(unittest.TestCase):
    @patch.object(sw, "deepseek_chat")
    def test_missing_plot_point_detected(self, mock_chat):
        mock_chat.return_value = json.dumps({
            "compliant": False,
            "violations": [
                {
                    "type": "plot_point_missing",
                    "rule": "主角发现手稿",
                    "evidence": "正文中未提及主角发现手稿",
                    "severity": "hard",
                }
            ],
        })
        ctx = {
            "current_user_directives": {
                "extra_instruction": "主角发现手稿。手稿预言了死亡",
                "instruction_plot_points": ["主角发现手稿", "手稿预言了死亡"],
            },
            "active_instruction_constraints": {
                "global_constraints": [],
                "chapter_constraints": [],
                "banned_patterns": [],
                "style_preferences": [],
            },
        }
        result = sw.check_instruction_compliance("一些没有手稿的文本", ctx)
        self.assertFalse(result["compliant"])
        plot_violations = [v for v in result["violations"] if v.get("type") == "plot_point_missing"]
        self.assertTrue(len(plot_violations) > 0)
        self.assertEqual(plot_violations[0]["severity"], "hard")


class TestCoerceStrList(unittest.TestCase):
    def test_list_input(self):
        self.assertEqual(sw._coerce_str_list(["a", "b"]), ["a", "b"])

    def test_string_single(self):
        self.assertEqual(sw._coerce_str_list("冷静沉着"), ["冷静沉着"])

    def test_string_with_chinese_comma(self):
        result = sw._coerce_str_list("冷静，沉着，果断")
        self.assertEqual(len(result), 3)
        self.assertIn("冷静", result)

    def test_string_with_chinese_enumeration_comma(self):
        result = sw._coerce_str_list("冷静、沉着、果断")
        self.assertEqual(len(result), 3)

    def test_empty_input(self):
        self.assertEqual(sw._coerce_str_list(""), [])
        self.assertEqual(sw._coerce_str_list(None), [])
        self.assertEqual(sw._coerce_str_list([]), [])

    def test_filters_empty_elements(self):
        self.assertEqual(sw._coerce_str_list(["a", "", "  ", "b"]), ["a", "b"])


class TestResolveField(unittest.TestCase):
    def test_primary_key(self):
        self.assertEqual(sw._resolve_field({"name": "李明"}, "name", ("alias",)), "李明")

    def test_alias_key(self):
        self.assertEqual(sw._resolve_field({"角色名": "李明"}, "name", ("角色名",)), "李明")

    def test_default_fallback(self):
        self.assertEqual(sw._resolve_field({}, "name", ("alias",), "默认"), "默认")

    def test_skips_empty_primary(self):
        self.assertEqual(sw._resolve_field({"name": "", "alias": "李明"}, "name", ("alias",)), "李明")

    def test_skips_empty_list_primary(self):
        self.assertEqual(sw._resolve_field({"personality": [], "性格": ["冷静"]}, "personality", ("性格",)), ["冷静"])


class TestNormalizeCharacterRecord(unittest.TestCase):
    def test_standard_fields(self):
        item = {
            "name": "李明",
            "role": "主角",
            "appearance": "高大英俊",
            "personality": ["冷静", "果断"],
            "motivation": "寻找真相",
        }
        result = sw.normalize_character_record(item, 1)
        self.assertEqual(result["name"], "李明")
        self.assertEqual(result["role"], "主角")
        self.assertEqual(result["appearance"], "高大英俊")
        self.assertEqual(result["personality"], ["冷静", "果断"])
        self.assertEqual(result["motivation"], "寻找真相")
        self.assertIn("current_state", result)
        self.assertIn("constraints", result)

    def test_chinese_field_names(self):
        item = {
            "姓名": "王芳",
            "身份": "配角",
            "外貌": "清秀",
            "性格": "温柔善良",
            "动机": "保护家人",
        }
        result = sw.normalize_character_record(item, 2)
        self.assertEqual(result["name"], "王芳")
        self.assertEqual(result["role"], "配角")
        self.assertEqual(result["appearance"], "清秀")
        self.assertEqual(result["personality"], ["温柔善良"])
        self.assertEqual(result["motivation"], "保护家人")

    def test_personality_as_string(self):
        item = {"name": "test", "personality": "冷静沉着"}
        result = sw.normalize_character_record(item, 1)
        self.assertEqual(result["personality"], ["冷静沉着"])

    def test_personality_as_comma_separated(self):
        item = {"name": "test", "性格": "冷静，沉着，果断"}
        result = sw.normalize_character_record(item, 1)
        self.assertEqual(len(result["personality"]), 3)

    def test_missing_fields_get_defaults(self):
        item = {"name": "李明"}
        result = sw.normalize_character_record(item, 1)
        self.assertEqual(result["appearance"], "")
        self.assertEqual(result["personality"], [])
        self.assertEqual(result["motivation"], "")
        self.assertEqual(result["constraints"], [])
        self.assertIsInstance(result["current_state"], dict)
        self.assertIn("location", result["current_state"])

    def test_non_dict_item(self):
        result = sw.normalize_character_record("李明", 1)
        self.assertEqual(result["name"], "李明")
        self.assertEqual(result["role"], "主角")

    def test_current_state_with_chinese_keys(self):
        item = {
            "name": "test",
            "当前状态": {"位置": "密室", "情绪": "紧张"},
        }
        result = sw.normalize_character_record(item, 1)
        self.assertEqual(result["current_state"]["location"], "密室")
        self.assertEqual(result["current_state"]["mood"], "紧张")


class TestBuildCharactersFromOutlineNormalization(unittest.TestCase):
    def test_variant_schema_characters(self):
        outline = sw.default_outline()
        outline["character_seed"] = [
            {"name": "李明", "role": "主角", "性格": "冷静", "外貌": "高大"},
            {"角色名": "王芳", "身份": "配角", "personality": ["温柔", "善良"]},
        ]
        result = sw.build_characters_from_outline(outline)
        chars = result["characters"]
        self.assertEqual(len(chars), 2)
        self.assertEqual(chars[0]["name"], "李明")
        self.assertEqual(chars[0]["personality"], ["冷静"])
        self.assertEqual(chars[0]["appearance"], "高大")
        self.assertEqual(chars[1]["name"], "王芳")
        self.assertEqual(chars[1]["personality"], ["温柔", "善良"])

    def test_string_only_seeds(self):
        outline = sw.default_outline()
        outline["character_seed"] = ["李明", "王芳"]
        result = sw.build_characters_from_outline(outline)
        chars = result["characters"]
        self.assertEqual(len(chars), 2)
        self.assertEqual(chars[0]["name"], "李明")
        self.assertEqual(chars[0]["id"], "c001")
        self.assertEqual(chars[1]["name"], "王芳")
        self.assertEqual(chars[1]["id"], "c002")


class TestMergeCharacterUpdatesNormalization(unittest.TestCase):
    def test_new_character_gets_normalized(self):
        characters = {"characters": [
            {"id": "c001", "name": "李明", "role": "主角", "appearance": "", "personality": [], "motivation": "", "constraints": [], "current_state": sw.default_character_state()},
        ]}
        new_chars = [
            {"id": "c999", "name": "神秘人", "role": "反派"},
        ]
        result = sw.merge_character_updates(characters, [], new_chars)
        added = [c for c in result["characters"] if c["id"] == "c999"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["name"], "神秘人")
        self.assertIn("personality", added[0])
        self.assertIn("appearance", added[0])
        self.assertIn("motivation", added[0])
        self.assertIn("constraints", added[0])
        self.assertIn("current_state", added[0])
        self.assertIn("location", added[0]["current_state"])


class TestStorylineThreadResolution(unittest.TestCase):
    def test_resolved_threads_removed_from_open_threads(self):
        storyline = {
            **sw.default_storyline(),
            "open_threads": ["谁在门外", "手稿来源"],
            "resolved_threads": [],
        }
        result = sw.update_storyline(
            storyline,
            2,
            "第2章",
            {
                "chapter_summary": [],
                "new_events": [],
                "open_threads": ["新的疑问"],
                "resolved_threads": ["谁在门外"],
                "storyline_summary": "继续推进",
            },
        )
        self.assertNotIn("谁在门外", result["open_threads"])
        self.assertIn("手稿来源", result["open_threads"])
        self.assertIn("新的疑问", result["open_threads"])
        self.assertIn("谁在门外", result["resolved_threads"])


class TestChapterHistoryInjection(unittest.TestCase):
    @patch.object(sw, "deepseek_chat")
    def test_generate_chapter_text_uses_confirmed_history_when_memory_provided(self, mock_chat):
        mock_chat.return_value = "新章节"
        memory = sw.default_conversation_memory()
        memory["recent_turns"] = [
            {
                "type": "chapter",
                "confirmed": True,
                "user_prompt_text": "上一章任务",
                "assistant_text": "上一章正文",
            }
        ]
        context = {
            "current_user_directives": {"length_target": 1800},
            "chapter_task": {"chapter_no": 2},
        }
        sw.generate_chapter_text(context, memory)
        messages = mock_chat.call_args[0][0]
        self.assertEqual(messages[1]["content"], "上一章任务")
        self.assertEqual(messages[2]["content"], "上一章正文")
        self.assertIn('"chapter_no": 2', messages[-1]["content"])


class TestArcCompressionQueue(unittest.TestCase):
    def _turn(self, no):
        return {
            "turn_id": no,
            "type": "chapter",
            "chapter_no": no,
            "confirmed": True,
            "assistant": f"第{no}章",
        }

    def test_compact_chapter_turns_keeps_partial_pending_batch(self):
        count = sw.KV_WINDOW + min(2, max(1, sw.ARC_SIZE - 1))
        memory = sw.default_conversation_memory()
        memory["recent_turns"] = [self._turn(no) for no in range(1, count + 1)]
        result = sw.compact_chapter_turns(memory)
        pending = result.get("pending_arc_compression", [])
        self.assertTrue(pending)
        self.assertEqual(len(pending[0]), count - sw.KV_WINDOW)
        self.assertEqual([t["chapter_no"] for t in pending[0]], list(range(1, count - sw.KV_WINDOW + 1)))

    @patch.object(sw, "generate_arc_summary")
    def test_drain_arc_compression_keeps_partial_and_processes_full_batches(self, mock_summary):
        mock_summary.return_value = {
            "arc_no": 1,
            "chapter_range": [1, sw.ARC_SIZE],
            "key_events": [],
            "character_state_snapshot": {},
            "open_threads_inherited": [],
            "arc_summary_text": "摘要",
        }
        partial = [self._turn(99)]
        full = [self._turn(no) for no in range(1, sw.ARC_SIZE + 1)]
        memory = sw.default_conversation_memory()
        memory["pending_arc_compression"] = [partial, full]
        result = sw.drain_arc_compression(memory)
        self.assertEqual(result["pending_arc_compression"], [partial])
        self.assertEqual(len(result["chapter_arc_summaries"]), 1)
        mock_summary.assert_called_once_with(full)

    @patch.object(sw, "save_json")
    @patch.object(sw, "generate_arc_summary")
    def test_drain_generation_memory_updates_state_before_context_build(self, mock_summary, mock_save):
        mock_summary.return_value = {
            "arc_no": 1,
            "chapter_range": [1, sw.ARC_SIZE],
            "key_events": ["关键事件"],
            "character_state_snapshot": {},
            "open_threads_inherited": [],
            "arc_summary_text": "已压缩弧线",
        }
        state = {
            "conversation_memory": {
                **sw.default_conversation_memory(),
                "pending_arc_compression": [[self._turn(no) for no in range(1, sw.ARC_SIZE + 1)]],
            }
        }
        sw.drain_generation_memory(state)
        self.assertEqual(state["conversation_memory"]["chapter_arc_summaries"][0]["arc_summary_text"], "已压缩弧线")
        mock_save.assert_called_with(sw.CONVERSATION_PATH, state["conversation_memory"])


class TestChapterGenerationHandlers(unittest.TestCase):
    def _state(self):
        outline = sw.default_outline()
        outline["status"] = "confirmed"
        return {
            "outline": outline,
            "characters": sw.default_characters(),
            "storyline": sw.default_storyline(),
            "conversation_memory": sw.default_conversation_memory(),
            "instruction_registry": sw.default_instruction_registry(),
            "continuity": sw.default_continuity(),
        }

    def _updates(self):
        return {
            "chapter_title": "抽取标题",
            "chapter_summary": ["摘要"],
            "new_events": ["事件"],
            "open_threads": ["线索"],
            "resolved_threads": [],
            "character_updates": [],
            "new_characters": [],
            "storyline_summary": "故事摘要",
            "ending_state": sw.default_ending_state(),
            "thread_priority": {"immediate_threads": [], "chapter_threads": [], "long_arc_threads": []},
        }

    def _load_json_side_effect(self, state):
        def side_effect(path, default):
            if path == sw.OUTLINE_PATH:
                return state["outline"]
            if path == sw.CONVERSATION_PATH:
                return sw.default_conversation_memory()
            return default
        return side_effect

    @patch.object(sw, "create_snapshot")
    @patch.object(sw, "save_json")
    @patch.object(sw, "extract_chapter_updates")
    @patch.object(sw, "run_chapter_quality_pipeline")
    @patch.object(sw, "deepseek_chat")
    @patch.object(sw, "load_json")
    @patch.object(sw, "read_state")
    def test_generate_chapter_non_stream_saves_extracted_title(
        self, mock_read_state, mock_load_json, mock_chat, mock_quality,
        mock_extract, mock_save, mock_snapshot,
    ):
        state = self._state()
        mock_read_state.return_value = state
        mock_load_json.side_effect = self._load_json_side_effect(state)
        mock_chat.return_value = "原始正文"
        mock_quality.return_value = ("修订正文", [], False)
        mock_extract.return_value = self._updates()
        handler = object.__new__(sw.Handler)
        handler._send_json = MagicMock()

        handler.handle_generate_chapter({"chapter_no": 1, "length_target": 1800})

        payload = handler._send_json.call_args[0][1]
        self.assertEqual(payload["title"], "抽取标题")
        chapter_saves = [call for call in mock_save.call_args_list if call.args[0] == sw.now_chapter_path(1)]
        self.assertEqual(chapter_saves[-1].args[1]["title"], "抽取标题")

    @patch.object(sw, "create_snapshot")
    @patch.object(sw, "save_json")
    @patch.object(sw, "extract_chapter_updates")
    @patch.object(sw, "run_chapter_quality_pipeline")
    @patch.object(sw, "deepseek_chat_stream")
    @patch.object(sw, "load_json")
    @patch.object(sw, "read_state")
    def test_generate_chapter_stream_updates_story_state_and_final_payload(
        self, mock_read_state, mock_load_json, mock_stream, mock_quality,
        mock_extract, mock_save, mock_snapshot,
    ):
        state = self._state()
        mock_read_state.return_value = state
        mock_load_json.side_effect = self._load_json_side_effect(state)
        mock_stream.return_value = iter(["原始", "正文"])
        mock_quality.return_value = ("修订正文", [], False)
        updates = self._updates()
        mock_extract.return_value = updates
        handler = object.__new__(sw.Handler)
        handler._send_sse_headers = MagicMock()
        handler._send_sse_event = MagicMock()

        handler.handle_generate_chapter_stream({"chapter_no": 1, "length_target": 1800})

        saved_paths = [call.args[0] for call in mock_save.call_args_list]
        self.assertIn(sw.CHARACTERS_PATH, saved_paths)
        self.assertIn(sw.STORYLINE_PATH, saved_paths)
        final_events = [
            call.args[1] for call in handler._send_sse_event.call_args_list
            if call.args[0] == "final"
        ]
        self.assertEqual(final_events[-1]["updates"], updates)
        self.assertEqual(final_events[-1]["title"], "抽取标题")

    @patch.object(sw, "save_json")
    @patch.object(sw, "run_chapter_quality_pipeline")
    @patch.object(sw, "deepseek_chat_stream")
    @patch.object(sw, "read_state")
    def test_start_chapter_stream_persists_chapter_task_content(
        self, mock_read_state, mock_stream, mock_quality, mock_save,
    ):
        state = self._state()
        mock_read_state.return_value = state
        mock_stream.return_value = iter(["正文"])
        mock_quality.return_value = ("正文", [], False)
        handler = object.__new__(sw.Handler)
        handler._send_sse_headers = MagicMock()
        handler._send_sse_event = MagicMock()

        handler.handle_start_chapter_stream({"chapter_no": 2, "length_target": 1800})

        chapter_saves = [call for call in mock_save.call_args_list if call.args[0] == sw.now_chapter_path(2)]
        self.assertTrue(chapter_saves)
        saved_draft = chapter_saves[-1].args[1]
        self.assertIn('"chapter_no": 2', saved_draft["chapter_task_content"])


if __name__ == "__main__":
    unittest.main()
