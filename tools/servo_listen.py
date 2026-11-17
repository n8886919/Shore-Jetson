"""Passively receive UART bytes. Stop the application owning the port first."""
from __future__ import annotations
import argparse
import json
import time
from shore_tracker.servo.port_guard import require_control_port


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', required=True)
    parser.add_argument('--seconds', type=float, default=20)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 60:
        parser.error('seconds must be in [1, 60]')
    require_control_port(args.device)
    import serial
    count = 0
    sample = bytearray()
    started = time.monotonic()
    with serial.Serial(args.device, baudrate=9600, bytesize=8, parity='N',
                       stopbits=1, timeout=.1, write_timeout=.1,
                       exclusive=True) as port:
        while time.monotonic() - started < args.seconds:
            data = port.read(min(4096, max(1, port.in_waiting)))
            count += len(data)
            sample.extend(data[:max(0, 64-len(sample))])
    print(json.dumps({'scope':'passive_uart_only', 'baud':9600,
                      'seconds':round(time.monotonic()-started, 3),
                      'tx_bytes':0, 'rx_bytes':count, 'first_64_bytes_hex':sample.hex(),
                      'proves_ack_or_position':False}))


if __name__ == '__main__':
    main()
