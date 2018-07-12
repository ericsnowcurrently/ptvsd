import contextlib
import sys
import threading

from _pydevd_bundle import pydevd_comm
from pydevd import pydevd_tracing

from ptvsd.socket import Address
from ptvsd.daemon import Daemon, DaemonStoppedError, DaemonClosedError
from ptvsd._util import debug, new_hidden_thread


def start_server(daemon, host, port, **kwargs):
    """Return a socket to a (new) local pydevd-handling daemon.

    The daemon supports the pydevd client wire protocol, sending
    requests and handling responses (and events).

    This is a replacement for _pydevd_bundle.pydevd_comm.start_server.
    """
    sock, next_session = daemon.start_server((host, port))

    def handle_next():
        try:
            session = next_session(**kwargs)
            debug('done waiting')
            return session
        except (DaemonClosedError, DaemonStoppedError):
            # Typically won't happen.
            debug('stopped')
            raise
        except Exception as exc:
            # TODO: log this?
            debug('failed:', exc, tb=True)
            return None

    def serve_forever():
        debug('waiting on initial connection')
        handle_next()
        while True:
            debug('waiting on next connection')
            try:
                handle_next()
            except (DaemonClosedError, DaemonStoppedError):
                break
        debug('done')

    t = new_hidden_thread(
        target=serve_forever,
        name='sessions',
    )
    t.start()
    return sock


def start_client(daemon, host, port, **kwargs):
    """Return a socket to an existing "remote" pydevd-handling daemon.

    The daemon supports the pydevd client wire protocol, sending
    requests and handling responses (and events).

    This is a replacement for _pydevd_bundle.pydevd_comm.start_client.
    """
    sock, start_session = daemon.start_client((host, port))
    start_session(**kwargs)
    return sock


def install(pydevd, address,
            start_server=start_server, start_client=start_client,
            **kwargs):
    """Configure pydevd to use our wrapper.

    This is a bit of a hack to allow us to run our VSC debug adapter
    in the same process as pydevd.  Note that, as with most hacks,
    this is somewhat fragile (since the monkeypatching sites may
    change).
    """
    addr = Address.from_raw(address)
    daemon = Daemon(**kwargs)

    _start_server = (lambda p: start_server(daemon, addr.host, p))
    _start_server.orig = start_server
    _start_client = (lambda h, p: start_client(daemon, h, p))
    _start_client.orig = start_client

    # These are the functions pydevd invokes to get a socket to the client.
    pydevd_comm.start_server = _start_server
    pydevd_comm.start_client = _start_client

    # Ensure that pydevd is using our functions.
    pydevd.start_server = _start_server
    pydevd.start_client = _start_client
    __main__ = sys.modules['__main__']
    if __main__ is not pydevd:
        if getattr(__main__, '__file__', None) == pydevd.__file__:
            __main__.start_server = _start_server
            __main__.start_client = _start_client
    return daemon


##################################
# monkey-patching sys.settrace()

_replace_settrace = pydevd_tracing.replace_sys_set_trace_func
_restore_settrace = pydevd_tracing.restore_sys_set_trace_func


@contextlib.contextmanager
def protect_settrace():
    """A context manager that keeps pydevd from replacing sys.settrace()."""
    lock = threading.Lock()
    lock.acquire()  # released when we're done

    # Avoid races with managing sys.settrace.
    def replace():
        lock.acquire()
        lock.release()
        pydevd_tracing.replace_sys_set_trace_func = _replace_settrace
        _replace_settrace()
    pydevd_tracing.replace_sys_set_trace_func = replace
    try:
        yield
    finally:
        lock.release()


@contextlib.contextmanager
def settrace_restored(orig=sys.settrace):
    """A context manager that temporary restores the original sys.settrace."""
    with protect_settrace():
        before = sys.gettrace
        _restore_settrace()
        after = sys.gettrace
        try:
            yield after
        finally:
            if after is not before:
                # pydevd was already installed
                _replace_settrace()
