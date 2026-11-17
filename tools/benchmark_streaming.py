#!/usr/bin/env python3
"""Bounded, capture-free JPEG/H.264 encoder comparison on Jetson.

Reads the native RAM frame, or uses --synthetic for a capability test. It never
arms tracking, moves hardware, changes camera settings, or saves video.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import resource
import struct
import time
import urllib.request


def compare(seconds,base,synthetic=False):
    import gi
    gi.require_version('Gst','1.0')
    from gi.repository import Gst
    Gst.init(None)
    frame_file=None
    if not synthetic:
        with urllib.request.urlopen(base+'/api/local-display',timeout=3) as r: info=json.load(r)
        path=info.get('path')
        if not path or not path.startswith('/dev/shm/shore-display-'):raise RuntimeError('Native RAM frame unavailable')
        frame_file=open(path,'rb',buffering=0)
    results=[]
    for codec,encoder in [('jpeg','videoconvert ! jpegenc quality=80'),('h264','nvvidconv ! video/x-raw(memory:NVMM),format=I420 ! nvv4l2h264enc bitrate=1500000 iframeinterval=10 insert-sps-pps=true maxperf-enable=true ! h264parse')]:
        producer=('videotestsrc is-live=true pattern=ball ! video/x-raw,width=960,height=540,framerate=10/1' if synthetic else 'appsrc name=source is-live=true format=time block=false caps=video/x-raw,format=BGRx,width=960,height=540,framerate=10/1')
        pipeline=Gst.parse_launch(producer+' ! queue leaky=downstream max-size-buffers=1 ! '+encoder+' ! appsink name=result emit-signals=true sync=false max-buffers=1 drop=true')
        sizes=[];latencies=[]
        def sample(sink):
            item=sink.emit('pull-sample');buf=item.get_buffer();sizes.append(buf.get_size())
            if buf.pts!=Gst.CLOCK_TIME_NONE:
                running=pipeline.get_clock().get_time()-pipeline.get_base_time()
                latencies.append(max(0,(running-buf.pts)/1e6))
            return Gst.FlowReturn.OK
        pipeline.get_by_name('result').connect('new-sample',sample)
        start=time.monotonic();usage=resource.getrusage(resource.RUSAGE_SELF);pipeline.set_state(Gst.State.PLAYING)
        source=pipeline.get_by_name('source');previous=0;error=None
        try:
            while time.monotonic()-start<seconds:
                message=pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
                if message: error=str(message.parse_error()[0]);break
                if frame_file:
                    fcntl.flock(frame_file,fcntl.LOCK_SH)
                    try:
                        frame_file.seek(0);header=frame_file.read(64)
                        magic,w,h,stride,_,seq,stamp=struct.unpack('<8sIIIIQd',header[:40])
                        payload=frame_file.read(stride*h) if seq!=previous and w==960 and h==540 and stride==3840 else None
                    finally: fcntl.flock(frame_file,fcntl.LOCK_UN)
                    if payload:
                        previous=seq
                        buf=Gst.Buffer.new_wrapped(payload)
                        buf.pts=pipeline.get_clock().get_time()-pipeline.get_base_time()
                        buf.duration=100_000_000;source.emit('push-buffer',buf)
                time.sleep(.01)
        finally:pipeline.set_state(Gst.State.NULL)
        elapsed=time.monotonic()-start;after=resource.getrusage(resource.RUSAGE_SELF)
        latencies.sort()
        results.append({'codec':codec,'source':'synthetic_ball' if synthetic else 'live_native_ram','seconds':round(elapsed,2),'frames':len(sizes),'fps':round(len(sizes)/elapsed,2),'mbps':round(sum(sizes)*8/elapsed/1e6,3),'cpu_one_core_percent':round(((after.ru_utime+after.ru_stime)-(usage.ru_utime+usage.ru_stime))/elapsed*100,1),'encode_p95_ms':round(latencies[min(len(latencies)-1,int(len(latencies)*.95))],1) if latencies else None,'error':error or ('no frames' if not sizes else None)})
    if frame_file:frame_file.close()
    return results


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--seconds',type=float,default=15);p.add_argument('--base',default='http://127.0.0.1:8090');p.add_argument('--synthetic',action='store_true');args=p.parse_args()
    if not 3<=args.seconds<=60:p.error('seconds must be between 3 and 60')
    print(json.dumps(compare(args.seconds,args.base,args.synthetic),ensure_ascii=False,indent=2))
