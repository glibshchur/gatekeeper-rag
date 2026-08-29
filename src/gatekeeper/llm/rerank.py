"""Cross-encoder reranking.

A bi-encoder embeds the query and the passage separately, so the two never interact
until a dot product at the very end. That is what makes it fast enough to index 74,000
chunks, and it is also its ceiling: the passage's representation cannot depend on the
question being asked.

A cross-encoder feeds `[query, passage]` through the model together, so attention runs
across both. It scores far better and cannot be indexed — the cost is one forward pass
per pair. The standard arrangement, used here, is to let the cheap retriever propose
~50 candidates and the expensive model reorder them.

**Model choice, and why it is not the one the plan named.** The plan specified
`bge-reranker-v2-m3`, which publishes no ONNX export at all. The next candidate,
`bge-reranker-base`, does — as a single 1.1 GB file, which is eight times the size of
every other model in this project combined and stalled on an unauthenticated Hub fetch
for fifteen minutes without transferring a byte.

`cross-encoder/ms-marco-MiniLM-L-6-v2` is 22M parameters and roughly 90 MB. It is the
long-standing baseline reranker for exactly this job — reordering the candidates a cheap
retriever proposed — and it reranks 50 passages on a CPU in tens of milliseconds. For a
project whose central promise is `make bootstrap` on a stranger's laptop with no API key,
a 1.1 GB download that dominates both setup time and query latency is the wrong trade.
The quality gap to a larger cross-encoder is real and is the price paid; the ablation
reports what this one is actually worth.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from gatekeeper.retrieval.search import RetrievedChunk

logger = logging.getLogger(__name__)


class CrossEncoderReranker:
    """ms-marco-MiniLM-L-6-v2 via onnxruntime on CPU."""

    REPO = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    MAX_TOKENS = 512

    def __init__(self, batch_size: int = 16) -> None:
        from huggingface_hub import hf_hub_download
        from onnxruntime import InferenceSession, SessionOptions
        from tokenizers import Tokenizer

        self.batch_size = batch_size
        model_path = hf_hub_download(self.REPO, "onnx/model.onnx")
        tokenizer_path = hf_hub_download(self.REPO, "tokenizer.json")

        self._session = InferenceSession(
            model_path, SessionOptions(), providers=["CPUExecutionProvider"]
        )
        self._inputs = {i.name for i in self._session.get_inputs()}
        self._tokenizer = Tokenizer.from_file(tokenizer_path)
        self._tokenizer.enable_truncation(max_length=self.MAX_TOKENS)
        self._tokenizer.enable_padding()

    @property
    def model(self) -> str:
        return "ms-marco-MiniLM-L-6-v2"

    def score(self, query: str, passages: list[str]) -> NDArray[np.float32]:
        """Relevance logits, one per passage. Higher is more relevant.

        These are raw logits, not probabilities, and are only meaningful *relative to
        each other for one query*. Comparing them across queries, or thresholding them
        as if they were calibrated, is a mistake the interface deliberately does not
        make easy.
        """
        if not passages:
            return np.zeros((0,), dtype=np.float32)

        out: list[NDArray[np.float32]] = []
        for start in range(0, len(passages), self.batch_size):
            batch = passages[start : start + self.batch_size]
            encodings = self._tokenizer.encode_batch([(query, p) for p in batch])
            feed: dict[str, Any] = {
                "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            }
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)
            logits = self._session.run(None, feed)[0]
            out.append(np.asarray(logits, dtype=np.float32).reshape(-1))
        return np.concatenate(out)

    def rerank(
        self, query: str, chunks: list[RetrievedChunk], top_k: int | None = None
    ) -> list[RetrievedChunk]:
        """Reorder `chunks` by cross-encoder score, keeping the top_k."""
        if not chunks:
            return []
        scores = self.score(query, [c.content for c in chunks])
        order = np.argsort(-scores)
        ranked = []
        for index in order[: top_k or len(chunks)]:
            chunk = chunks[int(index)]
            ranked.append(
                type(chunk)(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    title=chunk.title,
                    path=chunk.path,
                    source_uri=chunk.source_uri,
                    heading_path=chunk.heading_path,
                    content=chunk.content,
                    score=float(scores[int(index)]),
                    sensitivity=chunk.sensitivity,
                )
            )
        return ranked
