import http.client
import json
import threading
import pytest

from shore_tracker.config import AppConfig,WebConfig,validate_config,ConfigError
from shore_tracker.controller import AppController
from shore_tracker.preview import PreviewStore
from shore_tracker.status import StatusStore
from shore_tracker.web.server import make_server,_validated_loopback_address


def test_lan_requires_explicit_opt_in():
    with pytest.raises(ConfigError):validate_config(AppConfig(web=WebConfig(host='0.0.0.0')))
    validate_config(AppConfig(web=WebConfig(host='0.0.0.0',allow_lan=True)))
    assert _validated_loopback_address(('0.0.0.0',8090),allow_lan=True)[0]==('0.0.0.0',8090)
    with pytest.raises(ValueError):_validated_loopback_address(('8.8.8.8',8090),allow_lan=True)


@pytest.mark.parametrize('include_port', [False, True])
def test_lan_host_and_origin_checks(include_port):
    state=StatusStore();server=make_server(('0.0.0.0',0),state,AppController(state),PreviewStore(),allow_lan=True)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    def request(path,host,origin=None,post=False):
        c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
        headers={'Host':host,'Content-Type':'application/json'}
        if origin:headers['Origin']=origin
        c.request('POST' if post else 'GET',path,'{}' if post else None,headers)
        r=c.getresponse();result=r.status;r.read();c.close();return result
    try:
        host=f'127.0.0.1:{server.server_port}' if include_port else '127.0.0.1'
        assert request('/healthz',host)==200
        assert request('/streaming.js',host)==200
        assert request('/healthz','evil.example')==421
        assert request('/api/stop',host,'http://evil.example',True)==403
        assert request('/api/stop',host,'http://'+host,True)==200
    finally:server.shutdown();server.server_close()


def test_webrtc_offer_body_is_forwarded_once_and_bounded(monkeypatch):
    import io
    from shore_tracker.web import server as web
    offers=[]
    def relay(request,timeout):
        offers.append(request.data)
        return io.BytesIO(b'v=0\r\ns=answer\r\n')
    monkeypatch.setattr(web,'urlopen',relay)
    state=StatusStore();server=make_server(('127.0.0.1',0),state,AppController(state),PreviewStore())
    server.webrtc_preview=object()
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
        offer='v=0\r\ns='+('x'*5000)+'\r\n'
        c.request('POST','/api/stream/webrtc',offer,{'Content-Type':'application/sdp'})
        r=c.getresponse();assert r.status==200;assert r.read()==b'v=0\r\ns=answer\r\n'
        assert offers==[offer.encode()];c.close()
        c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
        c.request('POST','/api/stop','x'*5000,{'Content-Type':'application/json'})
        r=c.getresponse();assert r.status==413;r.read();c.close()
    finally:server.shutdown();server.server_close()



@pytest.mark.parametrize(
    ("peer", "allow_lan", "host", "allowed"),
    [
        ("100.64.0.11", True, "100.64.0.10", True),
        ("100.64.0.12", True, "100.64.0.10:80", True),
        ("100.64.0.11", False, "127.0.0.1", False),
        ("100.64.0.12", True, "evil.example", False),
        ("100.63.255.255", True, "100.64.0.10", False),
        ("100.128.0.1", True, "100.64.0.10", False),
        ("8.8.8.8", True, "127.0.0.1", False),
    ],
)
def test_tailnet_peer_requires_lan_opt_in_and_matching_host(peer, allow_lan, host, allowed):
    from types import SimpleNamespace
    from shore_tracker.web.server import DashboardHandler

    handler = object.__new__(DashboardHandler)
    handler.client_address = (peer, 12345)
    handler.server = SimpleNamespace(allow_lan=allow_lan)
    handler.headers = {"Host": host}
    handler.connection = SimpleNamespace(getsockname=lambda: ("100.64.0.10", 80))
    responses = []
    handler._json = lambda status, body: responses.append(status)
    assert handler._reject_nonlocal_host() is not allowed
    assert responses == ([] if allowed else [421])
