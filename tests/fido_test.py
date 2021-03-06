# -*- coding: utf-8 -*-
import logging
import mock
import threading
import time

import crochet
import pytest
import twisted.internet.error
import twisted.internet.defer
from six.moves import BaseHTTPServer
from six.moves import socketserver as SocketServer
import twisted.web.client
from twisted.web.client import Agent
from twisted.web.client import FileBodyProducer
from twisted.web.client import ProxyAgent
from yelp_bytes import to_bytes

import fido
from fido.fido import _build_body_producer
from fido.fido import _set_deferred_timeout
from fido.fido import DEFAULT_USER_AGENT

SERVER_OVERHEAD_TIME = 2.0
TIMEOUT_TEST = 1.0

ERROR_MESSAGE = 'I failed :('
ECHO_URL = '/echo'


@pytest.yield_fixture(scope="module")
def server_url():
    """Spin up a localhost web server for testing."""

    # Surpress 'No handlers could be found for logger "twisted"' messages
    logging.basicConfig()
    logging.getLogger('twisted').setLevel(logging.CRITICAL)

    class TestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
        def echo(self):
            if 'slow' in self.path:
                time.sleep(SERVER_OVERHEAD_TIME)

            self.send_response(200)

            for k, v in self.headers.items():
                self.send_header(k, v)
            self.end_headers()

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                self.wfile.write(self.rfile.read(content_length))

        def content_length(self):
            """Send back the content-length number as the response."""
            self.send_response(200)

            response = to_bytes(self.headers.get('Content-Length'))
            content_length = len(response)
            self.send_header('Content-Length', content_length)
            self.end_headers()

            self.wfile.write(response)

        def do_GET(self):
            if ECHO_URL in self.path:
                self.echo()

        def do_POST(self):
            if 'content_length' in self.path:
                self.content_length()
            elif ECHO_URL in self.path:
                self.echo()

    class MultiThreadedHTTPServer(SocketServer.ThreadingMixIn,
                                  BaseHTTPServer.HTTPServer):
        request_queue_size = 1000

    httpd = MultiThreadedHTTPServer(('localhost', 0), TestHandler)
    httpd_thread = threading.Thread(target=httpd.serve_forever)
    httpd_thread.start()
    yield 'http://%s:%d/' % (httpd.server_address[0], httpd.server_address[1])
    httpd.server_close()
    httpd_thread.join()


def test_fetch_basic(server_url):
    response = fido.fetch(server_url + ECHO_URL).wait()
    assert response.headers.get(b'User-Agent') == \
        [to_bytes(DEFAULT_USER_AGENT)]
    assert response.reason == b'OK'
    assert response.code == 200


def test_set_deferred_timeout_none():
    mock_reactor = mock.Mock()
    mock_deferred = mock.Mock()
    _set_deferred_timeout(mock_reactor, mock_deferred, None)

    assert mock_reactor.callLater.called is False
    assert mock_deferred.addBoth.called is False


def test_set_deferred_timeout_finite_value():
    mock_reactor = mock.Mock()
    mock_deferred = mock.Mock()

    _set_deferred_timeout(mock_reactor, mock_deferred, TIMEOUT_TEST)

    mock_reactor.callLater.assert_called_once_with(
        TIMEOUT_TEST, mock_deferred.cancel
    )

    assert mock_deferred.addBoth.called is True


def test_eventual_result_timeout(server_url):
    """
    Testing timeout on result retrieval
    """

    # fetch without setting timeouts -> we could potentially wait forever
    eventual_result = fido.fetch(server_url + ECHO_URL + '/slow')

    # make sure no timeout error is thrown here but only on result retrieval
    assert eventual_result.original_failure() is None

    with pytest.raises(crochet.TimeoutError):
        eventual_result.wait(timeout=TIMEOUT_TEST)

    assert eventual_result.original_failure() is None


def test_agent_timeout(server_url):
    """
    Testing that we don't wait forever on the server sending back a response
    """

    eventual_result = fido.fetch(
        server_url + ECHO_URL + '/slow',
        timeout=TIMEOUT_TEST
    )

    # wait for fido to estinguish the timeout and abort before test-assertions
    time.sleep(2 * TIMEOUT_TEST)

    # timeout errors were thrown and handled in the reactor thread.
    # EventualResult stores them and re-raises on result retrieval
    assert eventual_result.original_failure() is not None

    with pytest.raises(crochet.TimeoutError) as e:
        eventual_result.wait()

    assert (
        "Connection was closed by fido because the server took "
        "more than timeout={timeout} seconds to "
        "send the response".format(timeout=TIMEOUT_TEST)
        in str(e)
    )


def test_agent_connect_timeout():
    """
    Testing that we don't wait more than connect_timeout to establish a http
    connection
    """

    # google drops TCP SYN packets
    eventual_result = fido.fetch(
        "http://www.google.com:81",
        connect_timeout=TIMEOUT_TEST
    )
    # wait enough for the connection to be dropped by Twisted Agent
    time.sleep(2 * TIMEOUT_TEST)

    # timeout errors were thrown and handled in the reactor thread.
    # EventualResult stores them and re-raises on result retrieval
    assert eventual_result.original_failure() is not None

    with pytest.raises(crochet.TimeoutError) as e:
        eventual_result.wait()

    assert (
        "Connection was closed by Twisted Agent because the HTTP "
        "connection took more than connect_timeout={connect_timeout} seconds "
        "to establish.".format(connect_timeout=TIMEOUT_TEST)
        in str(e)
    )


def test_fetch_stress(server_url):
    eventual_results = [
        fido.fetch(server_url + ECHO_URL, timeout=10) for _ in range(1000)
    ]
    for eventual_result in eventual_results:
        eventual_result.wait(timeout=10)


def test_fetch_headers(server_url):
    headers = {'foo': ['bar']}
    eventual_result = fido.fetch(server_url + ECHO_URL, headers=headers)
    actual_headers = eventual_result.wait().headers
    assert actual_headers.get(b'Foo') == [b'bar']


def test_headers_keep_content_length_if_no_body():
    headers = {'Content-Length': '0'}
    bodyProducer, headers = _build_body_producer(None, headers)
    assert bodyProducer is None
    assert 'Content-Length' in headers


@pytest.mark.parametrize(
    'header', ('content-length', 'Content-Length')
)
def test_headers_remove_content_length_if_body(header):
    headers = {header: '22'}
    body = b'{"some_json_data": 30}'

    bodyProducer, headers = _build_body_producer(body, headers)
    assert isinstance(bodyProducer, FileBodyProducer)
    assert headers == {}


def test_json_body(server_url):
    body = b'{"some_json_data": 30}'
    eventual_result = fido.fetch(
        server_url + ECHO_URL,
        method='POST',
        body=body
    )
    assert eventual_result.wait().json()['some_json_data'] == 30


def test_content_length_readded_by_twisted(server_url):
    headers = {'Content-Length': '250'}
    body = b'{"some_json_data": 30}'
    eventual_result = fido.fetch(
        server_url + '/content_length',
        method='POST',
        headers=headers,
        body=body
    )
    content_length = int(eventual_result.wait().body)
    assert content_length == 22


def test_headers_not_modified_in_place():
    headers = {'foo': 'bar', 'Content-Length': '22'}
    body = b'{"some_json_data": 30}'
    _, _ = _build_body_producer(body, headers)

    assert headers == {'foo': 'bar', 'Content-Length': '22'}


def test_fetch_content_type(server_url):
    expected_content_type = b'text/html'
    eventual_result = fido.fetch(
        server_url + ECHO_URL,
        headers={'Content-Type': expected_content_type}
    )
    actual_content_type = eventual_result.wait().headers.\
        get(b'Content-Type')
    assert [expected_content_type] == actual_content_type


@pytest.mark.parametrize(
    'header_name', ('User-Agent', 'user-agent')
)
def test_fetch_user_agent(server_url, header_name):
    expected_user_agent = [b'skynet']
    headers = {header_name: expected_user_agent}
    eventual_result = fido.fetch(
        server_url + ECHO_URL,
        headers=headers,
    )
    actual_user_agent = eventual_result.wait().headers.get(b'User-Agent')
    assert expected_user_agent == actual_user_agent


def test_fetch_body(server_url):
    expected_body = b'corpus'
    eventual_result = fido.fetch(
        server_url + ECHO_URL,
        body=expected_body
    )
    actual_body = eventual_result.wait().body
    assert expected_body == actual_body


def test_get_agent_no_http_proxy():
    with mock.patch.dict('os.environ', clear=True):
        agent = fido.fido.get_agent(mock.Mock(spec=Agent),
                                    connect_timeout=None)
    assert isinstance(agent, Agent)


def test_get_agent_with_http_proxy():
    with mock.patch.dict('os.environ',
                         {'http_proxy': 'http://localhost:8000'}):
        agent = fido.fido.get_agent(mock.Mock(spec=Agent),
                                    connect_timeout=None)
    assert isinstance(agent, ProxyAgent)


def test_deferred_errback_chain():
    """
    Test exception thrown on the deferred correctly triggers the errback chain
    and it is thrown by EventualResult on result retrieval.
    """
    d = twisted.internet.defer.Deferred()
    mock_agent = mock.Mock()
    mock_agent.request.return_value = d

    # careful while patching get_agent cause it's being accessed
    # by the reactor thread
    with mock.patch('fido.fido.get_agent', return_value=mock_agent):
        eventual_result = fido.fido.fetch('http://some_url')

        # trigger an exception on the deferred
        d.errback(twisted.web.client.ResponseFailed(ERROR_MESSAGE))

        with pytest.raises(twisted.web.client.ResponseFailed) as e:
            eventual_result.wait(timeout=TIMEOUT_TEST)

        assert e.value.args == (ERROR_MESSAGE,)


def test_fetch_inner_exception_thrown_in_reactor_thread():
    """
    Test that an exception thrown in the reactor thread is trapped inside
    the EventualResult and thrown on result retrieval.
    """

    # careful while patching _build_body_producer cause it's being accessed
    # by the reactor thread
    with mock.patch('fido.fido.get_agent'):
        with mock.patch(
            'fido.fido._build_body_producer',
            side_effect=ValueError(ERROR_MESSAGE)
        ):

            # this should NOT raise! Exception is thrown in the reactor thread
            # and it is catched and saved inside EventualResult
            eventual_result = fido.fido.fetch('http://some_url')

            # exception should be thrown here, when waiting for the result
            with pytest.raises(ValueError) as e:
                eventual_result.wait(timeout=TIMEOUT_TEST)

            assert e.value.args == (ERROR_MESSAGE,)


def test_fetch_normally_throws_exception():
    """
    Testing an exception in the main thread of execution is normally thrown.
    """

    with mock.patch(
        'fido.fido.crochet.setup',
        side_effect=ValueError(ERROR_MESSAGE)
    ):
        with pytest.raises(ValueError) as e:
            fido.fido.fetch('http://some_url')

    assert e.value.args == (ERROR_MESSAGE,)
