import re, argparse, sys, os, string, shlex, subprocess, glob, parser
from collections import OrderedDict, namedtuple
import curses.ascii

def indentify(s, indent='    '):
    return s.replace('\n', '\n' + indent)

def log(x):
    sys.stdout.write(x)
    config_log.write(x)

def argv_to_shell(argv):
    quoteds = []
    for arg in argv:
        if re.match('^[a-zA-Z0-9_\.@/+=-]+$', arg):
            quoteds.append(arg)
        else:
            quoted = ''
            for c in arg:
                if c == '\n':
                    quoted += r'\n'
                elif c in r'$`\"':
                    quoted += '\\' + c
                elif not curses.ascii.isprint(c):
                    quoted += r'\x%02x' % ord(c)
                else:
                    quoted += c
            quoteds.append('"' + quoted + '"')
    return ' '.join(quoteds)


def init_config_log():
    global config_log
    config_log = open('config.log', 'w')
    config_log.write(argv_to_shell(sys.argv) + '\n')

# a wrapper for subprocess that logs results
# returns (stdout, stderr, status) [even if Popen fails]
def run_command(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs):
    config_log.write('Running command %s...\n' % (argv_to_shell(cmd),))
    try:
        p = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, **kwargs)
    except OSError:
        config_log.write('  OSError\n')
        return '', '', 127
    so, se = p.communicate()
    if p.returncode != 0:
        config_log.write('  failed with status %d\n' % (p.returncode,))
    config_log.write('-----------\n')
    config_log.write('  stdout:\n')
    config_log.write(so.rstrip())
    config_log.write('\n  stderr:\n')
    config_log.write(se.rstrip())
    return so, se, p.returncode

class DependencyNotFoundException(Exception):
    pass

# it must take no arguments, and throw DependencyNotFoundException on failure
class memoize(object):
    def __init__(self, f):
        self.f = f
    def __call__(self):
        if hasattr(self, 'threw'):
            raise self.threw
        elif hasattr(self, 'result'):
            return self.result
        else:
            try:
                self.result = self.f()
                return self.result
            except DependencyNotFoundException as self.threw:
                raise

class Pending(object):
    def __repr__(self):
        return 'Pending(%x%s)' % (id(self), ('; value=%r' % (self.value,)) if hasattr(self, 'value') else '')
    def resolve(self):
        return self.value
    # xxx py3
    def __getattr__(self, attr):
        if attr is 'value':
            raise AttributeError
        return PendingAttribute(self, attr)

class PendingOption(Pending, namedtuple('PendingOption', 'opt')):
    def resolve(self):
        return self.opt.value
    def __repr__(self):
        return 'PendingOption(%s)' % (self.opt.name,)

class PendingAttribute(Pending, namedtuple('PendingAttribute', 'base attr')):
    def resolve(self):
        return getattr(self.base, self.attr)
    def __repr__(self):
        return 'PendingAttribute(attr=%s, base=%r)' % (self.attr, self.base)

class SettingsGroup(object):
    def __init__(self, group_parent=None, inherit_parent=None, name=None):
        object.__setattr__(self, 'group_parent', group_parent)
        object.__setattr__(self, 'inherit_parent', inherit_parent)
        object.__setattr__(self, 'vals', OrderedDict())
        if name is None:
            name = '<0x%x>' % (id(self),)
        object.__setattr__(self, 'name', name)
    @staticmethod
    def get_meat(self, attr, exctype=KeyError):
        allow_pending = not did_parse_args
        try:
            obj = object.__getattribute__(self, 'vals')[attr]
        except KeyError:
            inherit_parent = object.__getattribute__(self, 'inherit_parent')
            if inherit_parent is not None:
                ret = SettingsGroup.get_meat(inherit_parent, attr, exctype)
                if isinstance(ret, SettingsGroup):
                    ret = self[attr] = ret.new_inheritor(name='%s.%s' % (object.__getattribute__(self, 'name'), attr))
                return ret
            raise exctype(attr)
        else:
            if isinstance(obj, Pending):
                try:
                    return obj.resolve()
                except:
                    if not allow_pending:
                        raise Exception("setting %r is pending; you need to set it" % (attr,))
                    return obj
            return obj
    def __getattribute__(self, attr):
        try:
            return object.__getattribute__(self, attr)
        except AttributeError:
            return SettingsGroup.get_meat(self, attr, AttributeError)
    def __setattr__(self, attr, val):
        try:
            object.__getattribute__(self, attr)
        except:
            self[attr] = val
        else:
            object.__setattribute__(self, attr, val)
    def __getitem__(self, attr):
        return self.__getattribute__(attr)
    def __setitem__(self, attr, val):
        self.vals[attr] = val

    def __iter__(self):
        return self.vals.__iter__()
    def items(self):
        return self.vals.items()

    def __str__(self):
        s = 'SettingsGroup %s {\n' % (self.name,)
        o = self
        while True:
            for attr, val in o.vals.items():
                s += '    %s: %s\n' % (attr, indentify(str(val)))
            if o.inherit_parent is None:
                break
            o = o.inherit_parent
            s += '  [inherited from %s:]\n' % (o.name,)
        s += '}'
        return s

    def add_setting_option(self, name, optname, optdesc, default, **kwargs):
        def f(value):
            self[name] = value
        if isinstance(default, str):
            old = default
            default = lambda: expand(old, self)
        opt = Option(optname, optdesc, f, default, **kwargs)
        self[name] = PendingOption(opt)

    def new_inheritor(self, *args, **kwargs):
        return SettingsGroup(inherit_parent=self, *args, **kwargs)

    def new_child(self, name, *args, **kwargs):
        sg = SettingsGroup(group_parent=self, name='%s.%s' % (self.name, name), *args, **kwargs)
        self[name] = sg
        return sg

class OptSection(object):
    def __init__(self, desc):
        self.desc = desc
        self.opts = []
        all_opt_sections.append(self)
    def move_to_end(self):
        all_opt_sections.remove(self)
        all_opt_sections.append(self)

class Option(object):
    def __init__(self, name, help, on_set, default=None, bool=False, show=True, section=None, metavar=None, type=str, **kwargs):
        if name.startswith('--'):
            self.is_env = False
            assert set(kwargs).issubset({'nargs', 'choices', 'required', 'metavar'})
        elif name.endswith('='):
            self.is_env = True
            assert len(kwargs) == 0
            assert bool is False
        else:
            raise ValueError("name %r should be '--opt' or 'ENV='" % (name,))
        self.name = name
        self.help = help
        self.default = default
        self.on_set = on_set
        self.show = show
        self.type = type
        if metavar is None:
            metavar = '...'
        self.metavar = metavar
        self.bool = bool
        self.section = section if section is not None else default_opt_section
        self.section.opts.append(self)
        self.argparse_kw = kwargs.copy()
        all_options.append(self)
        if name in all_options_by_name:
            raise KeyError('trying to create Option with duplicate name %r; old is:\n%r' % (name, all_options_by_name[name]))
        all_options_by_name[name] = self
    def __repr__(self):
        value = repr(self.value) if hasattr(self, 'value') else '<none yet>'
        return 'Option(name=%r, help=%r, value=%s, default=%r)' % (self.name, self.help, value, self.default)

    def need(self):
        self.show = True

    def set(self, value):
        if value is None:
            value = self.default
            if callable(value): # Pending
                value = value()
        self.value = value
        if self.on_set is not None:
            self.on_set(value)

def parse_expander(fmt):
    bits = []
    z = 0
    while True:
        y = fmt.find('(', z)
        if y == -1:
            bits.append(fmt[z:])
            break
        bits.append(fmt[z:y])
        should_shlex_result = False
        if fmt[y+1:y+2] == '*':
            should_shlex_result = True
            y += 1
        try:
            parser.expr(fmt[y+1:])
        except SyntaxError as e:
            offset = e.offset
            if offset == 0 or fmt[y+1+offset-1] != ')':
                raise
            bits.append((compile(fmt[y+1:y+1+offset-1], '<string>', 'eval'), should_shlex_result))
            z = y+1+offset
    return bits

def eval_expand_bit(bit, settings):
    dep = eval(bit, settings.vals, settings.__dict__)
    if isinstance(dep, Pending):
        dep = dep.resolve()
    return dep

def expand(fmt, settings):
    bits = parse_expander(fmt)
    return ''.join((bit if isinstance(bit, basestring) else eval_expand_bit(bit[0], settings)) for bit in bits)

def expand_argv(argv, settings):
    if isinstance(argv, basestring):
        bits = parse_expander(argv)
        shell = ''.join(bit if isinstance(bit, basestring) else '(!)' for bit in bits)
        codes = [bit for bit in bits if not isinstance(bit, basestring)]
        argv = shlex.split(shell)
        out_argv = []
        for arg in argv:
            first = True
            out_argv.append('')
            for bit in arg.split('(!)'):
                if not first:
                    code, should_shlex_result = codes.pop(0)
                    res = eval_expand_bit(code, settings)
                    res = shlex.split(res) if should_shlex_result else [res]
                    out_argv[-1] += res[0]
                    out_argv.extend(res[1:])
                first = False
                out_argv[-1] += bit
        return out_argv
    else:
        return [expand(arg, settings) for arg in argv]

def installation_dirs_group(sg):
    section = OptSection('Fine tuning of the installation directories:')
    for name, optname, optdesc, default in [
        ('prefix', '--prefix', '', '/usr/local'),
        ('exec_prefix', '--exec-prefix', '', '(prefix)'),
        ('bin', '--bindir', '', '(exec_prefix)/bin'),
        ('sbin', '--sbindir', '', '(exec_prefix)/sbin'),
        ('libexec', '--libexecdir', '', '(exec_prefix)/libexec'),
        ('etc', '--sysconfdir', '', '(prefix)/etc'),
        ('var', '--localstatedir', '', '(prefix)/var'),
        ('lib', '--libdir', '', '(prefix)/lib'),
        ('include', '--includedir', '', '(prefix)/include'),
        ('datarootdir', '--datarootdir', '', '(prefix)/share'),
        ('share', '--datadir', '', '(datarootdir)'),
        ('locale', '--localedir', '', '(datarootdir)/locale'),
        ('man', '--mandir', '', '(datarootdir)/man'),
        ('doc', '--docdir', '', '(datarootdir)/doc/(group_parent.package_unix_name)'),
        ('html', '--htmldir', '', '(doc)'),
        ('pdf', '--pdfdir', '', '(doc)'),
    ]:
        sg.add_setting_option(name, optname, optdesc, default, section=section, show=False)
    for ignored in ['--sharedstatedir', '--oldincludedir', '--infodir', '--dvidir', '--psdir']:
        Option(ignored, 'Ignored autotools compatibility setting', None, section=section, show=False)

def _make_argparse(include_unused, include_env):
    parser = argparse.ArgumentParser(
        add_help=False,
        usage='configure [OPTION]... [VAR=VALUE]...',
        prefix_chars=('-' + string.ascii_letters if include_env else '-'),
    )
    parser.add_argument('--help', action='store_true', help='Show this help', dest='__help')
    parser.add_argument('--help-all', action='store_true', help='Show this help, including unused options', dest='__help_all')
    for sect in all_opt_sections:
        def include(opt):
            return (include_unused or opt.show) and (include_env or not opt.is_env)
        if not any(map(include, sect.opts)):
            continue
        ag = parser.add_argument_group(description=sect.desc)
        for opt in sect.opts:
            if not include(opt):
                continue
            ag.add_argument(opt.name,
                            action='store_true' if opt.bool else 'store',
                            dest=opt.name[2:],
                            help=opt.help,
                            type=opt.type,
                            metavar=opt.metavar,
                            **opt.argparse_kw)
    return parser

def _print_help(include_unused=False):
    parser = _make_argparse(include_unused, include_env=True)
    parser.print_help()

def parse_args():
    will_need(pre_parse_args_will_need)
    default_opt_section.move_to_end()
    parser = _make_argparse(include_unused=True, include_env=False)
    args, argv = parser.parse_known_args()
    if args.__help or args.__help_all:
        _print_help(include_unused=args.__help_all)
        sys.exit(0)
    unrecognized_env = []
    def do_env_arg(arg):
        m = re.match('([^- ]+)=(.*)', arg)
        if not m:
            return True # keep for unrecognized
        if m.group(1) + '=' not in all_options_by_name:
            unrecognized_env.append(arg)
        else:
            os.environ[m.group(1)] = m.group(2)
        return False
    unrecognized_argv = list(filter(do_env_arg, argv))
    if unrecognized_argv:
        print ('unrecognized arguments: %s' % (argv_to_shell(unrecognized_argv),))
    if unrecognized_env:
        print ('unrecognized environment: %s' % (argv_to_shell(unrecognized_env),))
    if unrecognized_argv or unrecognized_env:
        _print_help()
        sys.exit(0)

    for opt in all_options:
        try:
            if opt.is_env:
                name = opt.name[:-1]
                opt.set(opt.type(os.environ[name]) if name in os.environ else None)
            else:
                opt.set(getattr(args, opt.name[2:]))
        except DependencyNotFoundException as e:
            def f(): raise e
            post_parse_args_will_need.append(f)
        #print args._unrecognized_args

    global did_parse_args
    did_parse_args = True
    will_need(post_parse_args_will_need)

# -- toolchains --
class Triple(namedtuple('Triple', 'triple arch vendor os abi')):
    def __new__(self, triple):
        if isinstance(triple, Triple):
            return triple
        else:
            bits = triple.split('-')
            numbits = len(bits)
            if numbits > 4:
                raise Exception('strange triple %r' % (triple,))
            if numbits in (2, 3) and bits[1] not in ('unknown', 'none', 'pc'):
                # assume the vendor was left out
                bits.insert(1, None)
            return super(Triple, self).__new__(self, triple, *((bits.pop(0) if bits else None) for i in range(4)))
    def __str__(self):
        return self.triple

class Machine(object):
    def __init__(self, name, settings, triple_help, triple_default):
        self.name = name
        self.settings = settings
        def on_set(val):
            self.triple = val
        self.triple_option = Option('--' + name, help=triple_help, default=triple_default, on_set=on_set, type=Triple, section=triple_options_section)
        self.triple = PendingOption(self.triple_option)

        self.toolchains = memoize(self.toolchains)
        self.c_tools = memoize(self.c_tools)

    def __eq__(self, other):
        return self.triple == other.triple
    def __ne__(self, other):
        return self.triple != other.triple
    def __repr__(self):
        return 'Machine(name=%r, triple=%s)' % (self.name, repr(self.triple) if hasattr(self, 'triple') else '<none yet>')

    def is_cross(self):
        # This is only really meaningful in GNU land, as it decides whether to
        # prepend the triple (hopefully other targets are sane enough not to
        # have a special separate "cross compilation mode" that skips
        # configuration checks, but...).  Declared here because it may be
        # useful to override.
        if not hasattr(self, '_is_cross'):
            self._is_cross = self.triple != self.settings.build_machine().triple
        return self._is_cross

    def is_darwin(self):
        return (self.triple.os is not None and 'darwin' in self.triple.os) or \
            (self.triple.triple == '' and os.path.exists('/System/Library/Frameworks'))

    # Get a list of appropriate toolchains.
    def toolchains(self): # memoized
        tcs = []
        if os.path.exists('/usr/bin/xcrun'):
            tcs.append(XcodeToolchain(self, self.settings))
        tcs.append(UnixToolchain(self, self.settings))
        return tcs

    #memoize
    def c_tools(self):
        return CTools(self.settings, self, self.toolchains())

class CLITool(object):
    def __init__(self, name, defaults, env, machine, toolchains, dont_suffix_env=False):
        self.name = name
        self.defaults = defaults
        self.env = env
        self.toolchains = toolchains
        self.needed = False
        if machine.name != 'host' and not dont_suffix_env:
            env = '%s_FOR_%s' % (env, machine.name.upper())
        def on_set(val):
            if val is not None:
                self.argv_from_opt = shlex.split(val)
        self.argv_opt = Option(env + '=', help='Default: %r' % (defaults,), on_set=on_set, show=False)
        self.argv = memoize(self.argv)

    def __repr__(self):
        return 'CLITool(name=%r, defaults=%r, env=%r)' % (self.name, self.defaults, self.env)

    def optional(self):
        self.argv_opt.need()

    def required(self):
        self.optional()
        post_parse_args_will_need.append(lambda: self.argv())

    def argv(self): # memoized
        # If the user specified it explicitly, don't question.
        if hasattr(self, 'argv_from_opt'):
            log('Using %s from command line: %s\n' % (self.name, argv_to_shell(self.argv_from_opt)))
            return self.argv_from_opt

        failure_notes = []
        for tc in self.toolchains:
            argv = tc.find_tool(self, failure_notes)
            if argv is not None:
                log('Found %s: %s\n' % (self.name, argv_to_shell(argv)))
                return argv

        log('** Failed to locate %s\n' % (self.name,))
        for n in failure_notes:
            log('  note: %s\n' % indentify(n, '  '))
        raise DependencyNotFoundException

    def locate_in_paths(self, prefix, paths):
        for path in paths:
            for default in self.defaults:
                default = prefix + default
                filename = os.path.join(path, default)
                if os.path.exists(filename):
                    return [filename]
        return None


class UnixToolchain(object):
    def __init__(self, machine, settings):
        self.machine = machine
        self.settings = settings

    def find_tool(self, tool, failure_notes):
        prefix = ''
        if self.machine.is_cross():
            prefix = self.machine.triple.triple + '-'
            failure_notes.append('detected cross compilation, so searched for %s-%s' % (self.machine.triple.triple, tool.name))
        return tool.locate_in_paths(prefix, self.settings.tool_search_paths)

# Reads a binary or XML plist (on OS X)
def read_plist(gunk):
    import plistlib
    if sys.version_info >= (3, 0):
        return plistlib.loads(gunk) # it can do it out of the box
    else:
        if gunk.startswith('bplist'):
            p = subprocess.Popen('plutil -convert xml1 - -o -'.split(), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            gunk, _ = p.communicate(gunk)
            assert p.returncode == 0

        return plistlib.readPlistFromString(gunk)

class XcodeToolchain(object):
    def __init__(self, machine, settings):
        self.machine = machine
        prefix = machine.name if machine.name != 'host' else ''
        name = '--%sxcode-sdk' % (prefix,)
        self.sdk_opt = Option(name, help='Use Xcode SDK - `xcodebuild -showsdks` lists; typical values: macosx, iphoneos, iphonesimulator, watchos, watchsimulator', on_set=self.on_set_sdk)
        self.got_sdk = False
        name = '--%sxcode-archs' % (prefix,)
        self.sdk_opt = Option(name, help='Comma-separated list of -arch settings for use with an Xcode toolchain', on_set=self.on_set_arch)
        self.got_arch = False

    def on_set_sdk(self, val):
        using_default = val is None
        self.using_default_sdk = using_default
        if using_default:
            if self.machine != settings_root.build_machine():
                # assume some other kind of cross compilation
                return
            val = 'macosx'
        # this is used for arch and also serves as a check
        sdk_platform_path, _, code = run_command(['/usr/bin/xcrun', '--sdk', val, '--show-sdk-platform-path'])
        if code == 127:
            log('* Failed to run /usr/bin/xcrun\n')
            if not using_default:
                raise DependencyNotFoundException
            return
        elif code:
            log('* Xcode SDK %r not found\n' % (val,))
            if not using_default:
                raise DependencyNotFoundException
            return
        self.sdk_platform_path = sdk_platform_path.rstrip()
        log('Xcode SDK platform path: %r\n' % (self.sdk_platform_path,))

        self.got_sdk = True

    def on_set_arch(self, val):
        if not self.got_sdk:
            return
        self.archs = self.get_archs(val)
        log('Using architectures: %s\n' % (repr(self.archs) if self.archs != [] else '(native)'))
        self.got_arch = True

    def get_archs(self, val):
        if val is not None:
            return re.sub('\s', '', val).split(',')
        # try to divine appropriate architectures
        # this may fail with future versions of Xcode, but at least we tried
        if self.sdk_platform_path.endswith('MacOSX.platform'):
            # Assume you just wanted to build natively
            return []
        xcspecs = glob.glob('%s/Developer/Library/Xcode/Specifications/*Architectures.xcspec' % (self.sdk_platform_path,)) + \
                  glob.glob('%s/Developer/Library/Xcode/PrivatePlugIns/*/Contents/Resources/Device.xcspec' % (self.sdk_platform_path,))
        for spec in xcspecs:
            def f():
                try:
                    pl = read_plist(open(spec, 'rb').read())
                except:
                    raise
                    return
                if not isinstance(pl, list):
                    return
                for item in pl:
                    if not isinstance(item, dict):
                        return
                    if item.get('ArchitectureSetting') != 'ARCHS_STANDARD':
                        return
                    archs = item.get('RealArchitectures')
                    if not isinstance(archs, list) and not all(isinstance(arch, basestring) for arch in archs):
                        return
                    return archs
            archs = f()
            if archs is not None:
                return archs
            log('(Failed to divine architectures from %r for some reason...)\n' % (spec,))

        # give up
        log("%s default Xcode SDK because I can't figure out a reasonable list of architectures; pass %s=arch1,arch2 to override\n" % (
            "Not using" if self.using_default_sdk else "Can't use",
            self.arch_opt.name,
        ))
        if self.using_default_sdk:
            raise DependencyNotFoundException

    def find_tool(self, tool, failure_notes):
        if not self.got_arch:
            return None
        argv = ['/usr/bin/xcrun', tool.name]
        sod, sed, code = run_command(argv + ['--asdf'])
        if code != 0:
            if sed.startswith('xcrun: error: unable to find utility'):
                failure_notes.append(sed)
                return None
        return argv

# Just a collection of common tools, plus flag options
class CTools(object):
    def __init__(self, settings, machine, toolchains):
        tools = [
            ('cc',   ['cc', 'gcc', 'clang'],    'CC'),
            ('cxx',  ['c++', 'g++', 'clang++'], 'CXX'),
            ('ar',),
            ('nm',),
            ('ranlib',),
            ('strip',),
            # GNU
            ('objdump', ['objdump', 'gobjdump'], 'OBJDUMP'),
            ('objcopy', ['objcopy', 'gobjcopy'], 'OBJCOPY'),
            # OS X
            ('lipo',),
        ]
        for spec in tools:
            if len(spec) == 1:
                name, defaults, env = spec[0], [spec[0]], spec[0].upper()
            else:
                name, defaults, env = spec
            tool = CLITool(name, defaults, env, machine, toolchains)
            setattr(self, name, tool)

        section = OptSection('Compiler/linker flags:')
        self.cflags_opt = settings.add_setting_option('cflags', 'CFLAGS=', 'Flags for $CC', [], section=section, type=shlex.split)
        self.cxxflags_opt = settings.add_setting_option('cxxflags', 'CXXFLAGS=', 'Flags for $CXX', [], section=section, type=shlex.split)
        self.ldflags_opt = settings.add_setting_option('ldflags', 'LDFLAGS=', 'Flags for $CC/$CXX when linking', [], section=section, type=shlex.split)
        self.cppflags_opt = settings.add_setting_option('cppflags', 'CPPFLAGS=', 'Flags for $CC/$CXX when not linking (supposed to be used for preprocessor flags)', [], section=section, type=shlex.split)


# A nicer - but optional - way of doing multiple tests that will print all the
# errors in one go and exit cleanly
def will_need(tests):
    failures = 0
    for test in tests:
        try:
            test()
        except DependencyNotFoundException:
            failures += 1
    if failures > 0:
        log('(%d failure%s.)\n' % (failures, 's' if failures != 1 else ''))
        sys.exit(1)

class Emitter(object):
    def pre_output(self):
        assert not hasattr(self, 'did_output')
        self.did_output = True
    def set_default_rule(self, rule):
        self.default_rule = rule
    def add_command(self, settings, outs, ins, argvs, *args, **kwargs):
        outs = [expand(x, settings) for x in outs]
        ins = [expand(x, settings) for x in ins]
        argvs = [expand_argv(x, settings) for x in argvs]
        return self.add_command_raw(outs, ins, argvs, *args, **kwargs)
    def emit(self, fn):
        output = self.output()
        log('Writing %s\n' % (fn,))
        fp = open(fn, 'w')
        fp.write(output)
        fp.close()


# In the future it may be desirable to use make variables and nontrivial ninja rules for efficiency.

class MakefileEmitter(Emitter):
    def __init__(self, settings):
        self.settings = settings
        self.makefile_bits = []

    def add_all_and_clean(self):
        if hasattr(self, 'default_rule'):
            if self.default_rule != 'all':
                self.add_command_raw(['all'], [self.default_rule], [], phony=True)
        else:
            log('Warning: %r: no default rule\n' % (self,))
        self.makefile_bits.append('clean:\n\trm -rf %s\n' % (self.filename_rel_and_escape(self.settings.out)))

    @staticmethod
    def filename_escape(fn):
        if re.search('[\n\0]', fn):
            raise ValueError("your awful filename %r can't be encoded in make (probably)" % (fn,))
        return re.sub(r'([ :\$\\])', r'\\\1', fn)
    def filename_rel_and_escape(self, fn):
        fn = os.path.relpath(fn, os.path.dirname(self.settings.emit_fn))
        return self.filename_escape(fn)
    # depfile = ('makefile', filename) or ('msvc',)
    def add_command_raw(self, outs, ins, argvs, phony=False, depfile=None):
        bit = ''
        outs = ' '.join(map(self.filename_rel_and_escape, outs))
        ins = ' '.join(map(self.filename_rel_and_escape, ins))
        if phony:
            bit += '.PHONY: %s\n' % (outs,)
        bit += '%s:%s%s\n' % (outs, ' ' if ins else '', ins)
        for argv in argvs:
            bit += '\t' + argv_to_shell(argv) + '\n'
        if depfile is not None:
            if depfile[0] != 'makefile':
                raise ValueError("don't support depfile of type %r" % (depfile[0],))
            bit += '-include %s\n' % (self.filename_rel_and_escape(depfile[1]),)
        if 'all' in outs:
            self.makefile_bits.insert(0, bit)
        else:
            self.makefile_bits.append(bit)

    def output(self):
        self.pre_output()
        self.add_all_and_clean()
        return '\n'.join(self.makefile_bits)

    def default_outfile(self):
        return 'Makefile'

class NinjaEmitter(Emitter):
    def __init__(self, settings):
        self.settings = settings
        self.ninja_bits = []
        self.ruleno = 0
    @staticmethod
    def filename_escape(fn):
        if re.search('[\n\0]', fn):
            raise ValueError("your awful filename %r can't be encoded in ninja (probably)" % (fn,))
        return re.sub(r'([ :\$])', r'$\1', fn)
    def add_command_raw(self, outs, ins, argvs, phony=False, depfile=None):
        bit = ''
        if phony:
            if len(argvs) == 0:
                self.ninja_bits.append('build %s: phony %s\n' % (' '.join(map(self.filename_escape, outs)), ' '.join(map(self.filename_escape, ins))))
                return
            outs2 = ['__phony_' + out for out in outs]
            bit += 'build %s: phony %s\n' % (' '.join(map(self.filename_escape, outs)), ' '.join(map(self.filename_escape, outs2)))
            outs = outs2
        rule_name = 'rule_%d' % (self.ruleno,)
        self.ruleno += 1
        bit += 'rule %s\n' % (rule_name,)
        bit += '  command = %s\n' % (' && $\n    '.join(map(argv_to_shell, argvs)))
        if depfile:
            if depfile[0] not in ('makefile', 'msvc'):
                raise ValueError("don't support depfile of type %r" % (depfile[0],))
            bit += '  deps = %s\n' % ({'makefile': 'gcc', 'msvc': 'msvc'}[depfile[0]],)
            bit += '  depfile = %s\n' % (self.filename_escape(depfile[1]),)
        bit += 'build %s: %s' % (' '.join(map(self.filename_escape, outs),), rule_name)
        if ins:
            bit += ' | %s' % (' '.join(map(self.filename_escape, ins),))
        bit += '\n'
        self.ninja_bits.append(bit)

    def add_default(self):
        if hasattr(self, 'default_rule'):
            self.ninja_bits.append('default %s\n' % (self.default_rule,))
        else:
            log('Warning: %r: no default rule\n' % (self,))

    def output(self):
        self.pre_output()
        self.add_default()
        return '\n'.join(self.ninja_bits)

    def default_outfile(self):
        return 'build.ninja'


def add_emitter_option():
    def on_set_generate(val):
        if val not in emitters:
            raise DependencyNotFoundException('Unknown build script type: %s (options: %s)' % (val, ' '.join(emitters.keys())))
        settings_root.emitter = emitters[val](settings_root)
    Option(
        '--generate',
        'The type of build script to generate.  Options: %s (default makefile)' % (', '.join(emitters.keys()),),
        on_set_generate, default='makefile', section=output_section)
    settings_root.add_setting_option('emit_fn', '--outfile', 'Output file.  Default: depends on type', section=output_section, default=lambda: settings_root.emitter.default_outfile())

def finish_and_emit():
    settings_root.emitter.emit(settings_root.emit_fn)

# see cc_to_use_cb
def default_cc_to_use(filename):
    root, ext = os.path.splitext(filename)
    return ext in ('cc', 'cpp', 'cxx', 'mm')

# emitter:      the emitter to add rules to
# machine:      machine
# settings:     settings object; will inspect {c,cxx,cpp,ld}flags
# sources:      list of source files
# headers:      *optional* list of header files that will be used in the future to
#               generate IDE projects - unused for makefile/ninja due to
#               depfiles
# objs:         list of .o files or other things to add to the link
# link_out:     optional linker output
# link_type:    'exec', 'dylib', 'staticlib', 'obj'; default exec
# info_cb:      (filename) -> a dict with any of these keys:
#                 'is_cxx': True ($CXX) or False ($CC); ignored in IDE native mode
#                 'cc':  override cc altogther; ignored in IDE native mode
#                 'cflags': *override* cflags; never ignored
#                 'obj_fn': ...
#                 'extra_deps': dependencies
# force_cli:    don't use IDEs' native C/C++ compilation mechanism
# expand:       call expand on filenames
def build_c_objs(emitter, machine, settings, sources, headers=[], info_cb=None, force_cli=False, expand=True):
    if expand:
        headers = [expand(header, settings) for header in headers]
    tools = machine.c_tools()
    any_was_cxx = False
    obj_fns = []
    _expand = globals()['expand']
    for fn in sources:
        if expand:
            fn = _expand(fn, settings)
        info = {} if info_cb is None else info_cb(fn)
        obj_fn = info['obj_fn'] if 'obj_fn' in info else guess_obj_fn(fn, settings)
        is_cxx = info.get('is_cxx', False)
        cflags = info['cflags'] if 'cflags' in info else (settings.cxxflags if is_cxx else settings.cflags)
        cc = info['cc'] if 'cc' in info else (tools.cxx if is_cxx else tools.cc).argv()
        extra_deps = info.get('extra_deps', [])
        any_was_cxx = any_was_cxx or is_cxx
        dep_fn = os.path.splitext(obj_fn)[0] + '.d'

        mkdir_cmd = ['mkdir', '-p', os.path.dirname(obj_fn)]
        cmd = cc + cflags + ['-c', '-o', obj_fn, '-MMD', '-MF', dep_fn, fn]

        emitter.add_command_raw([obj_fn], [fn] + extra_deps, [mkdir_cmd, cmd], depfile=('makefile', dep_fn))
        obj_fns.append(obj_fn)

    return obj_fns, any_was_cxx

def link_c_objs(emitter, machine, settings, link_type, link_out, objs, link_with_cxx=None, force_cli=False, expand=True, extra_deps=[]):
    if expand:
        _expand = globals()['expand']
        link_out = _expand(link_out, settings)
        objs = [_expand(obj, settings) for obj in objs]
    tools = machine.c_tools()
    assert link_type in ('exec', 'dylib', 'staticlib', 'obj')
    if link_type in ('exec', 'dylib'):
        assert link_with_cxx in (False, True)
        cc_for_link = (tools.cxx if link_with_cxx else tools.cc).argv()
        if link_type == 'dylib':
            typeflag = ['-dynamiclib'] if machine.is_darwin() else ['-shared']
        else:
            typeflag = []
        cmd = cc_for_link + typeflag + settings.ldflags + ['-o', link_out] + objs
    elif link_type == 'staticlib':
        cmd = tools.ar() + ['rcs'] + objs
    elif link_type == 'obj':
        cmd = tools.cc() + ['-Wl,-r', '-nostdlib', '-o', link_out] + objs
    mkdir_cmd = ['mkdir', '-p', os.path.dirname(link_out)]
    emitter.add_command_raw([link_out], objs + extra_deps, [mkdir_cmd, cmd])

def build_and_link_c_objs(emitter, machine, settings, link_type, link_out, sources, headers=[], objs=[], info_cb=None, force_cli=False, expand=True, extra_deps=[]):
    more_objs, link_with_cxx = build_c_objs(emitter, machine, settings, sources, headers, info_cb, force_cli, expand)
    link_c_objs(emitter, machine, settings, link_type, link_out, objs + more_objs, link_with_cxx, force_cli, expand, extra_deps)

def guess_obj_fn(fn, settings):
    rel = os.path.relpath(fn, settings.src)
    if not rel.startswith('../'):
        rel = os.path.splitext(rel)[0] + '.o'
        return os.path.join(settings.out, rel)
    raise ValueError("can't guess .o filename for %r, as it's not in settings.src" % (fn,))

# -- init code --


init_config_log()

did_parse_args = False

all_options = []
all_options_by_name = {}
all_opt_sections = []
default_opt_section = OptSection('Uncategorized options:')
pre_parse_args_will_need = []
post_parse_args_will_need = []

settings_root = SettingsGroup(name='root')
settings_root.package_unix_name = Pending()
installation_dirs_group(settings_root.new_child('idirs'))

output_section = OptSection('Output options:')

triple_options_section = OptSection('System types:')
settings_root.build_machine = memoize(lambda: Machine('build', settings_root, 'the machine doing the build', lambda: Triple('')))
settings_root.host_machine = memoize(lambda: settings_root.build_machine() and Machine('host', settings_root, 'the machine that will run the compiled program', lambda: settings_root.build_machine().triple))
# ...'the machine that the program will itself compile programs for',

settings_root.tool_search_paths = os.environ['PATH'].split(':')

settings_root.src = os.path.dirname(sys.argv[0])
settings_root.out = os.path.join(os.getcwd(), 'out')

emitters = {
    'makefile': MakefileEmitter,
    'ninja': NinjaEmitter,
}

pre_parse_args_will_need.append(add_emitter_option)

# --

