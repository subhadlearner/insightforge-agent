"""T15 public-share capability records and signed-token claims."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ShareRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    owner_id: str
    id: str
    report_id: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


class ShareClaims(BaseModel):
    model_config = ConfigDict(frozen=True)

    owner_id: str
    share_id: str
    report_id: str
    expires_at: datetime
