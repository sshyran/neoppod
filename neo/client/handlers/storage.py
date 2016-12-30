#
# Copyright (C) 2006-2017  Nexedi SA
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
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from ZODB.TimeStamp import TimeStamp

from neo.lib import logging
from neo.lib.protocol import ZERO_TID
from neo.lib.util import dump
from neo.lib.exception import NodeNotReady
from neo.lib.handler import MTEventHandler
from . import AnswerBaseHandler
from ..exception import NEOStorageError, NEOStorageNotFoundError
from ..exception import NEOStorageDoesNotExistError

class StorageEventHandler(MTEventHandler):

    def connectionLost(self, conn, new_state):
        node = self.app.nm.getByAddress(conn.getAddress())
        assert node is not None
        self.app.cp.removeConnection(node)
        self.app.dispatcher.unregister(conn)

    def connectionFailed(self, conn):
        # Connection to a storage node failed
        node = self.app.nm.getByAddress(conn.getAddress())
        assert node is not None
        self.app.cp.removeConnection(node)
        super(StorageEventHandler, self).connectionFailed(conn)

    def _acceptIdentification(self, node,
           uuid, num_partitions, num_replicas, your_uuid, primary,
           master_list):
        assert self.app.master_conn is None or \
          primary == self.app.master_conn.getAddress(), (
            primary, self.app.master_conn)
        assert uuid == node.getUUID(), (uuid, node.getUUID())

class StorageBootstrapHandler(AnswerBaseHandler):
    """ Handler used when connecting to a storage node """

    def notReady(self, conn, message):
        conn.close()
        raise NodeNotReady(message)

class StorageAnswersHandler(AnswerBaseHandler):
    """ Handle all messages related to ZODB operations """

    def answerObject(self, conn, oid, *args):
        self.app.setHandlerData(args)

    def answerStoreObject(self, conn, conflict, oid, serial):
        txn_context = self.app.getHandlerData()
        object_stored_counter_dict = txn_context[
            'object_stored_counter_dict'][oid]
        if conflict:
            # Warning: if a storage (S1) is much faster than another (S2), then
            # we may process entirely a conflict with S1 (i.e. we received the
            # answer to the store of the resolved object on S1) before we
            # receive the conflict answer from the first store on S2.
            logging.info('%r report a conflict for %r with %r',
                         conn, dump(oid), dump(conflict))
            # If this conflict is not already resolved, mark it for
            # resolution.
            if conflict not in txn_context[
                    'resolved_conflict_serial_dict'].get(oid, ()):
                if ZERO_TID != conflict in object_stored_counter_dict:
                    raise NEOStorageError('Storages %s accepted object %s'
                        ' for serial %s but %s reports a conflict for it.' % (
                        map(dump, object_stored_counter_dict[conflict]),
                        dump(oid), dump(conflict), dump(conn.getUUID())))
                conflict_serial_dict = txn_context['conflict_serial_dict']
                conflict_serial_dict.setdefault(oid, set()).add(conflict)
        else:
            uuid_set = object_stored_counter_dict.get(serial)
            if uuid_set is None: # store to first storage node
                object_stored_counter_dict[serial] = uuid_set = set()
                try:
                    data = txn_context['data_dict'].pop(oid)
                except KeyError: # multiple undo
                    assert txn_context['cache_dict'][oid] is None, oid
                else:
                    if type(data) is str:
                        size = len(data)
                        txn_context['data_size'] -= size
                        size += txn_context['cache_size']
                        if size < self.app._cache._max_size:
                            txn_context['cache_size'] = size
                        else:
                            # Do not cache data past cache max size, as it
                            # would just flush it on tpc_finish. This also
                            # prevents memory errors for big transactions.
                            data = None
                    txn_context['cache_dict'][oid] = data
            else: # replica
                assert oid not in txn_context['data_dict'], oid
            uuid_set.add(conn.getUUID())

    answerCheckCurrentSerial = answerStoreObject

    def answerStoreTransaction(self, conn):
        pass

    answerVoteTransaction = answerStoreTransaction

    def answerTIDsFrom(self, conn, tid_list):
        logging.debug('Get %u TIDs from %r', len(tid_list), conn)
        self.app.setHandlerData(tid_list)

    def answerTransactionInformation(self, conn, tid,
                                           user, desc, ext, packed, oid_list):
        self.app.setHandlerData(({
            'time': TimeStamp(tid).timeTime(),
            'user_name': user,
            'description': desc,
            'id': tid,
            'oids': oid_list,
            'packed': packed,
        }, ext))

    def answerObjectHistory(self, conn, _, history_list):
        # history_list is a list of tuple (serial, size)
        self.app.setHandlerData(history_list)

    def oidNotFound(self, conn, message):
        # This can happen either when :
        # - loading an object
        # - asking for history
        raise NEOStorageNotFoundError(message)

    def oidDoesNotExist(self, conn, message):
        raise NEOStorageDoesNotExistError(message)

    def tidNotFound(self, conn, message):
        # This can happen when requiring txn informations
        raise NEOStorageNotFoundError(message)

    def answerTIDs(self, conn, tid_list, tid_set):
        tid_set.update(tid_list)

    def answerObjectUndoSerial(self, conn, object_tid_dict,
                               undo_object_tid_dict):
        undo_object_tid_dict.update(object_tid_dict)

    def answerFinalTID(self, conn, tid):
        self.app.setHandlerData(tid)
