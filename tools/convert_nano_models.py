"""Expand ONNX HardSwish for TensorRT 8.2 and verify against the originals.

Run on a host with onnx and onnxruntime installed. Original Orin artifacts are
never overwritten. This changes graph representation, not weights or thresholds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import onnx
from onnx import helper
import onnxruntime as ort


def convert(source: Path, target: Path) -> dict:
    if source.resolve() == target.resolve():
        raise ValueError("source and target must differ")
    model = onnx.load(source)
    nodes = []
    used = {value for node in model.graph.node for value in (*node.input, *node.output)}
    count = 0
    for index, node in enumerate(model.graph.node):
        if node.op_type != "HardSwish" or node.domain not in ("", "ai.onnx"):
            nodes.append(node)
            continue
        if len(node.input) != 1 or len(node.output) != 1 or node.attribute:
            raise ValueError("unsupported HardSwish form")
        name = f"nano_hardswish_{index}"
        if name in used:
            raise ValueError("generated tensor name collision")
        used.add(name)
        nodes.extend([
            helper.make_node("HardSigmoid", list(node.input), [name],
                             name=name + "_gate", alpha=1.0 / 6.0, beta=0.5),
            helper.make_node("Mul", [node.input[0], name], list(node.output),
                             name=name + "_multiply"),
        ])
        count += 1
    del model.graph.node[:]
    model.graph.node.extend(nodes)
    onnx.checker.check_model(model)
    original = ort.InferenceSession(str(source), providers=["CPUExecutionProvider"])
    converted = ort.InferenceSession(model.SerializeToString(), providers=["CPUExecutionProvider"])
    declared = original.get_inputs()[0].shape
    batches = (1, 4, 20) if not isinstance(declared[0], int) else (declared[0],)
    rng = np.random.default_rng(20261019)
    errors = []
    for batch in batches:
        shape = [batch, *declared[1:]]
        for kind in ("zeros", "random"):
            x = np.zeros(shape, np.float32) if kind == "zeros" else rng.uniform(0, 255, shape).astype(np.float32)
            values = {original.get_inputs()[0].name: x}
            expected, actual = original.run(None, values), converted.run(None, values)
            for wanted, got in zip(expected, actual):
                if not np.isfinite(got).all() or not np.allclose(wanted, got, atol=1e-4, rtol=1e-4):
                    raise RuntimeError("converted model output differs from original")
                errors.append(float(np.max(np.abs(wanted - got))))
    target.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, target)
    return {"file": source.name, "expanded_hardswish": count,
            "batches_checked": list(batches), "max_absolute_error": max(errors),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "converted_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = [convert(args.source / name, args.output / name) for name in (
        "surfer_fcos_raw.onnx", "actions_mobilenet_v3_small.onnx")]
    proof = {"verification": "onnxruntime_cpu_float32", "models": results}
    (args.output / "conversion-proof.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(json.dumps(proof, indent=2))


if __name__ == "__main__":
    main()
