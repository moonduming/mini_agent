from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct
from config import get_settings
from ingestion.markdown import load_and_split_markdown


EMBEDDING_BATCH_SIZE = 32
PROJECT_ROOT = Path(__file__).resolve().parents[1]

settings = get_settings()
COLLECTION_NAME = settings.qdrant.collection
embedding_client = OpenAI(
    api_key=settings.llm.api_key,
    base_url=settings.llm.base_url,
)

qdrant_client = QdrantClient(
    host=settings.qdrant.host,
    port=settings.qdrant.port,
)

MARKDOWN_PATHS = [
    PROJECT_ROOT / "data/knowledge_raw/markdown/kafka/basic-kafka-operations.md",
    PROJECT_ROOT / "data/knowledge_raw/markdown/kafka/kraft.md",
    PROJECT_ROOT / "data/knowledge_raw/markdown/kubernetes/debug-init-containers.md",
    PROJECT_ROOT / "data/knowledge_raw/markdown/kubernetes/debug-running-pod.md",
    PROJECT_ROOT / "data/knowledge_raw/markdown/kubernetes/determine-reason-pod-failure.md",
    PROJECT_ROOT / "data/knowledge_raw/markdown/kubernetes/debug-pods.md",
    PROJECT_ROOT / "data/knowledge_raw/markdown/kubernetes/debug-service.md",
]


def embed_texts(texts: list[str]) -> list[list[float]]:
    response = embedding_client.embeddings.create(
        model=settings.llm.embedding_model,
        input=texts,
    )
    return [item.embedding for item in response.data]


def upsert_chunks(
    file_path: Path,
    chunks: list[dict],
    vectors: list[list[float]],
) -> None:
    if len(chunks) != len(vectors):
        raise ValueError("chunks 和 vectors 数量不一致")

    source = file_path.relative_to(PROJECT_ROOT).as_posix()
    points = []

    for chunk, vector in zip(chunks, vectors):
        # 相同文件、相同 chunk_index 每次生成相同 ID，重复执行时会覆盖旧数据。
        point_id = str(
            uuid5(NAMESPACE_URL, f"{source}:{chunk['chunk_index']}")
        )
        point = PointStruct(
            id=point_id,
            vector=vector,
            payload={
                "source": source,
                "heading_path": chunk["heading_path"],
                "chunk_index": chunk["chunk_index"],
                "content": chunk["content"],
                "block_types": chunk["block_types"],
            }
        )
        points.append(point)

    qdrant_client.upsert(
        collection_name=COLLECTION_NAME,
        points=points,
        wait=True,
    )


def main() -> None:
    for file_path in MARKDOWN_PATHS:
        _, chunks = load_and_split_markdown(
            str(file_path),
            overlap_tokens=0,
        )

        for start in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
            batch_chunks = chunks[start:start + EMBEDDING_BATCH_SIZE]
            vectors = embed_texts(
                [chunk["content"] for chunk in batch_chunks]
            )
            upsert_chunks(file_path, batch_chunks, vectors)


if __name__ == "__main__":
    main()
