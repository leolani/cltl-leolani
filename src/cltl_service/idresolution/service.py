import logging
import time

from cltl.commons.discrete import UtteranceType
from cltl.combot.infra.config import ConfigurationManager
from cltl.combot.infra.event import Event, EventBus
from cltl.combot.infra.resource import ResourceManager
from cltl.combot.infra.topic_worker import TopicWorker
from cltl.combot.infra.time_util import timestamp_now
from cltl_service.emissordata.client import EmissorDataClient

from cltl.friends.api import FriendStore

logger = logging.getLogger(__name__)


class IdResolutionService:
    @classmethod
    def from_config(cls, friend_store: FriendStore, emissor_client: EmissorDataClient,
                    event_bus: EventBus, resource_manager: ResourceManager, config_manager: ConfigurationManager):
        config = config_manager.get_config("cltl.leolani.idresolution")
        speaker_topic = config.get("topic_speaker")
        knowledge_topic = config.get("topic_knowledge")
        match_cases = "match_cases" in config and config.get_boolean("match_cases")

        return cls(speaker_topic, knowledge_topic, match_cases,
                   friend_store, emissor_client, event_bus, resource_manager)

    def __init__(self, speaker_topic: str, knowledge_topic: str, match_cases: bool,
                 friend_store: FriendStore, emissor_client: EmissorDataClient,
                 event_bus: EventBus, resource_manager: ResourceManager):
        self._event_bus = event_bus
        self._resource_manager = resource_manager

        self._speaker_topic = speaker_topic
        self._knowledge_topic = knowledge_topic

        self._match_cases = match_cases

        self._topic_worker = None

        self._emissor_client = emissor_client
        self._friend_store = friend_store
        self._scenario = None

    @property
    def app(self):
        return None

    def start(self, timeout=30):
        self._topic_worker = TopicWorker([self._speaker_topic],
                                         self._event_bus, provides=[self._knowledge_topic],
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
        mention = event.payload.mentions[0]
        signal_id = mention.segment[0].container_id
        name_annotation = next(iter(filter(lambda a: a.type == "Entity", mention.annotations)))
        id_annotation = next(iter(filter(lambda a: a.type == "VectorIdentity", mention.annotations)))

        capsule = [self._same_as(signal_id, id_annotation.value, name_annotation.value.text)]
        if self._match_cases:
            capsule.append(self._case_insensitive_same_as(signal_id, id_annotation.value, name_annotation.value.text))
        capsule = list(filter(None, capsule))

        if capsule:
            self._event_bus.publish(self._knowledge_topic, Event.for_payload(capsule))
            logger.info("Resolved identity %s to name %s (%s)",
                        id_annotation.value, name_annotation.value.text, capsule)
        else:
            logger.info("No identity resolution for %s with name %s", id_annotation.value, name_annotation.value.text)

    def _same_as(self, signal_id, id, speaker_name):
        if id == speaker_name:
            return None

        name_uri, _ = self._friend_store.get_friend(speaker_name)

        # Storing new ID happens in parallel
        id_uri, attempt = None, 0
        while not id_uri and attempt < 50:
            id_uri, _ = self._friend_store.get_friend(id)
            time.sleep(0.5 if not id_uri else 0.0)
            attempt += 1

        logger.debug("Found uri %s for id %s in attempt %s, name_uri %s", id_uri, id, attempt, name_uri)

        if not id_uri or not name_uri or name_uri == id_uri:
            return None

        scenario_id = self._emissor_client.get_current_scenario_id()

        capsule = {
            "chat": scenario_id,
            "turn": signal_id,
            "author": {"label": "Leolani", "type": ["robot"],
                       'uri': "http://cltl.nl/leolani/world/leolani"},
            "utterance": "",
            "utterance_type": UtteranceType.STATEMENT,
            "position": "",
            "subject": {"label": speaker_name, "type": ["person"],
                        'uri': id_uri},
            "predicate": {"label": None, "uri": "http://www.w3.org/2002/07/owl#sameAs"},
            "object": {"label": speaker_name, "type": ["person"],
                       'uri': name_uri},
            "perspective": {"certainty": 1, "polarity": 0, "sentiment": 0},
            "timestamp": timestamp_now(),
            "context_id": scenario_id
        }

        return capsule

    def _case_insensitive_same_as(self, signal_id, id, speaker_name):
        # Storing new ID happens in parallel
        id_uri, attempt = None, 0
        while not id_uri and attempt < 50:
            id_uri, _ = self._friend_store.get_friend(id)
            time.sleep(0.5 if not id_uri else 0.0)
            attempt += 1

        logger.debug("Found uri %s for id %s in attempt %s for case matching", id_uri, id, attempt)

        if not id_uri or id_uri.split('/')[-1].lower().startswith(speaker_name.lower()):
            logger.debug("Not matching uri %s for speaker %s", id_uri, speaker_name)
            return None

        scenario_id = self._emissor_client.get_current_scenario_id()

        uri_parts = id_uri.split('/')
        title_uri = '/'.join(uri_parts[:-1]) + '/' + uri_parts[-1].title()
        lower_case_uri = '/'.join(uri_parts[:-1]) + '/' + uri_parts[-1].lower()

        logger.debug("Matched uri %s and %s", title_uri, lower_case_uri)

        capsule = {
            "chat": scenario_id,
            "turn": signal_id,
            "author": {"label": "Leolani", "type": ["robot"],
                       'uri': "http://cltl.nl/leolani/world/leolani"},
            "utterance": "",
            "utterance_type": UtteranceType.STATEMENT,
            "position": "",
            "subject": {"label": speaker_name, "type": ["person"],
                        'uri': title_uri},
            "predicate": {"label": None, "uri": "http://www.w3.org/2002/07/owl#sameAs"},
            "object": {"label": speaker_name, "type": ["person"],
                       'uri': lower_case_uri},
            "perspective": {"certainty": 1, "polarity": 0, "sentiment": 0},
            "timestamp": timestamp_now(),
            "context_id": scenario_id
        }

        return capsule