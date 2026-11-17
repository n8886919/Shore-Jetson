from tools.servo_motion_test import run


def test_two_channels_return_to_center_and_never_exceed_five_degrees_per_second():
    now=[0.0]
    writes=[]
    class Port:
        in_waiting=0
        def read(self, count): return b''
        def write(self, data): writes.append((now[0],data));return len(data)
    def sleep(seconds): now[0] += max(seconds, .000000001)
    result=run(Port(),clock=lambda:now[0],sleep=sleep)
    assert result['rx_bytes'] == 0
    assert result['commands'] == 114
    for channel in (0,1):
        samples=[(t,int(data[4:8])) for t,data in writes if int(data[1:3]) == channel]
        assert samples[0][1] == samples[-1][1] == 1500
        assert max(p for _,p in samples) == 1556
        for (t1,p1),(t2,p2) in zip(samples,samples[1:]):
            assert abs(p2-p1)*.09/(t2-t1) <= 5
