#!/usr/bin/env python3
"""
Update sat_data.json channel data using the Cellnex FlySat Satellite Chart 11/2026 HTML.
Parses the HTML file, extracts transponder/stream/channel data, and merges it into sat_data.json.
"""
import json
import re
import html as ihtml

HTML_FILE = "/home/osboxes/abertis-dtt.github.io/cellnex - FlySat Satellite Chart 11_2026.html"
JSON_FILE = "/home/osboxes/abertis-dtt.github.io/sat_data.json"


def clean_text(s):
    """Remove HTML tags and decode entities, collapse whitespace."""
    s = re.sub(r'<[^>]+>', ' ', s)
    s = ihtml.unescape(s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def parse_apid_lang(s):
    """Convert HTML apid_lang (with <br>) to JSON format (space-separated)."""
    s = s.replace('<br>', ' ').replace('<br/>', ' ').replace('<br />', ' ')
    return clean_text(s)


def system_to_type_system(sys_str):
    """Map HTML System cell to JSON (type, system)."""
    sys_str = sys_str.strip()
    if '/' in sys_str:
        parts = sys_str.split('/')
        codec = parts[0].strip()
        res = parts[1].strip()
        return (res, codec)
    else:
        if sys_str == '':
            return ('Radio', '')
        return ('', sys_str)


def stream_number(title):
    """Extract the stream number (e.g. 2025 from 'Stream 2025 - ...')."""
    m = re.search(r'Stream\s+(\d+)', title)
    return m.group(1) if m else None


def stream_title_key(title):
    """Normalize a stream title for matching."""
    t = re.sub(r'\s*\(HS[^)]*\)\s*', ' ', title)
    t = re.sub(r'\s+', ' ', t).strip(' -')
    return t


def extract_td_cells(row):
    """Return list of cell contents (inner HTML) for each <td> in a row."""
    return re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)


def parse_html_transponders(html_text):
    """Parse the HTML and return a list of transponder dicts."""
    # Full <tr>...</tr> (incl. opening tag so class/rowspan attributes are visible)
    rows = re.findall(r'<tr[^>]*>.*?</tr>', html_text, re.DOTALL | re.IGNORECASE)

    transponders = []
    i = 0
    while i < len(rows):
        row = rows[i]

        # Detect transponder header row: first cell has Txp No with rowspan
        m = re.search(
            r'<td[^>]*rowspan="\d+"[^>]*><font face="Verdana" size="1">(\d+[A-Z]?)</font></td>',
            row
        )
        if not m:
            i += 1
            continue

        txp_no = m.group(1).strip()

        cells = extract_td_cells(row)
        if len(cells) < 6:
            i += 1
            continue

        onid_nid_tid = clean_text(cells[1])
        # freq_pol_mode cell: <b>freq</b><br><font>mode</font>
        freq_raw = cells[2]
        freq_match = re.search(r'<b>(.*?)</b><br>', freq_raw, re.DOTALL)
        freq_part = clean_text(freq_match.group(1)) if freq_match else ''
        mode_match = re.search(r'<br><font[^>]*>(.*?)</font>', freq_raw, re.DOTALL)
        mode_part = clean_text(mode_match.group(1)) if mode_match else ''
        freq_part = re.sub(r'\.\s+', '.', freq_part)
        freq_pol_mode = (freq_part + ' ' + mode_part).strip()
        sr_fec = clean_text(cells[3])
        provider = clean_text(cells[4])
        date = clean_text(cells[-1])

        tp = {
            'txp_no': txp_no,
            'onid_nid_tid': onid_nid_tid,
            'freq_pol_mode': freq_pol_mode,
            'sr_fec': sr_fec,
            'provider': provider,
            'date': date,
            'streams': []
        }

        i += 1
        current_stream = None

        while i < len(rows):
            row = rows[i]

            # New transponder header?
            if re.search(
                r'<td[^>]*rowspan="\d+"[^>]*><font face="Verdana" size="1">\d+[A-Z]?</font></td>',
                row
            ):
                break

            cells = extract_td_cells(row)

            # Stream header row (class="cstream" in opening tag)
            if 'class="cstream"' in row:
                # Get full bold title text
                full_match = re.search(r'<b>(.*?)</b>', row, re.DOTALL)
                if full_match:
                    title = clean_text(full_match.group(1))
                else:
                    title = ''

                pid_match = re.search(
                    r'<font face="Verdana" size="1" color="#008000">([^<]+)</font>', row
                )
                pid_info = pid_match.group(1).strip() if pid_match else ''

                enc_match = re.search(
                    r'<font face="Verdana" color="#0000ff" size="1"><b>([^<]+)</b>', row
                )
                encryption = enc_match.group(1).strip() if enc_match else ''

                current_stream = {
                    'title': title,
                    'pid_info': pid_info,
                    'encryption': encryption,
                    'channels': []
                }
                tp['streams'].append(current_stream)

            # Channel row: has background-color
            elif 'background-color' in row and current_stream is not None and cells:
                # Cell 0: name
                name = ''
                # TV channel: <font face="Arial" size="2" color="#000099"><b>NAME</b></font>
                tv_match = re.search(
                    r'<font face="Arial" size="2" color="#000099"><b>([^<]+)</b></font>', row
                )
                if tv_match:
                    name = clean_text(tv_match.group(1))
                else:
                    # Radio channel: <font ... class="radio_channel">-<b>R</b>- NAME</font>
                    radio_match = re.search(
                        r'class="radio_channel">-<b>[^<]*</b>-(.*?)</font>', row
                    )
                    if radio_match:
                        name = clean_text(radio_match.group(1))
                    else:
                        i += 1
                        continue

                is_radio = 'class="radio_channel"' in row

                # Cell positions:
                #   0: name, 1: system, 2: sid, 3: vpid, 4: apid_lang, ... last: comments
                sys_str = clean_text(cells[1]) if len(cells) > 1 else ''
                channel_sid = clean_text(cells[2]) if len(cells) > 2 else ''
                channel_vpid = clean_text(cells[3]) if len(cells) > 3 else ''
                channel_apid = parse_apid_lang(cells[4]) if len(cells) > 4 else ''

                type_val, system_val = system_to_type_system(sys_str)
                if is_radio:
                    type_val = 'Radio'
                    system_val = ''
                    channel_vpid = ''

                # Comments: last non-empty cell
                comments = ''
                for c in reversed(cells):
                    cc = clean_text(c)
                    if cc in ('new', 'upd'):
                        comments = cc
                        break

                ch = {
                    'type': type_val,
                    'name': name,
                    'system': system_val,
                    'sid': channel_sid,
                    'vpid': channel_vpid,
                    'apid_lang': channel_apid,
                    '_comments': comments,
                }
                current_stream['channels'].append(ch)

            i += 1

        transponders.append(tp)

    return transponders


def normalize_apid_json(apid):
    if not apid:
        return ''
    return re.sub(r'\s+', ' ', apid.strip()).upper()


def merge():
    with open(HTML_FILE, 'r', encoding='utf-8', errors='replace') as f:
        html_text = f.read()

    with open(JSON_FILE, 'r', encoding='utf-8') as f:
        json_data = json.load(f)

    html_tps = parse_html_transponders(html_text)

    print(f"Parsed {len(html_tps)} transponders from HTML")
    total_html_streams = sum(len(t['streams']) for t in html_tps)
    total_html_ch = sum(len(s['channels']) for t in html_tps for s in t['streams'])
    print(f"  Streams: {total_html_streams}, Channels: {total_html_ch}")
    print(f"Loaded {len(json_data)} transponders from JSON")

    # Build lookup for JSON transponders by (txp_no, provider) -> list
    json_lookup = {}
    for jtp in json_data:
        key = (jtp['txp_no'], jtp['provider'])
        json_lookup.setdefault(key, []).append(jtp)

    def freq_number(freq_pol_mode):
        m = re.match(r'\s*([\d\.]+)', freq_pol_mode or '')
        return m.group(1) if m else ''

    updates = 0
    new_channels_total = 0
    updated_channels_total = 0
    streams_not_found = 0

    for htp in html_tps:
        key = (htp['txp_no'], htp['provider'])
        candidates = json_lookup.get(key)
        if not candidates:
            print(f"  SKIP: transponder {htp['txp_no']} ({htp['freq_pol_mode']}) not found in JSON")
            continue

        h_freq = freq_number(htp['freq_pol_mode'])
        jtp = None
        for cand in candidates:
            if freq_number(cand['freq_pol_mode']) == h_freq:
                jtp = cand
                break
        if jtp is None:
            print(f"  SKIP: transponder {htp['txp_no']} freq {h_freq} not found in JSON")
            continue

        # Update transponder-level fields
        if jtp.get('date') != htp['date']:
            print(f"  Txp {htp['txp_no']}: date {jtp.get('date')} -> {htp['date']}")
            jtp['date'] = htp['date']
            updates += 1
        if jtp.get('freq_pol_mode') != htp['freq_pol_mode']:
            print(f"  Txp {htp['txp_no']}: freq_pol_mode '{jtp.get('freq_pol_mode')}' -> '{htp['freq_pol_mode']}'")
            jtp['freq_pol_mode'] = htp['freq_pol_mode']
            updates += 1
        if jtp.get('sr_fec') != htp['sr_fec']:
            print(f"  Txp {htp['txp_no']}: sr_fec '{jtp.get('sr_fec')}' -> '{htp['sr_fec']}'")
            jtp['sr_fec'] = htp['sr_fec']
            updates += 1

        # Build lookup for JSON streams: by stream number, then by title key
        json_stream_by_num = {}
        json_stream_by_key = {}
        for jst in jtp['streams']:
            num = stream_number(jst['title'])
            if num:
                json_stream_by_num[num] = jst
            json_stream_by_key[stream_title_key(jst['title'])] = jst

        def find_json_stream(hst):
            hnum = stream_number(hst['title'])
            if hnum and hnum in json_stream_by_num:
                return json_stream_by_num[hnum]
            hkey = stream_title_key(hst['title'])
            if hkey in json_stream_by_key:
                return json_stream_by_key[hkey]
            # Fallback: substring match on title key
            for k, jst2 in json_stream_by_key.items():
                if hkey in k or k in hkey:
                    return jst2
            return None

        for hst in htp['streams']:
            jst = find_json_stream(hst)
            if jst is None:
                print(f"    STREAM NOT FOUND in JSON: {hst['title']}")
                streams_not_found += 1
                continue

            # Build lookup for JSON channels by sid
            json_ch_lookup = {}
            for jch in jst['channels']:
                json_ch_lookup[str(jch['sid'])] = jch

            html_ch_by_sid = {}
            for hch in hst['channels']:
                html_ch_by_sid[str(hch['sid'])] = hch

            # Update existing channels and add new ones
            for hsid, hch in html_ch_by_sid.items():
                if hsid in json_ch_lookup:
                    jch = json_ch_lookup[hsid]
                    changed = False
                    if jch.get('name') != hch['name']:
                        print(f"      {hst['title']}: name '{jch.get('name')}' -> '{hch['name']}'")
                        jch['name'] = hch['name']
                        changed = True
                    if jch.get('type') != hch['type']:
                        print(f"      {hst['title']}: type '{jch.get('type')}' -> '{hch['type']}'")
                        jch['type'] = hch['type']
                        changed = True
                    if jch.get('system') != hch['system']:
                        print(f"      {hst['title']}: system '{jch.get('system')}' -> '{hch['system']}'")
                        jch['system'] = hch['system']
                        changed = True
                    if jch.get('vpid') != hch['vpid']:
                        print(f"      {hst['title']}: vpid '{jch.get('vpid')}' -> '{hch['vpid']}'")
                        jch['vpid'] = hch['vpid']
                        changed = True
                    j_apid = normalize_apid_json(jch.get('apid_lang', ''))
                    h_apid = normalize_apid_json(hch['apid_lang'])
                    if j_apid != h_apid:
                        print(f"      {hst['title']}: apid_lang '{jch.get('apid_lang')}' -> '{hch['apid_lang']}'")
                        jch['apid_lang'] = hch['apid_lang']
                        changed = True
                    if changed:
                        updated_channels_total += 1
                else:
                    print(f"      NEW channel in {hst['title']}: {hch['name']} (sid={hch['sid']}, comments={hch['_comments']})")
                    new_ch = {
                        'type': hch['type'],
                        'name': hch['name'],
                        'system': hch['system'],
                        'sid': hch['sid'],
                        'vpid': hch['vpid'],
                        'apid_lang': hch['apid_lang'],
                    }
                    jst['channels'].append(new_ch)
                    new_channels_total += 1

            # Remove channels in JSON that are not in HTML
            html_sids = set(html_ch_by_sid.keys())
            for sid in list(json_ch_lookup.keys()):
                if sid not in html_sids:
                    jch = json_ch_lookup[sid]
                    print(f"      REMOVED channel from {hst['title']}: {jch['name']} (sid={sid})")
                    jst['channels'] = [c for c in jst['channels'] if str(c['sid']) != sid]

    # Write back
    with open(JSON_FILE, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, indent=4)
        f.write('\n')

    print(f"\n=== Summary ===")
    print(f"Transponder field updates: {updates}")
    print(f"New channels added: {new_channels_total}")
    print(f"Existing channels updated: {updated_channels_total}")
    print(f"Streams not found: {streams_not_found}")
    print(f"Done. {JSON_FILE} updated.")


if __name__ == '__main__':
    merge()
