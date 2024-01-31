import logging
import uuid
from collections import Counter
from datetime import datetime

import requests
from cltl.combot.event.emissor import LeolaniContext, Agent, ScenarioStarted, ScenarioStopped, ScenarioEvent
from cltl.combot.infra.config import ConfigurationManager
from cltl.combot.infra.event import Event, EventBus
from cltl.combot.infra.resource import ResourceManager
from cltl.combot.infra.time_util import timestamp_now
from cltl.combot.infra.topic_worker import TopicWorker
from cltl.object_recognition.api import Object
from emissor.representation.scenario import Modality, Scenario, class_type

from cltl.friends.api import FriendStore

logger = logging.getLogger(__name__)


AGENT = Agent("Leolani", "http://cltl.nl/leolani/world/leolani")


class ContextService:
    @classmethod
    def from_config(cls, friend_store: FriendStore,
                    event_bus: EventBus, resource_manager: ResourceManager, config_manager: ConfigurationManager):
        config = config_manager.get_config("cltl.context")
        scenario_topic = config.get("topic_scenario")
        speaker_topic = config.get("topic_speaker")
        object_topic = config.get("topic_object")
        vector_id_topic = config.get("topic_vector_id")
        knowledge_topic = config.get("topic_knowledge")
        intention_topic = config.get("topic_intention")
        desire_topic = config.get("topic_desire")

        return cls(scenario_topic, speaker_topic, knowledge_topic,
                   object_topic, vector_id_topic,
                   intention_topic, desire_topic,
                   friend_store, event_bus, resource_manager)

    def __init__(self, scenario_topic: str, speaker_topic: str, knowledge_topic: str,
                 object_topic: str, vector_id_topic: str,
                 intention_topic: str, desire_topic: str,
                 friend_store: FriendStore, event_bus: EventBus, resource_manager: ResourceManager):
        self._event_bus = event_bus
        self._resource_manager = resource_manager

        self._scenario_topic = scenario_topic
        self._speaker_topic = speaker_topic
        self._intention_topic = intention_topic
        self._desire_topic = desire_topic
        self._object_topic = object_topic
        self._vector_id_topic = vector_id_topic
        self._knowledge_topic = knowledge_topic

        self._topic_worker = None

        self.AGENT = AGENT
        self._friend_store = friend_store
        self._scenario = None

    @property
    def app(self):
        return None

    def start(self, timeout=30):
        self._topic_worker = TopicWorker([self._intention_topic, self._desire_topic, self._speaker_topic,
                                          self._object_topic, self._vector_id_topic],
                                         self._event_bus, provides=[self._intention_topic],
                                         buffer_size=32, processor=self._process,
                                         resource_manager=self._resource_manager,
                                         name=self.__class__.__name__)
        self._topic_worker.start().wait()

    def stop(self):
        if not self._topic_worker:
            pass

        self._topic_worker.stop()
        self._topic_worker.await_stop()
        self._topic_worker = None

    def _process(self, event: Event):
        if event.metadata.topic == self._intention_topic:
            intentions = {intention.label for intention in event.payload.intentions}
            if "init" in intentions:
                self._start_scenario()
            if "terminate" in intentions:
                self._stop_scenario()
        elif event.metadata.topic == self._desire_topic:
            achieved = event.payload.achieved
            if "quit" in achieved:
                self._stop_scenario()
        elif event.metadata.topic == self._speaker_topic:
            self._update_scenario_speaker(event)
        elif event.metadata.topic == self._object_topic:
            self._update_scenario_context_objects(event)
        elif event.metadata.topic == self._vector_id_topic:
            self._update_scenario_context_people(event)
        else:
            logger.warning("Unhandled event: %s", event)

    def _start_scenario(self):
        scenario, capsule = self._create_scenario()
        self._event_bus.publish(self._scenario_topic,
                                Event.for_payload(ScenarioStarted.create(scenario)))
        self._event_bus.publish(self._knowledge_topic, Event.for_payload([capsule]))
        self._scenario = scenario
        logger.info("Started scenario %s", scenario)

    def _update_scenario_speaker(self, event):
        # TODO multiple mentions
        mention = event.payload.mentions[0]
        name_annotation = next(iter(filter(lambda a: a.type == "Entity", mention.annotations)))
        id_annotation = next(iter(filter(lambda a: a.type == "VectorIdentity", mention.annotations)))

        speaker_name = name_annotation.value.text
        uri = self._friend_store.add_friend(id_annotation.value, speaker_name,
                                            scenario_id=self._scenario.id, mention_id=mention.id)
        self._scenario.context.speaker = Agent(speaker_name, str(uri))

        self._event_bus.publish(self._scenario_topic, Event.for_payload(ScenarioEvent.create(self._scenario)))
        logger.info("Updated scenario %s", self._scenario)

    def _stop_scenario(self):
        self._scenario.ruler.end = timestamp_now()
        self._event_bus.publish(self._scenario_topic,
                                Event.for_payload(ScenarioStopped.create(self._scenario)))
        logger.info("Stopped scenario %s", self._scenario)

    def _create_scenario(self):
        signals = {
            Modality.IMAGE.name.lower(): "./image.json",
            Modality.TEXT.name.lower(): "./text.json",
            Modality.AUDIO.name.lower(): "./audio.json"
        }

        scenario_start = timestamp_now()
        location = self._get_location()

        scenario_context = LeolaniContext(AGENT, Agent(), str(uuid.uuid4()), location, [], [])
        scenario = Scenario.new_instance(str(uuid.uuid4()), scenario_start, None, scenario_context, signals)

        capsule = {
            "type": "context",
            "context_id": scenario.id,
            "date": datetime.utcfromtimestamp(scenario_start//1000).strftime('%Y-%m-%d'),
            "place": None,
            "place_id": None,
            "country": location["country"],
            "region": location["region"],
            "city": location["city"]
        }

        return scenario, capsule

    def _update_scenario_context_people(self, event):
        added = False
        # TODO replace "VectorIdentity" with class_type(VectorIdentity)
        for face_id in [annotation.value
                        for mention in event.payload.mentions
                        for annotation in mention.annotations
                        if annotation.type == "VectorIdentity"]:
            uri, names = self._friend_store.get_friend(face_id)
            if names and names[0] and uri:
                agent = Agent(names[0], uri)
            elif uri:
                agent = Agent(uri=uri)
            elif face_id:
                agent = Agent(uri=face_id)
            else:
                agent = None
                logger.debug("No person in event %s", event)

            if agent and agent not in self._scenario.context.persons:
                self._scenario.context.persons.append(agent)

            added = True

        if added:
            self._event_bus.publish(self._scenario_topic, Event.for_payload(ScenarioEvent.create(self._scenario)))
            logger.info("Updated scenario with persons %s", self._scenario)

    def _update_scenario_context_objects(self, event):
        object_labels = [annotation.value.label
                        for mention in event.payload.mentions
                        for annotation in mention.annotations
                        if annotation.type == class_type(Object) and annotation.value]

        object_counts = Counter(object_labels)
        current_counts = Counter(self._scenario.context.objects)
        object_counts.subtract(current_counts)

        for label, count in object_counts.items():
            if count > 0:
                self._scenario.context.objects.extend([label,] * count)

        if any(cnt > 0 for cnt in object_counts.values()):
            self._event_bus.publish(self._scenario_topic, Event.for_payload(ScenarioEvent.create(self._scenario)))
            logger.info("Updated scenario with persons %s", self._scenario)

    def _get_location(self):
        try:
            return requests.get("https://ipinfo.io").json()
        except:
            return {"country": "", "region": "", "city": ""}
