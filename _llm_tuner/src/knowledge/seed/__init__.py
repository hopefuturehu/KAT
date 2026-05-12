from src.knowledge.models import KnowledgeEntry
from src.knowledge.seed.redis_knowledge import REDIS_KNOWLEDGE
from src.knowledge.seed.mysql_knowledge import MYSQL_KNOWLEDGE
from src.knowledge.seed.os_knowledge import OS_KNOWLEDGE

ALL_SEED_KNOWLEDGE: list[KnowledgeEntry] = (
    REDIS_KNOWLEDGE + MYSQL_KNOWLEDGE + OS_KNOWLEDGE
)


async def seed_knowledge_base(retriever) -> None:
    from src.knowledge.retriever import KnowledgeBaseRetriever
    await retriever.seed(ALL_SEED_KNOWLEDGE)
