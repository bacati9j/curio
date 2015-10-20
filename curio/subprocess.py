# curio/subprocess.py
#
# Curio clone of the subprocess module.  

from .kernel import future_wait, new_task, sleep
from .file import File
import subprocess

__all__ = [ 'run', 'Popen', 'CompletedProcess', 'CalledProcessError', 'TimeoutExpired', 'SubprocessError',
            'check_output', 'PIPE', 'STDOUT', 'DEVNULL' ]

from subprocess import (
    CompletedProcess,
    SubprocessError, 
    CalledProcessError,
    TimeoutExpired,
    PIPE,
    STDOUT,
    DEVNULL,
    )

class Popen(object):
    '''
    Curio wrapper around the Popen class from the subprocess module. All of the
    methods from subprocess.Popen should be available, but the associated file
    objects for stdin, stdout, stderr have been replaced by async versions.
    Certain blocking operations (e.g., wait() and communicate()) have been
    replaced by async compatible implementations.
    '''
    def __init__(self, args, **kwargs):
        if 'universal_newlines' in kwargs:
            raise RuntimeError('universal_newlines argument not supported')
        if 'bufsize' in kwargs:
            raise RuntimeError('bufsize argument not supported')

        self._popen = subprocess.Popen(args, bufsize=0, **kwargs)
        if self._popen.stdin:
            self.stdin = File(self._popen.stdin)
        if self._popen.stdout:
            self.stdout = File(self._popen.stdout)
        if self._popen.stderr:
            self.stderr = File(self._popen.stderr)

    def __getattr__(self, name):
        return getattr(self._popen, name)

    async def wait(self, timeout=None):
        async def waiter():
            while True:
                retcode = self._popen.poll()
                if retcode is not None:
                    return retcode
                await sleep(0.0005)
        task = await new_task(waiter())
        try:
            return await task.join(timeout=timeout)
        except TimeoutError:
            print('waiter task timed out')
            await task.cancel()
            raise TimeoutExpired(self.args, timeout) from None

    async def communicate(self, input=b'', timeout=None):
        if input:
            assert self.stdin
            stdin_task = await new_task(self.stdin.write(input, close_on_complete=True))
        else:
            stdin_task = None

        stdout_task = await new_task(self.stdout.readall()) if self.stdout else None
        stderr_task = await new_task(self.stderr.readall()) if self.stderr else None

        # Collect the output from the workers
        try:
            if stdin_task:
                await stdin_task.join(timeout=timeout)
            stdout = await stdout_task.join(timeout=timeout) if stdout_task else b''
            stderr = await stderr_task.join(timeout=timeout) if stderr_task else b''
            return (stdout, stderr)
        except TimeoutError:
            await stdout_task.cancel()
            await stderr_task.cancel()
            if stdin_task:
                await stdin_task.cancel()
            raise

    def __enter__(self):
        raise RuntimeError('Use async-with')

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.stdout:
            self.stdout.close()

        if self.stderr:
            self.stderr.close()

        if self.stdin:
            self.stdin.close()

        # Wait for the process to terminate
        await self.wait()

async def run(args, *, stdin=None, input=None, stdout=None, stderr=None, shell=False, timeout=None, check=False):
    '''
    Curio-compatible version of subprocess.run()
    '''
    if input:
        stdin = subprocess.PIPE
    else:
        stdin = None

    async with Popen(args, stdin=stdin, stdout=stdout, stderr=stderr, shell=shell) as process:
        try:
            stdout, stderr = await process.communicate(input, timeout)
        except TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            raise TimeoutExpired(process.args, timeout, output=stdout, stderr=stderr)
        except:
            process.kill()
            raise

    retcode = process.poll()
    if check and retcode:
        raise CalledProcessError(retcode, process.args,
                                 output=stdout, stderr=stderr)
    return CompletedProcess(process.args, retcode, stdout, stderr)

async def check_output(args, *, stdin=None, stderr=None, shell=False, timeout=None):
     out = await run(args, stdout=PIPE, stdin=stdin, stderr=stderr, shell=shell, timeout=timeout, check=True)
     return out.stdout
