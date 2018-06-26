from __future__ import print_function
import json
import os
import pkgutil
import sys
import shutil
import subprocess
import glob
from collections import namedtuple
from operator import methodcaller
import distutils.spawn
import re
try:
    subprocess.run

    def run(*args, **kw):
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        r = subprocess.run(*args, **kw)
        r.stdout, r.stderr = map(proc_output, (r.stdout, r.stderr))
        debugp('[run] argv={} code={} stdout={} stderr={}'.format(args, r.returncode, r.stdout, r.stderr))
        return r
except AttributeError:  # no `subprocess.run`, py < 3.5
    CompletedProcess = namedtuple('CompletedProcess',
                                  ('args', 'returncode', 'stdout', 'stderr'))

    def run(argv, **kw):
        p = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
        out, err = map(proc_output, p.communicate())
        cp = CompletedProcess(argv, p.returncode, out, err)
        debugp('[run] argv={} code={} stdout={} stderr={}'.format(argv, p.returncode, out, err))
        return cp
try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse

import click
from pkg_resources import Requirement


try:
    WindowsError
except NameError:
    IS_WIN = False
    BIN_DIR = 'bin'
else:
    IS_WIN = True
    BIN_DIR = 'Scripts'

FIND_SCRIPTS_SCRIPT = pkgutil.get_data('pipsi', 'scripts/find_scripts.py').decode('utf-8')
GET_VERSION_SCRIPT = pkgutil.get_data('pipsi', 'scripts/get_version.py').decode('utf-8')

# The `click` custom context settings
CONTEXT_SETTINGS = dict(
    help_option_names=['-h', '--help'],
)


def debugp(*args):
    if os.environ.get('PIPSI_DEBUG'):
        print(*args)


def proc_output(s):
    s = s.strip()
    if  isinstance(s, bytes):
        s = s.decode('utf-8', 'replace')
    return s


def normalize_package(value):
    # Strips the version and normalizes name
    requirement = Requirement.parse(value)
    return requirement.project_name.lower()


def normalize_path(path):
    return os.path.normcase(norm_real_path(path))


def norm_real_path(path):
    return os.path.normpath(os.path.realpath(path))


def real_readlink(filename):
    try:
        target = os.readlink(filename)
    except (OSError, IOError, AttributeError):
        return None
    return norm_real_path(os.path.join(os.path.dirname(filename), target))


def publish_script(src, dst):
    if IS_WIN:
        # always copy new exe on windows
        shutil.copy(src, dst)
        click.echo('  Copied Executable ' + dst)
        return True
    else:
        old_target = real_readlink(dst)
        if old_target == src:
            return True
        try:
            os.remove(dst)
        except OSError:
            pass
        try:
            os.symlink(src, dst)
        except OSError:
            pass
        else:
            click.echo('  Linked script ' + dst)
            return True


def extract_package_version(virtualenv, package):
    prefix = normalize_path(os.path.join(virtualenv, BIN_DIR, ''))

    return run([
        os.path.join(prefix, 'python'), '-c', GET_VERSION_SCRIPT,
        package,
    ]).stdout.strip()


def find_scripts(virtualenv, package):
    prefix = normalize_path(os.path.join(virtualenv, BIN_DIR, ''))

    files = run([
        os.path.join(prefix, 'python'), '-c', FIND_SCRIPTS_SCRIPT,
        package, prefix
    ]).stdout.splitlines()

    files = map(normalize_path, files)
    files = filter(
        methodcaller('startswith', prefix),
        files,
    )

    def valid(filename):
        return os.path.isfile(filename) and \
            IS_WIN or os.access(filename, os.X_OK)

    result = list(filter(valid, files))

    if IS_WIN:
        for filename in files:
            globed = glob.glob(filename + '*')
            result.extend(filter(valid, globed))
    return result


class UninstallInfo(object):

    def __init__(self, package, paths=None, installed=True):
        self.package = package
        self.paths = paths or []
        self.installed = installed

    def perform(self):
        for path in self.paths:
            try:
                os.remove(path)
            except OSError:
                shutil.rmtree(path)


python_semver_regex = re.compile(r'^Python (\d)\.(\d+)\.(\d+)')


def get_python_semver(python_bin):
    cmd = [python_bin, '--version']
    r = run(cmd)
    if r.returncode != 0:
        raise ValueError(
            'Failed to run {}: {}, {}, {}'.format(cmd, r.returncode, r.stdout, r.stderr))
    raw_version = r.stdout.strip()
    if not raw_version:
        raw_version = r.stderr.strip()
    r = python_semver_regex.search(raw_version)
    if not r:
        raise ValueError(
            'Could not match {} out of {}'.format(
                python_semver_regex.pattern, repr(raw_version)))
    return tuple(int(i) for i in r.groups())


code_for_get_real_python = (
    'import sys; print("{},{}".format('
    'getattr(sys, "real_prefix", ""), '
    'sys.version_info.major))'
)


# `venv` for python 3 has the problem that `venv` cannot
# add pip in virtualenv if it is executed under a virtualenv,
# use this function to avoid this problem
def get_real_python(python):
    cmd = [python, '-c', code_for_get_real_python]
    r = run(cmd)
    if r.returncode != 0:
        raise ValueError(
            'Failed to run {}: {}, {}, {}'.format(cmd, r.returncode, r.stdout, r.stderr))
    debugp('get_real_python run {}: {}, {}, {}'.format(
        cmd, r.returncode, r.stdout, r.stderr))

    real_prefix, major = r.stdout.strip().split(',')
    if not real_prefix:
        return python

    for i in [major, '']:
        real_python = os.path.join(real_prefix, 'bin', 'python' + i)
        if os.path.exists(real_python):
            return real_python
    raise ValueError('Can not find real python under {}'.format(real_prefix))


class PackageInfo(object):
    name = None
    version = None
    scripts = []

    class Keys:
        name = 'name'
        version = 'version'
        scripts = 'scripts'

    def __init__(self, name, version, scripts):
        self.name = name
        self.version = version
        self.scripts = scripts

    def to_json(self, **kwargs):
        return json.dumps(self.to_dict(), **kwargs)

    def to_dict(self):
        return {
            self.Keys.name: self.name,
            self.Keys.version: self.version,
            self.Keys.scripts: self.scripts,
        }

    @classmethod
    def create_from_venv_path(cls, venv_path, package_name, scripts=None):
        """Create PackageInfo instance from venv_path, this function should be used
        when json file does not exist.
        """
        if scripts is None:
            scripts = []
        version = extract_package_version(venv_path, package_name)

        return cls(package_name, version, scripts)

    @classmethod
    def create_from_json(cls, json_str):
        d = json.loads(json_str)
        return cls(d.get(cls.Keys.name), d.get(cls.Keys.version), d.get(cls.Keys.scripts, []))


class Repo(object):
    package_info_filename = 'package_info.json'

    def __init__(self, home, bin_dir):
        self.home = os.path.realpath(home)
        self.bin_dir = bin_dir

    def resolve_package(self, spec, python=None):
        url = urlparse(spec)
        if url.netloc == 'file':
            location = url.path
        elif url.netloc != '':
            if not url.fragment.startswith('egg='):
                raise click.UsageError('When installing from URLs you need '
                                       'to add an egg at the end.  For '
                                       'instance git+https://.../#egg=Foo')
            return url.fragment[4:], [spec]
        elif os.path.isdir(spec):
            location = spec
        else:
            return spec, [spec]

        if not os.path.exists(os.path.join(location, 'setup.py')):
            raise click.UsageError('%s does not appear to be a local '
                                   'Python package.' % spec)

        res = run(
            [python or sys.executable, 'setup.py', '--name'],
            cwd=location)
        if res.returncode:
            raise click.UsageError(
                '%s does not appear to be a valid '
                'package. Error from setup.py: %s' % (spec, res.stderr)
            )
        name = res.stdout

        return name, [location]

    def get_package_path(self, package):
        package_name = normalize_package(package)
        return os.path.join(self.home, package_name), package_name

    def find_installed_executables(self, venv_path):
        prefix = os.path.join(norm_real_path(venv_path), '')
        try:
            for filename in os.listdir(self.bin_dir):
                exe = os.path.join(self.bin_dir, filename)
                target = real_readlink(exe)
                if target is None:
                    continue
                if target.startswith(prefix):
                    yield exe
        except OSError:
            pass

    def link_scripts(self, scripts):
        """Link venv script path to bin dir,
        returns list of tuple of (venv script path, bin dir symlink path)
        """
        rv = []
        for script in scripts:
            script_dst = os.path.join(
                self.bin_dir, os.path.basename(script))
            if publish_script(script, script_dst):
                rv.append((script, script_dst))

        return rv

    def save_package_info(self, venv_path, package_name, scripts):
        filepath = os.path.join(venv_path, self.package_info_filename)

        o = PackageInfo.create_from_venv_path(venv_path, package_name, scripts)
        with open(filepath, 'w') as f:
            # append EOL
            f.write(o.to_json() + '\n')

    def get_package_info(self, venv_path, package_name=None):
        """Get dict from json file `package_info.json` under venv

        if the file does not exist, get the result from `PackageInfo.create_from_venv_path`
        raises ValueError if the file content cannot be parsed to json.
        """
        filepath = os.path.join(venv_path, self.package_info_filename)
        if os.path.exists(filepath) and os.path.isfile(filepath):
            with open(filepath, 'r') as f:
                return PackageInfo.create_from_json(f.read())
        else:
            if package_name is None:
                package_name = os.path.basename(venv_path)
            return PackageInfo.create_from_venv_path(
                venv_path, package_name, scripts=list(self.find_installed_executables(venv_path)))

    def install(self, package, python=None, editable=False, system_site_packages=False):
        """
        :param str package: package spec, not necessarily the name of package
        """
        # `python` could be int as major version, or str as absolute bin path,
        # if it's int, then we will try to find the executable `python2` or `python3` in PATH
        if isinstance(python, int):
            python_exe = 'python{}'.format(python)
            python = distutils.spawn.find_executable(python_exe)
            if not python:
                raise ValueError('Can not find {} in PATH'.format(python_exe))
        if not python:
            python = sys.executable
        python_semver = get_python_semver(python)
        debugp('python: {}, python_bin_semver: {}'.format(python, python_semver))

        package, install_args = self.resolve_package(package, python)

        # use package_name for the lower cased name afterwards
        venv_path, package_name = self.get_package_path(package)
        if os.path.isdir(venv_path):
            click.echo('%s is already installed' % package_name)
            return

        if not os.path.exists(self.bin_dir):
            os.makedirs(self.bin_dir)

        from subprocess import Popen

        def _cleanup():
            try:
                shutil.rmtree(venv_path)
            except (OSError, IOError):
                pass
            return False

        # Install virtualenv, use the pipsi used python version by default
        args = [sys.executable, '-m', 'virtualenv', '-p', python, venv_path]

        if python_semver[0] == 3:
            # if target python is 3, use its builtin `venv` module to create virtualenv
            real_python = get_real_python(python)
            args = [real_python, '-m', 'venv', venv_path]

        if system_site_packages:
            args.append('--system-site-packages')

        try:
            debugp('Popen: {}'.format(args))
            if Popen(args).wait() != 0:
                click.echo('Failed to create virtualenv.  Aborting.')
                return _cleanup()

            args = [os.path.join(venv_path, BIN_DIR, 'python'), '-m', 'pip', 'install']
            if editable:
                args.append('--editable')

            debugp('Popen: {}'.format(args + install_args))
            if Popen(args + install_args).wait() != 0:
                click.echo('Failed to pip install.  Aborting.')
                return _cleanup()
        except Exception:
            _cleanup()
            raise

        # Find all the scripts
        scripts = find_scripts(venv_path, package)

        # And link them
        linked_scripts = self.link_scripts(scripts)

        self.save_package_info(venv_path, package_name, [i[1] for i in linked_scripts])

        # We did not link any, rollback.
        if not linked_scripts:
            click.echo('Did not find any scripts.  Uninstalling.')
            return _cleanup()
        return True

    def check_package_installed(self, package, venv_path, echo=False):
        if not os.path.isdir(venv_path):
            if echo:
                click.echo('%s is not installed' % package)
            return False
        return True

    def uninstall(self, package):
        venv_path, package_name = self.get_package_path(package)
        if not self.check_package_installed(package, venv_path):
            return UninstallInfo(package_name, installed=False)

        info = self.get_package_info(venv_path, package_name)
        paths = [venv_path] + info.scripts
        return UninstallInfo(info.name, paths)

    def upgrade(self, package, editable=False):
        package, install_args = self.resolve_package(package)

        venv_path, package_name = self.get_package_path(package)
        if not self.check_package_installed(package, venv_path, echo=True):
            return

        info = self.get_package_info(venv_path, package_name)

        from subprocess import Popen

        old_scripts = set(info.scripts)

        args = [os.path.join(venv_path, BIN_DIR, 'python'), '-m', 'pip', 'install',
                '--upgrade']
        if editable:
            args.append('--editable')

        if Popen(args + install_args).wait() != 0:
            click.echo('Failed to upgrade through pip.  Aborting.')
            return

        scripts = find_scripts(venv_path, package)
        linked_scripts = self.link_scripts(scripts)
        to_delete = old_scripts - set(i[1] for i in linked_scripts)

        for script in to_delete:
            try:
                click.echo('  Removing old script %s' % script)
                os.remove(script)
            except (IOError, OSError):
                pass

        self.save_package_info(venv_path, package_name, linked_scripts)

        return True

    def list_everything(self, versions=False):
        venvs = {}
        python = '/Scripts/python.exe' if IS_WIN else '/bin/python'
        if os.path.isdir(self.home):
            for venv in os.listdir(self.home):
                venv_path = os.path.join(self.home, venv)
                if os.path.isdir(venv_path) and \
                   os.path.isfile(venv_path + python):
                    info = self.get_package_info(venv_path)
                    venvs[venv] = [info.scripts, info.version]

        return sorted(venvs.items())


@click.group(context_settings=CONTEXT_SETTINGS)
@click.option(
    '--home', type=click.Path(),envvar='PIPSI_HOME',
    default=os.path.join(os.path.expanduser('~'), '.local', 'venvs'),
    help='The folder that contains the virtualenvs.')
@click.option(
    '--bin-dir', type=click.Path(),
    envvar='PIPSI_BIN_DIR',
    default=os.path.join(os.path.expanduser('~'), '.local', 'bin'),
    help='The path where the scripts are symlinked to.')
@click.version_option(
    message='%(prog)s, version %(version)s, python ' + str(sys.executable))
@click.pass_context
def cli(ctx, home, bin_dir):
    """pipsi is a tool that uses virtualenv and pip to install shell
    tools that are separated from each other.
    """
    ctx.obj = Repo(home, bin_dir)


@cli.command()
@click.argument('package')
@click.option(
    '--python', type=str,
    envvar='PIPSI_PYTHON',
    default=sys.executable,
    help=('The python interpreter to use, could be major version or path. '
          'By default it would be `sys.executable`'))
@click.option('--editable', '-e', is_flag=True,
              help='Enable editable installation.  This only works for '
                   'locally installed packages.')
@click.option('--system-site-packages', is_flag=True,
              help='Give the virtual environment access to the global '
                   'site-packages.')
@click.pass_obj
def install(repo, package, python, editable, system_site_packages):
    """Installs scripts from a Python package.

    Given a package this will install all the scripts and their dependencies
    of the given Python package into a new virtualenv and symlinks the
    discovered scripts into BIN_DIR (defaults to ~/.local/bin).
    """
    if re.search(r'^\d$', python):
        python = int(python)
    if repo.install(package, python, editable, system_site_packages):
        click.echo('Done.')
    else:
        sys.exit(1)


@cli.command()
@click.argument('package')
@click.option('--editable', '-e', is_flag=True,
              help='Enable editable installation.  This only works for '
                   'locally installed packages.')
@click.pass_obj
def upgrade(repo, package, editable):
    """Upgrades an already installed package."""
    if repo.upgrade(package, editable):
        click.echo('Done.')
    else:
        sys.exit(1)


@cli.command(short_help='Uninstalls scripts of a package.')
@click.argument('package')
@click.option('--yes', is_flag=True, help='Skips all prompts.')
@click.pass_obj
def uninstall(repo, package, yes):
    """Uninstalls all scripts of a Python package and cleans up the
    virtualenv.
    """
    uinfo = repo.uninstall(package)
    if not uinfo.installed:
        click.echo('%s is not installed' % uinfo.package)
    else:
        click.echo('The following paths will be removed:')
        for path in uinfo.paths:
            click.echo('  %s' % click.format_filename(path))
        click.echo()
        if yes or click.confirm('Do you want to uninstall %s?' % uinfo.package):
            uinfo.perform()
            click.echo('Done!')
        else:
            click.echo('Aborted!')
            sys.exit(1)


@cli.command('list')
@click.option('--versions', is_flag=True,
              help='Show packages version')
@click.pass_obj
def list_cmd(repo, versions):
    """Lists all scripts installed through pipsi."""
    list_of_non_empty_venv = [(venv, scripts)
                              for venv, scripts in repo.list_everything()
                              if scripts]
    if list_of_non_empty_venv:
        click.echo('Packages and scripts installed through pipsi:')
        for venv, (scripts, version) in list_of_non_empty_venv:
            if versions:
                click.echo('  Package "%s" (%s):' % (venv, version or 'unknown'))
            else:
                click.echo('  Package "%s":' % venv)
            for script in scripts:
                click.echo('    ' + script)
    else:
        click.echo('There are no scripts installed through pipsi')


if __name__ == '__main__':
    cli()
