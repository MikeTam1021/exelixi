#!/usr/bin/env python
# encoding: utf-8

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# author: Paco Nathan
# https://github.com/ceteri/exelixi


from contextlib import contextmanager
from gevent import monkey, shutdown, signal, spawn, wsgi, Greenlet
from gevent.event import Event
from gevent.queue import JoinableQueue
from hashring import HashRing
from json import dumps, loads
from signal import SIGQUIT
from util import instantiate_class, post_distrib_rest
from uuid import uuid1
import logging
import sys


######################################################################
## class definitions

class Worker (object):
    # http://www.gevent.org/gevent.wsgi.html
    # http://toastdriven.com/blog/2011/jul/31/gevent-long-polling-you/
    # http://blog.pythonisito.com/2012/07/gevent-and-greenlets.html

    DEFAULT_PORT = "9311"


    def __init__ (self, port=DEFAULT_PORT):
        # REST services
        monkey.patch_all()
        signal(SIGQUIT, shutdown)
        self.is_config = False
        self.server = wsgi.WSGIServer(('', int(port)), self._response_handler, log=None)

        # sharding
        self.prefix = None
        self.shard_id = None
        self.ring = None

        # concurrency based on message passing / barrier pattern
        self._task_event = None
        self._task_queue = None

        # UnitOfWork
        self._uow = None


    def shard_start (self):
        """start the worker service for this shard"""
        self.server.serve_forever()


    def shard_stop (self, *args, **kwargs):
        """stop the worker service for this shard"""
        payload = args[0]

        if (self.prefix == payload["prefix"]) and (self.shard_id == payload["shard_id"]):
            logging.info("worker service stopping... you can safely ignore any exceptions that follow")
            self.server.stop()
        else:
            # returns incorrect response in this case, to avoid exception
            logging.error("incorrect shard %s prefix %s", payload["shard_id"], payload["prefix"])


    ######################################################################
    ## authentication methods

    def auth_request (self, payload, start_response, body):
        """test the authentication credentials for a REST call"""
        if (self.prefix == payload["prefix"]) and (self.shard_id == payload["shard_id"]):
            return True
        else:
            # UoW caller did not provide correct credentials to access shard
            start_response('403 Forbidden', [('Content-Type', 'text/plain')])
            body.put("Forbidden, incorrect credentials for this shard\r\n")
            body.put(StopIteration)

            logging.error("incorrect credentials shard %s prefix %s", payload["shard_id"], payload["prefix"])
            return False


    def shard_config (self, *args, **kwargs):
        """configure the service to run a shard"""
        payload, start_response, body = self.get_response_context(args)

        if self.is_config:
            # hey, somebody call security...
            start_response('403 Forbidden', [('Content-Type', 'text/plain')])
            body.put("Forbidden, shard is already in a configured state\r\n")
            body.put(StopIteration)

            logging.warning("denied configuring shard %s prefix %s", self.shard_id, self.prefix)
        else:
            self.is_config = True
            self.prefix = payload["prefix"]
            self.shard_id = payload["shard_id"]

            # dependency injection for UnitOfWork
            uow_name = payload["uow_name"]
            logging.info("initializing unit of work based on %s", uow_name)

            ff = instantiate_class(uow_name)
            self._uow = ff.instantiate_uow(uow_name, self.prefix)

            start_response('200 OK', [('Content-Type', 'text/plain')])
            body.put("Bokay\r\n")
            body.put(StopIteration)

            logging.info("configuring shard %s prefix %s", self.shard_id, self.prefix)


    ######################################################################
    ## barrier pattern methods

    @contextmanager
    def wrap_task_event (self):
        """initialize a gevent.Event, to which the UnitOfWork will wait as a listener"""
        self._task_event = Event()
        yield

        # complete the Event, notifying the UnitOfWork which waited
        self._task_event.set()
        self._task_event = None


    def _consume_task_queue (self):
        """consume/serve requests until the task_queue empties"""
        while True:
            payload = self._task_queue.get()

            try:
                self._uow.perform_task(payload)
            finally:
                self._task_queue.task_done()


    def prep_task_queue (self):
        """prepare task_queue for another set of distributed tasks"""
        self._task_queue = JoinableQueue()
        spawn(self._consume_task_queue)


    def put_task_queue (self, payload):
        """put the given task definition into the task_queue"""
        self._task_queue.put_nowait(payload)


    def queue_wait (self, *args, **kwargs):
        """wait until all shards finished sending task_queue requests"""
        payload, start_response, body = self.get_response_context(args)

        if self.auth_request(payload, start_response, body):
            if self._task_event:
                self._task_event.wait()

            # HTTP response first, then initiate long-running task
            start_response('200 OK', [('Content-Type', 'text/plain')])
            body.put("Bokay\r\n")
            body.put(StopIteration)


    def queue_join (self, *args, **kwargs):
        """join on the task_queue, as a barrier to wait until it empties"""
        payload, start_response, body = self.get_response_context(args)

        if self.auth_request(payload, start_response, body):
            start_response('200 OK', [('Content-Type', 'text/plain')])
            body.put("join queue...\r\n")

            ## NB: TODO this step of emptying out the task_queue on
            ## shards could take a while on a large run... perhaps use
            ## a long-polling HTTP request or websocket instead?
            self._task_queue.join()

            body.put("done\r\n")
            body.put(StopIteration)


    ######################################################################
    ## hash ring methods

    def ring_init (self, *args, **kwargs):
        """initialize the HashRing"""
        payload, start_response, body = self.get_response_context(args)

        if self.auth_request(payload, start_response, body):
            self.ring = payload["ring"]

            start_response('200 OK', [('Content-Type', 'text/plain')])
            body.put("Bokay\r\n")
            body.put(StopIteration)

            logging.info("setting hash ring %s", self.ring)


    ######################################################################
    ## WSGI handler for REST endpoints

    def get_response_context (self, args):
        """decode the WSGI response context from the Greenlet args"""
        env = args[0]
        msg = env["wsgi.input"].read()
        payload = loads(msg)
        start_response = args[1]
        body = args[2]

        return payload, start_response, body


    def _response_handler (self, env, start_response):
        """handle HTTP request/response"""
        uri_path = env["PATH_INFO"]
        body = JoinableQueue()

        if self._uow and self._uow.handle_endpoints(self, uri_path, env, start_response, body):
            pass

        ##########################################
        # Worker endpoints

        elif uri_path == '/shard/config':
            # configure the service to run a shard
            Greenlet(self.shard_config, env, start_response, body).start()

        elif uri_path == '/shard/stop':
            # shutdown the service
            ## NB: must parse POST data specially, to avoid exception
            payload = loads(env["wsgi.input"].read())
            Greenlet(self.shard_stop, payload).start_later(1)

            # HTTP response starts first, to avoid error after server stops
            start_response('200 OK', [('Content-Type', 'text/plain')])
            body.put("Goodbye\r\n")
            body.put(StopIteration)

        elif uri_path == '/queue/wait':
            # wait until all shards have finished sending task_queue requests
            Greenlet(self.queue_wait, env, start_response, body).start()

        elif uri_path == '/queue/join':
            # join on the task_queue, as a barrier to wait until it empties
            Greenlet(self.queue_join, env, start_response, body).start()

        elif uri_path == '/check/persist':
            ## NB: TODO checkpoint the service state to durable storage
            start_response('200 OK', [('Content-Type', 'text/plain')])
            body.put("Bokay\r\n")
            body.put(StopIteration)

        elif uri_path == '/check/recover':
            ## NB: TODO restart the service, recovering from most recent checkpoint
            start_response('200 OK', [('Content-Type', 'text/plain')])
            body.put("Bokay\r\n")
            body.put(StopIteration)

        ##########################################
        # HashRing endpoints

        elif uri_path == '/ring/init':
            # initialize the HashRing
            Greenlet(self.ring_init, env, start_response, body).start()

        elif uri_path == '/ring/add':
            ## NB: TODO add a node to the HashRing
            start_response('200 OK', [('Content-Type', 'text/plain')])
            body.put("Bokay\r\n")
            body.put(StopIteration)

        elif uri_path == '/ring/del':
            ## NB: TODO delete a node from the HashRing
            start_response('200 OK', [('Content-Type', 'text/plain')])
            body.put("Bokay\r\n")
            body.put(StopIteration)

        ##########################################
        # utility endpoints

        elif uri_path == '/':
            # dump info about the service in general
            start_response('200 OK', [('Content-Type', 'text/plain')])
            body.put(str(env) + "\r\n")
            body.put(StopIteration)

        else:
            # ne znayu
            start_response('404 Not Found', [('Content-Type', 'text/plain')])
            body.put('Not Found\r\n')
            body.put(StopIteration)

        return body


class WorkerInfo (object):
    def __init__ (self, offer, task):
        self.host = offer.hostname
        self.slave_id = offer.slave_id.value
        self.task_id = task.task_id.value
        self.executor_id = task.executor.executor_id.value
        self.ip_addr = None
        self.port = None

    def get_shard_uri (self):
        """generate a URI for this worker service"""
        return self.ip_addr + ":" + self.port


    def report (self):
        """report the slave telemetry + state"""
        return "host %s slave %s task %s exe %s ip %s:%s" % (self.host, self.slave_id, str(self.task_id), self.executor_id, self.ip_addr, self.port)


class Framework (object):
    def __init__ (self, uow_name, prefix="/tmp/exelixi"):
        """initialize the system parameters, which represent operational state"""
        self.uuid = uuid1().hex
        self.prefix = prefix + "/" + self.uuid
        logging.info("prefix: %s", self.prefix)

        # dependency injection for UnitOfWork
        self.uow_name = uow_name
        logging.info("initializing unit of work based on %s", uow_name)

        ff = instantiate_class(self.uow_name)
        self._uow = ff.instantiate_uow(self.uow_name, self.prefix)

        self._shard_assoc = None
        self._ring = None


    def _gen_shard_id (self, i, n):
        """generate a shard_id"""
        s = str(i)
        z = ''.join([ '0' for _ in xrange(len(str(n)) - len(s)) ])
        return "shard/" + z + s


    def set_worker_list (self, worker_list, exe_info=None):
        """associate shards with Executors"""
        self._shard_assoc = {}

        for i in xrange(len(worker_list)):
            shard_id = self._gen_shard_id(i, len(worker_list))

            if not exe_info:
                self._shard_assoc[shard_id] = [worker_list[i], None]
            else:
                self._shard_assoc[shard_id] = [worker_list[i], exe_info[i]]

        logging.info("shard list: %s", str(self._shard_assoc))


    def get_worker_list (self):
        """generator for the worker shards"""
        for shard_id, (shard_uri, exe_info) in self._shard_assoc.items():
            yield shard_id, shard_uri


    def get_worker_count (self):
        """count the worker shards"""
        return len(self._shard_assoc)


    def send_worker_rest (self, shard_id, shard_uri, path, base_msg):
        """access a REST endpoint on the specified shard"""
        return post_distrib_rest(self.prefix, shard_id, shard_uri, path, base_msg)


    def send_ring_rest (self, path, base_msg):
        """access a REST endpoint on each of the shards"""
        json_str = []

        for shard_id, (shard_uri, exe_info) in self._shard_assoc.items():
            lines = post_distrib_rest(self.prefix, shard_id, shard_uri, path, base_msg)
            json_str.append(lines[0])

        return json_str


    def phase_barrier (self):
        """
        implements a two-phase barrier to (1) wait until all shards
        have finished sending task_queue requests, then (2) join on
        each task_queue, to wait until it has emptied
        """
        self.send_ring_rest("queue/wait", {})
        self.send_ring_rest("queue/join", {})


    def orchestrate_uow (self):
        """orchestrate a UnitOfWork distributed across the HashRing via REST endpoints"""
        # configure the shards and the hash ring
        self.send_ring_rest("shard/config", { "uow_name": self.uow_name })

        self._ring = { shard_id: shard_uri for shard_id, (shard_uri, exe_info) in self._shard_assoc.items() }
        self.send_ring_rest("ring/init", { "ring": self._ring })

        # distribute the UnitOfWork tasks
        self._uow.orchestrate(self)

        # shutdown
        self.send_ring_rest("shard/stop", {})


class UnitOfWork (object):
    def __init__ (self, uow_name, prefix):
        self.uow_name = uow_name
        self.uow_factory = instantiate_class(uow_name)

        self.prefix = prefix

        self._shard_id = None
        self._shard_dict = None
        self._hash_ring = None


    def set_ring (self, shard_id, shard_dict):
        """initialize the HashRing"""
        self._shard_id = shard_id
        self._shard_dict = shard_dict
        self._hash_ring = HashRing(shard_dict.keys())


    def perform_task (self, payload):
        """perform a task consumed from the Worker.task_queue"""
        pass


    def orchestrate (self, framework):
        """orchestrate Workers via REST endpoints"""
        pass


    def handle_endpoints (self, worker, uri_path, env, start_response, body):
        """UnitOfWork REST endpoints"""
        pass


if __name__=='__main__':
    if len(sys.argv) < 2:
        print "usage:\n  %s <host:port> <factory>" % (sys.argv[0])
        sys.exit(1)

    shard_uri = sys.argv[1]
    uow_name = sys.argv[2]

    fra = Framework(uow_name)
    print "framework launching based on %s stored at %s..." % (fra.uow_name, fra.prefix)

    fra.set_worker_list([ shard_uri ])
    fra.orchestrate_uow()
