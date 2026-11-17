#!/bin/bash
# Isolated Python 3.11 + TensorRT 8.2.1 bindings for JetPack 4.6.6.
# Requires prepare-nano-system.sh first; never replaces /usr/bin/python3.
set -euo pipefail
[[ $(id -u) != 0 && $(uname -m) == aarch64 ]] || exit 1
base=${1:-"$HOME/.local/share/shore-tracker"}
work=${2:-"$HOME/shore-tracker-setup"}
mkdir -p "$base" "$work"
cd "$work"
fetch() {
  local url=$1 file=$2 digest=$3
  if ! [[ -f $file ]] || ! printf '%s  %s\n' "$digest" "$file" | sha256sum -c - >/dev/null 2>&1; then
    wget -q --timeout=30 -O "$file" "$url"
  fi
  printf '%s  %s\n' "$digest" "$file" | sha256sum -c -
}
fetch 'https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.11.17%2B20261013-aarch64-unknown-linux-gnu-install_only.tar.gz' \
  python.tar.gz 643954c07b04c18e215f812691cd5fa15d924fec1f3a5fdd9c1789484382ac24
if [[ ! -x "$base/python/bin/python3" ]]; then
  tar -xzf python.tar.gz -C "$base"
fi
if [[ ! -x "$base/venv/bin/python" ]]; then
  "$base/python/bin/python3" -m venv "$base/venv"
fi
python="$base/venv/bin/python"
# The portable Python was built with Clang; use the system's native toolchain
# when compiling extensions instead of requiring a second compiler toolchain.
export CC=gcc CXX=g++
"$python" - <<'PY'
import importlib.util, sysconfig
from pathlib import Path
# GCC 7 does not implement the flag used by the portable interpreter's Clang.
# Adjust only this isolated interpreter's extension-build configuration.
p = Path(importlib.util.find_spec(sysconfig._get_sysconfigdata_name()).origin)
p.write_text(p.read_text().replace('-fstack-clash-protection', ''))
PY
"$python" -m pip install --only-binary=:all: \
  numpy==1.26.4 Pillow==10.4.0 pyserial==3.5 pybind11==2.10.4 \
  setuptools==75.8.2 wheel==0.45.1
# This PyGObject version supports Ubuntu 18.04's GLib 2.56 and Python 3.11.
"$python" -m pip install pycairo==1.23.0 PyGObject==3.42.2
fetch 'https://codeload.github.com/NVIDIA/TensorRT/tar.gz/refs/tags/8.2.1' \
  tensorrt-source.tar.gz 2b51b4e31d6d62d059fa63480d12618a53b192af2cbd3da8c05a75ded07cc02c
if [[ ! -d TensorRT-8.2.1 ]]; then
  tar -xzf tensorrt-source.tar.gz TensorRT-8.2.1/python TensorRT-8.2.1/include TensorRT-8.2.1/LICENSE
fi
source_dir="$work/TensorRT-8.2.1"
mkdir -p "$work/parser-headers/onnx"
ln -sf /usr/include/aarch64-linux-gnu/NvOnnxParser.h "$work/parser-headers/onnx/NvOnnxParser.h"
cuda_include=/usr/local/cuda-10.2/include
if [[ ! -f "$cuda_include/crt/host_defines.h" ]]; then
  # cuda-cudart-dev omits CRT headers shipped in cuda-nvcc. Extract the
  # distribution package privately; no system compiler or CUDA replacement.
  mkdir -p "$work/cuda-headers"
  cd "$work/cuda-headers"
  if [[ ! -f usr/local/cuda-10.2/targets/aarch64-linux/include/crt/host_defines.h ]]; then
    apt-get download cuda-nvcc-10-2=10.2.300-1
    dpkg-deb -x cuda-nvcc-10-2_10.2.300-1_arm64.deb .
  fi
  cuda_include="$work/cuda-headers/usr/local/cuda-10.2/targets/aarch64-linux/include;$cuda_include"
fi
mkdir -p "$source_dir/python/build"
cd "$source_dir/python/build"
py_include=$("$python" -c 'import sysconfig; print(sysconfig.get_path("include"))')
pybind_include=$("$python" -c 'import pybind11; print(pybind11.get_include())')
cmake .. -DCMAKE_BUILD_TYPE=Release -DTARGET=aarch64 -DNV_TARGET_OS=linux \
  -DEXT_PATH="$work" \
  -DPYTHON_MAJOR_VERSION=3 -DPYTHON_MINOR_VERSION=11 \
  -DPY_INCLUDE="$py_include" -DPY_CONFIG_INCLUDE="$py_include" \
  -DPYBIND11_DIR="$pybind_include" -DCUDA_INCLUDE_DIRS="$cuda_include" \
  -DTENSORRT_ROOT="$source_dir" -DTENSORRT_BUILD=/usr/lib/aarch64-linux-gnu \
  -DONNX_INC_DIR="$work/parser-headers"
# Limit peak memory on the 4 GB Nano.
make -j2
cp -r "$source_dir/python/packaging/." .
"$python" - <<'PY'
from pathlib import Path
for name in ('setup.py', 'tensorrt/__init__.py'):
    p = Path(name)
    # TensorRT 8.2 predates NumPy's removal of the deprecated np.bool alias.
    # Correct only the binding helper; do not monkey-patch NumPy globally.
    p.write_text(p.read_text().replace('##TENSORRT_VERSION##', '8.2.1.0')
                 .replace('bool: np.bool,', 'bool: np.bool_,'))
PY
"$python" -m pip install --no-build-isolation --no-deps .
"$python" - <<'PY'
import gi, tensorrt, numpy, PIL
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)
print('TensorRT', tensorrt.__version__, 'GStreamer', Gst.version_string())
assert tensorrt.nptype(tensorrt.float32) is numpy.float32
assert tensorrt.nptype(tensorrt.bool) is numpy.bool_
for name in ('rtspsrc', 'nvv4l2decoder', 'nvvidconv', 'jpegenc', 'appsink', 'splitmuxsink'):
    assert Gst.ElementFactory.find(name), name
print('NANO_RUNTIME_READY')
PY
