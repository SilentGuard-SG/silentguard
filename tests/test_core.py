import copy
import math
import unittest

from sg_guard.core import score_return, edit_return, edit_request


class WindowDeletionTests(unittest.TestCase):
    def test_signed_scores_and_strict_threshold(self):
        decision = score_return("abcd", [-2, 2, 2, -2], [(i, i+1) for i in range(4)], threshold=2, width=2)
        self.assertEqual(decision["window_scores"], [0, 2, 0])
        self.assertFalse(decision["alert"])
        self.assertEqual(edit_return("abcd", decision)[0], "abcd")

    def test_overlapping_windows_delete_union_once(self):
        decision = score_return("abcdef", [-1, 1, 1, 1, -1, -1], [(i, i+1) for i in range(6)], threshold=.5, width=2)
        self.assertEqual(decision["flagged_window_starts"], [1, 2])
        self.assertEqual([(s["start"], s["end"]) for s in decision["spans"]], [(1, 4)])
        filtered, receipt = edit_return("abcdef", decision)
        self.assertEqual(filtered, "aef")
        self.assertTrue(receipt["requires_fresh_prefill"])
        self.assertEqual(len(receipt["edits"]), 1)

    def test_short_return_uses_all_available_tokens(self):
        d = score_return("ab", [.2, .6], [(0, 1), (1, 2)], threshold=.3, width=16)
        self.assertEqual(d["effective_width"], 2)
        self.assertEqual(d["score"], .4)
        self.assertEqual(edit_return("ab", d)[0], "")

    def test_unicode_offsets_are_characters_not_bytes(self):
        d = score_return("请转账。", [-1, 2, 2, -1], [(i, i+1) for i in range(4)], threshold=1, width=2)
        self.assertEqual(edit_return("请转账。", d)[0], "请。")

    def test_empty_return(self):
        d = score_return("", [], [], threshold=.01)
        self.assertIsNone(d["score"])
        self.assertEqual(d["status"], "empty_return")
        self.assertEqual(edit_return("", d)[0], "")

    def test_no_threshold_cannot_edit(self):
        d = score_return("a", [1], [(0, 1)])
        self.assertIsNone(d["alert"])
        with self.assertRaises(ValueError):
            edit_return("a", d)

    def test_stale_decision_rejected(self):
        d = score_return("a", [1], [(0, 1)], threshold=.01)
        with self.assertRaises(ValueError):
            edit_return("b", d)

    def test_invalid_score_or_offset_rejected(self):
        for scores, offsets in [([math.nan], [(0, 1)]), ([1], []),
                                 ([1], [(0, 2)]), ([1], [(0, 0)]),
                                 ([1], [(0., 1)]), ([1, 1], [(1, 2), (0, 1)])]:
            with self.subTest(scores=scores, offsets=offsets), self.assertRaises(ValueError):
                score_return("ab" if len(scores) == 2 else "a", scores, offsets)

    def test_input_request_immutable_and_only_final_content_changed(self):
        request = {"messages": [{"role": "system", "content": "trusted"},
                                 {"role": "tool", "content": "abc", "tool_call_id": "one"}], "tools": []}
        original = copy.deepcopy(request)
        decision = score_return("abc", [-1, 2, -1], [(i, i+1) for i in range(3)], threshold=1, width=1)
        changed, receipt = edit_request(request, decision)
        self.assertEqual(request, original)
        self.assertEqual(changed["messages"][-1]["content"], "ac")
        self.assertEqual(changed["messages"][0], original["messages"][0])
        self.assertEqual(changed["messages"][-1]["tool_call_id"], "one")
        self.assertFalse(receipt["copied_untrusted_text_into_trusted_message"])

    def test_malformed_edit_spans_rejected(self):
        d = score_return("abc", [1], [(0, 3)], threshold=0)
        d["spans"][0]["end"] = 4
        with self.assertRaises(ValueError):
            edit_return("abc", d)


if __name__ == "__main__":
    unittest.main()
