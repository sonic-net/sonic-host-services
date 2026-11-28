# Copyright 2026 Nexthop Systems Inc. All rights reserved.

import subprocess
from unittest.mock import call

RESTORE_BINARIES = ('iptables-restore', 'ip6tables-restore')


def split_restore_line(line):
    """Splits like iptables-restore: only double quotes group, backslash escapes only inside them."""
    words, word, quoted, escaped, started = [], '', False, False, False
    for c in line:
        if escaped:
            word, escaped = word + c, False
        elif quoted and c == '\\':
            escaped = True
        elif c == '"':
            quoted, started = not quoted, True
        elif c in ' \t' and not quoted:
            if started:
                words.append(word)
            word, started = '', False
        else:
            word, started = word + c, True
    if started:
        words.append(word)
    return words


def restore_text_to_commands(binary, text, prefix=()):
    """
        The iptables/ip6tables commands an '<binary>-restore --noflush' input stands
        for, in order; a non-filter table becomes a leading '-t <table>'.
    """
    cmds, table = [], None
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        if line.startswith('*'):
            table = line[1:]
        elif line == 'COMMIT':
            table = None
        else:
            assert table is not None, "rule outside a table section: {}".format(line)
            tbl = [] if table == 'filter' else ['-t', table]
            cmds.append(list(prefix) + [binary] + tbl + split_restore_line(line))
    return cmds


def expand_restore_calls(mock_calls):
    """
        Popen calls from a mocked subprocess module (its mock_calls) with every
        '<binary>-restore' call replaced by the per-command Popen calls it stands
        for, so expectations written per command still apply.
    """
    out, pending = [], None
    for name, args, kwargs in mock_calls:
        if name == 'Popen':
            cmd = list(args[0])
            pos = next((i for i, w in enumerate(cmd) if w in RESTORE_BINARIES), None)
            if pos is None:
                out.append(call(*args, **kwargs))
            else:
                pending = (cmd[:pos], cmd[pos][:-len('-restore')])
        elif name == 'Popen().communicate' and pending is not None:
            prefix, binary = pending
            pending = None
            for cmd in restore_text_to_commands(binary, kwargs.get('input', args[0] if args else ''), prefix):
                out.append(call(cmd, universal_newlines=True, stdout=subprocess.PIPE))
    return out


def assert_calls_in(testcase, expected, actual):
    for c in expected:
        testcase.assertIn(c, actual)


def assert_restore_only(testcase, popen_call_args_list):
    """the rebuild reached iptables/ip6tables only through the two restore calls"""
    cmds = [list(c[0][0]) for c in popen_call_args_list]
    testcase.assertEqual([c for c in cmds if c[0] in RESTORE_BINARIES],
                         [['iptables-restore', '--noflush', '--wait=30'], ['ip6tables-restore', '--noflush', '--wait=30']])
    testcase.assertEqual([c for c in cmds if c[0] in ('iptables', 'ip6tables')], [])
