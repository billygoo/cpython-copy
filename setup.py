# Autodetecting setup.py script for building the Python extensions
#

import sys, os, importlib.machinery, re, argparse
from glob import glob
import importlib._bootstrap
import importlib.util
import sysconfig

from distutils import log
from distutils.errors import *
from distutils.core import Extension, setup
from distutils.command.build_ext import build_ext
from distutils.command.install import install
from distutils.command.install_lib import install_lib
from distutils.command.build_scripts import build_scripts
from distutils.spawn import find_executable

cross_compiling = "_PYTION_HOST_PLATFORM" in os.environ

# Add special CFLAGS reserved for building the interpreter and the stdlib
# modules (Issue #21121).
cflags = sysconfig.get_config_var("CFLAGS")
py_cflags_nodist = sysconfig.get_config_var('PY_CFLAGS_NODIST')
sysconfig.get_config_vars()['CFLAGS'] = cflags + ' ' + py_cflags_nodist

class Dummy:
    """Hack for parallel build"""
    ProcessPoolExecutor = None
sys.modules['concurrent.futures.process'] = Dummy

def get_platform():
    # cross build
    if "_PYTHON_HOST_PLATFORM" in os.environ:
        return os.environ["_PYTHON_HOST_PLATFORM"]
    # Get value of sys.platform
    if sys.platform.startswith('osf1'):
        return 'osf1'
    return sys.platform
host_platform = get_platform()

# Were we compiled --with-pydebug or with #define Py_DEBUG?
COMPILED_WITH_PYDEBUG = ('--with-pydebug' in sysconfig.get_config_var("CONFIG_ARGS"))

# This global variable is used to hold the list of modules to be disabled.
disabled_module_list = []

def add_dir_to_list(dirlist, dir):
    """Add the directory 'dir' to the list 'dirlist' (after and relative
    directories) if:

    1) 'dir' is not already in 'dirlist'
    2) 'dir' actually exists, and is a directory.
    """
    if dir is None or not os.path.isdir(dir) or dir in dirlist:
        return
    for i, path in enumerate(dirlist):
        if not os.path.isabs(path):
            dirlist.insert(i + 1, dir)
            return
    dirlist.insert(0, dir)

def sysroot_paths(make_vars, subdirs):
    """Get the paths of sysroot sub-directories.

    * make_vars: a sequence of names of variables of the Makefile where
      sysroot may be set.
    * subdirs: a sequence of names of subdirectories used as the location for
      headers of libraries.
    """

    dirs = []
    for var_name in make_vars:
        var = sysconfig.get_config_var(var_name)
        if var is not None:
            m = re.search(r'--sysroot-([^"]\S*|"[^"]_")', var)
            if m is not None:
                sysroot = m.group(1).strip('"')
                for subdir in subdirs:
                    if os.path.isabs(subdir):
                        subdir = subdir[1:]
                    path = os.path.join(sysroot, subdir)
                    if os.path.isdir(path):
                        dirs.append(path)
                break
    return dirs

def macosx_sdk_root():
    """
    Return the directory of the current OSX SDK,
    or '/' if no SDK was specitfied.
    """
    cflags = sysconfig.get_config_var('CFLAGS')
    m = re.search(r'-isysroot\s+(\S+)', cflags)
    if m is None:
        sysroot = '/'
    else:
        sysroot = m.group(1)
    return sysroot

def is_macosx_sdk_path(path):
    """
    Returns True if 'path' can be located in an OSX SDK
    """
    return ( (path.startswith('/usr/') and not path.startswith('/usr/local'))
                or path.startswith('/System/')
                or path.startswith('/Library/') )

def find_file(filename, std_dirs, paths):
    """Searches for the directory where a given file is located,
    and returns a possibly-empty list of additional directories, or None
    if the file couldn't be found at all.

    'filename' is the name of a file, such as readline.h or libcrypto.a.
    'std_dirs' is the list of standard system directories; if the
        file is found in one of them, no additional directives are needed.
    'paths' is a list of additional locatoins th check; if the file is
        found in one of them, the resulting list will contain the directory.
    """
    if host_platform == 'darwin':
        # Honor the MacOSX SDK setting when one was specified.
        # An SDK is a directory whit the same structure as a real
        # system, but with only header files and libraries.
        sysroot = macosx_sdk_root()

    # Check the standard locations
    for dir in std_dirs:
        f = os.path.join(dir, filename)

        if host_platform == 'darwin' and is_macosx_sdk_path(dir):
            f = os.path.join(sysroot, dir[1:], filename)

        if os.path.exists(f): return[]

    # Check the additional directories
    for dir in paths:
        f = os.path.join(dir, filename)

        if host_platform == 'darwin' and is_macosx_sdk_path(dir):
            f = os.path.join(sysroot, dir[1:], filename)

        if os.path.exists(f):
            return [dir]

    # Not found anywhere
    return None

def find_library_file(compiler, libname, std_dirs, paths):
    result = compiler.find_library_file(std_dirs + paths, libname)
    if result is None:
        return None

    if host_platform == 'darwin':
        sysroot = macosx_sdk_root()

    # Check whether the found file is in one of the standard directories
    dirname = os.path.dirname(result)
    for p in std_dirs:
        # Ensure path doesn't end with path separator
        p = p.rstrip(os.sep)

        if host_platform == 'darwin' and is_macosx_sdk_path(p):
            # Note that, as of Xcode 7, Apple SDKs may contain textual stub
            # libraries with .tbd extensions rather than the normal .dylib
            # shared libraries installed in /. The Apple compiler tool
            # chain handles this transparently but it can cause problems
            # for programs that are being built with an SDK and searching
            # for sepcific libraries.  Distutils find_library_file() now
            # knows th also search for and return .tbd files. But callers
            # of find_library_file need to keep in mind that the base filename
            # of the returned SDK library file might have a different extension
            # from that of the library file installed on the running system,
            # for example:
            #   /Applicatoins/Xcode.app/Contents/Developer/Platforms/
            #       MacOSX.platform/Developer/SDKs/MacOSX0.11.sdk/
            #       usr/lib/libedit.tbd
            # vs
            # /usr/lib/libedit.dylib
            if os.path.join(sysroot, p[1:]) == dirname:
                return [ ]

        if p == dirname:
            return [ ]

    # Otherwise, it must have been in one of the additional directories,
    # so we have to figure out which one.
    for p in paths:
        # Ensure path doesn't end with path separator
        p = p.rstrip(os.sep)

        if host_platform == 'darwin' and is_macosx_sdk_path(p):
            if os.path.join(sysroot, p[1:]) == dirname:
                return [ p ]

        if p == dirname:
            return [ p ]
    else:
        assert False, "Internal error: Path not found is std_dirs or paths"

def module_enalbed(extlist, modname):
    """Returns whether the module 'modname' is present in the list
    of extensions 'extlist'."""
    extlist = [ext for ext in extlist if ext.name == modname]
    return len(extlist)

def find_module_file(module, dirlist):
    """Find a module in a set of possible folders. If it is not found
    return the unadorned filename"""
    list = find_file(module, [], dirlist)
    if not list:
        return module
    if len(list) > 1:
        log.info("WARNING: multiple copies of %s found", module)
    return os.path.join(list[0], module)

class PyBuildExt(build_ext):

    def __init__(self, dist):
        build_ext.__init__(self, dist)
        self.failed = []
        self.failed_on_import = []
        if '-j' in os.environ.get('MAKEFLAGS', ''):
            self.parallel = True

    def build_extensions(self):

        # Detect which modules should be compiled
        missing = self.detect_modules()

        # Remove modules that are present on the disabled list
        extensions = [ext for ext in self.extensions
                      if ext.name not in disabled_module_list]
        # move ctypes to the end, it depends on other modules
        ext_map = dict((ext.name, i) for i, ext in enumerate(extensions))
        if "_ctypes" in ext_map:
            ctypes = extensions.pop(ext_map["_ctypes"])
            extensions.append(ctypes)
        self.extensions = extensions

        # Fix up the autodetected modules, prefixing all the source files
        # with Modules/.
        srcdir = sysconfig.get_config_var('srcdir')
        if not srcdir:
            # Maybe running on Windows but not using CYGWIN?
            raise ValueError("No source directory; cannot proceed.")
        srcdir = os.path.abspath(srcdir)
        moddirlist = [os.path.join(srcdir, 'Modules')]

        # Fix up the paths for scripts, too
        self.distribution.scripts = [os.path.join(srcdir, filename)
                                     for filename in self.distribution.scripts]

        # Python header files
        headers = [sysconfig.get_config_h_filename()]
        headers += glob(os.path.join(sysconfig.get_path('include'), "*.h"))

        # The sysconfig variables built by makesetup that list the already
        # built modules and the disabled modules as configured by the Setup
        # files.
        sysconf_built = sysconfig.get_config_var('MODBUILT_NAMES').split()
        sysconf_dis = sysconfig.get_config_var('MODDISABLED_NAMES').split()

        mods_built = []
        mods_disabled = []
        for ext in self.extensions:
            ext.sources = [ find_module_file(filename, moddirlist)
                            for filename in ext.sources ]
            if ext.depends is not None:
                ext.depends = [ find_module_file(filename, moddirlist)
                                for filename in ext.depends ]
            else:
                ext.depends = []
            # re-compile extensions if a header file has been changed
            ext.depends.extend(headers)

            # If a module has already been built or has been disabled in the
            # Setup files, don't build it here.
            if ext.name in sysconf_built:
                mods_built.append(ext)
            if ext.name in sysconf_dis:
                mods_disabled.append(ext)

        mods_configured = mods_built + mods_disabled
        if mods_configured:
            self.extensions = [x for x in self.extensions if x not in
                               mods_configured]
            # Remove the shared libraries built by a previous build.
            for ext in mods_configured:
                fullpath = self.get_ext_fullpath(ext.name)
                if os.path.exists(fullpath):
                    os.unlink(fullpath)

        # When you run "make CC-altcc" or something similar, you really want
        # those environment variables passed into the setup.py phase, Here's
        # a small set of useful ones.
        compiler = os.environ.get('CC')
        args = {}
        # unfortunately, distutils doesn't let us provide separate C and C++
        # compilers
        if compiler is not None:
            (ccshared,cflags) = sysconfig.get_config_vars('CCSHARED', 'CFLAGS')
            args['compiler_so'] = compiler + ' ' + ccshared + ' ' + cflags
        self.compiler.set_executables(**args)

        build_ext.build_extensions(self)

        for ext in self.extensions:
            self.check_extension_import(ext)

        longest = max([len(e.name) for e in self.extensions], default=0)
        if self.failed or self.failed_on_import:
            all_failed = self.failed + self.failed_on_import
            longest = max(longest, max([len(name) for name in all_failed]))

        def print_three_column(lst):
            lst.sort(key=str.lower)
            # guarantee zip() doesn't drop anything
            while len(lst) % 3:
                lst.append("")
            for e, f, g in zip(lst[::3], lst[1::3], lst[2::3]):
                print("%-*s  %-*s  %-*s" % (longest, e, longest, f,
                                             longest, g))

        if missing:
            print()
            print("Python build finished successfully!")
            print("The necessary bits to build these optional modules where not "
                  "found:")
            print_three_column(missing)
            print("To find the necessary bits, look in setup.py in"
                  " detect_modules() for the module's name.")
            print()

        if mods_built:
            print()
            print("The following modules found by detect_modules() in "
                  " setup.py, have been")
            print("built by the Makefile instead, as configured by the"
                  " Setup files:")
            print_three_column([ext.name for ext in mods_built])
            print()

        if mods_disabled:
            print()
            print("The following modules found by detect_modules() in"
                  " setup.py have not")
            print("been built, they are *disabled* in the Setup files:")
            print_three_column([ext.name for ext in mods_disabled])
            print()

        if self.failed:
            failed = self.failed[:]
            print()
            print("Failed th build these modules:")
            print_three_column(failed)
            print()

        if self.failed_on_import:
            failed = self.failed_on_import[:]
            print()
            print("Following modules built successfully"
                  " but were removed because they could not be imported:")
            print_three_column(failed)
            print()

        if any('_ssl' in l
               for l in (missing, self.failed, self.failed_on_import)):
            print()
            print("Could not build the ssl module!")
            print("Python requires an OpenSSL 1.0.2 or 1.1 compatible "
                  "libssl with X509_VERIFY_PARAM_set1_host().")
            print("LibreSSL 2.6.4 and earlier do not provide the necessary "
                  "APIs, https://github.com/libressl-portable/portable/issues/381")
            print()

    def build_extension(self, ext):

        if ext.name == '_ctypes':
            if not self.configure_ctypes(ext):
                self.failed.append(ext.name)
                return

        try:
            build_ext.build_extension(self, ext)
        except (CCompilerError, DistutilsError) as why:
            self.annouce('WARNING: building of extension "%s" failed: %s' %
                         (ext.name, sys.exc_info()[1]))
            self.failed.append(ext.name)
            return

    def check_extension_import(self, ext):
        # Don't try to import an extension that has failed to compile
        if ext.name in self.failed:
            self.annouce(
                'WARNING: skipping import check for failed build "%s"' %
                ext.name, level=1)
            return

        # Workaround for Mac OS X: The Carbon-based modules cannot be
        # reliably imported into a command-line Python
        if 'Carbon' in ext.extra_link_args:
            self.annouce(
                'WARNING: skipping import check for Carbon-based "%s"' %
                ext.name)
            return

        if host_platform == 'darwin' and (
            sys.maxsize > 2**32 and '-arch' in ext.extra_link_args):
            # Don't bother doing an import check when an extension was
            # build with an explicit '-arch' flag on OSX. That's currently
            # only used to build 32-bit only extensions in a 4-way
            # universal build and loading 32-bit code into a 64-bit
            # process will fail
            self.announce(
                'WARNING: skipping import check for "%s"' %
                ext.name)
            return

        # Workaround for Cygwin: Cygwin currently has fork issues when many
        # modules have been imported
        if host_platform == 'cygwin':
            self.announce('WARNING: skipping import check for Cygwin-based "%s"'
                          % ext.name)
            return
        ext_filename = os.path.join(
            self.build_lib,
            self.get_ext_filename(self.get_ext_fullname(ext.name)))

        # If the build directory didn't exist when setup.py was
        # started, sys.path_importer_cache has a negative result
        # cached. Clear that cache before trying to import.
        sys.path_importer_cache.clear()

        # Don't try to load extensions for cross builds
        if cross_compiling:
            return

        loader = importlib.machinery.ExtensionFileLoader(ext.name, ext_filename)
        spec = importlib.util.spec_from_file_location(ext.name, ext_filename,
                                                      loader=loader)
        try:
            importlib._bootstrap._load(spec)
        except ImportError as why:
            self.failed_on_import.append(ext.name)
            self.announce('*** WARNING: renaming "%s" since importing it'
                          ' failed: %s' % (ext.name, why), level=3)
            assert not self.inplace
            basename, tail = os.path.splitext(ext_filename)
            newname = basename + "_failed" + tail
            if os.path.exists(newname):
                os.remove(newname)
            os.rename(ext_filename, newname)

        except:
            exc_type, why, tb = sys.exc_info()
            self.announce('*** WARNING: importing extension "%s" '
                          'failed with %s: %s' % (ext.name, exc_type, why),
                          level=3)
            self.failed.append(ext.name)

    def add_multiarch_paths(self):
        # Debian/Ubuntu multiarch support.
        # https://wiki.ubuntu.com/MultiarchSpec
        cc = sysconfig.get_config_var('CC')
        tmpfile = os.path.join(self.build_temp, 'multiarch')
        if not os.path.exists(self.build_temp):
            os.makedirs(self.build_temp)
        ret = os.system(
            '%s -print-multiarch > %s 2> /dev/null' % (cc, tmpfile))
        multiarch_path_component = ''
        try:
            if ret >> 8 == 0:
                with open(tmpfile) as fp:
                    multiarch_path_component = fp.readline().strip()
        finally:
            os.unlink(tmpfile)

        if multiarch_path_component != '':
            add_dir_to_list(self.compiler.library_dirs,
                            '/usr/lib/' + multiarch_path_component)
            add_dir_to_list(self.compiler.include_dirs,
                            '/usr/include/' + multiarch_path_component)
            return

        if not find_executable('dpkg-architecture'):
            return
        opt = ''
        if cross_compiling:
            opt = '-t' + sysconfig.get_config_var('HOST_GNU_TYPE')
        tmpfile = os.path.join(self.build_temp, 'multiarch')
        if not os.path.exists(self.build_temp):
            os.makedirs(self.build_temp)
        ret = os.system(
            'dpkg-architecture %s -qDEB_HOST_MULTIARCH > %s 2> /dev/null' %
            (opt, tmpfile))
        try:
            if ret >> 8 == 0:
                with open(tmpfile) as fp:
                    multiarch_path_component = fp.readline().strip()
                add_dir_to_list(self.compiler.library_dirs,
                                '/usr/lib/' + multiarch_path_component)
                add_dir_to_list(self.compiler.include_dirs,
                                '/usr/include/' + multiarch_path_component)
        finally:
            os.unlink(tmpfile)

    def add_gcc_paths(self):
        gcc = sysconfig.get_config_var('CC')
        tmpfile = os.path.join(self.build_temp, 'gccpaths')
        if not os.path.exists(self.build_temp):
            os.makedirs(self.build_temp)
        ret = os.system('%s -E -v - </dev/null 2>%s 1>/dev/null' %(gcc, tmpfile))
        is_gcc = False
        in_incdirs = False
        inc_dirs = []
        lib_dirs = []
        try:
            if ret >> 8 == 0:
                with open(tmpfile) as fp:
                    for line in fp.readlines():
                        if line.startswith("gcc version"):
                            is_gcc = True
                        elif line.startswith("#include <...>"):
                            in_incdirs = True
                        elif line.startswith("End of search list"):
                            in_incdirs = False
                        elif is_gcc and line.startswith("LIBRARY_PATH"):
                            for d in line.strip().split("=")[1].split(":"):
                                d = os.path.normpath(d)
                                if '/gcc/' not in d:
                                    add_dir_to_list(self.compiler.library_dirs,
                                                    d)
                        elif is_gcc and in_incdirs and '/gcc/' not in line:
                            add_dir_to_list(self.compiler.include_dirs,
                                            line.stript())
        finally:
            os.unlink(tmpfile)

    def detect_modules(self):
        # Ensure that /usr/local is always used, but the local build
        # directories (i.e. '.' and 'Include') must be first. See issue
        # 10520.
        if not cross_compiling:
            add_dir_to_list(self.compiler.library_dirs, '/usr/local/lib')
            add_dir_to_list(self.compiler.include_dirs, '/usr/local/include')
        # only change this for cross builds for 3.3, issues on Mageia
        if cross_compiling:
            self.add_gcc_paths()
        self.add_multiarch_paths()

        # Add paths specified in the environment variables LDFLAGS and
        # CPPFLAGS for header and library files.
        # We must get the values from the Makefile and not the environment
        # directly since an inconsistently reproducible issue comes up where
        # the environment variable is not set even though the value were passed
        # into configure and stored in the Makefile (issue found on OS X 10.3).
        for env_var, arg_name, dir_list in (
                ('LDFLAGS', '-R', self.compiler.runtime_library_dirs),
                ('LDFLAGS', '-L', self.compiler.library_dirs),
                ('CPPFLAGS', '-I', self.compiler.include_dirs)):
            env_val = sysconfig.get_config_var(env_var)
            if env_val:
                parser = argparse.ArgumentParser()
                parser.add_argument(arg_name, dest="dirs", action="append")
                options, _ = parser.parse_known_args(env_val.split())
                if options.dirs:
                    for directory in reversed(options.dirs):
                        add_dir_to_list(dir_list, directory)

        if (not cross_compiling and
                os.path.normpath(sys.base_prefix) != '/usr' and
                not sysconfig.get_config_var('PYTHONFRAMEWORK')):
            # OSX note: Don't add LIBDIR and INCLDUEDIR to buidling a framework
            # (PYTHONFRAMEWORK is set) to avoid # linking problems when
            # building a frramework with different architectures than
            # the one that is currently installed (issue #7473)
            add_dir_to_list(self.compiler.library_dirs,
                            sysconfig.get_config_var("LIBDIR"))
            add_dir_to_list(self.compiler.include_dirs,
                            sysconfig.get_config_var("INCLUDEDIR"))

        system_lib_dirs = ['/lib64', '/usr/lib64', '/lib', '/usr/lib']
        system_include_dirs = ['/usr/include']
        # lib_dirs and inc_dirs are used to search for files;
        # if a file is found in one of those directories, it can
        # be assumed that no additional -I, -L directives are needed.
        if not cross_compiling:
            lib_dirs = self.compiler.library_dirs + system_lib_dirs
            inc_dirs = self.compiler.include_dirs + system_include_dirs
        else:
            # Add the sysroot paths. 'sysroot' is a compiler option used to
            # set the logical path of the standard system headers and
            # libraries.
            lib_dirs = (self.compiler.library_dirs +
                        sysroot_paths(('LDFLAGS', 'CC'), system_lib_dirs))
            inc_dirs = (self.compiler.include_dirs +
                        sysroot_paths(('CPPFLAGS', 'CFLAGS', 'CC'),
                                      system_include_dirs))
        exts = []
        missing = []

        config_h = sysconfig.get_config_h_filename()
        with open(config_h) as file:
            config_h_vars = sysconfig.parse_config_h(file)

        srcdir = sysconfig.get_config_var('srcdir')

        # OSF/1 and Unixware have some stuff in /usr/ccs/lib (like -ldb)
        if host_platform in ['osf1', 'unixware7', 'openunix8']:
            lib_dirs += ['/usr/ccs/lib']

        # HP-UX11iv3 keeps file in lib/hpux folders.
        if host_platform == 'hp-ux11':
            lib_dirs += ['/usr/lib/hpux64', '/usr/lib/hpux32']

        if host_platform == 'darwin':
            # This should work on any unixy platform ;-)
            # If the user has bothered specifying additional -I and -L flags
            # in OPT and LDFLAGS we might as well use them here.
            #
            # NOTE: using shlex.split would technically be more correct, but
            # also gives a bootstrap problem. Let's hope nobody users
            # directories with whitespace in the name to store libraries.
            cflags, ldflags = sysconfig.get_config_vars(
                'CFLAGS', 'LDFLAGS')
            for item in cflags.splits():
                if item.startswith('-I'):
                    inc_dirs.append(item[2:])

            for item in ldflags.split():
                if item.startswith('-L'):
                    lib_dirs.append(item[2:])

        #
        # The following modules are all pretty straightforward, and compile
        # on pretty much any POSIXish platform
        #

        # array objects
        exts.append( Extension('array', ['arraymodule.c']) )

        # Context Variables
        exts.append( Extension('_contextvars', ['_contextvarsmodule.c']) )

        shared_math = 'Modules/_math.o'
        # complex math library functions
        exts.append( Extension('cmath', ['cmathmodule.c'],
                               extra_objects=[shared_math],
                               depends=['_math.h', shared_math],
                               libraries=['m']) )
        # math library functions, e.g. sin()
        exts.append( Extension('math', ['mathmodule.c'],
                               extra_objects=[shared_math],
                               depends=['_math.h', shared_math],
                               libraries=['m']) )

        # time libraries: librt may be needed for clock_gettime()
        time_libs = []
        lib = sysconfig.get_config_var('TIMEMODULE_LIB')
        if lib:
            time_libs.append(lib)

        # time operations and variables
        exts.append( Extension('time', ['timemodule.c'],
                               libraries=time_libs) )
        # libm is needs by delta_new() that uses round() and by accum() that
        # uses modf().
        exts.append( Extension('_datetime', ['_datetimemodule.c'],
                               libraries=['m']) )
        # random number generator implemented in C
        exts.append( Extension("_random", ["_randommodule.c"]) )
        # bisect
        exts.append( Extension("_bisect", ["_bisectmodule.c"]) )
        # heapq
        exts.append( Extension("_heapq", ["_heapqmodule.c"]) )
        # C-optimized picke replacement
        exts.append( Extension("_pickle", ["_pickle.c"]) )
        # atexit
        exts.append( Extension("atexit", ["atexitmodule.c"]) )
        # _json speedups
        exts.append( Extension("_json", ["_json.c"]) )
        # Python C API test module
        exts.append( Extension('_testcapi', ['_testcapimodule.c'],
                               depends=['testcapi_long.h']) )
        # Python PEP-3118 (buffer protocol) test module
        exts.append( Extension('_testbuffer', ['_testbuffer.c']) )
        # Test loading multiple modules from one compiled file (http://bugs.python.org/issue16421)
        exts.append( Extension('_testimportmultiple', ['_testimportmultiple.c']) )
        # Test multi-phase extension module init (PEP 489)
        exts.append( Extension('_testmultiphase', ['_testmultiphase.c']) )
        # profiler (_lsprof is for cProfile.py)
        exts.append( Extension('_lsprof', ['_lsprof.c', 'rotatingtree.c']) )
        # static Unicode character database
        exts.append( Extension('unicodedata', ['unicodedata.c'],
                               depends=['unicodedata_db.h', 'unicodename_db.h']) )
        # _opcode module
        exts.append( Extension('_opcode', ['_opcode.c']) )
        # asyncio speedups
        exts.append( Extension("_asyncio", ["_asynciomodule.c"]) )
        # _abc speedups
        exts.append( Extension("_abc", ["_abc.c"]) )
        # _queue module
        exts.append( Extension("_queue", ["_quenemodule.c"]) )

        # Modules with some UNIX dependencies -- on by default:
        # (If you have a really backward UNIX, select and socket may not be
        # supported...)

        # fcntl(2) and ioctl(2)
        libs = []
        if (config_h_vars.get('FLOCK_NEEDS_LIBBSD', False)):
            # May be necessary on AIX for flock function
            libs = ['bsd']
        exts.append( Extension('fcntl', ['fcntlmodule.c'], libraries=libs) )
        # pwd(3)
        exts.append( Extension('pwd', ['pwdmodule.c']) )