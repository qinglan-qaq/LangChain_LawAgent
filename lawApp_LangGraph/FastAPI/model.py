from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


#  请求


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=5000, description="用户问题")
    session_id: Optional[str] = Field(
        default=None, description="会话 ID,传入可持续多轮对话;不传则新建"
    )


class ResumeRequest(BaseModel):
    """HITL resume:对 interrupt 的回复(反问补充 / 高风险确认 / PDF 确认)."""

    session_id: str = Field(..., description="发生 interrupt 的会话 ID")
    answer: str = Field(
        default="",
        max_length=3000,
        description="用户的回复内容;y/是 确认,n/否/跳过 拒绝",
    )


class FeedbackRequest(BaseModel):
    session_id: str = Field(..., description="被评价的会话 ID")
    rating: int = Field(..., ge=1, le=5, description="1-5 星")
    comment: str = Field(default="", max_length=2000, description="可选评价内容")


#  响应


class ToolInfo(BaseModel):
    """工具元信息"""

    name: str
    description: str


class SourceInfo(BaseModel):
    """回答引用的来源"""

    case_number: str = ""
    year: str = ""
    snippet: str = ""
    title: str = ""
    link: str = ""


class QueryResponse(BaseModel):
    query: str
    session_id: str
    final_answer: str
    final_prompt: str = ""
    sources: List[SourceInfo] = Field(default_factory=list)
    tool_calls: List[str] = Field(default_factory=list)
    reasoning: List[str] = Field(default_factory=list)
    # 中间 interrupt 请求(未完成时非空):type=clarify/risk_confirm/pdf_confirm
    interrupt: Optional[Dict[str, Any]] = None
    # 结构化提示词记录(评估 + 网络检索 + 法条)
    prompts_record: Dict[str, Any] = Field(default_factory=dict)
