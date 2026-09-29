"""Small make helpers; never select an artifact left by a failed build."""
from pathlib import Path
from email.parser import BytesParser
import os
import shutil
import subprocess
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ['PIP_NO_COMPILE'] = '1'


def remove(path):
    if path.exists():
        shutil.rmtree(path)


def wheel():
    paths = list((ROOT / 'dist').glob('openevent_modules_chat-*.whl'))
    if len(paths) != 1 or not (ROOT / 'dist/.success').is_file():
        sys.exit('Exactly one wheel from a successful make build is required')
    return str(paths[0])


def pip(*args):
    temporary = ROOT / 'build/tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary),
               PIP_CACHE_DIR=str(ROOT / 'build/cache/pip'))
    subprocess.run([sys.executable, '-B', '-m', 'pip', *args], env=env, check=True)


action = sys.argv[1]
if action == 'build':
    remove(ROOT / 'dist')
    remove(ROOT / 'build/package')
    remove(ROOT / 'build/wheel-test')
    for path in (ROOT / 'src').glob('*.egg-info'):
        remove(path)
    # Setuptools writes metadata beside its input sources. Build a local copy so
    # those files and all other intermediate outputs stay inside build/.
    source = ROOT / 'build/package'
    source.mkdir(parents=True)
    for name in ('pyproject.toml', 'README.md', 'LICENSE'):
        shutil.copyfile(ROOT / name, source / name)
    shutil.copytree(ROOT / 'src', source / 'src',
                    ignore=shutil.ignore_patterns('*.egg-info', '__pycache__', '*.pyc'))
    pip('wheel', '--no-deps', '--wheel-dir', str(ROOT / 'dist'), str(source))
    (ROOT / 'dist/.success').touch()
elif action == 'dependencies':
    with ZipFile(wheel()) as archive:
        metadata_path, = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        requirements = BytesParser().parsebytes(archive.read(metadata_path)).get_all('Requires-Dist', [])
    if requirements:
        pip('install', '--no-compile', *requirements)
elif action == 'install':
    args = sys.argv[2:]
    destinations = ('--target', '--prefix', '--root')
    if (any(arg.startswith('-t') or any(arg == option or arg.startswith(option + '=')
                                       for option in destinations) for arg in args)
            or any(os.environ.get(name) for name in ('PIP_TARGET', 'PIP_PREFIX', 'PIP_ROOT'))):
        sys.exit('make install installs into the selected Python environment; '
                 'choose its interpreter with PYTHON instead of --target, --prefix, or --root')
    artifact = wheel()
    # Resolve ordinary dependencies first, then replace only this project even
    # when its version is unchanged. Do not force-reinstall its dependencies.
    pip('install', '--no-compile', *args, artifact)
    pip('install', '--no-compile', '--force-reinstall', '--no-deps', *args, artifact)
elif action == 'verify':
    target = ROOT / 'build/wheel-test'
    remove(target)
    pip('install', '--no-deps', '--no-compile', '--target', str(target), wheel())
    if (target / 'openevent/__init__.py').exists():
        sys.exit('OpenEvent namespace must stay shared with the installed SDK')
    env = dict(os.environ, PYTHONPATH=str(target))
    subprocess.run([sys.executable, '-B', '-c',
        'from importlib.resources import files; import openevent.sdk, openevent.chat_sdk, openevent.chat_app; '
        'assert files("openevent.chat_app").joinpath("static/index.html").is_file()'],
        env=env, check=True)
    if list((ROOT / 'src').glob('*.egg-info')) or list(ROOT.glob('*.egg-info')):
        sys.exit('Build metadata must stay under build/')
elif action == 'clean':
    for path in (ROOT / 'build', ROOT / 'dist', *(ROOT / 'src').glob('*.egg-info')):
        remove(path)
else:
    sys.exit('Unknown packaging action')
