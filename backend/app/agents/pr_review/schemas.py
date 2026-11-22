from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low", "nit"]
Category = Literal["bug", "security", "performance", "readability", "tests", "docs"]
SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "nit": 4}


class RawFinding(BaseModel):
    line: int | None = Field(None, description="Line number in the NEW file (use the numbers shown), or null")
    severity: Severity
    category: Category
    title: str = Field(max_length=120)
    comment: str = Field(description="Specific, actionable explanation. Mention why it matters.")
    suggestion: str | None = Field(None, description="Short suggested fix or code, if clear")


class FileReview(BaseModel):
    findings: list[RawFinding] = Field(default_factory=list, description="Empty if the change looks fine")


class Summary(BaseModel):
    walkthrough: str = Field(description="3-6 sentence plain-English summary of what the PR does and overall risk")
    verdict: Literal["approve", "comment", "request_changes"]
