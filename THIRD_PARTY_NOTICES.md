# Third-party notices

The SilentGuard application code is provided under the [MIT License](LICENSE).

The bundled Qwen chat template is extracted, without changes to its template
text, from the `chat_template` field in the Qwen3-4B tokenizer configuration.
The enclosing runtime manifest and filename are specific to this package.

- Upstream: [Qwen/Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B).
- Revision: `1cfa9a7208912126459214e8b04321603b3df60c`.
- License: [Apache License 2.0](licenses/Qwen3-4B-LICENSE.txt).
- [Original license at the pinned revision](https://huggingface.co/Qwen/Qwen3-4B/blob/1cfa9a7208912126459214e8b04321603b3df60c/LICENSE).

The base model weights and tokenizer vocabulary are downloaded separately from
their upstream repository. They retain their upstream license. PyTorch,
Transformers, NumPy, FastAPI and Uvicorn are installed as dependencies; their
source distributions are not bundled here.
