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
import transaction
import ZODB
from ZODB.POSException import ConflictError
from Persistence import Persistent

from neo.tests.functional import NEOCluster, NEOFunctionalTest
from neo.protocol import ClusterStates, NodeStates


# simple persitent object with conflict resolution
class PCounter(Persistent):

    _value = 0

    def value(self):
        return self._value

    def inc(self):
        self._value += 1


class PCounterWithResolution(PCounter):

    def _p_resolveConflict(self, old, saved, new):
        new['_value'] = saved['_value'] + new['_value']
        return new


class ClientTests(NEOFunctionalTest):

    def setUp(self):
        NEOFunctionalTest.setUp(self)
        self.neo = NEOCluster(
            ['test_neo1'],
            port_base=20000,
            master_node_count=1,
            temp_dir=self.getTempDirectory()
        )

    def tearDown(self):
        if self.neo is not None:
            self.neo.stop()

    def __setup(self):
        # start cluster
        self.neo.setupDB()
        self.neo.start()
        self.neo.expectClusterRunning()
        self.db = ZODB.DB(self.neo.getZODBStorage())

    def makeTransaction(self):
        # create a transaction a get the root object
        txn = transaction.TransactionManager()
        conn = self.db.open(transaction_manager=txn)
        return (txn, conn)

    def testConflictResolutionTriggered(self):
        """ Check that ConflictError is raised on write conflict """
        # create the initial objects
        self.__setup()
        t, c = self.makeTransaction()
        c.root()['without_resolution'] = PCounter()
        t.commit()

        # first with no conflict resolution
        t1, c1 = self.makeTransaction()
        t2, c2 = self.makeTransaction()
        o1 = c1.root()['without_resolution']
        o2 = c2.root()['without_resolution']
        self.assertEqual(o1.value(), 0)
        self.assertEqual(o2.value(), 0)
        o1.inc()
        o2.inc()
        o2.inc()
        t1.commit()
        self.assertEqual(o1.value(), 1)
        self.assertEqual(o2.value(), 2)
        self.assertRaises(ConflictError, t2.commit)

    def testConflictResolutionTriggered(self):
        """ Check that conflict resolution works """
        # create the initial objects
        self.__setup()
        t, c = self.makeTransaction()
        c.root()['with_resolution'] = PCounterWithResolution()
        t.commit()

        # then with resolution
        t1, c1 = self.makeTransaction()
        t2, c2 = self.makeTransaction()
        o1 = c1.root()['with_resolution']
        o2 = c2.root()['with_resolution']
        self.assertEqual(o1.value(), 0)
        self.assertEqual(o2.value(), 0)
        o1.inc()
        o2.inc()
        o2.inc()
        t1.commit()
        self.assertEqual(o1.value(), 1)
        self.assertEqual(o2.value(), 2)
        t2.commit()
        t1.begin()
        t2.begin()
        self.assertEqual(o2.value(), 3)
        self.assertEqual(o1.value(), 3)

    def testIsolationAtZopeLevel(self):
        """ Check transaction isolation within zope connection """
        self.__setup()
        t, c = self.makeTransaction()
        c.root()['item'] = 0
        t.commit()
        t1, c1 = self.makeTransaction()
        t2, c2 = self.makeTransaction()
        c1.root()['item'] = 1
        t1.commit()
        # load objet from zope cache
        self.assertEqual(c1.root()['item'], 1)
        self.assertEqual(c2.root()['item'], 0)

    def testIsolationWithoutZopeCache(self):
        """ Check isolation with zope cache cleared """
        self.__setup()
        t, c = self.makeTransaction()
        c.root()['item'] = 0
        t.commit()
        t1, c1 = self.makeTransaction()
        t2, c2 = self.makeTransaction()
        c1.root()['item'] = 1
        t1.commit()
        # clear zope cache to force re-ask NEO
        c1.cacheMinimize()
        c2.cacheMinimize()
        self.assertEqual(c1.root()['item'], 1)
        self.assertEqual(c2.root()['item'], 0)

    def testIsolationWithNewConnection(self):
        """ Check isolation with zope cache cleared """
        self.__setup()
        t, c = self.makeTransaction()
        c.root()['item'] = 0
        t.commit()
        t1, c1 = self.makeTransaction()
        t2, c2 = self.makeTransaction()
        c1.root()['item'] = 1
        t1.commit()
        # open a new connection for this transaction
        c1 = self.db.open(transaction_manager=t1)
        c2 = self.db.open(transaction_manager=t2)
        self.assertEqual(c1.root()['item'], 1)
        self.assertEqual(c2.root()['item'], 0)





def test_suite():
    return unittest.makeSuite(ClientTests)

if __name__ == "__main__":
    unittest.main(defaultTest="test_suite")

