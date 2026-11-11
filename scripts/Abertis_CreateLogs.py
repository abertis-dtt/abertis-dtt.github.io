#!/usr/bin/env python3
"""Create astra analysis logs for the Abertis/Cellnex (Hispasat 30.0W) feeds.

Runs on the Enigma2 receiver and reads the astra configuration that the
receiver actually uses (default: /etc/astra/astra.conf). For every PID feed
declared in that file the script:

  1. tunes Enigma2 to the feed service reference (sref) that the config uses
     as the channel input (http://localhost:8001/<sref>:),
  2. waits for the tuner and the astra chain to settle,
  3. runs ``astra --analyze`` on the output channel of that PID, using the
     output URL published by the same config
     (http://0.0.0.0:9999/<provider>/pid<PID>).

When a PID is wrapped in a T2MI container the config adds a
``make_t2mi_decap`` block and a second channel whose input is ``t2mi://...``.
The script follows that chain and analyzes the decapsulated output channel,
because the raw PID only carries the encapsulated multiplex and yields no
PAT/PMT of its own.
"""
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# =======================================================================
# CONFIGURATION
# =======================================================================
# astra configuration used on the Enigma2 side (override by passing a path as
# the first argument or via the ASTRA_CONF environment variable).
ASTRA_CONF_PATH = (sys.argv[1] if len(sys.argv) > 1
                   else os.environ.get('ASTRA_CONF', '/etc/astra/astra.conf'))

# astra binary used for the analyzer (override with the ASTRA_BIN variable).
ASTRA_BIN = os.environ.get('ASTRA_BIN', 'astra')

# Enigma2 WebIf used to zap to the feed service.
WEBIF_URL = os.environ.get('ENIGMA2_WEBIF', 'http://127.0.0.1')

# How long (in seconds) to run the analysis for each PID to gather SDT/PMT data
ANALYZE_DURATION = 30

# Delay after a successful zap before launching astra --analyze. Lets the
# feed service settle so the analysis matches a manual run.
POST_ZAP_SETTLE = 4

# Time given to the tuner/streamrelay after a zap, before the settle delay.
ZAP_SETTLE = 6

# Zap to the feed service before analyzing its output channel. Set to False to
# analyze the streams exactly as they come in now (matches a manual
# `astra --analyze URL` run made without zapping first).
ZAP_BEFORE_ANALYZE = True

# For T2MI feeds the decapsulated output is analyzed. Set this to True to also
# analyze the raw PID output (saved as pid<PID>_raw.log).
ANALYZE_RAW_T2MI_OUTPUT = False

# Output directory for the logs
OUTPUT_DIR = 'pid_logs'

# =======================================================================

CHANNEL_RE = re.compile(r'make_channel\s*\(\s*\{(.*?)\}\s*\)', re.DOTALL)
T2MI_DECAP_RE = re.compile(
    r'([A-Za-z_]\w*)\s*=\s*make_t2mi_decap\s*\(\s*\{(.*?)\}\s*\)',
    re.DOTALL)
# Enigma2 sref inside an astra input URL:
#   http://localhost:8001/1:0:1:515:EA63:1:CE40000:0:0:0:
SREF_INPUT_RE = re.compile(r'^https?://[^/]+/([0-9A-Fa-f:]+)/?$')
OUTPUT_PID_RE = re.compile(r'/pid(\d+)(?![0-9])', re.IGNORECASE)
NAME_PID_RE = re.compile(r'PID\s*(\d+)', re.IGNORECASE)


def _quoted(text):
    """Return every double-quoted string found in ``text``."""
    return re.findall(r'"([^"]*)"', text)


def parse_astra_conf(path):
    """Return one record per PID feed found in ``make_channel(...)`` blocks.

    Each record carries the channel label, the feed service reference (from
    that channel's input URL), the PID (from the pipe command, the output URL
    or the channel name) and the output URL that astra --analyze must use.

    T2MI feeds are resolved through their ``make_t2mi_decap`` block: the
    decapsulated ``t2mi://`` channel becomes the analyzed output for the raw
    PID that feeds the decapsulator.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError as exc:
        print('Error: cannot read astra.conf %s: %s' % (path, exc))
        return []

    # T2MI decapsulators: variable name -> block details.
    decaps = {}
    for match in T2MI_DECAP_RE.finditer(text):
        var, body = match.group(1), match.group(2)
        name_m = re.search(r'name\s*=\s*"([^"]*)"', body)
        input_m = re.search(r'input\s*=\s*"([^"]*)"', body)
        decaps[var] = {
            'name': name_m.group(1).strip() if name_m else var,
            'input': input_m.group(1).strip() if input_m else '',
        }

    # Plain channels: raw feed channels (http input with an sref) and the
    # T2MI channels that consume a decapsulator (t2mi:// input).
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

        name = name_m.group(1).strip() if name_m else ''

        pid = None
        if command_m:
            # e.g. command = "/etc/astra/scripts/abertis 8008"
            tokens = command_m.group(1).split()
            if tokens and tokens[-1].isdigit():
                pid = int(tokens[-1])
        if pid is None:
            pid_m = OUTPUT_PID_RE.search(outputs[0])
            if pid_m:
                pid = int(pid_m.group(1))
        if pid is None:
            pid_m = NAME_PID_RE.search(name)
            if pid_m:
                pid = int(pid_m.group(1))

        channels.append({
            'name': name or outputs[0],
            'inputs': inputs,
            'outputs': outputs,
            'pid': pid,
        })

    # Map every astra output URL back to the channel that publishes it.
    by_output = {}
    for channel in channels:
        for output in channel['outputs']:
            by_output.setdefault(output, channel)

    # Resolve T2MI chains: decapsulator input -> raw channel; decapsulator
    # variable -> t2mi:// channel. The raw PID then analyzes the T2MI output.
    t2mi_by_source = {}
    for var, decap in decaps.items():
        source = by_output.get(decap['input'])
        if source is None:
            print('Warning: T2MI decap %s (%s) reads from unknown output %r'
                  ' - skipped' % (var, decap['name'], decap['input']))
            continue
        t2mi_channel = None
        for channel in channels:
            if ('t2mi://' + var) in channel['inputs']:
                t2mi_channel = channel
                break
        if t2mi_channel is None:
            print('Warning: T2MI decap %s (%s) has no t2mi:// channel - skipped'
                  % (var, decap['name']))
            continue
        t2mi_by_source[id(source)] = {
            'url': t2mi_channel['outputs'][0],
            'decap': decap,
        }

    feeds = []
    for channel in channels:
        # The feed service reference is the Enigma2 sref embedded in the
        # channel input URL. t2mi:// channels have no sref of their own.
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

        t2mi = t2mi_by_source.get(id(channel))
        raw_url = channel['outputs'][0]
        feeds.append({
            'label': channel['name'],
            'sref': sref,
            'pid': pid,
            'url': t2mi['url'] if t2mi else raw_url,
            'raw_url': raw_url,
            't2mi': t2mi,
        })

    return feeds


def zap_to_service(sref):
    """Zap to the specified Enigma2 service reference using the local WebIf."""
    encoded_sref = urllib.parse.quote(sref, safe='')
    url = '%s/web/zap?sRef=%s' % (WEBIF_URL.rstrip('/'), encoded_sref)
    try:
        print('[*] Zapping to feed service: %s' % sref)
        print('    -> zap command: curl -s %r | head' % url)
        with urllib.request.urlopen(url, timeout=5) as response:
            result = response.read().decode('utf-8', 'replace')
        if 'true' in result.lower():
            print('    -> Zap successful. Waiting %d seconds for tuner lock'
                  ' & stream start...' % ZAP_SETTLE)
            time.sleep(ZAP_SETTLE)
            return True
        print('    -> Zap failed. Check the sRef format (WebIf answered %r).'
              % result.strip())
        return False
    except Exception as exc:
        print('    -> Error during zap (Is WebIf running?): %s' % exc)
        return False


def analyze_pid(pid, url, suffix=''):
    """Run astra --analyze for the specified PID/output URL, save the log."""
    log_filepath = os.path.join(OUTPUT_DIR, 'pid%d%s.log' % (pid, suffix))
    cmd = [ASTRA_BIN, '--analyze', url]

    print('[*] Analyzing PID %d for %d seconds...' % (pid, ANALYZE_DURATION))
    print('    -> command: %s' % ' '.join(cmd))

    try:
        log_file = open(log_filepath, 'w', encoding='utf-8')
    except OSError as exc:
        print('    -> Error creating %s: %s' % (log_filepath, exc))
        return False

    try:
        try:
            # Start the astra analysis process in the background
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            print('    -> Error starting %s: %s' % (ASTRA_BIN, exc))
            return False

        timed_out = False
        try:
            # astra --analyze runs forever, so we intentionally wait for a
            # timeout and then terminate the process to finish the log.
            process.wait(timeout=ANALYZE_DURATION)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        if process.returncode not in (0, None) and not timed_out:
            print('    -> astra exited early (code %d); log saved to %s'
                  % (process.returncode, log_filepath))
            return False

        print('    -> Finished PID %d. Log saved to %s' % (pid, log_filepath))
        return True
    finally:
        log_file.close()


def main():
    feeds = parse_astra_conf(ASTRA_CONF_PATH)
    if not feeds:
        print('No PID feeds found in %s' % ASTRA_CONF_PATH)
        sys.exit(1)

    # 1. Create the pid_logs directory if it doesn't exist
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        print('Created directory: %s/' % OUTPUT_DIR)

    print('Parsed %d PID feed(s) from %s' % (len(feeds), ASTRA_CONF_PATH))

    total_pids_scanned = 0

    # 2. Tune to each feed service before analyzing that PID's output channel
    for feed in feeds:
        label = feed['label']
        sref = feed['sref']
        pid = feed['pid']
        url = feed['url']

        print('\n' + '=' * 50)
        print('Starting Scan for Channel: %s (PID %d)' % (label, pid))
        if feed['t2mi']:
            print('T2MI output: %s' % url)
        print('=' * 50)

        # 3. Tune to this PID's feed service first
        if ZAP_BEFORE_ANALYZE:
            if not zap_to_service(sref):
                print('Skipping PID %d for %s due to zap failure.'
                      % (pid, label))
                continue
            if POST_ZAP_SETTLE > 0:
                print('[*] Letting feed service settle for %d seconds...'
                      % POST_ZAP_SETTLE)
                time.sleep(POST_ZAP_SETTLE)
        else:
            print('[*] Skipping zap; analyzing streams as currently tuned.')

        # 4. Scan this PID's output channel (T2MI-decapsulated when present)
        if analyze_pid(pid, url):
            total_pids_scanned += 1
        if (feed['t2mi'] and ANALYZE_RAW_T2MI_OUTPUT
                and feed['raw_url'] != url):
            analyze_pid(pid, feed['raw_url'], suffix='_raw')

    print('\n' + '=' * 50)
    print('Scan Complete! Processed %d PIDs.' % total_pids_scanned)
    print("All logs are safely stored in the '%s' directory." % OUTPUT_DIR)
    print('=' * 50)


if __name__ == '__main__':
    main()
