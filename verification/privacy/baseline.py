from __future__ import annotations

import hashlib
import json


def content_sha256(doc: dict) -> str:
    hashed = {key: value for key, value in doc.items() if key != "content_sha256"}
    payload = json.dumps(hashed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
