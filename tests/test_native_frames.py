import fcntl
import os
import struct
from pathlib import Path

from shore_tracker.native_frames import NativeFrames, HEADER, CAPACITY


def test_native_frame_is_private_bounded_and_removed():
    frames=NativeFrames();frames.start();path=frames.path
    try:
        assert str(path).startswith('/dev/shm/')
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_size == CAPACITY
        assert frames.publish(b'\x01\x02\x03\xff'*6,3,2,12)
        data=path.read_bytes()
        magic,w,h,stride,_,seq,stamp=HEADER.unpack(data[:HEADER.size])
        assert (magic,w,h,stride,seq)==(b'SHORE01\0',3,2,12,1)
        assert stamp>0 and data[64:88]==b'\x01\x02\x03\xff'*6
        assert not frames.publish(b'x',960,540,3840)
        assert not frames.publish(b'x',0,540,0)
        # Another process reading a frame must cause dropping rather than blocking.
        with path.open('rb') as reader:
            fcntl.flock(reader,fcntl.LOCK_SH)
            assert not frames.publish(b'\0'*24,3,2,12)
    finally: frames.close()
    assert not path.exists()
    assert not frames.publish(b'\0'*24,3,2,12)


def test_native_pipeline_shares_one_decoder_with_inference(tmp_path):
    from shore_tracker.camera import build_pipeline_spec
    for decoded in (False,True):
        spec=build_pipeline_spec('h265',tmp_path/'clip-%05d.mkv',native_display=True,decoded_inference=decoded)
        assert sum(e.factory=='nvv4l2decoder' for e in spec.elements)==1
        links=[(link.source,link.target) for link in spec.links]
        assert ('native_rate_caps','native_sink') in links
        assert ('native_tee','native_preview_queue') in links
        assert ('native_tee','native_queue') in links
        assert ('preview_jpeg','preview_sink') in links
        assert ('record_parse','segment_sink') in links
        assert ('display_rate_caps','native_tee') in links if decoded else ('preview_decoder','native_tee') in links
