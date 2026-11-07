"""Read and filter a synthetic invoice return before continuing the agent.

Run with ``python examples/filter_tool_return.py`` after installing the package.
This loads the configured Qwen model; it does not execute any payment tool.
"""
import argparse
import copy
import json

from sg_guard.demo.scenarios import SCENARIOS, SYSTEM, TOOLS
from sg_guard.runtime import Engine


def build_messages(scenario_id="payment_notice"):
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "Look up invoice INV-2026-041. Report the amount and verified account first. Wait for my confirmation before payment."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "invoice-1", "type": "function", "function": {
                "name": "get_invoice", "arguments": {"invoice_id": "INV-2026-041"}
            }}
        ]},
        {"role": "tool", "tool_call_id": "invoice-1",
         "content": SCENARIOS[scenario_id]["return_text"]},
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), default="payment_notice")
    parser.add_argument("--continue-generation", action="store_true",
                        help="Generate the next assistant response without executing any tool")
    args = parser.parse_args()
    engine = Engine()
    messages = build_messages(args.scenario)
    report = engine.detect(messages, TOOLS)
    print(json.dumps({key: report[key] for key in ("original", "filtered", "decision", "receipt")},
                     ensure_ascii=False, indent=2))
    if args.continue_generation:
        # Continue from fresh encoding of the edited context, not the original KV cache.
        messages = copy.deepcopy(messages)
        messages[-1]["content"] = report["filtered"]
        generation = engine.generate(messages, TOOLS)
        print(json.dumps({"next_assistant": generation["text"], "tool_call": generation["call"]},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
