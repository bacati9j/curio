# channel.py
#
# Support for a message passing channel that can send bytes or pickled
# Python objects on a stream.  Compatible with the Connection class in the
# multiprocessing module, but rewritten for a purely asynchronous runtime.
#
# WARNING: This module is experimental, undocumented, and may be removed
# or rewritten at any time.

import os
import pickle
import struct
import io
import hmac
import multiprocessing.connection as mpc
from contextlib import contextmanager

from . import socket
from .kernel import CurioError
from .io import Stream

__all__ = ['Channel', 'Listener', 'Client']

# Authentication parameters (from multiprocessing)

import multiprocessing.connection as mpc

AUTH_MESSAGE_LENGTH = mpc.MESSAGE_LENGTH    # 20
CHALLENGE = mpc.CHALLENGE                   # b'#CHALLENGE#'
WELCOME = mpc.WELCOME                       # b'#WELCOME#'
FAILURE = mpc.FAILURE                       # b'#FAILURE#'

class ChannelError(CurioError):
    pass

class AuthenticationError(ChannelError):
    pass

class ChannelRPCError(ChannelError):
    pass

class Channel(object):
    '''
    A communication channel for sending size-prefixed messages of bytes
    or pickled Python objects.  Must be passed a pair of reader/writer
    streams for performing the underlying communication.
    '''
    def __init__(self, reader, writer):
        assert isinstance(reader, Stream) and isinstance(writer, Stream)
        self._reader = reader
        self._writer = writer

    @classmethod
    def from_Connection(cls, conn):
        '''
        Creates a channel from a multiprocessing Connection. Note: The
        multiprocessing connection is detached by having its handle set to None.
        
        This method can be used to make curio talk over Pipes as created by
        multiprocessing.  For example:

              p1, p2 = multiprocessing.Pipe()
              p1 = Channel.from_Connection(p1)
              p2 = Channel.from_Connection(p2)

        '''
        assert isinstance(conn, mpc._ConnectionBase)
        reader = Stream(open(conn._handle, 'rb', buffering=0))
        writer = Stream(open(conn._handle, 'wb', buffering=0, closefd=False))
        conn._handle = None
        return cls(reader, writer)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    @contextmanager
    def timeout(self, seconds):
        read_to = self._reader.settimeout(seconds)
        write_to = self._writer.settimeout(seconds)
        try:
            yield
        finally:
            self._reader.settimeout(read_to)
            self._writer.settimeout(write_to)
    
    async def close(self):
        await self._reader.close()
        await self._writer.close()

    async def send_bytes(self, buf, offset=0, size=None):
        '''
        Send a buffer of bytes as a single message
        '''
        m = memoryview(buf)
        if m.itemsize > 1:
            m = memoryview(bytes(m))
        n = len(m)
        if offset < 0:
            raise ValueError("offset is negative")
        if n < offset:
            raise ValueError("buffer length < offset")
        if size is None:
            size = n - offset
        elif size < 0:
            raise ValueError("size is negative")
        elif offset + size > n:
            raise ValueError("buffer length < offset + size")

        header = struct.pack('!i', size)
        if size >= 16384:
            await self._writer.write(header)
            await self._writer.write(m[offset:offset+size])
        else:
            msg = header + bytes(m[offset:offset+size])
            await self._writer.write(msg)

    async def recv_bytes(self, maxlength=None):
        '''
        Receive a message of bytes as a single message.
        '''
        header = await self._reader.read_exactly(4)
        size,  = struct.unpack('!i', header)
        if maxlength is not None:
            if size > maxlength:
                raise IOError('Message too large. %d bytes > %d maxlength' % (size, maxlength))

        msg = await self._reader.read_exactly(size)
        return msg

    async def recv_bytes_into(self, buf, offset=0):
        '''
        Receive bytes into a writable memory buffer
        '''
        pass

    async def send(self, obj):
        '''
        Send an arbitrary Python object. Uses pickle to serialize.
        '''
        await self.send_bytes(pickle.dumps(obj))

    async def recv(self):
        '''
        Receive a Python object. Uses pickle to unserialize.
        '''
        msg = await self.recv_bytes()
        return pickle.loads(msg)

    async def _deliver_challenge(self, authkey):
        message = os.urandom(AUTH_MESSAGE_LENGTH)
        await self.send_bytes(CHALLENGE + message)
        digest = hmac.new(authkey, message, 'md5').digest()
        response = await self.recv_bytes(maxlength=256)
        if response == digest:
            await self.send_bytes(WELCOME)
        else:
            await self.send_bytes(FAILURE)
            raise AuthenticationError('digest received was wrong')

    async def _answer_challenge(self, authkey):
        message = await self.recv_bytes(maxlength=256)
        assert message[:len(CHALLENGE)] == CHALLENGE, 'message = %r' % message
        message = message[len(CHALLENGE):]
        digest = hmac.new(authkey, message, 'md5').digest()
        await self.send_bytes(digest)
        response = await self.recv_bytes(maxlength=256)

        if response != WELCOME:
            raise AuthenticationError('digest sent was rejected')

    async def authenticate_server(self, authkey):
        await self._deliver_challenge(authkey)
        await self._answer_challenge(authkey)

    async def authenticate_client(self, authkey):
        await self._answer_challenge(authkey)
        await self._deliver_challenge(authkey)

    async def rpc_client(self, func, args, kwargs):
        '''
        Perform the client-side of a remote procedure call over a channel
        '''
        msg = (func, args, kwargs)
        try:
            await self.send(msg)
        except Exception as e:
            raise ChannelRPCError('RPC send failure') from e
        try:
            success, result = await self.recv()
        except TimeoutError as e:
            raise
        except Exception as e:
            # Note: A custom exception is raised on recv() failure to
            # indicate a problem with the protocol itself. This is
            # to disambiguate from server-side exceptions that are
            # raised by the 'raise result' statement below.
            raise ChannelRPCError('RPC receive failure') from e
        if success:
            return result
        else:
            raise result

    async def rpc_serve_one(self):
        '''
        Perform the server-side handling of a remote procedure call
        '''
        try:
            func, args, kwargs = await self.recv()
        except TimeoutError as e:
            raise
        except Exception as e:
            raise ChannelRPCError('RPC receive failure') from e
        try:
            result = (True, func(*args, **kwargs))
        except Exception as e:
            result = (False, e)
        try:
            await self.send(result)
        except TimeoutError as e:
            raise
        except Exception as e:
            raise ChannelRPCError('RPC send error') from e

class Listener(object):
    def __init__(self, address, family=socket.AF_INET, backlog=1, authkey=None):
        self._sock = socket.socket(family, socket.SOCK_STREAM)
        if family == socket.AF_INET:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
        self._sock.bind(address)
        self._sock.listen(backlog)
        self._authkey = authkey

    async def accept(self):
        client, addr = await self._sock.accept()
        fileno = client.detach()
        ch = Channel(Stream(open(fileno, 'rb', buffering=0)),
                     Stream(open(fileno, 'wb', buffering=0, closefd=False)))
        if self._authkey:
            await ch.authenticate_server(self._authkey)
        return ch

    async def close(self):
        await self._sock.close()

async def Client(address, family=socket.AF_INET, authkey=None):
    sock = socket.socket(family, socket.SOCK_STREAM)
    await sock.connect(address)
    fileno = sock.detach()
    ch = Channel(Stream(open(fileno, 'rb', buffering=0)),
                 Stream(open(fileno, 'wb', buffering=0, closefd=False)))
    if authkey:
        await ch.authenticate_client(authkey)
    return ch
