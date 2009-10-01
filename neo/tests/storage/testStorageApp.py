#
# Copyright (C) 2009  Nexedi SA
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

import unittest, logging, os
from mock import Mock
from neo.tests import NeoTestBase
from neo.storage.app import Application
from neo.protocol import CellStates, INVALID_PTID, INVALID_TID, \
     INVALID_UUID, Packet, NOTIFY_NODE_INFORMATION
from neo.storage.mysqldb import p64, u64, MySQLDatabaseManager
from collections import deque
from neo.pt import PartitionTable
from neo.util import dump

class StorageAppTests(NeoTestBase):

    def setUp(self):
        self.prepareDatabase(number=1)
        # create an application object
        config = self.getStorageConfiguration(master_number=1)
        self.app = Application(**config)
        self.app.event_queue = deque()
        
    def tearDown(self):
        NeoTestBase.tearDown(self)

    def test_01_loadPartitionTable(self):
      self.assertEqual(len(self.app.dm.getPartitionTable()), 0)
      self.assertEqual(self.app.pt, None)
      num_partitions = 3
      num_replicas = 2
      self.app.pt = PartitionTable(num_partitions, num_replicas)
      self.assertEqual(self.app.pt.getNodeList(), [])
      self.assertFalse(self.app.pt.filled())
      for x in xrange(num_partitions):
        self.assertFalse(self.app.pt.hasOffset(x))

      # load an empty table
      self.app.loadPartitionTable()
      self.assertEqual(self.app.pt.getNodeList(), [])
      self.assertFalse(self.app.pt.filled())
      for x in xrange(num_partitions):
        self.assertFalse(self.app.pt.hasOffset(x))

      # add some node, will be remove when loading table
      master_uuid = self.getNewUUID()      
      master = self.app.nm.createMaster(uuid=master_uuid)
      storage_uuid = self.getNewUUID()      
      storage = self.app.nm.createStorage(uuid=storage_uuid)
      client_uuid = self.getNewUUID()      
      client = self.app.nm.createClient(uuid=client_uuid)

      self.app.pt.setCell(0, master, CellStates.UP_TO_DATE)
      self.app.pt.setCell(0, storage, CellStates.UP_TO_DATE)
      self.assertEqual(len(self.app.pt.getNodeList()), 2)
      self.assertFalse(self.app.pt.filled())
      for x in xrange(num_partitions):
        if x == 0:
          self.assertTrue(self.app.pt.hasOffset(x))
        else:
          self.assertFalse(self.app.pt.hasOffset(x))
      # load an empty table, everything removed
      self.assertEqual(len(self.app.dm.getPartitionTable()), 0)
      self.app.loadPartitionTable()
      self.assertEqual(self.app.pt.getNodeList(), [])
      self.assertFalse(self.app.pt.filled())
      for x in xrange(num_partitions):
        self.assertFalse(self.app.pt.hasOffset(x))

      # add some node
      self.app.pt.setCell(0, master, CellStates.UP_TO_DATE)
      self.app.pt.setCell(0, storage, CellStates.UP_TO_DATE)
      self.assertEqual(len(self.app.pt.getNodeList()), 2)
      self.assertFalse(self.app.pt.filled())
      for x in xrange(num_partitions):
        if x == 0:
          self.assertTrue(self.app.pt.hasOffset(x))
        else:
          self.assertFalse(self.app.pt.hasOffset(x))
      # fill partition table
      self.app.dm.setPTID(1)
      self.app.dm.query('delete from pt;')
      self.app.dm.query("insert into pt (rid, uuid, state) values ('%s', '%s', %d)" % 
                        (0, dump(client_uuid), CellStates.UP_TO_DATE))
      self.app.dm.query("insert into pt (rid, uuid, state) values ('%s', '%s', %d)" % 
                        (1, dump(client_uuid), CellStates.UP_TO_DATE))
      self.app.dm.query("insert into pt (rid, uuid, state) values ('%s', '%s', %d)" % 
                        (1, dump(storage_uuid), CellStates.UP_TO_DATE))
      self.app.dm.query("insert into pt (rid, uuid, state) values ('%s', '%s', %d)" % 
                        (2, dump(storage_uuid), CellStates.UP_TO_DATE))
      self.app.dm.query("insert into pt (rid, uuid, state) values ('%s', '%s', %d)" % 
                        (2, dump(master_uuid), CellStates.UP_TO_DATE))
      self.assertEqual(len(self.app.dm.getPartitionTable()), 5)
      self.app.pt.clear()
      self.app.loadPartitionTable()
      self.assertTrue(self.app.pt.filled())
      for x in xrange(num_partitions):        
        self.assertTrue(self.app.pt.hasOffset(x))
      # check each row
      cell_list = self.app.pt.getCellList(0)
      self.assertEqual(len(cell_list), 1)
      self.assertEqual(cell_list[0].getUUID(), client_uuid)
      cell_list = self.app.pt.getCellList(1)
      self.assertEqual(len(cell_list), 2)
      self.failUnless(cell_list[0].getUUID() in (client_uuid, storage_uuid))
      self.failUnless(cell_list[1].getUUID() in (client_uuid, storage_uuid))
      cell_list = self.app.pt.getCellList(2)
      self.assertEqual(len(cell_list), 2)
      self.failUnless(cell_list[0].getUUID() in (master_uuid, storage_uuid))
      self.failUnless(cell_list[1].getUUID() in (master_uuid, storage_uuid))
      
    def test_02_queueEvent(self):
      self.assertEqual(len(self.app.event_queue), 0)
      event = Mock({"getId": 1325136})
      self.app.queueEvent(event, "test", key="value")
      self.assertEqual(len(self.app.event_queue), 1)
      event, args, kw = self.app.event_queue[0]
      self.assertEqual(event.getId(), 1325136)
      self.assertEqual(len(args), 1)
      self.assertEqual(args[0], "test")
      self.assertEqual(kw, {"key" : "value"})
      
    def test_03_executeQueuedEvents(self):
      self.assertEqual(len(self.app.event_queue), 0)
      event = Mock({"getId": 1325136})
      self.app.queueEvent(event, "test", key="value")
      self.app.executeQueuedEvents()
      self.assertEquals(len(event.mockGetNamedCalls("__call__")), 1)
      call = event.mockGetNamedCalls("__call__")[0]
      params = call.getParam(0)
      self.assertEqual(params, "test")
      params = call.kwparams
      self.assertEqual(params, {'key': 'value'})

if __name__ == '__main__':
    unittest.main()

