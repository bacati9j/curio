# echoserv.py
#
# Echo server using the run_server() function

from curio import run, run_server

async def echo_client(client, addr):
    print('Connection from', addr)
    while True:
        data = await client.recv(1000)
        if not data:
            break
        await client.sendall(data)
    print('Connection closed')

if __name__ == '__main__':
    run(run_server('', 25000, echo_client))
