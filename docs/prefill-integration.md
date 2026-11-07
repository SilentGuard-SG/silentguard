# 在工具返回处接入 SilentGuard

## 调用顺序

1. 让智能体生成工具调用，并按应用自身的权限规则执行工具。
2. 将返回值作为新的 `tool` 消息追加到当前上下文，保留对应的 `tool_call_id`。
3. 调用 `Engine.detect(messages, tools)`。读出最后一条工具消息，不根据示例攻击位置切片。
4. 保存原文与读出结果，将该消息的内容替换为 `filtered`。
5. 从处理后的消息继续生成。

探针的规则参照固定，不用改成针对某个收款人或某条攻击的特定规则。返回可能包含多段正常和异常内容；系统以滑窗和区间并集决定编辑范围。

## 最小调用

```python
from sg_guard.runtime import Engine

engine = Engine()
messages = [
    {"role": "system", "content": "You are a payment assistant. Wait for user confirmation before payment."},
    {"role": "user", "content": "Look up invoice INV-2026-041. Report its amount and account first."},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "invoice-1", "type": "function", "function": {
            "name": "get_invoice", "arguments": {"invoice_id": "INV-2026-041"}
        }}
    ]},
    {"role": "tool", "tool_call_id": "invoice-1", "content": tool_result},
]
report = engine.detect(messages, tools)
messages[-1] = {**messages[-1], "content": report["filtered"]}
next_generation = engine.generate(messages, tools)
```

`tool_result` 是本次实际返回，`tools` 是同一模型会话使用的原生工具定义。不要将攻击示例的标签、人工边界或对应良性文本作为检测输入。

## prefill 与缓存

当前实现把固定规则编码成独立的参照 K/V。读取工具返回时，观察器计算各层响应并投影到冻结探针，随后按冻结层权重汇总。它保持本次原生注意力输出不变，只返回观测分数。

这是**读出 prefill → 文本编辑 → 生成 prefill**的参考实现。观察器的原始文本缓存不交给编辑后的生成。若工程接入需要保留前缀缓存，只能复用未改变、位于工具返回之前的可信前缀，并重新编码被编辑的工具返回及后缀。本仓库的 `Engine.generate` 直接使用处理后的完整消息编码，不实现跨阶段前缀缓存复用。

不要将它替换为普通聊天API的一次远程请求：该实现需要访问 Qwen 模型内部的注意力计算。当前支持矩阵和版本见 [运行说明](runtime.md)。

## 评分与编辑约定

- 保留原始有符号分数，不将负值截为零。
- 默认窗口为16 token，步长为1；较短返回按实际长度形成一个窗口。
- 窗口均分采用严格大于阈值的判定。
- 将命中窗口映射到工具返回的字符偏移，合并重叠区间，只编辑这一条消息。
- 空返回不产生待删除内容；无命中时原文保持不变。

偏移是 Python 字符串索引，不是 UTF-8 字节索引。外部前端若使用 JavaScript UTF-16 索引，需要显式转换；内置界面和后端返回原文及 token 信息共同展示编辑。

## 审计记录

建议保存运行配置标识、原始返回、处理后的返回、token分数、窗口分数、删除区间，以及随后实际执行的工具调用。保留失败和正常返回记录，便于检查误删及任务损失。

不要仅用“没有发生攻击者目标”推断任务完成。Payment Agent 通过独立的模拟账本核对金额、账户、备注及额外付款；一般应用应使用自己的业务完成条件。
