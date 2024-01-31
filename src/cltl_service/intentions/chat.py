import logging
from typing import Union, List

from cltl.combot.event.emissor import TextSignalEvent, AnnotationEvent
from cltl.combot.infra.config import ConfigurationManager
from cltl.combot.infra.event import Event, EventBus
from cltl.combot.infra.resource import ResourceManager
from cltl.combot.infra.time_util import timestamp_now
from cltl.combot.infra.topic_worker import TopicWorker
from cltl.commons.discrete import UtteranceType
from cltl_service.emissordata.client import EmissorDataClient

logger = logging.getLogger(__name__)


class InitializeChatService():
    """
    Service used to integrate the component into applications.
    """

    @classmethod
    def from_config(cls, emissor_client: EmissorDataClient,
                    event_bus: EventBus, resource_manager: ResourceManager,
                    config_manager: ConfigurationManager):
        config = config_manager.get_config("cltl.leolani.intentions.chat")

        init_interval = config.get("init_interval") if "init_interval" in config else None
        intentions = config.get("intentions", multi=True) if "intentions" in config else []

        return cls(config.get("topic_scenario"), config.get("topic_utterance"), config.get("topic_speaker_mention"),
                   config.get("topic_intention"), intentions, init_interval,
                   emissor_client, event_bus, resource_manager)

    def __init__(self, scenario_topic: str, utterance_topic: str, speaker_mention_topic: str,
                 intention_topic: str, intentions: List[str], init_interval: int,
                 emissor_client: EmissorDataClient, event_bus: EventBus, resource_manager: ResourceManager):

        self._emissor_client = emissor_client
        self._event_bus = event_bus
        self._resource_manager = resource_manager

        self._scenario_topic = scenario_topic
        self._utterance_topic = utterance_topic
        self._speaker_mention_topic = speaker_mention_topic

        self._intention_topic = intention_topic
        self._intentions = intentions

        self._topic_worker = None

        self._init_interval = init_interval

        self._speaker = None
        self._last_utterance = None
        self._last_utterance_time = None
        self._active = False
        self._initialized = False

    def start(self, timeout=30):
        topics = [self._scenario_topic, self._intention_topic, self._utterance_topic]
        self._topic_worker = TopicWorker(topics, self._event_bus,
                                         provides=[self._speaker_mention_topic],
                                         resource_manager=self._resource_manager,
                                         scheduled=self._init_interval//2 if self._init_interval else None,
                                         buffer_size=4,
                                         processor=self._process, name=self.__class__.__name__)
        self._topic_worker.start().wait()

    def stop(self):
        if not self._topic_worker:
            pass

        self._topic_worker.stop()
        self._topic_worker.await_stop()
        self._topic_worker = None

    def _process(self, event: Event[Union[TextSignalEvent, AnnotationEvent]]):
        """
        This uses the last utterance before switching intention to 'chat'
        """
        if not event and self._last_utterance_time and self._init_interval:
            time_elapsed = timestamp_now() - self._last_utterance_time
            self._initialized = time_elapsed < self._init_interval // 1000
            logger.debug("Reset chat initialization after %s", time_elapsed)
        elif not event:
            pass
        elif (event.metadata.topic == self._scenario_topic
                and event.payload.scenario.context.speaker
                and event.payload.scenario.context.speaker.uri):
            self._speaker = event.payload.scenario.context.speaker
            logger.debug("Set speaker to %s", self._speaker)
        elif event.metadata.topic == self._utterance_topic:
            self._last_utterance = event.payload.signal.id
            self._last_utterance_time = event.metadata.timestamp
            # TODO ensure timestamps are millisec
            # self._last_utterance_time = event.payload.signal.time.end if event.payload.signal.time.end else event.metadata.timestamp
            logger.debug("Set last utterance to %s (%s)", self._last_utterance, event.payload.signal.text)
        else:
            self._active = self._chat_intention_is_active(event)
            self._initialized = self._active and self._initialized

        if self._active and not self._initialized and self._speaker:
            self._initialize_chat()
            self._initialized = True

    def _chat_intention_is_active(self, event):
        if event.metadata.topic != self._intention_topic or not hasattr(event.payload, "intentions"):
            return self._active

        return any(intention.label in self._intentions for intention in event.payload.intentions)

    def _initialize_chat(self):
        response_payload = self._create_payload()
        self._event_bus.publish(self._speaker_mention_topic, Event.for_payload(response_payload))
        logger.debug("Starting to chat with text mention %s", response_payload)

    def _create_payload(self):
        scenario_id = self._emissor_client.get_current_scenario_id()

        mention_text_capsule = {
            "chat": scenario_id,
            "turn": self._last_utterance,
            "author": self._get_author(),
            "utterance": "",
            "utterance_type": UtteranceType.TEXT_MENTION,
            "position": "",
            "item": self._get_author() | {'id': None},
            "perspective": {},
            'confidence': 1,
            "timestamp": timestamp_now(),
            "context_id": scenario_id
        }

        return [mention_text_capsule]

    def _get_author(self):
        return {
            "label": self._speaker.name if self._speaker and self._speaker.name else None,
            "type": ["person"],
            "uri": self._speaker.uri if self._speaker else None
        }
