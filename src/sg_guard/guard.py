"""Public prefill guard API for the supported model and frozen inference bundle."""
import time
from .config import load_assets
from .core import score_return, edit_request
from .observer import PrefillObserver, native_request


class SGGuard:
    """Inspect the final tool return and return its filtered text and audit data.

    The supplied model must use the bundled Qwen3-4B revision, FP32 and eager
    attention. Call this before agent continuation. After filtering, discard
    any generation cache containing the original return and prefill the edited
    messages; this class does not splice an already-consumed cache.
    """

    def __init__(self, model, tokenizer):
        import torch
        self.config, direction, weights, template = load_assets()
        config = self.config
        mc = model.config
        expected = (config["model_type"], config["hidden_size"], config["model_layers"])
        actual = (getattr(mc, "model_type", None), getattr(mc, "hidden_size", None),
                  getattr(mc, "num_hidden_layers", None))
        if actual != expected:
            raise ValueError("This bundle supports the pinned Qwen3-4B architecture only")
        if getattr(mc, "_attn_implementation", None) != "eager":
            raise ValueError("Load the model with attn_implementation='eager'")
        if any(p.dtype != torch.float32 for p in model.parameters() if p.is_floating_point()):
            raise ValueError("The provided readout requires FP32 model weights")
        commit = getattr(mc, "_commit_hash", None)
        if commit and commit != config["revision"]:
            raise ValueError("Model revision does not match the bundled probe")
        if tokenizer.chat_template != template:
            raise ValueError("Tokenizer template does not match; use Engine or the bundled chat_template.jinja")
        if not getattr(tokenizer, "is_fast", False):
            raise ValueError("A fast tokenizer with character offsets is required")
        if model.training:
            raise ValueError("Call model.eval() before constructing SGGuard")
        self.scorer = PrefillObserver(model, tokenizer, config, direction, weights)
        self.tokenizer = tokenizer

    def filter(self, messages, tools, verify_native=False, on_progress=None):
        """Return original/filtered text, signed token scores, windows and edits.

        ``messages`` is not mutated. The latest message must have role ``tool``.
        ``verify_native=True`` adds a check prefill and compares final logits.
        Progress callbacks receive real scores only once all required layers
        for those tokens have been observed.
        """
        started = time.time()
        request = native_request(messages, tools)
        result = self.scorer.score(request, verify_native=verify_native, on_progress=on_progress)
        decision = score_return(result["B_text"], result["scores"], result["B_offsets"],
                                threshold=self.config["threshold"], width=self.config["width"])
        edited, receipt = edit_request(request, decision)
        tokens = [dict(index=i, id=tid, text=self.tokenizer.decode([tid]), start=off[0],
                       end=off[1], score=float(q)) for i, (tid, off, q) in enumerate(
                           zip(result["B_ids"], result["B_offsets"], result["scores"]))]
        return dict(original=result["B_text"], filtered=edited["messages"][-1]["content"],
                    tokens=tokens, decision=decision, receipt=receipt,
                    seconds=round(time.time()-started, 3),
                    layer_weights=self.scorer.weights[0].tolist(), layer_scores=result["layer_scores"],
                    validation={"streamed_scores_exact": True, **{k: result[k] for k in (
                        "agent_input_unchanged", "attention_output_unchanged", "native_logits_exact",
                        "native_logits_max_error", "all_layers_tokens_observed", "attention_normalization_error",
                        "native_input_sha256", "detector_input_sha256")}})

    detect = filter
