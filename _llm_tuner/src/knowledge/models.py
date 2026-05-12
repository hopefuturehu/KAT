"""Knowledge base data models."""

from pydantic import BaseModel, Field
from datetime import datetime
import uuid


def uuid4_str() -> str:
    return str(uuid.uuid4())


class KnowledgeEntry(BaseModel):
    id: str = Field(default_factory=uuid4_str)
    system: str  # redis, mysql
    parameter_name: str
    title: str
    content: str
    category: str  # best_practice, relationship, risk, case_study, tuning_guide
    source: str = ""
    confidence: float = 1.0
    created_at: datetime = Field(default_factory=datetime.utcnow)

    def to_document(self) -> str:
        """Convert to a text document for embedding."""
        return f"""Parameter: {self.parameter_name}
System: {self.system}
Title: {self.title}
Category: {self.category}
Content: {self.content}
Source: {self.source}"""
