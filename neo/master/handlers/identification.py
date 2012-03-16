#
# Copyright (C) 2006-2012  Nexedi SA
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

import neo

from neo.lib.protocol import NodeTypes, Packets
from neo.lib.protocol import ProtocolError
from . import MasterHandler

class IdentificationHandler(MasterHandler):

    def requestIdentification(self, conn, *args, **kw):
        super(IdentificationHandler, self).requestIdentification(conn, *args,
            **kw)
        handler = conn.getHandler()
        assert not isinstance(handler, IdentificationHandler), handler
        handler.connectionCompleted(conn)

    def _setupNode(self, conn, node_type, uuid, address, node):
        app = self.app
        if node:
            if node.isRunning():
                # cloned/evil/buggy node connecting to us
                raise ProtocolError('already connected')
            else:
                assert not node.isConnected()
            node.setAddress(address)
            node.setRunning()

        # ask the app the node identification, if refused, an exception is
        # raised
        result = app.identifyNode(node_type, uuid, node)
        (uuid, node, state, handler, node_ctor) = result
        if uuid is None:
            # no valid uuid, give it one
            uuid = app.getNewUUID(node_type)
        if node is None:
            node = node_ctor(uuid=uuid, address=address)
        node.setUUID(uuid)
        node.setState(state)
        node.setConnection(conn)
        conn.setHandler(handler)
        app.broadcastNodesInformation([node])
        return uuid

class SecondaryIdentificationHandler(MasterHandler):

    def announcePrimary(self, conn):
        # If we received AnnouncePrimary on a client connection, we might have
        # set this handler on server connection, and might receive
        # AnnouncePrimary there too. As we cannot reach this without already
        # handling a first AnnouncePrimary, we can safely ignore this one.
        pass

    def _setupNode(self, conn, node_type, uuid, address, node):
        # Nothing to do, storage will disconnect when it receives our answer.
        # Primary will do the checks.
        return uuid

