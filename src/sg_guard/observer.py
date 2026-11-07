"""Read signed token scores against isolated rule memory during a diagnostic prefill.

The observer returns the original attention result unchanged. The diagnostic
cache is discarded; continuation consumes the filtered tool return in a fresh
prefill. No hook remains installed after scoring or after an exception.
"""
import copy
import hashlib
import json
from contextlib import contextmanager
import numpy as np

from .locking import INFERENCE_LOCK
from .protocol import encode_request


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def native_request(messages, tools):
    return dict(messages=copy.deepcopy(messages), tools=copy.deepcopy(tools),
                condition="native", policy=None, tool_protocol="qwen_native")


def prefill(model, ids, chunk=256):
    import torch
    if not ids:
        raise ValueError("Empty model input")
    cache = None
    device = next(model.parameters()).device
    with torch.inference_mode():
        for start in range(0, len(ids), chunk):
            stop = min(len(ids), start + chunk)
            pos = torch.arange(start, stop, device=device)
            out = model(input_ids=torch.tensor([ids[start:stop]], device=device),
                        attention_mask=torch.ones((1, stop), device=device, dtype=torch.long),
                        position_ids=pos[None], cache_position=pos, past_key_values=cache,
                        use_cache=True, return_dict=True, logits_to_keep=1)
            cache = out.past_key_values
        return out.logits[0, -1].detach().cpu()


@contextmanager
def attention_observer(callback):
    from transformers.models.qwen3 import modeling_qwen3 as mod
    with INFERENCE_LOCK:
        original = mod.eager_attention_forward
        mod.eager_attention_forward = callback(original)
        try:
            yield
        finally:
            mod.eager_attention_forward = original


class PrefillObserver:
    def __init__(self, model, tokenizer, config, direction, weights):
        self.model, self.tokenizer, self.config = model, tokenizer, config
        self.axes = direction[None]
        self.weights = weights[None]
        self.reference = None
        self.on_progress = None
        self.layer_count = config["observed_layers"]
        self.modules = {id(layer.self_attn) for layer in model.model.layers}

    def rule_memory(self):
        with INFERENCE_LOCK:
            if self.reference is not None:
                return self.reference
            request = native_request([dict(role="system", content=self.config["rule_prefix"] + self.config["rule"] + "\n")], [])
            enc = encode_request(request, self.tokenizer)
            positions = enc["regions"][0]["token_positions"]
            memory = {}

            def factory(original):
                def observe(module, query, key, value, attention_mask, scaling, dropout=0., **kwargs):
                    out = original(module, query, key, value, attention_mask, scaling, dropout, **kwargs)
                    if id(module) in self.modules and key.shape[-2] == len(enc["input_ids"]):
                        memory[int(module.layer_idx)] = (
                            key[:, :, positions].detach().clone(), value[:, :, positions].detach().clone())
                    return out
                return observe

            with attention_observer(factory):
                prefill(self.model, enc["input_ids"], max(len(enc["input_ids"]), 256))
            if len(memory) != self.config["model_layers"]:
                raise RuntimeError("Incomplete rule memory; check the supported eager attention runtime")
            self.reference = memory
            return memory

    def score(self, request, verify_native=False, on_progress=None):
        with INFERENCE_LOCK:
            return self._score(request, verify_native=verify_native, on_progress=on_progress)

    def _score(self, request, verify_native=False, on_progress=None):
        import torch
        from transformers.models.qwen3 import modeling_qwen3 as mod
        before = digest(request)
        source = copy.deepcopy(request)
        if not source.get("messages") or source["messages"][-1].get("role") != "tool":
            raise ValueError("Scoring requires the final message to be a tool return")
        enc = encode_request(source, self.tokenizer)
        if len(enc["input_ids"]) > self.config["max_context_tokens"]:
            raise ValueError("Context exceeds the supported 4096-token demo limit")
        last = len(source["messages"]) - 1
        region = next(r for r in enc["regions"] if r["message_index"] == last)
        bb, b0 = region["token_positions"], region["char_start"]
        btext = source["messages"][-1]["content"]
        offsets = [[max(0, enc["offsets"][i][0] - b0), min(len(btext), enc["offsets"][i][1] - b0)] for i in bb]
        tokens = [dict(index=i, id=enc["input_ids"][p], text=self.tokenizer.decode([enc["input_ids"][p]]),
                       start=off[0], end=off[1], score=None) for i, (p, off) in enumerate(zip(bb, offsets))]
        if on_progress:
            on_progress("readout_start", original=btext, tokens=tokens)
        memory = self.rule_memory()
        layers = self.layer_count
        layer_cos = np.zeros((1, layers, len(bb)), np.float32)
        layer_norm = np.zeros((layers, len(bb)), np.float32)
        observed = np.zeros((layers, len(bb)), bool)
        bb_index = {p: i for i, p in enumerate(bb)}
        streamed = {}
        axes = torch.as_tensor(self.axes, device=next(self.model.parameters()).device)
        max_norm_error = 0.

        def factory(original):
            def observe(module, query, key, value, attention_mask, scaling, dropout=0., **kwargs):
                nonlocal max_norm_error
                output, attention_weights = original(module, query, key, value, attention_mask, scaling, dropout, **kwargs)
                if id(module) not in self.modules:
                    return output, attention_weights
                layer = int(module.layer_idx)
                if layer >= layers:
                    return output, attention_weights
                start = key.shape[-2] - query.shape[-2]
                positions = [p for p in bb if start <= p < start + query.shape[-2]]
                if not positions:
                    return output, attention_weights
                v = mod.repeat_kv(value, module.num_key_value_groups)
                for lo in range(0, len(positions), 32):
                    pp = positions[lo:lo+32]
                    ii = torch.tensor([p-start for p in pp], device=query.device)
                    pw = attention_weights.index_select(2, ii)
                    rk, rv = memory[layer]
                    rk = mod.repeat_kv(rk, module.num_key_value_groups)
                    rv = mod.repeat_kv(rv, module.num_key_value_groups)
                    qq = query.index_select(2, ii)
                    native_k = mod.repeat_kv(key, module.num_key_value_groups)
                    native_logits = qq @ native_k.transpose(2, 3) * scaling
                    if attention_mask is not None:
                        native_logits += attention_mask[:, :, ii, :key.shape[-2]]
                    rule_logits = qq @ rk.transpose(2, 3) * scaling
                    combined = torch.softmax(torch.cat((native_logits, rule_logits), -1), -1,
                                             dtype=torch.float32).to(query.dtype)
                    max_norm_error = max(max_norm_error, float((combined.sum(-1)-1).abs().max()))
                    delta = combined[..., :key.shape[-2]] @ v + combined[..., key.shape[-2]:] @ rv - pw @ v
                    dv = delta.transpose(1, 2).contiguous().reshape(1, len(pp), -1)
                    f = torch.nn.functional.linear(dv, module.o_proj.weight, None)[0]
                    norms = torch.linalg.vector_norm(f, dim=-1)
                    cos = (f @ axes[:, layer].T) / norms.clamp_min(1e-30)[:, None]
                    ix = [bb_index[p] for p in pp]
                    if observed[layer, ix].any():
                        raise RuntimeError("Repeated token observation")
                    observed[layer, ix] = True
                    layer_cos[:, layer, ix] = cos.T.detach().cpu().numpy()
                    layer_norm[layer, ix] = norms.detach().cpu().numpy()
                    if layer == layers - 1:
                        if not observed[:, ix].all():
                            raise RuntimeError("Missing earlier token layer")
                        scores = np.einsum("sln,sl->sn", layer_cos.astype(float), self.weights)[0, ix]
                        batch = []
                        for i, q in zip(ix, scores):
                            if i in streamed:
                                raise RuntimeError("Repeated score event")
                            streamed[i] = float(q)
                            batch.append(dict(tokens[i], score=float(q)))
                        if on_progress:
                            on_progress("readout_progress", layer=layers, tokens=batch,
                                        completed=len(streamed), total=len(tokens))
                    elif layer in (0, 6, 12, 18, 24, 30) and on_progress:
                        on_progress("readout_layer", layer=layer+1, token_start=min(ix), token_end=max(ix)+1)
                return output, attention_weights
            return observe

        with attention_observer(factory):
            logits = prefill(self.model, enc["input_ids"], 256)
        if digest(request) != before:
            raise RuntimeError("Observer changed its input")
        if max_norm_error >= 1e-5 or not observed.all():
            raise RuntimeError("Attention or layer/token coverage check failed")
        native_equal = native_error = None
        if verify_native:
            check = prefill(self.model, enc["input_ids"], 256)
            native_equal = bool(torch.equal(logits, check))
            native_error = float((logits-check).abs().max())
            if not native_equal:
                raise RuntimeError(f"Observer changed native logits: {native_error}")
        scores = np.einsum("sln,sl->sn", layer_cos.astype(float), self.weights)[0]
        if not np.isfinite(scores).all() or len(streamed) != len(bb):
            raise RuntimeError("Nonfinite or incomplete token scores")
        if not np.array_equal([streamed[i] for i in range(len(bb))], scores):
            raise RuntimeError("Streamed scores differ from completed scores")
        return dict(B_text=btext, B_offsets=offsets, B_ids=[enc["input_ids"][p] for p in bb],
                    scores=scores.tolist(), layer_scores=layer_cos[0].tolist(),
                    agent_input_unchanged=True, attention_output_unchanged=True,
                    native_logits_exact=native_equal, native_logits_max_error=native_error,
                    attention_normalization_error=max_norm_error, all_layers_tokens_observed=True,
                    native_input_sha256=digest(enc["input_ids"]), detector_input_sha256=digest(enc["input_ids"]))
