from __future__ import annotations

import json
import logging
import os
import signal
import threading

from dotenv import load_dotenv
from rocketmq.client import ConsumeStatus, PushConsumer

from seeit.main import process_analysis

load_dotenv()
log = logging.getLogger("seeit.worker")


def main() -> None:
    nameserver = os.getenv("ROCKETMQ_NAMESERVER", "rmqnamesrv:9876")
    topic = os.getenv("ROCKETMQ_TOPIC", "video-analysis-topic")
    group = os.getenv("ROCKETMQ_CONSUMER_GROUP", "seeit-python-consumer")
    consumer = PushConsumer(group)
    consumer.set_namesrv_addr(nameserver)
    thread_count = max(1, int(os.getenv("ROCKETMQ_CONSUME_THREADS", "1")))
    if hasattr(consumer, "set_thread_count"):
        consumer.set_thread_count(thread_count)

    def handle(message):
        try:
            payload = json.loads(message.body.decode("utf-8"))
            outcome = process_analysis(payload["taskId"])
            if outcome == "RETRYING":
                return ConsumeStatus.RECONSUME_LATER
            return ConsumeStatus.CONSUME_SUCCESS
        except Exception:
            log.exception("analysis_message_failed message_id=%s", getattr(message, "id", "unknown"))
            return ConsumeStatus.RECONSUME_LATER

    consumer.subscribe(topic, handle)
    consumer.start()
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    stopped.wait()
    consumer.shutdown()


if __name__ == "__main__":
    main()
