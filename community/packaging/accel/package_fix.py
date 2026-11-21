"""Build the full local driver package with selected tested Metal and NVAccel binaries."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

scratch = Path(__file__).resolve().parent
workspace = scratch.parents[1]
build = Path(os.environ.get('NV_DRIVER_BUILD', workspace / 'build-3.0'))
mode, = sys.argv[1:]
if mode not in ('update', 'rollback'):
    raise ValueError(mode)
baseline = mode == 'rollback'
driver = scratch.parent / 'NVMTLDriver-8GiB' if baseline else scratch / 'NVMTLDriver'
accel = build / 'accel' if baseline else scratch / 'final-accel'
name = 'RTX3070-Frame-Sync-' + ('Rollback' if baseline else 'Fix') + '.pkg'
output = workspace / 'outputs' / name
if output.exists():
    raise FileExistsError(output)
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('package_local', build / 'package_local.py')
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)
with tempfile.TemporaryDirectory(dir=scratch) as tmpdir:
    tmp = Path(tmpdir)
    (tmp / 'plugin').mkdir()
    shutil.copy2(driver, tmp / 'plugin/NVMTLDriver')
    for key, path in [('accel', accel), ('fb', build / 'fb'), ('video', build / 'video')]:
        (tmp / key).symlink_to(path, target_is_directory=True)
    pack.BUILD = tmp
    pack.IDENTIFIER = 'local.dewei.rtx3070.frame-sync.' + mode
    pack.OUTPUT = tmp / name
    pack.main()
    inspection = tmp / 'inspection'
    subprocess.run(['pkgutil', '--expand-full', pack.OUTPUT, inspection], check=True)
    base = Path('Library/GPUBundles/NVMTLDriver.bundle')
    exe = Path('Contents/MacOS/NVMTLDriver')
    shipped = tmp / 'shipped'
    shipped.mkdir()
    with tarfile.open(inspection / 'Scripts/local-driver.tar.gz') as tar:
        for path in [base / exe, Path('Library/NullMoth/kexts/26/NVAccel.kext/Contents/MacOS/NVAccel')]:
            target = shipped / path.name
            with tar.extractfile('pkgroot/' + str(path)) as src, target.open('wb') as dst:
                shutil.copyfileobj(src, dst)
            print('embedded', target.name, pack.sha(target), flush=True)
            built = driver if path.name == 'NVMTLDriver' else accel / '26/NVAccel.kext/Contents/MacOS/NVAccel'
            assert pack.unsigned_content(built, tmp / 'input') == pack.unsigned_content(target, tmp / 'packed')
            if baseline:
                installed = Path('/') / base / exe if path.name == 'NVMTLDriver' else Path('/Library/Extensions/NVAccel.kext/Contents/MacOS/NVAccel')
                assert pack.sha(target) == pack.sha(installed), 'rollback must restore exact installed signed executable'
    os.link(pack.OUTPUT, output)
    print(output, pack.sha(output), flush=True)
