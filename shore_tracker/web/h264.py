"""On-demand, video-only hardware H.264 preview for the local WebRTC relay."""
from __future__ import annotations

import queue
import threading


class H264Preview:
    def __init__(self):
        self._lock = threading.RLock()
        self._clients = set()
        self._pipeline = None
        self._source = None

    @property
    def active(self):
        with self._lock:
            return self._pipeline is not None

    def _start(self):
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)
        self._gst = Gst
        self._pipeline = Gst.parse_launch(
            'appsrc name=source is-live=true format=time block=false max-bytes=4147200 '
            'caps=video/x-raw,format=BGRx,width=960,height=540,framerate=10/1 '
            '! queue leaky=downstream max-size-buffers=1 max-size-bytes=0 max-size-time=0 '
            '! nvvidconv ! video/x-raw(memory:NVMM),format=I420 '
            '! nvv4l2h264enc bitrate=1500000 iframeinterval=10 insert-sps-pps=true maxperf-enable=true '
            '! h264parse ! mpegtsmux alignment=7 '
            '! appsink name=output emit-signals=true sync=false max-buffers=2 drop=true')
        self._source = self._pipeline.get_by_name('source')
        self._pipeline.get_by_name('output').connect('new-sample', self._sample)
        self._pipeline.set_state(Gst.State.PLAYING)

    def _sample(self, sink):
        sample = sink.emit('pull-sample')
        if sample is None:
            return self._gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        data = buffer.extract_dup(0, buffer.get_size())
        # Never block the encoder on a slow relay or browser.
        with self._lock:
            for client in tuple(self._clients):
                try:
                    client.put_nowait(data)
                except queue.Full:
                    while not client.empty():
                        try: client.get_nowait()
                        except queue.Empty: break
                    client.put_nowait(None)
                    self._clients.discard(client)
        return self._gst.FlowReturn.OK

    def publish(self, data, width, height, stride, **_metadata):
        with self._lock:
            source, pipeline = self._source, self._pipeline
            if source is None or (width, height, stride) != (960, 540, 3840):
                return
            if source.get_property('current-level-bytes') >= 4147200:
                return
            clock = pipeline.get_clock()
            if clock is None:
                return
            pts = clock.get_time() - pipeline.get_base_time()
            buffer = self._gst.Buffer.new_wrapped(data)
            buffer.pts = pts
            buffer.duration = 100_000_000
            source.emit('push-buffer', buffer)

    def stream(self, handler):
        client = queue.Queue(128)
        with self._lock:
            if self._pipeline is None:
                try:
                    self._start()
                except Exception:
                    self._pipeline = self._source = None
                    handler.send_error(503, 'Hardware preview unavailable')
                    return
            self._clients.add(client)
        handler.connection.settimeout(5)
        handler.send_response(200)
        handler.send_header('Content-Type', 'video/mp2t')
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        try:
            while True:
                chunk = client.get(timeout=5)
                if chunk is None:
                    return
                handler.wfile.write(chunk)
                handler.wfile.flush()
        except (OSError, queue.Empty):
            pass
        finally:
            with self._lock:
                self._clients.discard(client)
                if not self._clients:
                    pipeline, self._pipeline, self._source = self._pipeline, None, None
                else:
                    pipeline = None
            if pipeline is not None:
                pipeline.set_state(self._gst.State.NULL)

    def close(self):
        with self._lock:
            pipeline, self._pipeline, self._source = self._pipeline, None, None
            for client in self._clients:
                try: client.put_nowait(None)
                except queue.Full: pass
            self._clients.clear()
        if pipeline is not None:
            pipeline.set_state(self._gst.State.NULL)
