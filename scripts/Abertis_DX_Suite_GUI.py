#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Graphical front-end for the Abertis / Cellnex DX Suite (Hispasat 30.0W).

A desktop GUI (PyQt6) around the engine in Abertis_DX_Suite.py.
"""

import contextlib
import io
import json
import os
import queue
import shlex
import sys
import threading
import time
import traceback
import types
import urllib.request
from collections import OrderedDict

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QByteArray, QTimer, QEvent
from PyQt6.QtGui import QFont, QColor, QPalette, QAction, QKeySequence, QTextCursor, QFontDatabase
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox, QSpinBox,
    QDoubleSpinBox, QTreeWidget, QTreeWidgetItem, QTextEdit, QTabWidget,
    QMenuBar, QStatusBar, QMessageBox, QFileDialog, QSlider, QSplitter,
    QGroupBox, QRadioButton, QScrollArea, QFrame, QScrollBar,
    QDialog, QProgressBar, QSizePolicy
)

import re
import shutil
import struct
import zlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_JSON_URL = 'https://abertis-dtt.github.io/sat_data.json'

BOUQUET_HEADER = u'#NAME \u2022 30,0 W - Abertis & Cellnex DX Suite ( streamrelay )'


# Modern dark palette used by the Qt front-end.
THEME = {
    'bg': '#0f1115', 'surface': '#171a21', 'surface2': '#1e222b',
    'field': '#1e222b', 'fg': '#e8eaf0', 'muted': '#98a2b3',
    'accent': '#4c8dff', 'accent_hi': '#6ba0ff', 'accent_lo': '#3a76e0',
    'ok': '#3fd07a', 'warn': '#f0b429', 'err': '#ff5c69',
    'border': '#262b36', 'border_hi': '#343b4a',
    'tree': '#12151b', 'sel': '#233654', 'sel_hi': '#2d4468',
}

# Base UI font is 18 pt; the stylesheet was originally tuned for 10.5 pt.
FONT_BASE_PT = 18.0
FONT_REFERENCE_PT = 10.5
UI_UNIT = FONT_BASE_PT / FONT_REFERENCE_PT


FEC_CODES = {
    '1/2': 1, '2/3': 2, '3/4': 3, '5/6': 4, '6/7': 5, '7/8': 6,
    '8/9': 7, '3/5': 8, '4/5': 9, '9/10': 10, 'AUTO': 15,
}

MODULATION_CODES = {'QPSK': 0, '8PSK': 2, '16APSK': 3, '32APSK': 4}

POLARISATION_CODES = {'H': 0, 'V': 1}


BISS_CAID = '2600'
BISS_CRC_SEED = 0x2600
DUMMY_BISS_KEY = '0000000000000000'

STREAM_PID_RE = re.compile(r'Stream\s+(\d+)', re.IGNORECASE)
FREQ_POL_MODE_RE = re.compile(r'^\s*([0-9]+(?:\.[0-9]+)?)\s+([HVLR])\s+(.+?)\s*$')
SERVICE_FLAG_PREFIXES = ('p:', 'c:', 'C:', 'f:', 'v:')


def namespace_for(position_tenths):
    pos = position_tenths + 3600 if position_tenths < 0 else position_tenths
    return (pos & 0xFFFF) << 16


def parse_transponders(data):
    txps = OrderedDict()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        match = FREQ_POL_MODE_RE.match(entry.get('freq_pol_mode') or '')
        if not match:
            print('Warning: cannot parse freq_pol_mode %r on txp %s - skipped'
                  % (entry.get('freq_pol_mode'), entry.get('txp_no')))
            continue
        freq_mhz = float(match.group(1))
        pol = match.group(2)
        delivery = match.group(3).upper()
        system = 1 if 'S2' in delivery else 0
        modulation = MODULATION_CODES.get(delivery.split('/')[-1].split()[0],
                                          2 if system else 0)
        if pol not in POLARISATION_CODES:
            print('Warning: unusual polarisation %r on txp %s' % (pol, entry.get('txp_no')))
        if 'PLS' in delivery or 'MISO' in delivery:
            print('Warning: txp %s (%s) uses %s - lamedb cannot carry PLS/MISO '
                  'parameters, add them manually if your image supports it'
                  % (entry.get('txp_no'), entry.get('freq_pol_mode'),
                     delivery[delivery.find('PLS' if 'PLS' in delivery else 'MISO'):]))
        sr_fec = (entry.get('sr_fec') or '').split()
        parts = [p.strip() for p in (entry.get('onid_nid_tid') or '').split('/')]
        onid, tsid = 1, None
        if len(parts) == 3:
            try:
                tsid = int(parts[2])
            except ValueError:
                tsid = None
            try:
                onid = int(parts[0])
            except ValueError:
                onid = 1
        if tsid is None:
            print('Warning: cannot parse tsid in onid_nid_tid %r on txp %s - skipped'
                  % (entry.get('onid_nid_tid'), entry.get('txp_no')))
            continue
        if not sr_fec:
            print('Warning: missing sr_fec on txp %s' % entry.get('txp_no'))
        provider = (entry.get('provider') or 'Abertis DTT').strip()
        tp = {
            'txp_no': entry.get('txp_no', '?'),
            'freq_khz': int(freq_mhz * 1000 + 0.5),
            'freq_label': str(int(freq_mhz)),
            'pol_letter': pol,
            'pol': POLARISATION_CODES.get(pol, 0),
            'system': system,
            'modulation': modulation,
            'delivery': delivery,
            'sr_ksps': int(sr_fec[0]) if sr_fec else 0,
            'fec': FEC_CODES.get(sr_fec[1].upper() if len(sr_fec) > 1 else '', 15),
            'onid': onid,
            'tsid': tsid,
            'provider': provider,
            'provider_short': (provider.split()[0] if provider.split() else '') or 'Abertis',
            'streams': entry.get('streams') or [],
        }
        key = (tp['tsid'], tp['onid'])
        if key in txps:
            txps[key]['streams'].extend(tp['streams'])
        else:
            txps[key] = tp
    return list(txps.values())


def collect_streams(txps):
    streams = []
    seen = {}
    channel_lists = []
    for tp in txps:
        for raw in tp['streams']:
            match = STREAM_PID_RE.search(raw.get('title') or '')
            if not match:
                channel_lists.append(raw.get('title') or '(untitled)')
                continue
            pid = int(match.group(1))
            if pid in seen and seen[pid] != (tp['tsid'], tp['onid']):
                print('Warning: PID %d appears on more than one transponder - '
                      'astra/bouquet URLs will collide' % pid)
            seen[pid] = (tp['tsid'], tp['onid'])

            channels = []
            sids = set()
            for ch in raw.get('channels') or []:
                name = (ch.get('name') or '').strip()
                try:
                    sid = int(ch.get('sid'))
                except (TypeError, ValueError):
                    continue
                if not name or sid <= 0 or sid in sids:
                    continue
                sids.add(sid)
                ctype = (ch.get('type') or '').strip().lower()
                radio = 'radio' in ctype or (not ctype and not (ch.get('vpid') or '').strip())
                channels.append({'sid': sid, 'name': name, 'radio': radio})

            def id_field(key, fallback, lo, hi):
                value = str(raw.get(key) or '').strip()
                if not value:
                    return fallback
                try:
                    num = int(value)
                except ValueError:
                    num = None
                if num is None or not lo <= num <= hi:
                    print('Warning: invalid %s %r on %r - using %d'
                          % (key, value, raw.get('title'), fallback))
                    return fallback
                return num

            streams.append({
                'pid': pid,
                'title': raw.get('title') or '',
                'encryption': (raw.get('encryption') or '').upper(),
                'sid': id_field('sid', pid + 1000, 1, 0xFFFF),
                'cpid': id_field('cpid', pid + 5000, 0, 0xFFFF),
                'plp': id_field('plp', 0, 0, 255),
                't2mi_pid': id_field('t2mi_pid', 4096, 0, 0x1FFF),
                't2mi_pnr': id_field('t2mi_pnr', 0, 0, 0xFFFF),
                'txp': tp,
                'channels': channels,
            })
    streams.sort(key=lambda s: s['pid'])
    if channel_lists:
        print('Skipped %d whole-transponder channel lists (no single PID to '
              'relay): %s' % (len(channel_lists), ', '.join(sorted(set(channel_lists)))))
    return streams


def feed_service_name(stream):
    tp = stream['txp']
    return '%s@%s%s%d PID %d' % (tp['provider_short'], tp['freq_label'],
                                  tp['pol_letter'], tp['sr_ksps'], stream['pid'])


def provider_code(stream):
    """Four-letter provider tag used in astra object names (Abertis -> ABER)."""
    short = (stream['txp'].get('provider_short') or 'Abertis').strip()
    return (short[:4] or 'PROV').upper()


def astra_feed_name(stream):
    """Astra name of the raw feed, e.g. Abertis@11653H19680PID301."""
    tp = stream['txp']
    return '%s@%s%s%dPID%d' % (tp['provider_short'], tp['freq_label'],
                               tp['pol_letter'], tp['sr_ksps'], stream['pid'])


def t2mi_tag(stream, t2mi_pid, plp):
    """Names for the T2MI leg: (variable, decap name, channel name, URL segment)."""
    tp = stream['txp']
    code = provider_code(stream)
    low = code.lower()
    pid = stream['pid']
    var_name = 'f%s%s%dpid%dplp%d' % (tp['freq_label'], low, pid, t2mi_pid, plp)
    decap_name = '%s%s%d@%s%d_PID%dPLP%d' % (
        tp['freq_label'], tp['pol_letter'], tp['sr_ksps'],
        code, pid, t2mi_pid, plp)
    return var_name, decap_name, decap_name.lower(), '%s%d_pid%dplp%d' % (
        low, pid, t2mi_pid, plp)


def relay_script_for(stream, override):
    return override or stream['txp']['provider_short'].lower()


def build_lamedb_entries(txps, streams, position):
    ns = namespace_for(position)
    tp_entries = []
    for tp in txps:
        header = '%08x:%04x:%04x' % (ns, tp['tsid'], tp['onid'])
        sline = '\ts %d:%d:%d:%d:%d:2:0:%d:%d:0:2' % (
            tp['freq_khz'], tp['sr_ksps'] * 1000, tp['pol'], tp['fec'],
            position, tp['system'], tp['modulation'])
        tp_entries.append((tuple(header.split(':')), [header, sline]))

    svc_entries = []
    for stream in streams:
        tp = stream['txp']
        sid = stream['sid']
        header = '%04x:%08x:%04x:%04x:1:0' % (sid, ns, tp['tsid'], tp['onid'])
        block = [header, feed_service_name(stream)]
        if 'BISS' in stream['encryption']:
            block.append('p:%s,c:031fff,c:18%04x,c:19%04x,C:%s,f:01'
                         % (tp['provider_short'].upper(), stream['pid'],
                            stream['cpid'], BISS_CAID))
        else:
            block.append('p:%s,c:031fff,c:18%04x,c:19%04x,f:01'
                         % (tp['provider_short'].upper(), stream['pid'],
                            stream['cpid']))
        svc_entries.append((tuple(header.split(':')[:4]), block))
    return tp_entries, svc_entries


def lamedb_text(tp_entries, svc_entries, fmt='v4'):
    lines = ['eDVB services /4/', 'transponders']
    for _key, block in tp_entries:
        lines.extend(block)
        lines.append('/')
    lines.extend(('end', 'services'))
    for _key, block in svc_entries:
        lines.extend(block)
    lines.append('end')
    return '\n'.join(lines) + '\n'


def astra_text(streams, args):
    ns = namespace_for(args.position)
    header = (
        '-- Astra Slonik Mod\n\n'
        '-- during revival and testing\n'
        '-- log.set({ stdout = true, debug = true, syslog = "astra", filename = "/tmp/astrasm.log", })\n'
        '-- after revival and testing\n'
        'log.set({ stdout = true, debug = false, syslog = "astra", filename = "/tmp/astra.log" })\n\n'
        '-- =========================================================================\n'
        '-- [ SECTION 1: ABERTIS PID CHANNELS ]\n'
        '-- =========================================================================\n\n'
        '-- Helper Text: This table maps the Abertis PID to its specific Enigma2 Stream Reference.\n\n'
    )
    blocks = []
    for stream in streams:
        tp = stream['txp']
        sid = stream['sid']
        sref = '1:0:1:%X:%X:%d:%X:0:0:0' % (sid, tp['tsid'], tp['onid'], ns)
        relay = relay_script_for(stream, args.relay_script)
        t2mi_pid = stream.get('t2mi_pid', 4096) or 4096

        block = '\n'.join([
            '-- %s %s %d PID%d Stream' % (tp['freq_label'], tp['pol_letter'],
                                          tp['sr_ksps'], stream['pid']),
            'make_channel({',
            '  name = "%s",' % astra_feed_name(stream),
            '  input = {',
            '    "http://%s:%d/%s:",' % (args.relay_host, args.relay_port, sref),
            '  },',
            '  transform = {{',
            '    format = "pipe",',
            '      command = "/etc/astra/scripts/%s %d",' % (relay, stream['pid']),
            '  }},',
            '  output = {',
            '    "http://%s:%d/%s/pid%d",' % (args.out_host, args.http_port,
                                             relay, stream['pid']),
            '  },',
            '})',
        ])

        plp = stream.get('plp', 0) or 0
        if plp == 0 and args.t2mi_plp is not None:
            plp = args.t2mi_plp
        pnr = stream.get('t2mi_pnr', 0) or 0
        if pnr == 0 and args.t2mi_pnr is not None:
            pnr = args.t2mi_pnr

        decap_var, decap_name, t2mi_name, output_seg = t2mi_tag(
            stream, t2mi_pid, plp)

        block += '\n\n' + '\n'.join([
            '%s = make_t2mi_decap({' % decap_var,
            '        name = "%s",' % decap_name,
            '        input = "http://%s:%d/%s/pid%d",' % (args.out_host, args.http_port,
                                                          relay, stream['pid']),
            '        plp = %d,' % plp,
            '        pnr = %d,' % pnr,
            '        pid = %d,' % t2mi_pid,
            '    })',
        ])

        block += '\n\n' + '\n'.join([
            'make_channel({',
            '  name = "%s",' % t2mi_name,
            '  input = {',
            '    "t2mi://%s",' % decap_var,
            '  },',
            '  output = {',
            '    "http://%s:%d/%s/%s",' % (args.out_host, args.http_port,
                                           relay, output_seg),
            '  },',
            '})',
        ])

        blocks.append(block)
    return header + '\n\n'.join(blocks) + '\n'


def bouquet_text(streams, args):
    lines = [BOUQUET_HEADER]
    for stream in streams:
        if not stream['channels']:
            print('Note: PID %d has no channels in the JSON - lamedb/astra '
                  'entries only' % stream['pid'])
            continue
        tp = stream['txp']
        relay = relay_script_for(stream, args.relay_script)
        t2mi_pid = stream.get('t2mi_pid', 4096) or 4096
        plp = stream.get('plp', 0) or 0
        if plp == 0 and args.t2mi_plp is not None:
            plp = args.t2mi_plp
        output_seg = t2mi_tag(stream, t2mi_pid, plp)[3]
        url = 'http://%s:%d/%s/%s' % (args.out_host, args.http_port,
                                      relay, output_seg)
        marker_name = '##### %s PID %d #####' % (stream['txp']['provider_short'],
                                                 stream['pid'])
        lines.append('#SERVICE 1:64:%X:0:0:0:0:0:0:0::%s' % (stream['pid'], marker_name))
        lines.append('#DESCRIPTION ##### %s #####' % feed_service_name(stream))
        encoded = url.replace(':', '%3A')
        groups = (('1', '(TV)', [c for c in stream['channels'] if not c['radio']]),
                  ('2', '(RADIO)', [c for c in stream['channels'] if c['radio']]))
        for svc_type, tag, channels in groups:
            for ch in channels:
                name = '%s %s' % (tag, ch['name'])
                lines.append('#SERVICE 1:0:%s:%X:2D85:12D:0:0:0:0:%s:%s'
                             % (svc_type, ch['sid'], encoded, name))
                lines.append('#DESCRIPTION %s' % name)
    return '\n'.join(lines) + '\n'


def softcam_key_id(sid, pmt_pid, stream_pid):
    return zlib.crc32(struct.pack('>HHH', sid, pmt_pid, stream_pid), BISS_CRC_SEED) & 0xFFFFFFFF


def softcam_text(streams):
    lines = [
        '; SoftCam.Key generated by the Abertis DX Suite',
        '; Key ID = CRC32(seed=0x2600, SID + PMT + stream PID), big-endian',
        '; Dummy BISS key 0000000000000000 - replace with the real BISS keys.',
        '; Format: F <key id> <expiry date> <BISS key> ;comment',
        '',
    ]
    entries = OrderedDict()
    for stream in streams:
        if 'BISS' not in (stream.get('encryption') or '').upper():
            continue
        key_id = softcam_key_id(stream['sid'], stream['cpid'], stream['pid'])
        entries.setdefault(key_id, []).append(feed_service_name(stream))
    for key_id, comments in entries.items():
        lines.append('F %08X 00000000 %s ;%s' % (key_id, DUMMY_BISS_KEY,
                                                 ' / '.join(comments)))
    return '\n'.join(lines) + '\n'


def parse_lamedb(lines):
    header = lines[0] if lines else ''
    if not header.startswith('eDVB services'):
        sys.exit('Error: not an Enigma2 lamedb (header %r)' % header)
    if '/4/' not in header:
        sys.exit('Error: unsupported lamedb format %r (only v4 /4/ supported)'
                 % header)
    fmt = 'v4'

    idx = 1
    tp_blocks = OrderedDict()
    if fmt == 'v4':
        if idx < len(lines) and lines[idx].strip() == 'transponders':
            idx += 1
        while idx < len(lines):
            line = lines[idx]
            if not line.strip():
                idx += 1
                continue
            if line.strip() == 'end':
                idx += 1
                break
            tp_parts = line.strip().split(':')
            if not (len(tp_parts) >= 3 and tp_parts[0]
                    and all(c in '0123456789abcdefABCDEF' for c in tp_parts[0])
                    and all(c in '0123456789abcdefABCDEF' for c in tp_parts[1])
                    and all(c in '0123456789abcdefABCDEF' for c in tp_parts[2])):
                idx += 1
                continue
            header_line = line
            idx += 1
            block = [header_line]
            while idx < len(lines) and lines[idx].startswith('\t'):
                block.append(lines[idx])
                idx += 1
            if idx < len(lines) and lines[idx].strip() == '/':
                idx += 1
            tp_blocks[tuple(header_line.split(':')[:3])] = block

    svc_blocks = OrderedDict()
    if fmt == 'v4' and idx < len(lines) and lines[idx].strip() == 'services':
        idx += 1
    while idx < len(lines):
        line = lines[idx]
        if not line.strip():
            idx += 1
            continue
        if fmt == 'v4' and line.strip() == 'end':
            break
        svc_parts = line.strip().split(':')
        if not (len(svc_parts) >= 5 and svc_parts[0] and svc_parts[1]
                and svc_parts[2] and svc_parts[3]
                and all(c in '0123456789abcdefABCDEF'
                        for c in svc_parts[0] + svc_parts[1] + svc_parts[2]
                        + svc_parts[3])):
            idx += 1
            continue
        block = [line]
        idx += 1
        if idx < len(lines) and lines[idx].strip() \
                and not lines[idx].startswith(SERVICE_FLAG_PREFIXES):
            block.append(lines[idx])
            idx += 1
        while idx < len(lines) and lines[idx].startswith(SERVICE_FLAG_PREFIXES):
            block.append(lines[idx])
            idx += 1
        svc_blocks[tuple(line.split(':')[:4])] = block
    return fmt, tp_blocks, svc_blocks


def normalize_tp_block(block):
    if not block:
        return block
    header = block[0].strip()
    sline = ''
    for line in block[1:]:
        st = line.strip()
        if st.startswith('s ') or (not st.endswith('\t') and '\t' in line):
            sline = line if line.startswith('\t') else '\t' + st
            break
    if not sline:
        for line in block[1:]:
            if line.strip():
                sline = line if line.startswith('\t') else '\t' + line.strip()
                break
    return [header, sline if sline.strip() else '']


def normalize_svc_block(block):
    if not block:
        return block
    header = block[0].strip()
    name = ''
    flags = []
    for line in block[1:]:
        st = line.strip()
        if not st:
            continue
        if st.startswith(('p:', 'c:', 'C:', 'f:', 'v:')):
            flags.append(st)
        elif not name:
            name = st
        else:
            name = (name + ' ' + st).strip()
    out = [header, name, ','.join(flags) if flags else '']
    return out


def merge_lamedb(path, tp_entries, svc_entries):
    with open(path, 'r', encoding='utf-8', errors='surrogateescape') as f:
        lines = f.read().split('\n')
    fmt, tp_blocks, svc_blocks = parse_lamedb(lines)
    generated_tp_keys = {key for key, _ in tp_entries}
    original_tp_keys = set(tp_blocks)
    original_svc_keys = set(svc_blocks)
    for key in list(tp_blocks):
        if key in generated_tp_keys:
            del tp_blocks[key]
    for key in list(svc_blocks):
        block = svc_blocks[key]
        name = ''
        if len(block) > 1:
            candidate = block[1]
            if candidate.strip() and not candidate.startswith(SERVICE_FLAG_PREFIXES):
                name = candidate.strip()
        if 'abertis' in name.lower():
            del svc_blocks[key]
    replaced = sum(1 for key, _ in tp_entries if key in original_tp_keys) \
        + sum(1 for key, _ in svc_entries if key in original_svc_keys)
    for key, block in tp_entries:
        tp_blocks[key] = block
    for key, block in svc_entries:
        svc_blocks[key] = block
    out = ['eDVB services /4/', 'transponders']
    for block in tp_blocks.values():
        out.extend(normalize_tp_block(block))
        out.append('/')
    out.extend(('end', 'services'))
    for block in svc_blocks.values():
        out.extend(normalize_svc_block(block))
    out.append('end')
    shutil.copy2(path, path + '.bak')
    with open(path, 'w', encoding='utf-8', errors='surrogateescape') as f:
        f.write('\n'.join(out) + '\n')
    return len(tp_blocks), len(svc_blocks), replaced


def edit_lamedb(path, tp_entries, svc_entries):
    with open(path, 'r', encoding='utf-8', errors='surrogateescape') as f:
        lines = f.read().split('\n')
    fmt, tp_blocks, svc_blocks = parse_lamedb(lines)
    original_tp_keys = set(tp_blocks)
    original_svc_keys = set(svc_blocks)
    generated_tp_keys = {key for key, _ in tp_entries}
    removed_tp = 0
    for key in list(tp_blocks):
        if key in generated_tp_keys:
            removed_tp += 1
            del tp_blocks[key]
    removed_svc = 0
    for key in list(svc_blocks):
        block = svc_blocks[key]
        name = ''
        if len(block) > 1:
            candidate = block[1]
            if candidate.strip() and not candidate.startswith(SERVICE_FLAG_PREFIXES):
                name = candidate.strip()
        if 'abertis' in name.lower():
            removed_svc += 1
            del svc_blocks[key]
    replaced = (sum(1 for key, _ in tp_entries if key in original_tp_keys)
                + sum(1 for key, _ in svc_entries if key in original_svc_keys))
    for key, block in tp_entries:
        tp_blocks[key] = block
    for key, block in svc_entries:
        svc_blocks[key] = block
    out = ['eDVB services /4/', 'transponders']
    for block in tp_blocks.values():
        out.extend(normalize_tp_block(block))
        out.append('/')
    out.extend(('end', 'services'))
    for block in svc_blocks.values():
        out.extend(normalize_svc_block(block))
    out.append('end')
    shutil.copy2(path, path + '.bak')
    with open(path, 'w', encoding='utf-8', errors='surrogateescape') as f:
        f.write('\n'.join(out) + '\n')
    return len(tp_blocks), len(svc_blocks), replaced, removed_tp, removed_svc


APP_NAME = 'Abertis / Cellnex DX Suite'
APP_SUBTITLE = 'Hispasat 30.0\u00b0W \u00b7 streamrelay + astra relay'
SETTINGS_FILE = os.path.join(os.path.expanduser('~'), '.abertis_dx_suite_gui.json')

DEFAULTS = {
    'source_mode': 'url',
    'url': DEFAULT_JSON_URL,
    'file': '',
    'out_dir': '.',
    'bouquet': 'userbouquet.abertis.tv',
    'merge_enabled': False,
    'merge_path': '/etc/enigma2/lamedb',
    'edit_enabled': False,
    'edit_path': '/etc/enigma2/lamedb',
    'lamedb_format': 'v4',
    'position': -300,
    'softcam_key_enabled': False,
    'softcam_key_path': '',
    'relay_host': 'localhost',
    'relay_port': 8001,
    'out_host': '0.0.0.0',
    'http_port': 9999,
    'relay_script': '',
    't2mi_plp': '',
    't2mi_pnr': '',
    'gui_scale': 1.0,
}


class SuiteError(Exception):
    pass


SECTIONS = [
    ('source', '1 \u00b7 Data source'),
    ('output', '2 \u00b7 Output'),
    ('sat', '3 \u00b7 Satellite'),
    ('relay', '4 \u00b7 Relay (streamrelay \u2192 astra)'),
]

OPTIONS = OrderedDict((
    ('source_mode', {
        'section': 'source', 'label': 'Database source',
        'help': 'Where the channel database comes from: download the live '
                'sat_data.json from GitHub Pages, or parse a copy saved on '
                'disk. The local file is the way to go on offline receivers '
                'or when you want to pin one specific version of the list.'}),
    ('url', {
        'section': 'source', 'label': 'JSON URL',
        'help': 'URL of the sat_data.json database (transponders, stream '
                'PIDs, services). Default is the official Abertis DTT list '
                'published on GitHub Pages; it also carries the Cellnex '
                'transponders.'}),
    ('file', {
        'section': 'source', 'label': 'Local JSON file',
        'help': 'Path to a sat_data.json saved locally. Used when "Local '
                'file" is selected. Tip: download the URL once, keep the '
                'file next to the receiver and re-run whenever the platform '
                'moves or adds streams.'}),
    ('out_dir', {
        'section': 'output', 'label': 'Output directory',
        'help': 'Directory that receives astra.conf, the bouquet and '
                '(outside merge mode) the standalone lamedb. Point it at a '
                'staging folder, or directly at /etc/enigma2 if you manage '
                'the files in place.'}),
    ('merge_enabled', {
        'section': 'output', 'label': 'Merge into existing lamedb',
        'help': 'Instead of writing a standalone lamedb, inject the '
                'generated transponders and feed services into an existing '
                'Enigma2 lamedb. Matching entries are replaced, everything '
                'else is preserved and a .bak backup is written first. '
                'Recommended for a receiver in daily use; restart Enigma2 '
                'after merging.'}),
    ('merge_path', {
        'section': 'output', 'label': 'lamedb to merge',
        'help': 'The lamedb file to merge into, normally '
                '/etc/enigma2/lamedb on the box. A backup is saved as '
                '<file>.bak before anything is touched.'}),
    ('edit_enabled', {
        'section': 'output', 'label': 'Edit external lamedb',
        'help': 'Instead of writing a standalone lamedb, edit an existing '
                'Enigma2 lamedb in place: remove transponders matching the '
                'generated ones and services whose name contains "abertis", '
                'then inject the generated entries. A .bak backup is written '
                'first.'}),
    ('edit_path', {
        'section': 'output', 'label': 'lamedb to edit',
        'help': 'The lamedb file to edit, normally /etc/enigma2/lamedb on '
                'the box. A backup is saved as <file>.bak before anything is '
                'touched.'}),
    ('lamedb_format', {
        'section': 'output', 'label': 'lamedb format',
        'help': 'Container layout of a standalone lamedb: "v4" is the '
                'sectioned format (header /4/, literal transponders / '
                'services / end keywords, one / after every transponder) '
                'written and required by current OpenATV, OpenViX, OpenBH '
                'and recent OpenPLi images. Only v4 is supported now. '
                'Transponder and service blocks are identical either way, '
                'so this setting only affects the standalone file.'}),
    ('bouquet', {
        'section': 'output', 'label': 'Bouquet file name',
        'help': 'File name of the generated Enigma2 bouquet. Register it '
                'from bouquets.tv with:\n'
                '  #SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "<name>" '
                'ORDER BY bouquet'}),
    ('softcam_key_enabled', {
        'section': 'output', 'label': 'Write SoftCam.Key',
        'help': 'Also generate a SoftCam.Key file with one dummy BISS entry '
                'per BISS-encrypted stream. The key ID is\n'
                '  CRC32(seed=0x2600, SID + Enigma2 namespace)\n'
                'written big-endian as `F <id> 00000000 0000000000000000 ;comment`.\n'
                'Replace the dummy key `0000000000000000` with the real BISS keys. '
                'Only standard-library zlib CRC-32 is used.'}),
    ('softcam_key_path', {
        'section': 'output', 'label': 'SoftCam.Key path',
        'help': 'Output path for SoftCam.Key. Leave empty to write '
                'SoftCam.Key next to the other outputs (in --out-dir).'}),
    ('position', {
        'section': 'sat', 'label': 'Orbital position',
        'help': 'Orbital position in tenths of a degree, negative = West. '
                'Hispasat 30.0\u00b0W = -300. The value drives the DVB '
                'namespace (30\u00b0W \u2192 0x0CE40000) that binds every '
                'feed service to its transponder and appears inside the '
                'astra input service references.'}),
    ('relay_host', {
        'section': 'relay', 'label': 'Streamrelay host / port',
        'help': 'Address of the Enigma2 streamrelay that astra pulls the '
                'tuned service from \u2014 the make_channel input becomes '
                'http://<host>:<port>/<service-ref>:. Default localhost:8001 '
                'is the streamrelay port of modern OE images; change the '
                'host when astra runs on another machine than the receiver.'}),
    ('out_host', {
        'section': 'relay', 'label': 'astra HTTP output host / port',
        'help': 'Address astra binds its HTTP outputs on. 0.0.0.0 listens '
                'on every interface \u2014 needed whenever Enigma2 (or any '
                'other player) is not on the same machine as astra. Every '
                'PID is published as /<provider>/pid<PID> beneath this '
                'port, and the bouquet points exactly there.'}),
    ('relay_script', {
        'section': 'relay', 'label': 'Relay script override',
        'help': 'Pipe script under /etc/astra/scripts/ that processes each '
                'feed, e.g. "/etc/astra/scripts/abertis 2025". Leave empty '
                'to derive it per stream from the JSON provider field '
                '(abertis, cellnex). Set a fixed name only if your box '
                'ships a single script that handles every provider.'}),
    ('t2mi_plp', {
        'section': 'relay', 'label': 'Default T2MI PLP id',
        'help': 'Default PLP id used by make_t2mi_decap for T2MI '
                'transponders when the JSON stream does not carry a "plp" '
                'field. Leave empty to use 0.'}),
    ('t2mi_pnr', {
        'section': 'relay', 'label': 'Default T2MI program number',
        'help': 'Default program number used by make_t2mi_decap for T2MI '
                'transponders when the JSON stream does not carry a '
                '"t2mi_pnr" field. Leave empty to use 0.'}),
    ('gui_scale', {
        'section': 'output', 'label': 'GUI scale factor',
        'help': 'Multiplier for all fonts and widgets of this window (for '
                'example 1.5 on a HiDPI / small display). Values range from '
                '0.5 up to 3.0. The setting is saved and applied on the next '
                'start for a fully consistent layout; the Apply button also '
                'updates the running window. Use the Apply scale button on the '
                'status bar to change it live.'}),

))

ACTION_HELP = {
    'load': 'Fetch / parse the database and build the preview tree '
            '(Ctrl+L). Everything the engine prints \u2014 warnings, skipped '
            'channel lists, PLS notices \u2014 lands in the Log tab.',
    'generate': 'Write lamedb (or merge into the receiver lamedb), astra.conf '
                'and the bouquet using the current settings (Ctrl+G). Runs a '
                'load first if no database is loaded yet.',
    'cli': 'Build the equivalent Abertis_DX_Suite.py command line for the '
           'current settings and copy it to the clipboard (Ctrl+K) \u2014 '
           'handy to repeat the exact same run from a shell or cron.',
}

QUICK_GUIDE = """\
HOW TO USE THE DX SUITE

 1. Load the database (Ctrl+L)
    Downloads sat_data.json from GitHub Pages (or reads your local copy)
    and shows every transponder, PID stream and channel in the Preview
    tab. Double-click a PID to inspect the exact lamedb / astra.conf /
    bouquet lines that will be generated for it.

 2. Pick the output mode
    * Standalone lamedb  - a fresh lamedb with only the feed services,
      meant to replace /etc/enigma2/lamedb on a dedicated dx box.
    * Merge mode         - injects/updates the entries inside the lamedb
      of your daily receiver (a .bak backup is written first).

 3. Check the relay settings
    astra pulls each feed service from streamrelay
    (http://localhost:8001/<sref>:), pipes it through
    /etc/astra/scripts/<provider> <PID> and republishes it per PID on
    http://0.0.0.0:9999/<provider>/pid<PID>. The bouquet references
    those URLs, so host/port must be reachable from Enigma2.

 4. Generate (Ctrl+G), then
    * restart Enigma2 so the new services appear,
    * start astra with the generated astra.conf,
    * copy the bouquet to /etc/enigma2/ and reference it in bouquets.tv:
      #SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.abertis.tv" ORDER BY bouquet

NOTES
 * Feed services take their SID and component PID from the JSON "sid" and
   "cpid" fields of each stream (decimal). The lamedb flags carry the raw
   stream PID in cache entry c:18 (cDATAPID) and the cpid in c:19
   (cPMTPID), plus CAID 2600 on BISS feeds. Missing/empty fields fall
   back to SID = PID+1000 and cpid = PID+5000.
 * Entries without a "Stream <pid>" title in the JSON (e.g. "RNE",
   "TDT Aragon") are whole-transponder channel lists: their transponder
   is created, but there is no single PID to relay.
 * lamedb v3 cannot carry PLS/MISO parameters - the 11491 V Cellnex
   carrier is flagged with a warning in the log.
 * The standalone lamedb is written in v4 layout (transponders/services/
   end, one / per transponder) as used by current images; switch to v3
   for older ones. Merging keeps the format of the existing lamedb.
 * Every setting is remembered between runs (~/.abertis_dx_suite_gui.json)
   and can be saved/loaded as a profile from the File menu.
"""


def position_label(tenths):
    try:
        tenths = int(tenths)
    except (TypeError, ValueError):
        return '?'
    return '%.1f\u00b0 %s' % (abs(tenths) / 10.0, 'W' if tenths < 0 else 'E')


def validate_values(v):
    problems = []
    if v['source_mode'] == 'file':
        if not v['file'].strip():
            problems.append('Local JSON file is selected but no path is given.')
        elif not os.path.isfile(os.path.expanduser(v['file'].strip())):
            problems.append('Local JSON file not found: %s' % v['file'])
    else:
        url = v['url'].strip()
        if not url:
            problems.append('JSON URL is empty.')
        elif not url.startswith(('http://', 'https://')):
            problems.append('JSON URL must start with http:// or https://')
    if not v['out_dir'].strip():
        problems.append('Output directory is empty.')
    if v['merge_enabled'] and not v['merge_path'].strip():
        problems.append('Merge mode is enabled but no lamedb path is given.')
    if v.get('edit_enabled') and v.get('edit_path', '').strip() and \
            not os.path.isfile(os.path.expanduser(v['edit_path'].strip())):
        problems.append('lamedb to edit not found: %s' % v['edit_path'])
    if v.get('edit_enabled') and not str(v.get('edit_path') or '').strip():
        problems.append('Edit mode is enabled but no lamedb path is given.')
    try:
        pos = int(v['position'])
        if not -1800 <= pos <= 1800:
            problems.append('Orbital position must be between -1800 and 1800 '
                            '(tenths of a degree).')
    except (TypeError, ValueError):
        problems.append('Orbital position must be an integer (tenths of a '
                        'degree, e.g. -300 for 30.0\u00b0W).')
    for key, what in (('relay_port', 'Streamrelay port'),
                      ('http_port', 'astra HTTP port')):
        try:
            if not 1 <= int(v[key]) <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            problems.append('%s must be a TCP port between 1 and 65535.' % what)
    if not v['relay_host'].strip() or not v['out_host'].strip():
        problems.append('Relay host and astra output host must not be empty.')
    return problems


def values_namespace(v):
    def to_int(value, fallback):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return fallback
    return types.SimpleNamespace(
        position=to_int(v['position'], DEFAULTS['position']),
        relay_host=v['relay_host'].strip() or DEFAULTS['relay_host'],
        relay_port=to_int(v['relay_port'], DEFAULTS['relay_port']),
        out_host=v['out_host'].strip() or DEFAULTS['out_host'],
        http_port=to_int(v['http_port'], DEFAULTS['http_port']),
        relay_script=v['relay_script'].strip() or None,
        t2mi_plp=to_int(v.get('t2mi_plp') or '', None),
        t2mi_pnr=to_int(v.get('t2mi_pnr') or '', None))


def build_cli_command(v):
    try:
        position = str(int(str(v['position']).strip()))
    except (TypeError, ValueError):
        position = str(DEFAULTS['position'])
    try:
        relay_port = str(int(str(v['relay_port']).strip()))
    except (TypeError, ValueError):
        relay_port = str(DEFAULTS['relay_port'])
    try:
        http_port = str(int(str(v['http_port']).strip()))
    except (TypeError, ValueError):
        http_port = str(DEFAULTS['http_port'])
    parts = ['python3', shlex.quote(os.path.join(SCRIPT_DIR, 'Abertis_DX_Suite.py'))]
    if v['source_mode'] == 'file' and v['file'].strip():
        parts += ['--file', shlex.quote(v['file'].strip())]
    elif v['url'].strip() and v['url'].strip() != DEFAULTS['url']:
        parts += ['--url', shlex.quote(v['url'].strip())]
    parts += ['--out-dir', shlex.quote(v['out_dir'].strip() or '.')]
    if v['merge_enabled'] and v['merge_path'].strip():
        parts += ['--merge-lamedb', shlex.quote(v['merge_path'].strip())]
    if v.get('edit_enabled') and str(v.get('edit_path') or '').strip():
        parts += ['--edit-lamedb', shlex.quote(v['edit_path'].strip())]
    parts += ['--position', position]
    parts += ['--relay-host', shlex.quote(v['relay_host'].strip()),
              '--relay-port', relay_port]
    parts += ['--out-host', shlex.quote(v['out_host'].strip()),
              '--http-port', http_port]
    if v['relay_script'].strip():
        parts += ['--relay-script', shlex.quote(v['relay_script'].strip())]
    if v.get('t2mi_plp'):
        parts += ['--t2mi-plp', shlex.quote(str(v['t2mi_plp']))]
    if v.get('t2mi_pnr'):
        parts += ['--t2mi-pnr', shlex.quote(str(v['t2mi_pnr']))]
    if v['bouquet'].strip() and v['bouquet'].strip() != DEFAULTS['bouquet']:
        parts += ['--bouquet', shlex.quote(v['bouquet'].strip())]
    if v.get('softcam_key_enabled'):
        parts += ['--softcam-key']
        if v.get('softcam_key_path'):
            parts += [shlex.quote(v['softcam_key_path'])]
    return ' '.join(parts)


def load_database(v):
    try:
        if v['source_mode'] == 'file':
            with open(os.path.expanduser(v['file'].strip()), 'r',
                      encoding='utf-8') as f:
                data = json.load(f)
        else:
            print('Fetching %s ...' % v['url'].strip())
            req = urllib.request.Request(
                v['url'].strip(), headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode('utf-8'))
    except (OSError, ValueError) as exc:
        raise SuiteError('Cannot load the database: %s' % exc)
    if not isinstance(data, list):
        raise SuiteError('Unexpected JSON layout - expected a list of '
                         'transponders.')
    txps = parse_transponders(data)
    streams = collect_streams(txps)
    if not txps or not streams:
        raise SuiteError('No transponders/streams found in the source data.')
    raw_by_pid = {}
    for tp in txps:
        for raw in tp['streams']:
            m = STREAM_PID_RE.search(raw.get('title') or '')
            if m:
                raw_by_pid[int(m.group(1))] = raw
    return data, txps, streams, raw_by_pid


def generate_artifacts(v, txps, streams):
    ns = values_namespace(v)
    out_dir = os.path.expanduser(v['out_dir'].strip() or '.')
    bouquet_name = v['bouquet'].strip() or DEFAULTS['bouquet']
    tp_entries, svc_entries = build_lamedb_entries(txps, streams, ns.position)

    outputs = OrderedDict()
    written = []
    summary = []
    merged = None
    edited = None
    merge_path = os.path.expanduser(v['merge_path'].strip()) \
        if v['merge_enabled'] and v['merge_path'].strip() else None
    edit_path = os.path.expanduser(v['edit_path'].strip()) \
        if v.get('edit_enabled') and str(v.get('edit_path') or '').strip() else None
    try:
        os.makedirs(out_dir, exist_ok=True)
        if merge_path:
            if not os.path.isfile(merge_path):
                raise SuiteError('lamedb to merge not found: %s' % merge_path)
            merged = merge_lamedb(merge_path, tp_entries, svc_entries)
            summary.append('lamedb: merged %d transponders / %d services into '
                           '%s (%d replaced, backup .bak written)'
                           % (merged[0], merged[1], merge_path, merged[2]))
        if edit_path:
            if not os.path.isfile(edit_path):
                raise SuiteError('lamedb to edit not found: %s' % edit_path)
            edited = edit_lamedb(edit_path, tp_entries, svc_entries)
            merged = edited
            summary.append('lamedb: edited %d transponders / %d services in '
                           '%s (%d removed transponders, %d abertis services '
                           'removed, backup .bak written)'
                           % (edited[0], edited[1], edit_path, edited[3],
                              edited[4]))
        if not merge_path and not edit_path:
            fmt = v.get('lamedb_format') or 'v4'
            text = lamedb_text(tp_entries, svc_entries, fmt)
            path = os.path.join(out_dir, 'lamedb')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            outputs['lamedb'] = text
            written.append(path)
            summary.append('lamedb (format %s): %d transponders, %d feed '
                           'services (%s)' % (fmt, len(tp_entries),
                                              len(svc_entries), path))
        astra = astra_text(streams, ns)
        path = os.path.join(out_dir, 'astra.conf')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(astra)
        outputs['astra.conf'] = astra
        written.append(path)
        summary.append('astra.conf: %d relay channels (%s)' % (len(streams), path))
        bouquet = bouquet_text(streams, ns)
        path = os.path.join(out_dir, bouquet_name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(bouquet)
        outputs[bouquet_name] = bouquet
        written.append(path)
        summary.append('bouquet: %s (%s)' % (bouquet_name, path))

        softcam_enabled = v.get('softcam_key_enabled', False)
        softcam_path = v.get('softcam_key_path') or ''
        if softcam_enabled:
            sk_path = softcam_path or os.path.join(out_dir, 'SoftCam.Key')
            sk_text = softcam_text(streams)
            with open(sk_path, 'w', encoding='utf-8') as f:
                f.write(sk_text)
            outputs['SoftCam.Key'] = sk_text
            written.append(sk_path)
            n_biss = sum(1 for s in streams
                         if 'BISS' in (s.get('encryption') or '').upper())
            summary.append('SoftCam.Key: %d dummy BISS entries (%s)'
                           % (n_biss, sk_path))
    except SystemExit as exc:
        raise SuiteError(str(exc.code) if exc.code else 'generation failed')
    except OSError as exc:
        raise SuiteError('Cannot write output: %s' % exc)
    return {'outputs': outputs, 'written': written,
            'summary': summary, 'merged': merged, 'edited': edited}


CHECK_FIXTURE = [
    {'txp_no': '125', 'freq_pol_mode': '11222.00 H DVB-S2/8PSK',
     'provider': 'Abertis DTT', 'sr_fec': '30000 3/4',
     'onid_nid_tid': '1/60000/60020', 'date': '25.04.2026',
     'streams': [
         {'title': 'Stream 2025 - (HS125.25) - RGE Extremadura',
          'pid_info': '8916-1109--12566', 'encryption': 'BISS',
          'sid': '3025', 'cpid': '7025',
          'channels': [
              {'type': 'HD', 'name': 'TVE La 1 Extremadura', 'system': 'MPEG-4',
               'sid': '504', 'vpid': '301', 'apid_lang': '302 Spa 303 org'},
              {'type': 'Radio', 'name': 'RNE Radio 1 Extremadura', 'system': '',
               'sid': '505', 'vpid': '', 'apid_lang': '2001 Spa'}]}]},
    {'txp_no': '200', 'freq_pol_mode': '11707.00 V DVB-S2/8PSK T2MI',
     'provider': 'Abertis DTT', 'sr_fec': '27500 3/4',
     'onid_nid_tid': '1/60000/60021', 'date': '25.04.2026',
     'streams': [
         {'title': 'Stream 4000 - T2MI test',
          'pid_info': '8916-1--60021', 'encryption': '',
          'sid': '5000', 'cpid': '9000', 'plp': '1', 't2mi_pid': '257', 't2mi_pnr': '1',
          'channels': [
              {'type': 'HD', 'name': 'T2MI Test TV', 'system': 'MPEG-4', 'sid': '10',
               'vpid': '500', 'apid_lang': '501 Spa'}]}]},
    {'txp_no': '199', 'freq_pol_mode': '11502.12 V DVB-S2/8PSK',
     'provider': 'Cellnex DTT', 'sr_fec': '10200 3/4',
     'onid_nid_tid': '12572/8916/1', 'date': '29.04.2026',
     'streams': [
         {'title': 'Stream 3150 - (HS199.1) - MAUT', 'pid_info': '8916-9--12573',
          'encryption': '', 'sid': '4200', 'cpid': '8150', 'channels': [
              {'type': 'HD', 'name': 'CMM TV', 'system': 'MPEG-4', 'sid': '2',
               'vpid': '408', 'apid_lang': '456 Spa'}]}]},
]


def run_checks():
    print('== Abertis_DX_Suite_GUI --check ==')
    for key, opt in OPTIONS.items():
        assert opt['label'] and opt['section'] in dict(SECTIONS), key
        assert len(opt['help']) > 60, ('help text too short', key)
        assert key in DEFAULTS, ('missing default', key)
    assert all(len(h) > 40 for h in ACTION_HELP.values())
    print('PASS  option registry: %d options + %d actions documented'
          % (len(OPTIONS), len(ACTION_HELP)))
    assert position_label(-300) == '30.0\u00b0 W'
    assert position_label(192) == '19.2\u00b0 E'
    print('PASS  position labels')
    bad = dict(DEFAULTS, source_mode='file', relay_port='notaport',
               position='-9999', file='/nope.json')
    problems = validate_values(bad)
    assert len(problems) == 3, problems
    assert not validate_values(DEFAULTS)
    print('PASS  validation')
    v = dict(DEFAULTS, source_mode='file', file='/tmp/sat_data.json',
             out_dir='/tmp/dx', merge_enabled=True,
             merge_path='/etc/enigma2/lamedb', relay_script='abertis')
    cmd = build_cli_command(v)
    assert '--file /tmp/sat_data.json' in cmd
    assert '--merge-lamedb /etc/enigma2/lamedb' in cmd
    assert '--position -300' in cmd and '--relay-script abertis' in cmd
    assert build_cli_command(DEFAULTS) == (
        'python3 %s --out-dir . --position -300 --relay-host localhost '
        '--relay-port 8001 --out-host 0.0.0.0 --http-port 9999'
        % shlex.quote(os.path.join(SCRIPT_DIR, 'Abertis_DX_Suite.py')))
    print('PASS  CLI command bridge')
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        fixture = os.path.join(tmp, 'fixture.json')
        with open(fixture, 'w', encoding='utf-8') as f:
            json.dump(CHECK_FIXTURE, f)
        v = dict(DEFAULTS, source_mode='file', file=fixture,
                 out_dir=os.path.join(tmp, 'out'))
        _data, txps, streams, raw_by_pid = load_database(v)
        assert len(txps) == 3 and len(streams) == 3, (len(txps), len(streams))
        assert 2025 in raw_by_pid and 3150 in raw_by_pid and 4000 in raw_by_pid
        result = generate_artifacts(v, txps, streams)
        lam = result['outputs']['lamedb']
        assert lam.startswith('eDVB services /4/\ntransponders\n'
                              '0ce40000:ea74:0001')
        assert '\nend\nservices\n' in lam and lam.rstrip('\n').endswith('end')
        assert '/\n0ce40000:0001:311c' in lam
        astx = result['outputs']['astra.conf']
        bq = result['outputs']['userbouquet.abertis.tv']
        assert ('0bd1:0ce40000:ea74:0001:1:0\n'
                'Abertis@11222H30000 PID 2025\n'
                'p:ABERTIS,c:031fff,c:1807e9,c:191b71,C:2600,f:01\n') in lam
        assert 'Cellnex@11502V10200 PID 3150' in lam
        assert ('1068:0ce40000:0001:311c:1:0\n'
                'Cellnex@11502V10200 PID 3150\n'
                'p:CELLNEX,c:031fff,c:180c4e,c:191fd6,f:01\n') in lam
        assert '1:0:1:BD1:EA74:1:CE40000:0:0:0' in astx
        assert '1:0:1:1068:1:12572:CE40000:0:0:0' in astx
        assert '/etc/astra/scripts/cellnex 3150' in astx
        assert '/cellnex/pid3150' in astx
        assert astx.count('make_t2mi_decap') == 3
        assert '1:0:1:1388:EA75:1:CE40000:0:0:0' in astx
        assert 't2mi://f11222aber2025pid4096plp0' in astx
        assert '-- 11222 H 30000 PID2025 Stream' in astx
        assert 'name = "Abertis@11222H30000PID2025"' in astx
        assert 'name = "11222H30000@ABER2025_PID4096PLP0"' in astx
        assert 'name = "11222h30000@aber2025_pid4096plp0"' in astx
        assert 't2mi://f11502cell3150pid4096plp0' in astx
        assert 'name = "11502V10200@CELL3150_PID4096PLP0"' in astx
        assert 't2mi://f11707aber4000pid257plp1' in astx
        assert 'http://0.0.0.0:9999/abertis/aber2025_pid4096plp0' in astx
        assert 'http://0.0.0.0:9999/cellnex/cell3150_pid4096plp0' in astx
        assert 'http://0.0.0.0:9999/abertis/aber4000_pid257plp1' in astx
        assert 'http%3A//0.0.0.0%3A9999/abertis/aber2025_pid4096plp0:(TV) TVE La 1 Extremadura' in bq
        assert 'http%3A//0.0.0.0%3A9999/cellnex/cell3150_pid4096plp0:(TV) CMM TV' in bq
        assert 'http%3A//0.0.0.0%3A9999/abertis/aber4000_pid257plp1:(TV) T2MI Test TV' in bq
        assert '(TV) TVE La 1 Extremadura' in bq
        assert '(RADIO) RNE Radio 1 Extremadura' in bq
        for p in result['written']:
            assert os.path.isfile(p), p
        print('PASS  generation round on fixture (standalone mode)')
        v_sk = dict(v, softcam_key_enabled=True)
        result_sk = generate_artifacts(v_sk, txps, streams)
        assert 'SoftCam.Key' in result_sk['outputs'], list(result_sk['outputs'])
        sk = result_sk['outputs']['SoftCam.Key']
        assert sk.startswith('; SoftCam.Key generated by the Abertis DX Suite')
        assert 'F 65ADC3CF 00000000 0000000000000000 ;Abertis@11222H30000 PID 2025' in sk
        assert not any('PID 3150' in line for line in sk.splitlines() if line.startswith('F '))
        sk_path = os.path.join(tmp, 'out', 'SoftCam.Key')
        assert os.path.isfile(sk_path)
        print('PASS  SoftCam.Key dummy BISS entries')
        existing = os.path.join(tmp, 'lamedb')
        with open(existing, 'w', encoding='utf-8') as f:
            f.write('eDVB services /4/\n'
                    'transponders\n'
                    '00c00000:0f4c:0001\n'
                    '\ts 10744000:22000000:0:5:192:2:0:0:0:0:0\n'
                    '/\n'
                    'end\n'
                    'services\n'
                    '012c:00c00000:0f4c:0001:1:0\nZDF\n'
                    'end\n')
        result2 = generate_artifacts(dict(v, merge_enabled=True,
                                          merge_path=existing), txps, streams)
        assert result2['merged'] and result2['merged'][2] == 0
        with open(existing, encoding='utf-8') as f:
            merged = f.read()
        assert merged.startswith('eDVB services /4/\ntransponders\n')
        assert 'ZDF' in merged and '00c00000:0f4c:0001' in merged
        assert 'Abertis@11222H30000 PID 2025' in merged
        assert 'end\nservices\n' in merged and merged.rstrip('\n').endswith('end')
        assert os.path.isfile(existing + '.bak')
        editfile = os.path.join(tmp, 'lamedb_edit')
        with open(editfile, 'w', encoding='utf-8') as f:
            f.write('eDVB services /4/\n'
                    'transponders\n'
                    '0ce40000:ea74:0001\n'
                    '\ts 11222000:30000000:0:3:-300:2:0:0:0:0:0\n'
                    '/\n'
                    'end\n'
                    'services\n'
                    '0bd1:0ce40000:ea74:0001:1:0\nAbertis@11222H30000 PID 2025 (old)\n'
                    'p:ABERTIS,c:031fff,c:1807e9,c:191b71,C:2600,f:01\n'
                    '012c:00c00000:0f4c:0001:1:0\nZDF\n'
                    'end\n')
        result3 = generate_artifacts(dict(v, merge_enabled=False,
                                          edit_enabled=True,
                                          edit_path=editfile), txps, streams)
        assert result3['edited'] is not None
        with open(editfile, encoding='utf-8') as f:
            edited = f.read()
        assert edited.startswith('eDVB services /4/\ntransponders\n')
        assert 'ZDF' in edited and 'Abertis@11222H30000 PID 2025' in edited
        assert 'old' not in edited
        assert edited.count('\n0ce40000:ea74:0001\n') == 1
        print('PASS  generation round on fixture (edit mode cleanup)')
    print('ALL CHECKS PASSED')


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class WorkerThread(QThread):
    ok = pyqtSignal(object)
    fail = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, kind, task):
        super().__init__()
        self.kind = kind
        self.task = task

    def run(self):
        try:
            payload = self.task()
            self.ok.emit((self.kind, payload))
        except SuiteError as exc:
            self.fail.emit(str(exc))
        except Exception:
            self.fail.emit('Unexpected error:\n' + traceback.format_exc())


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

if __name__ == '__main__' and '--check' in sys.argv:
    run_checks()
    sys.exit(0)


class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('%s \u2014 %s' % (APP_NAME, APP_SUBTITLE))
        self.setMinimumSize(1120, 700)
        self.C = dict(THEME)

        self.q = queue.Queue()
        self.data = None
        self.txps = None
        self.streams = None
        self.raw_by_pid = {}
        self.outputs = OrderedDict()
        self._stream_items = {}
        self._busy = False
        self.scale_factor = 1.0
        self._help_keys = {}
        self._scaled_sizes = []
        self._first_show = True
        self._split_user = False
        self._setting_sizes = False
        self._last_scale_msg = None

        # Debounced scale / filter timers keep the UI responsive while dragging.
        self._scale_timer = QTimer(self)
        self._scale_timer.setSingleShot(True)
        self._scale_timer.setInterval(220)
        self._scale_timer.timeout.connect(self._apply_gui_scale)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(180)
        self._filter_timer.timeout.connect(self._populate_tree)

        self._build_menu()
        self._build_ui()
        self._restore_settings()

        self.statusBar().showMessage('Ready \u2014 load the database (Ctrl+L) or press Generate (Ctrl+G).')
        self._poll_timer = self.startTimer(80)

    # ------------------------------------------------ fonts & style
    def _initial_gui_scale(self):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            scale = float(saved.get('values', {}).get(
                'gui_scale', DEFAULTS.get('gui_scale', 1.0)))
        except (OSError, ValueError, TypeError):
            scale = float(DEFAULTS.get('gui_scale', 1.0))
        if scale < 0.5 or scale > 2.0:
            scale = 1.0
        return scale

    def _setup_fonts(self, factor=None):
        if factor is None:
            factor = self.scale_factor
        fams = set(QFontDatabase.families())
        ui = next((f for f in ('Inter', 'Segoe UI', 'Noto Sans', 'Ubuntu',
                               'DejaVu Sans') if f in fams), 'Sans Serif')
        mono = next((f for f in ('JetBrains Mono', 'Cascadia Mono', 'Consolas',
                                 'DejaVu Sans Mono', 'Ubuntu Mono') if f in fams), 'Monospace')
        sc = factor

        def mk(family, base, weight=QFont.Weight.Normal):
            # Fractional point sizes so 1.0 -> 1.1 is actually visible.
            fnt = QFont(family)
            fnt.setPointSizeF(max(7.5, base * sc))
            fnt.setWeight(weight)
            return fnt
        self.fonts = {
            'base': mk(ui, 18),
            'small': mk(ui, 18),
            'bold': mk(ui, 18, QFont.Weight.DemiBold),
            'h1': mk(ui, 26, QFont.Weight.Bold),
            'mono': mk(ui, 18),
            'mono_small': mk(ui, 18),
        }
        # Keep menus, tooltips, dialogs and message boxes in step with the UI.
        app = QApplication.instance()
        if app is not None:
            app.setFont(self.fonts['base'])

    def _apply_style(self):
        C = self.C
        f = self.scale_factor

        def px(v):
            return max(1, int(round(v * f * UI_UNIT)))
        sheet = [
            'QMainWindow, QDialog { background: %s; }' % C['bg'],
            'QWidget { color: %s; }' % C['fg'],
            'QWidget#Root { background: %s; }' % C['bg'],
            'QWidget#Sidebar { background: %s; }' % C['bg'],
            'QWidget#TabPage { background: %s; }' % C['tree'],
            'QLabel { background: transparent; }',
            'QLabel#Muted { color: %s; }' % C['muted'],

            # ---- cards / group boxes
            'QGroupBox { background: %s; border: 1px solid %s; border-radius: %dpx;'
            ' margin-top: %dpx; padding: %dpx %dpx %dpx %dpx; font-weight: 600; }'
            % (C['surface'], C['border'], px(10), px(20), px(12), px(12), px(10), px(12)),
            'QGroupBox::title { subcontrol-origin: margin; left: %dpx; padding: 0 %dpx;'
            ' color: %s; }' % (px(12), px(6), C['accent']),

            # ---- inputs
            'QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {'
            ' background: %s; color: %s; border: 1px solid %s;'
            ' border-radius: %dpx; padding: %dpx %dpx;'
            ' selection-background-color: %s; selection-color: #ffffff; }'
            % (C['field'], C['fg'], C['border'], px(7), px(6), px(9), C['sel_hi']),
            'QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {'
            ' border-color: %s; }' % C['border_hi'],
            'QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {'
            ' border-color: %s; }' % C['accent'],
            'QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled,'
            ' QDoubleSpinBox:disabled { color: #6b7280; background: #191c23;'
            ' border-color: #232833; }',
            'QComboBox { background: #000000; padding-right: %dpx; }' % px(22),
            'QComboBox::drop-down { background: #000000; }',
            'QComboBox::drop-down { border: none; width: %dpx; }' % px(22),
            'QComboBox::down-arrow { image: none; width: 0; height: 0;'
            ' border-left: %dpx solid transparent; border-right: %dpx solid transparent;'
            ' border-top: %dpx solid %s; margin-right: %dpx; }'
            % (px(5), px(5), px(6), C['muted'], px(7)),
            'QComboBox QAbstractItemView { background: #000000; color: %s;'
            ' border: 1px solid %s; border-radius: %dpx; padding: %dpx;'
            ' selection-background-color: %s; selection-color: #ffffff; outline: 0; }'
            % (C['fg'], C['border_hi'], px(8), px(4), C['sel']),
            'QSpinBox::up-button, QDoubleSpinBox::up-button { subcontrol-origin: border;'
            ' subcontrol-position: top right; width: %dpx; border: none;'
            ' border-left: 1px solid %s; background: transparent; }' % (px(18), C['border']),
            'QSpinBox::down-button, QDoubleSpinBox::down-button { subcontrol-origin: border;'
            ' subcontrol-position: bottom right; width: %dpx; border: none;'
            ' border-left: 1px solid %s; background: transparent; }' % (px(18), C['border']),
            'QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: none; width: 0; height: 0;'
            ' border-left: %dpx solid transparent; border-right: %dpx solid transparent;'
            ' border-bottom: %dpx solid %s; }' % (px(4), px(4), px(5), C['muted']),
            'QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: none; width: 0; height: 0;'
            ' border-left: %dpx solid transparent; border-right: %dpx solid transparent;'
            ' border-top: %dpx solid %s; }' % (px(4), px(4), px(5), C['muted']),
            'QSpinBox::up-button:hover, QSpinBox::down-button:hover,'
            ' QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {'
            ' background: %s; }' % C['surface2'],

            # ---- buttons
            'QPushButton { background: %s; color: %s; border: 1px solid %s;'
            ' border-radius: %dpx; padding: %dpx %dpx; font-weight: 600; }'
            % (C['surface2'], C['fg'], C['border_hi'], px(8), px(7), px(15)),
            'QPushButton:hover { background: #2b3140; border-color: #414a5d; }',
            'QPushButton:pressed { background: #232833; }',
            'QPushButton:disabled { color: #6b7280; background: #191c23;'
            ' border-color: #232833; }',
            'QPushButton#Primary { background: %s; border: 1px solid %s; color: #071120; }'
            % (C['accent'], C['accent']),
            'QPushButton#Primary:hover { background: %s; border-color: %s; }'
            % (C['accent_hi'], C['accent_hi']),
            'QPushButton#Primary:pressed { background: %s; border-color: %s; }'
            % (C['accent_lo'], C['accent_lo']),
            'QPushButton#Primary:disabled { background: #24344f; border-color: #24344f;'
            ' color: #71809a; }',

            # ---- checkboxes / radios
            'QCheckBox, QRadioButton { background: transparent; spacing: %dpx; }' % px(8),
            'QCheckBox::indicator { width: %dpx; height: %dpx; border: 1px solid %s;'
            ' border-radius: %dpx; background: %s; }' % (px(16), px(16), C['border_hi'], px(4), C['field']),
            'QCheckBox::indicator:hover { border-color: %s; }' % C['accent'],
            'QCheckBox::indicator:checked { background: %s; border-color: %s; }'
            % (C['accent'], C['accent']),
            'QRadioButton::indicator { width: %dpx; height: %dpx; border: 1px solid %s;'
            ' border-radius: %dpx; background: %s; }' % (px(16), px(16), C['border_hi'], px(8), C['field']),
            'QRadioButton::indicator:hover { border-color: %s; }' % C['accent'],
            'QRadioButton::indicator:checked { border: %dpx solid %s; background: %s; }'
            % (px(5), C['accent'], C['field']),

            # ---- tree
            'QTreeWidget { background: %s; alternate-background-color: #151922; color: %s;'
            ' border: none; outline: 0; selection-background-color: %s;'
            ' selection-color: #ffffff; }' % (C['tree'], C['fg'], C['sel']),
            'QTreeWidget::item { padding: %dpx %dpx; border: none; }' % (px(4), px(2)),
            'QTreeWidget::item:hover { background: #1b2029; }',
            'QTreeWidget::item:selected { background: %s; color: #ffffff; }' % C['sel'],
            'QHeaderView::section { background: %s; color: %s; border: none;'
            ' border-bottom: 1px solid %s; padding: %dpx %dpx; font-weight: 600; }'
            % (C['surface'], C['muted'], C['border'], px(6), px(8)),

            # ---- text areas
            'QTextEdit { background: %s; color: %s; border: 1px solid %s;'
            ' border-radius: %dpx; padding: %dpx; selection-background-color: %s; }'
            % (C['tree'], C['fg'], C['border'], px(8), px(6), C['sel_hi']),

            # ---- tabs
            'QTabWidget::pane { border: 1px solid %s; border-radius: %dpx;'
            ' background: %s; top: -1px; }' % (C['border'], px(10), C['tree']),
            'QTabBar::tab { background: transparent; color: %s; padding: %dpx %dpx;'
            ' margin-right: %dpx; border: none; border-bottom: 2px solid transparent; }'
            % (C['muted'], px(8), px(16), px(4)),
            'QTabBar::tab:hover { color: %s; }' % C['fg'],
            'QTabBar::tab:selected { color: %s; border-bottom: 2px solid %s; }'
            % (C['fg'], C['accent']),

            # ---- menu / status
            'QMenuBar { background: %s; color: %s; border: none; padding: %dpx; }'
            % (C['surface'], C['muted'], px(2)),
            'QMenuBar::item { padding: %dpx %dpx; border-radius: %dpx; background: transparent; }'
            % (px(5), px(10), px(6)),
            'QMenuBar::item:selected { background: %s; color: %s; }' % (C['sel'], C['fg']),
            'QMenu { background: %s; color: %s; border: 1px solid %s;'
            ' border-radius: %dpx; padding: %dpx; }'
            % (C['surface'], C['fg'], C['border_hi'], px(8), px(6)),
            'QMenu::item { padding: %dpx %dpx; border-radius: %dpx; }'
            % (px(6), px(22), px(6)),
            'QMenu::item:selected { background: %s; }' % C['sel'],
            'QMenu::separator { height: 1px; background: %s; margin: %dpx %dpx; }'
            % (C['border'], px(4), px(8)),
            'QStatusBar { background: %s; color: %s; border-top: 1px solid %s; }'
            % (C['surface'], C['muted'], C['border']),
            'QStatusBar::item { border: none; }',

            # ---- scroll bars
            'QScrollArea { background: transparent; border: none; }',
            'QScrollArea > QWidget > QWidget { background: transparent; }',
            'QScrollBar:vertical { background: transparent; width: %dpx; margin: %dpx; }' % (px(10), px(2)),
            'QScrollBar::handle:vertical { background: #333a48; border-radius: %dpx;'
            ' min-height: %dpx; }' % (px(5), px(28)),
            'QScrollBar::handle:vertical:hover { background: #455066; }',
            'QScrollBar:horizontal { background: transparent; height: %dpx; margin: %dpx; }' % (px(10), px(2)),
            'QScrollBar::handle:horizontal { background: #333a48; border-radius: %dpx;'
            ' min-width: %dpx; }' % (px(5), px(28)),
            'QScrollBar::handle:horizontal:hover { background: #455066; }',
            'QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }',
            'QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }',

            # ---- splitter / progress / tooltip
            'QSplitter::handle { background: transparent; width: %dpx; }' % px(12),
            'QSplitter::handle:hover { background: %s; }' % C['border'],
            'QProgressBar { background: #191c23; border: 1px solid %s;'
            ' border-radius: %dpx; text-align: center; color: %s; }'
            % (C['border'], px(6), C['fg']),
            'QProgressBar::chunk { background: %s; border-radius: %dpx; }'
            % (C['accent'], px(5)),
            'QToolTip { background: %s; color: %s; border: 1px solid %s;'
            ' border-radius: %dpx; padding: %dpx; }'
            % (C['surface2'], C['fg'], C['border_hi'], px(6), px(6)),
        ]
        self.setStyleSheet('\n'.join(sheet))

    # -------------------------------------------------------- menu
    def _build_menu(self):
        menubar = self.menuBar()
        m_file = menubar.addMenu('File')
        m_file.addAction(QAction('Open local JSON\u2026', self, shortcut=QKeySequence('Ctrl+O'), triggered=self._menu_open_json))
        m_file.addSeparator()
        m_file.addAction(QAction('Save settings profile\u2026', self, triggered=self._save_profile))
        m_file.addAction(QAction('Load settings profile\u2026', self, triggered=self._load_profile))
        m_file.addSeparator()
        m_file.addAction(QAction('Quit', self, shortcut=QKeySequence('Ctrl+Q'), triggered=self.close))

        m_act = menubar.addMenu('Actions')
        m_act.addAction(QAction('Load database', self, shortcut=QKeySequence('Ctrl+L'), triggered=self._on_load))
        m_act.addAction(QAction('Generate suite', self, shortcut=QKeySequence('Ctrl+G'), triggered=self._on_generate))
        m_act.addAction(QAction('Copy CLI command', self, shortcut=QKeySequence('Ctrl+K'), triggered=self._on_cli))
        m_act.addSeparator()
        m_act.addAction(QAction('Clear log', self, triggered=self._clear_log))

        m_help = menubar.addMenu('Help')
        m_help.addAction(QAction('Option reference', self, shortcut=QKeySequence('F1'), triggered=self._show_reference))
        m_help.addAction(QAction('Quick guide', self, triggered=self._show_guide))
        m_help.addSeparator()
        m_help.addAction(QAction('About', self, triggered=self._show_about))

    # ------------------------------------------------------ layout
    def _build_ui(self):
        C = self.C
        self._setup_fonts()
        self._apply_style()

        central = QWidget()
        central.setObjectName('Root')
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 10, 14, 8)
        root.setSpacing(10)

        # ---- header
        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 2, 0)
        title = QLabel(APP_NAME)
        title.setFont(self.fonts['h1'])
        title.setStyleSheet('color: %s;' % C['accent'])
        self.title_label = title
        subtitle = QLabel(APP_SUBTITLE)
        subtitle.setFont(self.fonts['small'])
        subtitle.setStyleSheet('color: %s;' % C['muted'])
        self.subtitle_label = subtitle
        header.addWidget(title)
        header.addSpacing(10)
        header.addWidget(subtitle)
        header.addStretch()
        self.v_counts = QLabel('')
        self.v_counts.setStyleSheet('color: %s;' % C['muted'])
        header.addWidget(self.v_counts)
        root.addLayout(header)

        # ---- two panes (draggable splitter, left never collapses)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(12)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.splitterMoved.connect(self._on_splitter_moved)
        root.addWidget(self.splitter, stretch=1)

        # ---------- left panel (settings)
        left = QWidget()
        left.setObjectName('Sidebar')
        self.left_panel = left
        left_v = QVBoxLayout(left)
        left_v.setContentsMargins(0, 0, 0, 0)
        left_v.setSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_content = QWidget()
        self.scroll_content = scroll_content
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 6, 0)
        scroll_layout.setSpacing(10)

        self._build_left(scroll_layout, C)

        # help pane (inside the scroller: it is reached with the same
        # vertical scrollbar and grows together with the UI scale)
        helpbox = QGroupBox('Help \u2014 focus or hover an option')
        helpbox.setFont(self.fonts['bold'])
        self.help_box = helpbox
        help_v = QVBoxLayout(helpbox)
        self.help_title = QLabel('')
        self.help_title.setStyleSheet('color: %s;' % C['muted'])
        help_v.addWidget(self.help_title)
        self.help_text = QTextEdit()
        self.help_text.setReadOnly(True)
        self._scaled_size(self.help_text, 110, 'max_h')
        help_v.addWidget(self.help_text)
        scroll_layout.addWidget(helpbox)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        left_v.addWidget(scroll, stretch=1)

        # action buttons stay pinned below the scroller
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.btn_load = QPushButton('\u21bb  Load database')
        self.btn_gen = QPushButton('\u25b6  Generate suite')
        self.btn_gen.setObjectName('Primary')
        self.btn_cli = QPushButton('Copy CLI')
        btns.addWidget(self.btn_load)
        btns.addWidget(self.btn_gen)
        btns.addWidget(self.btn_cli)
        left_v.addLayout(btns)
        self.splitter.addWidget(left)

        # ---------- right panel (tabs)
        right = QTabWidget()
        right.setFont(self.fonts['base'])
        right.setDocumentMode(True)
        right.setMinimumWidth(0)
        self.right_panel = right

        # preview tab
        prev = QWidget()
        prev.setObjectName('TabPage')
        prev_v = QVBoxLayout(prev)
        prev_v.setContentsMargins(10, 8, 10, 10)
        prev_v.setSpacing(8)
        prev_bar = QHBoxLayout()
        prev_bar.setSpacing(8)
        flab = QLabel('Filter')
        flab.setStyleSheet('color: %s;' % C['muted'])
        prev_bar.addWidget(flab)
        self.v_filter = QLineEdit()
        self.v_filter.setPlaceholderText('Type to filter transponders, streams and channels\u2026')
        self.v_filter.setClearButtonEnabled(True)
        self.v_filter.textChanged.connect(lambda *_: self._filter_timer.start())
        prev_bar.addWidget(self.v_filter, stretch=1)
        self.v_tree_info = QLabel('No database loaded yet.')
        self.v_tree_info.setStyleSheet('color: %s;' % C['muted'])
        prev_bar.addWidget(self.v_tree_info)
        prev_v.addLayout(prev_bar)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(['Transponder / stream / channel', 'Details'])
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setMinimumWidth(0)
        self.tree.setAnimated(False)
        self.tree.setIndentation(int(18 * self.scale_factor * UI_UNIT))
        self.tree.header().setStretchLastSection(True)
        self.tree.header().setSectionResizeMode(0, self.tree.header().ResizeMode.Interactive)
        self.tree.setColumnWidth(0, int(320 * self.scale_factor * UI_UNIT))
        self.tree.itemDoubleClicked.connect(self._on_tree_double)
        prev_v.addWidget(self.tree, stretch=1)
        right.addTab(prev, '  Preview  ')

        # output tab
        out = QWidget()
        out.setObjectName('TabPage')
        out_v = QVBoxLayout(out)
        out_v.setContentsMargins(10, 8, 10, 10)
        out_v.setSpacing(8)
        out_bar = QHBoxLayout()
        out_bar.setSpacing(8)
        olab = QLabel('File')
        olab.setStyleSheet('color: %s;' % C['muted'])
        out_bar.addWidget(olab)
        self.v_outfile = QComboBox()
        self.v_outfile.setEditable(False)
        self._dark_combo(self.v_outfile)
        self.v_outfile.currentIndexChanged.connect(lambda *_: self._show_output_file())
        out_bar.addWidget(self.v_outfile)
        self.btn_save_out = QPushButton('Save as\u2026')
        self.btn_copy_out = QPushButton('Copy')
        out_bar.addWidget(self.btn_save_out)
        out_bar.addWidget(self.btn_copy_out)
        out_bar.addStretch()
        self.v_out_info = QLabel('Generate the suite to populate this view.')
        self.v_out_info.setStyleSheet('color: %s;' % C['muted'])
        out_bar.addWidget(self.v_out_info)
        out_v.addLayout(out_bar)

        self.out_text = QTextEdit()
        self.out_text.setReadOnly(True)
        self.out_text.setFont(self.fonts['mono'])
        self.out_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        out_v.addWidget(self.out_text, stretch=1)
        right.addTab(out, '  Generated output  ')

        # log tab
        logf = QWidget()
        logf.setObjectName('TabPage')
        log_v = QVBoxLayout(logf)
        log_v.setContentsMargins(10, 8, 10, 10)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(self.fonts['mono_small'])
        log_v.addWidget(self.log_text)
        right.addTab(logf, '  Log  ')

        self.splitter.addWidget(right)

        # ---- status bar: hints on the left, scale controls on the right
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self._scaled_size(self.progress, 140, 'fixed_w')
        self.progress.setVisible(False)
        sb.addPermanentWidget(self.progress)
        scale_lbl = QLabel('Scale')
        scale_lbl.setStyleSheet('color: %s;' % C['muted'])
        sb.addPermanentWidget(scale_lbl)
        self.v_scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.v_scale_slider.setRange(50, 200)
        self.v_scale_slider.setSingleStep(5)
        self.v_scale_slider.setPageStep(10)
        self.v_scale_slider.setValue(int(round(self.scale_factor * 100)))
        self._scaled_size(self.v_scale_slider, 160, 'fixed_w')
        self.v_scale_slider.setToolTip('Drag to resize the whole interface')
        sb.addPermanentWidget(self.v_scale_slider)
        self.v_scale = QDoubleSpinBox()
        self.v_scale.setRange(0.5, 2.0)
        self.v_scale.setSingleStep(0.05)
        self.v_scale.setDecimals(2)
        self.v_scale.setValue(self.scale_factor)
        self._scaled_size(self.v_scale, 70, 'fixed_w')
        sb.addPermanentWidget(self.v_scale)
        self.scale_apply_btn = QPushButton('Apply')
        self.scale_apply_btn.setToolTip('Apply the selected UI scale and save it')
        sb.addPermanentWidget(self.scale_apply_btn)

        self.btn_load.clicked.connect(self._on_load)
        self.btn_gen.clicked.connect(self._on_generate)
        self.btn_cli.clicked.connect(self._on_cli)
        self.btn_save_out.clicked.connect(self._save_output_as)
        self.btn_copy_out.clicked.connect(self._copy_output)
        self.scale_apply_btn.clicked.connect(self._apply_gui_scale)
        self.v_scale.valueChanged.connect(self._on_scale_spin)
        self.v_scale_slider.valueChanged.connect(self._on_scale_slider)

        self._update_pos_label()
        self._apply_scaled_sizes()
        self._reapply_fonts()

    def _form_label(self, text, key=None):
        lbl = QLabel(text)
        lbl.setStyleSheet('color: %s;' % self.C['muted'])
        if key:
            self._attach_help(lbl, key)
        return lbl

    def _dark_combo(self, combo):
        """Force a pure-black field and popup for a combo box."""
        view = combo.view()
        am = view.palette()
        for role in (QPalette.ColorRole.Base, QPalette.ColorRole.Window,
                     QPalette.ColorRole.AlternateBase):
            am.setColor(role, QColor('#000000'))
        am.setColor(QPalette.ColorRole.Text, QColor(self.C['fg']))
        view.setPalette(am)
        view.setAutoFillBackground(True)
        view.window().setStyleSheet(
            'background: #000000; color: %s;' % self.C['fg'])
        return combo

    def _build_left(self, layout, C):
        # -- 1 data source
        box = QGroupBox(SECTIONS[0][1])
        box.setFont(self.fonts['bold'])
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        srcrow = QHBoxLayout()
        self.v_source_url = QRadioButton('Download from URL')
        self.v_source_file = QRadioButton('Local file')
        self.v_source_url.toggled.connect(self._sync_source_state)
        srcrow.addWidget(self.v_source_url)
        srcrow.addSpacing(18)
        srcrow.addWidget(self.v_source_file)
        srcrow.addStretch()
        grid.addLayout(srcrow, 0, 0, 1, 3)

        self.e_url = QLineEdit(DEFAULTS['url'])
        self.e_url.setPlaceholderText('https://abertis-dtt.github.io/sat_data.json')
        self._attach_help(self.e_url, 'url')
        grid.addWidget(self._form_label('Database URL', 'url'), 1, 0)
        grid.addWidget(self.e_url, 1, 1, 1, 2)

        self.v_file = QLineEdit(DEFAULTS['file'])
        self.e_file = self.v_file
        b_file = QPushButton('Browse\u2026')
        b_file.clicked.connect(lambda: self._browse('file'))
        self._attach_help(self.v_file, 'file')
        self._attach_help(b_file, 'file')
        self._browse_btn_file = b_file
        grid.addWidget(self._form_label('Local JSON', 'file'), 2, 0)
        grid.addWidget(self.v_file, 2, 1)
        grid.addWidget(b_file, 2, 2)

        layout.addWidget(box)

        # -- 2 output
        box = QGroupBox(SECTIONS[1][1])
        box.setFont(self.fonts['bold'])
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        self.v_out_dir = QLineEdit(DEFAULTS['out_dir'])
        self.e_out_dir = self.v_out_dir
        b_out = QPushButton('Browse\u2026')
        b_out.clicked.connect(lambda: self._browse('out_dir'))
        self._attach_help(self.e_out_dir, 'out_dir')
        self._attach_help(b_out, 'out_dir')
        grid.addWidget(self._form_label('Output folder', 'out_dir'), 0, 0)
        grid.addWidget(self.v_out_dir, 0, 1)
        grid.addWidget(b_out, 0, 2)

        self.v_merge = QCheckBox('merge into existing lamedb')
        self.v_merge.stateChanged.connect(self._sync_merge_state)
        self._attach_help(self.v_merge, 'merge_enabled')
        grid.addWidget(self.v_merge, 1, 0, 1, 3)

        self.v_merge_path = QLineEdit(DEFAULTS['merge_path'])
        self.e_merge = self.v_merge_path
        b_mpath = QPushButton('Browse\u2026')
        b_mpath.clicked.connect(lambda: self._browse('merge_path'))
        self._attach_help(self.e_merge, 'merge_path')
        self._attach_help(b_mpath, 'merge_path')
        grid.addWidget(self._form_label('Merge target', 'merge_path'), 2, 0)
        grid.addWidget(self.v_merge_path, 2, 1)
        grid.addWidget(b_mpath, 2, 2)

        self.v_edit = QCheckBox('edit external lamedb')
        self.v_edit.stateChanged.connect(self._sync_edit_state)
        self._attach_help(self.v_edit, 'edit_enabled')
        grid.addWidget(self.v_edit, 3, 0, 1, 3)

        self.v_edit_path = QLineEdit(DEFAULTS['edit_path'])
        self.e_edit = self.v_edit_path
        b_epath = QPushButton('Browse\u2026')
        b_epath.clicked.connect(lambda: self._browse('edit_path'))
        self._attach_help(self.e_edit, 'edit_path')
        self._attach_help(b_epath, 'edit_path')
        grid.addWidget(self._form_label('Edit target', 'edit_path'), 4, 0)
        grid.addWidget(self.v_edit_path, 4, 1)
        grid.addWidget(b_epath, 4, 2)

        fr_fmt = QHBoxLayout()
        fr_fmt.setSpacing(10)
        self.v_format = QComboBox()
        self.v_format.addItems(['v4'])
        self._scaled_size(self.v_format, 90, 'max_w')
        self._dark_combo(self.v_format)
        note = QLabel('v4 = current images only')
        note.setStyleSheet('color: %s;' % C['muted'])
        fr_fmt.addWidget(self.v_format)
        fr_fmt.addWidget(note)
        fr_fmt.addStretch()
        self._attach_help(self.v_format, 'lamedb_format')
        self._attach_help(note, 'lamedb_format')
        grid.addWidget(self._form_label('lamedb type', 'lamedb_format'), 5, 0)
        grid.addLayout(fr_fmt, 5, 1, 1, 2)

        self.v_bouquet = QLineEdit(DEFAULTS['bouquet'])
        self._attach_help(self.v_bouquet, 'bouquet')
        grid.addWidget(self._form_label('Bouquet file', 'bouquet'), 6, 0)
        grid.addWidget(self.v_bouquet, 6, 1, 1, 2)

        self.v_softcam_key = QCheckBox('also write SoftCam.Key (dummy BISS keys)')
        self.v_softcam_key.stateChanged.connect(self._sync_softcam_state)
        self._attach_help(self.v_softcam_key, 'softcam_key_enabled')
        grid.addWidget(self.v_softcam_key, 7, 0, 1, 3)

        self.v_softcam_key_path = QLineEdit(DEFAULTS['softcam_key_path'])
        self.e_softcam_key_path = self.v_softcam_key_path
        b_skpath = QPushButton('Browse\u2026')
        b_skpath.clicked.connect(lambda: self._browse('softcam_key_path'))
        self._attach_help(self.e_softcam_key_path, 'softcam_key_path')
        self._attach_help(b_skpath, 'softcam_key_path')
        grid.addWidget(self._form_label('SoftCam.Key', 'softcam_key_path'), 8, 0)
        grid.addWidget(self.v_softcam_key_path, 8, 1)
        grid.addWidget(b_skpath, 8, 2)

        layout.addWidget(box)

        # -- 3 satellite
        box = QGroupBox(SECTIONS[2][1])
        box.setFont(self.fonts['bold'])
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        self.v_position = QSpinBox()
        self.v_position.setRange(-1800, 1800)
        self.v_position.setSingleStep(10)
        self.v_position.setValue(DEFAULTS['position'])
        self.v_position.valueChanged.connect(lambda *_: self._update_pos_label())
        self.v_pos_label = QLabel('')
        self.v_pos_label.setStyleSheet('color: %s;' % C['muted'])
        self._attach_help(self.v_position, 'position')
        self._attach_help(self.v_pos_label, 'position')
        fr_pos = QHBoxLayout()
        fr_pos.setSpacing(10)
        fr_pos.addWidget(self.v_position)
        fr_pos.addWidget(self.v_pos_label)
        fr_pos.addStretch()
        grid.addWidget(self._form_label('Orbital position', 'position'), 0, 0)
        grid.addLayout(fr_pos, 0, 1)

        layout.addWidget(box)

        # -- 4 relay
        box = QGroupBox(SECTIONS[3][1])
        box.setFont(self.fonts['bold'])
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        fr_relay = QHBoxLayout()
        fr_relay.setSpacing(6)
        fr_relay.addWidget(QLabel('http://'))
        self.v_relay_host = QLineEdit(DEFAULTS['relay_host'])
        self._scaled_size(self.v_relay_host, 140, 'max_w')
        fr_relay.addWidget(self.v_relay_host)
        fr_relay.addWidget(QLabel(':'))
        self.v_relay_port = QSpinBox()
        self.v_relay_port.setRange(1, 65535)
        self.v_relay_port.setValue(DEFAULTS['relay_port'])
        self._scaled_size(self.v_relay_port, 90, 'max_w')
        fr_relay.addWidget(self.v_relay_port)
        fr_relay.addStretch()
        self._attach_help(self.v_relay_host, 'relay_host')
        self._attach_help(self.v_relay_port, 'relay_host')
        grid.addWidget(self._form_label('Relay listen', 'relay_host'), 0, 0)
        grid.addLayout(fr_relay, 0, 1)

        fr_outaddr = QHBoxLayout()
        fr_outaddr.setSpacing(6)
        self.v_out_host = QLineEdit(DEFAULTS['out_host'])
        self._scaled_size(self.v_out_host, 140, 'max_w')
        fr_outaddr.addWidget(self.v_out_host)
        fr_outaddr.addWidget(QLabel(':'))
        self.v_http_port = QSpinBox()
        self.v_http_port.setRange(1, 65535)
        self.v_http_port.setValue(DEFAULTS['http_port'])
        self._scaled_size(self.v_http_port, 90, 'max_w')
        fr_outaddr.addWidget(self.v_http_port)
        hint = QLabel('/<provider>/pid<PID>')
        hint.setStyleSheet('color: %s;' % C['muted'])
        fr_outaddr.addWidget(hint)
        fr_outaddr.addStretch()
        self._attach_help(self.v_out_host, 'out_host')
        self._attach_help(self.v_http_port, 'out_host')
        grid.addWidget(self._form_label('HTTP output', 'out_host'), 1, 0)
        grid.addLayout(fr_outaddr, 1, 1)

        self.v_script = QLineEdit(DEFAULTS['relay_script'])
        self._attach_help(self.v_script, 'relay_script')
        grid.addWidget(self._form_label('Relay script', 'relay_script'), 2, 0)
        grid.addWidget(self.v_script, 2, 1)

        fr_t2 = QHBoxLayout()
        fr_t2.setSpacing(8)
        lbl_plp = QLabel('PLP')
        lbl_plp.setStyleSheet('color: %s;' % C['muted'])
        self.v_t2mi_plp = QLineEdit(DEFAULTS['t2mi_plp'])
        self._scaled_size(self.v_t2mi_plp, 80, 'max_w')
        lbl_pnr = QLabel('PNR')
        lbl_pnr.setStyleSheet('color: %s;' % C['muted'])
        self.v_t2mi_pnr = QLineEdit(DEFAULTS['t2mi_pnr'])
        self._scaled_size(self.v_t2mi_pnr, 80, 'max_w')
        fr_t2.addWidget(lbl_plp)
        fr_t2.addWidget(self.v_t2mi_plp)
        fr_t2.addWidget(lbl_pnr)
        fr_t2.addWidget(self.v_t2mi_pnr)
        fr_t2.addStretch()
        self._attach_help(lbl_plp, 't2mi_plp')
        self._attach_help(self.v_t2mi_plp, 't2mi_plp')
        self._attach_help(lbl_pnr, 't2mi_pnr')
        self._attach_help(self.v_t2mi_pnr, 't2mi_pnr')
        grid.addWidget(self._form_label('T2MI', 't2mi_plp'), 3, 0)
        grid.addLayout(fr_t2, 3, 1)

        layout.addWidget(box)

    # ------------------------------------------------ helpers
    def _attach_help(self, widget, key):
        opt = OPTIONS.get(key)
        if not opt:
            return
        widget.setToolTip(opt['help'])
        self._help_keys[id(widget)] = key
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.Enter):
            key = self._help_keys.get(id(obj))
            if key:
                self.show_help(key)
        return super().eventFilter(obj, event)

    def show_help(self, key):
        opt = OPTIONS.get(key)
        if not opt:
            return
        self.help_title.setText(opt['label'])
        self.help_text.setPlainText(opt['help'])

    # ---------------------------------------------------- scaling
    def showEvent(self, event):
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            QTimer.singleShot(0, self._set_splitter_sizes)

    def _left_min_width(self, factor=None):
        if factor is None:
            factor = self.scale_factor
        # Enough for the form grid (labels + fields + Browse buttons), but
        # never more than ~70% of the window (extreme scale can scroll).
        content = 0
        try:
            content = self.scroll_content.minimumSizeHint().width() + 34
        except (AttributeError, RuntimeError):
            pass
        base = int(360 * factor * UI_UNIT)
        return max(300, min(max(base, content), int(self.width() * 0.70)))

    def _set_splitter_sizes(self, factor=None):
        if factor is None:
            factor = self.scale_factor
        self.left_panel.setMinimumWidth(self._left_min_width(factor))
        if self._split_user:
            return
        total = self.splitter.width()
        if total <= 1:
            total = max(self.width() - 28, 960)
        # Grow the sidebar together with the fonts; allow it to take what
        # the form needs (extreme scales fall back to a horizontal bar).
        left = int(total * 0.47 * factor)
        left = max(self.left_panel.minimumWidth(), min(left, int(total * 0.70)))
        right = max(280, total - left)
        self._setting_sizes = True
        try:
            self.splitter.setSizes([left, right])
        finally:
            self._setting_sizes = False

    def _on_splitter_moved(self, pos, index):
        # Only a real mouse drag counts as a manual split; layout passes and
        # constraint clamping also emit this signal with no button pressed.
        if self._setting_sizes:
            return
        if not (QApplication.mouseButtons() & Qt.MouseButton.LeftButton):
            return
        self._split_user = True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._first_show:
            self._set_splitter_sizes()

    def _on_scale_spin(self, value):
        self.v_scale_slider.blockSignals(True)
        self.v_scale_slider.setValue(int(round(value * 100)))
        self.v_scale_slider.blockSignals(False)
        self._scale_timer.start()

    def _on_scale_slider(self, value):
        self.v_scale.blockSignals(True)
        self.v_scale.setValue(value / 100.0)
        self.v_scale.blockSignals(False)
        self._scale_timer.start()

    def _apply_gui_scale(self, *_):
        self._scale_timer.stop()
        try:
            factor = float(self.v_scale.value())
        except (TypeError, ValueError):
            factor = self.scale_factor
        factor = min(2.0, max(0.5, factor))
        changed = abs(factor - self.scale_factor) > 0.001
        self.scale_factor = factor

        if abs(self.v_scale_slider.value() - int(round(factor * 100))) > 0:
            self.v_scale_slider.blockSignals(True)
            self.v_scale_slider.setValue(int(round(factor * 100)))
            self.v_scale_slider.blockSignals(False)

        self._setup_fonts(factor)
        self._apply_style()
        self._reapply_fonts()

        self._apply_scaled_sizes()
        self.tree.setIndentation(int(18 * factor * UI_UNIT))
        self.tree.setColumnWidth(0, int(320 * factor * UI_UNIT))
        self._set_splitter_sizes(factor)

        if changed:
            self.statusBar().showMessage(
                'UI scale set to %.2f \u2014 saved for next start.' % factor, 4000)

    def _scaled_size(self, widget, base, mode='max_w'):
        """Remember a base size (in unscaled px) and re-apply it on scale changes."""
        self._scaled_sizes.append((widget, base, mode))
        return widget

    def _apply_scaled_sizes(self):
        f = self.scale_factor
        for widget, base, mode in self._scaled_sizes:
            px = max(1, int(round(base * f * UI_UNIT)))
            try:
                if mode == 'fixed_w':
                    widget.setFixedWidth(px)
                elif mode == 'max_w':
                    widget.setMaximumWidth(px)
                elif mode == 'min_w':
                    widget.setMinimumWidth(px)
                elif mode == 'fixed_h':
                    widget.setFixedHeight(px)
                elif mode == 'max_h':
                    widget.setMaximumHeight(px)
            except RuntimeError:
                pass

    def _reapply_fonts(self):
        base = self.fonts['base']
        small = self.fonts['small']
        mono = self.fonts['mono']
        mono_small = self.fonts['mono_small']
        bold = self.fonts['bold']

        # Explicit pass: a stylesheet re-polish otherwise resets inherited
        # widget fonts to the tiny application default.
        for w in self.findChildren(QWidget):
            if isinstance(w, QGroupBox):
                w.setFont(bold)
            elif isinstance(w, (QPushButton, QLineEdit, QComboBox, QSpinBox,
                                QDoubleSpinBox, QCheckBox, QRadioButton,
                                QTreeWidget, QTabWidget, QLabel)):
                w.setFont(base)

        self.tree.header().setFont(small)
        self.title_label.setFont(self.fonts['h1'])
        self.subtitle_label.setFont(small)
        self.v_counts.setFont(small)
        self.v_tree_info.setFont(small)
        self.v_out_info.setFont(small)
        self.v_pos_label.setFont(small)
        self.help_title.setFont(bold)
        self.help_text.setFont(base)
        self.out_text.setFont(mono)
        self.log_text.setFont(mono_small)
        self.statusBar().setFont(small)

    def _update_pos_label(self):
        try:
            pos = int(self.v_position.value())
            ns = namespace_for(pos)
            self.v_pos_label.setText('= %s  (namespace 0x%08X)' % (position_label(pos), ns))
        except ValueError:
            self.v_pos_label.setText('= ?')

    # ---------------------------------------------------- values
    def collect_values(self):
        return {
            'source_mode': 'url' if self.v_source_url.isChecked() else 'file',
            'url': self.e_url.text().strip(),
            'file': self.v_file.text().strip(),
            'out_dir': self.v_out_dir.text().strip(),
            'bouquet': self.v_bouquet.text().strip() or DEFAULTS['bouquet'],
            'merge_enabled': self.v_merge.isChecked(),
            'merge_path': self.v_merge_path.text().strip(),
            'edit_enabled': self.v_edit.isChecked(),
            'edit_path': self.v_edit_path.text().strip(),
            'softcam_key_enabled': self.v_softcam_key.isChecked(),
            'softcam_key_path': self.v_softcam_key_path.text().strip(),
            'lamedb_format': self.v_format.currentText() or 'v4',
            'position': str(self.v_position.value()),
            'relay_host': self.v_relay_host.text().strip(),
            'relay_port': str(self.v_relay_port.value()),
            'out_host': self.v_out_host.text().strip(),
            'http_port': str(self.v_http_port.value()),
            'relay_script': self.v_script.text().strip(),
            't2mi_plp': self.v_t2mi_plp.text().strip(),
            't2mi_pnr': self.v_t2mi_pnr.text().strip(),
            'gui_scale': float(self.v_scale.value()),
        }

    def _sync_source_state(self):
        url_mode = self.v_source_url.isChecked()
        self.e_url.setEnabled(url_mode)
        self.e_file.setEnabled(not url_mode)
        self._browse_btn_file.setEnabled(not url_mode)

    def _sync_merge_state(self):
        en = self.v_merge.isChecked()
        self.e_merge.setEnabled(en)

    def _sync_edit_state(self):
        en = self.v_edit.isChecked()
        self.e_edit.setEnabled(en)

    def _sync_softcam_state(self):
        en = self.v_softcam_key.isChecked()
        self.e_softcam_key_path.setEnabled(en)

    def _browse(self, what):
        if what == 'file':
            path, _ = QFileDialog.getOpenFileName(self, 'Select sat_data.json',
                                                  filter='JSON database (*.json);;All files (*)')
            if path:
                self.v_file.setText(path)
                self.v_source_file.setChecked(True)
                self._sync_source_state()
        elif what == 'out_dir':
            path = QFileDialog.getExistingDirectory(self, 'Select output directory')
            if path:
                self.v_out_dir.setText(path)
        elif what == 'merge_path':
            path, _ = QFileDialog.getOpenFileName(self, 'Select the lamedb to merge into',
                                                  filter='lamedb (*);;All files (*)')
            if path:
                self.v_merge_path.setText(path)
        elif what == 'edit_path':
            path, _ = QFileDialog.getOpenFileName(self, 'Select the lamedb to edit',
                                                  filter='lamedb (*);;All files (*)')
            if path:
                self.v_edit_path.setText(path)
        elif what == 'softcam_key_path':
            path, _ = QFileDialog.getSaveFileName(self, 'Select SoftCam.Key output file',
                                                  initialFile='SoftCam.Key',
                                                  filter='SoftCam.Key (*.Key *SoftCam.Key);;All files (*)')
            if path:
                self.v_softcam_key_path.setText(path)
                self.v_softcam_key.setChecked(True)
                self._sync_softcam_state()

    # --------------------------------------------------- workers
    def _set_busy(self, busy, what=''):
        self._busy = busy
        for w in (self.btn_load, self.btn_gen, self.btn_cli):
            w.setEnabled(not busy)
        if busy:
            self.progress.setVisible(True)
            self.progress.setRange(0, 0)
            self.statusBar().showMessage('Working: %s \u2026' % what)
        else:
            self.progress.setVisible(False)
            self.statusBar().showMessage('Ready.')

    def _start_worker(self, kind, task):
        if self._busy:
            QMessageBox.information(self, APP_NAME, 'Busy \u2014 please wait for the current task to finish.')
            return

        def runner():
            try:
                payload = task()
            except SuiteError as exc:
                self.q.put(('fail', kind, str(exc)))
                return
            except Exception:
                self.q.put(('fail', kind, 'Unexpected error:\n' + traceback.format_exc()))
                return
            self.q.put(('ok', kind, payload))

        self._set_busy(True, {'load': 'loading database',
                              'generate': 'generating suite'}[kind])
        t = threading.Thread(target=runner, daemon=True)
        t.start()

    def _on_load(self):
        self._start_worker('load', self._task_load)

    def _on_generate(self):
        self._start_worker('generate', self._task_generate)

    def _task_load(self):
        v = self.collect_values()
        problems = validate_values(v)
        if problems:
            raise SuiteError('\n'.join(problems))
        with contextlib.redirect_stdout(LogStream(self._log_line)), \
                contextlib.redirect_stderr(LogStream(self._log_line)):
            loaded = load_database(v)
        return loaded

    def _task_generate(self):
        v = self.collect_values()
        problems = validate_values(v)
        if problems:
            raise SuiteError('\n'.join(problems))
        with contextlib.redirect_stdout(LogStream(self._log_line)), \
                contextlib.redirect_stderr(LogStream(self._log_line)):
            loaded = None
            if self.txps is None:
                loaded = load_database(v)
                txps, streams = loaded[1], loaded[2]
            else:
                txps, streams = self.txps, self.streams
            result = generate_artifacts(v, txps, streams)
        return {'loaded': loaded, 'result': result, 'values': v}

    def timerEvent(self, event):
        if event.timerId() == self._poll_timer:
            self._poll()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == 'log':
                    self._log_line(msg[1])
                elif msg[0] == 'ok':
                    self._finished_ok(msg[1], msg[2])
                elif msg[0] == 'fail':
                    self._finished_fail(msg[1], msg[2])
        except queue.Empty:
            pass

    def _finished_ok(self, kind, payload):
        self._set_busy(False)
        if kind == 'load':
            self._apply_loaded(payload)
            self.statusBar().showMessage('Database loaded \u2014 ready to generate.')
        else:
            if payload['loaded']:
                self._apply_loaded(payload['loaded'])
            result = payload['result']
            self.outputs = result['outputs']
            names = list(self.outputs.keys())
            self.v_outfile.clear()
            self.v_outfile.addItems(names)
            if names:
                self.v_outfile.setCurrentIndex(0)
                self._show_output_file()
            self.v_out_info.setText('%d files written' % len(result['written']))
            self.statusBar().showMessage('Suite generated.')
            self._log_line('generation finished: %s' % '; '.join(result['summary']), muted=True)
            QMessageBox.information(
                self, APP_NAME,
                'Suite generated successfully:\n\n'
                + '\n'.join(result['summary'])
                + '\n\nRestart Enigma2 after replacing/merging the lamedb.')

    def _finished_fail(self, kind, message):
        self._set_busy(False)
        self.statusBar().showMessage('Failed \u2014 see the message / log.')
        self._log_line('ERROR (%s)' % kind, muted=True)
        for line in message.splitlines():
            self._log_line('  ' + line, muted=True)
        QMessageBox.critical(self, APP_NAME, message)

    def _apply_loaded(self, loaded):
        self.data, self.txps, self.streams, self.raw_by_pid = loaded
        self._populate_tree()
        n_ch = sum(len(s['channels']) for s in self.streams)
        n_lists = sum(len(tp['streams']) for tp in self.txps) - len(self.streams)
        self.v_counts.setText('%d transponders \u00b7 %d PID streams \u00b7 %d channels'
                          % (len(self.txps), len(self.streams), n_ch))
        self._log_line('database loaded: %d transponders, %d PID streams, '
                       '%d channels, %d whole-tp lists skipped'
                       % (len(self.txps), len(self.streams), n_ch, n_lists))

    # ---------------------------------------------------- preview
    def _populate_tree(self):
        query = self.v_filter.text().strip().lower()
        tree = self.tree

        def match(*parts):
            return not query or any(query in (p or '').lower() for p in parts)

        tree.setUpdatesEnabled(False)
        try:
            tree.clear()
            if not self.txps:
                self.v_tree_info.setText('No database loaded yet.')
                return
            mod_names = {0: 'QPSK', 2: '8PSK', 3: '16APSK', 4: '32APSK'}
            base = self.fonts['base']
            bold = self.fonts['bold']
            muted = QColor('#98a2b3')

            # Group streams by transponder once (avoids O(transponders x streams)).
            by_tp = {}
            for s in self.streams:
                by_tp.setdefault(id(s['txp']), []).append(s)

            shown = {'tp': 0, 'stream': 0}
            for tp in self.txps:
                tp_text = 'TxP %s \u00b7 %s%s %d \u00b7 %s' % (
                    tp['txp_no'], tp['freq_label'], tp['pol_letter'],
                    tp['sr_ksps'], tp['provider'])
                tp_detail = 'tsid %d \u00b7 onid %d \u00b7 %s %s' % (
                    tp['tsid'], tp['onid'],
                    'DVB-S2' if tp['system'] else 'DVB-S',
                    mod_names.get(tp['modulation'], ''))
                tp_match = match(tp_text, tp_detail)
                children = []
                for s in by_tp.get(id(tp), ()):
                    label = s['title'].split(' - ')[-1] if ' - ' in s['title'] else s['title']
                    s_text = 'PID %d \u00b7 %s \u00b7 %s' % (
                        s['pid'], s['encryption'] or 'FTA', label)
                    s_detail = '%s \u00b7 SID %d \u00b7 CPID %d \u00b7 %d channel%s' % (
                        feed_service_name(s), s['sid'], s['cpid'],
                        len(s['channels']), '' if len(s['channels']) == 1 else 's')
                    if query and not (tp_match or match(s_text, s_detail)):
                        continue
                    children.append((s, s_text, s_detail))
                if query and not (tp_match or children):
                    continue
                item = QTreeWidgetItem([tp_text, tp_detail])
                item.setFont(0, bold)
                item.setForeground(1, muted)
                tree.addTopLevelItem(item)
                shown['tp'] += 1
                for s, s_text, s_detail in children:
                    tag_color = QColor('#f0b429' if 'BISS' in s['encryption'] else '#3fd07a')
                    siid = QTreeWidgetItem([s_text, s_detail])
                    siid.setForeground(0, tag_color)
                    siid.setForeground(1, muted)
                    siid.setData(0, Qt.ItemDataRole.UserRole, s['pid'])
                    item.addChild(siid)
                    shown['stream'] += 1
                    for ch in s['channels']:
                        ch_item = QTreeWidgetItem([
                            '%s %s' % ('(RADIO)' if ch['radio'] else '(TV)', ch['name']),
                            'SID %d' % ch['sid']])
                        ch_item.setForeground(0, muted)
                        ch_item.setForeground(1, muted)
                        siid.addChild(ch_item)
                item.setExpanded(not query)
            if query:
                self.v_tree_info.setText('filter: %d transponders, %d streams'
                                         % (shown['tp'], shown['stream']))
            else:
                self.v_tree_info.setText('%d transponders \u00b7 %d PID streams'
                                         % (len(self.txps), len(self.streams)))
        finally:
            tree.setUpdatesEnabled(True)

    def _on_tree_double(self, item, column):
        pid = item.data(0, Qt.ItemDataRole.UserRole)
        if pid is not None:
            self._show_stream_dialog(pid)

    # ------------------------------------------------- inspectors
    def _show_stream_dialog(self, pid):
        stream = next((s for s in self.streams if s['pid'] == pid), None)
        if stream is None:
            return
        raw = self.raw_by_pid.get(pid, {})
        v = self.collect_values()
        ns = values_namespace(v)
        tp = stream['txp']
        tp_e, svc_e = build_lamedb_entries([tp], [stream], ns.position)
        lam = lamedb_text(tp_e, svc_e)
        astx = astra_text([stream], ns)
        bqx = bouquet_text([stream], ns)
        sid = stream.get('sid', pid + 1000)
        cpid = stream.get('cpid', pid + 5000)
        raw_sid = str(raw.get('sid') or '').strip()
        raw_cpid = str(raw.get('cpid') or '').strip()
        sid_src = '' if raw_sid else '   (auto = PID + 1000)'
        cpid_src = '' if raw_cpid else '   (auto = PID + 5000)'
        info = [
            'Title        %s' % stream['title'],
            'Stream PID   %d' % pid,
            'Feed SID     %d  (0x%04X)%s' % (sid, sid, sid_src),
            'Comp. PID    %d  (0x%04X)%s' % (cpid, cpid, cpid_src),
            'Cache flags  c:18%04x = data PID (stream PID) \u00b7 c:19%04x = cpid (PMT)' % (pid, cpid),
            'Encryption   %s' % (stream['encryption'] or 'FTA (clear)'),
            'Provider     %s' % tp['provider'],
            'Transponder  %s%s %d \u00b7 sr %d \u00b7 tsid %d \u00b7 onid %d'
            % (tp['freq_label'], tp['pol_letter'], tp['sr_ksps'],
               tp['sr_ksps'], tp['tsid'], tp['onid']),
            'pid_info     %s' % (raw.get('pid_info') or '-'),
            'Channels     %d' % len(stream['channels']),
        ]
        sep = '\u2500' * 74
        content = '\n'.join(info) + '\n\n' + sep + ' lamedb\n' + sep + '\n' \
            + lam + '\n' + sep + ' astra.conf\n' + sep + '\n' + astx
        if stream['channels']:
            content += '\n' + sep + ' userbouquet\n' + sep + '\n' + bqx
        else:
            content += '\n(no channels in the JSON \u2014 no bouquet lines)'
        win = QDialog(self)
        win.setFont(self.fonts['base'])
        win.setWindowTitle('Stream inspector \u2014 PID %d' % pid)
        win.setMinimumSize(780, 560)
        win_l = QVBoxLayout(win)
        bar = QHBoxLayout()
        btn_copy = QPushButton('Copy all')
        btn_copy.clicked.connect(lambda: self._copy_to_clipboard(content))
        bar.addWidget(btn_copy)
        bar.addWidget(QLabel('exact artifacts generated with the current settings'))
        bar.addStretch()
        win_l.addLayout(bar)
        txt = QTextEdit()
        txt.setFont(self.fonts['mono'])
        txt.setPlainText(content)
        txt.setReadOnly(True)
        win_l.addWidget(txt, stretch=1)
        win.exec()

    # ------------------------------------------------- output
    def _set_text(self, widget, content):
        widget.setPlainText(content)

    def _log_line(self, line, muted=False):
        stamp = time.strftime('%H:%M:%S')
        doc = self.log_text.document()
        if doc.blockCount() > 3000:
            self.log_text.clear()
        self.log_text.append('%s  %s' % (stamp, line))
        self.log_text.moveCursor(QTextCursor.MoveOperation.End)

    def _clear_log(self):
        self.log_text.clear()

    def status(self, text, kind=None):
        self.statusBar().showMessage(text)

    def _show_output_file(self):
        name = self.v_outfile.currentText()
        content = self.outputs.get(name)
        if content is not None:
            self._set_text(self.out_text, content)

    def _save_output_as(self):
        name = self.v_outfile.currentText()
        content = self.outputs.get(name)
        if content is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Save as\u2026', initialFile=name)
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            self.statusBar().showMessage('Saved %s' % path)

    def _copy_output(self):
        name = self.v_outfile.currentText()
        content = self.outputs.get(name)
        if content is not None:
            self._copy_to_clipboard(content)

    def _copy_to_clipboard(self, text):
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage('Copied to clipboard.')

    # ------------------------------------------------ doc windows
    def _doc_window(self, title, content):
        win = QDialog(self)
        win.setFont(self.fonts['base'])
        win.setWindowTitle(title)
        win.setMinimumSize(820, 600)
        win_l = QVBoxLayout(win)
        bar = QHBoxLayout()
        btn_copy = QPushButton('Copy')
        btn_copy.clicked.connect(lambda: self._copy_to_clipboard(content))
        bar.addWidget(btn_copy)
        bar.addStretch()
        win_l.addLayout(bar)
        txt = QTextEdit()
        txt.setFont(self.fonts['mono_small'])
        txt.setPlainText(content)
        txt.setReadOnly(True)
        win_l.addWidget(txt, stretch=1)
        win.exec()

    def _show_reference(self):
        lines = ['OPTION REFERENCE', '=' * 60, '']
        for sec_key, sec_title in SECTIONS:
            lines.append(sec_title.upper())
            lines.append('-' * len(sec_title) + '-')
            for key, opt in OPTIONS.items():
                if opt['section'] != sec_key:
                    continue
                default = DEFAULTS.get(key, '')
                lines.append('%s   (%s)' % (opt['label'], key))
                lines.append('default: %r' % (default,))
                for chunk in opt['help'].split('\n'):
                    lines.append('  ' + chunk)
                lines.append('')
        lines.append('ACTIONS')
        lines.append('-------')
        for key, help_ in ACTION_HELP.items():
            lines.append(key)
            lines.append('  ' + help_)
            lines.append('')
        lines.append('SHORTCUTS')
        lines.append('---------')
        lines.append('  Ctrl+L  load database        Ctrl+G  generate suite')
        lines.append('  Ctrl+O  open local JSON      Ctrl+K  copy CLI command')
        lines.append('  F1      this reference       Ctrl+Q   quit')
        self._doc_window('Option reference', '\n'.join(lines))

    def _show_guide(self):
        self._doc_window('Quick guide', QUICK_GUIDE)

    def _show_about(self):
        QMessageBox.information(
            self, 'About',
            '%s\n%s\n\nGUI front-end for Abertis_DX_Suite.py\n'
            'Engine: lamedb + astra relay + Enigma2 bouquet generator '
            'for the Abertis / Cellnex DTT feeds on Hispasat 30.0\u00b0W.\n\n'
            'PyQt6. Data source:\n%s'
            % (APP_NAME, APP_SUBTITLE, DEFAULT_JSON_URL))

    # ----------------------------------------------- CLI command
    def _on_cli(self):
        v = self.collect_values()
        problems = validate_values(v)
        cmd = build_cli_command(v)
        win = QDialog(self)
        win.setFont(self.fonts['base'])
        win.setWindowTitle('CLI command')
        win.setMinimumSize(760, 200)
        win_l = QVBoxLayout(win)
        bar = QHBoxLayout()
        btn_copy = QPushButton('Copy')
        btn_copy.clicked.connect(lambda: self._copy_to_clipboard(cmd))
        bar.addWidget(btn_copy)
        hint = ('equivalent command line for the current settings'
                if not problems else
                'note: current settings have validation problems')
        lbl = QLabel(hint)
        lbl.setStyleSheet('color: #9aa1ad;')
        bar.addWidget(lbl)
        bar.addStretch()
        win_l.addLayout(bar)
        txt = QTextEdit()
        txt.setFont(self.fonts['mono'])
        txt.setPlainText(cmd)
        txt.setReadOnly(True)
        win_l.addWidget(txt, stretch=1)
        win.exec()

    # ------------------------------------------ profiles & state
    def _menu_open_json(self):
        self._browse('file')

    def _save_profile(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Save settings profile',
                                              'dx_suite_profile.json',
                                              'DX suite profile (*.json)')
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'font_version': 2, 'values': self.collect_values(),
                       'geometry': self.saveGeometry().data().hex()}, f, indent=2)
        self.statusBar().showMessage('Profile saved: %s' % path)

    def _load_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Load settings profile',
                                              filter='DX suite profile (*.json);;All files (*)')
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                profile = json.load(f)
            self._apply_values(profile.get('values', {}))
            if profile.get('geometry'):
                try:
                    self.restoreGeometry(QByteArray.fromhex(profile['geometry']))
                except Exception:
                    pass
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, 'Cannot load profile', '%s' % exc)
        else:
            self.statusBar().showMessage('Profile loaded: %s' % path)

    def _apply_values(self, values):
        if 'source_mode' in values:
            self.v_source_url.setChecked(values['source_mode'] == 'url')
            self.v_source_file.setChecked(values['source_mode'] == 'file')
            self._sync_source_state()
        var_map = {
            'url': self.e_url, 'file': self.v_file,
            'out_dir': self.v_out_dir, 'bouquet': self.v_bouquet,
            'merge_path': self.v_merge_path,
            'edit_path': self.v_edit_path,
            'lamedb_format': self.v_format,
            'softcam_key_enabled': self.v_softcam_key,
            'softcam_key_path': self.v_softcam_key_path,
            'position': self.v_position,
            'relay_host': self.v_relay_host, 'relay_port': self.v_relay_port,
            'out_host': self.v_out_host, 'http_port': self.v_http_port,
            'relay_script': self.v_script,
            't2mi_plp': self.v_t2mi_plp, 't2mi_pnr': self.v_t2mi_pnr,
            'gui_scale': self.v_scale,
        }
        for key, widget in var_map.items():
            if key in values and widget is not None:
                if isinstance(widget, QCheckBox):
                    widget.setChecked(bool(values[key]))
                elif isinstance(widget, QComboBox):
                    widget.setCurrentText(str(values[key]))
                elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                    try:
                        widget.setValue(float(values[key]))
                    except (TypeError, ValueError):
                        pass
                else:
                    widget.setText(str(values[key]))
        if 'merge_enabled' in values:
            self.v_merge.setChecked(bool(values['merge_enabled']))
            self._sync_merge_state()
        if 'edit_enabled' in values:
            self.v_edit.setChecked(bool(values['edit_enabled']))
            self._sync_edit_state()
        self._sync_source_state()
        self._sync_edit_state()
        self._update_pos_label()
        self._apply_gui_scale()

    def _restore_settings(self):
        self._sync_source_state()
        self._sync_merge_state()
        self._sync_edit_state()
        self._update_pos_label()
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            values = saved.get('values', {})
            if int(saved.get('font_version', 0)) < 2:
                # Previous builds used a ~10 pt base, so a saved 1.6x was
                # chosen only to compensate. Reset to 1.0 for the 18 pt base.
                values['gui_scale'] = 1.0
            self._apply_values(values)
            if saved.get('geometry'):
                try:
                    self.restoreGeometry(QByteArray.fromhex(saved['geometry']))
                except Exception:
                    pass
        except (OSError, ValueError):
            pass

    def closeEvent(self, event):
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump({'font_version': 2, 'values': self.collect_values(),
                           'geometry': self.saveGeometry().data().hex()}, f, indent=2)
        except OSError:
            pass
        event.accept()


class LogStream(io.TextIOBase):
    def __init__(self, emit_fn):
        self.emit_fn = emit_fn

    def write(self, s):
        for line in s.splitlines():
            if line.strip():
                self.emit_fn(line)
        return len(s)

    def flush(self):
        pass


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle('Fusion')
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(THEME['bg']))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(THEME['fg']))
    pal.setColor(QPalette.ColorRole.Base, QColor(THEME['field']))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor('#151922'))
    pal.setColor(QPalette.ColorRole.Text, QColor(THEME['fg']))
    pal.setColor(QPalette.ColorRole.Button, QColor(THEME['surface2']))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(THEME['fg']))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(THEME['accent']))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor('#ffffff'))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(THEME['surface2']))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(THEME['fg']))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(THEME['muted']))
    app.setPalette(pal)

    window = App()
    window.showMaximized()
    sys.exit(app.exec())


if __name__ == '__main__':
    if '--check' in sys.argv:
        run_checks()
        sys.exit(0)
    main()
