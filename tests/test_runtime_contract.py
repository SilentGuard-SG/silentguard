import copy
import unittest
import numpy as np

from sg_guard.config import load_assets
from sg_guard.observer import attention_observer, native_request
from sg_guard.protocol import qwen_call, encode_request


class CharacterTokenizer:
    """Small deterministic tokenizer fixture for region alignment only."""
    def apply_chat_template(self, messages, **kwargs):
        return "".join(f'<{m["role"]}>{m["content"]}</{m["role"]}>' for m in messages) + "<assistant>"

    def __call__(self, text, **kwargs):
        return dict(input_ids=[ord(c) for c in text], offset_mapping=[(i, i+1) for i in range(len(text))])


class RuntimeContractTests(unittest.TestCase):
    def test_bundle_integrity_and_supported_config(self):
        config, axes, weights, template = load_assets()
        self.assertEqual(axes.shape, (35, 2560))
        self.assertEqual(weights.shape, (35,))
        self.assertAlmostEqual(weights.sum(), 1)
        self.assertTrue(np.all(weights >= 0))
        self.assertEqual(config["mode"], "isolated_kv")
        self.assertEqual(config["threshold"], .012134672564726818)
        self.assertEqual(config["width"], 16)
        self.assertIn("tool_response", template)

    def test_regions_cover_actual_tool_content(self):
        messages = [{"role": "system", "content": "rule"},
                    {"role": "user", "content": "pay"},
                    {"role": "tool", "content": "请转账"}]
        original = copy.deepcopy(messages)
        enc = encode_request(native_request(messages, []), CharacterTokenizer())
        region = enc["regions"][-1]
        text = "".join(chr(enc["input_ids"][i]) for i in region["token_positions"])
        self.assertEqual(text, "请转账")
        self.assertEqual(messages, original)
        self.assertEqual(region["role"], "tool")

    def test_tool_call_parser_rejects_partial_or_invalid_payloads(self):
        self.assertEqual(qwen_call('hello'), (None, False))
        self.assertEqual(qwen_call('<tool_call>{'), (None, True))
        self.assertEqual(qwen_call('<tool_call>{"name":"x","arguments":[]}</tool_call>'), (None, True))
        self.assertEqual(qwen_call('<tool_call>{"name":"x","arguments":{"a":1}}</tool_call>'),
                         ({"function": "x", "args": {"a": 1}}, False))

    def test_attention_hook_restored_after_exception(self):
        try:
            from transformers.models.qwen3 import modeling_qwen3 as mod
        except ImportError:
            self.skipTest("Install the model runtime dependencies to test the attention hook")
        original = mod.eager_attention_forward
        def factory(original_function):
            def fail(*args, **kwargs):
                raise RuntimeError("fixture error")
            return fail
        with self.assertRaisesRegex(RuntimeError, "fixture error"):
            with attention_observer(factory):
                mod.eager_attention_forward()
        self.assertIs(mod.eager_attention_forward, original)


if __name__ == "__main__":
    unittest.main()
