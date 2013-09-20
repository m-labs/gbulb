# vim:sw=4:sts=4:nosta:et:
"""PEP 3156 event loop based on GLib"""

from gi.repository import GLib, GObject, Gtk

from tulip import events
from tulip import futures
from tulip import tasks
from tulip.log import tulip_log

from . import unix_events

import threading
import signal
import weakref
import collections
import os


class GLibHandle(events.Handle):
    def __init__(self, loop, source, repeat, callback, args):
        super().__init__(callback, args)

        self._loop   = loop
        self._source = source
        self._repeat = repeat
        self._ready  = False
        source.set_callback(self.__class__._callback, self)
        source.attach(loop._context)
        loop._handlers.add(self)

    def cancel(self):
        super().cancel()
        self._source.destroy()
        self._loop._handlers.discard(self)

    def _run(self):
        self._ready = False
        super()._run()

    def _callback(self):
        if not self._ready:
            self._ready = True
            self._loop._ready.append(self)

        self._loop._dispatch()

        if not self._repeat:
            self._loop._handlers.discard(self)
        return self._repeat


class GLibChildHandle(events.Handle):
    def __init__(self, loop, pid, callback, args):

        if loop._context != GLib.main_context_default():
            raise RuntimeError("Children processes cannot be monitored outside the main event loop")
        super().__init__(callback, args)

        self._loop   = loop
        self._source = GLib.child_watch_add(0, pid, self.__class__._callback, self)
        loop._handlers.add(self)

    def cancel(self):
        super().cancel()
        GLib.source_remove(self._source)
        self._loop._handlers.discard(self)

    @staticmethod
    def _callback(pid, status, self):
        print("Child callback", pid, status, self)

        if os.WIFSIGNALED(status):
            print("signaled!")
            returncode = -os.WTERMSIG(status)
        elif os.WIFEXITED(status):
            print("exited!")
            returncode = os.WEXITSTATUS(status)

            #FIXME: Hack for adjusting invalid status returned by GLIB
            #	Looks like there is a bug in glib or in pygobject
            if returncode > 128:
                returncode = 128 - returncode
        else:
            returncode = None
        print("-> returncode", returncode)

        self._args += returncode,

        self._loop._ready.append(self)
        self._loop._dispatch()


#
# Divergences with PEP 3156
#
# In GLib, the concept of event loop is split in two classes: GLib.MainContext
# and GLib.MainLoop.
#
# The thing is mostly implemented by MainContext. MainLoop is just a wrapper
# that implements the run() and quit() functions. MainLoop.run() atomically
# acquires a MainContext and repeatedly calls MainContext.iteration() until
# MainLoop.quit() is called.
#
# A MainContext is not bound to a particular thread, however is cannot be used
# by multiple threads concurrently. If the context is owned by another thread,
# then MainLoop.run() will block until the context is released by the other
# thread.
#
# MainLoop.run() may be called recursively by the same thread (this is mainly
# used for implementing modal dialogs in Gtk).
#
#
# The issue: given a context, GLib provides no ways to know if there is an
# existing event loop running for that context. It implies the following
# divergences with PEP 3156:
#
#  - .run_forever() and .run_until_complete() are not guaranteed to run
#    immediatly. If the context is owned by another thread, then they will
#    block until the context is released by the other thread.
#
#  - .stop() is relevant only when the currently running Glib.MainLoop object
#    was created by this tulip object (i.e. by calling .run_forever() or
#    .run_until_complete()). The event loop will quit only when it regains
#    control of the context. This can happen in two cases:
#     1. when multiple event loop are enclosed (by creating new MainLoop
#        objects and calling .run() recursively)
#     2. when the event loop has not even yet started because it is still
#        trying to acquire the context
#
# It should be wiser not to use any recursion at all. GLibEventLoop will
# actually prevent you from doing that (in accordance with PEP 3156). However
# you should keep in mind that enclosed loops may be started at any time by
# third-party code calling directly GLib's primitives.
#
#
# TODO: documentation about signal GLib allows catching signals from any
# thread. It is dispatched to the first handler whose flag is not yet raised.
#
# about SIGINT -> KeyboardInterrupt will never be raised asynchronously

class BaseGlibEventLoop(unix_events.SelectorEventLoop):
    """GLib base event loop

    This class handles only the operations related to Glib.MainContext objects.

    Glib.MainLoop operations are implemented in the derived classes.
    """

    class DefaultSigINTHandler:
        def __init__(self):
            s = GLib.unix_signal_source_new(signal.SIGINT)
            s.attach()
            s.set_callback(self.__class__._callback, self)

            self._source = s
            self._loop   = None

        def attach(self, loop):
            if self._loop:
                l = self._loop()
                if l and l != loop:
                    tulip_log.warning(
                        "Multiple event loops for the GLib default context. "
                        "SIGINT may not be caught reliably")

            self._loop = weakref.ref(loop)

        def detach(self, loop):
            if self._loop:
                l = self._loop()
                if l == loop:
                    self._loop = None

        def _callback(self):
            if self._loop:
                l = self._loop()
                if l:
                    def interrupt(loop):
                        loop._interrupted = True
                        loop.stop()

                    l.call_soon_threadsafe(interrupt, l)
            return True

    @staticmethod
    def init_class():
        if not hasattr(BaseGlibEventLoop, "_default_sigint_handler"):
            BaseGlibEventLoop._default_sigint_handler = BaseGlibEventLoop.DefaultSigINTHandler()

    def __init__(self, glib_context=None):

        self._context = glib_context if glib_context else GLib.MainContext()

        self._readers = {}
        self._writers = {}
        self._sighandlers = {}
        self._chldhandlers = {}
        self._handlers = set()
        self._ready   = collections.deque()
        self._wakeup  = None
        self._will_dispatch = False

        super().__init__()

        # install a default handler for SIGINT
        # in the default context
        if self._context == GLib.main_context_default():
            assert hasattr(self, "_default_sigint_handler"), "Must call BaseGlibEventLoop.init_class() first"
            self._default_sigint_handler.attach(self)

    def _dispatch(self):
#		print("dispatch enter", self._ready)
        # This is the only place where callbacks are actually *called*. All
        # other places just add them to ready. Note: We run all currently
        # scheduled callbacks, but not any callbacks scheduled by callbacks run
        # this time around -- they will be run the next time (after another I/O
        # poll). Use an idiom that is threadsafe without using locks.

        self._will_dispatch = True

        ntodo = len(self._ready)
        for i in range(ntodo):
            handle = self._ready.popleft()
            if not handle._cancelled:
                print ("dispatch", handle)
                handle._run()

        self._schedule_dispatch()
        self._will_dispatch = False

    def _schedule_dispatch(self):
        if not self._ready or self._wakeup is not None:
            return

        def wakeup_cb(self):
            self._dispatch()
            if self._ready:
                return True
            else:
                self._wakeup.destroy()
                self._wakeup = None
                return False

        self._wakeup = GLib.Timeout(0)
        self._wakeup.attach(self._context)
        self._wakeup.set_callback(wakeup_cb, self)

#		print("dispatch leave", self._ready)

    def run_until_complete(self, future):
        """Run the event loop until a Future is done.

        Return the Future's result, or raise its exception.
        """

        def stop(f):
            self.stop()

        future = tasks.async(future, loop=self)
        future.add_done_callback(stop)
        try:
            self.run_forever()
        finally:
            future.remove_done_callback(stop)

        if not future.done():
            raise RuntimeError('Event loop stopped before Future completed.')

        return future.result()

    def run_forever(self):
        """Run the event loop until stop() is called."""

        raise NotImplementedError

    def is_running(self):
        """Return whether the event loop is currently running."""
        raise NotImplementedError

    def close(self):
        for s in list(self._handlers):
            s.cancel()
        self._default_sigint_handler.detach(self)

        super().close()

    # Methods scheduling callbacks.  All these return Handles.
    def call_soon(self, callback, *args):
        print("call_soon", self, callback, args)
        h = events.Handle(callback, args)
        self._ready.append(h)
        if not self._will_dispatch:
            self._schedule_dispatch()
        return h

    def call_later(self, delay, callback, *args):

        if delay <= 0:
            return self.call_soon(callback, *args)
        else:
            return GLibHandle(
                self,
                GLib.Timeout(delay*1000 if delay > 0 else 0),
                False,
                callback, args)

    def call_at(self, when, callback, *args):
        return self.call_later(when - self.time(), callback, *args)

    def time(self):
        return GLib.get_monotonic_time() / 1000000

    # Methods for interacting with threads.

#	def call_soon_threadsafe(self, callback, *args):
#		raise NotImplementedError
#
#	def run_in_executor(self, executor, callback, *args):
#		raise NotImplementedError
#
#	def set_default_executor(self, executor):
#		raise NotImplementedError

    # Network I/O methods returning Futures.

#	def getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
#		raise NotImplementedError
#
#	def getnameinfo(self, sockaddr, flags=0):
#		raise NotImplementedError
#
#	def create_connection(self, protocol_factory, host=None, port=None, *,
#						  ssl=None, family=0, proto=0, flags=0, sock=None,
#						  local_addr=None):
#		raise NotImplementedError
#
#	def start_serving(self, protocol_factory, host=None, port=None, *,
#					  family=socket.AF_UNSPEC, flags=socket.AI_PASSIVE,
#					  sock=None, backlog=100, ssl=None, reuse_address=None):
#		"""A coroutine which creates a TCP server bound to host and
#		port and whose result will be a list of socket objects which
#		will later be handled by protocol_factory.
#
#		If host is an empty string or None all interfaces are assumed
#		and a list of multiple sockets will be returned (most likely
#		one for IPv4 and another one for IPv6).
#
#		family can be set to either AF_INET or AF_INET6 to force the
#		socket to use IPv4 or IPv6. If not set it will be determined
#		from host (defaults to AF_UNSPEC).
#
#		flags is a bitmask for getaddrinfo().
#
#		sock can optionally be specified in order to use a preexisting
#		socket object.
#
#		backlog is the maximum number of queued connections passed to
#		listen() (defaults to 100).
#
#		ssl can be set to an SSLContext to enable SSL over the
#		accepted connections.
#
#		reuse_address tells the kernel to reuse a local socket in
#		TIME_WAIT state, without waiting for its natural timeout to
#		expire. If not specified will automatically be set to True on
#		UNIX.
#		"""
#		raise NotImplementedError
#
#	def stop_serving(self, sock):
#		"""Stop listening for incoming connections. Close socket."""
#		raise NotImplementedError
#
#	def create_datagram_endpoint(self, protocol_factory,
#								 local_addr=None, remote_addr=None, *,
#								 family=0, proto=0, flags=0):
#		raise NotImplementedError
#
#	def connect_read_pipe(self, protocol_factory, pipe):
#		"""Register read pipe in eventloop.
#
#		protocol_factory should instantiate object with Protocol interface.
#		pipe is file-like object already switched to nonblocking.
#		Return pair (transport, protocol), where transport support
#		ReadTransport ABC"""
#		# The reason to accept file-like object instead of just file descriptor
#		# is: we need to own pipe and close it at transport finishing
#		# Can got complicated errors if pass f.fileno(),
#		# close fd in pipe transport then close f and vise versa.
#		raise NotImplementedError
#
#	def connect_write_pipe(self, protocol_factory, pipe):
#		"""Register write pipe in eventloop.
#
#		protocol_factory should instantiate object with BaseProtocol interface.
#		Pipe is file-like object already switched to nonblocking.
#		Return pair (transport, protocol), where transport support
#		WriteTransport ABC"""
#		# The reason to accept file-like object instead of just file descriptor
#		# is: we need to own pipe and close it at transport finishing
#		# Can got complicated errors if pass f.fileno(),
#		# close fd in pipe transport then close f and vise versa.
#		raise NotImplementedError
#
#	def subprocess_shell(self, protocol_factory, cmd, *, stdin=subprocess.PIPE,
#						 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
#						 **kwargs):
#		raise NotImplementedError
#
#	def subprocess_exec(self, protocol_factory, *args, stdin=subprocess.PIPE,
#						stdout=subprocess.PIPE, stderr=subprocess.PIPE,
#						**kwargs):
#		raise NotImplementedError

    # Ready-based callback registration methods.
    # The add_*() methods return None.
    # The remove_*() methods return True if something was removed,
    # False if there was nothing to delete.

    # FIXME: these functions are not available on windows
    def add_reader(self, fd, callback, *args):
        if not isinstance(fd, int):
            fd = fd.fileno()

        self.remove_reader(fd)

        s = GLib.unix_fd_source_new(fd, GLib.IO_IN)

        assert fd not in self._readers
        self._readers[fd] = GLibHandle(self, s, True, callback, args)

    def remove_reader(self, fd):
        if not isinstance(fd, int):
            fd = fd.fileno()

        try:
            self._readers.pop(fd).cancel()
            return True

        except KeyError:
            return False

    def add_writer(self, fd, callback, *args):
        if not isinstance(fd, int):
            fd = fd.fileno()

        self.remove_writer(fd)

        s = GLib.unix_fd_source_new(fd, GLib.IO_OUT)

        assert fd not in self._writers
        self._writers[fd] = GLibHandle(self, s, True, callback, args)

    def remove_writer(self, fd):
        if not isinstance(fd, int):
            fd = fd.fileno()

        try:
            self._writers.pop(fd).cancel()
            return True

        except KeyError:
            return False

    # Completion based I/O methods returning Futures.

#	def sock_recv(self, sock, nbytes):
#		raise NotImplementedError
#
#	def sock_sendall(self, sock, data):
#		raise NotImplementedError
#
#	def sock_connect(self, sock, address):
#		raise NotImplementedError
#
#	def sock_accept(self, sock):
#		raise NotImplementedError

    # Signal handling.

    def add_signal_handler(self, sig, callback, *args):
        self._check_signal(sig)
        self.remove_signal_handler(sig)

        s = GLib.unix_signal_source_new(sig)
        if s is None:
            if sig == signal.SIGKILL:
                raise RuntimeError("cannot catch SIGKILL")
            else:
                raise ValueError("signal not supported")

        assert sig not in self._sighandlers
        self._sighandlers[sig] = GLibHandle(self, s, True, callback, args)

    def remove_signal_handler(self, sig):
        self._check_signal(sig)
        try:
            self._sighandlers.pop(sig).cancel()
            return True

        except KeyError:
            return False

    def _add_child_handler(self, pid, callback, *args):
        self._remove_child_handler(pid)

        assert pid not in self._chldhandlers
        self._chldhandlers[pid] = GLibChildHandle(self, pid, callback, args)

    def _remove_child_handler(self, pid):
        try:
            self._chldhandlers.pop(pid).cancel()
            return True

        except KeyError:
            return False


class GLibEventLoop(BaseGlibEventLoop):
    """GLib event loop

    See the module documentation for more details
    """

    def __init__(self, glib_context=None):
        super().__init__(glib_context)

        self._mainloop    = None
        self._interrupted = False

    def run_until_complete(self, future):
        """Run the event loop until a Future is done.

        Return the Future's result, or raise its exception.
        """

        def stop(f):
            self.stop()

        future = tasks.async(future, loop=self)
        future.add_done_callback(stop)
        try:
            self.run_forever()
        finally:
            future.remove_done_callback(stop)

        if not future.done():
            raise RuntimeError('Event loop stopped before Future completed.')

        return future.result()

    def run_forever(self):
        """Run the event loop until stop() is called."""

        if self._mainloop is not None:
            raise RuntimeError('Event loop is running.')

        # We use the introspected MainLoop object directly, because the
        # override in pygobject tampers with SIGINT
        self._mainloop = l = GLib._introspection_module.MainLoop.new(self._context, True)
        try:
            self._dispatch()

            # We actually run the loop only if it is was already interrupted.
            # This may happen if .stop() was called in the dispatched
            # callbacks.
            if l.is_running():
                print("enter run() %r" % l)
                l.run()
                print("leave run() %r" % l)

            if self._interrupted:
                # ._interrupted is set when SIGINT is caught be the default
                # signal handler implemented in this module.
                #
                # If no user-defined handler is registered, then the default
                # behaviour is just to raise KeyboardInterrupt
                #
                self._interrupted = False
                raise KeyboardInterrupt()
        finally:
            self._mainloop = None

    def stop(self):
        if self._mainloop is not None:
            self._mainloop.quit()

    def is_running(self):
        """Return whether the event loop is currently running."""

        return self._mainloop is not None


class GtkEventLoop(BaseGlibEventLoop):
    """Gtk event loop

    See the module documentation for more details
    """

    def __init__(self):
        super().__init__(GLib.main_context_default())

        self._running     = False
        self._interrupted = False

    def run_forever(self):
        """Run the event loop until stop() is called."""

        if self._running:
            raise RuntimeError('Event loop is running.')

        # We do not run the callbacks immediately. We need to call them
        # when the Gtk loop is running, in case one callback calls .stop()
        self._schedule_dispatch()

        self._running = True
        try:
            Gtk.main()

            if self._interrupted:
                # ._interrupted is set when SIGINT is caught be the default
                # signal handler implemented in this module.
                #
                # If no user-defined handler is registered, then the default
                # behaviour is just to raise KeyboardInterrupt
                #
                self._interrupted = False
                raise KeyboardInterrupt()
        finally:
            self._running = False

    def stop(self):
        if self._running:
            Gtk.main_quit()

    def is_running(self):
        """Return whether the event loop is currently running."""
        return self._running


class GLibEventLoopPolicy(events.AbstractEventLoopPolicy):
    """Default GLib event loop policy

    In this policy, each thread has its own event loop.  However, we only
    automatically create an event loop by default for the main thread; other
    threads by default have no event loop.
    """

    #TODO add a parameter to synchronise with GLib's thead default contextes
    #	(g_main_context_push_thread_default())
    def __init__(self, *, full=False, default=True, threads=True):
        """Constructor

        threads     Multithread support (default: True)

            Indicates whether you indend to use multiple python threads in your
            application. When set this flags disables some optimisations
            related to the GIL. (see GObject.threads_init())

        full        Full GLib (default: False)

            By default the policy is to create a GLibEventLoop object only for
            the main thread. Other threads will use regular tulip event loops.
            If this flag is set, then this policy will use a glib event loop
            for every thread. Use this parameter if you want your loops to
            interact with modules written in other languages.

        default     Use the default context (default: True)

            Indicates whether you want to use the GLib default context. If set,
            then the loop associated with the main thread will use the default
            (NULL) GLib context (instead of creating a new one).
        """
        self._full    = full
        self._default = default

        self._default_loop = None

        self._policy  = events.DefaultEventLoopPolicy()
        self._policy.new_event_loop = self.new_event_loop

        self.get_event_loop = self._policy.get_event_loop
        self.set_event_loop = self._policy.set_event_loop

        BaseGlibEventLoop.init_class()

        if threads:
            tulip_log.info("GLib threads enabled")
            GObject.threads_init()
        else:
            tulip_log.info("GLib threads not used")

            def __new__(cls, *k, **kw):
                raise RuntimeError("GLib threads not enabled (you should use %s(threads=True)" % self.__class__.__name__)

            threading.Thread.__new__ = __new__

    def new_event_loop(self):
        if self._default and isinstance(threading.current_thread(), threading._MainThread):
            l = self.get_default_loop()
        elif self._full:
            l = GLibEventLoop()
        else:
            l = events.DefaultEventLoopPolicy.new_event_loop(self._policy)

        print("new event loop:", l)

        return l

    def get_default_loop(self):

        if not self._default_loop:
            if not self._default:
                raise RuntimeError("%s configured not to used a default loop" % self.__class__.__name__)

            self._default_loop = self._new_default_loop()

        return self._default_loop

    def _new_default_loop(self):
        return GLibEventLoop(GLib.main_context_default())

class GtkEventLoopPolicy(GLibEventLoopPolicy):
    def __init__(self, *, full=False, threads=True):
        super().__init__ (default=True, full=full, threads=threads)

    def _new_default_loop(self):
        return GtkEventLoop()

class wait_signal (futures.Future):
    def __init__(self, obj, name, *, loop=None):
        try:
            super().__init__(loop=loop)
            self._obj = obj
            #FIXME: use  a weakref ?
            self._hnd = obj.connect(name, self._signal_callback)
        except:
            import traceback
            traceback.print_exc()
    
    def _signal_callback(self, *k):
        self._obj.disconnect(self._hnd)
        self.set_result(k)

    def cancel(self):
        super().cancel()
        if self._obj:
            self._obj.disconnect(self._hnd)
            self._obj = None

def get_default_loop(self):
    return tulip.get_event_loop_policy().get_default_loop()
