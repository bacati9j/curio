# ssl_echo
#
# An example of a simple SSL echo client

import curio
from curio import ssl
from curio import network
import time
from curio import socket

async def main(host, port):
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    sock = await network.open_tcp_connection(host, port, ssl=True, server_hostname=None, family=socket.AF_INET)
    for i in range(1000):
        msg = ('Message %d' % i).encode('ascii')
        print(msg)
        await sock.sendall(msg)
        resp = await sock.recv(1000)
        assert msg == resp
    await sock.close()

if __name__ == '__main__':
    kernel = curio.Kernel()
    kernel.run(main('localhost', 10000))
