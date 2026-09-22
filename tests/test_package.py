"""Exercise installation with local wheels, without network or a project build."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def write_wheel(directory, name, version, files, requires=()):
    metadata = f'{name}-{version}.dist-info'
    contents = dict(files)
    contents[f'{metadata}/METADATA'] = (
        f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n'
        + ''.join(f'Requires-Dist: {requirement}\n' for requirement in requires)
    )
    contents[f'{metadata}/WHEEL'] = (
        'Wheel-Version: 1.0\nGenerator: installation-regression-test\n'
        'Root-Is-Purelib: true\nTag: py3-none-any\n'
    )
    record = f'{metadata}/RECORD'
    contents[record] = ''.join(f'{path},,\n' for path in [*contents, record])
    path = directory / f'{name}-{version}-py3-none-any.whl'
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in contents.items():
            archive.writestr(name, content)


class PackageInstallationTests(unittest.TestCase):
    def test_same_version_replaces_only_project_after_resolving_dependencies(self):
        with tempfile.TemporaryDirectory(prefix='chat-package-test-') as temporary:
            root = Path(temporary)
            (root / 'scripts').mkdir()
            shutil.copyfile(ROOT / 'scripts/package.py', root / 'scripts/package.py')
            wheels = root / 'dist'
            wheels.mkdir()
            (wheels / '.success').touch()
            environment = root / 'environment'
            # Use the existing pip without downloading/bootstrap-installing one.
            builder = venv.EnvBuilder(system_site_packages=True)
            builder.create(environment)
            python = builder.ensure_directories(environment).env_exe
            process_env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PIP_NO_COMPILE='1',
                               PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK='1')
            for name in ('PIP_TARGET', 'PIP_PREFIX', 'PIP_ROOT', 'PIP_USER', 'PYTHONPATH'):
                process_env.pop(name, None)
            site_packages = Path(subprocess.check_output(
                [str(python), '-B', '-c', "import sysconfig; print(sysconfig.get_path('purelib'))"],
                env=process_env, text=True,
            ).strip())

            def project(value, *, obsolete=False, requires=('chat_install_dependency>=1',)):
                files = {'chat_install_project.py': value}
                if obsolete:
                    files['chat_install_obsolete.py'] = 'old'
                write_wheel(wheels, 'openevent_modules_chat', '0.1.0', files, requires)

            def install(*args):
                return subprocess.run(
                    [str(python), '-B', str(root / 'scripts/package.py'), 'install',
                     '--no-index', '--find-links', str(wheels), *args],
                    env=process_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )

            write_wheel(wheels, 'chat_install_dependency', '1.0', {'chat_install_dependency.py': 'dependency one'})
            project('old', obsolete=True)
            first = install()
            self.assertEqual(first.returncode, 0, first.stdout)
            dependency = site_packages / 'chat_install_dependency.py'
            self.assertEqual(dependency.read_text(), 'dependency one')
            dependency_mtime = dependency.stat().st_mtime_ns
            self.assertEqual((site_packages / 'chat_install_project.py').read_text(), 'old')

            # A newer compatible dependency must not be upgraded just to replace
            # the project wheel. Obsolete project files must also be removed.
            write_wheel(wheels, 'chat_install_dependency', '2.0', {'chat_install_dependency.py': 'dependency two'})
            project('new')
            second = install()
            self.assertEqual(second.returncode, 0, second.stdout)
            self.assertEqual((site_packages / 'chat_install_project.py').read_text(), 'new')
            self.assertFalse((site_packages / 'chat_install_obsolete.py').exists())
            self.assertEqual(dependency.read_text(), 'dependency one')
            self.assertEqual(dependency.stat().st_mtime_ns, dependency_mtime)

            project('dry run')
            dry_run = install('--dry-run')
            self.assertEqual(dry_run.returncode, 0, dry_run.stdout)
            self.assertEqual((site_packages / 'chat_install_project.py').read_text(), 'new')

            project('must not install', requires=('chat_install_missing_dependency==1',))
            failed = install()
            self.assertNotEqual(failed.returncode, 0, failed.stdout)
            self.assertEqual((site_packages / 'chat_install_project.py').read_text(), 'new')

    def test_alternate_destinations_are_rejected_before_installation(self):
        for arguments in (('--target', '/unused'), ('--target=/unused',), ('-t/unused',),
                          ('--prefix', '/unused'), ('--root=/unused',)):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, '-B', str(ROOT / 'scripts/package.py'), 'install', *arguments],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('choose its interpreter with PYTHON', result.stdout)
