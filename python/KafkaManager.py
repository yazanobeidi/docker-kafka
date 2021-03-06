import subprocess
from pipes import quote
from shlex import split as shlexsplit
from configparser import ConfigParser
from os import environ, path, makedirs
from pykafka import KafkaClient
from pykafka.utils.compat import get_bytes
from json import dumps, loads
from time import sleep
from shutil import copyfile
import logging

__author__ = 'yazan'
__licence__ = 'Apache V2'

class KafkaManager(object):
    """Convenience interface to facilitate CLI interface with the host of a
    running kafka server when kafka is packaged within a docker container.
    Since $KAFKA_HOME/bin scripts can only be run on the host, this class
    will constantly listen to topic <kafka-manager-in> and respond to requests
    such as creating topics, and so forth to topic <kafka-manager-out>. All 
    subscribers to these two topics receive all published messages and are
    able to relate requests between the two topics through transaction IDs.
    """
    def __init__(self, config, log):
        self.config = config
        self.log = log
        self.kafka_home = environ.get(self.config.get('kafka', 'home'))
        self.scripts = dict(self.config.items('kafka-cli'))
        self.zkpr = self.config.get('zookeeper', 'gateway')
        self.consumer_grp = bytes(self.config.get('kafka', 'default_consumer_group'))
        self.kafka_host = self.config.get('kafka', 'gateway')
        self.num_brokers = 1 # we start with 1 broker by default
        self.log.info("Connecting to kafka ({})".format(self.kafka_host))
        self.client = KafkaClient(hosts=self.kafka_host)
        # Create dictionary of possible request functions - layer of protection
        # against running unverified commands on the host 
        self.func_dict = {'list_topics': self.list_topics,
                          'is_topic': self.is_topic,
                          'make_topic': self.make_topic,
                          'delete_topic': self.delete_topic}
        self.log.info("Creating <kafka-manager> topics")
        self.make_topic(topic='kafka-manager-in')
        self.make_topic(topic='kafka-manager-out')
        if self.config.getboolean('kafka', 'create_debug_topic'):
            self.log.debug("Creating topic <debug>")
            self.make_topic(topic='debug')

    def health_check(self):
        """Idea, every 30 min run thru tests to ensure cluster is healthy"""
        raise NotImplementedError

    def poll(self):
        """Main runtime of KafkaManager - handle incoming requests."""
        # Get topic handles
        in_topic = self.client.topics['kafka-manager-in']
        out_topic = self.client.topics['kafka-manager-out']
        # Subscribe consumer
        balanced_consumer = in_topic.get_balanced_consumer(
                                        consumer_group=self.consumer_grp,
                                        auto_commit_enable=True,
                                        zookeeper_connect=self.zkpr)
        # Continuously poll
        self.log.info("Polling ...")
        with out_topic.get_producer() as producer:
            while True:
                for message in balanced_consumer:
                    count = 0
                    if message is not None:
                        self.log.info("Found msg <{}> @ offset {}".format
                                                (message.value,message.offset))
                        contents = loads(message.value)
                        function = contents['function']
                        kwargs = contents['kwargs']
                        _id = contents['id']
                        self.log.info('Checking ..')
                        if function in self.func_dict:
                            # Execute script with supplied key word args
                            output = self.func_dict[function](**kwargs)
                            # Post response
                            response = dumps({'function': function,
                                              'kwargs': dumps(kwargs),
                                              'output': output,
                                              'id': _id})
                            count += 1
                            self.log.info('Submitting response: {}'.format(response))
                            producer.produce(response, partition_key=str(count))
                        else:
                            self.log.info("Function {} not found".format(function))
                    else:
                        self.log.info("Found None msg")

    def _get_sh(self, name):
        """Returns shell script by string matching name."""
        if name in self.scripts:
            return path.join(self.kafka_home, self.scripts[name])
        else:
            self.log.info("{} script not found".format(name))
            return None

    def _run_sh(self, script, args):
        """Run kafka-topics.sh with the provided list of arguments.
           We quote(cmd) for safety.
        """
        script = self._get_sh(script)
        cmd = [script, '--zookeeper', self.zkpr] + args # might need to change
        cmd = ' '.join([str(c) for c in cmd]) # cmd needs to be str
        self.log.info("running: {}".format(cmd))
        return subprocess.check_output(shlexsplit(quote(cmd).replace("'", '')))

    def list_topics(self):
        """Returns string formatted newline separated list of topics"""
        args = ['--list']
        response = self._run_sh('topics_sh', args)
        return response.strip().split(b'\n')

    def is_topic(self, topic):
        """Check if topic exists"""
        return topic in self.list_topics()

    def make_topic(self, 
                   topic, 
                   partitions=3, 
                   replication=1):
        """Use kafka-topics.sh to create a topic."""
        if self.is_topic(topic):
            self.log.info("{} topic already exists".format(topic))
            return 'already_exists'
        else:
            self.log.info("Creating topic {}".format(topic))
            args = ['--create',
                   '--topic', get_bytes(topic),
                   '--partitions', partitions,
                   '--replication-factor', replication]
            self._run_sh('topics_sh', args)
            sleep(0.5)
            if self.is_topic(topic):
                self.log.info('Topic {} created'.format(topic))
                return 'created'

    def delete_topic(self, topic):
        """Delete single topic by name"""
        self.log.info("Deleting topic {}".format(topic))
        args = ['--delete', '--topic', topic]
        self._run_sh('topics_sh', args)
        return 'deleted'

    def add_broker(self):
        """Adds a new broker to the cluster"""
        i = self.num_brokers
        self.log.info('Adding broker for total of {}'.format(i))
        server_properties = path.join(self.kafka_home, 'config/server.properties')
        new_server_properties = '{}-{}'.format(server_properties, i)
        copyfile(server_properties, new_server_properties)
        cmds = ['sed -r -i "s/(broker.id)=(.*)/\1={}/g"'.format(i),
                'sed -r -i "s/#(listeners=PLAINTEXT:\/\/:)(.*)/\1={}/g"'.format(str(9092+i)),
                'sed -r -i "s/(log.dirs)=(.*)/\1=\/tmp\/kafka-logs-{}/g"'.format(i),
                'sed -r -i "s/#(delete.topic.enable)=(.*)/\1=$DELETE_TOPIC_ENABLE/g"']
        for cmd in cmds:
            cmd = ' '.join([cmd, new_server_properties])
            self.log.debug('Running cmd {} ...'.format(cmd))
            subprocess.check_output(shlexsplit(quote(cmd).replace("'", '')))
        start_kafka = '$KAFKA_HOME/bin/kafka-server-start.sh {}'.format(new_server_properties)
        self.log.info('Initializing broker ... ')
        subprocess.check_output(shlexsplit(quote(start_kafka).replace("'", '')))
        self.log.info('New broker (id:{}) successfully added'.format(i))
        self.num_brokers += 1

    def remove_broker(self, id):
        """Kill broker from the cluster by ID"""
        raise NotImplementedError


def start_logger(logs):
    """Logging boilerplate"""
    # Create logging directory if it does not exist:
    if not path.exists(path.dirname(logs)):
        makedirs(path.dirname(logs))
    # Logging boilerplate
    logger = logging.getLogger('KafkaManager')
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(logs, mode='a+')
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s '\
                                                            '- %(message)s')
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


if __name__ == "__main__":
    config = ConfigParser(environ)
    conf = path.join(environ.get('KAFKA_MANAGER_CONFIG_DIR'), 'config.cfg')
    config.read(conf)
    log = start_logger(config.get('log', 'path_to_log'))
    log.info('Initializing KafkaManager')
    # Initialize
    try:
        kafka_manager = KafkaManager(config, log)
    except:
        log.exception("Exception in KafkaManager() Initialization")
        raise
    # Start polling to handle requests
    try:
        kafka_manager.poll()
    except:
        log.exception('Exception during poll()')
        raise
    log.info('KafkaManager exit.')