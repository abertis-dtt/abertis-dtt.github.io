#!/usr/bin/env python3
"""Analyze astra --analyze logs and optionally update sat_data.json.

Reads the pid<PID>.log files created by Abertis_CreateLogs.py and extracts the
PAT, PMT and SDT sections. The result is grouped by transponder and stream the
same way as the site's sat_data.json:

    transponder -> stream (one PID feed) -> channels

Transponder metadata (txp_no, freq_pol_mode, sr_fec, onid_nid_tid, date) and
stream metadata (title, pid_info, encryption, sid, cpid) are merged from
sat_data.json when a matching transponder/PID exists. Services that only
appear in the SDT (not in the PID's own PAT/PMT) are added as extra
channels, using the vpid/apid that sat_data.json has for the same SID when
available.

The default mode is a small PyQt6 GUI. Use --cli for the command line, and
--update-sat-data to replace the channels list of matching PID streams in the
selected sat_data.json (all other data is left untouched).
"""
import argparse
import contextlib
import difflib
import io
import json
import os
import re
import shutil
import sys

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QPalette
    from PyQt6.QtWidgets import (
        QAbstractItemView, QApplication, QCheckBox, QFileDialog, QGridLayout,
        QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
        QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTreeWidget,
        QTreeWidgetItem, QVBoxLayout, QWidget)
    PYQT_AVAILABLE = True
except ImportError:
    PYQT_AVAILABLE = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_NAME = 'pid_logs_data.json'

APP_NAME = 'Abertis Log Parser'
APP_SUBTITLE = ('Analyze pid_logs \u00b7 build sat_data-style JSON \u00b7 '
                'update sat_data.json')
SETTINGS_FILE = os.path.join(os.path.expanduser('~'),
                             '.abertis_parse_logs_gui.json')

# Same palette as Abertis_DX_Suite_GUI.py so both tools look alike.
THEME = {
    'bg': '#0f1115', 'surface': '#171a21', 'surface2': '#1e222b',
    'field': '#1e222b', 'fg': '#e8eaf0', 'muted': '#98a2b3',
    'accent': '#4c8dff', 'accent_hi': '#6ba0ff', 'accent_lo': '#3a76e0',
    'ok': '#3fd07a', 'warn': '#f0b429', 'err': '#ff5c69',
    'border': '#262b36', 'border_hi': '#343b4a',
    'tree': '#12151b', 'sel': '#233654', 'sel_hi': '#2d4468',
}

# =======================================================================
# PARSER PATTERNS
# =======================================================================
# astra log lines look like:
#   Nov 11 15:02:18: INFO: PAT: pid: 300 PMT pnr: 504
# The timestamp/level prefix is optional so plain message lines also work.
TIMESTAMP_RE = re.compile(
    r'^(?:[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}:\s+[A-Z]+:\s?)?')

PAT_TSID_RE = re.compile(r'^PAT: tsid: (\d+)')
PAT_PMT_RE = re.compile(r'^PAT: pid: \d+ PMT pnr: (\d+)')
PMT_PNR_RE = re.compile(r'^PMT: pnr: (\d+)')
PMT_PCR_RE = re.compile(r'^PMT: pid: (\d+) PCR')
ES_RE = re.compile(r'^(VIDEO|AUDIO|DATA|SUB): pid: (\d+) type: (.*)$')
ES_LANG_RE = re.compile(r'^(VIDEO|AUDIO|DATA|SUB): Language: (\S+)')
SDT_SID_RE = re.compile(r'^SDT: sid: (\d+)')
SDT_SERVICE_RE = re.compile(r'^SDT:\s+Service:\s*(.*)$')
SDT_PROVIDER_RE = re.compile(r'^SDT:\s+Provider:\s*(.*)$')
BITRATE_RE = re.compile(r'^Bitrate: (\d+) Kbit/s')

LOG_NAME_RE = re.compile(r'^pid(\d+)\.log$', re.IGNORECASE)

# astra.conf channel names:
#   Abertis@11653H19680PID301  (current)
#   Abertis@11653H19680 PID 301 (older)
LABEL_RE = re.compile(
    r'^(?P<provider>[^@]+)@\s*(?P<freq>\d+(?:\.\d+)?)\s*(?P<pol>[HV])\s*'
    r'(?P<sr>\d+)\s*PID\s*(?P<pid>\d+)$', re.IGNORECASE)

CHANNEL_RE = re.compile(r'make_channel\s*\(\s*\{(.*?)\}\s*\)', re.DOTALL)
T2MI_DECAP_RE = re.compile(
    r'([A-Za-z_]\w*)\s*=\s*make_t2mi_decap\s*\(\s*\{(.*?)\}\s*\)',
    re.DOTALL)
SREF_INPUT_RE = re.compile(r'^https?://[^/]+/([0-9A-Fa-f:]+)/?$')

LANG_TAGS = {
    'spa': 'Spa',
    'qaa': 'org',
    'eng': 'Eng',
    'cat': 'Cat',
    'gal': 'Gal',
    'eus': 'Eus',
    'und': 'N/A',
    '': 'N/A',
}


# =======================================================================
# HELPERS
# =======================================================================
def clean_text(text):
    """Keep printable characters and collapse whitespace."""
    text = ''.join(ch if ch.isprintable() else ' ' for ch in text)
    return re.sub(r'\s+', ' ', text).strip()


def decode_log(raw):
    """Decode a log that may mix UTF-8 and raw latin-1 service names."""
    text = raw.decode('utf-8', 'surrogateescape')
    return ''.join(
        chr(ord(ch) - 0xDC00) if 0xDC80 <= ord(ch) <= 0xDCFF else ch
        for ch in text)


def _quoted(text):
    """Return every double-quoted string found in ``text``."""
    return re.findall(r'"([^"]*)"', text)


def _int_attr(text, key):
    """Return the integer value of ``key = <number>`` inside ``text``."""
    match = re.search(r'\b%s\s*=\s*(\d+)' % key, text)
    return int(match.group(1)) if match else None


def language_tag(code):
    """Map an ISO 639 code from the log to the site's apid language tag."""
    code = (code or '').strip().lower()
    if code in LANG_TAGS:
        return LANG_TAGS[code]
    return code.capitalize() if code else 'N/A'


def system_from_codec(type_text):
    """Return MPEG-2 / MPEG-4 / HEVC from a PMT stream type."""
    upper = (type_text or '').upper()
    if 'HEVC' in upper:
        return 'HEVC'
    if 'MPEG-4' in upper:
        return 'MPEG-4'
    if 'MPEG-2' in upper:
        return 'MPEG-2'
    return ''


def channel_type(name, system, has_video):
    """Derive the sat_data channel type from the PMT and the SDT name."""
    if not has_video:
        return 'Radio'
    if 'UHD' in (name or '').upper():
        return 'UHD'
    if system == 'MPEG-2':
        return 'SD'
    return 'HD'


def guess_extra_type(name):
    """Best-effort type for an SDT-only service without sat_data metadata."""
    upper = (name or '').upper()
    if re.search(r'\b(RADIO|RNE)\b', upper):
        return 'Radio'
    if 'UHD' in upper:
        return 'UHD'
    if 'HD' in upper:
        return 'HD'
    return ''


def normalize_name(name):
    """Normalize a service name for sat_data matching."""
    name = re.sub(r'[^A-Z0-9]+', ' ', (name or '').upper())
    return name.strip()


def names_match(first, second):
    """Return True when two service names look like the same service."""
    first = normalize_name(first)
    second = normalize_name(second)
    if not first or not second:
        return False
    if first == second or first in second or second in first:
        return True

    common = set(first.split()) & set(second.split())
    if any(len(token) >= 3 for token in common):
        return True

    return difflib.SequenceMatcher(None, first, second).ratio() >= 0.7


def freq_number(text):
    """Return the integer frequency from a sat_data freq_pol_mode string.

    sat_data keeps the exact frequency (11674.85) while astra.conf channel
    names use the whole MHz part (11674H15750), so truncate instead of
    rounding.
    """
    match = re.match(r'\s*(\d+(?:\.\d+)?)', text or '')
    if not match:
        return None
    return int(float(match.group(1)))


def stream_pid(stream):
    """Return the PID from a sat_data stream title (Stream 2025 - ...)."""
    match = re.search(r'Stream\s+(\d+)', stream.get('title', ''))
    return int(match.group(1)) if match else None


def dedupe_streams(streams):
    """Drop duplicated elementary streams from repeated PMT sections."""
    seen = {}
    ordered = []
    for stream in streams:
        key = (stream['kind'], stream['pid'])
        if key in seen:
            if not seen[key]['language'] and stream.get('language'):
                seen[key]['language'] = stream['language']
            continue
        seen[key] = stream
        ordered.append(stream)
    return ordered


# =======================================================================
# LOG PARSING
# =======================================================================
def parse_log(path):
    """Parse one astra --analyze log and return its PAT/PMT/SDT data."""
    try:
        with open(path, 'rb') as log_file:
            raw = log_file.read()
    except OSError as exc:
        print('Warning: cannot read %s: %s' % (path, exc))
        return None

    info = {
        'tsid': '',
        'pat_pnrs': [],
        'pmts': {},
        'sdt': {},
        'bitrates': [],
    }
    current_pmt = None
    current_es = None
    current_sid = None

    for raw_line in decode_log(raw).splitlines():
        line = TIMESTAMP_RE.sub('', raw_line, count=1).strip()
        if not line:
            continue

        match = PAT_TSID_RE.match(line)
        if match:
            info['tsid'] = match.group(1)
            continue

        match = PAT_PMT_RE.match(line)
        if match:
            pnr = match.group(1)
            if pnr not in info['pat_pnrs']:
                info['pat_pnrs'].append(pnr)
            info['pmts'].setdefault(pnr, {'pcr': '', 'streams': []})
            continue

        match = PMT_PNR_RE.match(line)
        if match:
            current_pmt = match.group(1)
            info['pmts'].setdefault(current_pmt, {'pcr': '', 'streams': []})
            current_es = None
            continue

        match = PMT_PCR_RE.match(line)
        if match and current_pmt is not None:
            info['pmts'][current_pmt]['pcr'] = match.group(1)
            continue

        match = ES_RE.match(line)
        if match:
            if current_pmt is not None:
                current_es = {
                    'kind': match.group(1),
                    'pid': int(match.group(2)),
                    'type': match.group(3).strip(),
                    'language': '',
                }
                info['pmts'][current_pmt]['streams'].append(current_es)
            continue

        match = ES_LANG_RE.match(line)
        if match:
            if current_es is not None:
                current_es['language'] = match.group(2)
            continue

        match = SDT_SID_RE.match(line)
        if match:
            current_sid = match.group(1)
            info['sdt'].setdefault(current_sid, {'name': '', 'provider': ''})
            continue

        match = SDT_SERVICE_RE.match(line)
        if match and current_sid is not None:
            info['sdt'][current_sid]['name'] = clean_text(match.group(1))
            continue

        match = SDT_PROVIDER_RE.match(line)
        if match and current_sid is not None:
            info['sdt'][current_sid]['provider'] = clean_text(match.group(1))
            continue

        match = BITRATE_RE.match(line)
        if match:
            info['bitrates'].append(int(match.group(1)))

    return info


# =======================================================================
# ASTRA.CONF PARSING
# =======================================================================
def parse_label(label):
    """Return provider/frequency/polarization/symbol rate from a name."""
    match = LABEL_RE.match(label)
    if not match:
        return {}
    return {
        'provider': match.group('provider').strip(),
        'freq': int(float(match.group('freq'))),
        'pol': match.group('pol').upper(),
        'sr': int(match.group('sr')),
    }


def parse_astra_conf(path):
    """Return {pid: feed metadata} for the feed channels in astra.conf."""
    if not path:
        print('Note: no astra.conf selected - grouping PIDs by TSID')
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as conf_file:
            text = conf_file.read()
    except OSError as exc:
        print('Warning: cannot read astra.conf %s: %s' % (path, exc))
        return {}

    decaps = {}
    for match in T2MI_DECAP_RE.finditer(text):
        var, body = match.group(1), match.group(2)
        input_m = re.search(r'input\s*=\s*"([^"]*)"', body)
        decaps[var] = {
            'input': input_m.group(1).strip() if input_m else '',
            'pid': _int_attr(body, 'pid'),
            'plp': _int_attr(body, 'plp'),
        }

    channels = []
    for match in CHANNEL_RE.finditer(text):
        body = match.group(1)
        name_m = re.search(r'name\s*=\s*"([^"]*)"', body)
        input_m = re.search(r'input\s*=\s*\{(.*?)\}', body, re.DOTALL)
        output_m = re.search(r'output\s*=\s*\{(.*?)\}', body, re.DOTALL)
        command_m = re.search(r'command\s*=\s*"([^"]*)"', body)
        if not input_m or not output_m:
            continue

        inputs = [url.strip() for url in _quoted(input_m.group(1))]
        outputs = [url.strip() for url in _quoted(output_m.group(1))]
        if not inputs or not outputs:
            continue

        pid = None
        if command_m:
            # e.g. command = "/etc/astra/scripts/abertis 8008"
            tokens = command_m.group(1).split()
            if tokens and tokens[-1].isdigit():
                pid = int(tokens[-1])
        if pid is None:
            pid_m = re.search(r'/pid(\d+)(?![0-9])', outputs[0],
                              re.IGNORECASE)
            if pid_m:
                pid = int(pid_m.group(1))

        channels.append({
            'name': name_m.group(1).strip() if name_m else '',
            'inputs': inputs,
            'outputs': outputs,
            'pid': pid,
        })

    # Follow the make_t2mi_decap chain so a T2MI feed also reports its
    # container PID/PLP.
    by_output = {}
    for channel in channels:
        for output in channel['outputs']:
            by_output.setdefault(output, channel)

    for var, decap in decaps.items():
        source = by_output.get(decap['input'])
        if source is None:
            continue
        for channel in channels:
            if ('t2mi://' + var) in channel['inputs']:
                source['t2mi_pid'] = decap['pid']
                source['t2mi_plp'] = decap['plp']
                break

    feeds = {}
    for channel in channels:
        sref = None
        for input_url in channel['inputs']:
            sref_m = SREF_INPUT_RE.match(input_url)
            if sref_m:
                sref = sref_m.group(1)
                break
        if sref is None:
            continue

        pid = channel['pid']
        if pid is None:
            print('Warning: cannot determine PID of %r - skipped'
                  % channel['name'])
            continue

        label = channel['name'] or channel['outputs'][0]
        meta = parse_label(label)
        feeds[pid] = {
            'pid': pid,
            'label': label,
            'provider': meta.get('provider', ''),
            'freq': meta.get('freq'),
            'pol': meta.get('pol', ''),
            'sr': meta.get('sr'),
            't2mi_pid': channel.get('t2mi_pid'),
            't2mi_plp': channel.get('t2mi_plp'),
        }

    return feeds


# =======================================================================
# SAT_DATA MERGING
# =======================================================================
def load_sat_data(path):
    """Load sat_data.json, returning an empty list when it is unavailable."""
    if not path or not os.path.exists(path):
        print('Note: sat_data.json not found at %s - continuing without it'
              % path)
        return []
    try:
        with open(path, 'r', encoding='utf-8') as json_file:
            data = json.load(json_file)
    except (OSError, ValueError) as exc:
        print('Warning: cannot read sat_data.json %s: %s' % (path, exc))
        return []
    return data if isinstance(data, list) else []


def index_sat_data(sat_data):
    """Index sat_data transponders by frequency and services by SID."""
    by_freq = {}
    services = {}
    for transponder in sat_data:
        key = freq_number(transponder.get('freq_pol_mode', ''))
        if key is not None:
            by_freq.setdefault(key, transponder)
        for stream in transponder.get('streams', []):
            for channel in stream.get('channels', []):
                services.setdefault(str(channel.get('sid', '')), []).append(
                    channel)
    return by_freq, services


def index_transponder(transponder):
    """Index one sat_data transponder by stream PID and service SID."""
    streams = {}
    services = {}
    for stream in transponder.get('streams', []):
        pid = stream_pid(stream)
        if pid is not None:
            streams.setdefault(pid, stream)
        for channel in stream.get('channels', []):
            services.setdefault(str(channel.get('sid', '')), []).append(
                channel)
    return streams, services


def find_service(sid, name, transponder_services, global_services):
    """Find the sat_data channel for a SID, preferring the same transponder.

    A SID is unique inside one transport stream, so a match from the same
    transponder is used directly.  The global fallback is only accepted when
    the service names look alike, because SIDs are reused across transponders.
    """
    sid = str(sid)
    candidates = transponder_services.get(sid) or []
    if candidates:
        return candidates[0]

    for candidate in global_services.get(sid) or []:
        if names_match(name, candidate.get('name', '')):
            return candidate
    return None


# =======================================================================
# OUTPUT BUILDING
# =======================================================================
def build_channels(info, transponder_services, global_services,
                   include_extras=True):
    """Turn one log's PAT/PMT/SDT data into sat_data-style channels."""
    channels = []
    pmts = info.get('pmts', {})
    sdt = info.get('sdt', {})
    order = info.get('pat_pnrs') or list(pmts.keys())
    used_sids = set()

    # Services that belong to this PID's own PAT/PMT.
    for pnr in order:
        pnr = str(pnr)
        used_sids.add(pnr)

        pmt = pmts.get(pnr) or {'pcr': '', 'streams': []}
        streams = dedupe_streams(pmt.get('streams', []))
        service = sdt.get(pnr) or {}
        sat_service = find_service(pnr, service.get('name', ''),
                                   transponder_services, global_services)

        name = service.get('name') or ''
        if not name and sat_service:
            name = sat_service.get('name', '')
        if not name:
            name = 'Service %s' % pnr

        video = None
        for es in streams:
            if es['kind'] == 'VIDEO':
                video = es
                break

        system = system_from_codec(video['type']) if video else ''
        vpid = str(video['pid']) if video else ''
        ctype = channel_type(name, system, video is not None)
        if (ctype == 'HD' and system == 'HEVC' and sat_service
                and sat_service.get('type') == 'UHD'):
            ctype = 'UHD'

        apid_parts = []
        for es in streams:
            if es['kind'] == 'AUDIO' or (es['kind'] == 'DATA'
                                         and es.get('language')):
                apid_parts.append('%d %s'
                                  % (es['pid'],
                                     language_tag(es.get('language'))))

        channels.append({
            'type': ctype,
            'name': name,
            'system': system,
            'sid': pnr,
            'vpid': vpid,
            'apid_lang': ' '.join(apid_parts),
        })

    # SDT services that are not part of this PID's PAT/PMT ("extra channels").
    if include_extras:
        extra_sids = [sid for sid in sdt if sid not in used_sids]
        extra_sids.sort(key=lambda sid: int(sid) if sid.isdigit() else 0)
        for sid in extra_sids:
            service = sdt[sid]
            sat_service = find_service(sid, service.get('name', ''),
                                       transponder_services, global_services)

            name = service.get('name') or ''
            if not name and sat_service:
                name = sat_service.get('name', '')
            if not name:
                name = 'Service %s' % sid

            if sat_service:
                ctype = (sat_service.get('type', '')
                         or guess_extra_type(name))
                system = sat_service.get('system', '')
                vpid = str(sat_service.get('vpid', '') or '')
                apid_lang = sat_service.get('apid_lang', '') or ''
            else:
                ctype = guess_extra_type(name)
                system = ''
                vpid = ''
                apid_lang = ''

            channels.append({
                'type': ctype,
                'name': name,
                'system': system,
                'sid': sid,
                'vpid': vpid,
                'apid_lang': apid_lang,
            })

    return channels


def build_stream(feed, info, sat_stream, transponder_services,
                 global_services, include_extras=True):
    """Build one sat_data-style stream record from a log."""
    if sat_stream:
        title = sat_stream.get('title', '') or feed['label']
        pid_info = sat_stream.get('pid_info', '')
        encryption = sat_stream.get('encryption', '')
        sid = sat_stream.get('sid')
        cpid = sat_stream.get('cpid')
    else:
        title = feed['label']
        pid_info = ''
        encryption = ''
        sid = None
        cpid = None

    if feed.get('t2mi_pid') is not None:
        t2mi_pid = str(feed['t2mi_pid'])
        t2mi_plp = (str(feed['t2mi_plp'])
                    if feed.get('t2mi_plp') is not None else 'N/A')
    elif sat_stream:
        t2mi_pid = sat_stream.get('t2mi_pid') or 'N/A'
        t2mi_plp = sat_stream.get('t2mi_plp') or 'N/A'
    else:
        t2mi_pid = 'N/A'
        t2mi_plp = 'N/A'

    bitrates = info.get('bitrates') or []
    return {
        'title': title,
        'pid_info': pid_info,
        'encryption': encryption,
        'sid': str(sid) if sid is not None else 'N/A',
        'cpid': str(cpid) if cpid is not None else 'N/A',
        't2mi_pid': t2mi_pid,
        't2mi_plp': t2mi_plp,
        'pid': feed['pid'],
        'tsid': info.get('tsid', ''),
        'bitrate_kbps': max(bitrates) if bitrates else None,
        'channels': build_channels(info, transponder_services,
                                   global_services, include_extras),
    }


def build_transponders(logs, feeds, sat_by_freq, global_services,
                       include_extras=True):
    """Group parsed logs by transponder and return sat_data-like records."""
    groups = {}
    for pid, info in logs.items():
        feed = feeds.get(pid)
        if feed is None:
            feed = {
                'pid': pid,
                'label': 'PID %d' % pid,
                'provider': '',
                'freq': None,
                'pol': '',
                'sr': None,
                't2mi_pid': None,
                't2mi_plp': None,
            }

        if feed.get('freq') is not None:
            key = ('freq', feed['freq'], feed.get('pol', ''))
        else:
            key = ('tsid', info.get('tsid') or 'unknown')

        group = groups.setdefault(key, {
            'freq': feed.get('freq'),
            'pol': feed.get('pol', ''),
            'sr': feed.get('sr'),
            'provider': feed.get('provider', ''),
            'feeds': [],
        })
        group['feeds'].append((feed, info))

    def sort_key(key):
        group = groups[key]
        if group['freq'] is not None:
            return (0, group['freq'], '')
        return (1, 0, key[1])

    output = []
    for key in sorted(groups, key=sort_key):
        group = groups[key]
        sat_tp = (sat_by_freq.get(int(round(group['freq'])))
                  if group['freq'] is not None else None)

        if sat_tp:
            transponder = {
                'txp_no': sat_tp.get('txp_no', ''),
                'freq_pol_mode': sat_tp.get('freq_pol_mode', ''),
                'provider': sat_tp.get('provider', ''),
                'sr_fec': sat_tp.get('sr_fec', ''),
                'onid_nid_tid': sat_tp.get('onid_nid_tid', ''),
                'date': sat_tp.get('date', ''),
            }
            sat_streams, transponder_services = index_transponder(sat_tp)
        else:
            transponder = {
                'txp_no': '',
                'freq_pol_mode': ('%d %s' % (group['freq'], group['pol'])
                                  if group['freq'] is not None else ''),
                'provider': group['provider'] or '',
                'sr_fec': str(group['sr']) if group['sr'] else '',
                'onid_nid_tid': '',
                'date': '',
            }
            sat_streams, transponder_services = {}, {}

        streams = []
        for feed, info in sorted(group['feeds'],
                                 key=lambda item: item[0]['pid']):
            streams.append(build_stream(
                feed, info, sat_streams.get(feed['pid']),
                transponder_services, global_services, include_extras))

        transponder['streams'] = streams
        output.append(transponder)

    return output


# =======================================================================
# ANALYSIS / SAT_DATA UPDATE
# =======================================================================
def analyze_logs(log_dir, astra_conf, sat_data_path, include_extras=True):
    """Parse all pid<PID>.log files and return the analysis components."""
    sat_data = load_sat_data(sat_data_path)
    sat_by_freq, global_services = index_sat_data(sat_data)
    feeds = parse_astra_conf(astra_conf)

    if not os.path.isdir(log_dir):
        raise ValueError('Log directory not found: %s' % log_dir)

    logs = {}
    for name in sorted(os.listdir(log_dir)):
        match = LOG_NAME_RE.match(name)
        if not match:
            continue
        pid = int(match.group(1))
        info = parse_log(os.path.join(log_dir, name))
        if info is not None:
            logs[pid] = info

    if not logs:
        raise ValueError('No pid<PID>.log files found in %s' % log_dir)

    output = build_transponders(logs, feeds, sat_by_freq, global_services,
                                include_extras)
    return {
        'output': output,
        'logs': logs,
        'feeds': feeds,
        'sat_by_freq': sat_by_freq,
        'global_services': global_services,
        'missing': sorted(set(feeds) - set(logs)),
        'unknown': sorted(set(logs) - set(feeds)),
    }


def build_update_output(analysis, include_extras=False):
    """Rebuild the analyzed output for a sat_data update.

    By default the update uses only the PID's own PAT/PMT channels; the
    SDT-only extras are useful in the generated JSON but would duplicate
    services across streams in sat_data.json.
    """
    return build_transponders(
        analysis['logs'], analysis['feeds'], analysis['sat_by_freq'],
        analysis['global_services'], include_extras)


def save_output(output, path):
    """Write the analyzed dataset as pretty JSON."""
    output_dir = os.path.dirname(os.path.abspath(path))
    if output_dir and not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    with open(path, 'w', encoding='utf-8', newline='\n') as json_file:
        json.dump(output, json_file, indent=4, ensure_ascii=False)
        json_file.write('\n')


def transponder_pol(transponder):
    """Return the polarization letter from a freq_pol_mode string."""
    match = re.match(r'^\s*[\d.]+\s+([HVLR])',
                     transponder.get('freq_pol_mode', '') or '',
                     re.IGNORECASE)
    return match.group(1).upper() if match else ''


def find_sat_transponder(sat_data, analyzed):
    """Find the sat_data transponder that matches an analyzed one."""
    freq = freq_number(analyzed.get('freq_pol_mode', ''))
    pol = transponder_pol(analyzed)
    txp_no = analyzed.get('txp_no')

    candidates = []
    if freq is not None:
        candidates = [transponder for transponder in sat_data
                      if freq_number(transponder.get('freq_pol_mode', ''))
                      == freq]
    if not candidates and txp_no:
        candidates = [transponder for transponder in sat_data
                      if transponder.get('txp_no') == txp_no]
    if pol:
        pol_matches = [transponder for transponder in candidates
                       if transponder_pol(transponder) == pol]
        if pol_matches:
            candidates = pol_matches
    return candidates[0] if candidates else None


def channel_sort_key(channel):
    """Sort TV channels before radio, then by ascending numeric SID."""
    channel_type = str(channel.get('type', '')).strip().lower()
    is_radio = 1 if channel_type == 'radio' else 0
    sid_text = str(channel.get('sid', '') or '')
    try:
        sid = int(sid_text)
    except (TypeError, ValueError):
        sid = 10 ** 9
    return (is_radio, sid, sid_text)


def sort_channels(channels):
    """Return channels with TV first, radio last, then ascending SID."""
    return sorted(channels, key=channel_sort_key)


def update_sat_data(path, analyzed_output, create_backup=True):
    """Replace channels only for analyzed PID streams in sat_data.json.

    Transponders are matched by frequency (and polarization), streams by the
    PID in their "Stream <pid>" title.  Every other key in sat_data.json is
    preserved.  Channels are written TV first, radio last, and sorted by
    ascending SID.  Returns a report with updated/skipped entries.
    """
    try:
        with open(path, 'r', encoding='utf-8') as json_file:
            sat_data = json.load(json_file)
    except (OSError, ValueError) as exc:
        raise ValueError('Cannot read %s: %s' % (path, exc))
    if not isinstance(sat_data, list):
        raise ValueError('%s is not a sat_data.json list' % path)

    report = {'updated': [], 'skipped': [], 'backup': None}

    for analyzed_txp in analyzed_output:
        target_txp = find_sat_transponder(sat_data, analyzed_txp)
        if target_txp is None:
            for stream in analyzed_txp.get('streams', []):
                report['skipped'].append({
                    'pid': stream.get('pid'),
                    'txp_no': analyzed_txp.get('txp_no', ''),
                    'freq': analyzed_txp.get('freq_pol_mode', ''),
                    'reason': 'transponder not found',
                })
            continue

        target_by_pid = {}
        for target_stream in target_txp.get('streams', []):
            pid = stream_pid(target_stream)
            if pid is not None:
                target_by_pid.setdefault(pid, target_stream)

        for analyzed_stream in analyzed_txp.get('streams', []):
            pid = analyzed_stream.get('pid')
            target_stream = target_by_pid.get(pid)
            if target_stream is None:
                report['skipped'].append({
                    'pid': pid,
                    'txp_no': target_txp.get('txp_no', ''),
                    'freq': analyzed_txp.get('freq_pol_mode', ''),
                    'reason': 'stream not found',
                })
                continue
            channels = sort_channels(
                analyzed_stream.get('channels') or [])
            target_stream['channels'] = channels
            report['updated'].append({
                'pid': pid,
                'txp_no': target_txp.get('txp_no', ''),
                'freq': analyzed_txp.get('freq_pol_mode', ''),
                'channels': len(channels),
                'title': target_stream.get('title', ''),
            })

    if report['updated']:
        if create_backup:
            backup_path = path + '.bak'
            try:
                shutil.copy2(path, backup_path)
            except OSError as exc:
                raise ValueError('Cannot create backup %s: %s'
                                 % (backup_path, exc))
            report['backup'] = backup_path
        try:
            with open(path, 'w', encoding='utf-8',
                      newline='\n') as json_file:
                json.dump(sat_data, json_file, indent=4,
                          ensure_ascii=False)
                json_file.write('\n')
        except OSError as exc:
            raise ValueError('Cannot write %s: %s' % (path, exc))

    return report


# =======================================================================
# COMMAND LINE
# =======================================================================
def default_log_dir():
    candidate = os.path.join(SCRIPT_DIR, 'pid_logs')
    if os.path.isdir(candidate):
        return candidate
    return os.path.join(os.getcwd(), 'pid_logs')


def default_astra_conf():
    candidate = os.path.join(SCRIPT_DIR, 'astra.conf')
    if os.path.exists(candidate):
        return candidate
    return '/etc/astra/astra.conf'


def default_sat_data():
    candidate = os.path.normpath(os.path.join(SCRIPT_DIR, os.pardir,
                                              'sat_data.json'))
    if os.path.exists(candidate):
        return candidate
    return os.path.join(os.getcwd(), 'sat_data.json')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Build a sat_data.json-like dataset from the astra '
                    '--analyze logs in pid_logs.')
    parser.add_argument(
        '--log-dir',
        default=os.environ.get('PID_LOG_DIR', default_log_dir()),
        help='directory containing pid<PID>.log files')
    parser.add_argument(
        '--astra-conf',
        default=os.environ.get('ASTRA_CONF', default_astra_conf()),
        help='astra.conf used to group PIDs by transponder')
    parser.add_argument(
        '--sat-data',
        default=os.environ.get('SAT_DATA', default_sat_data()),
        help='sat_data.json used to merge transponder/service metadata '
             'and, with --update-sat-data, as the file to update')
    parser.add_argument(
        '--output',
        default=os.environ.get('PID_DATA_OUT', ''),
        help='output JSON path (default: <log-dir>/%s)' % OUTPUT_NAME)
    parser.add_argument(
        '--no-extras', action='store_true',
        help='do not add SDT-only extra channels')
    parser.add_argument(
        '--update-sat-data', action='store_true',
        help='replace the channels list of matching PID streams in '
             '--sat-data (other data is preserved)')
    parser.add_argument(
        '--update-extras', action='store_true',
        help='with --update-sat-data, also write SDT-only extra channels')
    parser.add_argument(
        '--no-backup', action='store_true',
        help='do not create <sat-data>.bak before updating')
    args = parser.parse_args(argv)
    if not args.output:
        args.output = os.path.join(args.log_dir, OUTPUT_NAME)
    return args


def cli_main(argv=None):
    """Command-line mode (also used when no desktop display is available)."""
    args = parse_args(argv)
    try:
        analysis = analyze_logs(args.log_dir, args.astra_conf, args.sat_data,
                                include_extras=not args.no_extras)
    except ValueError as exc:
        print('Error: %s' % exc)
        return 1

    missing = analysis['missing']
    if missing:
        print('No log found for PID(s): %s'
              % ', '.join(str(pid) for pid in missing))
    unknown = analysis['unknown']
    if unknown:
        print('PID(s) not declared in astra.conf: %s'
              % ', '.join(str(pid) for pid in unknown))

    output = analysis['output']
    try:
        save_output(output, args.output)
    except OSError as exc:
        print('Error: cannot write %s: %s' % (args.output, exc))
        return 1

    total_streams = sum(len(tp['streams']) for tp in output)
    total_channels = sum(len(stream['channels'])
                         for tp in output for stream in tp['streams'])
    print('Parsed %d log(s)' % len(analysis['logs']))
    print('Wrote %d transponder(s), %d stream(s), %d channel(s) to %s'
          % (len(output), total_streams, total_channels, args.output))

    if args.update_sat_data:
        update_output = build_update_output(
            analysis, include_extras=args.update_extras)
        try:
            report = update_sat_data(args.sat_data, update_output,
                                     create_backup=not args.no_backup)
        except ValueError as exc:
            print('Error: %s' % exc)
            return 1
        print('Updated %d stream(s) in %s'
              % (len(report['updated']), args.sat_data))
        if report['backup']:
            print('Backup: %s' % report['backup'])
        for item in report['skipped']:
            print('Skipped PID %s (%s): %s'
                  % (item['pid'], item['freq'], item['reason']))
    return 0


def has_display():
    """Return True when a desktop display is available for the GUI."""
    if sys.platform.startswith(('win', 'darwin')):
        return True
    return bool(os.environ.get('DISPLAY')
                or os.environ.get('WAYLAND_DISPLAY'))


def build_stylesheet():
    """Return the dark QSS used by the parser window."""
    C = THEME
    return '\n'.join([
        'QMainWindow, QDialog { background: %s; }' % C['bg'],
        'QWidget { color: %s; font-size: 10.5pt; }' % C['fg'],
        'QLabel#Title { color: %s; font-size: 18pt; font-weight: 700; }'
        % C['accent'],
        'QLabel#Muted { color: %s; }' % C['muted'],
        'QGroupBox { background: %s; border: 1px solid %s;'
        ' border-radius: 8px; margin-top: 10px;'
        ' padding: 12px 10px 10px 10px; font-weight: 600; }'
        % (C['surface'], C['border']),
        'QGroupBox::title { subcontrol-origin: margin; left: 12px;'
        ' padding: 0 6px; color: %s; }' % C['accent'],
        'QLineEdit { background: %s; color: %s; border: 1px solid %s;'
        ' border-radius: 6px; padding: 6px 9px;'
        ' selection-background-color: %s; }'
        % (C['field'], C['fg'], C['border'], C['sel_hi']),
        'QLineEdit:focus { border-color: %s; }' % C['accent'],
        'QPushButton { background: %s; color: %s; border: 1px solid %s;'
        ' border-radius: 6px; padding: 7px 14px; }'
        % (C['surface2'], C['fg'], C['border']),
        'QPushButton:hover { border-color: %s; }' % C['border_hi'],
        'QPushButton:disabled { color: %s; background: %s; }'
        % (C['muted'], C['surface']),
        'QPushButton#Primary { background: %s; border-color: %s;'
        ' color: #ffffff; font-weight: 600; }'
        % (C['accent'], C['accent']),
        'QPushButton#Primary:hover { background: %s; }' % C['accent_hi'],
        'QCheckBox { spacing: 8px; }',
        'QTreeWidget { background: %s; alternate-background-color: %s;'
        ' border: 1px solid %s; border-radius: 6px; }'
        % (C['tree'], C['surface'], C['border']),
        'QTreeWidget::item { padding: 3px 4px; }',
        'QTreeWidget::item:selected { background: %s; }' % C['sel'],
        'QHeaderView::section { background: %s; color: %s; border: none;'
        ' border-bottom: 1px solid %s; padding: 6px; font-weight: 600; }'
        % (C['surface2'], C['fg'], C['border']),
        'QPlainTextEdit { background: %s; color: %s; border: 1px solid %s;'
        ' border-radius: 6px; padding: 6px; }'
        % (C['tree'], C['fg'], C['border']),
        'QStatusBar { color: %s; }' % C['muted'],
        'QToolTip { background: %s; color: %s; border: 1px solid %s; }'
        % (C['surface2'], C['fg'], C['border']),
    ])


if PYQT_AVAILABLE:
    class ParseLogsWindow(QMainWindow):
        """Small front-end around the log parser and sat_data updater."""

        def __init__(self):
            super().__init__()
            self.setWindowTitle('%s \u2014 %s' % (APP_NAME, APP_SUBTITLE))
            self.resize(1280, 820)
            self.setMinimumSize(980, 620)
            self.analysis = None
            self._busy = False
            self._build_ui()
            self._load_settings()

        def _build_ui(self):
            central = QWidget()
            self.setCentralWidget(central)
            root = QVBoxLayout(central)
            root.setContentsMargins(16, 14, 16, 12)
            root.setSpacing(10)

            title = QLabel(APP_NAME)
            title.setObjectName('Title')
            root.addWidget(title)
            subtitle = QLabel(APP_SUBTITLE)
            subtitle.setObjectName('Muted')
            root.addWidget(subtitle)

            paths = QGroupBox('Inputs and output')
            form = QGridLayout(paths)
            form.setContentsMargins(12, 18, 12, 12)
            form.setHorizontalSpacing(8)
            form.setVerticalSpacing(8)
            form.setColumnStretch(1, 1)

            self.log_dir_edit = QLineEdit()
            self.astra_conf_edit = QLineEdit()
            self.sat_data_edit = QLineEdit()
            self.output_edit = QLineEdit()
            rows = (
                ('Logs folder', self.log_dir_edit, 'dir',
                 'Select logs folder'),
                ('astra.conf', self.astra_conf_edit, 'conf',
                 'Select astra.conf'),
                ('sat_data.json', self.sat_data_edit, 'json',
                 'Select sat_data.json'),
                ('Output JSON', self.output_edit, 'save',
                 'Select output JSON'),
            )
            for row, (label, edit, kind, dialog_title) in enumerate(rows):
                form.addWidget(QLabel(label), row, 0)
                form.addWidget(edit, row, 1)
                button = QPushButton('Browse')
                button.setFixedWidth(96)
                button.clicked.connect(
                    lambda checked=False, e=edit, k=kind, t=dialog_title:
                    self._browse(e, k, t))
                form.addWidget(button, row, 2)
            root.addWidget(paths)

            options = QGroupBox('Options')
            opt = QHBoxLayout(options)
            opt.setContentsMargins(12, 18, 12, 12)
            self.extras_check = QCheckBox(
                'Include SDT-only extra channels')
            self.extras_check.setChecked(True)
            self.extras_check.setToolTip(
                'Add services that are listed in the SDT but are not part '
                'of the PID own PAT/PMT. Their vpid/apid are taken from '
                'sat_data.json when the same SID is found.')
            self.extras_check.stateChanged.connect(self._invalidate_analysis)
            self.backup_check = QCheckBox(
                'Create .bak backup before updating sat_data.json')
            self.backup_check.setChecked(True)
            self.update_extras_check = QCheckBox(
                'Include extras in sat_data update')
            self.update_extras_check.setChecked(False)
            self.update_extras_check.setToolTip(
                'When updating sat_data.json, also write the SDT-only '
                'extra channels. Off by default so each PID stream keeps '
                'only the services from its own PAT/PMT.')
            opt.addWidget(self.extras_check)
            opt.addWidget(self.update_extras_check)
            opt.addWidget(self.backup_check)
            opt.addStretch(1)
            root.addWidget(options)

            buttons = QHBoxLayout()
            self.analyze_btn = QPushButton('Analyze logs')
            self.analyze_btn.setObjectName('Primary')
            self.analyze_btn.clicked.connect(self._analyze)
            self.save_btn = QPushButton('Save JSON')
            self.save_btn.setEnabled(False)
            self.save_btn.clicked.connect(self._save_output)
            self.update_btn = QPushButton('Update sat_data.json')
            self.update_btn.setEnabled(False)
            self.update_btn.clicked.connect(self._update_sat_data)
            buttons.addWidget(self.analyze_btn)
            buttons.addWidget(self.save_btn)
            buttons.addWidget(self.update_btn)
            buttons.addStretch(1)
            root.addLayout(buttons)

            splitter = QSplitter(Qt.Orientation.Vertical)
            self.tree = QTreeWidget()
            self.tree.setColumnCount(6)
            self.tree.setHeaderLabels([
                'Transponder / stream / channel', 'Type', 'System', 'SID',
                'VPID', 'APID'])
            self.tree.setAlternatingRowColors(True)
            self.tree.setUniformRowHeights(True)
            self.tree.setSelectionMode(
                QAbstractItemView.SelectionMode.SingleSelection)
            header = self.tree.header()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            for column in range(1, 6):
                header.setSectionResizeMode(
                    column, QHeaderView.ResizeMode.ResizeToContents)
            splitter.addWidget(self.tree)

            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setPlaceholderText('Analysis messages appear here.')
            self.log.setMaximumBlockCount(5000)
            splitter.addWidget(self.log)
            splitter.setSizes([520, 180])
            root.addWidget(splitter, 1)

            self.statusBar().showMessage('Ready')

        def _browse(self, edit, kind, dialog_title):
            current = edit.text().strip() or os.getcwd()
            if kind == 'dir':
                path = QFileDialog.getExistingDirectory(
                    self, dialog_title, current)
            elif kind == 'save':
                path, _ = QFileDialog.getSaveFileName(
                    self, dialog_title, current,
                    'JSON files (*.json);;All files (*)')
            elif kind == 'conf':
                path, _ = QFileDialog.getOpenFileName(
                    self, dialog_title, current,
                    'Config files (*.conf);;All files (*)')
            else:
                path, _ = QFileDialog.getOpenFileName(
                    self, dialog_title, current,
                    'JSON files (*.json);;All files (*)')
            if not path:
                return
            edit.setText(path)
            if edit is self.log_dir_edit:
                output = self.output_edit.text().strip()
                if not output or os.path.basename(output) == OUTPUT_NAME:
                    self.output_edit.setText(
                        os.path.join(path, OUTPUT_NAME))

        def _set_busy(self, busy):
            self._busy = busy
            if busy:
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            else:
                QApplication.restoreOverrideCursor()
            self._refresh_buttons()
            QApplication.processEvents()

        def _refresh_buttons(self):
            idle = not self._busy
            self.analyze_btn.setEnabled(idle)
            ready = idle and self.analysis is not None
            self.save_btn.setEnabled(ready)
            self.update_btn.setEnabled(ready)

        def _invalidate_analysis(self, *args):
            if self.analysis is None:
                return
            self.analysis = None
            self.tree.clear()
            self._refresh_buttons()
            self._append_log('Options changed - analyze the logs again.')

        def _append_log(self, message):
            self.log.appendPlainText(message)
            scrollbar = self.log.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

        def _analyze(self):
            log_dir = self.log_dir_edit.text().strip()
            astra_conf = self.astra_conf_edit.text().strip()
            sat_data = self.sat_data_edit.text().strip()

            if not log_dir or not os.path.isdir(log_dir):
                QMessageBox.warning(
                    self, APP_NAME, 'Select a valid logs folder.')
                return
            if astra_conf and not os.path.exists(astra_conf):
                answer = QMessageBox.question(
                    self, APP_NAME,
                    'astra.conf not found:\n%s\n\nContinue without it? '
                    'PIDs will be grouped by TSID instead of transponder.'
                    % astra_conf)
                if answer != QMessageBox.StandardButton.Yes:
                    return
            if sat_data and not os.path.exists(sat_data):
                answer = QMessageBox.question(
                    self, APP_NAME,
                    'sat_data.json not found:\n%s\n\nContinue without it?'
                    % sat_data)
                if answer != QMessageBox.StandardButton.Yes:
                    return

            self._set_busy(True)
            captured = io.StringIO()
            try:
                with contextlib.redirect_stdout(captured):
                    analysis = analyze_logs(
                        log_dir, astra_conf, sat_data,
                        include_extras=self.extras_check.isChecked())
            except Exception as exc:
                QMessageBox.critical(
                    self, APP_NAME, 'Analysis failed:\n%s' % exc)
                return
            finally:
                self._set_busy(False)

            messages = captured.getvalue().strip()
            if messages:
                self._append_log(messages)
            self.analysis = analysis
            self._populate_tree(analysis)
            self._refresh_buttons()

            total_streams = sum(len(txp['streams'])
                                for txp in analysis['output'])
            total_channels = sum(
                len(stream['channels'])
                for txp in analysis['output'] for stream in txp['streams'])
            self._append_log(
                'Analyzed %d log(s): %d transponder(s), %d stream(s), '
                '%d channel(s).'
                % (len(analysis['logs']), len(analysis['output']),
                   total_streams, total_channels))
            if analysis['missing']:
                self._append_log(
                    'No log found for PID(s): %s'
                    % ', '.join(str(pid) for pid in analysis['missing']))
            if analysis['unknown']:
                self._append_log(
                    'PID(s) not declared in astra.conf: %s'
                    % ', '.join(str(pid) for pid in analysis['unknown']))
            self.statusBar().showMessage('Analysis ready')

        def _populate_tree(self, analysis):
            self.tree.clear()
            for txp in analysis['output']:
                channels = sum(len(stream['channels'])
                               for stream in txp['streams'])
                label = '%s  %s  [%d streams / %d channels]' % (
                    txp.get('txp_no') or '?',
                    txp.get('freq_pol_mode') or '',
                    len(txp['streams']), channels)
                txp_item = QTreeWidgetItem([label, '', '', '', '', ''])
                txp_item.setToolTip(
                    0, 'Provider: %s\nONID/NID/TID: %s\nDate: %s'
                    % (txp.get('provider', ''),
                       txp.get('onid_nid_tid', ''),
                       txp.get('date', '')))
                font = txp_item.font(0)
                font.setBold(True)
                txp_item.setFont(0, font)
                self.tree.addTopLevelItem(txp_item)

                for stream in txp['streams']:
                    stream_item = QTreeWidgetItem([
                        'PID %d  -  %s  [%d channels]'
                        % (stream['pid'], stream.get('title', ''),
                           len(stream['channels'])),
                        '', '', '', '', ''])
                    stream_item.setToolTip(
                        0, 'TSID: %s\nT2MI: %s/%s\nBitrate: %s kbit/s'
                        % (stream.get('tsid', ''),
                           stream.get('t2mi_pid', 'N/A'),
                           stream.get('t2mi_plp', 'N/A'),
                           stream.get('bitrate_kbps') or 'N/A'))
                    txp_item.addChild(stream_item)

                    for channel in stream['channels']:
                        channel_item = QTreeWidgetItem([
                            channel.get('name', ''),
                            channel.get('type', ''),
                            channel.get('system', ''),
                            str(channel.get('sid', '')),
                            str(channel.get('vpid', '')),
                            channel.get('apid_lang', '')])
                        stream_item.addChild(channel_item)
                txp_item.setExpanded(True)

        def _save_output(self):
            if self.analysis is None:
                QMessageBox.warning(
                    self, APP_NAME, 'Analyze the logs first.')
                return
            path = self.output_edit.text().strip()
            if not path:
                QMessageBox.warning(
                    self, APP_NAME, 'Select an output JSON path.')
                return
            if os.path.exists(path):
                answer = QMessageBox.question(
                    self, APP_NAME, 'Overwrite this file?\n\n%s' % path)
                if answer != QMessageBox.StandardButton.Yes:
                    return
            try:
                save_output(self.analysis['output'], path)
            except OSError as exc:
                QMessageBox.critical(
                    self, APP_NAME, 'Cannot write %s:\n%s' % (path, exc))
                return
            self._append_log('Saved %s' % path)
            self.statusBar().showMessage('Saved %s' % path)

        def _update_sat_data(self):
            if self.analysis is None:
                QMessageBox.warning(
                    self, APP_NAME, 'Analyze the logs first.')
                return
            path = self.sat_data_edit.text().strip()
            if not path or not os.path.exists(path):
                QMessageBox.warning(
                    self, APP_NAME,
                    'Select an existing sat_data.json to update.')
                return

            update_output = build_update_output(
                self.analysis,
                include_extras=self.update_extras_check.isChecked())
            streams = sum(len(txp['streams']) for txp in update_output)
            channels = sum(len(stream['channels'])
                           for txp in update_output
                           for stream in txp['streams'])
            message = (
                'Update channels for the analyzed PID streams in:\n\n'
                '%s\n\n'
                '%d stream(s), %d channel(s) will be matched by '
                'transponder frequency and PID.\n'
                'Only the channels list is replaced; all other '
                'sat_data.json data is preserved.\n'
                'Channels are ordered TV first, then radio, by ascending '
                'SID.'
                % (path, streams, channels))
            if self.update_extras_check.isChecked():
                message += ('\n\nSDT-only extra channels will also be '
                            'written.')
            else:
                message += ('\n\nOnly the PID PAT/PMT channels are '
                            'written; SDT-only extras are skipped.')
            if self.backup_check.isChecked():
                message += '\n\nA backup will be written to:\n%s.bak' % path
            answer = QMessageBox.question(self, APP_NAME, message)
            if answer != QMessageBox.StandardButton.Yes:
                return

            try:
                report = update_sat_data(
                    path, update_output,
                    create_backup=self.backup_check.isChecked())
            except ValueError as exc:
                QMessageBox.critical(self, APP_NAME, str(exc))
                return

            self._append_log(
                'Updated %d stream(s) in %s'
                % (len(report['updated']), path))
            if report['backup']:
                self._append_log('Backup: %s' % report['backup'])
            for item in report['skipped']:
                self._append_log(
                    'Skipped PID %s (%s): %s'
                    % (item['pid'], item['freq'], item['reason']))

            if report['updated']:
                QMessageBox.information(
                    self, APP_NAME,
                    'Updated %d stream(s).\nSkipped %d stream(s).\n\n%s'
                    % (len(report['updated']), len(report['skipped']),
                       path))
                self.statusBar().showMessage(
                    'Updated %d stream(s)' % len(report['updated']))
            else:
                QMessageBox.warning(
                    self, APP_NAME,
                    'No matching PID streams were found in %s.' % path)

        def _load_settings(self):
            saved = {}
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
            except (OSError, ValueError):
                saved = {}

            self.log_dir_edit.setText(
                saved.get('log_dir') or default_log_dir())
            self.astra_conf_edit.setText(
                saved.get('astra_conf') or default_astra_conf())
            self.sat_data_edit.setText(
                saved.get('sat_data') or default_sat_data())
            output = saved.get('output') or ''
            if not output:
                log_dir = self.log_dir_edit.text().strip()
                if log_dir:
                    output = os.path.join(log_dir, OUTPUT_NAME)
            self.output_edit.setText(output)
            self.extras_check.setChecked(bool(saved.get('extras', True)))
            self.backup_check.setChecked(bool(saved.get('backup', True)))
            self.update_extras_check.setChecked(
                bool(saved.get('update_extras', False)))

            size = saved.get('window')
            if isinstance(size, list) and len(size) == 2:
                try:
                    self.resize(int(size[0]), int(size[1]))
                except (TypeError, ValueError):
                    pass

        def _save_settings(self):
            data = {
                'log_dir': self.log_dir_edit.text().strip(),
                'astra_conf': self.astra_conf_edit.text().strip(),
                'sat_data': self.sat_data_edit.text().strip(),
                'output': self.output_edit.text().strip(),
                'extras': self.extras_check.isChecked(),
                'update_extras': self.update_extras_check.isChecked(),
                'backup': self.backup_check.isChecked(),
                'window': [self.width(), self.height()],
            }
            try:
                with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                    f.write('\n')
            except OSError:
                pass

        def closeEvent(self, event):
            self._save_settings()
            super().closeEvent(event)


def gui_main(argv=None):
    """Start the PyQt6 GUI."""
    if not PYQT_AVAILABLE:
        print('PyQt6 is not installed. Use --cli.')
        return 1
    app = QApplication([sys.argv[0]] + list(argv or []))
    app.setApplicationName(APP_NAME)
    app.setStyle('Fusion')

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(THEME['bg']))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(THEME['fg']))
    palette.setColor(QPalette.ColorRole.Base, QColor(THEME['field']))
    palette.setColor(QPalette.ColorRole.AlternateBase,
                     QColor(THEME['surface']))
    palette.setColor(QPalette.ColorRole.Text, QColor(THEME['fg']))
    palette.setColor(QPalette.ColorRole.Button, QColor(THEME['surface2']))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(THEME['fg']))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(THEME['accent']))
    palette.setColor(QPalette.ColorRole.HighlightedText,
                     QColor('#ffffff'))
    palette.setColor(QPalette.ColorRole.ToolTipBase,
                     QColor(THEME['surface2']))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(THEME['fg']))
    palette.setColor(QPalette.ColorRole.PlaceholderText,
                     QColor(THEME['muted']))
    app.setPalette(palette)
    app.setStyleSheet(build_stylesheet())

    window = ParseLogsWindow()
    window.show()
    return app.exec()


def main():
    argv = sys.argv[1:]
    force_cli = '--cli' in argv or '--no-gui' in argv
    force_gui = '--gui' in argv
    if force_cli:
        argv = [arg for arg in argv
                if arg not in ('--cli', '--no-gui', '--gui')]
        return cli_main(argv)
    if '-h' in argv or '--help' in argv:
        return cli_main(argv)
    if force_gui:
        argv = [arg for arg in argv if arg != '--gui']
    if not PYQT_AVAILABLE or not (force_gui or has_display()):
        print('PyQt6 GUI not available - running in CLI mode.')
        return cli_main(argv)
    return gui_main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
