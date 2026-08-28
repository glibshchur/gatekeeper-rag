"""Embedding backends.

Two backends, one interface. The local ONNX backend is the default and requires no API
key, which is what makes ``make bootstrap`` work on a stranger's laptop. The hosted
backend exists so that Phase 3 can put both in the same ablation table -- comparing
retrieval quality across embedding spaces is only possible if both can be indexed
simultaneously, which is why the dimension is part of the backend's identity.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)

# BGE models are trained with an asymmetric objective: queries get an instruction prefix,
# passages do not. Omitting it costs measurable recall, and it is the single easiest
# thing to get wrong when wiring up this family of models.
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


@dataclass(frozen=True)
class EmbeddingSpace:
    """Identifies a vector space. Two backends are interchangeable only if these match."""

    model: str
    dim: int
    max_tokens: int

    @property
    def column(self) -> str:
        """The chunks column holding vectors in this space."""
        return f"embedding_{self.dim}"


class Embedder(ABC):
    """Encode text into a fixed vector space.

    Implementations must return L2-normalised vectors so that cosine distance and inner
    product agree, and so the ``<=>`` operator in Postgres needs no extra scaling.
    """

    space: EmbeddingSpace

    @abstractmethod
    def encode_passages(self, texts: Sequence[str]) -> NDArray[np.float32]: ...

    @abstractmethod
    def encode_query(self, text: str) -> NDArray[np.float32]: ...

    def count_tokens(self, text: str) -> int:
        """Token count in *this backend's* tokenizer.

        Chunk sizing must use the tokenizer of the model that will embed the chunk. A
        chunk sized with a different tokenizer can silently exceed the model's context
        window and get truncated, which loses the tail of the chunk without any error.
        """
        raise NotImplementedError


def _l2_normalise(vectors: NDArray[np.float32]) -> NDArray[np.float32]:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    # A zero vector cannot be normalised; leave it alone rather than emitting NaN.
    np.maximum(norms, 1e-12, out=norms)
    return (vectors / norms).astype(np.float32)


class LocalOnnxEmbedder(Embedder):
    """BGE via onnxruntime on CPU. No API key, no network after the first run."""

    REPO = "BAAI/bge-small-en-v1.5"
    space = EmbeddingSpace(model="bge-small-en-v1.5", dim=384, max_tokens=512)

    def __init__(self, batch_size: int = 32) -> None:
        from huggingface_hub import hf_hub_download
        from onnxruntime import InferenceSession, SessionOptions
        from tokenizers import Tokenizer

        self.batch_size = batch_size

        model_path = hf_hub_download(self.REPO, "onnx/model.onnx")
        tokenizer_path = hf_hub_download(self.REPO, "tokenizer.json")

        options = SessionOptions()
        options.intra_op_num_threads = 0  # let onnxruntime pick
        self._session = InferenceSession(model_path, options, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}

        self._tokenizer = Tokenizer.from_file(tokenizer_path)
        self._tokenizer.enable_truncation(max_length=self.space.max_tokens)
        self._tokenizer.enable_padding()

        # A second, unconstrained tokenizer purely for measurement. Counting with the
        # truncating one would report 512 for a 900-token passage, so the chunker would
        # believe every oversized chunk fit and the tail would be silently dropped.
        self._counter = Tokenizer.from_file(tokenizer_path)

    def count_tokens(self, text: str) -> int:
        return len(self._counter.encode(text, add_special_tokens=True).ids)

    def _forward(self, texts: Sequence[str]) -> NDArray[np.float32]:
        encodings = self._tokenizer.encode_batch(list(texts))
        feed: dict[str, Any] = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)

        hidden = self._session.run(None, feed)[0]
        # BGE pools the [CLS] token rather than mean-pooling. Mean pooling here would
        # produce plausible-looking vectors that quietly underperform.
        return _l2_normalise(np.asarray(hidden[:, 0], dtype=np.float32))

    def encode_passages(self, texts: Sequence[str]) -> NDArray[np.float32]:
        if not texts:
            return np.zeros((0, self.space.dim), dtype=np.float32)
        out = [
            self._forward(texts[i : i + self.batch_size])
            for i in range(0, len(texts), self.batch_size)
        ]
        return np.vstack(out)

    def encode_query(self, text: str) -> NDArray[np.float32]:
        # Indexing a 2-D array loses the element type in numpy's stubs; re-assert it.
        return np.asarray(self._forward([BGE_QUERY_INSTRUCTION + text])[0], dtype=np.float32)


class OpenAIEmbedder(Embedder):
    """text-embedding-3-*, with retry on rate limits handled by the SDK."""

    SPACES: ClassVar[dict[str, EmbeddingSpace]] = {
        "text-embedding-3-small": EmbeddingSpace("text-embedding-3-small", 1536, 8191),
        "text-embedding-3-large": EmbeddingSpace("text-embedding-3-large", 3072, 8191),
    }

    def __init__(self, api_key: str, model: str = "text-embedding-3-small", batch_size: int = 128):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "the hosted embedding backend needs the 'providers' extra: "
                "uv sync --extra providers"
            ) from exc
        import tiktoken

        if model not in self.SPACES:
            raise ValueError(f"unknown embedding model {model!r}")
        self.space = self.SPACES[model]
        self.batch_size = batch_size
        self._client = OpenAI(api_key=api_key, max_retries=6)
        self._encoding = tiktoken.get_encoding("cl100k_base")

    def count_tokens(self, text: str) -> int:
        return len(self._encoding.encode(text))

    def _embed(self, texts: Sequence[str]) -> NDArray[np.float32]:
        started = time.monotonic()
        response = self._client.embeddings.create(model=self.space.model, input=list(texts))
        logger.debug("embedded %d texts in %.2fs", len(texts), time.monotonic() - started)
        # The API does not guarantee ordering; it does return an index on each item.
        ordered = sorted(response.data, key=lambda d: d.index)
        return _l2_normalise(np.array([d.embedding for d in ordered], dtype=np.float32))

    def encode_passages(self, texts: Sequence[str]) -> NDArray[np.float32]:
        if not texts:
            return np.zeros((0, self.space.dim), dtype=np.float32)
        out = [
            self._embed(texts[i : i + self.batch_size])
            for i in range(0, len(texts), self.batch_size)
        ]
        return np.vstack(out)

    def encode_query(self, text: str) -> NDArray[np.float32]:
        # Symmetric model: no instruction prefix, unlike BGE.
        return np.asarray(self._embed([text])[0], dtype=np.float32)


# Declared statically so migrations and the schema can know every space up front without
# instantiating a backend (which would download a model).
KNOWN_SPACES: tuple[EmbeddingSpace, ...] = (
    LocalOnnxEmbedder.space,
    *OpenAIEmbedder.SPACES.values(),
)


def build_embedder(backend: str, openai_api_key: str | None = None, **kwargs: Any) -> Embedder:
    if backend == "local":
        return LocalOnnxEmbedder(**kwargs)
    if backend.startswith("openai"):
        if not openai_api_key:
            raise RuntimeError("GK_OPENAI_API_KEY is required for the openai embedding backend")
        model = kwargs.pop("model", None) or (
            backend.split(":", 1)[1] if ":" in backend else "text-embedding-3-small"
        )
        return OpenAIEmbedder(openai_api_key, model=model, **kwargs)
    raise ValueError(f"unknown embedding backend {backend!r}")


def spaces_by_dim() -> dict[int, list[EmbeddingSpace]]:
    grouped: dict[int, list[EmbeddingSpace]] = {}
    for space in KNOWN_SPACES:
        grouped.setdefault(space.dim, []).append(space)
    return grouped


def iter_dims() -> Iterable[int]:
    return sorted(spaces_by_dim())
