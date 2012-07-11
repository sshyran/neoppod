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

# WARNING: Log rotating should not be implemented here.
#          SQLite does not access database only by file descriptor,
#          and an OperationalError exception would be raised if a log is emitted
#          between a rename and a reopen.
#          Fortunately, SQLite allow multiple process to access the same DB,
#          so an external tool should be able to dump and empty tables.

from collections import deque
from functools import wraps
from logging import getLogger, Formatter, Logger, LogRecord, StreamHandler, \
    DEBUG, WARNING
from time import time
from traceback import format_exception
import bz2, inspect, neo, os, signal, sqlite3, threading

# Stats for storage node of matrix test (py2.7:SQLite)
RECORD_SIZE = ( 234360832 # extra memory used
              - 16777264  # sum of raw data ('msg' attribute)
              ) // 187509 # number of records

FMT = ('%(asctime)s %(levelname)-9s %(name)-10s'
       ' [%(module)14s:%(lineno)3d] \n%(message)s')

class _Formatter(Formatter):

    def formatTime(self, record, datefmt=None):
        return Formatter.formatTime(self, record,
           '%Y-%m-%d %H:%M:%S') + '.%04d' % (record.msecs * 10)

    def format(self, record):
        lines = iter(Formatter.format(self, record).splitlines())
        prefix = lines.next()
        return '\n'.join(prefix + line for line in lines)


class PacketRecord(object):

    args = None
    levelno = DEBUG
    __init__ = property(lambda self: self.__dict__.update)


class NEOLogger(Logger):

    default_root_handler = StreamHandler()
    default_root_handler.setFormatter(_Formatter(FMT))

    def __init__(self):
        Logger.__init__(self, None)
        self.parent = root = getLogger()
        if not root.handlers:
            root.addHandler(self.default_root_handler)
        self.db = None
        self._record_queue = deque()
        self._record_size = 0
        self._async = set()
        l = threading.Lock()
        self._acquire = l.acquire
        release = l.release
        def _release():
            try:
                while self._async:
                    self._async.pop()(self)
            finally:
                release()
        self._release = _release
        self.backlog()

    def __async(wrapped):
        def wrapper(self):
            self._async.add(wrapped)
            if self._acquire(0):
                self._release()
        return wraps(wrapped)(wrapper)

    @__async
    def flush(self):
        if self.db is None:
            return
        self.db.execute("BEGIN")
        for r in self._record_queue:
            self._emit(r)
        self.db.commit()
        self._record_queue.clear()
        self._record_size = 0

    def backlog(self, max_size=1<<24):
        self._acquire()
        try:
            self._max_size = max_size
            if max_size is None:
                self.flush()
            else:
                q = self._record_queue
                while max_size < self._record_size:
                    self._record_size -= RECORD_SIZE + len(q.popleft().msg)
        finally:
            self._release()

    def setup(self, filename=None, reset=False):
        self._acquire()
        try:
            from . import protocol as p
            global uuid_str
            uuid_str = p.uuid_str
            if self.db is not None:
                self.db.close()
                if not filename:
                    self.db = None
                    self._record_queue.clear()
                    self._record_size = 0
                    return
            if filename:
                self.db = sqlite3.connect(filename, isolation_level=None,
                                                    check_same_thread=False)
                q = self.db.execute
                if reset:
                    for t in 'log', 'packet':
                        q('DROP TABLE IF EXISTS ' + t)
                q("""CREATE TABLE IF NOT EXISTS log (
                        date REAL NOT NULL,
                        name TEXT,
                        level INTEGER NOT NULL,
                        pathname TEXT,
                        lineno INTEGER,
                        msg TEXT)
                  """)
                q("""CREATE INDEX IF NOT EXISTS _log_i1 ON log(date)""")
                q("""CREATE TABLE IF NOT EXISTS packet (
                        date REAL NOT NULL,
                        name TEXT,
                        msg_id INTEGER NOT NULL,
                        code INTEGER NOT NULL,
                        peer TEXT NOT NULL,
                        body BLOB)
                  """)
                q("""CREATE INDEX IF NOT EXISTS _packet_i1 ON packet(date)""")
                q("""CREATE TABLE IF NOT EXISTS protocol (
                        date REAL PRIMARY KEY NOT NULL,
                        text BLOB NOT NULL)
                  """)
                with open(inspect.getsourcefile(p)) as p:
                    p = buffer(bz2.compress(p.read()))
                for t, in q("SELECT text FROM protocol ORDER BY date DESC"):
                    if p == t:
                        break
                else:
                    q("INSERT INTO protocol VALUES (?,?)", (time(), p))
        finally:
            self._release()
    __del__ = setup

    def isEnabledFor(self, level):
        return True

    def _emit(self, r):
        if type(r) is PacketRecord:
            ip, port = r.addr
            peer = '%s %s (%s:%u)' % ('>' if r.outgoing else '<',
                                      uuid_str(r.uuid), ip, port)
            self.db.execute("INSERT INTO packet VALUES (?,?,?,?,?,?)",
                (r.created, r._name, r.msg_id, r.code, peer, buffer(r.msg)))
        else:
            pathname = os.path.relpath(r.pathname, *neo.__path__)
            self.db.execute("INSERT INTO log VALUES (?,?,?,?,?,?)",
                (r.created, r._name, r.levelno, pathname, r.lineno, r.msg))

    def _queue(self, record):
        record._name = self.name and str(self.name)
        self._acquire()
        try:
            if self._max_size is None:
                self._emit(record)
            else:
                self._record_size += RECORD_SIZE + len(record.msg)
                q = self._record_queue
                q.append(record)
                if record.levelno < WARNING:
                    while self._max_size < self._record_size:
                        self._record_size -= RECORD_SIZE + len(q.popleft().msg)
                else:
                    self.flush()
        finally:
            self._release()

    def callHandlers(self, record):
        if self.db is not None:
            record.msg = record.getMessage()
            record.args = None
            if record.exc_info:
                record.msg += '\n' + ''.join(
                    format_exception(*record.exc_info)).strip()
                record.exc_info = None
            self._queue(record)
        if Logger.isEnabledFor(self, record.levelno):
            record.name = self.name or 'NEO'
            self.parent.callHandlers(record)

    def packet(self, connection, packet, outgoing):
        if self.db is not None:
            ip, port = connection.getAddress()
            self._queue(PacketRecord(
                created=time(),
                msg_id=packet._id,
                code=packet._code,
                outgoing=outgoing,
                uuid=connection.getUUID(),
                addr=connection.getAddress(),
                msg=packet._body))


logging = NEOLogger()
signal.signal(signal.SIGRTMIN, lambda signum, frame: logging.flush())
