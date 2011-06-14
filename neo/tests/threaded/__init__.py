#
# Copyright (c) 2011 Nexedi SARL and Contributors. All Rights Reserved.
#                    Julien Muchembled <jm@nexedi.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

import os, random, socket, sys, threading, time, types
from collections import deque
from functools import wraps
from Queue import Queue, Empty
from weakref import ref as weak_ref
from mock import Mock
import transaction, ZODB
import neo.admin.app, neo.master.app, neo.storage.app
import neo.client.app, neo.neoctl.app
from neo.client import Storage
from neo.lib import bootstrap, setupLog
from neo.lib.connection import BaseConnection
from neo.lib.connector import SocketConnector, \
    ConnectorConnectionRefusedException
from neo.lib.event import EventManager
from neo.lib.protocol import CellStates, ClusterStates, NodeStates, NodeTypes
from neo.lib.util import SOCKET_CONNECTORS_DICT, parseMasterList
from neo.tests import NeoUnitTestBase, getTempDirectory, \
    ADDRESS_TYPE, IP_VERSION_FORMAT_DICT

BIND = IP_VERSION_FORMAT_DICT[ADDRESS_TYPE], 0
LOCAL_IP = socket.inet_pton(ADDRESS_TYPE, IP_VERSION_FORMAT_DICT[ADDRESS_TYPE])
SERVER_TYPE = ['master', 'storage', 'admin']
VIRTUAL_IP = [socket.inet_ntop(ADDRESS_TYPE, LOCAL_IP[:-1] + chr(2 + i))
              for i in xrange(len(SERVER_TYPE))]

def getVirtualIp(server_type):
    return VIRTUAL_IP[SERVER_TYPE.index(server_type)]


class Serialized(object):

    _global_lock = threading.Lock()
    _global_lock.acquire()
    # TODO: use something else than Queue, for inspection or editing
    #       (e.g. we'd like to suspend nodes temporarily)
    _lock_list = Queue()
    _pdb = False
    pending = 0

    @staticmethod
    def release(lock=None, wake_other=True, stop=False):
        """Suspend lock owner and resume first suspended thread"""
        if lock is None:
            lock = Serialized._global_lock
            if stop: # XXX: we should fix ClusterStates.STOPPING
                Serialized.pending = None
            else:
                Serialized.pending = 0
        try:
            sys._getframe(1).f_trace.im_self.set_continue()
            Serialized._pdb = True
        except AttributeError:
            pass
        q = Serialized._lock_list
        q.put(lock)
        if wake_other:
            q.get().release()

    @staticmethod
    def acquire(lock=None):
        """Suspend all threads except lock owner"""
        if lock is None:
            lock = Serialized._global_lock
        lock.acquire()
        if Serialized.pending is None: # XXX
            if lock is Serialized._global_lock:
                Serialized.pending = 0
            else:
                sys.exit()
        if Serialized._pdb:
            Serialized._pdb = False
            try:
                sys.stdout.write(threading.currentThread().node_name)
            except AttributeError:
                pass
            pdb(1)

    @staticmethod
    def tic(lock=None):
        # switch to another thread
        # (the following calls are not supposed to be debugged into)
        Serialized.release(lock); Serialized.acquire(lock)

    @staticmethod
    def background():
        try:
            Serialized._lock_list.get(0).release()
        except Empty:
            pass

class SerializedEventManager(Serialized, EventManager):

    _lock = None
    _timeout = 0

    @classmethod
    def decorate(cls, func):
        def decorator(*args, **kw):
            try:
                EventManager.__init__ = types.MethodType(
                    cls.__init__.im_func, None, EventManager)
                return func(*args, **kw)
            finally:
                EventManager.__init__ = types.MethodType(
                    cls._super__init__.im_func, None, EventManager)
        return wraps(func)(decorator)

    _super__init__ = EventManager.__init__.im_func

    def __init__(self):
        cls = self.__class__
        assert cls is EventManager
        self.__class__ = SerializedEventManager
        self._super__init__()

    def _poll(self, timeout=1):
        if self._pending_processing:
            assert not timeout
        elif 0 == self._timeout == timeout == Serialized.pending == len(
            self.writer_set):
            return
        else:
            if self.writer_set and Serialized.pending is not None:
                Serialized.pending = 1
            # Jump to another thread before polling, so that when a message is
            # sent on the network, one can debug immediately the receiving part.
            # XXX: Unfortunately, this means we have a useless full-cycle
            #      before the first message is sent.
            # TODO: Detect where a message is sent to jump immediately to nodes
            #       that will do something.
            self.tic(self._lock)
            if timeout != 0:
                timeout = self._timeout
                if timeout != 0 and Serialized.pending:
                    Serialized.pending = timeout = 0
        EventManager._poll(self, timeout)


class ServerNode(object):

    class __metaclass__(type):
        def __init__(cls, name, bases, d):
            type.__init__(cls, name, bases, d)
            if object not in bases and threading.Thread not in cls.__mro__:
                cls.__bases__ = bases + (threading.Thread,)

    @SerializedEventManager.decorate
    def __init__(self, cluster, address, **kw):
        self._init_args = (cluster, address), dict(kw)
        threading.Thread.__init__(self)
        self.daemon = True
        h, p = address
        self.node_type = getattr(NodeTypes,
            SERVER_TYPE[VIRTUAL_IP.index(h)].upper())
        self.node_name = '%s_%u' % (self.node_type, p)
        kw.update(getCluster=cluster.name, getBind=address,
                  getMasters=parseMasterList(cluster.master_nodes, address))
        super(ServerNode, self).__init__(Mock(kw))

    def resetNode(self):
        assert not self.isAlive()
        args, kw = self._init_args
        kw['getUUID'] = self.uuid
        self.__dict__.clear()
        self.__init__(*args, **kw)

    def start(self):
        Serialized.pending = 1
        self.em._lock = l = threading.Lock()
        l.acquire()
        Serialized.release(l, wake_other=0)
        threading.Thread.start(self)

    def run(self):
        try:
            Serialized.acquire(self.em._lock)
            super(ServerNode, self).run()
        finally:
            self._afterRun()
            neo.lib.logging.debug('stopping %r', self)
            Serialized.background()

    def _afterRun(self):
        try:
            self.listening_conn.close()
        except AttributeError:
            pass

    def getListeningAddress(self):
        try:
            return self.listening_conn.getAddress()
        except AttributeError:
            raise ConnectorConnectionRefusedException

class AdminApplication(ServerNode, neo.admin.app.Application):
    pass

class MasterApplication(ServerNode, neo.master.app.Application):
    pass

class StorageApplication(ServerNode, neo.storage.app.Application):

    def resetNode(self, clear_database=False):
        self._init_args[1]['getReset'] = clear_database
        dm = self.dm
        super(StorageApplication, self).resetNode()
        if dm and not clear_database:
            self.dm = dm

    def _afterRun(self):
        super(StorageApplication, self)._afterRun()
        try:
            self.dm.close()
            self.dm = None
        except StandardError: # AttributeError & ProgrammingError
            pass

class ClientApplication(neo.client.app.Application):

    @SerializedEventManager.decorate
    def __init__(self, cluster):
        super(ClientApplication, self).__init__(
            cluster.master_nodes, cluster.name)
        self.em._lock = threading.Lock()

    def setPoll(self, master=False):
        if master:
            self.em._timeout = 1
            if not self.em._lock.acquire(0):
                Serialized.background()
        else:
            Serialized.release(wake_other=0); Serialized.acquire()
            self.em._timeout = 0

    def __del__(self):
        try:
            super(ClientApplication, self).__del__()
        finally:
            Serialized.background()
    close = __del__

class NeoCTL(neo.neoctl.app.NeoCTL):

    @SerializedEventManager.decorate
    def __init__(self, cluster, address=(getVirtualIp('admin'), 0)):
        self._cluster = cluster
        super(NeoCTL, self).__init__(address)
        self.em._timeout = None

    server = property(lambda self: self._cluster.resolv(self._server),
                      lambda self, address: setattr(self, '_server', address))


class NEOCluster(object):

    BaseConnection_checkTimeout = staticmethod(BaseConnection.checkTimeout)
    SocketConnector_makeClientConnection = staticmethod(
        SocketConnector.makeClientConnection)
    SocketConnector_makeListeningConnection = staticmethod(
        SocketConnector.makeListeningConnection)
    SocketConnector_send = staticmethod(SocketConnector.send)
    Storage__init__ = staticmethod(Storage.__init__)

    _cluster = None

    @classmethod
    def patch(cls):
        def makeClientConnection(self, addr):
            # XXX: 'threading.currentThread()._cluster'
            #      does not work for client. We could monkey-patch
            #      ClientConnection instead of using a global variable.
            cluster = cls._cluster()
            try:
                real_addr = cluster.resolv(addr)
                return cls.SocketConnector_makeClientConnection(self, real_addr)
            finally:
                self.remote_addr = addr
        def send(self, msg):
            result = cls.SocketConnector_send(self, msg)
            Serialized.pending = 1
            return result
        # TODO: 'sleep' should 'tic' in a smart way, so that storages can be
        #       safely started even if the cluster isn't.
        def sleep(seconds):
            l = threading.currentThread().em._lock
            while Serialized.pending:
                Serialized.tic(l)
            Serialized.tic(l)
        bootstrap.sleep = lambda seconds: None
        BaseConnection.checkTimeout = lambda self, t: None
        SocketConnector.makeClientConnection = makeClientConnection
        SocketConnector.makeListeningConnection = lambda self, addr: \
            cls.SocketConnector_makeListeningConnection(self, BIND)
        SocketConnector.send = send
        Storage.setupLog = lambda *args, **kw: None

    @classmethod
    def unpatch(cls):
        bootstrap.sleep = time.sleep
        BaseConnection.checkTimeout = cls.BaseConnection_checkTimeout
        SocketConnector.makeClientConnection = \
            cls.SocketConnector_makeClientConnection
        SocketConnector.makeListeningConnection = \
            cls.SocketConnector_makeListeningConnection
        SocketConnector.send = cls.SocketConnector_send
        Storage.setupLog = setupLog

    def __init__(self, master_count=1, partitions=1, replicas=0,
                       adapter=os.getenv('NEO_TESTS_ADAPTER', 'BTree'),
                       storage_count=None, db_list=None,
                       db_user='neo', db_password='neo'):
        self.name = 'neo_%s' % random.randint(0, 100)
        ip = getVirtualIp('master')
        self.master_nodes = ' '.join('%s:%s' % (ip, i)
                                     for i in xrange(master_count))
        kw = dict(cluster=self, getReplicas=replicas, getPartitions=partitions,
                  getAdapter=adapter, getReset=True)
        self.master_list = [MasterApplication(address=(ip, i), **kw)
                            for i in xrange(master_count)]
        ip = getVirtualIp('storage')
        if db_list is None:
            if storage_count is None:
                storage_count = replicas + 1
            db_list = ['test_neo%u' % i for i in xrange(storage_count)]
        db = '%s:%s@%%s' % (db_user, db_password)
        self.storage_list = [StorageApplication(address=(ip, i),
                                                getDatabase=db % x, **kw)
                             for i, x in enumerate(db_list)]
        ip = getVirtualIp('admin')
        self.admin_list = [AdminApplication(address=(ip, 0), **kw)]
        self.client = ClientApplication(self)
        self.neoctl = NeoCTL(self)

    # A few shortcuts that work when there's only 1 master/storage/admin
    @property
    def master(self):
        master, = self.master_list
        return master
    @property
    def storage(self):
        storage, = self.storage_list
        return storage
    @property
    def admin(self):
        admin, = self.admin_list
        return admin
    ###

    def resolv(self, addr):
        host, port = addr
        try:
            attr = SERVER_TYPE[VIRTUAL_IP.index(host)] + '_list'
        except ValueError:
            return addr
        return getattr(self, attr)[port].getListeningAddress()

    def reset(self, clear_database=False):
        for node_type in SERVER_TYPE:
            kw = {}
            if node_type == 'storage':
                kw['clear_database'] = clear_database
            for node in getattr(self, node_type + '_list'):
                node.resetNode(**kw)
        self.client = ClientApplication(self)
        self.neoctl = NeoCTL(self)

    def start(self, client=False, storage_list=None, fast_startup=True):
        self.__class__._cluster = weak_ref(self)
        for node_type in 'master', 'admin':
            for node in getattr(self, node_type + '_list'):
                node.start()
        self.tic()
        if fast_startup:
            self.neoctl.startCluster()
        if storage_list is None:
            storage_list = self.storage_list
        for node in storage_list:
            node.start()
        self.tic()
        if not fast_startup:
            self.neoctl.startCluster()
            self.tic()
        assert self.neoctl.getClusterState() == ClusterStates.RUNNING
        self.enableStorageList(storage_list)
        if client:
            self.startClient()

    def enableStorageList(self, storage_list):
        self.neoctl.enableStorageList([x.uuid for x in storage_list])
        self.tic()
        for node in storage_list:
            assert self.getNodeState(node) == NodeStates.RUNNING

    def startClient(self):
        self.client.setPoll(True)
        self.db = ZODB.DB(storage=self.getZODBStorage())

    def stop(self):
        if hasattr(self, 'db'):
            self.db.close()
        #self.neoctl.setClusterState(ClusterStates.STOPPING) # TODO
        try:
            Serialized.release(stop=1)
            for node_type in SERVER_TYPE[::-1]:
                for node in getattr(self, node_type + '_list'):
                    if node.isAlive():
                        node.join()
        finally:
            Serialized.acquire()
        self.__class__._cluster = None

    def tic(self, force=False):
        if force:
            Serialized.tic()
        while Serialized.pending:
            Serialized.tic()

    def getNodeState(self, node):
        uuid = node.uuid
        for node in self.neoctl.getNodeList(node.node_type):
            if node[2] == uuid:
                return node[3]

    def getOudatedCells(self):
        return [cell for row in self.neoctl.getPartitionRowList()[1]
                     for cell in row[1]
                     if cell[1] == CellStates.OUT_OF_DATE]

    def getZODBStorage(self, **kw):
        return Storage.Storage(None, self.name, _app=self.client, **kw)

    def getTransaction(self):
        txn = transaction.TransactionManager()
        return txn, self.db.open(txn)


class LoggerThreadName(object):

    def __getattr__(self, attr):
        return getattr(str(self), attr)

    def __str__(self):
        try:
            return threading.currentThread().node_name
        except AttributeError:
            return 'TEST'

class NEOThreadedTest(NeoUnitTestBase):

    def setupLog(self):
        log_file = os.path.join(getTempDirectory(), self.id() + '.log')
        setupLog(LoggerThreadName(), log_file, True)

    def setUp(self):
        NeoUnitTestBase.setUp(self)
        NEOCluster.patch()

    def tearDown(self):
        NEOCluster.unpatch()
        NeoUnitTestBase.tearDown(self)