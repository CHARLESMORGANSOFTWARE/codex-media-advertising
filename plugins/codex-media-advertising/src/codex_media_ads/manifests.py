from __future__ import annotations

import json
from pathlib import Path

from .models import CampaignManifest


def load_campaign(path: Path) -> CampaignManifest:
    data = json.loads(Path(path).read_text())
    return CampaignManifest.model_validate(data)
