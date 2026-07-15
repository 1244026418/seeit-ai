from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time

from dotenv import load_dotenv
from rocketmq.client import ConsumeStatus, PushConsumer

from seeit.main import (
    process_analysis,
    process_bilibili_import,
    stale_queued_analysis_task_ids,
    warm_local_asr_model,
)

load_dotenv()
log = logging.getLogger("seeit.worker")


def main() -> None:
    nameserver = os.getenv("ROCKETMQ_NAMESERVER", "rmqnamesrv:9876")
    topic = os.getenv("ROCKETMQ_TOPIC", "video-analysis-topic")
    group = os.getenv("ROCKETMQ_CONSUMER_GROUP", "seeit-python-consumer")
    consumer = PushConsumer(group)
    consumer.set_name_server_address(nameserver)
    thread_count = max(1, int(os.getenv("ROCKETMQ_CONSUME_THREADS", "1")))
    if hasattr(consumer, "set_thread_count"):
        consumer.set_thread_count(thread_count)

    warm_local_asr_model()

    stopped = threading.Event()

    def handle(message):
        started = time.perf_counter()
        try:
            payload = json.loads(message.body.decode("utf-8"))
            task_id = payload["taskId"]
            task_type = payload.get("type", "analysis")
            log.info("task_message_received type=%s task_id=%s", task_type, task_id)
            if payload.get("type") == "bilibili_import":
                outcome = process_bilibili_import(task_id)
            else:
                outcome = process_analysis(task_id)
            log.info(
                "task_message_finished type=%s task_id=%s outcome=%s elapsed_ms=%s",
                task_type,
                task_id,
                outcome,
                int((time.perf_counter() - started) * 1000),
            )
            if outcome == "RETRYING":
                return ConsumeStatus.RECONSUME_LATER
            return ConsumeStatus.CONSUME_SUCCESS
        except Exception:
            log.exception("analysis_message_failed message_id=%s", getattr(message, "id", "unknown"))
            return ConsumeStatus.RECONSUME_LATER

    consumer.subscribe(topic, handle)
    consumer.start()
    log.info("worker_started nameserver=%s topic=%s group=%s threads=%s", nameserver, topic, group, thread_count)

    def queue_watchdog() -> None:
        interval = max(1.0, float(os.getenv("QUEUE_WATCHDOG_INTERVAL_SECONDS", "2")))
        batch_size = max(1, int(os.getenv("QUEUE_WATCHDOG_BATCH_SIZE", "10")))
        while not stopped.wait(interval):
            try:
                task_ids = stale_queued_analysis_task_ids(batch_size)
                for task_id in task_ids:
                    started = time.perf_counter()
                    outcome = process_analysis(task_id)
                    log.warning(
                        "queued_task_fallback task_id=%s outcome=%s elapsed_ms=%s",
                        task_id,
                        outcome,
                        int((time.perf_counter() - started) * 1000),
                    )
            except Exception:
                log.exception("queue_watchdog_failed")

    watchdog = threading.Thread(target=queue_watchdog, name="queue-watchdog", daemon=True)
    watchdog.start()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    stopped.wait()
    consumer.shutdown()
    watchdog.join(timeout=5)
    log.info("worker_stopped")


if __name__ == "__main__":
    main()
