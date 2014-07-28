import logging
import gevent
import simplejson as json
import time

from datetime import datetime, timedelta
from pyramid_sockjs import get_session_manager
from gevent.queue import Empty, Queue
from heapq import heappush, heappop

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


STATE_NEW = 0
STATE_OPEN = 1
STATE_CLOSING = 2
STATE_CLOSED = 3


class QueueMemCached(Queue):

    def __init__(self, session_id, *args, **kwargs):
        super(QueueMemCached, self).__init__(*args, **kwargs)
        self.session_id = session_id
        import memcache
        self.mc = memcache.Client(['127.0.0.1:11211'], debug=0)

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


class SessionMemCached(object):
    """ SockJS session object

    ``state``: Session state

    ``manager``: Session manager that hold this session

    ``request``: Request object

    ``registry``: Pyramid component registry

    ``acquierd``: Acquired state, indicates that transport is using session

    ``timeout``: Session timeout

    """

    manager = None
    request = None
    registry = None
    acquired = False
    timeout = timedelta(seconds=10)
    state = STATE_NEW

    def __init__(self, id, timeout=timedelta(seconds=10), request=None):
        self.id = id
        self.expired = False
        self.timeout = timeout
        self.request = request
        self.registry = getattr(request, 'registry', None)
        self.expires = datetime.now() + timeout

        self.queue = QueueMemCached(id)

        self.hits = 0
        self.heartbeats = 0
        self.name = 'sockjs'

    def __str__(self):
        result = ['id=%r' % (self.id,)]

        if self.state == STATE_OPEN:
            result.append('connected')
        else:
            result.append('disconnected')

        if self.queue.qsize():
            result.append('queue[%s]' % self.queue.qsize())
        if self.hits:
            result.append('hits=%s' % self.hits)
        if self.heartbeats:
            result.append('heartbeats=%s' % self.heartbeats)

        return ' '.join(result)

    def tick(self, timeout=None):
        self.expired = False

        if timeout is None:
            self.expires = datetime.now() + self.timeout
        else:
            self.expires = datetime.now() + timeout

    def heartbeat(self):
        self.heartbeats += 1

    def expire(self):
        """ Manually expire a session. """
        self.expired = True

    def open(self):
        log.debug('open session: %s', self.id)
        self.state = STATE_OPEN
        try:
            self.on_open()
        except:
            log.exception("Exceptin in .on_open method.")

    def close(self):
        """ close session """
        log.debug('close session: %s', self.id)
        self.state = STATE_CLOSING
        try:
            self.on_close()
        except:
            log.exception("Exceptin in .on_close method.")

    def closed(self):
        log.debug('session closed: %s', self.id)
        self.state = STATE_CLOSED
        self.release()
        self.expire()
        try:
            self.on_closed()
        except:
            log.exception("Exceptin in .on_closed method.")

    def acquire(self, request=None):
        self.manager.acquire(self, request)

    def release(self):
        if self.manager is not None:
            self.manager.release(self)

    def get_transport_message(self, block=True, timeout=None):
        self.tick()
        return self.queue.get(block=block, timeout=timeout)

    def send(self, msg):
        """ send message to client """
        if self.manager.debug:
            log.debug('outgoing message: %s, %s', self.id, str(msg)[:200])

        self.tick()
        self.queue.put_nowait(msg)

    def message(self, msg):
        log.debug('incoming message: %s, %s', self.id, msg[:200])
        self.tick()
        try:
            self.on_message(msg)
        except:
            log.exception("Exceptin in .on_message method.")

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
                                   request=request)
        session.manager = manager
        session.registry = registry
        return session

    def on_open(self):
        """ override in subsclass """

    def on_message(self, msg):
        """ executes when new message is received from client """

    def on_close(self):
        """ executes after session marked as closing """

    def on_closed(self):
        """ executes after session marked as closed """

    def on_remove(self):
        """ executes before removing from session manager """


_marker = object()


class SessionMemCachedManager(object):
    """ A basic session manager """

    factory = SessionMemCached

    _gc_thread = None
    _gc_thread_stop = False

    def __init__(self, name, registry, session=None,
                 gc_cycle=5.0, timeout=timedelta(seconds=10)):
        self.name = name
        self.route_name = 'sockjs-url-%s' % name
        self.registry = registry
        if session is not None:
            self.factory = session
        import memcache
        self.mc = memcache.Client(['127.0.0.1:11211'], debug=0)

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
        return self.mc.set('acquired', value)

    @property
    def pool(self):
        return self.mc.get('pool')

    @pool.setter
    def pool(self, value):
        return self.mc.set('pool', value)

    @property
    def sessions(self):
        return self.mc.get('sessions')

    @sessions.setter
    def sessions(self, value):
        return self.mc.set('sessions', value)

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

    def route_url(self, request):
        return request.route_url(self.route_name)

    def start(self):
        if self._gc_thread is None:
            def _gc_sessions():
                while not self._gc_thread_stop:
                    gevent.sleep(self._gc_cycle)
                    # TODO: Uncomment _gc
                    # self._gc() # pragma: no cover

            self._gc_thread = gevent.Greenlet(_gc_sessions)

        if not self._gc_thread:
            self._gc_thread.start()

    def stop(self):
        if self._gc_thread:
            self._gc_thread_stop = True
            self._gc_thread.join()

    def _gc(self):
        current_time = datetime.now()
        while self.pool:
            expires, session = self.pool[0]

            # check if session is removed
            if session.id in self.sessions:
                if expires > current_time:
                    break
            else:
                self.pool.pop(0)
                continue

            expires, session = self.pool.pop(0)

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
            # TODO: This does not work still
            heappush(self.pool, (session.expires, session))

    def on_session_gc(self, session):
        return session.on_remove()

    def _add(self, session):
        if session.expired:
            raise ValueError("Can't add expired session")

        session.manager = self
        session.registry = self.registry
        self.add_session(session.id, session)
        # TODO: This does not work still
        heappush(self.pool, (session.expires, session))

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
            raise KeyError("Another connection still open2")
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

    def broadcast(self, *args, **kw):
        sessions = self.sessions
        for session_value in sessions.values():
            session = SessionMemCached.deserialize(session_value,
                                                   self, self.registry)

            if not session.expired:
                session.send(*args, **kw)

    def __del__(self):
        self.sessions.flush_all()
        self.stop()
