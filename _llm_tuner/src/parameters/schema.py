"""Supported target systems and their parameter schema types."""

SUPPORTED_SYSTEMS = ["redis", "mysql"]

from pydantic import BaseModel, Field
from enum import Enum


class ParameterCategory(str, Enum):
    MEMORY = "memory"
    IO = "io"
    PERSISTENCE = "persistence"
    NETWORK = "network"
    CONNECTIONS = "connections"
    REPLICATION = "replication"
    LOGGING = "logging"
    GENERAL = "general"


class ParameterRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ParameterDefinition(BaseModel):
    name: str
    category: ParameterCategory
    description: str
    default_value: str
    type: str = "string"  # string, integer, float, boolean, enum
    min_value: str | None = None
    max_value: str | None = None
    enum_values: list[str] | None = None
    restart_required: bool = False
    risk: ParameterRisk = ParameterRisk.LOW
    depends_on: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)
    notes: str = ""
