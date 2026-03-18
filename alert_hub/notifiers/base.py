from __future__ import annotations

from typing import Protocol

from alert_hub.config import NtfyTargetConfig
from alert_hub.models import DeliveryJob, DeliveryResult


class Notifier(Protocol):
    def send(self, job: DeliveryJob, target: NtfyTargetConfig) -> DeliveryResult:
        ...
