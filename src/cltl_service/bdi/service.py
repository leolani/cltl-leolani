import logging

from cltl.combot.event.bdi import IntentionEvent, Intention
from cltl.combot.infra.config import ConfigurationManager
from cltl.combot.infra.event import Event, EventBus
from cltl.combot.infra.resource import ResourceManager
from cltl.combot.infra.topic_worker import TopicWorker

logger = logging.getLogger(__name__)


CONTENT_TYPE_SEPARATOR = ';'


class BDIService:
    """
    Service to manage the BDI model of the agent.

    Components should listen to intentions (use intentions in the TopicWorker
    to activate them for certain intentions only) and publish achieved desires.
    """

    @classmethod
    def from_config(cls, bdi_model: dict, event_bus: EventBus, resource_manager: ResourceManager,
                    config_manager: ConfigurationManager):
        config = config_manager.get_config("cltl.bdi")

        return cls(bdi_model, config.get("topic_scenario"), config.get("topic_intention"), config.get("topic_desire"),
                   event_bus, resource_manager)

    def __init__(self, bdi_model: dict, scenario_topic: str, intention_topic: str, desire_topic: str,
                 event_bus: EventBus, resource_manager: ResourceManager):
        self._event_bus = event_bus
        self._resource_manager = resource_manager

        self._scenario_topic = scenario_topic
        self._intention_topic = intention_topic
        self._desire_topic = desire_topic

        self._topic_worker = None

        self._scenario = None
        self._intentions = []

        self._bdi = bdi_model

    @property
    def app(self):
        return None

    def start(self, timeout=30):
        self._topic_worker = TopicWorker([self._intention_topic, self._desire_topic],
                                         self._event_bus, provides=[self._intention_topic],
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
        try:
            if event.metadata.topic == self._intention_topic:
                self._intentions = {intention.label for intention in event.payload.intentions}
                logger.info("Set intentions to %s", self._intentions)
            elif event.metadata.topic == self._desire_topic:
                self._intentions = [intention
                                    for current_intention in self._intentions
                                    for achieved in event.payload.achieved
                                    for intention in self._bdi[current_intention][achieved]]
                intentions_payload = [Intention(intention, None) for intention in self._intentions]
                self._event_bus.publish(self._intention_topic, Event.for_payload(IntentionEvent(intentions_payload)))
                logger.info("Achieved %s, set intentions to %s", event.payload.achieved[0], intentions_payload)
        except:
            logger.exception("Failed to process achieved desire %s for intentions %s", event.payload, self._intentions)
