"""Compare controller framing at equal pulses; the two servos have different ranges."""
import shutil
import subprocess
import pytest
from shore_tracker.servo.pulse_controller import encode_position
from tools.check_shore_spotter import upstream_path


def test_m0_frame_matches_current_upstream_at_equal_pulses(tmp_path):
    try: root = upstream_path()
    except ValueError: pytest.skip('sibling source unavailable in deployment checkout')
    compiler = shutil.which('g++')
    if not compiler: pytest.skip('C++ compiler unavailable')
    source = tmp_path / 'controller.cpp'
    source.write_text(r"""
#include <cassert>
#include <iostream>
#include "servo_controller.h"
struct Serial {
  int availableForWrite() { return 64; }
  size_t write(const uint8_t* data, size_t length) {
    std::cout.write(reinterpret_cast<const char*>(data), length);
    std::cout << std::endl; return length;
  }
};
int main() {
  static_assert(servo_controller::kBaud == 9600);
  Serial serial;
  servo_controller::Output output;
  uint32_t now = 100;
  for (double angle : {0.0, 89.75, 179.5, 269.25, 359.0}) {
    assert(output.send(serial, angle, now) == servo_controller::Result::Sent);
    now += 50;
  }
  assert(output.send(serial, 360, now) == servo_controller::Result::Fault);
}
""")
    executable = tmp_path / 'controller'
    subprocess.run([compiler, '-std=c++17', '-I', str(root / 'include'),
                    str(source), '-o', str(executable)], check=True,
                   capture_output=True, timeout=30)
    result = subprocess.run([str(executable)], capture_output=True, check=True, timeout=5)
    assert result.stdout.splitlines() == [encode_position(a) for a in [0,45,90,135,180]]
