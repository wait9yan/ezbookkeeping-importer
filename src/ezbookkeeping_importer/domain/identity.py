"""业务身份及规范化报告内容；不混入采集位置或邮件头。"""

import base64
import hashlib
import json

from .errors import ImporterError


def transaction_id(report_key: str, report_row_key: str) -> str:
    if not report_key or not report_row_key or ":" in report_key or ":" in report_row_key:
        raise ImporterError("report and row keys must be nonempty and contain no colon")
    digest = hashlib.sha256(f"{report_key}:{report_row_key}".encode()).digest()[:12]
    return base64.urlsafe_b64encode(digest).decode("ascii")


def content_fingerprint(content: dict) -> str:
    rows = [{k: v for k, v in row.items() if k != "evidence"} for row in content["rows"]]
    normalized = {**content, "rows": sorted(rows, key=lambda row: row["row_key"])}
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
