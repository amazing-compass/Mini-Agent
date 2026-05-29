# ✅ Todo -- 需要了解各个字段在Agent内部流转时的具体含义和作用
# 数据契约层 --- 定义了所有在Agent内部流转的核心数据结构
from enum import Enum
from typing import Any

from pydantic import BaseModel

# 支持的LLM后端枚举
class LLMProvider(str, Enum):
    """LLM provider types."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"

# LLM要调用的函数描述 --- 记录函数名name和已解析好的参数arguments
class FunctionCall(BaseModel):
    """Function call details."""

    name: str
    arguments: dict[str, Any]  # Function arguments as dict

# 完整的工具调用请求 --- 对OpenAI的Tool Call 的封装 
class ToolCall(BaseModel):
    """Tool call structure."""

    id: str
    type: str  # "function"
    function: FunctionCall

# 对话历史中的一条消息 -- Agent主循环的核心数据结构
class Message(BaseModel):
    """Chat message."""

    role: str  # "system", "user", "assistant", "tool"
    content: str | list[dict[str, Any]]  # Can be string or list of content blocks
    thinking: str | None = None  # Extended thinking content for assistant messages
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None  # For tool role

# TokenUsage -- 一次API调用的Token消耗明细 含Prompt Cache细分 --- 用于计算LLM成本
class TokenUsage(BaseModel):
    """Token usage statistics from LLM API response.

    ``prompt_tokens`` keeps its original semantics: total input tokens
    (uncached + cache_read + cache_creation). The new fields are
    *additional* breakdowns, not replacements, so downstream callers
    that only read ``prompt_tokens`` keep working.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_miss_tokens: int = 0

# 上下文压缩产物 --- 把历史多轮对话结构化归纳存储
class ContextSummary(BaseModel):
    """Cache-aligned compaction product, independent of Message."""

    covered_rounds: list[int]  # Which rounds are covered (e.g. [1,2,3,4,5])
    user_goals: list[str]  # Original user prompts (preserved losslessly)
    completed_work: list[str]  # Completed work items
    active_files: list[str]  # Active files
    key_findings: list[str]  # Key discoveries
    pending_todo: list[str]  # Pending items
    raw_text: str  # Rendered full text (for sending to API)

# LLM API 调用的统一返回封装
class LLMResponse(BaseModel):
    """LLM response."""

    content: str
    thinking: str | None = None  # Extended thinking blocks
    tool_calls: list[ToolCall] | None = None
    finish_reason: str
    usage: TokenUsage | None = None  # Token usage from API response
