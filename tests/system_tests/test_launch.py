import os
import os.path
from textwrap import dedent
import unittest

from ptvsd.wrapper import INITIALIZE_RESPONSE  # noqa
from tests.helpers.debugclient import EasyDebugClient as DebugClient
from tests.helpers.script import find_line

from . import (
    _match_event, _match_response,
    _strip_messages, _strip_exit, _strip_newline_output_events,
    lifecycle_handshake,
    LifecycleTestsBase,
)


PORT = 9876


def _fix_module_events(received, strip=True):
    def match(msg):
        return _match_event(msg, 'module')
    msgs = iter(received)
    for msg in msgs:
        if match(msg):
            # Ensure package is None, changes based on version
            # of Python.
            msg.body['module']['package'] = None
            if strip:
                yield msg
                break
        yield msg
    # We only care about the first 'module' event. (?)
    for msg in _strip_messages(msgs, match):
        yield msg


def _fix_stack_traces(received, maxframes=1, firstonly=True):
    msgs = iter(received)
    for msg in msgs:
        if _match_response(msg, 'stackTrace'):
            if 'stackFrames' in msg.body:
                msg.body['stackFrames'] = _fix_stack_trace(
                    msg.body['stackFrames'],
                    maxframes,
                )
                msg.body['totalFrames'] = len(msg.body['stackFrames'])
            if firstonly:
                yield msg
                break
        yield msg
    # We only care about the first one. (?)
    for msg in msgs:
        yield msg


def _fix_stack_trace(frames, maxframes):
    # Ignore non-user stack trace.
    if len(frames) > maxframes:
        frames = frames[:maxframes]
    return frames


def _fix_paths(received):
    for msg in received:
        if _match_event(msg, 'module'):
            msg.body['module']['path'] = _fix_path(msg.body['module']['path'])
        elif _match_response(msg, 'stackTrace'):
            for frame in msg.body['stackFrames']:
                frame['source']['path'] = _fix_path(frame['source']['path'])
        yield msg


def _fix_path(path):
    # Sometimes, the temp paths on mac get prefixed with /private/
    # So, during creation of the file its in /var/, but when debugging,
    # paths CAN soemtimes get returned with the /private/ prefix.
    # Here we just remove them.
    if path.startswith('/private/'):
        return path[len('/private'):]


class FileLifecycleTests(LifecycleTestsBase):
    def create_source_file(self, file_name, source):
        return self.write_script(file_name, source)

    def get_cwd(self):
        return None

    def find_line(self, filepath, label):
        with open(filepath) as scriptfile:
            script = scriptfile.read()
        return find_line(script, label)

    def get_test_info(self, source):
        filepath = self.create_source_file('spam.py', source)
        env = None
        expected_module = filepath
        argv = [filepath]
        return ('spam.py', filepath, env, expected_module, False, argv,
                self.get_cwd())

    def test_with_output(self):
        source = dedent("""
            import sys
            sys.stdout.write('ok')
            sys.stderr.write('ex')
            """)
        options = {'debugOptions': ['RedirectOutput']}
        (filename, filepath, env, expected_module, is_module, argv,
         cwd) = self.get_test_info(source)

        with DebugClient(port=PORT) as editor:
            adapter, session = editor.host_local_debugger(
                argv, env=env, cwd=cwd)
            with session.wait_for_event('exited'):
                with session.wait_for_event('thread'):
                    (req_initialize,
                     req_launch,
                     req_config,
                     _, _, _,
                     ) = lifecycle_handshake(session, 'launch',
                                             options=options)

            adapter.wait()

        received = list(_strip_newline_output_events(session.received))
        # Skipping the 'thread exited' and 'terminated' messages which
        # may appear randomly in the received list.
        received = list(_strip_exit(received))
        self.assert_received(received, [
            self.new_version_event(received),
            self.new_response(req_initialize, **INITIALIZE_RESPONSE),
            self.new_event('initialized'),
            self.new_response(req_launch),
            self.new_response(req_config),
            self.new_event('process', **{
                'isLocalProcess': True,
                'systemProcessId': adapter.pid,
                'startMethod': 'launch',
                'name': expected_module,
            }),
            self.new_event('thread', reason='started', threadId=1),
            self.new_event('output', category='stdout', output='ok'),
            self.new_event('output', category='stderr', output='ex'),
        ])

    def test_with_arguments(self):
        source = dedent("""
            import sys
            print(len(sys.argv))
            for arg in sys.argv:
                print(arg)
            """)
        options = {'debugOptions': ['RedirectOutput']}
        (filename, filepath, env, expected_module, is_module, argv,
         cwd) = self.get_test_info(source)

        #from tests.helpers.debugadapter import DebugAdapter
        #DebugAdapter.VERBOSE = True
        with DebugClient(port=PORT) as editor:
            adapter, session = editor.host_local_debugger(
                argv=argv + ['1', 'Hello', 'World'], env=env, cwd=cwd)
            with session.wait_for_event('exited'):
                with session.wait_for_event('thread'):
                    (
                        req_initialize,
                        req_launch,
                        req_config,
                        _,
                        _,
                        _,
                    ) = lifecycle_handshake(
                        session, 'launch', options=options)

            adapter.wait()

        received = list(_strip_newline_output_events(session.received))
        # Skipping the 'thread exited' and 'terminated' messages which
        # may appear randomly in the received list.
        received = list(_strip_exit(received))
        self.assert_received(received, [
            self.new_version_event(received),
            self.new_response(req_initialize, **INITIALIZE_RESPONSE),
            self.new_event('initialized'),
            self.new_response(req_launch),
            self.new_response(req_config),
            self.new_event('process', **{
                'isLocalProcess': True,
                'systemProcessId': adapter.pid,
                'startMethod': 'launch',
                'name': expected_module,
            }),
            self.new_event('thread', reason='started', threadId=1),
            self.new_event('output', category='stdout', output='4'),
            self.new_event(
                'output', category='stdout', output=expected_module),
            self.new_event('output', category='stdout', output='1'),
            self.new_event('output', category='stdout', output='Hello'),
            self.new_event('output', category='stdout', output='World'),
        ])

    def test_with_break_points(self):
        source = dedent("""
            a = 1
            b = 2
            # <Token>
            c = 3
            """)
        (filename, filepath, env, expected_module, is_module, argv,
         cwd) = self.get_test_info(source)

        bp_line = self.find_line(filepath, 'Token')
        breakpoints = [{
            'source': {
                'path': filepath
            },
            'breakpoints': [{
                'line': bp_line
            }]
        }]

        with DebugClient(port=PORT, connecttimeout=3.0) as editor:
            adapter, session = editor.host_local_debugger(
                argv, env=env, cwd=cwd)
            with session.wait_for_event('terminated'):
                with session.wait_for_event('stopped') as result:
                    (
                        req_initialize,
                        req_launch,
                        req_config,
                        reqs_bps,
                        _,
                        _,
                    ) = lifecycle_handshake(
                        session, 'launch', breakpoints=breakpoints)
                req_bps, = reqs_bps  # There should only be one.
                tid = result['msg'].body['threadId']

                req_stacktrace = session.send_request(
                    'stackTrace', threadId=tid)

                with session.wait_for_event('continued'):
                    req_continue = session.send_request(
                        'continue', threadId=tid)

            adapter.wait()

        received = list(_strip_newline_output_events(session.received))

        # Cleanup.
        received = list(_fix_module_events(received))
        received = list(_fix_stack_traces(received))

        # Skipping the 'thread exited' and 'terminated' messages which
        # may appear randomly in the received list.
        received = list(_strip_exit(received))
        self.assert_received(received, [
            self.new_version_event(received),
            self.new_response(req_initialize, **INITIALIZE_RESPONSE),
            self.new_event('initialized'),
            self.new_response(req_launch),
            self.new_response(
                req_bps, **{
                    'breakpoints': [{
                        'id': 1,
                        'line': bp_line,
                        'verified': True
                    }]
                }),
            self.new_response(req_config),
            self.new_event(
                'process', **{
                    'isLocalProcess': True,
                    'systemProcessId': adapter.pid,
                    'startMethod': 'launch',
                    'name': expected_module,
                }),
            self.new_event('thread', reason='started', threadId=tid),
            self.new_event(
                'stopped',
                reason='breakpoint',
                threadId=tid,
                text=None,
                description=None,
            ),
            self.new_event(
                'module',
                module={
                    'id': 1,
                    'name': '__main__',
                    'path': filepath,
                    'package': None,
                },
                reason='new',
            ),
            self.new_response(
                req_stacktrace,
                **{
                    'totalFrames':
                    1,
                    'stackFrames': [{
                        'id': 1,
                        'name': '<module>',
                        'source': {
                            'path': filepath,
                            'sourceReference': 0
                        },
                        'line': bp_line,
                        'column': 1,
                    }],
                }),
            self.new_response(req_continue),
            self.new_event('continued', threadId=tid),
        ])

    @unittest.skip('Needs fixing')
    def test_with_break_points_across_files(self):
        source = dedent("""
            from . import bar
            def foo():
                # <Token>
                bar.do_something()
            foo()
            """)
        (filename, filepath, env, expected_module, is_module, argv,
         cwd) = self.get_test_info(source)
        foo_line = self.find_line(filepath, 'Token')

        source = dedent("""
            def do_something():
                # <Token>
                print("inside bar")
            """)
        bar_filepath = self.create_source_file('bar.py', source)
        bp_line = self.find_line(bar_filepath, 'Token')
        breakpoints = [{
            'source': {
                'path': bar_filepath
            },
            'breakpoints': [{
                'line': bp_line
            }],
            'lines': [bp_line]
        }]

        with DebugClient(port=PORT, connecttimeout=3.0) as editor:
            adapter, session = editor.host_local_debugger(
                argv, env=env, cwd=cwd)
            with session.wait_for_event('terminated'):
                with session.wait_for_event('stopped') as result:
                    (
                        req_initialize,
                        req_launch,
                        req_config,
                        reqs_bps,
                        _,
                        _,
                    ) = lifecycle_handshake(
                        session, 'launch', breakpoints=breakpoints)

                req_bps, = reqs_bps  # There should only be one.
                tid = result['msg'].body['threadId']

                req_stacktrace = session.send_request(
                    'stackTrace', threadId=tid)

                with session.wait_for_event('continued'):
                    req_continue = session.send_request(
                        'continue', threadId=tid)

            adapter.wait()

        received = list(_strip_newline_output_events(session.received))

        # Cleanup.
        received = list(_fix_module_events(received))
        received = list(_fix_stack_traces(received, maxframes=2))
        received = list(_fix_paths(received))

        # Skipping the 'thread exited' and 'terminated' messages which
        # may appear randomly in the received list.
        received = list(_strip_exit(received))
        self.assert_received(received, [
            self.new_version_event(received),
            self.new_response(req_initialize, **INITIALIZE_RESPONSE),
            self.new_event('initialized'),
            self.new_response(req_launch),
            self.new_response(
                req_bps, **{
                    'breakpoints': [{
                        'id': 1,
                        'line': bp_line,
                        'verified': True
                    }]
                }),
            self.new_response(req_config),
            self.new_event(
                'process', **{
                    'isLocalProcess': True,
                    'systemProcessId': adapter.pid,
                    'startMethod': 'launch',
                    'name': expected_module,
                }),
            self.new_event('thread', reason='started', threadId=tid),
            self.new_event(
                'stopped',
                reason='breakpoint',
                threadId=tid,
                text=None,
                description=None,
            ),
            self.new_event(
                'module',
                module={
                    'id': 1,
                    'name': 'mymod.bar' if is_module else 'bar',
                    'path': None if is_module else bar_filepath,
                    'package': None,
                },
                reason='new',
            ),
            self.new_response(
                req_stacktrace,
                **{
                    'totalFrames': 2,
                    'stackFrames': [{
                        'id': 1,
                        'name': 'do_something',
                        'source': {
                            'path': None if is_module else bar_filepath,
                            'sourceReference': 0
                        },
                        'line': bp_line,
                        'column': 1,
                    }, {
                        'id': 2,
                        'name': 'foo',
                        'source': {
                            'path': None,
                            'sourceReference': 0
                        },
                        'line': foo_line,
                        'column': 1,
                    }],
                }),
            self.new_response(req_continue),
            self.new_event('continued', threadId=tid),
        ])

    def test_with_log_points(self):
        source = dedent("""
            print('foo')
            a = 1
            for i in range(2):
                # <Token>
                b = i
            print('bar')
            """)
        (filename, filepath, env, expected_module, is_module, argv,
         cwd) = self.get_test_info(source)
        bp_line = self.find_line(filepath, 'Token')
        breakpoints = [{
            'source': {
                'path': filepath,
                'name': filename
            },
            'breakpoints': [{
                'line': bp_line,
                'logMessage': '{a + i}'
            }],
            'lines': [bp_line]
        }]
        options = {'debugOptions': ['RedirectOutput']}

        with DebugClient(port=PORT, connecttimeout=3.0) as editor:
            adapter, session = editor.host_local_debugger(
                argv, env=env, cwd=cwd)
            with session.wait_for_event('terminated'):
                (
                    req_initialize,
                    req_launch,
                    req_config,
                    reqs_bps,
                    _,
                    _,
                ) = lifecycle_handshake(
                    session,
                    'launch',
                    breakpoints=breakpoints,
                    options=options)
                req_bps, = reqs_bps  # There should only be one.

            adapter.wait()

        received = list(_strip_newline_output_events(session.received))
        # Skipping the 'thread exited' and 'terminated' messages which
        # may appear randomly in the received list.
        received = list(_strip_exit(received))
        self.assert_received(received, [
            self.new_version_event(received),
            self.new_response(req_initialize, **INITIALIZE_RESPONSE),
            self.new_event('initialized'),
            self.new_response(req_launch),
            self.new_response(
                req_bps, **{
                    'breakpoints': [{
                        'id': 1,
                        'line': bp_line,
                        'verified': True
                    }]
                }),
            self.new_response(req_config),
            self.new_event(
                'process', **{
                    'isLocalProcess': True,
                    'systemProcessId': adapter.pid,
                    'startMethod': 'launch',
                    'name': expected_module,
                }),
            self.new_event('thread', reason='started', threadId=1),
            self.new_event('output', category='stdout', output='foo'),
            self.new_event(
                'output', category='stdout',
                output='1' + os.linesep),
            self.new_event(
                'output', category='stdout',
                output='2' + os.linesep),
            self.new_event('output', category='stdout', output='bar'),
        ])

    def test_with_conditional_break_points(self):
        source = dedent("""
            a = 1
            b = 2
            for i in range(5):
                # <Token>
                print(i)
            """)
        (filename, filepath, env, expected_module, is_module, argv,
         cwd) = self.get_test_info(source)
        bp_line = self.find_line(filepath, 'Token')
        breakpoints = [{
            'source': {
                'path': filepath,
                'name': filename
            },
            'breakpoints': [{
                'line': bp_line,
                'condition': 'i == 2'
            }],
            'lines': [bp_line]
        }]
        options = {'debugOptions': ['RedirectOutput']}

        with DebugClient(port=PORT, connecttimeout=3.0) as editor:
            adapter, session = editor.host_local_debugger(
                argv, env=env, cwd=cwd)
            with session.wait_for_event('terminated'):
                with session.wait_for_event('stopped') as result:
                    (
                        req_initialize,
                        req_launch,
                        req_config,
                        reqs_bps,
                        _,
                        _,
                    ) = lifecycle_handshake(
                        session,
                        'launch',
                        breakpoints=breakpoints,
                        options=options)
                req_bps, = reqs_bps  # There should only be one.
                tid = result['msg'].body['threadId']

                # with session.wait_for_response('stopped') as result:
                req_stacktrace = session.send_request(
                    'stackTrace', threadId=tid, wait=True)

                with session.wait_for_event('continued'):
                    req_continue = session.send_request(
                        'continue', threadId=tid)

            adapter.wait()

        received = list(_strip_newline_output_events(session.received))

        # Cleanup.
        received = list(_fix_module_events(received))
        received = list(_fix_stack_traces(received))

        # Skipping the 'thread exited' and 'terminated' messages which
        # may appear randomly in the received list.
        received = list(_strip_exit(received))
        self.assert_received(received, [
            self.new_version_event(received),
            self.new_response(req_initialize, **INITIALIZE_RESPONSE),
            self.new_event('initialized'),
            self.new_response(req_launch),
            self.new_response(
                req_bps, **{
                    'breakpoints': [{
                        'id': 1,
                        'line': bp_line,
                        'verified': True
                    }]
                }),
            self.new_response(req_config),
            self.new_event(
                'process', **{
                    'isLocalProcess': True,
                    'systemProcessId': adapter.pid,
                    'startMethod': 'launch',
                    'name': expected_module,
                }),
            self.new_event('thread', reason='started', threadId=tid),
            self.new_event('output', category='stdout', output='0'),
            self.new_event('output', category='stdout', output='1'),
            self.new_event(
                'stopped',
                reason='breakpoint',
                threadId=tid,
                text=None,
                description=None,
            ),
            self.new_event(
                'module',
                module={
                    'id': 1,
                    'name': '__main__',
                    'path': filepath,
                    'package': None,
                },
                reason='new',
            ),
            self.new_response(
                req_stacktrace,
                **{
                    'totalFrames':
                    1,
                    'stackFrames': [{
                        'id': 1,
                        'name': '<module>',
                        'source': {
                            'path': filepath,
                            'sourceReference': 0
                        },
                        'line': bp_line,
                        'column': 1,
                    }],
                }),
            self.new_response(req_continue),
            self.new_event('continued', threadId=tid),
            self.new_event('output', category='stdout', output='2'),
            self.new_event('output', category='stdout', output='3'),
            self.new_event('output', category='stdout', output='4'),
        ])

    @unittest.skip('termination needs fixing')
    def test_terminating_program(self):
        source = dedent("""
            import time

            while True:
                time.sleep(0.1)
            """)
        (filename, filepath, env, expected_module, argv,
         module_name) = self.get_test_info(source)

        with DebugClient(port=PORT, connecttimeout=3.0) as editor:
            adapter, session = editor.host_local_debugger(argv)
            with session.wait_for_event('terminated'):
                (req_initialize, req_launch, req_config, _, _,
                 _) = lifecycle_handshake(  # noqa
                     session, 'launch')

                session.send_request('disconnect')

            adapter.wait()


class FileWithCWDLifecycleTests(FileLifecycleTests):
    def get_cwd(self):
        return os.path.dirname(__file__)


# class ModuleLifecycleTests(FileLifecycleTests):
#     def create_source_file(self, file_name, source):
#         return self.write_script(os.path.join('mymod', file_name), source)

#     def get_test_info(self, source):
#         module_name = 'mymod'
#         self.workspace.ensure_dir(module_name)
#         self.create_source_file('__main__.py', '')

#         filepath = self.create_source_file('__init__.py', source)
#         env = {'PYTHONPATH': os.path.dirname(os.path.dirname(filepath))}
#         expected_module = module_name + ':'
#         argv = ['-m', module_name]

#         return ('__init__.py', filepath, env, expected_module, True, argv,
#                 self.get_cwd())


# class ModuleWithCWDLifecycleTests(ModuleLifecycleTests,
#                                   FileWithCWDLifecycleTests):  # noqa
#     def get_cwd(self):
#         return os.path.dirname(__file__)
