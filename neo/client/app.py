#
# Copyright (C) 2006-2010  Nexedi SA
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

from thread import get_ident
from cPickle import dumps, loads
from zlib import compress as real_compress, decompress
from neo.locking import Queue, Empty
from random import shuffle
import time

from ZODB.POSException import UndoError, StorageTransactionError, ConflictError
from ZODB.ConflictResolution import ResolvedSerial

from neo import setupLog
setupLog('CLIENT', verbose=True)

from neo import logging
from neo.protocol import NodeTypes, Packets, INVALID_PARTITION
from neo.event import EventManager
from neo.util import makeChecksum as real_makeChecksum, dump
from neo.locking import Lock
from neo.connection import MTClientConnection, OnTimeout
from neo.node import NodeManager
from neo.connector import getConnectorHandler
from neo.client.exception import NEOStorageError
from neo.client.exception import NEOStorageNotFoundError, ConnectionClosed
from neo.exception import NeoException
from neo.client.handlers import storage, master
from neo.dispatcher import Dispatcher, ForgottenPacket
from neo.client.poll import ThreadedPoll
from neo.client.iterator import Iterator
from neo.client.mq import MQ
from neo.client.pool import ConnectionPool
from neo.util import u64, parseMasterList
from neo.profiling import profiler_decorator, PROFILING_ENABLED
from neo.live_debug import register as registerLiveDebugger

if PROFILING_ENABLED:
    # Those functions require a "real" python function wrapper before they can
    # be decorated.
    @profiler_decorator
    def compress(data):
        return real_compress(data)

    @profiler_decorator
    def makeChecksum(data):
        return real_makeChecksum(data)
else:
    # If profiling is disabled, directly use original functions.
    compress = real_compress
    makeChecksum = real_makeChecksum

class ThreadContext(object):

    def __init__(self):
        super(ThreadContext, self).__setattr__('_threads_dict', {})

    def __getThreadData(self):
        thread_id = get_ident()
        try:
            result = self._threads_dict[thread_id]
        except KeyError:
            self.clear(thread_id)
            result = self._threads_dict[thread_id]
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

    def clear(self, thread_id=None):
        if thread_id is None:
            thread_id = get_ident()
        thread_dict = self._threads_dict.get(thread_id)
        if thread_dict is None:
            queue = Queue(0)
        else:
            queue = thread_dict['queue']
        self._threads_dict[thread_id] = {
            'tid': None,
            'txn': None,
            'data_dict': {},
            'object_serial_dict': {},
            'object_stored_counter_dict': {},
            'conflict_serial_dict': {},
            'resolved_conflict_serial_dict': {},
            'object_stored': 0,
            'txn_voted': False,
            'txn_finished': False,
            'queue': queue,
            'txn_info': 0,
            'history': None,
            'node_tids': {},
            'node_ready': False,
            'asked_object': 0,
            'undo_conflict_oid_list': [],
            'undo_error_oid_list': [],
            'involved_nodes': set(),
        }


class Application(object):
    """The client node application."""

    def __init__(self, master_nodes, name, connector=None, compress=True, **kw):
        # Start polling thread
        self.em = EventManager()
        self.poll_thread = ThreadedPoll(self.em)
        # Internal Attributes common to all thread
        self._db = None
        self.name = name
        self.connector_handler = getConnectorHandler(connector)
        self.dispatcher = Dispatcher()
        self.nm = NodeManager()
        self.cp = ConnectionPool(self)
        self.pt = None
        self.master_conn = None
        self.primary_master_node = None
        self.trying_master_node = None

        # load master node list
        for address in parseMasterList(master_nodes):
            self.nm.createMaster(address=address)

        # no self-assigned UUID, primary master will supply us one
        self.uuid = None
        self.mq_cache = MQ()
        self.new_oid_list = []
        self.last_oid = '\0' * 8
        self.storage_event_handler = storage.StorageEventHandler(self)
        self.storage_bootstrap_handler = storage.StorageBootstrapHandler(self)
        self.storage_handler = storage.StorageAnswersHandler(self)
        self.primary_handler = master.PrimaryAnswersHandler(self)
        self.primary_bootstrap_handler = master.PrimaryBootstrapHandler(self)
        self.notifications_handler = master.PrimaryNotificationsHandler( self)
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
        self.compress = compress
        registerLiveDebugger(on_log=self.log)

    def log(self):
        self.em.log()
        self.nm.log()
        if self.pt is not None:
            self.pt.log()

    @profiler_decorator
    def _handlePacket(self, conn, packet, handler=None):
        """
          conn
            The connection which received the packet (forwarded to handler).
          packet
            The packet to handle.
          handler
            The handler to use to handle packet.
            If not given, it will be guessed from connection's not type.
        """
        if handler is None:
            # Guess the handler to use based on the type of node on the
            # connection
            node = self.nm.getByAddress(conn.getAddress())
            if node is None:
                raise ValueError, 'Expecting an answer from a node ' \
                    'which type is not known... Is this right ?'
            if node.isStorage():
                handler = self.storage_handler
            elif node.isMaster():
                handler = self.primary_handler
            else:
                raise ValueError, 'Unknown node type: %r' % (node.__class__, )
        conn.lock()
        try:
            handler.dispatch(conn, packet)
        finally:
            conn.unlock()

    @profiler_decorator
    def _waitAnyMessage(self, block=True):
        """
          Handle all pending packets.
          block
            If True (default), will block until at least one packet was
            received.
        """
        pending = self.dispatcher.pending
        queue = self.local_var.queue
        get = queue.get
        _handlePacket = self._handlePacket
        while pending(queue):
            try:
                conn, packet = get(block)
            except Empty:
                break
            if packet is None or isinstance(packet, ForgottenPacket):
                # connection was closed or some packet was forgotten
                continue
            block = False
            try:
                _handlePacket(conn, packet)
            except ConnectionClosed:
                pass

    @profiler_decorator
    def _waitMessage(self, target_conn, msg_id, handler=None):
        """Wait for a message returned by the dispatcher in queues."""
        get = self.local_var.queue.get
        _handlePacket = self._handlePacket
        while True:
            conn, packet = get(True)
            is_forgotten = isinstance(packet, ForgottenPacket)
            if target_conn is conn:
                # check fake packet
                if packet is None:
                    raise ConnectionClosed
                if msg_id == packet.getId():
                    if is_forgotten:
                        raise ValueError, 'ForgottenPacket for an ' \
                            'explicitely expected packet.'
                    self._handlePacket(conn, packet, handler=handler)
                    break
            elif not is_forgotten and packet is not None:
                self._handlePacket(conn, packet)

    @profiler_decorator
    def _askStorage(self, conn, packet):
        """ Send a request to a storage node and process it's answer """
        msg_id = conn.ask(packet, queue=self.local_var.queue)
        self._waitMessage(conn, msg_id, self.storage_handler)

    @profiler_decorator
    def _askPrimary(self, packet):
        """ Send a request to the primary master and process it's answer """
        conn = self._getMasterConnection()
        msg_id = conn.ask(packet, queue=self.local_var.queue)
        self._waitMessage(conn, msg_id, self.primary_handler)

    @profiler_decorator
    def _getMasterConnection(self):
        """ Connect to the primary master node on demand """
        # acquire the lock to allow only one thread to connect to the primary
        result = self.master_conn
        if result is None:
            self._connecting_to_master_node_acquire()
            try:
                self.new_oid_list = []
                result = self._connectToPrimaryNode()
                self.master_conn = result
            finally:
                self._connecting_to_master_node_release()
        return result

    def _getPartitionTable(self):
        """ Return the partition table manager, reconnect the PMN if needed """
        # this ensure the master connection is established and the partition
        # table is up to date.
        self._getMasterConnection()
        return self.pt

    @profiler_decorator
    def _getCellListForOID(self, oid, readable=False, writable=False):
        """ Return the cells available for the specified OID """
        pt = self._getPartitionTable()
        return pt.getCellListForOID(oid, readable, writable)

    def _getCellListForTID(self, tid, readable=False, writable=False):
        """ Return the cells available for the specified TID """
        pt = self._getPartitionTable()
        return pt.getCellListForTID(tid, readable, writable)

    @profiler_decorator
    def _connectToPrimaryNode(self):
        logging.debug('connecting to primary master...')
        ready = False
        nm = self.nm
        queue = self.local_var.queue
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
                    master_list = nm.getMasterList()
                    try:
                        self.trying_master_node = master_list[index]
                    except IndexError:
                        time.sleep(1)
                        index = 0
                        self.trying_master_node = master_list[0]
                    index += 1
                # Connect to master
                conn = MTClientConnection(self.em,
                        self.notifications_handler,
                        addr=self.trying_master_node.getAddress(),
                        connector=self.connector_handler(),
                        dispatcher=self.dispatcher)
                # Query for primary master node
                if conn.getConnector() is None:
                    # This happens if a connection could not be established.
                    logging.error('Connection to master node %s failed',
                                  self.trying_master_node)
                    continue
                msg_id = conn.ask(Packets.AskPrimary(), queue=queue)
                try:
                    self._waitMessage(conn, msg_id,
                            handler=self.primary_bootstrap_handler)
                except ConnectionClosed:
                    continue
                # If we reached the primary master node, mark as connected
                connected = self.primary_master_node is not None and \
                        self.primary_master_node is self.trying_master_node

            logging.info('connected to a primary master node')
            # Identify to primary master and request initial data
            while conn.getUUID() is None:
                if conn.getConnector() is None:
                    logging.error('Connection to master node %s lost',
                                  self.trying_master_node)
                    self.primary_master_node = None
                    break
                p = Packets.RequestIdentification(NodeTypes.CLIENT,
                        self.uuid, None, self.name)
                msg_id = conn.ask(p, queue=queue)
                try:
                    self._waitMessage(conn, msg_id,
                            handler=self.primary_bootstrap_handler)
                except ConnectionClosed:
                    self.primary_master_node = None
                    break
                if conn.getUUID() is None:
                    # Node identification was refused by master.
                    time.sleep(1)
            if self.uuid is not None:
                msg_id = conn.ask(Packets.AskNodeInformation(), queue=queue)
                self._waitMessage(conn, msg_id,
                        handler=self.primary_bootstrap_handler)
                msg_id = conn.ask(Packets.AskPartitionTable(), queue=queue)
                self._waitMessage(conn, msg_id,
                        handler=self.primary_bootstrap_handler)
            ready = self.uuid is not None and self.pt is not None \
                                 and self.pt.operational()
        logging.info("connected to primary master node %s" %
                self.primary_master_node)
        return conn

    def registerDB(self, db, limit):
        self._db = db

    def getDB(self):
        return self._db

    @profiler_decorator
    def new_oid(self):
        """Get a new OID."""
        self._oid_lock_acquire()
        try:
            if len(self.new_oid_list) == 0:
                # Get new oid list from master node
                # we manage a list of oid here to prevent
                # from asking too many time new oid one by one
                # from master node
                self._askPrimary(Packets.AskNewOIDs(100))
                if len(self.new_oid_list) <= 0:
                    raise NEOStorageError('new_oid failed')
            self.last_oid = self.new_oid_list.pop()
            return self.last_oid
        finally:
            self._oid_lock_release()

    def getStorageSize(self):
        # return the last OID used, this is innacurate
        return int(u64(self.last_oid))

    @profiler_decorator
    def getSerial(self, oid):
        # Try in cache first
        self._cache_lock_acquire()
        try:
            if oid in self.mq_cache:
                return self.mq_cache[oid][0]
        finally:
            self._cache_lock_release()
        # history return serial, so use it
        hist = self.history(oid, size=1, object_only=1)
        if len(hist) == 0:
            raise NEOStorageNotFoundError()
        if hist[0] != oid:
            raise NEOStorageError('getSerial failed')
        return hist[1][0][0]


    @profiler_decorator
    def _load(self, oid, serial=None, tid=None, cache=0):
        """Internal method which manage load ,loadSerial and loadBefore."""
        cell_list = self._getCellListForOID(oid, readable=True)
        if len(cell_list) == 0:
            # No cells available, so why are we running ?
            logging.error('oid %s not found because no storage is ' \
                    'available for it', dump(oid))
            raise NEOStorageNotFoundError()

        shuffle(cell_list)
        cell_list.sort(key=self.cp.getCellSortKey)
        self.local_var.asked_object = 0
        for cell in cell_list:
            logging.debug('trying to load %s from %s',
                          dump(oid), dump(cell.getUUID()))
            conn = self.cp.getConnForCell(cell)
            if conn is None:
                continue

            try:
                self._askStorage(conn, Packets.AskObject(oid, serial, tid))
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
                              noid, dump(oid), cell.getAddress())
                self.local_var.asked_object = -1
                continue
            elif checksum != makeChecksum(data):
                # Check checksum.
                logging.error('wrong checksum from node %s for oid %s',
                              cell.getAddress(), dump(oid))
                self.local_var.asked_object = -1
                continue
            else:
                # Everything looks alright.
                break

        if self.local_var.asked_object == 0:
            # We didn't got any object from all storage node because of
            # connection error
            logging.warning('oid %s not found because of connection failure',
                    dump(oid))
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
        if data == '':
            data = None
        return data, start_serial, end_serial


    @profiler_decorator
    def load(self, oid, version=None):
        """Load an object for a given oid."""
        # First try from cache
        self._load_lock_acquire()
        try:
            self._cache_lock_acquire()
            try:
                if oid in self.mq_cache:
                    logging.debug('load oid %s is cached', dump(oid))
                    serial, data = self.mq_cache[oid]
                    return data, serial
            finally:
                self._cache_lock_release()
            # Otherwise get it from storage node
            return self._load(oid, cache=1)[:2]
        finally:
            self._load_lock_release()


    @profiler_decorator
    def loadSerial(self, oid, serial):
        """Load an object for a given oid and serial."""
        # Do not try in cache as it manages only up-to-date object
        logging.debug('loading %s at %s', dump(oid), dump(serial))
        return self._load(oid, serial=serial)[0]


    @profiler_decorator
    def loadBefore(self, oid, tid):
        """Load an object for a given oid before tid committed."""
        # Do not try in cache as it manages only up-to-date object
        logging.debug('loading %s before %s', dump(oid), dump(tid))
        data, start, end = self._load(oid, tid=tid)
        if end is None:
            # No previous version
            return None
        else:
            return data, start, end


    @profiler_decorator
    def tpc_begin(self, transaction, tid=None, status=' '):
        """Begin a new transaction."""
        # First get a transaction, only one is allowed at a time
        if self.local_var.txn is transaction:
            # We already begin the same transaction
            return
        if self.local_var.txn is not None:
            raise NeoException, 'local_var is not clean in tpc_begin'
        # ask the primary master to start a transaction, if no tid is supplied,
        # the master will supply us one. Otherwise the requested tid will be
        # used if possible.
        self.local_var.tid = None
        self._askPrimary(Packets.AskBeginTransaction(tid))
        if self.local_var.tid is None:
            raise NEOStorageError('tpc_begin failed')
        self.local_var.txn = transaction


    @profiler_decorator
    def store(self, oid, serial, data, version, transaction):
        """Store object."""
        if transaction is not self.local_var.txn:
            raise StorageTransactionError(self, transaction)
        logging.debug('storing oid %s serial %s',
                     dump(oid), dump(serial))
        # Find which storage node to use
        cell_list = self._getCellListForOID(oid, writable=True)
        if len(cell_list) == 0:
            raise NEOStorageError
        if data is None:
            # this is a George Bailey object, stored as an empty string
            data = ''
        if self.compress:
            compressed_data = compress(data)
            if len(compressed_data) > len(data):
                compressed_data = data
                compression = 0
            else:
                compression = 1
        else:
            compressed_data = data
            compression = 0
        checksum = makeChecksum(compressed_data)
        p = Packets.AskStoreObject(oid, serial, compression,
                 checksum, compressed_data, self.local_var.tid)
        on_timeout = OnTimeout(self.onStoreTimeout, self.local_var.tid, oid)
        # Store object in tmp cache
        self.local_var.data_dict[oid] = data
        # Store data on each node
        self.local_var.object_stored_counter_dict[oid] = {}
        self.local_var.object_serial_dict[oid] = (serial, version)
        getConnForCell = self.cp.getConnForCell
        queue = self.local_var.queue
        add_involved_nodes = self.local_var.involved_nodes.add
        for cell in cell_list:
            conn = getConnForCell(cell)
            if conn is None:
                continue
            try:
                conn.ask(p, on_timeout=on_timeout, queue=queue)
                add_involved_nodes(cell.getNode())
            except ConnectionClosed:
                continue

        self._waitAnyMessage(False)
        return None

    def onStoreTimeout(self, conn, msg_id, tid, oid):
        # NOTE: this method is called from poll thread, don't use
        # local_var !
        # Stop expecting the timed-out store request.
        queue = self.dispatcher.forget(conn, msg_id)
        # Ask the storage if someone locks the object.
        # Shorten timeout to react earlier to an unresponding storage.
        conn.ask(Packets.AskHasLock(tid, oid), timeout=5, queue=queue)
        return True

    @profiler_decorator
    def _handleConflicts(self, tryToResolveConflict):
        result = []
        append = result.append
        local_var = self.local_var
        # Check for conflicts
        data_dict = local_var.data_dict
        object_serial_dict = local_var.object_serial_dict
        conflict_serial_dict = local_var.conflict_serial_dict
        resolved_conflict_serial_dict = local_var.resolved_conflict_serial_dict
        for oid, conflict_serial_set in conflict_serial_dict.items():
            resolved_serial_set = resolved_conflict_serial_dict.setdefault(
                oid, set())
            conflict_serial = max(conflict_serial_set)
            if resolved_serial_set and conflict_serial <= max(resolved_serial_set):
                # A later serial has already been resolved, skip.
                resolved_serial_set.update(conflict_serial_dict.pop(oid))
                continue
            serial, version = object_serial_dict[oid]
            data = data_dict[oid]
            tid = local_var.tid
            resolved = False
            if conflict_serial <= tid:
                new_data = tryToResolveConflict(oid, conflict_serial, serial,
                    data)
                if new_data is not None:
                    logging.info('Conflict resolution succeed for %r:%r with %r',
                        dump(oid), dump(serial), dump(conflict_serial))
                    # Mark this conflict as resolved
                    resolved_serial_set.update(conflict_serial_dict.pop(oid))
                    # Try to store again
                    self.store(oid, conflict_serial, new_data, version,
                        local_var.txn)
                    append(oid)
                    resolved = True
                else:
                    logging.info('Conflict resolution failed for %r:%r with %r',
                        dump(oid), dump(serial), dump(conflict_serial))
            else:
                logging.info('Conflict reported for %r:%r with later ' \
                    'transaction %r , cannot resolve conflict.', dump(oid),
                    dump(serial), dump(conflict_serial))
            if not resolved:
                # XXX: Is it really required to remove from data_dict ?
                del data_dict[oid]
                raise ConflictError(oid=oid,
                    serials=(tid, serial), data=data)
        return result

    @profiler_decorator
    def waitResponses(self):
        """Wait for all requests to be answered (or their connection to be
        dected as closed)"""
        queue = self.local_var.queue
        pending = self.dispatcher.pending
        _waitAnyMessage = self._waitAnyMessage
        while pending(queue):
            _waitAnyMessage()

    @profiler_decorator
    def waitStoreResponses(self, tryToResolveConflict):
        result = []
        append = result.append
        resolved_oid_set = set()
        update = resolved_oid_set.update
        local_var = self.local_var
        tid = local_var.tid
        _handleConflicts = self._handleConflicts
        while True:
            self.waitResponses()
            conflicts = _handleConflicts(tryToResolveConflict)
            if conflicts:
                update(conflicts)
            else:
                # No more conflict resolutions to do, no more pending store
                # requests
                break

        # Check for never-stored objects, and update result for all others
        for oid, store_dict in \
            local_var.object_stored_counter_dict.iteritems():
            if not store_dict:
                raise NEOStorageError('tpc_store failed')
            elif oid in resolved_oid_set:
                append((oid, ResolvedSerial))
            else:
                append((oid, tid))
        return result

    @profiler_decorator
    def tpc_vote(self, transaction, tryToResolveConflict):
        """Store current transaction."""
        local_var = self.local_var
        if transaction is not local_var.txn:
            raise StorageTransactionError(self, transaction)

        result = self.waitStoreResponses(tryToResolveConflict)

        tid = local_var.tid
        # Store data on each node
        voted_counter = 0
        p = Packets.AskStoreTransaction(tid, str(transaction.user),
            str(transaction.description), dumps(transaction._extension),
            local_var.data_dict.keys())
        add_involved_nodes = self.local_var.involved_nodes.add
        for cell in self._getCellListForTID(tid, writable=True):
            logging.debug("voting object %s %s", cell.getAddress(),
                cell.getState())
            conn = self.cp.getConnForCell(cell)
            if conn is None:
                continue

            local_var.txn_voted = False
            try:
                self._askStorage(conn, p)
                add_involved_nodes(cell.getNode())
            except ConnectionClosed:
                continue

            if not self.isTransactionVoted():
                raise NEOStorageError('tpc_vote failed')
            voted_counter += 1

        # check at least one storage node accepted
        if voted_counter == 0:
            raise NEOStorageError('tpc_vote failed')
        # Check if master connection is still alive.
        # This is just here to lower the probability of detecting a problem
        # in tpc_finish, as we should do our best to detect problem before
        # tpc_finish.
        self._getMasterConnection()

        return result

    @profiler_decorator
    def tpc_abort(self, transaction):
        """Abort current transaction."""
        if transaction is not self.local_var.txn:
            return

        tid = self.local_var.tid
        p = Packets.AbortTransaction(tid)
        getConnForNode = self.cp.getConnForNode
        # cancel transaction one all those nodes
        for node in self.local_var.involved_nodes:
            conn = getConnForNode(node)
            if conn is None:
                continue
            try:
                conn.notify(p)
            except:
                logging.error('Exception in tpc_abort while notifying ' \
                    'storage node %r of abortion, ignoring.', conn, exc_info=1)

        # Abort the transaction in the primary master node.
        conn = self._getMasterConnection()
        try:
            conn.notify(p)
        except:
            logging.error('Exception in tpc_abort while notifying master ' \
                'node %r of abortion, ignoring.', conn, exc_info=1)

        # Just wait for responses to arrive. If any leads to an exception,
        # log it and continue: we *must* eat all answers to not disturb the
        # next transaction.
        queue = self.local_var.queue
        pending = self.dispatcher.pending
        _waitAnyMessage = self._waitAnyMessage
        while pending(queue):
            try:
                _waitAnyMessage()
            except:
                logging.error('Exception in tpc_abort while handling ' \
                    'pending answers, ignoring.', exc_info=1)

        self.local_var.clear()

    @profiler_decorator
    def tpc_finish(self, transaction, f=None):
        """Finish current transaction."""
        if self.local_var.txn is not transaction:
            return
        self._load_lock_acquire()
        try:
            tid = self.local_var.tid
            # Call function given by ZODB
            if f is not None:
                f(tid)

            # Call finish on master
            oid_list = self.local_var.data_dict.keys()
            p = Packets.AskFinishTransaction(tid, oid_list)
            self._askPrimary(p)

            if not self.isTransactionFinished():
                raise NEOStorageError('tpc_finish failed')

            # Update cache
            self._cache_lock_acquire()
            try:
                mq_cache = self.mq_cache
                for oid, data in self.local_var.data_dict.iteritems():
                    if data == '':
                        if oid in mq_cache:
                            del mq_cache[oid]
                    else:
                        # Now serial is same as tid
                        mq_cache[oid] = tid, data
            finally:
                self._cache_lock_release()
            self.local_var.clear()
            return tid
        finally:
            self._load_lock_release()

    def undo(self, undone_tid, txn, tryToResolveConflict):
        if txn is not self.local_var.txn:
            raise StorageTransactionError(self, undone_tid)

        # First get transaction information from a storage node.
        cell_list = self._getCellListForTID(undone_tid, readable=True)
        assert len(cell_list), 'No cell found for transaction %s' % (
            dump(undone_tid), )
        shuffle(cell_list)
        cell_list.sort(key=self.cp.getCellSortKey)
        for cell in cell_list:
            conn = self.cp.getConnForCell(cell)
            if conn is None:
                continue

            self.local_var.txn_info = 0
            self.local_var.txn_ext = 0
            try:
                self._askStorage(conn, Packets.AskTransactionInformation(
                    undone_tid))
            except ConnectionClosed:
                continue

            if self.local_var.txn_info == -1:
                # Tid not found, try with next node
                logging.warning('Transaction %s was not found on node %s',
                    dump(undone_tid), self.nm.getByAddress(conn.getAddress()))
                continue
            elif isinstance(self.local_var.txn_info, dict):
                break
            else:
                raise NEOStorageError('undo failed')
        else:
            raise NEOStorageError('undo failed')

        if self.local_var.txn_info['packed']:
            UndoError('non-undoable transaction')

        tid = self.local_var.tid

        undo_conflict_oid_list = self.local_var.undo_conflict_oid_list = []
        undo_error_oid_list = self.local_var.undo_error_oid_list = []
        ask_undo_transaction = Packets.AskUndoTransaction(tid, undone_tid)
        getConnForNode = self.cp.getConnForNode
        queue = self.local_var.queue
        for storage_node in self.nm.getStorageList():
            storage_conn = getConnForNode(storage_node)
            storage_conn.ask(ask_undo_transaction, queue=queue)
        # Wait for all AnswerUndoTransaction.
        self.waitResponses()

        # Don't do any handling for "live" conflicts, raise
        if undo_conflict_oid_list:
            raise ConflictError(oid=undo_conflict_oid_list[0], serials=(tid,
                undone_tid), data=None)

        # Try to resolve undo conflicts
        for oid in undo_error_oid_list:
            def loadBefore(oid, tid):
                try:
                    result = self._load(oid, tid=tid)
                except NEOStorageNotFoundError:
                    raise UndoError("Object not found while resolving undo " \
                        "conflict")
                return result[:2]
            # Load the latest version we are supposed to see
            data, data_tid = loadBefore(oid, tid)
            # Load the version we were undoing to
            undo_data, _ = loadBefore(oid, undone_tid)
            # Resolve conflict
            new_data = tryToResolveConflict(oid, data_tid, undone_tid, undo_data,
                data)
            if new_data is None:
                raise UndoError('Some data were modified by a later ' \
                    'transaction', oid)
            else:
                self.store(oid, data_tid, new_data, '', self.local_var.txn)

        oid_list = self.local_var.txn_info['oids']
        # Consistency checking: all oids of the transaction must have been
        # reported as undone
        data_dict = self.local_var.data_dict
        for oid in oid_list:
            assert oid in data_dict, repr(oid)
        return self.local_var.tid, oid_list

    def _insertMetadata(self, txn_info, extension):
        for k, v in loads(extension).items():
            txn_info[k] = v

    def __undoLog(self, first, last, filter=None, block=0, with_oids=False):
        if last < 0:
            # See FileStorage.py for explanation
            last = first - last

        # First get a list of transactions from all storage nodes.
        # Each storage node will return TIDs only for UP_TO_DATE state and
        # FEEDING state cells
        pt = self._getPartitionTable()
        storage_node_list = pt.getNodeList()

        self.local_var.node_tids = {}
        queue = self.local_var.queue
        for storage_node in storage_node_list:
            conn = self.cp.getConnForNode(storage_node)
            if conn is None:
                continue
            conn.ask(Packets.AskTIDs(first, last, INVALID_PARTITION), queue=queue)

        # Wait for answers from all storages.
        while len(self.local_var.node_tids) != len(storage_node_list):
            self._waitAnyMessage()

        # Reorder tids
        ordered_tids = set()
        update = ordered_tids.update
        for tid_list in self.local_var.node_tids.itervalues():
            update(tid_list)
        ordered_tids = list(ordered_tids)
        ordered_tids.sort(reverse=True)
        logging.debug("UndoLog, tids %s", ordered_tids)
        # For each transaction, get info
        undo_info = []
        append = undo_info.append
        for tid in ordered_tids:
            cell_list = self._getCellListForTID(tid, readable=True)
            shuffle(cell_list)
            cell_list.sort(key=self.cp.getCellSortKey)
            for cell in cell_list:
                conn = self.cp.getConnForCell(cell)
                if conn is not None:
                    self.local_var.txn_info = 0
                    self.local_var.txn_ext = 0
                    try:
                        self._askStorage(conn,
                                Packets.AskTransactionInformation(tid))
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
                self.local_var.txn_info.pop('packed')
                if not with_oids:
                    self.local_var.txn_info.pop("oids")
                append(self.local_var.txn_info)
                self._insertMetadata(self.local_var.txn_info,
                        self.local_var.txn_ext)
                if len(undo_info) >= last - first:
                    break
        # Check we return at least one element, otherwise call
        # again but extend offset
        if len(undo_info) == 0 and not block:
            undo_info = self.__undoLog(first=first, last=last*5, filter=filter,
                    block=1, with_oids=with_oids)
        return undo_info

    def undoLog(self, first, last, filter=None, block=0):
        return self.__undoLog(first, last, filter, block)

    def transactionLog(self, first, last):
        return self.__undoLog(first, last, with_oids=True)

    def history(self, oid, version=None, size=1, filter=None, object_only=0):
        # Get history informations for object first
        cell_list = self._getCellListForOID(oid, readable=True)
        shuffle(cell_list)
        cell_list.sort(key=self.cp.getCellSortKey)
        for cell in cell_list:
            conn = self.cp.getConnForCell(cell)
            if conn is None:
                continue

            self.local_var.history = None
            try:
                self._askStorage(conn, Packets.AskObjectHistory(oid, 0, size))
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

        if self.local_var.history[1] == [] or \
            self.local_var.history[1][0][1] == 0:
            # KeyError expected if no history was found
            # XXX: this may requires an error from the storages
            raise KeyError

        if object_only:
            # Use by getSerial
            return self.local_var.history

        # Now that we have object informations, get txn informations
        history_list = []
        for serial, size in self.local_var.history[1]:
            self._getCellListForTID(serial, readable=True)
            shuffle(cell_list)
            cell_list.sort(key=self.cp.getCellSortKey)
            for cell in cell_list:
                conn = self.cp.getConnForCell(cell)
                if conn is None:
                    continue

                # ask transaction information
                self.local_var.txn_info = None
                try:
                    self._askStorage(conn,
                            Packets.AskTransactionInformation(serial))
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
            self.local_var.txn_info.pop('packed')
            self.local_var.txn_info['tid'] = serial
            self.local_var.txn_info['version'] = ''
            self.local_var.txn_info['size'] = size
            if filter is None or filter(self.local_var.txn_info):
                history_list.append(self.local_var.txn_info)
            self._insertMetadata(self.local_var.txn_info,
                    self.local_var.txn_ext)

        return history_list

    @profiler_decorator
    def importFrom(self, source, start, stop, tryToResolveConflict):
        serials = {}
        def updateLastSerial(oid, result):
            if result:
                if isinstance(result, str):
                    assert oid is not None
                    serials[oid] = result
                else:
                    for oid, serial in result:
                        assert isinstance(serial, str), serial
                        serials[oid] = serial
        transaction_iter = source.iterator(start, stop)
        for transaction in transaction_iter:
            self.tpc_begin(transaction, transaction.tid, transaction.status)
            for r in transaction:
                pre = serials.get(r.oid, None)
                # TODO: bypass conflict resolution, locks...
                result = self.store(r.oid, pre, r.data, r.version, transaction)
                updateLastSerial(r.oid, result)
            updateLastSerial(None, self.tpc_vote(transaction,
                        tryToResolveConflict))
            self.tpc_finish(transaction)
        transaction_iter.close()

    def iterator(self, start=None, stop=None):
        return Iterator(self, start, stop)

    def lastTransaction(self):
        # XXX: this doesn't consider transactions created by other clients,
        #  should ask the primary master
        return self.local_var.tid

    def abortVersion(self, src, transaction):
        if transaction is not self.local_var.txn:
            raise StorageTransactionError(self, transaction)
        return '', []

    def commitVersion(self, src, dest, transaction):
        if transaction is not self.local_var.txn:
            raise StorageTransactionError(self, transaction)
        return '', []

    def loadEx(self, oid, version):
        data, serial = self.load(oid=oid)
        return data, serial, ''

    def __del__(self):
        """Clear all connection."""
        # Due to bug in ZODB, close is not always called when shutting
        # down zope, so use __del__ to close connections
        for conn in self.em.getConnectionList():
            conn.close()
        # Stop polling thread
        self.poll_thread.stop()
    close = __del__

    def sync(self):
        self._waitAnyMessage(False)

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

    def setTransactionFinished(self):
        self.local_var.txn_finished = True

    def isTransactionFinished(self):
        return self.local_var.txn_finished

    def setTransactionVoted(self):
        self.local_var.txn_voted = True

    def isTransactionVoted(self):
        return self.local_var.txn_voted

