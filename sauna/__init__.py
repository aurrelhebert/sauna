from collections import namedtuple
import threading
import queue
import logging
import time
import socket
import os
import textwrap
import signal
import importlib
import pkgutil


from sauna import plugins, consumers
from sauna.plugins.base import Check
from sauna.consumers.base import QueuedConsumer
from sauna.consumers import ConsumerRegister
from sauna.plugins import PluginRegister

__version__ = '0.0.4'

ServiceCheck = namedtuple('ServiceCheck',
                          ['timestamp', 'hostname', 'name',
                           'status', 'output'])

# Global dict containing the last status of each check
# Needs to hold a lock to access it
check_results = {}
check_results_lock = threading.Lock()

try:
    # In Python 3.2 threading.Event is a factory function
    # the real class lives in threading._Event
    event_type = threading._Event
except AttributeError:
    event_type = threading.Event


class DependencyError(Exception):

    def __init__(self, plugin, dep_name, pypi='', deb=''):
        self.msg = '{} depends on {}. It can be installed with:\n'.format(
            plugin, dep_name
        )
        if pypi:
            self.msg = '{}    pip install {}\n'.format(self.msg, pypi)
        if deb:
            self.msg = '{}    apt-get install {}\n'.format(self.msg, deb)

    def __str__(self):
        return self.msg


def read_config(config_file):
    # importing yaml here because dependency is not installed
    # when fetching __version__ from setup.py
    import yaml

    try:
        with open(config_file) as f:
            return yaml.safe_load(f)
    except OSError as e:
        print('Cannot read configuration file {}: {}'
              .format(config_file, e))
        exit(1)


class Sauna:

    def __init__(self, config=None):
        if config is None:
            config = {}
        self.config = config

        self.must_stop = threading.Event()
        self._consumers_queues = []

    @classmethod
    def assemble_config_sample(cls, path):
        sample = '---\nperiodicity: 120\nhostname: node-1.domain.tld\n'

        sample += '\nconsumers:\n'
        consumers_sample = ''
        for _, consumer_info in ConsumerRegister.all_consumers.items():
            if hasattr(consumer_info['consumer_cls'], 'config_sample'):
                consumers_sample += textwrap.dedent(
                    consumer_info['consumer_cls'].config_sample()
                )
        sample += consumers_sample.replace('\n', '\n  ')

        sample += '\nplugins:\n'
        plugins_sample = ''
        for _, plugin_info in PluginRegister.all_plugins.items():
            if hasattr(plugin_info['plugin_cls'], 'config_sample'):
                plugins_sample += textwrap.dedent(
                    plugin_info['plugin_cls'].config_sample()
                )
        sample += plugins_sample.replace('\n', '\n  ')

        file_path = os.path.join(path, 'sauna-sample.yml')
        with open(file_path, 'w') as f:
            f.write(sample)

        return file_path

    @property
    def hostname(self):
        return self.config.get('hostname', socket.getfqdn())

    @property
    def periodicity(self):
        return self.config.get('periodicity', 120)

    def get_active_checks_name(self):
        checks = self.get_all_active_checks()
        return [check.name for check in checks]

    def get_all_available_consumers(self):
        return_consumers = []
        for plugin_name, _ in ConsumerRegister.all_consumers.items():
            return_consumers.append(plugin_name)

        return return_consumers

    def get_all_available_checks(self):
        checks = {}
        for plugin_name, data in PluginRegister.all_plugins.items():
            checks[plugin_name] = []
            for check in data['checks']:
                checks[plugin_name].append(check)
        return checks

    def get_all_active_checks(self):
        checks = []
        deps_error = []
        for plugin_name, plugin_data in self.config['plugins'].items():

            # Load plugin
            plugin_info = PluginRegister.get_plugin(plugin_name)
            if not plugin_info:
                print('Plugin {} does not exist'.format(plugin_name))
                exit(1)

            # Configure plugin
            try:
                plugin = plugin_info['plugin_cls'](
                    plugin_data.get('config', {})
                )
            except DependencyError as e:
                deps_error.append(str(e))
                continue

            # Launch plugin checks
            for check in plugin_data['checks']:
                check_func = getattr(plugin, check['type'])

                if not check_func:
                    print('Unknown check {} on plugin {}'.format(check['type'],
                                                                 plugin_name))
                    exit(1)

                check_name = (check.get('name') or '{}_{}'.format(
                    plugin_name, check['type']
                ).lower())

                checks.append(Check(check_name, check_func, check))
        if deps_error:
            for error in deps_error:
                print(error)
            exit(1)
        return checks

    def launch_all_checks(self, hostname):
        for check in self.get_all_active_checks():

            try:
                status, output = check.run_check()
            except Exception as e:
                logging.warning('Could not run check {}: {}'.format(
                    check.name, str(e)
                ))
                status = 3
                output = str(e)
            s = ServiceCheck(
                timestamp=int(time.time()),
                hostname=hostname,
                name=check.name,
                status=status,
                output=output
            )
            yield s

    def run_producer(self):
        while True:
            for service_check in self.launch_all_checks(self.hostname):
                logging.debug(
                    'Pushing to main queue: {}'.format(service_check))
                self.send_data_to_consumers(service_check)
                with check_results_lock:
                    check_results[service_check.name] = service_check
            if self.must_stop.wait(timeout=self.periodicity):
                break
        logging.debug('Exited producer thread')

    def term_handler(self, *args):
        """Notify producer and consumer that they should stop."""
        if not self.must_stop.is_set():
            self.must_stop.set()
            self.send_data_to_consumers(self.must_stop)
            logging.info('Exiting...')

    def send_data_to_consumers(self, data):
        for queue in self._consumers_queues:
            queue.put(data)

    def launch(self):
        # Start producer and consumer threads
        producer = threading.Thread(
            name='producer', target=self.run_producer
        )
        producer.start()

        consumers_threads = []
        for consumer_name, consumer_config in self.config['consumers'].items():

            consumer_info = ConsumerRegister.get_consumer(consumer_name)
            if not consumer_info:
                print('Plugin {} does not exist'.format(consumer_name))
                exit(1)

            try:
                consumer = consumer_info['consumer_cls'](consumer_config)
            except DependencyError as e:
                print(str(e))
                exit(1)

            if isinstance(consumer, QueuedConsumer):
                consumer_queue = queue.Queue()
                self._consumers_queues.append(consumer_queue)
            else:
                consumer_queue = None

            consumer_thread = threading.Thread(
                name='consumer_{}'.format(consumer_name),
                target=consumer.run,
                args=(self.must_stop, consumer_queue)
            )

            consumer_thread.start()
            consumers_threads.append(consumer_thread)
            logging.debug(
                'Running {} with {}'.format(consumer_name, consumer_config)
            )

        signal.signal(signal.SIGTERM, self.term_handler)
        signal.signal(signal.SIGINT, self.term_handler)

        producer.join()
        self.term_handler()

        for consumer_thread in consumers_threads:
            consumer_thread.join()

        logging.debug('Exited main thread')


def get_all_subclass(main_class):
    # Check all subclass with recursion
    subclasses = []
    for subclass in main_class.__subclasses__():
        subclasses.append(subclass)
        subclasses += get_all_subclass(subclass)
    return tuple(subclasses)


def import_submodules(package):
    # Please don't use logger in this method,
    # it will disable all logger parameters
    if isinstance(package, str):
        package = importlib.import_module(package)

    for loader, name, is_pkg in pkgutil.walk_packages(package.__path__):
        if not name.startswith("_") and is_pkg is False:
            full_name = package.__name__ + '.' + name
            importlib.import_module(full_name)

import_submodules(__name__+'.plugins.ext')
import_submodules(__name__+'.consumers.ext')
