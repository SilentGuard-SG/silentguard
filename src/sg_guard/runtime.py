"""Pinned local model runtime for the payment demo and tool-return integration."""
from pathlib import Path
import hashlib
import json
import os
import time

from .config import load_assets
from .guard import SGGuard
from .locking import INFERENCE_LOCK
from .observer import digest, native_request
from .protocol import encode_request, qwen_call
from .token_stream import TokenStream


def model_provenance(model_config, local_source, expected_revision):
    """Describe the checks performed without treating metadata as weight hashes."""
    reported = getattr(model_config, "_commit_hash", None)
    return dict(source="local_directory" if local_source else "hub_pinned_revision",
                reported_revision=reported,
                expected_revision=expected_revision,
                verification="architecture_and_reported_revision" if reported else "architecture_only",
                weight_hashes_verified=False)


class Engine:
    def __init__(self, model_path=None, device=None):
        import torch
        import transformers
        from transformers import AutoTokenizer, AutoModelForCausalLM
        config, _, _, template = load_assets()
        self.torch = torch
        self.frozen = config
        torch.set_num_threads(8)
        torch.manual_seed(0)
        local = model_path or os.environ.get("SG_MODEL_PATH")
        if local and not Path(local).is_dir():
            raise ValueError("SG_MODEL_PATH must be an existing local model directory")
        selected_device = device or os.environ.get("SG_DEVICE", "auto")
        if selected_device == "auto":
            selected_device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        if selected_device not in ("cuda", "mps", "cpu"):
            raise ValueError("SG_DEVICE must be auto, cuda, mps or cpu")
        self.device = selected_device
        source = str(local) if local else config["model_id"]
        kwargs = dict(local_files_only=bool(local), revision=config["revision"])
        started = time.time()
        self.tokenizer = AutoTokenizer.from_pretrained(source, use_fast=True, **kwargs)
        self.tokenizer.chat_template = template
        self.model = AutoModelForCausalLM.from_pretrained(
            source, torch_dtype=torch.float32, attn_implementation="eager", **kwargs).to(self.device).eval()
        self.model.requires_grad_(False)
        self.guard = SGGuard(self.model, self.tokenizer)
        self.scorer = self.guard.scorer
        self.scorer.rule_memory()
        self.identity = dict(model="Qwen3-4B", revision=config["revision"],
                             expected_revision=config["revision"],
                             model_provenance=model_provenance(self.model.config, bool(local), config["revision"]),
                             device=self.device, dtype="float32", torch=torch.__version__,
                             transformers=transformers.__version__, mode=config["mode"],
                             rule=config["rule"], threshold=config["threshold"], width=config["width"],
                             relation="prohibition", layers=config["observed_layers"],
                             load_seconds=round(time.time()-started, 2), rule_in_agent_input=False,
                             probe_sha256=config["assets"]["probe.npz"],
                             config_sha256=digest(config), source_assets=config["assets"],
                             source_code={})

    def request(self, messages, tools):
        return native_request(messages, tools)

    def generate(self, messages, tools, max_new_tokens=160, on_token=None):
        """Fresh greedy continuation. No cache containing unfiltered content is reused."""
        with INFERENCE_LOCK:
            started = time.time()
            torch = self.torch
            request = self.request(messages, tools)
            enc = encode_request(request, self.tokenizer)
            if len(enc["input_ids"]) > self.frozen["max_context_tokens"]:
                raise ValueError("Context exceeds the supported 4096-token demo limit")
            if type(max_new_tokens) is not int or max_new_tokens < 1:
                raise ValueError("max_new_tokens must be a positive integer")
            ids = torch.tensor([enc["input_ids"]], device=self.device)
            streamer = TokenStream(self.tokenizer, on_token)
            with torch.inference_mode():
                out = self.model.generate(input_ids=ids, attention_mask=torch.ones_like(ids),
                                          do_sample=False, max_new_tokens=max_new_tokens,
                                          pad_token_id=self.tokenizer.eos_token_id, use_cache=True,
                                          streamer=streamer)
            generated = out[0, len(enc["input_ids"]):].detach().cpu().tolist()
            if streamer.ids != generated:
                raise RuntimeError("Streamed tokens differ from generated output")
            text = self.tokenizer.decode(generated, skip_special_tokens=False)
            call, invalid = qwen_call(text)
            return dict(text=text, call=call, invalid=invalid, input_tokens=len(enc["input_ids"]),
                        generated_tokens=len(generated), token_ids=generated,
                        seconds=round(time.time()-started, 3), native_request_sha256=digest(request),
                        finish_reason="length" if len(generated) >= max_new_tokens else "stop",
                        streamed_tokens_exact=True)

    def detect(self, messages, tools, verify_native=False, on_progress=None):
        return self.guard.filter(messages, tools, verify_native=verify_native, on_progress=on_progress)
