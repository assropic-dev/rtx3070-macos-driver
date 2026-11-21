#!/usr/bin/env python3
"""Exercise the packed 10-bit scanout gate and the CSC actually submitted for each surface."""
import pathlib
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
fb = (root / 'kexts/NVRMFB/fb/nvrm-fb.cpp').read_text()
gate_start = fb.index('bool NVRMNVDAFramebuffer::canScanL10r() const')
gate = fb[fb.index('{', gate_start):fb.index('\n}', gate_start) + 2]
submit = fb.index('NvBool NVRMNVDAFramebuffer::submitConfig(')
prepare = fb[fb.index('    struct NvKmsKapiLayerRequestedConfig &primary', submit):
             fb.index('    surroundReplicate(cfg);', submit)]
publish = next(line for line in fb.splitlines() if 'if (primary.flags.surfaceChanged) fFrontL10r =' in line)
flip = fb.index('bool NVRMNVDAFramebuffer::flipToMemory(')
preflight = fb[fb.index('    bool valid = true;', flip):fb.index('    if (pure) { const SInt64 us', flip)]

fixture = r'''
#include <cassert>
#include "nvrm-fb-safety.h"
enum { NVKMS_KAPI_LAYER_PRIMARY_IDX = 0, NV_TRUE = 1, NvKmsSurfaceMemoryFormatX2B10G10R10 = 5 };
struct NvKmsKapiLayerRequestedConfig {
    struct { bool cscChanged = false, surfaceChanged = false; } flags;
    struct { void *surface = nullptr; struct { int m[3][4] = {}; } csc; } config;
};
struct Config {
    struct { struct { bool bActive = true; } modeSetConfig;
             NvKmsKapiLayerRequestedConfig layerRequestedConfig[1]; } headRequestedConfig[1];
};
struct Head {
    bool fScan30 = true, fHdrActive = false, fSurActive = false, fProxy = false;
    unsigned long long fPrimaryFormats = 1ULL << NvKmsSurfaceMemoryFormatX2B10G10R10;
    unsigned fHead = 0;
    int gVibrance = 0;
    void *fFrontL10r = nullptr;
    bool canScanL10r() const GATE
    void submit(Config *cfg, bool frame, bool l10r, bool accepted) {
        PREPARE
        if (accepted) { PUBLISH }
    }
};
struct Probe {
    using NvBool = bool;
    enum { NV_TRUE = 1, NV_FALSE = 0, NV_KMS_FLIP_RESULT_SUCCESS = 0 };
    struct Reply { int flipResult = 0; };
    bool testOK = true, front = false;
    int testResult = 0, tests = 0, commits = 0;
    static void nvu_zero(Reply *p, unsigned) { *p = {}; }
    bool submitConfig(Config *, Reply *p, bool commit, bool, bool, bool, bool) {
        if (commit) { commits++; return true; }
        tests++; p->flipResult = testResult; return testOK;
    }
    bool flip(bool l10r) {
        bool fFrontL10r = front;
        Config c, *cfg = &c;
        Reply r, *rep = &r;
        PREFLIGHT
        return ok;
    }
};
static void checkMatrix(const NvKmsKapiLayerRequestedConfig &layer, int vibrance, bool swapped)
{
    int expected[3][3];
    nvrmSaturationMatrix(vibrance, expected);
    // A red, green or blue ramp has the same output as the corresponding
    // correctly ordered source passed through the existing vibrance matrix.
    for (int row = 0; row < 3; row++) {
        for (int channel = 0; channel < 3; channel++)
            assert(layer.config.csc.m[row][swapped ? 2 - channel : channel] == expected[row][channel]);
        assert(layer.config.csc.m[row][3] == 0);
    }
    assert(layer.flags.cscChanged);
}
int main()
{
    Probe probe;
    probe.testOK = false; assert(!probe.flip(true) && probe.commits == 0);
    probe.testOK = true; probe.testResult = 1; assert(!probe.flip(true) && probe.commits == 0);
    probe.testResult = 0; assert(probe.flip(true) && probe.tests == 3 && probe.commits == 1);
    probe.front = true; assert(probe.flip(true) && probe.tests == 3 && probe.commits == 2);
    probe.front = false; assert(probe.flip(false) && probe.tests == 3 && probe.commits == 3);
    Head head; int packed, bgra;
    assert(head.canScanL10r());
    for (auto field : { &Head::fHdrActive, &Head::fSurActive, &Head::fProxy }) {
        head.*field = true; assert(!head.canScanL10r()); head.*field = false;
    }
    head.fScan30 = false; assert(head.canScanL10r()); // 10-bit input to an 8-bit SDR output
    head.fPrimaryFormats = 0; assert(!head.canScanL10r());
    Config cfg;
    auto &primary = cfg.headRequestedConfig[0].layerRequestedConfig[0];
    primary.config.surface = &packed; primary.flags.surfaceChanged = true;
    head.submit(&cfg, true, true, false); checkMatrix(primary, 0, true);
    assert(head.fFrontL10r == nullptr);
    head.submit(&cfg, true, true, true); assert(head.fFrontL10r == &packed);
    // A mode change retaining the same front and a live vibrance update.
    head.gVibrance = 65;
    head.submit(&cfg, false, false, true); checkMatrix(primary, 65, true);
    primary.flags.surfaceChanged = false; primary.config.surface = nullptr;
    head.submit(&cfg, false, false, true); checkMatrix(primary, 65, true);
    // An unrelated cursor update does not alter the primary CSC.
    primary.flags.cscChanged = false;
    head.submit(&cfg, false, false, true); assert(!primary.flags.cscChanged && head.fFrontL10r == &packed);
    primary.flags.surfaceChanged = true; primary.config.surface = &bgra;
    head.submit(&cfg, true, false, true); checkMatrix(primary, 65, false);
    assert(head.fFrontL10r == nullptr);
    primary.flags.cscChanged = false;
    head.submit(&cfg, true, false, true); assert(!primary.flags.cscChanged);
}
'''
fixture = ('#include <initializer_list>\n' + fixture.replace('GATE', gate).replace('PREPARE', prepare)
           .replace('PUBLISH', publish).replace('PREFLIGHT', preflight))
with tempfile.TemporaryDirectory() as tmp:
    directory = pathlib.Path(tmp)
    cpp = directory / 'scanout.cpp'
    cpp.write_text(fixture)
    binary = directory / 'scanout'
    subprocess.run(['clang++', '-std=c++17', '-Wall', '-Werror',
                    '-I' + str(root / 'kexts/NVRMFB/fb'), str(cpp), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
print('PASS: scanout format gates, RGB order, vibrance, retained front, rejected flip and return to BGRA')
