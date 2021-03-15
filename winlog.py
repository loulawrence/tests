from __future__ import unicode_literals

import atexit
import errno
import multiprocessing
import io
import os
import re
import select
import sys
import ctypes
import traceback
import signal
import tempfile
import subprocess
import threading
import queue
from threading import Thread
from contextlib import contextmanager
from os.path import sys

from typing import Optional  # novm
from types import ModuleType  # novm

class logwin32_output:
    def __init__(self, file_like=None, echo=False, debug=0, buffer=False, env=None):
        print("I AM IN INIT")
        self.file_like = file_like
        self.echo = echo
        self.debug = debug
        self.buffer = buffer
        self.env = env 
        self._active = False  # used to prevent re-entry
        # this part needed for libc.fflush and that is needed to capture libc output
        if sys.version_info < (3, 5):
            self.libc = ctypes.CDLL(ctypes.util.find_library('c'))
        else:
            if hasattr(sys, 'gettotalrefcount'): # debug build
                self.libc = ctypes.CDLL('ucrtbased')
            else:
                self.libc = ctypes.CDLL('api-ms-win-crt-stdio-l1-1-0')

    def __enter__(self):
        print("I AM IN ENTER")
        if self._active:
            raise RuntimeError("Can't re-enter the same log_output!")
        if self.file_like is None:
            raise RuntimeError(
                "file argument must be set by either __init__ or __call__")
        self.saved_stdout = sys.stdout.fileno()
        self.saved_stderr = sys.stderr.fileno()

        # Save a copy of the original stdout fd in saved_stdout_fd
        self.new_stdout = os.dup(sys.stdout.fileno())
        self.new_stderr = os.dup(sys.stderr.fileno())
        # Create a temporary file and redirect stdout to it
        self.tfile = tempfile.TemporaryFile(mode='w+b')
        self.tfile2 = tempfile.TemporaryFile(mode='w+b')
        self._redirect_stdout(self.tfile.fileno())
        self._redirect_stderr(self.tfile2.fileno())
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        print("I AM IN EXIT")
        self._redirect_stdout(self.saved_stdout)
        self._redirect_stderr(self.saved_stderr)
        # Copy contents of temporary file to the given stream
        self.log_file = open(self.file_like,"wb+")
        self.tfile.flush()
        self.tfile.seek(0, io.SEEK_SET)
        self.log_file.write(self.tfile.read())
        self.tfile2.flush()
        self.tfile2.seek(0, io.SEEK_SET)
        self.log_file.write(self.tfile2.read())
        self.log_file.close()
        self.tfile.close()
        os.close(self.saved_stderr)
        # track whether we're currently inside this log_output
        self._active = True

        # return this log_output object so that the user can do things
        # like temporarily echo some ouptut.
        self._active = False  # safe to enter again
        return self

    def _redirect_stdout(self, to_fd):
        self.libc.fflush(None) 
        sys.stdout.close()
        os.dup2(to_fd, self.saved_stdout)
        sys.stdout = io.TextIOWrapper(os.fdopen(self.saved_stdout, 'wb'))

    def _redirect_stderr(self, to_fd):
        self.libc.fflush(None) 
        sys.stderr.close()
        os.dup2(to_fd, self.saved_stdout)
        sys.stderr = io.TextIOWrapper(os.fdopen(self.saved_stderr, 'wb'))


class StreamWrapper:
    def __init__(self, sys_attr):
        self.sys_attr = sys_attr
        self.saved_stream = None
        if sys.platform.startswith('win32'):
            if sys.version_info < (3, 5):
                libc = ctypes.CDLL(ctypes.util.find_library('c'))
            else:
                if hasattr(sys, 'gettotalrefcount'):  # debug build
                    libc = ctypes.CDLL('ucrtbased')
                else:
                    libc = ctypes.CDLL('api-ms-win-crt-stdio-l1-1-0')

            kernel32 = ctypes.WinDLL('kernel32')

            # https://docs.microsoft.com/en-us/windows/console/getstdhandle
            if self.sys_attr == 'stdout':
                STD_HANDLE = -11
            elif self.sys_attr == 'stderr':
                STD_HANDLE = -12
            else:
                raise KeyError(self.sys_attr)

            c_stdout = kernel32.GetStdHandle(STD_HANDLE)
            self.libc = libc
            self.c_stream = c_stdout
        else:
            # The original fd stdout points to. Usually 1 on POSIX systems for stdout.
            self.libc = ctypes.CDLL(None)
            self.c_stream = ctypes.c_void_p.in_dll(self.libc, self.sys_attr)
        self.sys_stream = getattr(sys, self.sys_attr)
        self.orig_stream_fd = self.sys_stream.fileno()
        # Save a copy of the original stdout fd in saved_sys_stream_fd
        self.saved_stream = os.dup(self.orig_stream_fd)

    def _redirect_stream(self, to_fd):
        """Redirect stdout to the given file descriptor."""
        # Flush the C-level buffer stdout
        if sys.platform.startswith('win32'):
            self.libc.fflush(None)
        else:
            self.libc.fflush(self.c_stream)
        # Flush and close sys.stdout - also closes the file descriptor (fd)
        sys_stream = getattr(sys, self.sys_attr)
        sys_stream.flush()
        sys_stream.close()
        # Make orig_sys_stream_fd point to the same file as to_fd
        os.dup2(to_fd, self.orig_stream_fd)
        # Set sys.stdout to a new stream that points to the redirected fd
        new_buffer = open(self.orig_stream_fd, 'wb')
        new_stream = io.TextIOWrapper(new_buffer)
        setattr(sys, self.sys_attr, new_stream)
        self.sys_stream = getattr(sys, self.sys_attr)

    def flush(self):
        if sys.platform.startswith('win32'):
            self.libc.fflush(None)
        else:
            self.libc.fflush(self.c_stream)
        self.sys_stream.flush()

    def close(self):
        try:
            if self.saved_stream is not None:
                self._redirect_stream(self.saved_stream)
        finally:
            if self.saved_stream is not None:
                os.close(self.saved_stream)


class winlog:
    def __init__(self, logfile, echo ):
        self.echo = echo
        self.logfile = logfile
        self.stdout = StreamWrapper('stdout')
        self.stderr = StreamWrapper('stderr')

    def __enter__(self):
        self.writer = open(self.logfile, mode='wb+')
        self.writer.write(b'')
        self.reader = open(self.logfile, mode='rb+')
        #TEMPORARY: change to both later
        # Create a temporary file and redirect stdout to it
        self.echo_writer = open(os.dup(sys.stdout.fileno()), "w")
        self.stdout._redirect_stream(self.writer.fileno())
        self.stderr._redirect_stream(self.writer.fileno())
        self.logged_lines = []
        self._kill = threading.Event()
        def background_reader(reader, echo_writer, _kill):
            while True:
                is_killed = _kill.wait(.1)
                self.stdout.flush()
                self.stderr.flush()
                line = reader.readline()
                while line:
                    self.logged_lines.append(line)
                    if self.echo:
                        self.echo_writer.write('thread write: {}\n'.format(line))
                        self.echo_writer.flush()
                    line = reader.readline()

                if is_killed:
                    break

        self._thread = Thread(target=background_reader, args=(self.reader, self.echo_writer, self._kill))
        self._thread.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        print('THREAD: FINISHED REDIRECTION')
        self.echo_writer.flush()
        self.stdout.flush()
        self.stderr.flush()
        self._kill.set()
        self._thread.join()
        self.stdout.close()
        self.stderr.close()

        print('logged_lines = {!r}'.format(self.logged_lines))

if __name__ == '__main__':
    f = 'output.txt'
    with winlog(f, True) as logger:
        print('hello world')

        subprocess.run(['cmake', '--version'])
        subprocess.run(['cmake', 'badcommand'])
        subprocess.run(['cmake', '--version'])
        sys.stderr.write("Some Error!\n")
        print('hello world')
        #uncomment to confirm __exit__ on failure
        #raise KeyError("Key Error")
        
    print('finished redirection')
    sys.stderr.write("Another Error!\n")
    print('finished redirection')