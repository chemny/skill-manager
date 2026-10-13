"""Bounded HTTPS reads with retries only for transient transport/server failures."""
import http.client
import socket
import ssl
import time
import urllib.error
import urllib.request


def transient(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (500, 502, 503, 504)
    reason = getattr(error, 'reason', error)
    if isinstance(reason, ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in str(reason):
        return False
    if isinstance(reason, socket.gaierror):
        return reason.errno == socket.EAI_AGAIN
    return isinstance(reason, (TimeoutError, ConnectionError,
                               http.client.IncompleteRead, http.client.RemoteDisconnected))


def read(request, *, timeout=20, attempts=3, limit=4_000_000, opener=None, destination=None):
    deadline = time.monotonic() + timeout
    open_url = opener or urllib.request.urlopen
    for attempt in range(attempts):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError('Remote request deadline exceeded')
            # Reserve time for recovery if a connect or body read consumes its timeout.
            attempt_deadline = time.monotonic() + (remaining / 2 if attempt + 1 < attempts else remaining)
            with open_url(request, timeout=max(.001, attempt_deadline - time.monotonic())) as response:
                headers = getattr(response, 'headers', {})
                expected = headers.get('Content-Length')
                expected = int(expected) if expected is not None else None
                if expected is not None and expected > limit:
                    raise ValueError('Remote response exceeds size limit')
                parts, received = [], 0
                output = destination.open('wb') if destination is not None else None
                try:
                    while True:
                        remaining = attempt_deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Remote request deadline exceeded')
                        socket_handle = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                        if socket_handle is not None: socket_handle.settimeout(remaining)
                        read_chunk = getattr(response, 'read1', response.read)
                        block = read_chunk(min(128 * 1024, limit + 1 - received))
                        if not block: break
                        received += len(block)
                        if received > limit: raise ValueError('Remote response exceeds size limit')
                        if output: output.write(block)
                        else: parts.append(block)
                    if expected is not None and received != expected:
                        raise http.client.IncompleteRead(b'', max(0, expected - received))
                finally:
                    if output: output.close()
                return received if destination is not None else b''.join(parts)
        except Exception as error:
            if destination is not None: destination.unlink(missing_ok=True)
            delay = .2 * 2 ** attempt
            if attempt + 1 >= attempts or not transient(error) or time.monotonic() + delay >= deadline:
                raise
            time.sleep(delay)
