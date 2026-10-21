#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Graphical front-end for the Abertis / Cellnex DX Suite (Hispasat 30.0W).

A desktop GUI (Tkinter, standard library only) around the engine in
Abertis_DX_Suite.py. It exposes every option of the command line tool with
hover tooltips, a focus-sensitive help pane and an option reference window,
shows the loaded database as a browsable transponder/stream/channel tree,
can inspect the exact lamedb/astra/bouquet artifacts of a single stream, and
bridges back to the CLI with a copy-pasteable command line.

Run it:
  ./Abertis_DX_Suite_GUI.py            normal start
  ./Abertis_DX_Suite_GUI.py --check    headless self-test (no display needed)

Keyboard shortcuts:
  Ctrl+L   load / refresh the database      Ctrl+G   generate the suite
  Ctrl+O   open a local JSON database       Ctrl+K   copy CLI command
  F1       option reference                 Ctrl+Q   quit

Requires python3-tk (Debian/Ubuntu: sudo apt install python3-tk).
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

import re
import shutil
import struct
import zlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

import re
import shutil
import struct
import sys
import zlib
from collections import OrderedDict
DEFAULT_JSON_URL = 'https://abertis-dtt.github.io/sat_data.json'

BOUQUET_HEADER = u'#NAME \u2022 30,0 W - Abertis & Cellnex DX Suite ( streamrelay )'


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
    """DVB namespace for a satellite position; West positions wrap at 3600."""
    pos = position_tenths + 3600 if position_tenths < 0 else position_tenths
    return (pos & 0xFFFF) << 16



def parse_transponders(data):
    """Normalise the sat_data.json list into transponders carrying their streams."""
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
        # onid is '-' (unconfirmed) on part of the Abertis network; the
        # confirmed original network id there is 1, so fall back to it.
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
    """Flatten (transponder, stream) pairs, extracting PID and channel lists."""
    streams = []
    seen = {}
    channel_lists = []
    for tp in txps:
        for raw in tp['streams']:
            match = STREAM_PID_RE.search(raw.get('title') or '')
            if not match:
                # Entries without a "Stream <pid>" title (e.g. "RNE",
                # "TDT Aragon") are whole-transponder channel lists: their
                # pid_info carries nid/onid/tsid instead of a stream PID,
                # so there is no single PID to relay through astra.
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

            # Stream-level identifiers from the database: the SID of the fake
            # feed service and the component data PID that goes into the
            # c:19<xxxx> lamedb cache entry. Empty/invalid -> arithmetic
            # defaults (SID = PID+1000, component PID = PID+5000).
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
                'txp': tp,
                'channels': channels,
            })
    streams.sort(key=lambda s: s['pid'])
    if channel_lists:
        print('Skipped %d whole-transponder channel lists (no single PID to '
              'relay): %s' % (len(channel_lists), ', '.join(sorted(set(channel_lists)))))
    return streams



def feed_service_name(stream):
    """lamedb/astra name of the fake feed service, e.g. Abertis@11222H30000 PID 2025."""
    tp = stream['txp']
    return '%s@%s%s%d PID %d' % (tp['provider_short'], tp['freq_label'],
                                  tp['pol_letter'], tp['sr_ksps'], stream['pid'])



def relay_script_for(stream, override):
    """Relay script name (and URL segment) for a stream's provider."""
    return override or stream['txp']['provider_short'].lower()



def build_lamedb_entries(txps, streams, position):
    """Return ((key, lines), ...) for the transponder and service sections."""
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
        # SID of the feed service comes straight from the JSON (field "sid"),
        # never from arithmetic, so it always matches the database.
        sid = stream['sid']
        header = '%04x:%08x:%04x:%04x:1:0' % (sid, ns, tp['tsid'], tp['onid'])
        block = [header, feed_service_name(stream)]
        # lamedb cache ids are DECIMAL in the file: c:18 = cDATAPID gets the
        # raw stream PID, c:19 = cPMTPID gets the component data PID
        # ("cpid" field of the JSON, used by the astra pipe script to
        # rebuild the PMT). c:031fff = dummy pid, C:2600 = BISS CAID.
        if 'BISS' in stream['encryption']:
            block.append('p:%s,c:031fff,c:18%04x,c:19%04x,C:%s,f:01'
                         % (tp['provider_short'].upper(), stream['pid'],
                            stream['cpid'], BISS_CAID))
        else:
            # FTA raw PID: same cache entries, no BISS CAID.
            block.append('p:%s,c:031fff,c:18%04x,c:19%04x,f:01'
                         % (tp['provider_short'].upper(), stream['pid'],
                            stream['cpid']))
        svc_entries.append((tuple(header.split(':')[:4]), block))
    return tp_entries, svc_entries



def lamedb_text(tp_entries, svc_entries, fmt='v4'):
    """Serialize as lamedb v4 (sectioned, written by current images) or
    v3 (classic layout for older images). Service and transponder blocks
    themselves are identical in both versions."""
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
        blocks.append('\n'.join([
            'make_channel({',
            '  name = "%s",' % feed_service_name(stream),
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
        ]))
    return header + '\n\n'.join(blocks) + '\n'



def bouquet_text(streams, args):
    lines = [BOUQUET_HEADER]
    for stream in streams:
        if not stream['channels']:
            print('Note: PID %d has no channels in the JSON - lamedb/astra '
                  'entries only' % stream['pid'])
            continue
        relay = relay_script_for(stream, args.relay_script)
        marker_name = '##### %s PID %d #####' % (stream['txp']['provider_short'],
                                                 stream['pid'])
        lines.append('#SERVICE 1:64:%X:0:0:0:0:0:0:0::%s' % (stream['pid'], marker_name))
        lines.append('#DESCRIPTION ##### %s #####' % feed_service_name(stream))
        url = 'http://%s:%d/%s/pid%d' % (args.out_host, args.http_port,
                                          relay, stream['pid'])
        # Enigma2 requires colons in URLs to be URL-encoded (uppercase %3A)
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
    """BISS key ID = CRC32(seed=0x2600, SID(2) + PMT(2) + stream PID(s)(2)), big-endian.

    Matches OSCam's universal BISS hash: the fake ECM is
    [sid][pmtpid][pid1][pid2]...[tsid][onid][ens] and the hash covers everything
    before [tsid][onid][ens]. One feed service carries a single stream PID here.
    """
    return zlib.crc32(struct.pack('>HHH', sid, pmt_pid, stream_pid), BISS_CRC_SEED) & 0xFFFFFFFF



def softcam_text(streams):
    """Serialize dummy BISS SoftCam.Key entries for every BISS-encrypted stream.

    Each line: F <key id> 00000000 0000000000000000 ;<feed service name>
    """
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
        # c:19<cpid> is the cPMTPID cache entry (PMT); stream['pid'] is the raw
        # stream PID carried in c:18<cDATAPID>.
        key_id = softcam_key_id(stream['sid'], stream['cpid'], stream['pid'])
        entries.setdefault(key_id, []).append(feed_service_name(stream))
    for key_id, comments in entries.items():
        lines.append('F %08X 00000000 %s ;%s' % (key_id, DUMMY_BISS_KEY,
                                                 ' / '.join(comments)))
    return '\n'.join(lines) + '\n'



def parse_lamedb(lines):
    """Parse a lamedb file body into (fmt, tp_blocks, svc_blocks).

    Only the v4 sectioned layout (transponders ... each block closed by '/'
    ... end / services ... end) is supported.
    """
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
                idx += 1  # per-block terminator, regenerated on save
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
        block = [line]  # sid:namespace:tsid:onid:type:number
        idx += 1
        if idx < len(lines) and lines[idx].strip() \
                and not lines[idx].startswith(SERVICE_FLAG_PREFIXES):
            block.append(lines[idx])  # service name
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
        if st.startswith('s ') or st.endswith('\t') is False and '\t' in line:
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
    # Preserve the 3rd line even when flags are missing.
    out = [header, name, ','.join(flags) if flags else '']
    return out



def merge_lamedb(path, tp_entries, svc_entries):
    """Insert/replace our transponders and services inside an existing lamedb.

    Only v4 /4/ lamedb files are supported.
    """
    with open(path, 'r', encoding='utf-8', errors='surrogateescape') as f:
        lines = f.read().split('\n')

    fmt, tp_blocks, svc_blocks = parse_lamedb(lines)

    # Clean stale entries first (avoid duplicates): remove transponders
    # matching the generated ones and services whose name contains
    # "abertis", then record how many generated keys already existed.
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
    """Edit an external lamedb in place with cleanup before adding entries."""
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




try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    import tkinter.font as tkfont
    TK_AVAILABLE = True
except ImportError:
    TK_AVAILABLE = False

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
    'gui_scale': 1.0,
}


class SuiteError(Exception):
    """User-facing error with a friendly message."""


# ---------------------------------------------------------------------------
# Option registry: single source of truth for labels and help texts, used by
# the tooltips, the focus help pane and Help > Option Reference.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Pure logic (no Tk): validation, CLI bridge, database load, generation.
# Used by the GUI and by the headless --check self-test.
# ---------------------------------------------------------------------------

def position_label(tenths):
    """-300 -> '30.0° W', 192 -> '19.2° E'."""
    try:
        tenths = int(tenths)
    except (TypeError, ValueError):
        return '?'
    return '%.1f\u00b0 %s' % (abs(tenths) / 10.0, 'W' if tenths < 0 else 'E')


def validate_values(v):
    """Return a list of human readable problems (empty list = all good)."""
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
    """Values dict -> the namespace the engine's generators expect."""
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
        relay_script=v['relay_script'].strip() or None)


def build_cli_command(v):
    """Equivalent CLI invocation for the current settings."""
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
    if v['bouquet'].strip() and v['bouquet'].strip() != DEFAULTS['bouquet']:
        parts += ['--bouquet', shlex.quote(v['bouquet'].strip())]
    if v.get('softcam_key_enabled'):
        parts += ['--softcam-key']
        if v.get('softcam_key_path'):
            parts += [shlex.quote(v['softcam_key_path'])]
    return ' '.join(parts)


def load_database(v):
    """Fetch/parse the database. Returns (data, txps, streams, raw_by_pid)."""
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
    """Run the generation for real. Returns a result dict for the GUI."""
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
    except SystemExit as exc:                      # engine guards with sys.exit
        raise SuiteError(str(exc.code) if exc.code else 'generation failed')
    except OSError as exc:
        raise SuiteError('Cannot write output: %s' % exc)
    return {'outputs': outputs, 'written': written,
            'summary': summary, 'merged': merged, 'edited': edited}


# ---------------------------------------------------------------------------
# Headless self-test (--check): exercises validation, the CLI bridge and a
# full generation round on an embedded fixture, without needing a display.
# ---------------------------------------------------------------------------

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
               'sid': '505', 'vpid': '', 'apid_lang': '2001 Spa'}]},
         {'title': 'RNE', 'pid_info': '1-60020--60000', 'encryption': '',
          'channels': [
              {'type': 'Radio', 'name': 'RNE Radio 3 Nacion', 'system': '',
               'sid': '300', 'vpid': '', 'apid_lang': '301 Spa'}]}]},
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

    # 1. registry sanity: every option documented, defaults present
    for key, opt in OPTIONS.items():
        assert opt['label'] and opt['section'] in dict(SECTIONS), key
        assert len(opt['help']) > 60, ('help text too short', key)
        assert key in DEFAULTS, ('missing default', key)
    assert all(len(h) > 40 for h in ACTION_HELP.values())
    print('PASS  option registry: %d options + %d actions documented'
          % (len(OPTIONS), len(ACTION_HELP)))

    # 2. position labels
    assert position_label(-300) == '30.0\u00b0 W'
    assert position_label(192) == '19.2\u00b0 E'
    print('PASS  position labels')

    # 3. validation
    bad = dict(DEFAULTS, source_mode='file', relay_port='notaport',
               position='-9999', file='/nope.json')
    problems = validate_values(bad)
    assert len(problems) == 3, problems
    assert not validate_values(DEFAULTS)
    print('PASS  validation (rejects bad port/position/file, accepts defaults)')

    # 4. CLI bridge
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

    # 5. full generation round on the embedded fixture (GUI code path)
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        fixture = os.path.join(tmp, 'fixture.json')
        with open(fixture, 'w', encoding='utf-8') as f:
            json.dump(CHECK_FIXTURE, f)
        v = dict(DEFAULTS, source_mode='file', file=fixture,
                 out_dir=os.path.join(tmp, 'out'))
        _data, txps, streams, raw_by_pid = load_database(v)
        assert len(txps) == 2 and len(streams) == 2, (len(txps), len(streams))
        assert 2025 in raw_by_pid and 3150 in raw_by_pid
        result = generate_artifacts(v, txps, streams)
        lam = result['outputs']['lamedb']
        assert lam.startswith('eDVB services /4/\ntransponders\n'
                              '0ce40000:ea74:0001')
        assert '\nend\nservices\n' in lam and lam.rstrip('\n').endswith('end')
        assert '/\n0ce40000:0001:311c' in lam          # one / per transponder
        astx = result['outputs']['astra.conf']
        bq = result['outputs']['userbouquet.abertis.tv']
        assert ('0bd1:0ce40000:ea74:0001:1:0\n'
                'Abertis@11222H30000 PID 2025\n'
                'p:ABERTIS,c:031fff,c:1807e9,c:191b71,C:2600,f:01\n') in lam
        # FTA Cellnex feed with custom JSON sid/cpid overriding the arithmetic
        assert 'Cellnex@11502V10200 PID 3150' in lam
        assert ('1068:0ce40000:0001:311c:1:0\n'
                'Cellnex@11502V10200 PID 3150\n'
                'p:CELLNEX,c:031fff,c:180c4e,c:191fd6,f:01\n') in lam  # sid 4200=0x1068, pid 0xc4e, cpid 8150
        assert '1:0:1:BD1:EA74:1:CE40000:0:0:0' in astx          # sid 3025 from JSON
        assert '1:0:1:1068:1:12572:CE40000:0:0:0' in astx        # sid 4200, tsid 1, onid 12572
        assert '/etc/astra/scripts/cellnex 3150' in astx
        assert '/cellnex/pid3150' in astx
        assert 'http%3A//0.0.0.0%3A9999/cellnex/pid3150:(TV) CMM TV' in bq
        assert '(TV) TVE La 1 Extremadura' in bq
        assert '(RADIO) RNE Radio 1 Extremadura' in bq
        for p in result['written']:
            assert os.path.isfile(p), p
        print('PASS  generation round on fixture (standalone mode)')

        # 5b. SoftCam.Key: dummy BISS entries with CRC32(seed=0x2600, SID+ns)
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

        # 6. merge round (v4 only)
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

        # 7. edit rounds: removes stale abertis services and matching txps
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
# GUI (requires tkinter)
# ---------------------------------------------------------------------------

if TK_AVAILABLE:

    PALETTE = {
        'bg': '#1b1d23', 'panel': '#23252d', 'field': '#2b2d36',
        'fg': '#e6e7ec', 'muted': '#9aa1ad', 'accent': '#3d8bfd',
        'accent2': '#2f6cd8', 'ok': '#3fb950', 'warn': '#d8a333',
        'err': '#f2555a', 'border': '#363943', 'tree': '#22242b',
        'sel': '#33465f', 'tip': '#101217',
    }

    class Tooltip:
        """Hover tooltip with wrapped, multi-line help text."""

        def __init__(self, app, widget, text, delay=450):
            self.app, self.widget, self.text = app, widget, text
            self.delay, self._after, self._win = delay, None, None
            widget.bind('<Enter>', self._schedule, add='+')
            widget.bind('<Leave>', self._hide_now, add='+')
            widget.bind('<ButtonPress>', self._hide_now, add='+')

        def _schedule(self, _event=None):
            self._cancel()
            self._after = self.widget.after(self.delay, self._show)

        def _cancel(self):
            if self._after:
                self.widget.after_cancel(self._after)
                self._after = None

        def _show(self):
            if self._win or not self.text:
                return
            x = self.widget.winfo_rootx() + 14
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            win = tk.Toplevel(self.widget)
            win.wm_overrideredirect(True)
            win.wm_geometry('+%d+%d' % (x, y))
            tk.Label(win, text=self.text, justify='left', wraplength=470,
                     bg=PALETTE['tip'], fg=PALETTE['fg'], relief='solid',
                     borderwidth=1, padx=9, pady=6,
                     font=self.app.fonts['small']).pack()
            self._win = win

        def _hide_now(self, _event=None):
            self._cancel()
            if self._win:
                self._win.destroy()
                self._win = None

    class QueueLog(io.TextIOBase):
        """stdout/stderr replacement that feeds the GUI log queue."""

        def __init__(self, app):
            self.app = app

        def write(self, s):
            for line in s.splitlines():
                if line.strip():
                    self.app.q.put(('log', line))
            return len(s)

        def flush(self):
            pass

    class App(tk.Tk):

        def __init__(self):
            super().__init__()
            self.title('%s \u2014 %s' % (APP_NAME, APP_SUBTITLE))
            self.minsize(1080, 660)
            self._setup_fonts()
            self._setup_style()
            self.q = queue.Queue()
            self._log_stream = QueueLog(self)
            self.busy = False
            self.data = None
            self.txps = None
            self.streams = None
            self.raw_by_pid = {}
            self.outputs = OrderedDict()
            self._stream_items = {}
            self._busy_widgets = []
            self._build_menu()
            self._build_ui()
            self._restore_settings()
            self.protocol('WM_DELETE_WINDOW', self._on_close)
            self.after(80, self._poll)
            self.status('Ready \u2014 load the database (Ctrl+L) or press '
                        'Generate (Ctrl+G).')

        # ------------------------------------------------ fonts & style
        def _initial_gui_scale(self):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                scale = float(saved.get('values', {}).get(
                    'gui_scale', DEFAULTS.get('gui_scale', 1.0)))
            except (OSError, ValueError, TypeError):
                scale = float(DEFAULTS.get('gui_scale', 1.0))
            if scale < 0.5 or scale > 3.0:
                scale = 1.0
            return scale

        def _setup_fonts(self):
            self.scale_factor = self._initial_gui_scale()
            fams = set(tkfont.families(self))
            ui = ('Segoe UI' if 'Segoe UI' in fams else
                  'DejaVu Sans' if 'DejaVu Sans' in fams else 'Helvetica')
            mono = ('Consolas' if 'Consolas' in fams else
                    'DejaVu Sans Mono' if 'DejaVu Sans Mono' in fams else
                    'Courier')
            sc = self.scale_factor

            def sz(base):
                return max(7, int(round(base * sc)))
            self.fonts = {
                'base': tkfont.Font(family=ui, size=sz(9)),
                'small': tkfont.Font(family=ui, size=sz(8)),
                'bold': tkfont.Font(family=ui, size=sz(9), weight='bold'),
                'h1': tkfont.Font(family=ui, size=sz(13), weight='bold'),
                'mono': tkfont.Font(family=mono, size=sz(9)),
                'mono_small': tkfont.Font(family=mono, size=sz(8)),
            }

        def _setup_style(self):
            C = PALETTE
            style = ttk.Style(self)
            try:
                style.theme_use('clam')
            except tk.TclError:
                pass
            style.configure('.', background=C['panel'], foreground=C['fg'],
                            font=self.fonts['base'], bordercolor=C['border'],
                            lightcolor=C['panel'], darkcolor=C['panel'])
            style.configure('TFrame', background=C['panel'])
            style.configure('TLabel', background=C['panel'], foreground=C['fg'])
            style.configure('Muted.TLabel', foreground=C['muted'])
            style.configure('Title.TLabel', font=self.fonts['h1'])
            style.configure('TLabelframe', background=C['panel'],
                            bordercolor=C['border'], relief='solid',
                            borderwidth=1)
            style.configure('TLabelframe.Label', background=C['panel'],
                            foreground=C['accent'], font=self.fonts['bold'])
            style.configure('TEntry', fieldbackground=C['field'],
                            foreground=C['fg'], insertcolor=C['fg'],
                            bordercolor=C['border'], lightcolor=C['border'],
                            darkcolor=C['border'], padding=3)
            style.map('TEntry', bordercolor=[('focus', C['accent'])])
            style.configure('TSpinbox', fieldbackground=C['field'],
                            foreground=C['fg'], arrowcolor=C['fg'],
                            bordercolor=C['border'], lightcolor=C['border'],
                            darkcolor=C['border'], padding=2,
                            arrowsize=11)
            style.configure('TRadiobutton', background=C['panel'],
                            foreground=C['fg'], focuscolor=C['panel'])
            style.map('TRadiobutton',
                      indicatorcolor=[('selected', C['accent'])],
                      background=[('active', C['panel'])])
            style.configure('TCheckbutton', background=C['panel'],
                            foreground=C['fg'], focuscolor=C['panel'])
            style.map('TCheckbutton',
                      indicatorcolor=[('selected', C['accent'])],
                      background=[('active', C['panel'])])
            style.configure('TCombobox', fieldbackground=C['field'],
                            foreground=C['fg'], arrowcolor=C['fg'],
                            bordercolor=C['border'], padding=3)
            self.option_add('*TCombobox*Listbox.background', C['field'])
            self.option_add('*TCombobox*Listbox.foreground', C['fg'])
            self.option_add('*TCombobox*Listbox.selectBackground', C['sel'])
            self.option_add('*TCombobox*Listbox.selectForeground', '#ffffff')
            style.configure('TNotebook', background=C['panel'],
                            bordercolor=C['border'], tabmargins=(6, 6, 6, 0))
            style.configure('TNotebook.Tab', background=C['bg'],
                            foreground=C['muted'], padding=(13, 5))
            style.map('TNotebook.Tab',
                      background=[('selected', C['field'])],
                      foreground=[('selected', C['fg'])])
            style.configure('Treeview', background=C['tree'],
                            foreground=C['fg'], fieldbackground=C['tree'],
                            bordercolor=C['border'], rowheight=23)
            style.configure('Treeview.Heading', background=C['bg'],
                            foreground=C['muted'],
                            font=self.fonts['small'], bordercolor=C['border'],
                            relief='flat')
            style.map('Treeview', background=[('selected', C['sel'])],
                      foreground=[('selected', '#ffffff')])
            style.configure('TProgressbar', background=C['accent'],
                            troughcolor=C['field'], bordercolor=C['field'],
                            lightcolor=C['accent'], darkcolor=C['accent'])
            for orient in ('Horizontal', 'Vertical'):
                style.configure(orient + '.TScrollbar', background=C['field'],
                                troughcolor=C['bg'], bordercolor=C['bg'],
                                arrowcolor=C['muted'], relief='flat')

        def button(self, parent, text, command, kind='normal', **kw):
            C = PALETTE
            bg = {'normal': C['field'], 'accent': C['accent'],
                  'ghost': C['panel']}[kind]
            fg = '#ffffff' if kind == 'accent' else C['fg']
            active = {'normal': '#343743', 'accent': C['accent2'],
                      'ghost': '#2a2c35'}[kind]
            return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                             activebackground=active, activeforeground=fg,
                             disabledforeground=C['muted'], relief='flat',
                             padx=13, pady=4, borderwidth=0,
                             highlightthickness=0, cursor='hand2',
                             font=self.fonts['base'], **kw)

        # -------------------------------------------------------- menu
        def _menu(self, parent):
            C = PALETTE
            return tk.Menu(parent, tearoff=0, bg=C['panel'], fg=C['fg'],
                           activebackground=C['sel'],
                           activeforeground='#ffffff', bd=0)

        def _build_menu(self):
            menubar = self._menu(self)
            m_file = self._menu(menubar)
            m_file.add_command(label='Open local JSON\u2026',
                               accelerator='Ctrl+O',
                               command=self._menu_open_json)
            m_file.add_separator()
            m_file.add_command(label='Save settings profile\u2026',
                               command=self._save_profile)
            m_file.add_command(label='Load settings profile\u2026',
                               command=self._load_profile)
            m_file.add_separator()
            m_file.add_command(label='Quit', accelerator='Ctrl+Q',
                               command=self._on_close)
            m_act = self._menu(menubar)
            m_act.add_command(label='Load database', accelerator='Ctrl+L',
                              command=lambda: self._on_load())
            m_act.add_command(label='Generate suite', accelerator='Ctrl+G',
                              command=lambda: self._on_generate())
            m_act.add_command(label='Copy CLI command', accelerator='Ctrl+K',
                              command=self._on_cli)
            m_act.add_separator()
            m_act.add_command(label='Clear log', command=self._clear_log)
            m_help = self._menu(menubar)
            m_help.add_command(label='Option reference', accelerator='F1',
                               command=self._show_reference)
            m_help.add_command(label='Quick guide', command=self._show_guide)
            m_help.add_separator()
            m_help.add_command(label='About', command=self._show_about)
            for label, menu in (('File', m_file), ('Actions', m_act),
                                ('Help', m_help)):
                menubar.add_cascade(label=label, menu=menu)
            self.config(menu=menubar)
            self.bind_all('<Control-l>', lambda e: self._on_load())
            self.bind_all('<Control-L>', lambda e: self._on_load())
            self.bind_all('<Control-g>', lambda e: self._on_generate())
            self.bind_all('<Control-G>', lambda e: self._on_generate())
            self.bind_all('<Control-o>', lambda e: self._menu_open_json())
            self.bind_all('<Control-O>', lambda e: self._menu_open_json())
            self.bind_all('<Control-k>', lambda e: self._on_cli())
            self.bind_all('<Control-K>', lambda e: self._on_cli())
            self.bind_all('<Control-q>', lambda e: self._on_close())
            self.bind_all('<Control-Q>', lambda e: self._on_close())
            self.bind_all('<F1>', lambda e: self._show_reference())

        # ------------------------------------------------------ layout
        def _build_ui(self):
            self.configure(bg=PALETTE['panel'])
            root = ttk.Frame(self)
            root.pack(fill='both', expand=True, padx=10, pady=8)

            header = ttk.Frame(root)
            header.pack(fill='x')
            ttk.Label(header, text=APP_NAME, style='Title.TLabel') \
                .pack(side='left')
            ttk.Label(header, text='   %s' % APP_SUBTITLE,
                      style='Muted.TLabel').pack(side='left')

            paned = ttk.PanedWindow(root, orient='horizontal')
            paned.pack(fill='both', expand=True, pady=(8, 0))

            left = ttk.Frame(paned, width=430)
            left.pack_propagate(False)
            right = ttk.Notebook(paned)
            paned.add(left, weight=0)
            paned.add(right, weight=1)

            self._build_left(left)
            self._build_right(right)

            status = ttk.Frame(root)
            status.pack(fill='x', pady=(8, 0))
            self.v_status = tk.StringVar(value='')
            ttk.Label(status, textvariable=self.v_status,
                      style='Muted.TLabel').pack(side='left')
            self.progress = ttk.Progressbar(status, mode='indeterminate',
                                            length=130)
            self.progress.pack(side='right')
            self.v_counts = tk.StringVar(value='')
            ttk.Label(status, textvariable=self.v_counts,
                      style='Muted.TLabel').pack(side='right', padx=(0, 14))

            ttk.Label(status, text='Scale:', style='Muted.TLabel') \
                .pack(side='right')
            self.v_scale = tk.StringVar(value='%.2f' % self.scale_factor)
            self.scale_spin = ttk.Spinbox(
                status, from_=0.5, to=3.0, increment=0.1, width=5,
                textvariable=self.v_scale)
            self.scale_spin.pack(side='right', padx=(4, 10))
            self.scale_apply_btn = self.button(status, 'Apply scale',
                                              self._apply_gui_scale)
            self.scale_apply_btn.pack(side='right', padx=(4, 6))

        def _attach_help(self, widget, text, key=None):
            Tooltip(self, widget, text)
            if key:
                widget.bind('<FocusIn>',
                            lambda e, k=key: self.show_help(k), add='+')
                widget.bind('<Enter>',
                            lambda e, k=key: self.show_help(k), add='+')

        def _row(self, parent, key, widget, full=False):
            """Label + widget row; wires tooltip + help pane."""
            opt = OPTIONS[key]
            row = ttk.Frame(parent)
            row.pack(fill='x', pady=(4, 0))
            lbl = ttk.Label(row, text=opt['label'] + ':',
                            style='Muted.TLabel', width=19, anchor='nw')
            lbl.pack(side='left', pady=(3, 0))
            holder = ttk.Frame(row)
            holder.pack(side='left', fill='x', expand=True)
            widget(holder)
            for w in (lbl, row):
                self._attach_help(w, opt['help'], key)
            return row

        def _build_left(self, parent):
            # -- 1 data source
            box = ttk.LabelFrame(parent, text=SECTIONS[0][1])
            box.pack(fill='x', pady=(0, 6), ipadx=6, ipady=5)

            srcrow = ttk.Frame(box)
            srcrow.pack(fill='x', pady=(3, 0))
            self.v_source = tk.StringVar(value=DEFAULTS['source_mode'])
            rb1 = ttk.Radiobutton(srcrow, text='Download from URL',
                                  value='url', variable=self.v_source,
                                  command=self._sync_source_state)
            rb2 = ttk.Radiobutton(srcrow, text='Local file',
                                  value='file', variable=self.v_source,
                                  command=self._sync_source_state)
            rb1.pack(side='left')
            rb2.pack(side='left', padx=(14, 0))
            for rb in (rb1, rb2):
                self._attach_help(rb, OPTIONS['source_mode']['help'],
                                  'source_mode')

            def w_url(holder):
                self.v_url = tk.StringVar(value=DEFAULTS['url'])
                self.e_url = ttk.Entry(holder, textvariable=self.v_url)
                self.e_url.pack(side='left', fill='x', expand=True)
                self._attach_help(self.e_url, OPTIONS['url']['help'], 'url')
            self._row(box, 'url', w_url)

            def w_file(holder):
                fr = ttk.Frame(holder)
                fr.pack(fill='x')
                self.v_file = tk.StringVar(value=DEFAULTS['file'])
                self.e_file = ttk.Entry(fr, textvariable=self.v_file)
                self.e_file.pack(side='left', fill='x', expand=True)
                b = self.button(fr, 'Browse\u2026',
                                lambda: self._browse('file'))
                b.pack(side='left', padx=(6, 0))
                self._browse_btn_file = b
            self._row(box, 'file', w_file)

            # -- 2 output
            box = ttk.LabelFrame(parent, text=SECTIONS[1][1])
            box.pack(fill='x', pady=(0, 6), ipadx=6, ipady=5)

            def w_out(holder):
                fr = ttk.Frame(holder)
                fr.pack(fill='x')
                self.v_out_dir = tk.StringVar(value=DEFAULTS['out_dir'])
                e = ttk.Entry(fr, textvariable=self.v_out_dir)
                e.pack(side='left', fill='x', expand=True)
                b = self.button(fr, 'Browse\u2026',
                                lambda: self._browse('out_dir'))
                b.pack(side='left', padx=(6, 0))
                self._attach_help(e, OPTIONS['out_dir']['help'], 'out_dir')
                self._attach_help(b, OPTIONS['out_dir']['help'], 'out_dir')
            self._row(box, 'out_dir', w_out)

            def w_merge(holder):
                self.v_merge = tk.BooleanVar(value=DEFAULTS['merge_enabled'])
                cb = ttk.Checkbutton(
                    holder, text='merge into existing lamedb',
                    variable=self.v_merge, command=self._sync_merge_state)
                cb.pack(side='left')
                self._attach_help(cb, OPTIONS['merge_enabled']['help'],
                                  'merge_enabled')
            self._row(box, 'merge_enabled', w_merge)

            def w_mpath(holder):
                fr = ttk.Frame(holder)
                fr.pack(fill='x')
                self.v_merge_path = tk.StringVar(value=DEFAULTS['merge_path'])
                self.e_merge = ttk.Entry(fr, textvariable=self.v_merge_path)
                self.e_merge.pack(side='left', fill='x', expand=True)
                b = self.button(fr, 'Browse\u2026',
                                lambda: self._browse('merge_path'))
                b.pack(side='left', padx=(6, 0))
                self._attach_help(self.e_merge, OPTIONS['merge_path']['help'],
                                  'merge_path')
            self._row(box, 'merge_path', w_mpath)

            def w_edit(holder):
                self.v_edit = tk.BooleanVar(value=DEFAULTS['edit_enabled'])
                cb = ttk.Checkbutton(
                    holder, text='edit external lamedb',
                    variable=self.v_edit, command=self._sync_edit_state)
                cb.pack(side='left')
                self._attach_help(cb, OPTIONS['edit_enabled']['help'],
                                  'edit_enabled')
            self._row(box, 'edit_enabled', w_edit)

            def w_epath(holder):
                fr = ttk.Frame(holder)
                fr.pack(fill='x')
                self.v_edit_path = tk.StringVar(value=DEFAULTS['edit_path'])
                self.e_edit = ttk.Entry(fr, textvariable=self.v_edit_path)
                self.e_edit.pack(side='left', fill='x', expand=True)
                b = self.button(fr, 'Browse\\u2026',
                                lambda: self._browse('edit_path'))
                b.pack(side='left', padx=(6, 0))
                self._attach_help(self.e_edit, OPTIONS['edit_path']['help'],
                                  'edit_path')
            self._row(box, 'edit_path', w_epath)

            def w_fmt(holder):
                self.v_format = tk.StringVar(value=DEFAULTS['lamedb_format'])
                cb = ttk.Combobox(holder, textvariable=self.v_format,
                                  state='readonly', width=5,
                                  values=('v4',))
                cb.pack(side='left')
                note = ttk.Label(holder, text='v4 = current images only',
                                 style='Muted.TLabel')
                note.pack(side='left', padx=(8, 0))
                for w in (cb, note):
                    self._attach_help(w, OPTIONS['lamedb_format']['help'],
                                      'lamedb_format')
            self._row(box, 'lamedb_format', w_fmt)

            def w_bq(holder):
                self.v_bouquet = tk.StringVar(value=DEFAULTS['bouquet'])
                e = ttk.Entry(holder, textvariable=self.v_bouquet)
                e.pack(side='left', fill='x', expand=True)
                self._attach_help(e, OPTIONS['bouquet']['help'], 'bouquet')
            self._row(box, 'bouquet', w_bq)

            def w_softcam(holder):
                self.v_softcam_key = tk.BooleanVar(value=DEFAULTS['softcam_key_enabled'])
                cb = ttk.Checkbutton(
                    holder, text='also write SoftCam.Key (dummy BISS keys)',
                    variable=self.v_softcam_key, command=self._sync_softcam_state)
                cb.pack(side='left')
                self._attach_help(cb, OPTIONS['softcam_key_enabled']['help'],
                                  'softcam_key_enabled')
            self._row(box, 'softcam_key_enabled', w_softcam)

            def w_softcam_path(holder):
                fr = ttk.Frame(holder)
                fr.pack(fill='x')
                self.v_softcam_key_path = tk.StringVar(value=DEFAULTS['softcam_key_path'])
                self.e_softcam_key_path = ttk.Entry(fr, textvariable=self.v_softcam_key_path)
                self.e_softcam_key_path.pack(side='left', fill='x', expand=True)
                b = self.button(fr, 'Browse\u2026',
                                lambda: self._browse('softcam_key_path'))
                b.pack(side='left', padx=(6, 0))
                self._attach_help(self.e_softcam_key_path, OPTIONS['softcam_key_path']['help'],
                                  'softcam_key_path')
                self._attach_help(b, OPTIONS['softcam_key_path']['help'],
                                  'softcam_key_path')
            self._row(box, 'softcam_key_path', w_softcam_path)

            # -- 3 satellite
            box = ttk.LabelFrame(parent, text=SECTIONS[2][1])
            box.pack(fill='x', pady=(0, 6), ipadx=6, ipady=5)

            def w_pos(holder):
                fr = ttk.Frame(holder)
                fr.pack(fill='x')
                self.v_position = tk.StringVar(value=str(DEFAULTS['position']))
                sp = ttk.Spinbox(fr, from_=-1800, to=1800, increment=10,
                                 width=7, textvariable=self.v_position,
                                 command=self._update_pos_label)
                sp.pack(side='left')
                self.v_pos_label = tk.StringVar(value='')
                lbl = ttk.Label(fr, textvariable=self.v_pos_label,
                                style='Muted.TLabel')
                lbl.pack(side='left', padx=(10, 0))
                sp.bind('<KeyRelease>', lambda e: self._update_pos_label())
                self._attach_help(sp, OPTIONS['position']['help'], 'position')
                self._attach_help(lbl, OPTIONS['position']['help'], 'position')
            self._row(box, 'position', w_pos)

            # -- 4 relay
            box = ttk.LabelFrame(parent, text=SECTIONS[3][1])
            box.pack(fill='x', pady=(0, 6), ipadx=6, ipady=5)

            def w_relay(holder):
                fr = ttk.Frame(holder)
                fr.pack(fill='x')
                self.v_relay_host = tk.StringVar(value=DEFAULTS['relay_host'])
                self.v_relay_port = tk.StringVar(value=str(DEFAULTS['relay_port']))
                ttk.Label(fr, text='http://', style='Muted.TLabel') \
                    .pack(side='left')
                e1 = ttk.Entry(fr, textvariable=self.v_relay_host, width=12)
                e1.pack(side='left', padx=(2, 0))
                ttk.Label(fr, text=':', style='Muted.TLabel').pack(side='left')
                e2 = ttk.Entry(fr, textvariable=self.v_relay_port, width=6)
                e2.pack(side='left')
                for w in (e1, e2):
                    self._attach_help(w, OPTIONS['relay_host']['help'],
                                      'relay_host')
            self._row(box, 'relay_host', w_relay)

            def w_outaddr(holder):
                fr = ttk.Frame(holder)
                fr.pack(fill='x')
                self.v_out_host = tk.StringVar(value=DEFAULTS['out_host'])
                self.v_http_port = tk.StringVar(value=str(DEFAULTS['http_port']))
                e1 = ttk.Entry(fr, textvariable=self.v_out_host, width=12)
                e1.pack(side='left')
                ttk.Label(fr, text=':', style='Muted.TLabel').pack(side='left')
                e2 = ttk.Entry(fr, textvariable=self.v_http_port, width=6)
                e2.pack(side='left')
                ttk.Label(fr, text='/<provider>/pid<PID>',
                          style='Muted.TLabel').pack(side='left', padx=(8, 0))
                for w in (e1, e2):
                    self._attach_help(w, OPTIONS['out_host']['help'], 'out_host')
            self._row(box, 'out_host', w_outaddr)

            def w_script(holder):
                self.v_script = tk.StringVar(value=DEFAULTS['relay_script'])
                e = ttk.Entry(holder, textvariable=self.v_script)
                e.pack(side='left', fill='x', expand=True)
                self._attach_help(e, OPTIONS['relay_script']['help'],
                                  'relay_script')
            self._row(box, 'relay_script', w_script)

            # -- action buttons
            btns = ttk.Frame(parent)
            btns.pack(fill='x', pady=(4, 6))
            self.btn_load = self.button(btns, '\u21bb  Load database',
                                        self._on_load)
            self.btn_gen = self.button(btns, '\u25b6  Generate suite',
                                       self._on_generate, kind='accent')
            self.btn_cli = self.button(btns, 'Copy CLI', self._on_cli)
            self.btn_load.pack(side='left')
            self.btn_gen.pack(side='left', padx=(8, 0))
            self.btn_cli.pack(side='left', padx=(8, 0))
            for btn, key in ((self.btn_load, 'load'), (self.btn_gen, 'generate'),
                             (self.btn_cli, 'cli')):
                self._attach_help(btn, ACTION_HELP[key])
            self._busy_widgets = [self.btn_load, self.btn_gen, self.btn_cli]

            # -- help pane
            helpbox = ttk.LabelFrame(parent, text='Help \u2014 focus or hover '
                                                  'an option')
            helpbox.pack(fill='both', expand=True, ipadx=6, ipady=4)
            self.help_title = tk.StringVar(value='')
            ttk.Label(helpbox, textvariable=self.help_title,
                      style='Muted.TLabel').pack(anchor='w', padx=8,
                                                 pady=(4, 0))
            self.help_text = tk.Text(helpbox, height=7, wrap='word',
                                     bg=PALETTE['bg'], fg=PALETTE['fg'],
                                     relief='flat', highlightthickness=0,
                                     padx=8, pady=5,
                                     font=self.fonts['small'], state='disabled')
            self.help_text.pack(fill='both', expand=True, padx=6, pady=(0, 4))

        def _build_right(self, notebook):
            C = PALETTE

            # ---- preview tab
            prev = ttk.Frame(notebook)
            notebook.add(prev, text='  Preview  ')
            bar = ttk.Frame(prev)
            bar.pack(fill='x', padx=8, pady=(8, 4))
            ttk.Label(bar, text='Filter:', style='Muted.TLabel') \
                .pack(side='left')
            self.v_filter = tk.StringVar(value='')
            filt = ttk.Entry(bar, textvariable=self.v_filter, width=28)
            filt.pack(side='left', padx=(6, 0))
            self.v_filter.trace_add('write', lambda *_: self._populate_tree())
            Tooltip(self, filt, 'Type to filter the tree by transponder, PID, '
                                'feed name or channel name (case-insensitive). '
                                'Double-click a PID row to inspect the exact '
                                'generated artifacts for that stream.')
            self.v_tree_info = tk.StringVar(value='No database loaded yet.')
            ttk.Label(bar, textvariable=self.v_tree_info,
                      style='Muted.TLabel').pack(side='right')

            wrap = ttk.Frame(prev)
            wrap.pack(fill='both', expand=True, padx=8, pady=(0, 8))
            self.tree = ttk.Treeview(wrap, columns=('detail',),
                                     selectmode='browse')
            ysb = ttk.Scrollbar(wrap, orient='vertical', command=self.tree.yview)
            self.tree.configure(yscrollcommand=ysb.set)
            self.tree.pack(side='left', fill='both', expand=True)
            ysb.pack(side='right', fill='y')
            self.tree.heading('#0', text='Transponder / stream / channel',
                              anchor='w')
            self.tree.heading('detail', text='Details', anchor='w')
            self.tree.column('#0', width=430, stretch=True, anchor='w')
            self.tree.column('detail', width=330, anchor='w')
            self.tree.tag_configure('tp', foreground=C['accent'],
                                    font=self.fonts['bold'])
            self.tree.tag_configure('biss', foreground=C['warn'])
            self.tree.tag_configure('fta', foreground=C['ok'])
            self.tree.tag_configure('ch', foreground=C['muted'])
            self.tree.bind('<Double-1>', self._on_tree_double)

            # ---- output tab
            out = ttk.Frame(notebook)
            notebook.add(out, text='  Generated output  ')
            bar = ttk.Frame(out)
            bar.pack(fill='x', padx=8, pady=(8, 4))
            ttk.Label(bar, text='File:', style='Muted.TLabel').pack(side='left')
            self.v_outfile = tk.StringVar(value='')
            self.cb_outfile = ttk.Combobox(bar, textvariable=self.v_outfile,
                                           state='readonly', width=28)
            self.cb_outfile.pack(side='left', padx=(6, 0))
            self.cb_outfile.bind('<<ComboboxSelected>>',
                                 lambda e: self._show_output_file())
            b1 = self.button(bar, 'Save as\u2026', self._save_output_as)
            b2 = self.button(bar, 'Copy', self._copy_output)
            b1.pack(side='left', padx=(10, 0))
            b2.pack(side='left', padx=(6, 0))
            Tooltip(self, b1, 'Save the currently displayed generated file '
                              'under a new name.')
            Tooltip(self, b2, 'Copy the currently displayed generated file '
                              'to the clipboard.')
            self.v_out_info = tk.StringVar(value='Generate the suite to '
                                                 'populate this view.')
            ttk.Label(bar, textvariable=self.v_out_info,
                      style='Muted.TLabel').pack(side='right')

            wrap = ttk.Frame(out)
            wrap.pack(fill='both', expand=True, padx=8, pady=(0, 8))
            self.out_text = tk.Text(wrap, wrap='none', undo=False,
                                    bg=C['tree'], fg=C['fg'], relief='flat',
                                    highlightthickness=0, padx=8, pady=6,
                                    font=self.fonts['mono'], state='disabled')
            xsb = ttk.Scrollbar(wrap, orient='horizontal',
                                command=self.out_text.xview)
            ysb2 = ttk.Scrollbar(wrap, orient='vertical',
                                 command=self.out_text.yview)
            self.out_text.configure(xscrollcommand=xsb.set,
                                    yscrollcommand=ysb2.set)
            self.out_text.grid(row=0, column=0, sticky='nsew')
            ysb2.grid(row=0, column=1, sticky='ns')
            xsb.grid(row=1, column=0, sticky='ew')
            wrap.rowconfigure(0, weight=1)
            wrap.columnconfigure(0, weight=1)

            # ---- log tab
            logf = ttk.Frame(notebook)
            notebook.add(logf, text='  Log  ')
            wrap = ttk.Frame(logf)
            wrap.pack(fill='both', expand=True, padx=8, pady=8)
            self.log_text = tk.Text(wrap, wrap='word', undo=False,
                                    bg=C['bg'], fg=C['fg'], relief='flat',
                                    highlightthickness=0, padx=8, pady=6,
                                    font=self.fonts['mono_small'],
                                    state='disabled')
            ysb3 = ttk.Scrollbar(wrap, orient='vertical',
                                 command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=ysb3.set)
            self.log_text.pack(side='left', fill='both', expand=True)
            ysb3.pack(side='right', fill='y')
            self.log_text.tag_configure('muted', foreground=C['muted'])
            self._log_line('welcome to the %s' % APP_NAME, muted=True)

        # ------------------------------------------------- help pane
        def show_help(self, key):
            opt = OPTIONS.get(key)
            if not opt:
                return
            self.help_title.set(opt['label'])
            self.help_text.configure(state='normal')
            self.help_text.delete('1.0', 'end')
            self.help_text.insert('1.0', opt['help'])
            self.help_text.configure(state='disabled')

        # ---------------------------------------------------- helpers
        FONT_BASE_SIZES = {
            'base': 9, 'small': 8, 'bold': 9, 'h1': 13, 'mono': 9,
            'mono_small': 8,
        }

        def _apply_gui_scale(self, silent=False, event=None):
            try:
                factor = float(self.v_scale.get())
            except (TypeError, ValueError):
                factor = self.scale_factor
            factor = min(3.0, max(0.5, factor))
            self.scale_factor = factor
            self.v_scale.set('%.2f' % factor)
            for name, base in self.FONT_BASE_SIZES.items():
                sz = max(7, int(round(base * factor)))
                self.fonts[name].configure(size=sz)
            try:
                self.tk.call('tk', 'scaling', factor * 1.33)
            except tk.TclError:
                pass
            if not silent:
                self.status('GUI scale set to %.2f. Saved for next start.'
                            % factor)

        def _reconfigure_fonts(self):
            self._apply_gui_scale(silent=True)

        def _set_text(self, widget, content):
            widget.configure(state='normal')
            widget.delete('1.0', 'end')
            widget.insert('1.0', content)
            widget.configure(state='disabled')

        def _log_line(self, line, muted=False):
            stamp = time.strftime('%H:%M:%S')
            self.log_text.configure(state='normal')
            if muted:
                self.log_text.insert('end', '%s  %s\n' % (stamp, line), 'muted')
            else:
                self.log_text.insert('end', '%s  %s\n' % (stamp, line))
            self.log_text.see('end')
            self.log_text.configure(state='disabled')

        def _clear_log(self):
            self.log_text.configure(state='normal')
            self.log_text.delete('1.0', 'end')
            self.log_text.configure(state='disabled')

        def status(self, text, kind=None):
            self.v_status.set(text)

        def collect_values(self):
            return {
                'source_mode': self.v_source.get(),
                'url': self.v_url.get().strip(),
                'file': self.v_file.get().strip(),
                'out_dir': self.v_out_dir.get().strip(),
                'bouquet': self.v_bouquet.get().strip() or DEFAULTS['bouquet'],
                'merge_enabled': bool(self.v_merge.get()),
                'merge_path': self.v_merge_path.get().strip(),
                'edit_enabled': bool(self.v_edit.get()),
                'edit_path': self.v_edit_path.get().strip(),
                'softcam_key_enabled': bool(self.v_softcam_key.get()),
                'softcam_key_path': self.v_softcam_key_path.get().strip(),
                'lamedb_format': self.v_format.get() or 'v4',
                'position': self.v_position.get().strip() or '-300',
                'relay_host': self.v_relay_host.get().strip(),
                'relay_port': self.v_relay_port.get().strip() or '8001',
                'out_host': self.v_out_host.get().strip(),
                'http_port': self.v_http_port.get().strip() or '9999',
                'relay_script': self.v_script.get().strip(),
                'gui_scale': float(self.v_scale.get() or self.scale_factor),
            }

        def _sync_source_state(self):
            mode = self.v_source.get()
            for entry in (self.e_url,):
                entry.configure(state='normal' if mode == 'url' else 'disabled')
            self.e_file.configure(
                state='normal' if mode == 'file' else 'disabled')
            self._browse_btn_file.configure(
                state='normal' if mode == 'file' else 'disabled')

        def _sync_merge_state(self):
            state = 'normal' if self.v_merge.get() else 'disabled'
            self.e_merge.configure(state=state)

        def _sync_edit_state(self):
            state = 'normal' if self.v_edit.get() else 'disabled'
            self.e_edit.configure(state=state)

        def _sync_softcam_state(self):
            state = 'normal' if self.v_softcam_key.get() else 'disabled'
            self.e_softcam_key_path.configure(state=state)

        def _update_pos_label(self):
            try:
                pos = int(self.v_position.get())
                ns = namespace_for(pos)
                self.v_pos_label.set(
                    '= %s  (namespace 0x%08X)' % (position_label(pos), ns))
            except ValueError:
                self.v_pos_label.set('= ?')

        def _browse(self, what):
            if what == 'file':
                path = filedialog.askopenfilename(
                    parent=self, title='Select sat_data.json',
                    filetypes=[('JSON database', '*.json'), ('All files', '*')])
                if path:
                    self.v_file.set(path)
                    self.v_source.set('file')
                    self._sync_source_state()
            elif what == 'out_dir':
                path = filedialog.askdirectory(
                    parent=self, title='Select output directory')
                if path:
                    self.v_out_dir.set(path)
            elif what == 'merge_path':
                path = filedialog.askopenfilename(
                    parent=self, title='Select the lamedb to merge into',
                    filetypes=[('lamedb', 'lamedb*'), ('All files', '*')])
                if path:
                    self.v_merge_path.set(path)
            elif what == 'edit_path':
                path = filedialog.askopenfilename(
                    parent=self, title='Select the lamedb to edit',
                    filetypes=[('lamedb', 'lamedb*'), ('All files', '*')])
                if path:
                    self.v_edit_path.set(path)
            elif what == 'softcam_key_path':
                path = filedialog.asksaveasfilename(
                    parent=self, title='Select SoftCam.Key output file',
                    initialfile='SoftCam.Key',
                    filetypes=[('SoftCam.Key', '*.Key *SoftCam.Key'), ('All files', '*')])
                if path:
                    self.v_softcam_key_path.set(path)
                    self.v_softcam_key.set(True)

        # --------------------------------------------------- workers
        def _set_busy(self, busy, what=''):
            self.busy = busy
            state = 'disabled' if busy else 'normal'
            for w in self._busy_widgets:
                w.configure(state=state)
            if busy:
                self.progress.start(12)
                self.status('Working: %s \u2026' % what)
            else:
                self.progress.stop()

        def _start_worker(self, kind, task):
            if self.busy:
                messagebox.showinfo(APP_NAME, 'Busy \u2014 please wait for the '
                                              'current task to finish.',
                                    parent=self)
                return

            def runner():
                try:
                    payload = task()
                    self.q.put(('ok', kind, payload))
                except SuiteError as exc:
                    self.q.put(('fail', kind, str(exc)))
                except Exception:
                    self.q.put(('fail', kind,
                                'Unexpected error:\n' + traceback.format_exc()))
            self._set_busy(True, {'load': 'loading database',
                                  'generate': 'generating suite'}[kind])
            threading.Thread(target=runner, daemon=True).start()

        def _on_load(self):
            self._start_worker('load', self._task_load)

        def _on_generate(self):
            self._start_worker('generate', self._task_generate)

        def _task_load(self):
            v = self.collect_values()
            problems = validate_values(v)
            if problems:
                raise SuiteError('\n'.join(problems))
            with contextlib.redirect_stdout(self._log_stream), \
                    contextlib.redirect_stderr(self._log_stream):
                loaded = load_database(v)
            return loaded

        def _task_generate(self):
            v = self.collect_values()
            problems = validate_values(v)
            if problems:
                raise SuiteError('\n'.join(problems))
            with contextlib.redirect_stdout(self._log_stream), \
                    contextlib.redirect_stderr(self._log_stream):
                loaded = None
                if self.txps is None:
                    loaded = load_database(v)
                    txps, streams = loaded[1], loaded[2]
                else:
                    txps, streams = self.txps, self.streams
                result = generate_artifacts(v, txps, streams)
            return {'loaded': loaded, 'result': result, 'values': v}

        def _poll(self):
            try:
                while True:
                    msg = self.q.get_nowait()
                    if msg[0] == 'log':
                        self._log_line(msg[1])
                    elif msg[0] == 'ok':
                        self._finished_ok(msg[1], msg[2])
                    else:
                        self._finished_fail(msg[1], msg[2])
            except queue.Empty:
                pass
            self.after(80, self._poll)

        def _finished_ok(self, kind, payload):
            self._set_busy(False)
            if kind == 'load':
                self._apply_loaded(payload)
                self.status('Database loaded \u2014 ready to generate.')
            else:
                if payload['loaded']:
                    self._apply_loaded(payload['loaded'])
                result = payload['result']
                self.outputs = result['outputs']
                names = list(self.outputs.keys())
                self.cb_outfile.configure(values=names)
                if names:
                    self.cb_outfile.set(names[0])
                    self._show_output_file()
                self.v_out_info.set('%d files written'
                                    % len(result['written']))
                self.status('Suite generated.')
                self._log_line('generation finished: %s'
                               % '; '.join(result['summary']), muted=True)
                messagebox.showinfo(
                    APP_NAME,
                    'Suite generated successfully:\n\n'
                    + '\n'.join(result['summary'])
                    + '\n\nRestart Enigma2 after replacing/merging the lamedb.',
                    parent=self)

        def _finished_fail(self, kind, message):
            self._set_busy(False)
            self.status('Failed \u2014 see the message / log.')
            self._log_line('ERROR (%s)' % kind, muted=True)
            for line in message.splitlines():
                self._log_line('  ' + line, muted=True)
            messagebox.showerror(APP_NAME, message, parent=self)

        def _apply_loaded(self, loaded):
            self.data, self.txps, self.streams, self.raw_by_pid = loaded
            self._populate_tree()
            n_ch = sum(len(s['channels']) for s in self.streams)
            n_lists = sum(len(tp['streams']) for tp in self.txps) \
                - len(self.streams)
            self.v_counts.set('%d transponders \u00b7 %d PID streams \u00b7 '
                              '%d channels' % (len(self.txps), len(self.streams),
                                               n_ch))
            self._log_line('database loaded: %d transponders, %d PID streams, '
                           '%d channels, %d whole-tp lists skipped'
                           % (len(self.txps), len(self.streams), n_ch, n_lists))

        # ---------------------------------------------------- preview
        def _populate_tree(self):
            query = self.v_filter.get().strip().lower()
            tree = self.tree
            tree.delete(*tree.get_children())
            self._stream_items = {}

            def match(*parts):
                return not query or any(query in (p or '').lower()
                                        for p in parts)

            if not self.txps:
                self.v_tree_info.set('No database loaded yet.')
                return
            mod_names = {0: 'QPSK', 2: '8PSK', 3: '16APSK', 4: '32APSK'}
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
                for s in self.streams:
                    if s['txp'] is not tp:
                        continue
                    label = s['title'].split(' - ')[-1] if ' - ' in s['title'] \
                        else s['title']
                    s_text = 'PID %d \u00b7 %s \u00b7 %s' % (
                        s['pid'], s['encryption'] or 'FTA', label)
                    s_detail = '%s \u00b7 SID %d \u00b7 CPID %d \u00b7 %d channel%s' % (
                        feed_service_name(s), s['sid'], s['cpid'],
                        len(s['channels']),
                        '' if len(s['channels']) == 1 else 's')
                    if query and not (tp_match or match(s_text, s_detail)):
                        continue
                    children.append((s, s_text, s_detail))
                if query and not (tp_match or children):
                    continue
                iid = tree.insert('', 'end', text=tp_text, values=(tp_detail,),
                                  open=not query, tags=('tp',))
                shown['tp'] += 1
                for s, s_text, s_detail in children:
                    tag = 'biss' if 'BISS' in s['encryption'] else 'fta'
                    siid = tree.insert(iid, 'end', text=s_text,
                                       values=(s_detail,), tags=(tag,))
                    shown['stream'] += 1
                    self._stream_items[siid] = s['pid']
                    for ch in s['channels']:
                        tree.insert(siid, 'end',
                                    text='%s %s' % ('(RADIO)' if ch['radio']
                                                    else '(TV)', ch['name']),
                                    values=('SID %d' % ch['sid'],),
                                    tags=('ch',))
            if query:
                self.v_tree_info.set('filter: %d transponders, %d streams'
                                     % (shown['tp'], shown['stream']))
            else:
                self.v_tree_info.set('%d transponders \u00b7 %d PID streams'
                                     % (len(self.txps), len(self.streams)))

        def _on_tree_double(self, event):
            iid = self.tree.identify_row(event.y)
            if not iid:
                return
            pid = getattr(self, '_stream_items', {}).get(iid)
            if pid is not None:
                self._show_stream_dialog(pid)

        # ------------------------------------------------- inspectors
        def _popup(self, title, size='780x560'):
            C = PALETTE
            win = tk.Toplevel(self)
            win.title(title)
            win.configure(bg=C['panel'])
            win.geometry(size)
            win.transient(self)
            win.grab_set()
            return win

        def _show_stream_dialog(self, pid):
            stream = next((s for s in self.streams if s['pid'] == pid), None)
            if stream is None:
                return
            raw = self.raw_by_pid.get(pid, {})
            v = self.collect_values()
            ns = values_namespace(v)
            tp = stream['txp']
            tp_e, svc_e = build_lamedb_entries([tp], [stream], ns.position)
            with contextlib.redirect_stdout(self._log_stream):
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
                'Cache flags  c:18%04x = data PID (stream PID) \u00b7 '
                'c:19%04x = cpid (PMT)' % (pid, cpid),
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
            win = self._popup('Stream inspector \u2014 PID %d' % pid)
            bar = ttk.Frame(win)
            bar.pack(fill='x', padx=8, pady=(8, 4))
            self.button(bar, 'Copy all', lambda: self._copy_to_clipboard(
                content)).pack(side='left')
            ttk.Label(bar, text='exact artifacts generated with the current '
                                'settings', style='Muted.TLabel') \
                .pack(side='left', padx=(10, 0))
            txt = tk.Text(win, wrap='none', bg=PALETTE['tree'], fg=PALETTE['fg'],
                          relief='flat', highlightthickness=0, padx=8, pady=6,
                          font=self.fonts['mono'])
            ysb = ttk.Scrollbar(win, orient='vertical', command=txt.yview)
            xsb = ttk.Scrollbar(win, orient='horizontal', command=txt.xview)
            txt.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
            txt.grid(row=1, column=0, sticky='nsew', padx=(8, 0), pady=(0, 8))
            ysb.grid(row=1, column=1, sticky='ns', pady=(0, 8))
            xsb.grid(row=2, column=0, sticky='ew', padx=(8, 0))
            win.rowconfigure(1, weight=1)
            win.columnconfigure(0, weight=1)
            txt.insert('1.0', content)
            txt.configure(state='disabled')

        def _copy_to_clipboard(self, text):
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
            self.status('Copied to clipboard.')

        def _show_output_file(self):
            name = self.v_outfile.get()
            content = self.outputs.get(name)
            if content is not None:
                self._set_text(self.out_text, content)

        def _save_output_as(self):
            name = self.v_outfile.get()
            content = self.outputs.get(name)
            if content is None:
                return
            path = filedialog.asksaveasfilename(
                parent=self, title='Save as\u2026', initialfile=name)
            if path:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                self.status('Saved %s' % path)

        def _copy_output(self):
            name = self.v_outfile.get()
            content = self.outputs.get(name)
            if content is not None:
                self._copy_to_clipboard(content)

        # ------------------------------------------------ doc windows
        def _doc_window(self, title, content):
            win = self._popup(title, '820x600')
            bar = ttk.Frame(win)
            bar.pack(fill='x', padx=8, pady=(8, 4))
            self.button(bar, 'Copy', lambda: self._copy_to_clipboard(
                content)).pack(side='left')
            txt = tk.Text(win, wrap='word', bg=PALETTE['tree'], fg=PALETTE['fg'],
                          relief='flat', highlightthickness=0, padx=10, pady=8,
                          font=self.fonts['mono_small'])
            ysb = ttk.Scrollbar(win, orient='vertical', command=txt.yview)
            txt.configure(yscrollcommand=ysb.set)
            txt.grid(row=1, column=0, sticky='nsew', padx=(8, 0), pady=(0, 8))
            ysb.grid(row=1, column=1, sticky='ns', pady=(0, 8))
            win.rowconfigure(1, weight=1)
            win.columnconfigure(0, weight=1)
            txt.insert('1.0', content)
            txt.configure(state='disabled')

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
            lines.append('  F1      this reference       Ctrl+Q  quit')
            self._doc_window('Option reference', '\n'.join(lines))

        def _show_guide(self):
            self._doc_window('Quick guide', QUICK_GUIDE)

        def _show_about(self):
            messagebox.showinfo(
                'About',
                '%s\n%s\n\nGUI front-end for Abertis_DX_Suite.py\n'
                'Engine: lamedb + astra relay + Enigma2 bouquet generator '
                'for the Abertis / Cellnex DTT feeds on Hispasat 30.0\u00b0W.\n\n'
                'Standard library only (tkinter). Data source:\n%s'
                % (APP_NAME, APP_SUBTITLE, DEFAULT_JSON_URL),
                parent=self)

        # ----------------------------------------------- CLI command
        def _on_cli(self):
            v = self.collect_values()
            problems = validate_values(v)
            cmd = build_cli_command(v)
            win = self._popup('CLI command', '760x200')
            bar = ttk.Frame(win)
            bar.pack(fill='x', padx=8, pady=(8, 4))
            self.button(bar, 'Copy',
                        lambda: self._copy_to_clipboard(cmd)).pack(side='left')
            hint = ('equivalent command line for the current settings'
                    if not problems else
                    'note: current settings have validation problems')
            ttk.Label(bar, text=hint, style='Muted.TLabel') \
                .pack(side='left', padx=(10, 0))
            txt = tk.Text(win, wrap='char', bg=PALETTE['tree'], fg=PALETTE['fg'],
                          relief='flat', highlightthickness=0, padx=10, pady=8,
                          font=self.fonts['mono'])
            txt.pack(fill='both', expand=True, padx=8, pady=(0, 8))
            txt.insert('1.0', cmd)
            txt.configure(state='disabled')

        # ------------------------------------------ profiles & state
        def _menu_open_json(self):
            self._browse('file')

        def _save_profile(self):
            path = filedialog.asksaveasfilename(
                parent=self, title='Save settings profile',
                defaultextension='.json', initialfile='dx_suite_profile.json',
                filetypes=[('DX suite profile', '*.json')])
            if not path:
                return
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'values': self.collect_values(),
                           'geometry': self.geometry()}, f, indent=2)
            self.status('Profile saved: %s' % path)

        def _load_profile(self):
            path = filedialog.askopenfilename(
                parent=self, title='Load settings profile',
                filetypes=[('DX suite profile', '*.json'),
                           ('All files', '*')])
            if not path:
                return
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    profile = json.load(f)
                self._apply_values(profile.get('values', {}))
                if profile.get('geometry'):
                    try:
                        self.geometry(profile['geometry'])
                    except tk.TclError:
                        pass
            except (OSError, ValueError) as exc:
                messagebox.showerror(APP_NAME,
                                     'Cannot load profile: %s' % exc,
                                     parent=self)
            else:
                self.status('Profile loaded: %s' % path)

        def _apply_values(self, values):
            var_map = {
                'source_mode': self.v_source,
                'url': self.v_url, 'file': self.v_file,
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
                'gui_scale': self.v_scale,
            }
            for key, var in var_map.items():
                if key in values:
                    var.set(str(values[key]))
            if 'merge_enabled' in values:
                self.v_merge.set(bool(values['merge_enabled']))
            if 'edit_enabled' in values:
                self.v_edit.set(bool(values['edit_enabled']))
            if 'edit_path' in values:
                self.v_edit_path.set(str(values['edit_path']))
            self._sync_source_state()
            self._sync_merge_state()
            self._sync_edit_state()
            self._update_pos_label()
            self._apply_gui_scale(silent=True)

        def _restore_settings(self):
            self._sync_source_state()
            self._sync_merge_state()
            self._sync_edit_state()
            self._update_pos_label()
            self._apply_gui_scale(silent=True)
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                self._apply_values(saved.get('values', {}))
                if saved.get('geometry'):
                    self.geometry(saved['geometry'])
            except (OSError, ValueError):
                pass

        def _on_close(self):
            try:
                with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                    json.dump({'values': self.collect_values(),
                               'geometry': self.geometry()}, f, indent=2)
            except OSError:
                pass
            self.destroy()


    def main():
        app = App()
        app.mainloop()

else:

    def main():
        sys.exit('tkinter is not available - install it with\n'
                 '  sudo apt install python3-tk\n'
                 'or use the command line version: Abertis_DX_Suite.py --help')


if __name__ == '__main__':
    if '--check' in sys.argv:
        run_checks()
        sys.exit(0)
    main()