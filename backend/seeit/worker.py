from __future__ import annotations

import json
import os
import signal
import threading

from dotenv import load_dotenv
from rocketmq.client import ConsumeStatus, PushConsumer

from seeit.main import process_analysis

load_dotenv()


def main() -> None:
    nameserver = os.getenv("ROCKETMQ_NAMESERVER", "rmqnamesrv:9876")
    topic = os.getenv("ROCKETMQ_TOPIC", "video-analysis-topic")
    group = os.getenv("ROCKETMQ_CONSUMER_GROUP", "seeit-python-consumer")
    consumer = PushConsumer(group)
    consumer.set_namesrv_addr(nameserver)

    def handle(message):
        payload = json.loads(message.body.decode("utf-8"))
        process_analysis(payload["taskId"])
        return ConsumeStatus.CONSUME_SUCCESS

    consumer.subscribe(topic, handle)
    consumer.start()
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    stopped.wait()
    consumer.shutdown()


if __name__ == "__main__":
    main()
