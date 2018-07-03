import contextlib
import sys
import threading

from _pydevd_bundle.pydevd_comm import (
    CMD_VERSION,
)

from ._pydevd import (
    parse_message, encode_message, iter_messages, Message, CMDID)
from tests.helpers import protocol, socket
from ._binder import BinderBase


PROTOCOL = protocol.MessageProtocol(
    parse=parse_message,
    encode=encode_message,
    iter=iter_messages,
)


class Binder(BinderBase):

    def __init__(self, singlesession=True):
        super(Binder, self).__init__(
            singlesession=singlesession,
        )
        self._lock = threading.Lock()
        self._lock.acquire()

    def _run_debugger(self):
        self._start_ptvsd()
        # Block until "done" debugging.
        self._lock.acquire()

    def _wrap_sock(self):
        return socket.Connection(self.ptvsd.fakesock, self.ptvsd.server)

    def _done(self):
        self._lock.release()


class Started(protocol.MessageDaemonStarted):

    def send_response(self, msg):
        self.wait_until_connected()
        return self.daemon.send_response(msg)

    def send_event(self, msg):
        self.wait_until_connected()
        return self.daemon.send_event(msg)


class FakePyDevd(protocol.MessageDaemon):
    """A testing double for PyDevd.

    Note that you have the option to provide a handler function.  This
    function will be called for each received message, with two args:
    the received message and the fake's "send_message" method.  If
    appropriate, it may call send_message() in response to the received
    message, along with doing anything else it needs to do.  Any
    exceptions raised by the handler are recorded but otherwise ignored.

    Example usage:

      >>> fake = FakePyDevd('127.0.0.1', 8888)
      >>> with fake.start('127.0.0.1', 8888):
      ...   fake.send_response(b'101\t1\t')
      ...   fake.send_event(b'900\t2\t')
      ... 
      >>> fake.assert_received(testcase, [
      ...   b'101\t1\t',  # the "run" request
      ...   # some other requests
      ... ])
      >>> 

    A description of the protocol:
      https://github.com/fabioz/PyDev.Debugger/blob/master/_pydevd_bundle/pydevd_comm.py
    """  # noqa

    STARTED = Started
    EXTERNAL = False

    PROTOCOL = PROTOCOL
    VERSION = '1.1.1'

    @classmethod
    def validate_message(cls, msg):
        """Ensure the message is legitimate."""
        # TODO: Check the message.

    @classmethod
    def handle_request(cls, req, send_message, handler=None):
        """The default message handler."""
        if handler is not None:
            handler(req, send_message)

        resp = cls._get_response(req)
        if resp is not None:
            send_message(resp)

    @classmethod
    def _get_response(cls, req):
        try:
            cmdid, seq, _ = req
        except (IndexError, ValueError):
            req = req.msg
            cmdid, seq, _ = req

        if cmdid == CMD_VERSION:
            return Message(CMD_VERSION, seq, cls.VERSION)
        else:
            return None

    def __init__(self, handler=None, **kwargs):
        self.binder = Binder(**kwargs)

        super(FakePyDevd, self).__init__(
            self.binder.bind,
            PROTOCOL,
            (lambda msg, send: self.handle_request(msg, send, handler)),
        )

    def wait_for_command(self, cmdid, seq=None, **kwargs):
        return self._wait_for_command('cmd', cmdid, seq, **kwargs)

    def wait_for_request(self, cmdid, text, reqid=None, **kwargs):
        if reqid is None:
            reqid = cmdid
        respid = cmdid

        def handle_msg(req, send_msg):
            resp = Message(respid, req.seq, text)
            send_msg(resp)
        return self._wait_for_command(
            'request',
            reqid,
            handler=handle_msg,
            **kwargs
        )

    def send_response(self, msg):
        """Send a response message to the adapter (ptvsd)."""
        # XXX Ensure it's a response?
        return self._send_message(msg)

    def send_event(self, msg):
        """Send an event message to the adapter (ptvsd)."""
        # XXX Ensure it's a request?
        return self.send_message(msg)

    def add_pending_response(self, cmdid, text, reqid=None, handlername=None):
        """Add a response for a request."""
        if reqid is None:
            reqid = cmdid
        respid = cmdid

        if handlername is None:
            handlername = '<request cmdid={!r}>'.format(CMDID.from_raw(reqid))
        match = self._new_matcher(reqid)

        def handle_request(req, send_message):
            resp = Message(respid, req.seq, text)
            send_message(resp)
        self.add_matcher(match, handle_request, handlername=handlername)

    # internal methods

    def _new_sockfile(self):
        if sys.version_info >= (3,):
            return super(FakePyDevd, self)._new_sockfile()

        def socklines():
            lines = socket.iter_lines(
                self._sock,
                stop=(lambda: self._closed),
            )
            try:
                for line in lines:
                    yield line
            except EOFError:
                # TODO: Trigger self.close()?
                pass

        class SocketWrapper(object):
            def __iter__(self):
                return socklines()

            def close(self):
                pass
        return SocketWrapper()

    @contextlib.contextmanager
    def _wait_for_command(self, kind, cmdid, seq=None, **kwargs):
        kwargs['stacklevel'] = kwargs.get('stacklevel', 1) + 2

        cmdid = CMDID.from_raw(cmdid)
        if seq is None:
            handlername = '<{} id={!r}>'.format(kind, cmdid)
        else:
            handlername = '<{} id={!r} seq={}>'.format(kind, cmdid, seq)
        kwargs.setdefault('handlername', handlername)

        match = self._new_matcher(cmdid, seq)
        with self.wait_for_message(match, req=None, **kwargs):
            yield

    def _new_matcher(self, cmdid, seq=None):
        def match(msg):
            try:
                actual = msg.cmdid
            except AttributeError:
                return False
            if actual != cmdid:
                return False
            if seq is not None:
                try:
                    actual = msg.seq
                except AttributeError:
                    return False
                if actual != seq:
                    return False
            return True
        return match

    def _close(self):
        self.binder._done()
        super(FakePyDevd, self)._close()
