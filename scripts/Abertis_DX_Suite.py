#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Abertis / Cellnex DTT feeds on Hispasat 30.0W - full DX suite generator.

Reads the transponder / stream / channel database published as JSON
(default: https://abertis-dtt.github.io/sat_data.json) and generates the three
pieces needed to receive the single-PID feeds through Enigma2 streamrelay +
astra:

  1. lamedb                    Enigma2 lamedb (v4 sectioned layout):
                              the real transponders plus one fake feed
                              service per stream PID. The service SID comes
                              from the JSON "sid" field (fallback PID+1000)
                              and the flags carry the raw stream PID in the
                              c:18 cache entry (cDATAPID) and the JSON
                              "cpid" component data PID in c:19 (cPMTPID),
                              plus CAID 2600 on BISS services so Enigma2 can
                              tune a raw PID and attach the BISS descrambler.
  2. astra.conf                one make_channel() per PID: pull the feed
                              service from streamrelay, pipe it through
                              /etc/astra/scripts/<provider> <PID> and
                              republish it per PID on
                              http://0.0.0.0:9999/<provider>/pid<PID>
  3. userbouquet.abertis.tv    Enigma2 IPTV bouquet listing the real channels
                              of every PID behind the astra outputs,
                              grouped by PID with markers.

Examples:
  ./Abertis_DX_Suite.py
  ./Abertis_DX_Suite.py --file sat_data.json --out-dir ./dx
  ./Abertis_DX_Suite.py --merge-lamedb /etc/enigma2/lamedb
"""

import argparse
import json
import os
import re
import shutil
import sys
import urllib.request
from collections import OrderedDict

DEFAULT_JSON_URL = 'https://abertis-dtt.github.io/sat_data.json'
BOUQUET_HEADER = u'#NAME \u2022 30,0 W - Abertis & Cellnex DX Suite ( streamrelay )'

# Enigma2 lamedb numeric codes
FEC_CODES = {
    '1/2': 1, '2/3': 2, '3/4': 3, '5/6': 4, '6/7': 5, '7/8': 6,
    '8/9': 7, '3/5': 8, '4/5': 9, '9/10': 10, 'AUTO': 15,
}
MODULATION_CODES = {'QPSK': 0, '8PSK': 2, '16APSK': 3, '32APSK': 4}
POLARISATION_CODES = {'H': 0, 'V': 1}

# lamedb service cache entries of a raw-PID feed service (cache ids are
# DECIMAL in the file, cf. eDVBService::cacheID in idvb.h):
#   c:03<0x1fff>  dummy pid (none)
#   c:18<pid>     cDATAPID = the raw stream PID itself
#   c:19<cpid>    cPMTPID  = component data PID from the JSON "cpid" field
# plus C:2600 = CAID 0x2600 (BISS) and f:01 (never touch the name/SDT).
BISS_CAID = '2600'

STREAM_PID_RE = re.compile(r'Stream\s+(\d+)', re.IGNORECASE)
FREQ_POL_MODE_RE = re.compile(r'^\s*([0-9]+(?:\.[0-9]+)?)\s+([HVLR])\s+(.+?)\s*$')
SERVICE_FLAG_PREFIXES = ('p:', 'c:', 'C:', 'f:', 'v:')


def namespace_for(position_tenths):
    """DVB namespace for a satellite position; West positions wrap at 3600."""
    pos = position_tenths + 3600 if position_tenths < 0 else position_tenths
    return (pos & 0xFFFF) << 16


def load_source(args):
    if args.file:
        try:
            with open(args.file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError) as exc:
            sys.exit('Error: cannot read %s: %s' % (args.file, exc))
    print('Fetching %s ...' % args.url)
    req = urllib.request.Request(args.url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as exc:
        sys.exit('Error: cannot fetch %s: %s\nUse --file to read a local copy instead.'
                 % (args.url, exc))


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
            'provider_short': provider.split()[0] if provider.split() else 'Abertis',
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
        out.extend(block)
        out.append('/')
    out.extend(('end', 'services'))
    for block in svc_blocks.values():
        out.extend(block)
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
        out.extend(block)
        out.append('/')
    out.extend(('end', 'services'))
    for block in svc_blocks.values():
        out.extend(block)
    out.append('end')

    shutil.copy2(path, path + '.bak')
    with open(path, 'w', encoding='utf-8', errors='surrogateescape') as f:
        f.write('\n'.join(out) + '\n')
    return len(tp_blocks), len(svc_blocks), replaced, removed_tp, removed_svc


def main():
    parser = argparse.ArgumentParser(
        description='Generate the Abertis/Cellnex (Hispasat 30.0W) DX suite: '
                    'lamedb feed services, astra relay config and Enigma2 '
                    'bouquet from the sat_data.json channel database.',
        epilog='examples:\n'
               '  %(prog)s                                      live data, write to ./\n'
               '  %(prog)s -f sat_data.json -o ./dx             use a local JSON copy\n'
               '  %(prog)s --merge-lamedb /etc/enigma2/lamedb   merge into the receiver lamedb',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-u', '--url', default=DEFAULT_JSON_URL,
                        help='URL of sat_data.json (default: %(default)s)')
    parser.add_argument('-f', '--file',
                        help='read the JSON from a local file instead of --url')
    parser.add_argument('-o', '--out-dir', default='.',
                        help='directory for the generated files (default: current)')
    parser.add_argument('--merge-lamedb', metavar='LAMEDB',
                        help='merge the generated entries into this existing lamedb '
                             '(a .bak backup is written first) instead of writing '
                             'a standalone lamedb')
    parser.add_argument('--edit-lamedb', metavar='LAMEDB',
                        help='edit this existing lamedb in place: remove '
                             'transponders matching the generated ones and '
                             'services whose name contains "abertis", then add '
                             'the generated entries (a .bak backup is written '
                             'first). Only v4 /4/ lamedb files are supported')
    parser.add_argument('--lamedb-format', choices=('v4',), default='v4',
                        dest='lamedb_format',
                        help='container format of a standalone lamedb: v4 = '
                             'sectioned layout written by current images '
                             '(default). Only v4 is supported.')
    parser.add_argument('--position', type=int, default=-300,
                        help='orbital position in tenths of a degree, West negative '
                             '(default: %(default)s = 30.0W)')
    parser.add_argument('--relay-host', default='localhost',
                        help='streamrelay host (default: %(default)s)')
    parser.add_argument('--relay-port', type=int, default=8001,
                        help='streamrelay port (default: %(default)s)')
    parser.add_argument('--out-host', default='0.0.0.0',
                        help='astra HTTP output address (default: %(default)s)')
    parser.add_argument('--http-port', type=int, default=9999,
                        help='astra HTTP output port (default: %(default)s)')
    parser.add_argument('--relay-script', metavar='NAME',
                        help='relay script under /etc/astra/scripts/ and URL segment '
                             'for every stream (default: lower-cased provider name, '
                             'e.g. abertis / cellnex)')
    parser.add_argument('--bouquet', default='userbouquet.abertis.tv',
                        dest='bouquet_name',
                        help='bouquet file name (default: %(default)s)')
    args = parser.parse_args()

    data = load_source(args)
    if not isinstance(data, list):
        sys.exit('Error: unexpected JSON layout - expected a list of transponders')

    txps = parse_transponders(data)
    streams = collect_streams(txps)
    if not txps or not streams:
        sys.exit('Error: no transponders/streams found in the source data')
    os.makedirs(args.out_dir, exist_ok=True)

    providers = {}
    for stream in streams:
        providers[stream['txp']['provider']] = providers.get(stream['txp']['provider'], 0) + 1
    dates = sorted({(t.get('date') or '').strip() for t in data if isinstance(t, dict)})
    print('Parsed %d transponders, %d streams, %d channels%s'
          % (len(txps), len(streams), sum(len(s['channels']) for s in streams),
             ' (data date: %s)' % ', '.join(d for d in dates if d) if any(dates) else ''))
    for provider, count in sorted(providers.items()):
        print('  %-12s %2d streams' % (provider, count))
    overrides = [s for s in streams
                 if s['sid'] != s['pid'] + 1000 or s['cpid'] != s['pid'] + 5000]
    if overrides:
        print('  %d stream(s) with custom sid/cpid values from the JSON:'
              % len(overrides))
        for s in overrides:
            print('    PID %-5d SID %-5d CPID %d' % (s['pid'], s['sid'], s['cpid']))

    tp_entries, svc_entries = build_lamedb_entries(txps, streams, args.position)

    if args.merge_lamedb:
        n_tp, n_svc, replaced = merge_lamedb(args.merge_lamedb, tp_entries, svc_entries)
        print('Merged into %s: %d transponders / %d services in total '
              '(%d replaced, backup at %s.bak)'
              % (args.merge_lamedb, n_tp, n_svc, replaced, args.merge_lamedb))
    elif args.edit_lamedb:
        n_tp, n_svc, replaced, removed_tp, removed_svc = edit_lamedb(
            args.edit_lamedb, tp_entries, svc_entries)
        print('Edited %s: %d transponders / %d services in total '
              '(%d replaced, %d transponders removed, %d abertis services '
              'removed, backup at %s.bak)'
              % (args.edit_lamedb, n_tp, n_svc, replaced, removed_tp,
                 removed_svc, args.edit_lamedb))
    else:
        path = os.path.join(args.out_dir, 'lamedb')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(lamedb_text(tp_entries, svc_entries, args.lamedb_format))
        print('Wrote %s (%d transponders, %d feed services, lamedb format %s)'
              % (path, len(tp_entries), len(svc_entries), args.lamedb_format))

    path = os.path.join(args.out_dir, 'astra.conf')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(astra_text(streams, args))
    print('Wrote %s (%d channels)' % (path, len(streams)))

    path = os.path.join(args.out_dir, args.bouquet_name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(bouquet_text(streams, args))
    print('Wrote %s (%d services)'
          % (path, sum(len(s['channels']) for s in streams)))

    print(
        '\nInstall:\n'
        '  lamedb:    copy to /etc/enigma2/lamedb (format %s; or re-run with\n'
        '             --merge-lamedb /etc/enigma2/lamedb) and restart Enigma2\n'
        % args.lamedb_format +
        '  astra:     relay scripts must exist as\n'
        '             /etc/astra/scripts/<provider> <PID> (e.g. abertis, cellnex),\n'
        '             then run astra with this config\n'
        '  bouquet:   copy %s to /etc/enigma2/ and add to bouquets.tv:\n'
        '             #SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "%s" ORDER BY bouquet'
        % (args.bouquet_name, args.bouquet_name))


if __name__ == '__main__':
    main()