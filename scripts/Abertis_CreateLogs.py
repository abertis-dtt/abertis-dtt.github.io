#!/usr/bin/env python3
"""Create astra analysis logs for the Abertis/Cellnex (Hispasat 30.0W) feeds.

Runs on the Enigma2 side. Instead of a hand-maintained transponder/PID table,
it derives every (feed service reference, PID) pair from an astra.conf file,
so the list of feeds always matches the generated suite.

Each logical transponder exposes a single feed service reference and one astra
output per PID; the script zaps to that service reference once per transponder
and then runs ``astra --analyze`` on each PID's output URL.
"""
import os
import re
import sys
import time
import urllib.request
import subprocess

# =======================================================================
# CONFIGURATION
# =======================================================================
# Path to the astra.conf produced by Abertis_DX_Suite.py (override by passing
# a path as the first argument, or via the ASTRA_CONF environment variable).
ASTRA_CONF_PATH = (sys.argv[1] if len(sys.argv) > 1
                    else os.environ.get('ASTRA_CONF', '/etc/astra/astra.conf'))

# How long (in seconds) to run the analysis for each PID to gather SDT/PMT data
ANALYZE_DURATION = 30

# Delay after a successful zap before launching astra --analyze. Lets the
# feed service settle so the analysis matches a manual run.
POST_ZAP_SETTLE = 4

# Zap to the feed service before analyzing a transponder group. Set to False to
# analyze the streams exactly as they come in now (matches a manual
# `astra --analyze URL` run made without zapping first).
ZAP_BEFORE_ANALYZE = True

# Output directory for the logs
OUTPUT_DIR = "pid_logs"

# =======================================================================


def parse_astra_conf(path):
    """Return one record per PID channel as found in ``make_channel(...)`` blocks.

    Each record carries the channel label, its feed service reference (taken
    from that same channel's input URL) and the output URL that astra
    --analyze must use. Channels are tuned one at a time so the analysis always
    follows the correct feed service.
    """
    channels = []

    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError as exc:
        print('Error: cannot read astra.conf %s: %s' % (path, exc))
        return channels

    for block in re.finditer(r'make_channel\s*\(\s*\{(.*?)\}\s*\)',
                             text, re.DOTALL):
        body = block.group(1)

        name_m = re.search(r'name\s*=\s*"([^"]*)"', body)
        input_m = re.search(r'input\s*=\s*\{\s*"([^"]*)"', body, re.DOTALL)
        output_m = re.search(r'output\s*=\s*\{\s*"([^"]*)"', body, re.DOTALL)
        if not input_m or not output_m:
            continue

        input_url = input_m.group(1)
        output_url = output_m.group(1)

        # Strip the scheme/host/port, keep the trailing Enigma2 sref (colon).
        sref = input_url.split('://', 1)[-1]
        if '/' in sref:
            sref = sref.split('/', 1)[1]
        sref = sref.strip()

        name = name_m.group(1).strip() if name_m else ''
        pid = None
        pid_m = re.search(r'/pid(\d+)', output_url, re.IGNORECASE)
        if pid_m:
            pid = int(pid_m.group(1))
        else:
            pid_m = re.search(r'PID\s+(\d+)', name, re.IGNORECASE)
            if pid_m:
                pid = int(pid_m.group(1))
        if pid is None:
            continue

        # Use the exact output URL published in astra.conf.
        analyze_url = output_url.strip()

        label = name.split(' PID ', 1)[0].strip() if ' PID ' in name else name
        if not label:
            label = sref

        channels.append({'label': label, 'sref': sref, 'pid': pid,
                         'url': analyze_url})

    return channels


def zap_to_service(sref):
    """Zap to the specified Enigma2 service reference using the local WebIf."""
    # Encode colons to ensure WebIf properly parses the string
    encoded_sref = sref.replace(':', '%3A')
    url = 'http://127.0.0.1/web/zap?sRef=%s' % encoded_sref
    try:
        print('[*] Zapping to feed service: %s' % sref)
        print('    -> zap command: curl -s %r | head' % url)
        # Call the local WebIf to change the channel
        with urllib.request.urlopen(url, timeout=5) as response:
            result = response.read().decode('utf-8')
            if 'true' in result.lower():
                print('    -> Zap successful. Waiting 6 seconds for tuner lock'
                      ' & stream start...')
                time.sleep(6)  # Give the tuner time to lock and astra time to route
                return True
            else:
                print('    -> Zap failed. Check the sRef format.')
                return False
    except Exception as e:
        print('    -> Error during zap (Is WebIf running?): %s' % e)
        return False


def analyze_pid(pid, url):
    """Run astra --analyze for the specified PID/output URL, save the log."""
    log_filepath = os.path.join(OUTPUT_DIR, 'pid%d.log' % pid)
    cmd = "astra --analyze '%s'" % url

    print('[*] Analyzing PID %d for %d seconds...' % (pid, ANALYZE_DURATION))
    print('    -> command: %s' % cmd)

    with open(log_filepath, 'w', encoding='utf-8') as log_file:
        try:
            # Start the astra analysis process in the background
            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=log_file,
                stderr=subprocess.STDOUT
            )

            # astra --analyze runs forever, so we intentionally wait for a timeout
            process.wait(timeout=ANALYZE_DURATION)

        except subprocess.TimeoutExpired:
            # Time is up! Terminate the process to finish the log
            process.terminate()
            process.wait()
            print('    -> Finished PID %d. Log saved to %s' % (pid, log_filepath))
        except Exception as e:
            print('    -> Error running analysis for PID %d: %s' % (pid, e))


def main():
    channels = parse_astra_conf(ASTRA_CONF_PATH)
    if not channels:
        print('No feeds found in %s' % ASTRA_CONF_PATH)
        sys.exit(1)

    # 1. Create the pid_logs directory if it doesn't exist
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        print('Created directory: %s/' % OUTPUT_DIR)

    total_pids_scanned = 0

    print('Parsed %d PID channel(s) from %s' % (len(channels), ASTRA_CONF_PATH))

    # 2. Tune to each feed service before analyzing that PID's output URL
    for idx, ch in enumerate(channels, 1):
        label = ch['label']
        sref = ch['sref']
        pid = ch['pid']
        url = ch['url']

        print('\n' + '=' * 50)
        print('Starting Scan for Channel: %s (PID %d)' % (label, pid))
        print('=' * 50)

        # 3. Tune to this PID's feed service first
        if ZAP_BEFORE_ANALYZE:
            zap_success = zap_to_service(sref)
            if not zap_success:
                print('Skipping PID %d for %s due to zap failure.' % (pid, label))
                continue
            if POST_ZAP_SETTLE > 0:
                print('[*] Letting feed service settle for %d seconds...' %
                      POST_ZAP_SETTLE)
                time.sleep(POST_ZAP_SETTLE)
        else:
            print('[*] Skipping zap; analyzing streams as currently tuned.')

        # 4. Scan this PID's output channel
        analyze_pid(pid, url)
        total_pids_scanned += 1

    print('\n' + '=' * 50)
    print('Scan Complete! Processed %d PIDs.' % total_pids_scanned)
    print('All logs are safely stored in the \'%s\' directory.' % OUTPUT_DIR)
    print('=' * 50)


if __name__ == '__main__':
    main()