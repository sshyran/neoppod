# Copyright (C) 2006-2009  Nexedi SA
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import logging
from thread import get_ident
from cPickle import dumps
from zlib import compress, decompress
from Queue import Queue, Empty
from random import shuffle
from time import sleep

from neo.client.mq import MQ
from neo.node import NodeManager, MasterNode, StorageNode
from neo.connection import MTClientConnection
from neo import protocol
from neo.protocol import INVALID_UUID, INVALID_TID, INVALID_PARTITION, \
        INVALID_PTID, CLIENT_NODE_TYPE, INVALID_SERIAL, \
        DOWN_STATE, HIDDEN_STATE
from neo.client.handlers.master import PrimaryBootstrapHandler, \
        PrimaryNotificationsHandler, PrimaryAnswersHandler
from neo.client.handlers.storage import StorageBootstrapHandler, \
        StorageAnswersHandler, StorageEventHandler
from neo.client.exception import NEOStorageError, NEOStorageConflictError, \
     NEOStorageNotFoundError
from neo.exception import NeoException
from neo.util import makeChecksum, dump
from neo.connector import getConnectorHandler
from neo.client.dispatcher import Dispatcher
from neo.client.poll import ThreadedPoll
from neo.event import EventManager
from neo.locking import RLock, Lock

from ZODB.POSException import UndoError, StorageTransactionError, ConflictError

class ConnectionClosed(Exception): pass

class ConnectionPool(object):
    """This class manages a pool of connections to storage nodes."""

    def __init__(self, app, max_pool_size = 25):
        self.app = app
        self.max_pool_size = max_pool_size
        self.connection_dict = {}
        # Define a lock in order to create one connection to
        # a storage node at a time to avoid multiple connections
        # to the same node.
        l = RLock()
        self.connection_lock_acquire = l.acquire
        self.connection_lock_release = l.release

    def _initNodeConnection(self, node):
        """Init a connection to a given storage node."""
        addr = node.getServer()
        if addr is None:
            return None

        app = self.app

        # Loop until a connection is obtained.
        while True:
            logging.info('trying to connect to %s - %s', node, node.getState())
            app.setNodeReady()
            conn = MTClientConnection(app.em, app.storage_event_handler, addr,
                                      connector_handler=app.connector_handler)
            conn.lock()
            try:
                if conn.getConnector() is None:
                    # This happens, if a connection could not be established.
                    logging.error('Connection to storage node %s failed', node)
                    return None

                p = protocol.requestNodeIdentification(CLIENT_NODE_TYPE,
                            app.uuid, addr[0], addr[1], app.name)
                msg_id = conn.ask(p)
                app.dispatcher.register(conn, msg_id, app.local_var.queue)
            finally:
                conn.unlock()

            try:
                app._waitMessage(conn, msg_id, handler=app.storage_bootstrap_handler)
            except ConnectionClosed:
                logging.error('Connection to storage node %s failed', node)
                return None

            if app.isNodeReady():
                logging.info('connected to storage node %s', node)
                return conn
            else:
                logging.info('Storage node %s not ready', node)
                return None

    def _dropConnections(self):
        """Drop connections."""
        for node_uuid, conn in self.connection_dict.items():
            # Drop first connection which looks not used
            conn.lock()
            try:
                if not conn.pending() and \
                        not self.app.dispatcher.registered(conn):
                    del self.connection_dict[conn.getUUID()]
                    conn.close()
                    logging.info('_dropConnections : connection to storage node %s:%d closed', 
                                 *(conn.getAddress()))
                    if len(self.connection_dict) <= self.max_pool_size:
                        break
            finally:
                conn.unlock()

    def _createNodeConnection(self, node):
        """Create a connection to a given storage node."""
        if len(self.connection_dict) > self.max_pool_size:
            # must drop some unused connections
            self._dropConnections()

        self.connection_lock_release()
        try:
            conn = self._initNodeConnection(node)
        finally:
            self.connection_lock_acquire()

        if conn is None:
            return None

        # add node to node manager
        if self.app.nm.getNodeByServer(node.getServer()) is None:
            n = StorageNode(node.getServer())
            self.app.nm.add(n)
        self.connection_dict[node.getUUID()] = conn
        conn.lock()
        return conn

    def getConnForCell(self, cell):
        return self.getConnForNode(cell.getNode())

    def getConnForNode(self, node):
        """Return a locked connection object to a given node
        If no connection exists, create a new one"""
        if node.getState() in (DOWN_STATE, HIDDEN_STATE):
            return None
        uuid = node.getUUID()
        self.connection_lock_acquire()
        try:
            try:
                conn = self.connection_dict[uuid]
                # Already connected to node
                conn.lock()
                return conn
            except KeyError:
                # Create new connection to node
                return self._createNodeConnection(node)
        finally:
            self.connection_lock_release()

    def removeConnection(self, node):
        """Explicitly remove connection when a node is broken."""
        self.connection_lock_acquire()
        try:
            try:
                del self.connection_dict[node.getUUID()]
            except KeyError:
                pass
        finally:
            self.connection_lock_release()


class ThreadContext(object):

    _threads_dict = {}

    def __getThreadData(self):
        id = get_ident()
        try:
            result = self._threads_dict[id]
        except KeyError:
            self.clear(id)
            result = self._threads_dict[id]
        return result

    def __getattr__(self, name):
        thread_data = self.__getThreadData()
        try:
            return thread_data[name]
        except KeyError:
            raise AttributeError, name
        
    def __setattr__(self, name, value):
        thread_data = self.__getThreadData()
        thread_data[name] = value

    def clear(self, id=None):
        if id is None:
            id = get_ident()
        self._threads_dict[id] = {
            'tid': None,
            'txn': None,
            'data_dict': {},
            'object_stored': 0,
            'txn_voted': False,
            'txn_finished': False,
            'queue': Queue(5),
        }


class Application(object):
    """The client node application."""

    def __init__(self, master_nodes, name, connector, **kw):
        logging.basicConfig(level = logging.DEBUG)
        logging.debug('master node address are %s' %(master_nodes,))
        em = EventManager()
        # Start polling thread
        self.poll_thread = ThreadedPoll(em)
        # Internal Attributes common to all thread
        self.name = name
        self.em = em
        self.connector_handler = getConnectorHandler(connector)
        self.dispatcher = Dispatcher()
        self.nm = NodeManager()
        self.cp = ConnectionPool(self)
        self.pt = None
        self.master_conn = None
        self.primary_master_node = None
        self.trying_master_node = None
        # XXX: this code duplicates neo.config.ConfigurationManager.getMasterNodeList
        self.master_node_list = master_node_list = []
        for node in master_nodes.split():
            if not node:
                continue
            if ':' in node:
                ip_address, port = node.split(':')
                port = int(port)
            else:
                ip_address = node
                port = 10100 # XXX: default_master_port
            server = (ip_address, port)
            master_node_list.append(server)
            self.nm.add(MasterNode(server=server))
        # no self-assigned UUID, primary master will supply us one
        self.uuid = INVALID_UUID
        self.mq_cache = MQ()
        self.new_oid_list = []
        self.ptid = INVALID_PTID
        self.storage_event_handler = StorageEventHandler(self, self.dispatcher)
        self.storage_bootstrap_handler = StorageBootstrapHandler(self)
        self.storage_handler = StorageAnswersHandler(self)
        self.primary_handler = PrimaryAnswersHandler(self)
        self.primary_bootstrap_handler = PrimaryBootstrapHandler(self)
        self.notifications_handler = PrimaryNotificationsHandler(self, self.dispatcher)
        # Internal attribute distinct between thread
        self.local_var = ThreadContext()
        # Lock definition :
        # _load_lock is used to make loading and storing atomic
        lock = Lock()
        self._load_lock_acquire = lock.acquire
        self._load_lock_release = lock.release
        # _oid_lock is used in order to not call multiple oid
        # generation at the same time
        lock = Lock()
        self._oid_lock_acquire = lock.acquire
        self._oid_lock_release = lock.release
        lock = Lock()
        # _cache_lock is used for the client cache
        self._cache_lock_acquire = lock.acquire
        self._cache_lock_release = lock.release
        lock = Lock()
        # _connecting_to_master_node is used to prevent simultaneous master
        # node connection attemps
        self._connecting_to_master_node_acquire = lock.acquire
        self._connecting_to_master_node_release = lock.release
        # _nm ensure exclusive access to the node manager
        lock = Lock()
        self._nm_acquire = lock.acquire
        self._nm_release = lock.release

    def notifyDeadNode(self, conn):
        """ Notify a storage failure to the primary master """
        s_node = self.nm.getNodeByServer(conn.getAddress())
        if s_node is None or s_node.getNodeType() != protocol.STORAGE_NODE_TYPE:
            return
        s_uuid = s_node.getUUID()
        ip_address, port = s_node.getServer()
        m_conn = self._getMasterConnection()
        m_conn.lock()
        try:
            node_list = [(protocol.STORAGE_NODE_TYPE, ip_address, port, s_uuid, s_node.getState())]
            m_conn.notify(protocol.notifyNodeInformation(node_list))
        finally:
            m_conn.unlock()

    def _waitMessage(self, target_conn = None, msg_id = None, handler=None):
        """Wait for a message returned by the dispatcher in queues."""
        local_queue = self.local_var.queue
        while 1:
            if msg_id is None:
                try:
                    conn, packet = local_queue.get_nowait()
                except Empty:
                    break
            else:
                conn, packet = local_queue.get()
            # check fake packet
            if packet is None:
                if conn.getUUID() == target_conn.getUUID():
                    raise ConnectionClosed
                else:
                    continue
            # Guess the handler to use based on the type of node on the
            # connection
            if handler is None:
                node = self.nm.getNodeByServer(conn.getAddress())
                if node is None:
                    raise ValueError, 'Expecting an answer from a node ' \
                        'which type is not known... Is this right ?'
                else:
                    node_type = node.getType()
                    if node_type == protocol.STORAGE_NODE_TYPE:
                        handler = self.storage_handler
                    elif node_type == protocol.MASTER_NODE_TYPE:
                        handler = self.primary_handler
                    else:
                        raise ValueError, 'Unknown node type: %r' % (
                            node_type, )
            handler.dispatch(conn, packet)
            if target_conn is conn and msg_id == packet.getId() \
                    and packet.getType() & 0x8000:
                break

    def _askStorage(self, conn, packet, timeout=5, additional_timeout=30):
        """ Send a request to a storage node and process it's answer """
        try:
            msg_id = conn.ask(packet, timeout, additional_timeout)
            self.dispatcher.register(conn, msg_id, self.local_var.queue)
        finally:
            # assume that the connection was already locked
            conn.unlock()
        self._waitMessage(conn, msg_id, self.storage_handler)

    def _askPrimary(self, packet, timeout=5, additional_timeout=30):
        """ Send a request to the primary master and process it's answer """
        conn = self._getMasterConnection()
        conn.lock()
        try:
            msg_id = conn.ask(packet, timeout, additional_timeout)
            self.dispatcher.register(conn, msg_id, self.local_var.queue)
        finally:
            conn.unlock()
        self._waitMessage(conn, msg_id, self.primary_handler)

    def _getMasterConnection(self):
        """ Connect to the primary master node on demand """
        # acquire the lock to allow only one thread to connect to the primary 
        lock = self._connecting_to_master_node_acquire()
        try:
            if self.master_conn is None:    
                self.master_conn = self._connectToPrimaryMasterNode()
            return self.master_conn
        finally:
            self._connecting_to_master_node_release()

    def _getPartitionTable(self):
        """ Return the partition table manager, reconnect the PMN if needed """
        # this ensure the master connection is established and the partition
        # table is up to date.
        self._getMasterConnection()
        return self.pt

    def _getCellListForID(self, id, readable=False, writable=False):
        """ Return the cells available for the specified (O|T)ID """
        pt = self._getPartitionTable()
        return pt.getCellListForID(id, readable, writable)

    def _connectToPrimaryMasterNode(self):
        logging.debug('connecting to primary master...')
        ready = False
        nm = self.nm
        while not ready:
            # Get network connection to primary master
            index = 0
            connected = False
            while not connected:
                if self.primary_master_node is not None:
                    # If I know a primary master node, pinpoint it.
                    self.trying_master_node = self.primary_master_node
                    self.primary_master_node = None
                else:
                    # Otherwise, check one by one.
                    master_list = nm.getMasterNodeList()
                    try:
                        self.trying_master_node = master_list[index]
                    except IndexError:
                        index = 0
                        self.trying_master_node = master_list[0]
                    index += 1
                # Connect to master
                conn = MTClientConnection(self.em, self.notifications_handler,
                                          addr=self.trying_master_node.getServer(),
                                          connector_handler=self.connector_handler)
                # Query for primary master node
                conn.lock()
                try:
                    if conn.getConnector() is None:
                        # This happens, if a connection could not be established.
                        logging.error('Connection to master node %s failed',
                                      self.trying_master_node)
                        continue
                    msg_id = conn.ask(protocol.askPrimaryMaster())
                    self.dispatcher.register(conn, msg_id, self.local_var.queue)
                finally:
                    conn.unlock()
                try:
                    self._waitMessage(conn, msg_id, handler=self.primary_bootstrap_handler)
                except ConnectionClosed:
                    continue
                # If we reached the primary master node, mark as connected
                connected = self.primary_master_node is not None \
                            and self.primary_master_node is self.trying_master_node

            logging.info('connected to a primary master node')
            # Identify to primary master and request initial data
            while conn.getUUID() is None:
                conn.lock()
                try:
                    if conn.getConnector() is None:
                        logging.error('Connection to master node %s lost',
                                      self.trying_master_node)
                        self.primary_master_node = None
                        break
                    p = protocol.requestNodeIdentification(CLIENT_NODE_TYPE,
                            self.uuid, '0.0.0.0', 0, self.name)
                    msg_id = conn.ask(p)
                    self.dispatcher.register(conn, msg_id, self.local_var.queue)
                finally:
                    conn.unlock()
                try:
                    self._waitMessage(conn, msg_id, handler=self.primary_bootstrap_handler)
                except ConnectionClosed:
                    self.primary_master_node = None
                    break
                if conn.getUUID() is None:
                    # Node identification was refused by master.
                    # Sleep a bit an retry.
                    # XXX: This should be replaced by:
                    # - queuing requestNodeIdentification at master side
                    # - sending the acceptance from master when it becomes
                    #   ready
                    # Thus removing the need to:
                    # - needlessly bother the primary master every 5 seconds
                    #   (...per client)
                    # - have a sleep in the code (yuck !)
                    sleep(5)
            if self.uuid != INVALID_UUID:
                # TODO: pipeline those 2 requests
                # This is currently impossible because _waitMessage can only
                # wait on one message at a time
                conn.lock()
                try:
                    msg_id = conn.ask(protocol.askPartitionTable([]))
                    self.dispatcher.register(conn, msg_id, self.local_var.queue)
                finally:
                    conn.unlock()
                self._waitMessage(conn, msg_id, handler=self.primary_bootstrap_handler)
                conn.lock()
                try:
                    msg_id = conn.ask(protocol.askNodeInformation())
                    self.dispatcher.register(conn, msg_id, self.local_var.queue)
                finally:
                    conn.unlock()
                self._waitMessage(conn, msg_id, handler=self.primary_bootstrap_handler)
            ready = self.uuid != INVALID_UUID and self.pt is not None \
                                 and self.pt.operational()
        logging.info("connected to primary master node %s" % self.primary_master_node)
        return conn
        
    def registerDB(self, db, limit):
        self._db = db

    def getDB(self):
        return self._db

    def new_oid(self):
        """Get a new OID."""
        self._oid_lock_acquire()
        try:
            if len(self.new_oid_list) == 0:
                # Get new oid list from master node
                # we manage a list of oid here to prevent
                # from asking too many time new oid one by one
                # from master node
                self._askPrimary(protocol.askNewOIDs(100))
                if len(self.new_oid_list) <= 0:
                    raise NEOStorageError('new_oid failed')
            return self.new_oid_list.pop()
        finally:
            self._oid_lock_release()


    def getSerial(self, oid):
        # Try in cache first
        self._cache_lock_acquire()
        try:
            if oid in self.mq_cache:
                return self.mq_cache[oid][0]
        finally:
            self._cache_lock_release()
        # history return serial, so use it
        hist = self.history(oid, length = 1, object_only = 1)
        if len(hist) == 0:
            raise NEOStorageNotFoundError()
        if hist[0] != oid:
            raise NEOStorageError('getSerial failed')
        return hist[1][0][0]


    def _load(self, oid, serial = INVALID_TID, tid = INVALID_TID, cache = 0):
        """Internal method which manage load ,loadSerial and loadBefore."""
        cell_list = self._getCellListForID(oid, readable=True)
        if len(cell_list) == 0:
            # No cells available, so why are we running ?
            logging.error('oid %s not found because no storage is available for it', dump(oid))
            raise NEOStorageNotFoundError()

        shuffle(cell_list)
        self.local_var.asked_object = 0
        for cell in cell_list:
            logging.debug('trying to load %s from %s',
                          dump(oid), dump(cell.getUUID()))
            conn = self.cp.getConnForCell(cell)
            if conn is None:
                continue

            try:
                self._askStorage(conn, protocol.askObject(oid, serial, tid))
            except ConnectionClosed:
                continue

            if self.local_var.asked_object == -1:
                # OID not found
                break

            # Check data
            noid, start_serial, end_serial, compression, checksum, data \
                = self.local_var.asked_object
            if noid != oid:
                # Oops, try with next node
                logging.error('got wrong oid %s instead of %s from node %s',
                              noid, dump(oid), cell.getServer())
                self.local_var.asked_object = -1
                continue
            elif checksum != makeChecksum(data):
                # Check checksum.
                logging.error('wrong checksum from node %s for oid %s',
                              cell.getServer(), dump(oid))
                self.local_var.asked_object = -1
                continue
            else:
                # Everything looks alright.
                break

        if self.local_var.asked_object == 0:
            # We didn't got any object from all storage node because of connection error
            logging.warning('oid %s not found because of connection failure', dump(oid))
            raise NEOStorageNotFoundError()

        if self.local_var.asked_object == -1:
            # We didn't got any object from all storage node
            logging.info('oid %s not found', dump(oid))
            raise NEOStorageNotFoundError()

        # Uncompress data
        if compression:
            data = decompress(data)

        # Put in cache only when using load
        if cache:
            self._cache_lock_acquire()
            try:
                self.mq_cache[oid] = start_serial, data
            finally:
                self._cache_lock_release()
        if end_serial == INVALID_SERIAL:
            end_serial = None
        return data, start_serial, end_serial


    def load(self, oid, version=None):
        """Load an object for a given oid."""
        # First try from cache
        self._load_lock_acquire()
        try:
            self._cache_lock_acquire()
            try:
                if oid in self.mq_cache:
                    logging.debug('load oid %s is cached', dump(oid))
                    return self.mq_cache[oid][1], self.mq_cache[oid][0]
            finally:
                self._cache_lock_release()
            # Otherwise get it from storage node
            return self._load(oid, cache=1)[:2]
        finally:
            self._load_lock_release()


    def loadSerial(self, oid, serial):
        """Load an object for a given oid and serial."""
        # Do not try in cache as it manages only up-to-date object
        logging.debug('loading %s at %s', dump(oid), dump(serial))
        return self._load(oid, serial=serial)[0]


    def loadBefore(self, oid, tid):
        """Load an object for a given oid before tid committed."""
        # Do not try in cache as it manages only up-to-date object
        if tid is None:
            tid = INVALID_TID
        logging.debug('loading %s before %s', dump(oid), dump(tid))
        data, start, end = self._load(oid, tid=tid)
        if end is None:
            # No previous version
            return None
        else:
            return data, start, end


    def tpc_begin(self, transaction, tid=None, status=' '):
        """Begin a new transaction."""
        # First get a transaction, only one is allowed at a time
        if self.local_var.txn is transaction:
            # We already begin the same transaction
            return
        # Get a new transaction id if necessary
        if tid is None:
            self.local_var.tid = None
            self._askPrimary(protocol.askNewTID())
            if self.local_var.tid is None:
                raise NEOStorageError('tpc_begin failed')
        else:
            self.local_var.tid = tid
        self.local_var.txn = transaction            


    def store(self, oid, serial, data, version, transaction):
        """Store object."""
        if transaction is not self.local_var.txn:
            raise StorageTransactionError(self, transaction)
        if serial is None:
            serial = INVALID_SERIAL
        logging.debug('storing oid %s serial %s',
                     dump(oid), dump(serial))
        # Find which storage node to use
        cell_list = self._getCellListForID(oid, writable=True)
        if len(cell_list) == 0:
            # FIXME must wait for cluster to be ready
            raise NEOStorageError
        # Store data on each node
        compressed_data = compress(data)
        checksum = makeChecksum(compressed_data)
        self.local_var.object_stored_counter = 0
        for cell in cell_list:
            #logging.info("storing object %s %s" %(cell.getServer(),cell.getState()))
            conn = self.cp.getConnForCell(cell)
            if conn is None:                
                continue

            self.local_var.object_stored = 0
            p = protocol.askStoreObject(oid, serial, 1,
                     checksum, compressed_data, self.local_var.tid)
            try:
                self._askStorage(conn, p)
            except ConnectionClosed:
                continue

            # Check we don't get any conflict
            if self.local_var.object_stored[0] == -1:
                if self.local_var.data_dict.has_key(oid):
                    # One storage already accept the object, is it normal ??
                    # remove from dict and raise ConflictError, don't care of
                    # previous node which already store data as it would be resent
                    # again if conflict is resolved or txn will be aborted
                    del self.local_var.data_dict[oid]
                self.conflict_serial = self.local_var.object_stored[1]
                raise NEOStorageConflictError
            # increase counter so that we know if a node has stored the object or not
            self.local_var.object_stored_counter += 1

        if self.local_var.object_stored_counter == 0:
            # no storage nodes were available
            raise NEOStorageError('tpc_store failed')
        
        # Store object in tmp cache
        self.local_var.data_dict[oid] = data

        return self.local_var.tid


    def tpc_vote(self, transaction):
        """Store current transaction."""
        if transaction is not self.local_var.txn:
            raise StorageTransactionError(self, transaction)
        user = transaction.user
        desc = transaction.description
        ext = dumps(transaction._extension)
        oid_list = self.local_var.data_dict.keys()
        # Store data on each node
        pt = self._getPartitionTable()
        cell_list = self._getCellListForID(self.local_var.tid, writable=True)
        self.local_var.voted_counter = 0
        for cell in cell_list:
            logging.info("voting object %s %s" %(cell.getServer(), cell.getState()))
            conn = self.cp.getConnForCell(cell)
            if conn is None:
                continue

            self.local_var.txn_voted = False
            p = protocol.askStoreTransaction(self.local_var.tid, 
                    user, desc, ext, oid_list)
            try:
                self._askStorage(conn, p)
            except ConnectionClosed:
                continue

            if not self.isTransactionVoted():
                raise NEOStorageError('tpc_vote failed')
            self.local_var.voted_counter += 1

        # check at least one storage node accepted
        if self.local_var.voted_counter == 0:
            raise NEOStorageError('tpc_vote failed')

    def tpc_abort(self, transaction):
        """Abort current transaction."""
        if transaction is not self.local_var.txn:
            return

        cell_set = set()
        # select nodes where objects were stored
        for oid in self.local_var.data_dict.iterkeys():
            cell_set |= set(self._getCellListForID(oid, writable=True))
        # select nodes where transaction was stored
        cell_set |= set(self._getCellListForID(self.local_var.tid, writable=True))

        # cancel transaction one all those nodes
        for cell in cell_set:
            conn = self.cp.getConnForCell(cell)
            if conn is None:
                continue
            try:
                conn.notify(protocol.abortTransaction(self.local_var.tid))
            finally:
                conn.unlock()

        # Abort the transaction in the primary master node.
        conn = self._getMasterConnection()
        conn.lock()
        try:
            conn.notify(protocol.abortTransaction(self.local_var.tid))
        finally:
            conn.unlock()
        self.local_var.clear()

    def tpc_finish(self, transaction, f=None):
        """Finish current transaction."""
        if self.local_var.txn is not transaction:
            return
        self._load_lock_acquire()
        try:
            # Call function given by ZODB
            if f is not None:
                f(self.local_var.tid)

            # Call finish on master
            oid_list = self.local_var.data_dict.keys()
            p = protocol.finishTransaction(oid_list, self.local_var.tid)
            self._askPrimary(p)

            if not self.isTransactionFinished():
                raise NEOStorageError('tpc_finish failed')

            # Update cache
            self._cache_lock_acquire()
            try:
                for oid in self.local_var.data_dict.iterkeys():
                    data = self.local_var.data_dict[oid]
                    # Now serial is same as tid
                    self.mq_cache[oid] = self.local_var.tid, data
            finally:
                self._cache_lock_release()
            self.local_var.clear()
            return self.local_var.tid
        finally:
            self._load_lock_release()

    def undo(self, transaction_id, txn, wrapper):
        if txn is not self.local_var.txn:
            raise StorageTransactionError(self, transaction_id)

        # First get transaction information from a storage node.
        cell_list = self._getCellListForID(transaction_id, writable=True)
        shuffle(cell_list)
        for cell in cell_list:
            conn = self.cp.getConnForCell(cell)
            if conn is None:
                continue

            self.local_var.txn_info = 0
            try:
                self._askStorage(conn, protocol.askTransactionInformation(transaction_id))
            except ConnectionClosed:
                continue

            if self.local_var.txn_info == -1:
                # Tid not found, try with next node
                continue
            elif isinstance(self.local_var.txn_info, dict):
                break
            else:
                raise NEOStorageError('undo failed')

        if self.local_var.txn_info in (-1, 0):
            raise NEOStorageError('undo failed')

        oid_list = self.local_var.txn_info['oids']
        # Second get object data from storage node using loadBefore
        data_dict = {}
        for oid in oid_list:
            try:
                result = self.loadBefore(oid, transaction_id)
            except NEOStorageNotFoundError:
                # no previous revision, can't undo (as in filestorage)
                raise UndoError("no previous record", oid)
            data, start, end = result
            # end must be TID we are going to undone otherwise it means
            # a later transaction modify the object
            if end != transaction_id:
                raise UndoError("non-undoable transaction", oid)
            data_dict[oid] = data

        # Third do transaction with old data
        oid_list = data_dict.keys()
        for oid in oid_list:
            data = data_dict[oid]
            try:
                self.store(oid, transaction_id, data, None, txn)
            except NEOStorageConflictError, serial:
                if serial <= self.local_var.tid:
                    new_data = wrapper.tryToResolveConflict(oid, self.local_var.tid,
                                                            serial, data)
                    if new_data is not None:
                        self.store(oid, self.local_var.tid, new_data, None, txn)
                        continue
                raise ConflictError(oid = oid, serials = (self.local_var.tid, serial),
                                    data = data)
        return self.local_var.tid, oid_list

    def undoLog(self, first, last, filter=None, block=0):
        if last < 0:
            # See FileStorage.py for explanation
            last = first - last

        # First get a list of transactions from all storage nodes.
        # Each storage node will return TIDs only for UP_TO_DATE_STATE and
        # FEEDING_STATE cells
        pt = self._getPartitionTable()
        storage_node_list = pt.getNodeList()

        self.local_var.node_tids = {}
        for storage_node in storage_node_list:
            conn = self.cp.getConnForNode(storage_node)
            if conn is None:
                continue

            try:
                p = protocol.askTIDs(first, last, INVALID_PARTITION)
                msg_id = conn.ask(p)
                self.dispatcher.register(conn, msg_id, self.local_var.queue)
            finally:
                conn.unlock()

        # Wait for answers from all storages.
        # FIXME this is a busy loop.
        while len(self.local_var.node_tids) != len(storage_node_list):
            try:
                self._waitMessage(handler=self.storage_handler)
            except ConnectionClosed:
                continue

        # Reorder tids
        ordered_tids = set()
        update = ordered_tids.update
        for tid_list in self.local_var.node_tids.itervalues():
          update(tid_list)
        ordered_tids = list(ordered_tids)
        # XXX do we need a special cmp function here ?
        ordered_tids.sort(reverse=True)
        logging.info("UndoLog, tids %s", ordered_tids)
        # For each transaction, get info
        undo_info = []
        append = undo_info.append
        for tid in ordered_tids:
            cell_list = self._getCellListForID(tid, readable=True)
            shuffle(cell_list)
            for cell in cell_list:
                conn = self.cp.getConnForCell(cell)
                if conn is not None:
                    self.local_var.txn_info = 0
                    try:
                        self._askStorage(conn, protocol.askTransactionInformation(tid))
                    except ConnectionClosed:
                        continue
                    if isinstance(self.local_var.txn_info, dict):
                        break

            if self.local_var.txn_info in (-1, 0):
                # TID not found at all
                raise NeoException, 'Data inconsistency detected: ' \
                                    'transaction info for TID %r could not ' \
                                    'be found' % (tid, )

            if filter is None or filter(self.local_var.txn_info):
                self.local_var.txn_info.pop("oids")
                append(self.local_var.txn_info)
                if len(undo_info) >= last - first:
                    break
        # Check we return at least one element, otherwise call
        # again but extend offset
        if len(undo_info) == 0 and not block:
            undo_info = self.undoLog(first=first, last=last*5, filter=filter, block=1)
        return undo_info

    # FIXME: filter function isn't used 
    def history(self, oid, version=None, length=1, filter=None, object_only=0):
        # Get history informations for object first
        cell_list = self._getCellListForID(oid, readable=True)
        shuffle(cell_list)

        for cell in cell_list:
            conn = self.cp.getConnForCell(cell)
            if conn is None:
                continue

            self.local_var.history = None
            try:
                self._askStorage(conn, protocol.askObjectHistory(oid, 0, length))
            except ConnectionClosed:
                continue

            if self.local_var.history == -1:
                # Not found, go on with next node
                continue
            if self.local_var.history[0] != oid:
                # Got history for wrong oid
                raise NEOStorageError('inconsistency in storage: asked oid ' \
                                      '%r, got %r' % (
                                      oid, self.local_var.history[0]))

        if not isinstance(self.local_var.history, tuple):
            raise NEOStorageError('history failed')
        if object_only:
            # Use by getSerial
            return self.local_var.history

        # Now that we have object informations, get txn informations
        history_list = []
        for serial, size in self.local_var.history[1]:
            self._getCellListForID(serial, readable=True)
            shuffle(cell_list)

            for cell in cell_list:
                conn = self.cp.getConnForCell(cell)
                if conn is None:
                    continue

                # ask transaction information
                self.local_var.txn_info = None
                try:
                    self._askStorage(conn, protocol.askTransactionInformation(serial))
                except ConnectionClosed:
                    continue

                if self.local_var.txn_info == -1:
                    # TID not found
                    continue
                if isinstance(self.local_var.txn_info, dict):
                    break

            # create history dict
            self.local_var.txn_info.pop('id')
            self.local_var.txn_info.pop('oids')
            self.local_var.txn_info['tid'] = serial
            self.local_var.txn_info['version'] = None
            self.local_var.txn_info['size'] = size
            history_list.append(self.local_var.txn_info)

        return history_list

    def __del__(self):
        """Clear all connection."""
        # Due to bug in ZODB, close is not always called when shutting
        # down zope, so use __del__ to close connections
        for conn in self.em.getConnectionList():
            conn.lock()
            try:
                conn.close()
            finally:
                conn.release()
        # Stop polling thread
        self.poll_thread.stop()
    close = __del__

    def sync(self):
        self._waitMessage()

    def setNodeReady(self):
        self.local_var.node_ready = True

    def setNodeNotReady(self):
        self.local_var.node_ready = False

    def isNodeReady(self):
        return self.local_var.node_ready

    def setTID(self, value):
        self.local_var.tid = value

    def getTID(self):
        return self.local_var.tid

    def getConflictSerial(self):
        return self.conflict_serial

    def setTransactionFinished(self):
        self.local_var.txn_finished = True

    def isTransactionFinished(self):
        return self.local_var.txn_finished

    def setTransactionVoted(self):
        self.local_var.txn_voted = True

    def isTransactionVoted(self):
        return self.local_var.txn_voted

