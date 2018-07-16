import sys
import threading

import pydevd
from _pydevd_bundle import pydevd_trace_dispatch

from ptvsd._util import debug, new_hidden_thread, lock_release
from ptvsd.pydevd_hooks import (
    install, start_server, settrace_restored, protect_frames,
)
from ptvsd.socket import Address


def _pydevd_settrace(redirect_output=None, _pydevd=pydevd, **kwargs):
    if redirect_output is not None:
        kwargs.setdefault('stdoutToServer', redirect_output)
        kwargs.setdefault('stderrToServer', redirect_output)
    # pydevd.settrace() only enables debugging of the current
    # thread and all future threads.  PyDevd is not enabled for
    # existing threads (other than the current one).  Consequently,
    # pydevd.settrace() must be called ASAP in the current thread.
    # See issue #509.
    #
    # This is tricky, however, because settrace() will block until
    # it receives a CMD_RUN message.  You can't just call it in a
    # thread to avoid blocking; doing so would prevent the current
    # thread from being debugged.
    _pydevd.settrace(**kwargs)


# TODO: Split up enable_attach() to align with module organization.
# This should including making better use of Daemon (e,g, the
# start_server() method).
# Then move at least some parts to the appropriate modules.  This module
# is focused on running the debugger.

def enable_attach(address,
                  on_attach=(lambda: None),
                  is_ready=(lambda: True),
                  redirect_output=True,
                  _pydevd=pydevd,
                  _install=install,
                  _settrace=_pydevd_settrace,
                  **kwargs):
    addr = Address.as_server(*address)

    readylock = threading.Lock()
    readylock.acquire()  # released in tracefunc() below

    def notify_ready(session):
        on_attach()
        debug('attached')
        # Ensure that debugging has been enabled in the current thread.
        readylock.acquire()
        readylock.release()

    debug('installing ptvsd as server')
    # pydevd.settrace() forces a "client" connection, so we trick it
    # by setting start_client to start_server..
    daemon = _install(
        _pydevd,
        addr,
        start_client=start_server,
        notify_session_debugger_ready=notify_ready,
        singlesession=False,
        **kwargs
    )

    # Start pydevd using threads and monkey-patching sys.settrace.

    def debug_current_thread(suspend=False, **kwargs):
        # Make sure that pydevd has finished starting before enabling
        # in the current thread.
        t.join()
        debug('enabling pydevd (current thread)')
        _settrace(
            host=None,  # ignored
            stdoutToServer=False,  # ignored
            stderrToServer=False,  # ignored
            port=None,  # ignored
            suspend=suspend,
            trace_only_current_thread=True,
            overwrite_prev_trace=True,
            patch_multiprocessing=False,
            _pydevd=_pydevd,
            **kwargs
        )
        debug('pydevd enabled (current thread)')
    tracing = _ensure_current_thread_will_debug(
        debug_current_thread,
        is_ready,
        readylock,
    )

    def start_pydevd():
        debug('enabling pydevd')
        # Only pass the port so start_server() gets triggered.
        # As noted above, we also have to trick settrace() because it
        # *always* forces a client connection.
        _settrace(
            stdoutToServer=redirect_output,
            stderrToServer=redirect_output,
            port=addr.port,
            suspend=False,
            _pydevd=_pydevd,
        )
        debug('pydevd enabled')
    t = new_hidden_thread('start-pydevd', start_pydevd)
    t.start()

    return daemon


def _ensure_current_thread_will_debug(enable, is_ready, readylock):
    # We use Python's tracing facilities to delay enabling debugging
    # in the current thread until pydevd has started.  More or less,
    # tracing allows us to run arbitrary code in a running thread.
    # In this case the thread is the one where "ptvsd.enable_attach()"
    # was called and the code we run enables debugging in that thread.
    # The catch is that tracing is triggered only when code is executing
    # and not if the code is blocking (e.g. IO, C code).  However,
    # that shouldn't be a problem in practice.  Also, since tracing can
    # trigger a lot, we must take care here not to add unnecessary
    # execution overhead.

    # pydevd relies on its own tracing function.  So we must be careful
    # to work around that.

    def handle_tracing():
        if not is_ready():
            return

        # Now we can enable debugging in the original thread.
        enable()  # Note: This waits for the "start_pydevd" thread.

        # Remove the tracing handler.
        debug('restoring original tracefunc')
        tracing.uninstall()

        # Allow pydevd to proceed.
        lock_release(readylock)
    debug('injecting temp tracefunc')
    tracing = TracingWrapper(handle_tracing)
    tracing.install()
    from _pydevd_bundle.pydevd_additional_thread_info import PyDBAdditionalThreadInfo
    print(PyDBAdditionalThreadInfo.iter_frames)
    return tracing


##################################
# monkey-patching sys.settrace()

class TracingWrapper(object):
    """A monkey-patcher for sys.settrace() that injects a handler per call."""

    HIDDEN = pydevd_trace_dispatch.trace_dispatch

    @classmethod
    def _get_caller(cls):
        f = sys._getframe()
        while f and f.f_code.co_name != 'enable_attach':
            f = f.f_back
        while f and f.f_code.co_name == 'enable_attach':
            f = f.f_back
        return f

    def __init__(self, handle_call):
        self._handle_call = handle_call
        self._tid = threading.current_thread().ident
        self._caller = self._get_caller()
        self._orig_settrace = sys.settrace
        self._orig_tracefunc = sys.gettrace()
        self._orig_f_tracefunc = self._caller.f_trace
        self._revive_frames = None

    def install(self):
        """Inject the wrapping settrace and tracefunc."""
        self._revive_frames = self._protect_frames()
        if self._orig_f_tracefunc is self.HIDDEN:
            def _local_tracefunc(frame, event, arg):
                print('          ** tracing local **')
                self._handle_call()
                return _local_tracefunc
        else:
            def _local_tracefunc(frame, event, arg):
                print('          ** tracing local **')
                self._handle_call()
                if self._orig_f_tracefunc is not None:
                    self._orig_f_tracefunc =  self._orig_f_tracefunc(
                        frame, event, arg)
                return _local_tracefunc

        with settrace_restored():
            self._orig_settrace = sys.settrace
            sys.settrace = self._settrace
            self._caller.f_trace = _local_tracefunc
            sys.settrace(self._tracefunc)
            # TODO: Also monkey-patch sys.gettrace()?

    def uninstall(self):
        """restore the wrapped settrace and tracefunc."""
        if self._revive_frames is not None:
            self._revive_frames()
        with settrace_restored():
            sys.settrace = self._orig_settrace
            self._caller.f_trace = self._orig_f_tracefunc
            sys.settrace(self._orig_tracefunc)

    # internal methods

    def _tracefunc(self, frame, event, arg):
        print('          ** tracing **')
        self._handle_call()

        if self._orig_tracefunc is None:
            return None
        return self._orig_tracefunc(frame, event, arg)

    def _settrace(self, tracefunc):
        tid = threading.current_thread().ident
        if tid != self._tid:
            self._orig_settrace(tracefunc)
        else:
            self._orig_tracefunc = tracefunc

    def _protect_frames(self):
        return protect_frames(
            (lambda f: f is self._caller),
            FrameWrapper,
        )


class FrameWrapper(object):  # types.FrameType

    def __new__(cls, frame):
        if type(frame) is cls:
            return frame
        return object.__new__(cls)

    def __init__(self, frame):
        self._frame = frame

    def __getattr__(self, name):
        return getattr(self._frame, name)

    def __setattr__(self, name, value):
        if name == 'f_trace':
            return
        setattr(self._frame, name, value)
