import logging
import Queue
import time

import gevent
import memcache
import simplejson as json

from datetime import datetime, timedelta
from heapq import heappush, heappop

from gevent.queue import Empty, Queue as GeventQueue

from pyramid.threadlocal import get_current_registry
from pyramid_sockjs import get_session_manager
from pyramid_sockjs.session import (STATE_OPEN, STATE_CLOSING, STATE_CLOSED, Session)

log = logging.getLogger('gecoscc.socks')


def get_manager(request):
    return get_session_manager('sockjs', request.registry)


def invalidate_change(request, schema_detail, objtype, objnew, objold):
    manager = get_manager(request)
    manager.broadcast(json.dumps({
        'action': 'change',
        'object': schema_detail().serialize(objnew)
    }))


def invalidate_delete(request, schema_detail, objtype, obj):
    manager = get_manager(request)
    manager.broadcast(json.dumps({
        'action': 'delete',
        'object': schema_detail().serialize(obj)
    }))


def invalidate_jobs(request):
    manager = get_manager(request)
    manager.broadcast(json.dumps({
        'action': 'jobs',
        'object': None
    }))


# See http://stackoverflow.com/questions/9539052/python-dynamically-changing-base-classes-at-runtime-how-to
# Don't inherit client from threading.local so that we can reuse clients in
# different threads
memcache.Client = type('Client', (object,), dict(memcache.Client.__dict__))
# Client.__init__ references local, so need to replace that, too


class Local(object):
    pass

memcache.local = Local


class PoolClient(object):
    '''Pool of memcache clients that has the same API as memcache.Client'''
    def __init__(self, pool_size=10, pool_timeout=3, *args, **kwargs):
        self.pool_timeout = pool_timeout
        self.queue = Queue.Queue()
        for _i in range(pool_size):
            self.queue.put(memcache.Client(*args, **kwargs))

    def __getattr__(self, name):
        return lambda *args, **kw: self._call_client_method(name, *args, **kw)

    def _call_client_method(self, name, *args, **kwargs):
        try:
            client = self.queue.get(timeout=self.pool_timeout)
        except Queue.Empty:
            return

        try:
            return getattr(client, name)(*args, **kwargs)
        finally:
            self.queue.put(client)


class QueueMemCached(GeventQueue):

    def __init__(self, session_id, mc=None, *args, **kwargs):
        super(QueueMemCached, self).__init__(*args, **kwargs)
        self.session_id = session_id
        settings = get_current_registry().settings
        self.mc = mc or PoolClient(10, 3, [settings['session.memcache_backend']], debug=0)

    def put_nowait(self, item):
        sessions = self.mc.get('sessions')
        session = sessions[self.session_id]
        if 'queue' not in session:
            session['queue'] = []
        queue = session['queue']
        queue.append(item)
        self.mc.set('sessions', sessions)

    def get(self, block=True, timeout=timedelta(seconds=5)):
        if block:
            time.sleep(timeout)
        sessions = self.mc.get('sessions')
        session = sessions[self.session_id]
        queue = session.get('queue', [])
        try:
            item = queue.pop()
            self.mc.set('sessions', sessions)
            return item
        except IndexError:
            raise Empty


class SessionMemCached(Session):
    """ SockJS session object

    ``state``: Session state

    ``manager``: Session manager that hold this session

    ``request``: Request object

    ``registry``: Pyramid component registry

    ``acquierd``: Acquired state, indicates that transport is using session

    ``timeout``: Session timeout

    """

    def __init__(self, id, timeout=timedelta(seconds=10), request=None, mc=None):
        self.id = id
        self.name = 'sockjs'
        self.request = request
        self.registry = getattr(request, 'registry', None)
        settings = get_current_registry().settings
        self.queue = QueueMemCached(id, mc=mc)
        self.mc = mc or PoolClient(10, 3, [settings['session.memcache_backend']], debug=0)

        self.expired = False
        self.timeout = timeout
        self.expires = datetime.now() + timeout
        self.hits = 0
        self.heartbeats = 0

    def get_attr_of_memcached(self, attr):
        sessions = self.mc.get('sessions')
        session = sessions[self.id]
        return session[attr]

    def set_attr_of_memcached(self, attr, value):
        sessions = self.mc.get('sessions')
        session = sessions[self.id]
        session[attr] = value
        return self.mc.set('sessions', sessions)

    # @property
    # def expired(self):
        # return self.get_attr_of_memcached('expired')

    # @expired.setter
    # def expired(self, value):
        # return self.set_attr_of_memcached('expired', value)

    # @property
    # def expires(self):
        # return self.get_attr_of_memcached('expires')

    # @expires.setter
    # def expires(self, value):
        # return self.set_attr_of_memcached('expires', value)

    # @property
    # def heartbeats(self):
        # return self.get_attr_of_memcached('heartbeats')

    # @heartbeats.setter
    # def heartbeats(self, value):
        # return self.set_attr_of_memcached('heartbeats', value)

    # @property
    # def hits(self):
        # return self.get_attr_of_memcached('hits')

    # @hits.setter
    # def hits(self, value):
        # return self.set_attr_of_memcached('hits', value)

    # @property
    # def timeout(self):
        # return self.get_attr_of_memcached('timeout')

    # @timeout.setter
    # def timeout(self, value):
        # return self.set_attr_of_memcached('timeout', value)

    def serialize(self):
        return {'hits': self.hits,
                'heartbeats': self.heartbeats,
                'expires': self.expires,
                'timeout': self.timeout,
                'expired': self.expired,
                'id': self.id}

    @classmethod
    def deserialize(self, value, manager, registry, request=None):
        session = SessionMemCached(id=value['id'],
                                   timeout=value['timeout'],
                                   request=request,
                                   mc=manager.mc)
        session.manager = manager
        session.registry = registry
        return session

_marker = object()


class SessionMemCachedManager(object):
    """ A basic session manager """

    factory = SessionMemCached

    _gc_thread = None
    _gc_thread_stop = False

    def __init__(self, name, registry, session=None,
                 gc_cycle=10.0, timeout=timedelta(seconds=10)):
        self.name = name
        self.route_name = 'sockjs-url-%s' % name
        self.registry = registry
        if session is not None:
            self.factory = session
        settings = registry.settings
        self.mc = PoolClient(10, 3, [settings['session.memcache_backend']], debug=0)

        self.acquired = {}
        self.pool = []
        self.sessions = {}
        self.timeout = timeout
        debug = registry.settings.get('debug_sockjs', '').lower()
        self.debug = debug in ('true', 't', 'yes')
        self._gc_cycle = gc_cycle

    @property
    def acquired(self):
        return self.mc.get('acquired')

    @acquired.setter
    def acquired(self, value):
        return self.mc.set('acquired', value, 0)

    @property
    def pool(self):
        return self.mc.get('pool')

    @pool.setter
    def pool(self, value):
        success = self.mc.set('pool', value, 0)
        return success

    @property
    def sessions(self):
        return self.mc.get('sessions')

    @sessions.setter
    def sessions(self, value):
        return self.mc.set('sessions', value, 0)

    def add_session(self, session_id, session):
        sessions = self.mc.get('sessions')
        sessions[session_id] = session.serialize()
        self.sessions = sessions

    def remove_session(self, session_id):
        sessions = self.mc.get('sessions')
        del sessions[session_id]
        self.sessions = sessions

    def add_acquired(self, session_id, acquired_value):
        acquired = self.mc.get('acquired')
        acquired[session_id] = acquired_value
        self.acquired = acquired

    def remove_acquired(self, session_id):
        acquired = self.mc.get('acquired')
        del acquired[session_id]
        self.acquired = acquired

    def add_session_in_pool(self, session):
        pool = self.pool
        heappush(pool, (session.expires, session.serialize()))
        self.pool = pool

    def pop_session_pool(self, index):
        pool = self.pool
        item = pool.pop(index)
        self.pool = pool
        return item

    def route_url(self, request):
        return request.route_url(self.route_name)

    def start(self):
        if self._gc_thread is None:
            def _gc_sessions():
                while not self._gc_thread_stop:
                    gevent.sleep(self._gc_cycle)
                    self._gc()  # pragma: no cover
            self._gc_thread = gevent.Greenlet(_gc_sessions)

        if not self._gc_thread:
            self._gc_thread.start()

    def stop(self):
        if self._gc_thread:
            self._gc_thread_stop = True
            self._gc_thread.join()

    def _gc(self):
        current_time = datetime.now()
        pool = self.pool
        while pool:
            expires, session_value = pool[0]
            session = SessionMemCached.deserialize(session_value,
                                                   self, self.registry)
            # check if session is removed
            if session.id in self.sessions:
                if expires > current_time:
                    break
            else:
                self.pop_session_pool(0)
                continue

            expires, session_value = self.pop_session_pool(0)
            session = SessionMemCached.deserialize(session_value,
                                                   self, self.registry)

            # Session is to be GC'd immedietely
            if session.expires < current_time:
                if not self.on_session_gc(session):
                    self.remove_session(session.id)
                    if session.id in self.acquired:
                        self.remove_acquired(session.id)
                    if session.state == STATE_OPEN:
                        session.close()
                    if session.state == STATE_CLOSING:
                        session.closed()
                continue
            self.add_session_in_pool(session)

    def on_session_gc(self, session):
        return session.on_remove()

    def _add(self, session):
        if session.expired:
            raise ValueError("Can't add expired session")

        session.manager = self
        session.registry = self.registry
        self.add_session(session.id, session)
        self.add_session_in_pool(session)

    def get(self, id, create=False, request=None, default=_marker):
        session = self.sessions.get(id, None)
        if session is None:
            if create:
                session = self.factory(id, self.timeout, request=request)
                self._add(session)
            else:
                if default is not _marker:
                    return default
                raise KeyError(id)

        return session

    def acquire(self, session, request=None):
        sid = session.id
        if sid in self.acquired:
            raise KeyError("Another connection still open")
        if sid not in self.sessions:
            raise KeyError("Unknown session")

        session.tick()
        session.hits += 1
        session.manager = self
        if request is not None:
            session.request = request

        self.acquired[sid] = True
        return session

    def is_acquired(self, session):
        return session.id in self.acquired

    def release(self, session):
        if session.id in self.acquired:
            self.remove_acquired(session.id)

    def active_sessions(self):
        for session in self.values():
            if not session.expired:
                yield session

    def clear(self):
        """ Manually expire all sessions in the pool. """
        while self.pool:
            expr, session = heappop(self.pool)
            if session.state != STATE_CLOSED:
                session.closed()
            self.remove_session(session.id)

        self.sessions.flush_all()
        self.pool.flush_all()
        self.acquired.flush_all()

    def broadcast(self, *args, **kw):
        sessions = self.sessions
        for session_value in sessions.values():
            session = SessionMemCached.deserialize(session_value,
                                                   self, self.registry)

            if not session.expired:
                session.send(*args, **kw)

    def __del__(self):
        self.clear()
        self.stop()
