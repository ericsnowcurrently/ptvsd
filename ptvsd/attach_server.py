# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See LICENSE in the project root
# for license information.

import threading

# TODO: Why import run_module & run_file?
from ptvsd._local import run_module, run_file  # noqa
from ptvsd._remote import (
    enable_attach as ptvsd_enable_attach, _pydevd_settrace,
)


WAIT_TIMEOUT = 1.0

DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 5678

_enabled = False
_attached = threading.Event()
_debug_current_thread = None
_pending_threads = set()


def wait_for_attach(timeout=None):
    """If a remote debugger is attached, returns immediately. Otherwise,
    blocks until a remote debugger attaches to this process, or until the
    optional timeout occurs.

    Parameters
    ----------
    timeout : float, optional
        The timeout for the operation in seconds (or fractions thereof).
    """
    _attached.wait(timeout)

    tid = threading.current_thread().ident
    if tid in _pending_threads:
        _pending_threads.remove(tid)
        # Enable pydevd in the current thread.  This is necessary because
        # we started pydevd in a new thread.  We must do it here because
        # that previous invocation must have finished already.
        _debug_current_thread()


def enable_attach(address=(DEFAULT_HOST, DEFAULT_PORT), redirect_output=True):
    """Enables a client to attach to this process remotely to debug Python code.

    Parameters
    ----------
    address : (str, int), optional
        Specifies the interface and port on which the debugging server should
        listen for TCP connections. It is in the same format as used for
        regular sockets of the `socket.AF_INET` family, i.e. a tuple of
        ``(hostname, port)``. On client side, the server is identified by the
        Qualifier string in the usual ``'hostname:port'`` format, e.g.:
        ``'myhost.cloudapp.net:5678'``. Default is ``('0.0.0.0', 5678)``.
    redirect_output : bool, optional
        Specifies whether any output (on both `stdout` and `stderr`) produced
        by this program should be sent to the debugger. Default is ``True``.

    Notes
    -----
    This function returns immediately after setting up the debugging server,
    and does not block program execution. If you need to block until debugger
    is attached, call `ptvsd.wait_for_attach`. The debugger can be detached
    and re-attached multiple times after `enable_attach` is called.

    Only the thread on which this function is called, and any threads that are
    created after it returns, will be visible in the debugger once it is
    attached. Any threads that are already running before this function is
    called will not be visible.
    """
    global _enabled
    if _enabled:
        return
    _enabled = True
    _attached.clear()

    # Note: this only starts pydevd (e.g. sets it up) and enables
    # debugging for *future* threads.  It does not actually enable
    # debugging in the *current* thread.  That is done in
    # wait_for_attach().  Thus this approach is problematic if
    # wait_for_attach() is never called.
    # TODO: Is there any way to ensure that debug_current_thread()
    # gets called in the current thread, regardless of if
    # wait_for_attach() gets called?
    _, wait, debug_current_thread = ptvsd_enable_attach(
        address,
        on_attach=_attached.set,
        redirect_output=redirect_output,
    )
    global _debug_current_thread
    _debug_current_thread = debug_current_thread

    # Give it a chance to finish starting.  This helps reduce possible
    # issues due to relying on wait_for_attach().
    if wait(WAIT_TIMEOUT):
        debug_current_thread()
    else:
        _pending_threads.add(threading.current_thread().ident)


# TODO: Add disable_attach()?


def is_attached():
    """Returns ``True`` if debugger is attached, ``False`` otherwise."""
    return _attached.isSet()


def break_into_debugger():
    """If a remote debugger is attached, pauses execution of all threads,
    and breaks into the debugger with current thread as active.
    """
    if not _attached.isSet() or not _enabled:
        return

    import sys
    _pydevd_settrace(
        suspend=True,
        trace_only_current_thread=True,
        patch_multiprocessing=False,
        stop_at_frame=sys._getframe().f_back,
    )
