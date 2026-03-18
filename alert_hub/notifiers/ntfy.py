from __future__ import annotations

from urllib.parse import quote

import httpx

from alert_hub.config import NtfyTargetConfig
from alert_hub.models import DeliveryJob, DeliveryResult, Severity

PRIORITY_MAP = {
    Severity.INFO: "3",
    Severity.WARNING: "4",
    Severity.ERROR: "4",
    Severity.CRITICAL: "5",
}


class NtfyNotifier:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def send(self, job: DeliveryJob, target: NtfyTargetConfig) -> DeliveryResult:
        endpoint = f"{target.base_url.rstrip('/')}/{quote(target.topic, safe='')}"
        headers = {
            "Title": f"[{job.severity.value.upper()}] {job.summary}",
            "Priority": PRIORITY_MAP[job.severity],
            "Tags": ",".join(dict.fromkeys((*target.tags, job.severity.value))),
        }
        if job.links:
            first_link = job.links[0].get("url")
            if first_link:
                headers["Click"] = first_link
        if target.token:
            headers["Authorization"] = f"Bearer {target.token}"

        lines = [f"Source: {job.source}", f"Type: {job.event_type}"]
        if job.body:
            lines.extend(["", job.body])
        if len(job.links) > 1:
            lines.append("")
            for link in job.links[1:]:
                if not link.get("url"):
                    continue
                label = link.get("label")
                lines.append(f"{label}: {link['url']}" if label else link["url"])
        body = "\n".join(lines)

        try:
            response = self._client.post(endpoint, content=body.encode("utf-8"), headers=headers)
        except httpx.TimeoutException as exc:
            return DeliveryResult(delivered=False, retryable=True, error=f"timeout: {exc}")
        except httpx.RequestError as exc:
            return DeliveryResult(delivered=False, retryable=True, error=f"request error: {exc}")

        if 200 <= response.status_code < 300:
            return DeliveryResult(delivered=True, retryable=False)
        if response.status_code == 429 or 500 <= response.status_code < 600:
            return DeliveryResult(
                delivered=False,
                retryable=True,
                error=f"ntfy returned {response.status_code}: {response.text[:200]}",
            )
        return DeliveryResult(
            delivered=False,
            retryable=False,
            error=f"ntfy returned {response.status_code}: {response.text[:200]}",
        )
