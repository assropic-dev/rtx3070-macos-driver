"""Compare fresh drawable creation on the loaded framebuffer before and after the update."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import time

BUILD = Path(__file__).resolve().parent.parent
PROBE = BUILD / "sync-10ms/surface-scale/probe"
PLUGIN = "/Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver"
SIZES = ("5120x2880x4", "4170x2426x8")
COUNTERS = ("vram_prepare_hits", "vram_prepare_misses", "vram_prepare_cleared", "vram_prepare_budget")


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def counters():
    values = output("sysctl", "-n", *("debug.nvrmfb_" + name for name in COUNTERS))
    return dict(zip(COUNTERS, map(int, values.splitlines())))


def measure(size, count):
    before = counters()
    env = dict(os.environ, NVMTL_PROCESS_ENV="1", NVMTL_SURF_IMPORT_CACHE="1", IOSURFACE_SCALE_COLD_ONLY="1")
    result = subprocess.run([PROBE, PLUGIN, "1", str(count), size], env=env,
                            text=True, check=True, stdout=subprocess.PIPE, timeout=45)
    after = counters()
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    if len(rows) != count or any(r["phase"] != "cold" or r["family_vram"] != "1" for r in rows):
        raise RuntimeError("The probe did not measure the requested fresh GPU-backed surfaces")
    return {"size": size, "texture_ms": [int(r["texture_ns"]) / 1e6 for r in rows],
            "counter_delta": {key: after[key] - before[key] for key in COUNTERS}}


def state():
    loaded = output("kmutil", "showloaded")
    fb = next(line.strip() for line in loaded.splitlines() if "com.nullmoth.NVRMFB (" in line)
    return {"framebuffer": fb, "boot": output("sysctl", "-n", "kern.bootsessionuuid"),
            "mode": json.loads(output(BUILD / "continuous-window-trace/mode"))}


if __name__ == "__main__":
    start = state()
    warmup = [measure(size, 1) for size in SIZES]
    bursts = []
    for size in SIZES * 2:
        time.sleep(4)
        bursts.append(measure(size, 3))
    if state() != start:
        raise RuntimeError("Display mode or loaded driver changed during the measurement")
    print(json.dumps({**start, "warmup": warmup, "alternating_bursts": bursts}, indent=2))
