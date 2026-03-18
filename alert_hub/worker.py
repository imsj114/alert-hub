from __future__ import annotations

import threading
from dataclasses import dataclass

from alert_hub.service import AlertHubService


@dataclass
class WorkerHandle:
    stop_event: threading.Event
    thread: threading.Thread

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)


def start_worker(service: AlertHubService) -> WorkerHandle:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=service.run_worker_loop,
        args=(stop_event,),
        name="alert-hub-worker",
        daemon=True,
    )
    thread.start()
    return WorkerHandle(stop_event=stop_event, thread=thread)
