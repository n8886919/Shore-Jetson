"""Historical GXServo/M0 test, not for RS485 雲台.

Requires a physically confirmed 90-degree start on the old PWM assembly only.
OUT1 then OUT2, 1500 -> 1556 -> 1500 us. At most 2 us every 50 ms,
equivalent to 3.6 deg/s for the historical 500..2500 us / 180-degree scale.
Successful writes are not board acknowledgement or measured motion.
"""
from __future__ import annotations
import argparse
import json
import time
from shore_tracker.servo.port_guard import require_control_port


def test_steps(channels=(0, 1)):
    if not channels or any(type(c) is not int or c not in (0, 1) for c in channels):
        raise ValueError('channels must select OUT1/OUT2')
    for channel in channels:
        for pulse in (1500, *range(1502, 1557, 2), *range(1554, 1499, -2)):
            yield channel, pulse


def run(port, *, channels=(0, 1), clock=time.monotonic, sleep=time.sleep):
    rx = bytearray()
    rx_count = tx_count = 0
    last_write = None
    previous_channel = None
    log = []
    def receive():
        nonlocal rx_count
        data = port.read(min(4096, port.in_waiting))
        rx_count += len(data)
        rx.extend(data[:max(0, 64-len(rx))])
    for channel, pulse in test_steps(channels):
        if previous_channel is not None and channel != previous_channel:
            sleep(1)
        if last_write is not None:
            # No catch-up writes after scheduler or USB delays.
            while clock() - last_write < .05:
                sleep(.05 - (clock() - last_write))
        command = f'#{channel:02d}P{pulse:04d}!'.encode('ascii')
        if port.write(command) != len(command):
            raise OSError('short serial write; motion test stopped')
        last_write = clock()
        log.append((last_write, channel, pulse))
        tx_count += len(command)
        previous_channel = channel
        receive()
    deadline = clock() + 3
    while clock() < deadline:
        receive()
        sleep(.05)
    return {'scope': 'bounded_command_test_no_position_feedback',
            'channels': list(channels), 'start_end_pulse_us': 1500,
            'maximum_pulse_us': 1556, 'maximum_excursion_deg': 5.04,
            'maximum_command_speed_deg_s': 3.6,
            'commands': len(log), 'tx_bytes': tx_count, 'rx_bytes': rx_count,
            'first_64_rx_bytes_hex': rx.hex(), 'proves_actual_motion': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', required=True)
    parser.add_argument('--confirmed-center-90', required=True, action='store_true')
    parser.add_argument('--channel', type=int, choices=(0, 1), help='test only this channel')
    args = parser.parse_args()
    require_control_port(args.device)
    import serial
    with serial.Serial(args.device,baudrate=9600,bytesize=8,parity='N',stopbits=1,
                       timeout=0,write_timeout=.1,exclusive=True) as port:
        print(json.dumps(run(port, channels=(args.channel,) if args.channel is not None else (0, 1))))


if __name__ == '__main__':
    main()
