import sys
import threading

import pydevd

from ptvsd._util import debug, new_hidden_thread, lock_release
from ptvsd.pydevd_hooks import install, start_server, settrace_restored
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
    _ensure_current_thread_will_debug(
        debug_current_thread,
        is_ready,
        readylock,
    )

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

    def tracefunc(frame, event, arg):
        if not is_ready():
            return None

        # Now we can enable debugging in the original thread.
        enable()  # Note: This waits for the "start_pydevd" thread.

        # Remove the tracing handler.
        debug('restoring original tracefunc')
        with settrace_restored():
            sys.settrace(None)

        # Allow pydevd to proceed.
        lock_release(readylock)

        return None
    debug('injecting temp tracefunc')
    with settrace_restored():
        assert sys.gettrace() is None  # TODO: Fix this.
        sys.settrace(tracefunc)
