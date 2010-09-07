#
# Copyright (C) 2009-2010  Nexedi SA
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

import unittest
import MySQLdb
from mock import Mock
from neo.util import dump, p64, u64
from neo.protocol import CellStates, INVALID_PTID, ZERO_OID, ZERO_TID, MAX_TID
from neo.tests import NeoTestBase
from neo.exception import DatabaseFailure
from neo.storage.database.mysqldb import MySQLDatabaseManager

NEO_SQL_DATABASE = 'test_mysqldb0'
NEO_SQL_USER = 'test'

class StorageMySQSLdbTests(NeoTestBase):

    def setUp(self):
        self.prepareDatabase(number=1, prefix=NEO_SQL_DATABASE[:-1])
        # db manager
        database = '%s@%s' % (NEO_SQL_USER, NEO_SQL_DATABASE)
        self.db = MySQLDatabaseManager(database)
        self.db.setup()

    def tearDown(self):
        self.db.close()

    def checkCalledQuery(self, query=None, call=0):
        self.assertTrue(len(self.db.conn.mockGetNamedCalls('query')) > call)
        call = self.db.conn.mockGetNamedCalls('query')[call]
        call.checkArgs('BEGIN')

    def test_MySQLDatabaseManagerInit(self):
        db = MySQLDatabaseManager('%s@%s' % (NEO_SQL_USER, NEO_SQL_DATABASE))
        # init
        self.assertEquals(db.db, NEO_SQL_DATABASE)
        self.assertEquals(db.user, NEO_SQL_USER)
        # & connect
        self.assertTrue(isinstance(db.conn, MySQLdb.connection))
        self.assertEquals(db.isUnderTransaction(), False)

    def test_begin(self):
        # no current transaction
        self.db.conn = Mock({ })
        self.assertEquals(self.db.isUnderTransaction(), False)
        self.db.begin()
        self.checkCalledQuery(query='COMMIT')
        self.assertEquals(self.db.isUnderTransaction(), True)

    def test_commit(self):
        self.db.conn = Mock()
        self.db.begin()
        self.db.commit()
        self.assertEquals(len(self.db.conn.mockGetNamedCalls('commit')), 1)
        self.assertEquals(self.db.isUnderTransaction(), False)

    def test_rollback(self):
        # rollback called and no current transaction
        self.db.conn = Mock({ })
        self.db.under_transaction = True
        self.db.rollback()
        self.assertEquals(len(self.db.conn.mockGetNamedCalls('rollback')), 1)
        self.assertEquals(self.db.isUnderTransaction(), False)

    def test_query1(self):
        # fake result object
        from array import array
        result_object = Mock({
            "num_rows": 1,
            "fetch_row": ((1, 2, array('b', (1, 2, ))), ),
        })
        # expected formatted result
        expected_result = (
            (1, 2, '\x01\x02', ),
        )
        self.db.conn = Mock({ 'store_result': result_object })
        result = self.db.query('QUERY')
        self.assertEquals(result, expected_result)
        calls = self.db.conn.mockGetNamedCalls('query')
        self.assertEquals(len(calls), 1)
        calls[0].checkArgs('QUERY')

    def test_query2(self):
        # test the OperationalError exception
        # fake object, raise exception during the first call
        from MySQLdb import OperationalError
        from MySQLdb.constants.CR import SERVER_GONE_ERROR
        class FakeConn(object):
            def query(*args):
                raise OperationalError(SERVER_GONE_ERROR, 'this is a test')
        self.db.conn = FakeConn()
        self.connect_called = False
        def connect_hook():
            # mock object, break raise/connect loop
            self.db.conn = Mock({'num_rows': 0})
            self.connect_called = True
        self.db._connect = connect_hook
        # make a query, exception will be raised then connect() will be
        # called and the second query will use the mock object
        self.db.query('QUERY')
        self.assertTrue(self.connect_called)

    def test_query3(self):
        # OperationalError > raise DatabaseFailure exception
        from MySQLdb import OperationalError
        class FakeConn(object):
            def close(self):
                pass
            def query(*args):
                raise OperationalError(-1, 'this is a test')
        self.db.conn = FakeConn()
        self.assertRaises(DatabaseFailure, self.db.query, 'QUERY')

    def test_escape(self):
        self.assertEquals(self.db.escape('a"b'), 'a\\"b')
        self.assertEquals(self.db.escape("a'b"), "a\\'b")

    def test_setup(self):
        # create all tables
        self.db.conn = Mock()
        self.db.setup()
        calls = self.db.conn.mockGetNamedCalls('query')
        self.assertEquals(len(calls), 6)
        # create all tables but drop them first
        self.db.conn = Mock()
        self.db.setup(reset=True)
        calls = self.db.conn.mockGetNamedCalls('query')
        self.assertEquals(len(calls), 7)

    def test_configuration(self):
        # check if a configuration entry is well written
        self.db.setConfiguration('a', 'c')
        result = self.db.getConfiguration('a')
        self.assertEquals(result, 'c')

    def checkConfigEntry(self, get_call, set_call, value):
        # generic test for all configuration entries accessors
        self.assertRaises(KeyError, get_call)
        set_call(value)
        self.assertEquals(get_call(), value)
        set_call(value * 2)
        self.assertEquals(get_call(), value * 2)

    def test_UUID(self):
        self.checkConfigEntry(self.db.getUUID, self.db.setUUID, 'TEST_VALUE')

    def test_NumPartitions(self):
        self.checkConfigEntry(self.db.getNumPartitions,
                self.db.setNumPartitions, 10)

    def test_Name(self):
        self.checkConfigEntry(self.db.getName, self.db.setName, 'TEST_NAME')

    def test_15_PTID(self):
        self.checkConfigEntry(
            get_call=self.db.getPTID,
            set_call=self.db.setPTID,
            value=self.getPTID(1))

    def test_getPartitionTable(self):
        ptid = self.getPTID(1)
        uuid1, uuid2 = self.getNewUUID(), self.getNewUUID()
        cell1 = (0, uuid1, CellStates.OUT_OF_DATE)
        cell2 = (1, uuid1, CellStates.UP_TO_DATE)
        self.db.setPartitionTable(ptid, [cell1, cell2])
        result = self.db.getPartitionTable()
        self.assertEqual(set(result), set([cell1, cell2]))

    def test_getLastOID(self):
        oid1 = self.getOID(1)
        self.db.setLastOID(oid1)
        result1 = self.db.getLastOID()
        self.assertEquals(result1, oid1)

    def getOIDs(self, count):
        return [self.getOID(i) for i in xrange(count)]

    def getTIDs(self, count):
        tid_list = [self.getNextTID()]
        while len(tid_list) != count:
            tid_list.append(self.getNextTID(tid_list[-1]))
        return tid_list

    def getTransaction(self, oid_list):
        transaction = (oid_list, 'user', 'desc', 'ext', False)
        object_list = [(oid, 1, 0, '', None) for oid in oid_list]
        return (transaction, object_list)

    def checkSet(self, list1, list2):
        self.assertEqual(set(list1), set(list2))

    def test_getLastTID(self):
        tid1, tid2, tid3, tid4 = self.getTIDs(4)
        oid1, oid2 = self.getOIDs(2)
        txn, objs = self.getTransaction([oid1, oid2])
        # max TID is in obj table
        self.db.storeTransaction(tid1, objs, txn, False)
        self.db.storeTransaction(tid2, objs, txn, False)
        self.assertEqual(self.db.getLastTID(), tid2)
        # max tid is in ttrans table
        self.db.storeTransaction(tid3, objs, txn)
        result = self.db.getLastTID()
        self.assertEqual(self.db.getLastTID(), tid3)
        # max tid is in tobj (serial)
        self.db.storeTransaction(tid4, objs, None)
        self.assertEqual(self.db.getLastTID(), tid4)

    def test_getUnfinishedTIDList(self):
        tid1, tid2, tid3, tid4 = self.getTIDs(4)
        oid1, oid2 = self.getOIDs(2)
        txn, objs = self.getTransaction([oid1, oid2])
        # nothing pending
        self.db.storeTransaction(tid1, objs, txn, False)
        self.checkSet(self.db.getUnfinishedTIDList(), [])
        # one unfinished txn
        self.db.storeTransaction(tid2, objs, txn)
        self.checkSet(self.db.getUnfinishedTIDList(), [tid2])
        # no changes
        self.db.storeTransaction(tid3, objs, None, False)
        self.checkSet(self.db.getUnfinishedTIDList(), [tid2])
        # a second txn known by objs only
        self.db.storeTransaction(tid4, objs, None)
        self.checkSet(self.db.getUnfinishedTIDList(), [tid2, tid4])

    def test_objectPresent(self):
        tid = self.getNextTID()
        oid = self.getOID(1)
        txn, objs = self.getTransaction([oid])
        # not present
        self.assertFalse(self.db.objectPresent(oid, tid, all=True))
        self.assertFalse(self.db.objectPresent(oid, tid, all=False))
        # available in temp table
        self.db.storeTransaction(tid, objs, txn)
        self.assertTrue(self.db.objectPresent(oid, tid, all=True))
        self.assertFalse(self.db.objectPresent(oid, tid, all=False))
        # available in both tables
        self.db.finishTransaction(tid)
        self.assertTrue(self.db.objectPresent(oid, tid, all=True))
        self.assertTrue(self.db.objectPresent(oid, tid, all=False))

    def test_getObject(self):
        oid1, = self.getOIDs(1)
        tid1, tid2 = self.getTIDs(2)
        FOUND_BUT_NOT_VISIBLE = False
        OBJECT_T1_NO_NEXT = (tid1, None, 1, 0, '', None)
        OBJECT_T1_NEXT = (tid1, tid2, 1, 0, '', None)
        OBJECT_T2 = (tid2, None, 1, 0, '', None)
        txn1, objs1 = self.getTransaction([oid1])
        txn2, objs2 = self.getTransaction([oid1])
        # non-present
        self.assertEqual(self.db.getObject(oid1), None)
        self.assertEqual(self.db.getObject(oid1, tid1), None)
        self.assertEqual(self.db.getObject(oid1, before_tid=tid1), None)
        # one non-commited version
        self.db.storeTransaction(tid1, objs1, txn1)
        self.assertEqual(self.db.getObject(oid1), None)
        self.assertEqual(self.db.getObject(oid1, tid1), None)
        self.assertEqual(self.db.getObject(oid1, before_tid=tid1), None)
        # one commited version
        self.db.finishTransaction(tid1)
        self.assertEqual(self.db.getObject(oid1), OBJECT_T1_NO_NEXT)
        self.assertEqual(self.db.getObject(oid1, tid1), OBJECT_T1_NO_NEXT)
        self.assertEqual(self.db.getObject(oid1, before_tid=tid1),
            FOUND_BUT_NOT_VISIBLE)
        # two version available, one non-commited
        self.db.storeTransaction(tid2, objs2, txn2)
        self.assertEqual(self.db.getObject(oid1), OBJECT_T1_NO_NEXT)
        self.assertEqual(self.db.getObject(oid1, tid1), OBJECT_T1_NO_NEXT)
        self.assertEqual(self.db.getObject(oid1, before_tid=tid1),
            FOUND_BUT_NOT_VISIBLE)
        self.assertEqual(self.db.getObject(oid1, tid2), FOUND_BUT_NOT_VISIBLE)
        self.assertEqual(self.db.getObject(oid1, before_tid=tid2),
            OBJECT_T1_NO_NEXT)
        # two commited versions
        self.db.finishTransaction(tid2)
        self.assertEqual(self.db.getObject(oid1), OBJECT_T2)
        self.assertEqual(self.db.getObject(oid1, tid1), OBJECT_T1_NO_NEXT)
        self.assertEqual(self.db.getObject(oid1, before_tid=tid1),
            FOUND_BUT_NOT_VISIBLE)
        self.assertEqual(self.db.getObject(oid1, tid2), OBJECT_T2)
        self.assertEqual(self.db.getObject(oid1, before_tid=tid2),
            OBJECT_T1_NEXT)

    def test_setPartitionTable(self):
        ptid = self.getPTID(1)
        uuid1, uuid2 = self.getNewUUID(), self.getNewUUID()
        cell1 = (0, uuid1, CellStates.OUT_OF_DATE)
        cell2 = (1, uuid1, CellStates.UP_TO_DATE)
        cell3 = (1, uuid1, CellStates.DISCARDED)
        # no partition table
        self.assertEqual(self.db.getPartitionTable(), [])
        # set one
        self.db.setPartitionTable(ptid, [cell1])
        result = self.db.getPartitionTable()
        self.assertEqual(result, [cell1])
        # then another
        self.db.setPartitionTable(ptid, [cell2])
        result = self.db.getPartitionTable()
        self.assertEqual(result, [cell2])
        # drop discarded cells
        self.db.setPartitionTable(ptid, [cell2, cell3])
        result = self.db.getPartitionTable()
        self.assertEqual(result, [])

    def test_changePartitionTable(self):
        ptid = self.getPTID(1)
        uuid1, uuid2 = self.getNewUUID(), self.getNewUUID()
        cell1 = (0, uuid1, CellStates.OUT_OF_DATE)
        cell2 = (1, uuid1, CellStates.UP_TO_DATE)
        cell3 = (1, uuid1, CellStates.DISCARDED)
        # no partition table
        self.assertEqual(self.db.getPartitionTable(), [])
        # set one
        self.db.changePartitionTable(ptid, [cell1])
        result = self.db.getPartitionTable()
        self.assertEqual(result, [cell1])
        # add more entries
        self.db.changePartitionTable(ptid, [cell2])
        result = self.db.getPartitionTable()
        self.assertEqual(set(result), set([cell1, cell2]))
        # drop discarded cells
        self.db.changePartitionTable(ptid, [cell2, cell3])
        result = self.db.getPartitionTable()
        self.assertEqual(result, [cell1])

    def test_dropUnfinishedData(self):
        oid1, oid2 = self.getOIDs(2)
        tid1, tid2 = self.getTIDs(2)
        txn1, objs1 = self.getTransaction([oid1])
        txn2, objs2 = self.getTransaction([oid1])
        # nothing
        self.assertEqual(self.db.getObject(oid1), None)
        self.assertEqual(self.db.getObject(oid2), None)
        self.assertEqual(self.db.getUnfinishedTIDList(), [])
        # one is still pending
        self.db.storeTransaction(tid1, objs1, txn1)
        self.db.storeTransaction(tid2, objs2, txn2)
        self.db.finishTransaction(tid1)
        result = self.db.getObject(oid1)
        self.assertEqual(result, (tid1, None, 1, 0, '', None))
        self.assertEqual(self.db.getObject(oid2), None)
        self.assertEqual(self.db.getUnfinishedTIDList(), [tid2])
        # drop it
        self.db.dropUnfinishedData()
        self.assertEqual(self.db.getUnfinishedTIDList(), [])
        result = self.db.getObject(oid1)
        self.assertEqual(result, (tid1, None, 1, 0, '', None))
        self.assertEqual(self.db.getObject(oid2), None)

    def test_storeTransaction(self):
        oid1, oid2 = self.getOIDs(2)
        tid1, tid2 = self.getTIDs(2)
        txn1, objs1 = self.getTransaction([oid1])
        txn2, objs2 = self.getTransaction([oid2])
        # nothing in database
        self.assertEqual(self.db.getLastTID(), None)
        self.assertEqual(self.db.getUnfinishedTIDList(), [])
        self.assertEqual(self.db.getObject(oid1), None)
        self.assertEqual(self.db.getObject(oid2), None)
        self.assertEqual(self.db.getTransaction(tid1, True), None)
        self.assertEqual(self.db.getTransaction(tid2, True), None)
        self.assertEqual(self.db.getTransaction(tid1, False), None)
        self.assertEqual(self.db.getTransaction(tid2, False), None)
        # store in temporary tables
        self.db.storeTransaction(tid1, objs1, txn1)
        self.db.storeTransaction(tid2, objs2, txn2)
        result = self.db.getTransaction(tid1, True)
        self.assertEqual(result, ([oid1], 'user', 'desc', 'ext', False))
        result = self.db.getTransaction(tid2, True)
        self.assertEqual(result, ([oid2], 'user', 'desc', 'ext', False))
        self.assertEqual(self.db.getTransaction(tid1, False), None)
        self.assertEqual(self.db.getTransaction(tid2, False), None)
        # commit pending transaction
        self.db.finishTransaction(tid1)
        self.db.finishTransaction(tid2)
        result = self.db.getTransaction(tid1, True)
        self.assertEqual(result, ([oid1], 'user', 'desc', 'ext', False))
        result = self.db.getTransaction(tid2, True)
        self.assertEqual(result, ([oid2], 'user', 'desc', 'ext', False))
        result = self.db.getTransaction(tid1, False)
        self.assertEqual(result, ([oid1], 'user', 'desc', 'ext', False))
        result = self.db.getTransaction(tid2, False)
        self.assertEqual(result, ([oid2], 'user', 'desc', 'ext', False))

    def test_askFinishTransaction(self):
        oid1, oid2 = self.getOIDs(2)
        tid1, tid2 = self.getTIDs(2)
        txn1, objs1 = self.getTransaction([oid1])
        txn2, objs2 = self.getTransaction([oid2])
        # stored but not finished
        self.db.storeTransaction(tid1, objs1, txn1)
        self.db.storeTransaction(tid2, objs2, txn2)
        result = self.db.getTransaction(tid1, True)
        self.assertEqual(result, ([oid1], 'user', 'desc', 'ext', False))
        result = self.db.getTransaction(tid2, True)
        self.assertEqual(result, ([oid2], 'user', 'desc', 'ext', False))
        self.assertEqual(self.db.getTransaction(tid1, False), None)
        self.assertEqual(self.db.getTransaction(tid2, False), None)
        # stored and finished
        self.db.finishTransaction(tid1)
        self.db.finishTransaction(tid2)
        result = self.db.getTransaction(tid1, True)
        self.assertEqual(result, ([oid1], 'user', 'desc', 'ext', False))
        result = self.db.getTransaction(tid2, True)
        self.assertEqual(result, ([oid2], 'user', 'desc', 'ext', False))
        result = self.db.getTransaction(tid1, False)
        self.assertEqual(result, ([oid1], 'user', 'desc', 'ext', False))
        result = self.db.getTransaction(tid2, False)
        self.assertEqual(result, ([oid2], 'user', 'desc', 'ext', False))

    def test_deleteTransaction(self):
        oid1, oid2 = self.getOIDs(2)
        tid1, tid2 = self.getTIDs(2)
        txn1, objs1 = self.getTransaction([oid1])
        txn2, objs2 = self.getTransaction([oid2])
        # delete only from temporary tables
        self.db.storeTransaction(tid1, objs1, txn1)
        self.db.storeTransaction(tid2, objs2, txn2)
        self.db.finishTransaction(tid1)
        self.db.deleteTransaction(tid1)
        self.db.deleteTransaction(tid2)
        result = self.db.getTransaction(tid1, True)
        self.assertEqual(result, ([oid1], 'user', 'desc', 'ext', False))
        self.assertEqual(self.db.getTransaction(tid2, True), None)
        # delete from all
        self.db.deleteTransaction(tid1, True)
        self.assertEqual(self.db.getTransaction(tid1, True), None)
        self.assertEqual(self.db.getTransaction(tid2, True), None)

    def test_deleteObject(self):
        oid1, oid2 = self.getOIDs(2)
        tid1, tid2 = self.getTIDs(2)
        txn1, objs1 = self.getTransaction([oid1, oid2])
        txn2, objs2 = self.getTransaction([oid1, oid2])
        self.db.storeTransaction(tid1, objs1, txn1)
        self.db.storeTransaction(tid2, objs2, txn2)
        self.db.finishTransaction(tid1)
        self.db.finishTransaction(tid2)
        self.db.deleteObject(oid1)
        self.assertEqual(self.db.getObject(oid1, tid=tid1), None)
        self.assertEqual(self.db.getObject(oid1, tid=tid2), None)
        self.db.deleteObject(oid2, serial=tid1)
        self.assertEqual(self.db.getObject(oid2, tid=tid1), False)
        self.assertEqual(self.db.getObject(oid2, tid=tid2), (tid2, None) + \
            objs2[1][1:])

    def test_getTransaction(self):
        oid1, oid2 = self.getOIDs(2)
        tid1, tid2 = self.getTIDs(2)
        txn1, objs1 = self.getTransaction([oid1])
        txn2, objs2 = self.getTransaction([oid2])
        # get from temporary table or not
        self.db.storeTransaction(tid1, objs1, txn1)
        self.db.storeTransaction(tid2, objs2, txn2)
        self.db.finishTransaction(tid1)
        result = self.db.getTransaction(tid1, True)
        self.assertEqual(result, ([oid1], 'user', 'desc', 'ext', False))
        result = self.db.getTransaction(tid2, True)
        self.assertEqual(result, ([oid2], 'user', 'desc', 'ext', False))
        # get from non-temporary only
        result = self.db.getTransaction(tid1, False)
        self.assertEqual(result, ([oid1], 'user', 'desc', 'ext', False))
        self.assertEqual(self.db.getTransaction(tid2, False), None)

    def test_getObjectHistory(self):
        oid = self.getOID(1)
        tid1, tid2, tid3 = self.getTIDs(3)
        txn1, objs1 = self.getTransaction([oid])
        txn2, objs2 = self.getTransaction([oid])
        txn3, objs3 = self.getTransaction([oid])
        # one revision
        self.db.storeTransaction(tid1, objs1, txn1)
        self.db.finishTransaction(tid1)
        result = self.db.getObjectHistory(oid, 0, 3)
        self.assertEqual(result, [(tid1, 0)])
        result = self.db.getObjectHistory(oid, 1, 1)
        self.assertEqual(result, None)
        # two revisions
        self.db.storeTransaction(tid2, objs2, txn2)
        self.db.finishTransaction(tid2)
        result = self.db.getObjectHistory(oid, 0, 3)
        self.assertEqual(result, [(tid2, 0), (tid1, 0)])
        result = self.db.getObjectHistory(oid, 1, 3)
        self.assertEqual(result, [(tid1, 0)])
        result = self.db.getObjectHistory(oid, 2, 3)
        self.assertEqual(result, None)

    def test_getObjectHistoryFrom(self):
        oid1 = self.getOID(0)
        oid2 = self.getOID(1)
        tid1, tid2, tid3, tid4 = self.getTIDs(4)
        txn1, objs1 = self.getTransaction([oid1])
        txn2, objs2 = self.getTransaction([oid2])
        txn3, objs3 = self.getTransaction([oid1])
        txn4, objs4 = self.getTransaction([oid2])
        self.db.storeTransaction(tid1, objs1, txn1)
        self.db.storeTransaction(tid2, objs2, txn2)    
        self.db.storeTransaction(tid3, objs3, txn3)
        self.db.storeTransaction(tid4, objs4, txn4)
        self.db.finishTransaction(tid1)
        self.db.finishTransaction(tid2)
        self.db.finishTransaction(tid3)
        self.db.finishTransaction(tid4)
        # Check full result
        result = self.db.getObjectHistoryFrom(ZERO_OID, ZERO_TID, MAX_TID, 10,
            1, 0)
        self.assertEqual(result, {
            oid1: [tid1, tid3],
            oid2: [tid2, tid4],
        })
        # Lower bound is inclusive
        result = self.db.getObjectHistoryFrom(oid1, tid1, MAX_TID, 10, 1, 0)
        self.assertEqual(result, {
            oid1: [tid1, tid3],
            oid2: [tid2, tid4],
        })
        # Upper bound is inclusive
        result = self.db.getObjectHistoryFrom(ZERO_OID, ZERO_TID, tid3, 10,
            1, 0)
        self.assertEqual(result, {
            oid1: [tid1, tid3],
            oid2: [tid2],
        })
        # Length is total number of serials
        result = self.db.getObjectHistoryFrom(ZERO_OID, ZERO_TID, MAX_TID, 3,
            1, 0)
        self.assertEqual(result, {
            oid1: [tid1, tid3],
            oid2: [tid2],
        })
        # Partition constraints are honored
        result = self.db.getObjectHistoryFrom(ZERO_OID, ZERO_TID, MAX_TID, 10,
            2, 0)
        self.assertEqual(result, {
            oid1: [tid1, tid3],
        })
        result = self.db.getObjectHistoryFrom(ZERO_OID, ZERO_TID, MAX_TID, 10,
            2, 1)
        self.assertEqual(result, {
            oid2: [tid2, tid4],
        })

    def _storeTransactions(self, count):
        # use OID generator to know result of tid % N
        tid_list = self.getOIDs(count)
        oid = self.getOID(1)
        for tid in tid_list:
            txn, objs = self.getTransaction([oid])
            self.db.storeTransaction(tid, objs, txn)
            self.db.finishTransaction(tid)
        return tid_list

    def test_getTIDList(self):
        tid1, tid2, tid3, tid4 = self._storeTransactions(4)
        # get tids
        result = self.db.getTIDList(0, 4, 1, [0])
        self.checkSet(result, [tid1, tid2, tid3, tid4])
        result = self.db.getTIDList(0, 4, 2, [0])
        self.checkSet(result, [tid1, tid3])
        result = self.db.getTIDList(0, 4, 2, [0, 1])
        self.checkSet(result, [tid1, tid2, tid3, tid4])
        result = self.db.getTIDList(0, 4, 3, [0])
        self.checkSet(result, [tid1, tid4])
        # get a subset of tids
        result = self.db.getTIDList(2, 4, 1, [0])
        self.checkSet(result, [tid1, tid2])
        result = self.db.getTIDList(0, 2, 1, [0])
        self.checkSet(result, [tid3, tid4])
        result = self.db.getTIDList(0, 1, 3, [0])
        self.checkSet(result, [tid4])

    def test_getReplicationTIDList(self):
        tid1, tid2, tid3, tid4 = self._storeTransactions(4)
        # get tids
        # - all
        result = self.db.getReplicationTIDList(ZERO_TID, MAX_TID, 10, 1, 0)
        self.checkSet(result, [tid1, tid2, tid3, tid4])
        # - one partition
        result = self.db.getReplicationTIDList(ZERO_TID, MAX_TID, 10, 2, 0)
        self.checkSet(result, [tid1, tid3])
        # - min_tid is inclusive
        result = self.db.getReplicationTIDList(tid3, MAX_TID, 10, 1, 0)
        self.checkSet(result, [tid3, tid4])
        # - max tid is inclusive
        result = self.db.getReplicationTIDList(ZERO_TID, tid2, 10, 1, 0)
        self.checkSet(result, [tid1, tid2])
        # - limit
        result = self.db.getReplicationTIDList(ZERO_TID, MAX_TID, 2, 1, 0)
        self.checkSet(result, [tid1, tid2])

    def test__getObjectData(self):
        db = self.db
        db.setup(reset=True)
        tid0 = self.getNextTID()
        tid1 = self.getNextTID()
        tid2 = self.getNextTID()
        tid3 = self.getNextTID()
        assert tid0 < tid1 < tid2 < tid3
        oid1 = self.getOID(1)
        oid2 = self.getOID(2)
        oid3 = self.getOID(3)
        db.storeTransaction(
            tid1, (
                (oid1, 0, 0, 'foo', None),
                (oid2, None, None, None, tid0),
                (oid3, None, None, None, tid2),
            ), None, temporary=False)
        db.storeTransaction(
            tid2, (
                (oid1, None, None, None, tid1),
                (oid2, None, None, None, tid1),
                (oid3, 0, 0, 'bar', None),
            ), None, temporary=False)

        original_getObjectData = db._getObjectData
        def _getObjectData(*args, **kw):
            call_counter.append(1)
            return original_getObjectData(*args, **kw)
        db._getObjectData = _getObjectData

        # NOTE: all tests are done as if values were fetched by _getObject, so
        # there is already one indirection level.

        # oid1 at tid1: data is immediately found
        call_counter = []
        self.assertEqual(
            db._getObjectData(u64(oid1), u64(tid1), u64(tid3)),
            (u64(tid1), 0, 0, 'foo'))
        self.assertEqual(sum(call_counter), 1)

        # oid2 at tid1: missing data in table, raise IndexError on next
        # recursive call
        call_counter = []
        self.assertRaises(IndexError, db._getObjectData, u64(oid2), u64(tid1),
            u64(tid3))
        self.assertEqual(sum(call_counter), 2)

        # oid3 at tid1: data_serial grater than row's tid, raise ValueError
        # on next recursive call - even if data does exist at that tid (see
        # "oid3 at tid2" case below)
        call_counter = []
        self.assertRaises(ValueError, db._getObjectData, u64(oid3), u64(tid1),
            u64(tid3))
        self.assertEqual(sum(call_counter), 2)
        # Same with wrong parameters (tid0 < tid1)
        call_counter = []
        self.assertRaises(ValueError, db._getObjectData, u64(oid3), u64(tid1),
            u64(tid0))
        self.assertEqual(sum(call_counter), 1)
        # Same with wrong parameters (tid1 == tid1)
        call_counter = []
        self.assertRaises(ValueError, db._getObjectData, u64(oid3), u64(tid1),
            u64(tid1))
        self.assertEqual(sum(call_counter), 1)

        # oid1 at tid2: data is found after ons recursive call
        call_counter = []
        self.assertEqual(
            db._getObjectData(u64(oid1), u64(tid2), u64(tid3)),
            (u64(tid1), 0, 0, 'foo'))
        self.assertEqual(sum(call_counter), 2)

        # oid2 at tid2: missing data in table, raise IndexError after two
        # recursive calls
        call_counter = []
        self.assertRaises(IndexError, db._getObjectData, u64(oid2), u64(tid2),
            u64(tid3))
        self.assertEqual(sum(call_counter), 3)

        # oid3 at tid2: data is immediately found
        call_counter = []
        self.assertEqual(
            db._getObjectData(u64(oid3), u64(tid2), u64(tid3)),
            (u64(tid2), 0, 0, 'bar'))
        self.assertEqual(sum(call_counter), 1)

    def test__getDataTIDFromData(self):
        db = self.db
        db.setup(reset=True)
        tid1 = self.getNextTID()
        tid2 = self.getNextTID()
        oid1 = self.getOID(1)
        db.storeTransaction(
            tid1, (
                (oid1, 0, 0, 'foo', None),
            ), None, temporary=False)
        db.storeTransaction(
            tid2, (
                (oid1, None, None, None, tid1),
            ), None, temporary=False)

        self.assertEqual(
            db._getDataTIDFromData(u64(oid1),
                db._getObject(u64(oid1), tid=u64(tid1))),
            (u64(tid1), u64(tid1)))
        self.assertEqual(
            db._getDataTIDFromData(u64(oid1),
                db._getObject(u64(oid1), tid=u64(tid2))),
            (u64(tid2), u64(tid1)))

    def test__getDataTID(self):
        db = self.db
        db.setup(reset=True)
        tid1 = self.getNextTID()
        tid2 = self.getNextTID()
        oid1 = self.getOID(1)
        db.storeTransaction(
            tid1, (
                (oid1, 0, 0, 'foo', None),
            ), None, temporary=False)
        db.storeTransaction(
            tid2, (
                (oid1, None, None, None, tid1),
            ), None, temporary=False)

        self.assertEqual(
            db._getDataTID(u64(oid1), tid=u64(tid1)),
            (u64(tid1), u64(tid1)))
        self.assertEqual(
            db._getDataTID(u64(oid1), tid=u64(tid2)),
            (u64(tid2), u64(tid1)))

    def test_findUndoTID(self):
        db = self.db
        db.setup(reset=True)
        tid1 = self.getNextTID()
        tid2 = self.getNextTID()
        tid3 = self.getNextTID()
        tid4 = self.getNextTID()
        oid1 = self.getOID(1)
        db.storeTransaction(
            tid1, (
                (oid1, 0, 0, 'foo', None),
            ), None, temporary=False)

        # Undoing oid1 tid1, OK: tid1 is latest
        # Result: current tid is tid1, data_tid is None (undoing object
        # creation)
        self.assertEqual(
            db.findUndoTID(oid1, tid4, tid1, None),
            (tid1, None, True))

        # Store a new transaction
        db.storeTransaction(
            tid2, (
                (oid1, 0, 0, 'bar', None),
            ), None, temporary=False)

        # Undoing oid1 tid2, OK: tid2 is latest
        # Result: current tid is tid2, data_tid is tid1
        self.assertEqual(
            db.findUndoTID(oid1, tid4, tid2, None),
            (tid2, tid1, True))

        # Undoing oid1 tid1, Error: tid2 is latest
        # Result: current tid is tid2, data_tid is -1
        self.assertEqual(
            db.findUndoTID(oid1, tid4, tid1, None),
            (tid2, None, False))

        # Undoing oid1 tid1 with tid2 being undone in same transaction,
        # OK: tid1 is latest
        # Result: current tid is tid2, data_tid is None (undoing object
        # creation)
        # Explanation of transaction_object: oid1, no data but a data serial
        # to tid1
        self.assertEqual(
            db.findUndoTID(oid1, tid4, tid1,
                (u64(oid1), None, None, None, tid1)),
            (tid4, None, True))

        # Store a new transaction
        db.storeTransaction(
            tid3, (
                (oid1, None, None, None, tid1),
            ), None, temporary=False)

        # Undoing oid1 tid1, OK: tid3 is latest with tid1 data
        # Result: current tid is tid2, data_tid is None (undoing object
        # creation)
        self.assertEqual(
            db.findUndoTID(oid1, tid4, tid1, None),
            (tid3, None, True))

if __name__ == "__main__":
    unittest.main()
