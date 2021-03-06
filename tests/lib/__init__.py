#!/usr/bin/env python
import imp
import os
import sys
import re
import tempfile
import shutil
import glob
import atexit
import textwrap
import site

from scripttest import TestFileEnvironment, FoundDir
from tests.lib.path import Path, curdir, u
from pip.util import rmtree
from pip.backwardcompat import uses_pycache

pyversion = sys.version[:3]
pyversion_nodot = "%d%d" % (sys.version_info[0], sys.version_info[1])
tests_lib = Path(__file__).abspath.folder # pip/tests/lib
tests_root = tests_lib.folder # pip/tests
tests_cache = os.path.join(tests_root, 'tests_cache') # pip/tests/tests_cache
src_folder = tests_root.folder  # pip/
tests_data = os.path.join(tests_root, 'data') # pip/tests/data
packages = os.path.join(tests_data, 'packages') # pip/tests/data/packages
tests_unit = os.path.join(tests_root, 'unit') # pip/tests/unit
tests_functional = os.path.join(tests_root, 'functional') # pip/tests/functional
download_cache = tempfile.mkdtemp(prefix='pip-test-cache')
site_packages_suffix = site.USER_SITE[len(site.USER_BASE) + 1:]

def path_to_url(path):
    """
    Convert a path to URI. The path will be made absolute and
    will not have quoted path parts.
    (adapted from pip.util)
    """
    path = os.path.normpath(os.path.abspath(path))
    drive, path = os.path.splitdrive(path)
    filepath = path.split(os.path.sep)
    url = '/'.join(filepath)
    if drive:
        return 'file:///' + drive + url
    return 'file://' +url

find_links = path_to_url(os.path.join(tests_data, 'packages'))
find_links2 = path_to_url(os.path.join(tests_data, 'packages2'))

def demand_dirs(path):
    if not os.path.exists(path):
        os.makedirs(path)


# Tweak the path so we can find up-to-date pip sources
# (http://bitbucket.org/ianb/pip/issue/98)
sys.path = [src_folder] + sys.path


def create_virtualenv(where, distribute=False):
    import virtualenv
    if sys.version_info[0] > 2:
        distribute = True
    virtualenv.create_environment(
        where, use_distribute=distribute, unzip_setuptools=True)

    return virtualenv.path_locations(where)


def relpath(root, other):
    """a poor man's os.path.relpath, since we may not have Python 2.6"""
    prefix = root+Path.sep
    assert other.startswith(prefix)
    return Path(other[len(prefix):])

if 'PYTHONPATH' in os.environ:
    del os.environ['PYTHONPATH']


try:
    any
except NameError:

    def any(seq):
        for item in seq:
            if item:
                return True
        return False


def clear_environ(environ):
    return dict(((k, v) for k, v in environ.items()
                if not k.lower().startswith('pip_')))


def install_setuptools(env):
    easy_install = os.path.join(env.bin_path, 'easy_install')
    version = 'setuptools==0.6c11'
    if sys.platform != 'win32':
        return env.run(easy_install, version)

    tempdir = tempfile.mkdtemp()
    try:
        for f in glob.glob(easy_install+'*'):
            shutil.copy2(f, tempdir)
        return env.run(os.path.join(tempdir, 'easy_install'), version)
    finally:
        rmtree(tempdir)


env = None

def reset_env(environ=None,
              use_distribute=False,
              system_site_packages=False,
              sitecustomize=None,
              insecure=True):
    """Return a test environment.

    Keyword arguments:
    environ: an environ object to use.
    use_distribute: use distribute, not setuptools.
    system_site_packages: create a virtualenv that simulates --system-site-packages.
    sitecustomize: a string containing python code to add to sitecustomize.py.
    insecure: how to set the --insecure option for py25 tests.
    """

    if sys.version_info >= (3,):
        use_distribute = True

    global env

    if use_distribute:
        test_class = TestPipEnvironmentD
    else:
        test_class = TestPipEnvironment

    env = test_class(environ, sitecustomize=sitecustomize)
    test_class.rebuild_venv = False

    if system_site_packages:
        #testing often occurs starting from a private virtualenv (e.g. with tox)
        #from that context, you can't successfully use virtualenv.create_environment
        #to create a 'system-site-packages' virtualenv
        #hence, this workaround
        (env.lib_path/'no-global-site-packages.txt').rm()
        test_class.rebuild_venv = True

    return env


class TestFailure(AssertionError):
    """

    An "assertion" failed during testing.

    """
    pass


#
# This cleanup routine prevents the __del__ method that cleans up the tree of
# the last TestPipEnvironment from firing after shutil has already been
# unloaded.  It also ensures that FastTestPipEnvironment doesn't leave an
# environment hanging around that might confuse the next test run.
#
def _cleanup():
    global env
    del env
    rmtree(download_cache, ignore_errors=True)
    rmtree(fast_test_env_root, ignore_errors=True)
    rmtree(fast_test_env_backup, ignore_errors=True)

atexit.register(_cleanup)


class TestPipResult(object):

    def __init__(self, impl, verbose=False):
        self._impl = impl

        if verbose:
            print(self.stdout)
            if self.stderr:
                print('======= stderr ========')
                print(self.stderr)
                print('=======================')

    def __getattr__(self, attr):
        return getattr(self._impl, attr)

    if sys.platform == 'win32':

        @property
        def stdout(self):
            return self._impl.stdout.replace('\r\n', '\n')

        @property
        def stderr(self):
            return self._impl.stderr.replace('\r\n', '\n')

        def __str__(self):
            return str(self._impl).replace('\r\n', '\n')
    else:
        # Python doesn't automatically forward __str__ through __getattr__

        def __str__(self):
            return str(self._impl)

    def assert_installed(self, pkg_name, editable=True, with_files=[], without_files=[], without_egg_link=False, use_user_site=False):
        e = self.test_env

        if editable:
            pkg_dir = e.venv/ 'src'/ pkg_name.lower()
        else:
            without_egg_link = True
            pkg_dir = e.site_packages / pkg_name

        if use_user_site:
            egg_link_path = e.user_site / pkg_name + '.egg-link'
        else:
            egg_link_path = e.site_packages / pkg_name + '.egg-link'
        if without_egg_link:
            if egg_link_path in self.files_created:
                raise TestFailure('unexpected egg link file created: '\
                                  '%r\n%s' % (egg_link_path, self))
        else:
            if not egg_link_path in self.files_created:
                raise TestFailure('expected egg link file missing: '\
                                  '%r\n%s' % (egg_link_path, self))

            egg_link_file = self.files_created[egg_link_path]

            if not (# FIXME: I don't understand why there's a trailing . here
                    egg_link_file.bytes.endswith('.')
                and egg_link_file.bytes[:-1].strip().endswith(pkg_dir)):
                raise TestFailure(textwrap.dedent(u('''\
                Incorrect egg_link file %r
                Expected ending: %r
                ------- Actual contents -------
                %s
                -------------------------------''' % (
                        egg_link_file,
                        pkg_dir + u('\n.'),
                        egg_link_file.bytes))))

        if use_user_site:
            pth_file = Path.string(e.user_site / 'easy-install.pth')
        else:
            pth_file = Path.string(e.site_packages / 'easy-install.pth')

        if (pth_file in self.files_updated) == without_egg_link:
            raise TestFailure('%r unexpectedly %supdated by install' % (
                pth_file, (not without_egg_link and 'not ' or '')))

        if (pkg_dir in self.files_created) == (curdir in without_files):
            raise TestFailure(textwrap.dedent('''\
            expected package directory %r %sto be created
            actually created:
            %s
            ''') % (
                Path.string(pkg_dir),
                (curdir in without_files and 'not ' or ''),
                sorted(self.files_created.keys())))

        for f in with_files:
            if not (pkg_dir/f).normpath in self.files_created:
                raise TestFailure('Package directory %r missing '\
                                  'expected content %f' % (pkg_dir, f))

        for f in without_files:
            if (pkg_dir/f).normpath in self.files_created:
                raise TestFailure('Package directory %r has '\
                                  'unexpected content %f' % (pkg_dir, f))


fast_test_env_root = tests_cache / 'test_ws'
fast_test_env_backup = tests_cache / 'test_ws_backup'


class TestPipEnvironment(TestFileEnvironment):
    """A specialized TestFileEnvironment for testing pip"""

    #
    # Attribute naming convention
    # ---------------------------
    #
    # Instances of this class have many attributes representing paths
    # in the filesystem.  To keep things straight, absolute paths have
    # a name of the form xxxx_path and relative paths have a name that
    # does not end in '_path'.

    # The following paths are relative to the root_path, and should be
    # treated by clients as instance attributes.  The fact that they
    # are defined in the class is an implementation detail

    # where we'll create the virtual Python installation for testing
    #
    # Named with a leading dot to reduce the chance of spurious
    # results due to being mistaken for the virtualenv package.
    venv = Path('.virtualenv')

    # The root of a directory tree to be used arbitrarily by tests
    scratch = Path('scratch')

    exe = sys.platform == 'win32' and '.exe' or ''
    verbose = False
    use_distribute = False
    # Keep short to undercut windows path length issues
    setuptools = 's'
    rebuild_venv = True

    def __init__(self, environ=None, sitecustomize=None):
        import virtualenv

        self.root_path = fast_test_env_root / self.setuptools
        self.backup_path = fast_test_env_backup / self.setuptools

        self.scratch_path = self.root_path / self.scratch

        # We will set up a virtual environment at root_path.
        self.venv_path = self.root_path / self.venv

        if not environ:
            environ = os.environ.copy()
            environ = clear_environ(environ)
            environ['PIP_DOWNLOAD_CACHE'] = str(download_cache)

        environ['PIP_NO_INPUT'] = '1'
        environ['PIP_LOG_FILE'] = str(self.root_path/'pip-log.txt')

        TestFileEnvironment.__init__(self,
            self.root_path, ignore_hidden=False,
            environ=environ, split_cmd=False, start_clear=False,
            cwd=self.scratch_path, capture_temp=True, assert_no_temp=True)

        virtualenv_paths = virtualenv.path_locations(self.venv_path)

        for id, path in zip(('venv', 'lib', 'include', 'bin'), virtualenv_paths):
            #fix for virtualenv issue #306
            if hasattr(sys, "pypy_version_info") and id == 'lib':
                path = os.path.join(self.venv_path, 'lib-python', pyversion)
            setattr(self, id+'_path', Path(path))
            setattr(self, id, relpath(self.root_path, path))

        assert self.venv == TestPipEnvironment.venv # sanity check

        if hasattr(sys, "pypy_version_info"):
            self.site_packages = self.venv/'site-packages'
        else:
            self.site_packages = self.lib/'site-packages'
        self.user_base_path = self.venv_path/'user'
        self.user_site_path = self.venv_path/'user'/'lib'/self.lib.name/'site-packages'

        self.user_site = relpath(self.root_path, self.user_site_path)

        self.environ["PYTHONUSERBASE"] = self.user_base_path

        # put the test-scratch virtualenv's bin dir first on the PATH
        self.environ['PATH'] = Path.pathsep.join((self.bin_path, self.environ['PATH']))

        if self.root_path.exists:
            rmtree(self.root_path)
        if self.backup_path.exists and not self.rebuild_venv:
            shutil.copytree(self.backup_path, self.root_path, True)
        else:
            demand_dirs(self.venv_path)
            demand_dirs(self.scratch_path)

            # Create a virtualenv and remember where it's putting things.
            create_virtualenv(self.venv_path, distribute=self.use_distribute)

            demand_dirs(self.user_site_path)

            # create easy-install.pth in user_site, so we always have it updated instead of created
            open(self.user_site_path/'easy-install.pth', 'w').close()

            # test that test-scratch virtualenv creation produced sensible venv python
            result = self.run('python', '-c', 'import sys; print(sys.executable)')
            pythonbin = result.stdout.strip()

            if Path(pythonbin).noext != self.bin_path/'python':
                raise RuntimeError(
                    "Oops! 'python' in our test environment runs %r"
                    " rather than expected %r" % (pythonbin, self.bin_path/'python'))

            # make sure we have current setuptools to avoid svn incompatibilities
            if not self.use_distribute:
                install_setuptools(self)

            # Uninstall whatever version of pip came with the virtualenv.
            # Earlier versions of pip were incapable of
            # self-uninstallation on Windows, so we use the one we're testing.
            self.run('python', '-c',
                     '"import sys; sys.path.insert(0, %r); import pip; sys.exit(pip.main());"' % src_folder,
                     'uninstall', '-vvv', '-y', 'pip')

            # Install this version instead
            self.run('python', 'setup.py', 'install', cwd=src_folder, expect_stderr=True)

            # make the backup (remove previous backup if exists)
            if self.backup_path.exists:
                rmtree(self.backup_path)
            shutil.copytree(self.root_path, self.backup_path, True)

        #create sitecustomize.py and add patches
        self._create_empty_sitecustomize()
        self._use_cached_pypi_server()
        if sitecustomize:
            self._add_to_sitecustomize(sitecustomize)

        assert self.root_path.exists

        # Ensure that $TMPDIR exists (because we use start_clear=False, it's not created for us)
        if self.temp_path and not os.path.exists(self.temp_path):
            os.makedirs(self.temp_path)


    def _ignore_file(self, fn):
        if fn.endswith('__pycache__') or fn.endswith(".pyc"):
            result = True
        else:
            result = super(TestPipEnvironment, self)._ignore_file(fn)
        return result

    def run(self, *args, **kw):
        if self.verbose:
            print('>> running %s %s' % (args, kw))
        cwd = kw.pop('cwd', None)
        run_from = kw.pop('run_from', None)
        assert not cwd or not run_from, "Don't use run_from; it's going away"
        cwd = Path.string(cwd or run_from or self.cwd)
        assert not isinstance(cwd, Path)
        return TestPipResult(super(TestPipEnvironment, self).run(cwd=cwd, *args, **kw), verbose=self.verbose)

    def _use_cached_pypi_server(self):
        # previously, this was handled in a pth file, and not in sitecustomize.py
        # pth processing happens during the construction of sys.path.
        # 'import pypi_server' ultimately imports pkg_resources (which intializes pkg_resources.working_set based on the current state of sys.path)
        # pkg_resources.get_distribution (used in pip.req) requires an accurate pkg_resources.working_set
        # therefore, 'import pypi_server' shouldn't occur in a pth file.

        patch = """
            import sys
            sys.path.insert(0, %r)
            import pypi_server
            pypi_server.PyPIProxy.setup()
            sys.path.remove(%r)""" % (str(tests_lib), str(tests_lib))
        self._add_to_sitecustomize(patch)

    def _create_empty_sitecustomize(self):
        "Create empty sitecustomize.py."
        sitecustomize_path = self.lib_path / 'sitecustomize.py'
        sitecustomize = open(sitecustomize_path, 'w')
        sitecustomize.close()

    def _add_to_sitecustomize(self, snippet):
        "Adds a python code snippet to sitecustomize.py."
        sitecustomize_path = self.lib_path / 'sitecustomize.py'
        sitecustomize = open(sitecustomize_path, 'a')
        sitecustomize.write(textwrap.dedent('''
                               %s
        ''' %snippet))
        sitecustomize.close()
        # caught py32 with an outdated __pycache__ file after a sitecustomize update (after python should have updated it)
        # https://github.com/pypa/pip/pull/893#issuecomment-16426701
        # will delete the cache file to be sure
        if uses_pycache:
            cache_path = imp.cache_from_source(sitecustomize_path)
            if os.path.isfile(cache_path):
                os.remove(cache_path)



class TestPipEnvironmentD(TestPipEnvironment):
    """A specialized TestFileEnvironment that contains distribute"""

    use_distribute = True
    # Keep short to undercut windows path length issues
    setuptools = 'd'
    rebuild_venv = True


def run_pip(*args, **kw):
    result = env.run('pip', *args, **kw)
    ignore = []
    for path, f in result.files_before.items():
        # ignore updated directories, often due to .pyc or __pycache__
        if (path in result.files_updated and
            isinstance(result.files_updated[path], FoundDir)):
            ignore.append(path)
    for path in ignore:
        del result.files_updated[path]
    return result

def pip_install_local(*args, **kw):
    """Run 'pip install' using --find-links against our local test packages"""
    run_pip('install', '--no-index', '--find-links=%s' % find_links, *args, **kw)

def write_file(filename, text, dest=None):
    """Write a file in the dest (default=env.scratch_path)

    """
    env = get_env()
    if dest:
        complete_path = dest/ filename
    else:
        complete_path = env.scratch_path/ filename
    f = open(complete_path, 'w')
    f.write(text)
    f.close()


def mkdir(dirname):
    os.mkdir(os.path.join(get_env().scratch_path, dirname))


def get_env():
    if env is None:
        reset_env()
    return env


# FIXME ScriptTest does something similar, but only within a single
# ProcResult; this generalizes it so states can be compared across
# multiple commands.  Maybe should be rolled into ScriptTest?
def diff_states(start, end, ignore=None):
    """
    Differences two "filesystem states" as represented by dictionaries
    of FoundFile and FoundDir objects.

    Returns a dictionary with following keys:

    ``deleted``
        Dictionary of files/directories found only in the start state.

    ``created``
        Dictionary of files/directories found only in the end state.

    ``updated``
        Dictionary of files whose size has changed (FIXME not entirely
        reliable, but comparing contents is not possible because
        FoundFile.bytes is lazy, and comparing mtime doesn't help if
        we want to know if a file has been returned to its earlier
        state).

    Ignores mtime and other file attributes; only presence/absence and
    size are considered.

    """
    ignore = ignore or []

    def prefix_match(path, prefix):
        if path == prefix:
            return True
        prefix = prefix.rstrip(os.path.sep) + os.path.sep
        return path.startswith(prefix)

    start_keys = set([k for k in start.keys()
                      if not any([prefix_match(k, i) for i in ignore])])
    end_keys = set([k for k in end.keys()
                    if not any([prefix_match(k, i) for i in ignore])])
    deleted = dict([(k, start[k]) for k in start_keys.difference(end_keys)])
    created = dict([(k, end[k]) for k in end_keys.difference(start_keys)])
    updated = {}
    for k in start_keys.intersection(end_keys):
        if (start[k].size != end[k].size):
            updated[k] = end[k]
    return dict(deleted=deleted, created=created, updated=updated)


def assert_all_changes(start_state, end_state, expected_changes):
    """
    Fails if anything changed that isn't listed in the
    expected_changes.

    start_state is either a dict mapping paths to
    scripttest.[FoundFile|FoundDir] objects or a TestPipResult whose
    files_before we'll test.  end_state is either a similar dict or a
    TestPipResult whose files_after we'll test.

    Note: listing a directory means anything below
    that directory can be expected to have changed.
    """
    start_files = start_state
    end_files = end_state
    if isinstance(start_state, TestPipResult):
        start_files = start_state.files_before
    if isinstance(end_state, TestPipResult):
        end_files = end_state.files_after

    diff = diff_states(start_files, end_files, ignore=expected_changes)
    if list(diff.values()) != [{}, {}, {}]:
        raise TestFailure('Unexpected changes:\n' + '\n'.join(
            [k + ': ' + ', '.join(v.keys()) for k, v in diff.items()]))

    # Don't throw away this potentially useful information
    return diff


def _create_test_package(env):
    mkdir('version_pkg')
    version_pkg_path = env.scratch_path/'version_pkg'
    write_file('version_pkg.py', textwrap.dedent('''\
                                def main():
                                    print('0.1')
                                '''), version_pkg_path)
    write_file('setup.py', textwrap.dedent('''\
                        from setuptools import setup, find_packages
                        setup(name='version_pkg',
                              version='0.1',
                              packages=find_packages(),
                              py_modules=['version_pkg'],
                              entry_points=dict(console_scripts=['version_pkg=version_pkg:main']))
                        '''), version_pkg_path)
    env.run('git', 'init', cwd=version_pkg_path)
    env.run('git', 'add', '.', cwd=version_pkg_path)
    env.run('git', 'commit', '-q',
            '--author', 'Pip <python-virtualenv@googlegroups.com>',
            '-am', 'initial version', cwd=version_pkg_path)
    return version_pkg_path


def _change_test_package_version(env, version_pkg_path):
    write_file('version_pkg.py', textwrap.dedent('''\
        def main():
            print("some different version")'''), version_pkg_path)
    env.run('git', 'clean', '-qfdx', cwd=version_pkg_path, expect_stderr=True)
    env.run('git', 'commit', '-q',
            '--author', 'Pip <python-virtualenv@googlegroups.com>',
            '-am', 'messed version',
            cwd=version_pkg_path, expect_stderr=True)


def assert_raises_regexp(exception, reg, run, *args, **kwargs):
    """Like assertRaisesRegexp in unittest"""
    try:
        run(*args, **kwargs)
        assert False, "%s should have been thrown" %exception
    except Exception:
        e = sys.exc_info()[1]
        p = re.compile(reg)
        assert p.search(str(e)), str(e)


if __name__ == '__main__':
    sys.stderr.write("Run pip's tests using nosetests. Requires virtualenv, ScriptTest, mock, and nose.\n")
    sys.exit(1)
