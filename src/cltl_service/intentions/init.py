import logging
from typing import Mapping

from cltl.combot.event.bdi import DesireEvent
from cltl.combot.event.emissor import TextSignalEvent
from cltl.combot.infra.config import ConfigurationManager
from cltl.combot.infra.event import Event, EventBus
from cltl.combot.infra.resource import ResourceManager
from cltl.combot.infra.time_util import timestamp_now
from cltl.combot.infra.topic_worker import TopicWorker
from cltl_service.emissordata.client import EmissorDataClient
from emissor.representation.scenario import TextSignal

logger = logging.getLogger(__name__)


TIMEOUT = 30_000


class InitService:
    @classmethod
    def from_config(cls, emissor_client: EmissorDataClient,
                    event_bus: EventBus, resource_manager: ResourceManager, config_manager: ConfigurationManager):
        config = config_manager.get_config("cltl.leolani.intentions.init")
        topics = {
            "intention_topic": config.get("topic_intention"),
            "desire_topic": config.get("topic_desire"),
            "text_in_topic": config.get("topic_text_in"),
            "text_out_topic": config.get("topic_text_out"),
            "face_topic": config.get("topic_face"),
        }

        greeting = config.get("greeting")

        return cls(topics, greeting, emissor_client, event_bus, resource_manager)

    def __init__(self, topics: Mapping[str, str], greeting: str,
                 emissor_client: EmissorDataClient, event_bus: EventBus, resource_manager: ResourceManager):
        self._event_bus = event_bus
        self._resource_manager = resource_manager
        self._emissor_client = emissor_client

        self._intention_topic = topics["intention_topic"]
        self._desire_topic = topics["desire_topic"]
        self._text_in_topic = topics["text_in_topic"]
        self._text_out_topic = topics["text_out_topic"]
        self._face_topic = topics["face_topic"]
        self._greeting = greeting

        self._topic_worker = None

        self._timeout = None

    @property
    def app(self):
        return None

    def start(self, timeout=30):
        self._topic_worker = TopicWorker(list(filter(bool, [self._face_topic, self._text_in_topic])),
                                         self._event_bus, provides=[self._text_out_topic],
                                         intentions=["init"], intention_topic=self._intention_topic,
                                         resource_manager=self._resource_manager, processor=self._process,
                                         name=self.__class__.__name__)
        self._topic_worker.start().wait()

    def stop(self):
        if not self._topic_worker:
            pass

        self._topic_worker.stop()
        self._topic_worker.await_stop()
        self._topic_worker = None

    def _process(self, event: Event):
        timestamp = timestamp_now()
        if self._face_or_keyword(event) and not self._timeout:
            self._event_bus.publish(self._text_out_topic, Event.for_payload(self._greeting_payload()))
            self._timeout = timestamp
            logger.info("Start initialization")
        elif self._timeout and timestamp - self._timeout < TIMEOUT and self._start_utterance(event):
            self._timeout = None
            self._event_bus.publish(self._desire_topic, Event.for_payload(DesireEvent(["initialized"])))
            logger.info("Interaction initialized")
        elif self._timeout and timestamp - self._timeout > TIMEOUT:
            self._timeout = None
            logger.info("Reset initialization")

        logger.debug("Unhandled event %s (%s - %s)", event, timestamp, self._timeout)

    def _start_utterance(self, event):
        return event.metadata.topic == self._text_in_topic and "start" in event.payload.signal.text.lower()

    def _face_or_keyword(self, event):
        if event.metadata.topic == self._face_topic:
            return any(annotation.value
                for mention in event.payload.mentions
                for annotation in mention.annotations)
        if event.metadata.topic == self._text_in_topic:
            return "hallo" in event.payload.signal.text.lower()

    def _greeting_payload(self):
        scenario_id = self._emissor_client.get_current_scenario_id()
        signal = TextSignal.for_scenario(scenario_id, timestamp_now(), timestamp_now(), None, self._greeting)

        return TextSignalEvent.create(signal)
