# Writeups

Three long-form pieces drawn from this project, drafted for publication (dev.to, a
personal blog, or Hacker News). Each is built around a result that surprised me, because
a post that only confirms what the reader already believes generates no discussion.

| | Angle | Core result |
|---|---|---|
| [1. The selectivity cliff](01-filtered-ann-selectivity-cliff.md) | Filtered ANN under row-level security | The most restricted users were the slowest — 79 ms vs 2 ms — and the obvious fix was on the wrong axis |
| [2. Containment beats detection](02-red-teaming-a-rag-system.md) | Red-teaming indirect prompt injection | 2 payloads defeated the classifier entirely and still widened nothing, because the grant lives where the model cannot write |
| [3. What actually moved retrieval quality](03-what-moved-retrieval-quality.md) | An ablation with honest negatives | Hybrid search didn't pay; the reranker did; and one "finding" was my own measurement bug |

All numbers are reproducible from the repo: `make eval`, `make bench`, `make redteam-indirect`, `make load`.
