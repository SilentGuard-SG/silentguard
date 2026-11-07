"""Read-only token streamer for Transformers generate; no extra decoding pass."""
class TokenStream:
    def __init__(self, tokenizer, callback):
        self.tokenizer = tokenizer
        self.callback = callback
        self.first = True
        self.ids = []

    def put(self, value):
        if self.first:
            self.first = False  # generate first emits the unchanged prompt
            return
        values = value.detach().cpu().reshape(-1).tolist()
        self.ids.extend(values)
        if self.callback:
            self.callback(dict(token_ids=values, generated_tokens=len(self.ids), text=self.tokenizer.decode(self.ids, skip_special_tokens=False)))

    def end(self):
        pass
