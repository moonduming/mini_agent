from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

from config import get_settings

settings = get_settings()
client = QdrantClient(
    host=settings.qdrant.host,
    port=settings.qdrant.port,
)


def create():
    client.create_collection(
        collection_name=settings.qdrant.collection,
        vectors_config=VectorParams(
            size=1024,
            distance=Distance.COSINE
        )
    )


def insert():
    client.upsert(
        collection_name=settings.qdrant.collection,
        points=[
            PointStruct(
                id=1,
                vector=[0.1] * 1024,
                payload={"test": "FastAPI is a modern Python web framework."}
            ),
        ]
    )


def get():
    results = client.query_points(
        collection_name=settings.qdrant.collection,
        query=[0.1] * 1024,
        limit=3
    )

    print(results)


if __name__ == "__main__":
    create()
    # insert()
    # get()
