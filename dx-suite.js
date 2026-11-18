/* ==========================================================================
   Abertis DX Suite - browser port of scripts/Abertis_DX_Suite.py
   --------------------------------------------------------------------------
   Generates the same Enigma2 lamedb / astra.conf / bouquet / SoftCam.Key
   files as the Python CLI, completely client side. Exposed as DXSuite.
   ========================================================================== */
(function (global) {
    'use strict';

    const DEFAULT_JSON_URL = 'https://abertis-dtt.github.io/sat_data.json';
    const BOUQUET_HEADER = '#NAME \u2022 30,0 W - Abertis & Cellnex DX Suite ( streamrelay )';

    const FEC_CODES = {
        '1/2': 1, '2/3': 2, '3/4': 3, '5/6': 4, '6/7': 5, '7/8': 6,
        '8/9': 7, '3/5': 8, '4/5': 9, '9/10': 10, 'AUTO': 15
    };
    const MODULATION_CODES = { QPSK: 0, '8PSK': 2, '16APSK': 3, '32APSK': 4 };
    const POLARISATION_CODES = { H: 0, V: 1 };

    const BISS_CAID = '2600';
    const BISS_CRC_SEED = 0x2600;
    const DUMMY_BISS_KEY = '0000000000000000';

    const STREAM_PID_RE = /Stream\s+(\d+)/i;
    const FREQ_POL_MODE_RE = /^\s*([0-9]+(?:\.[0-9]+)?)\s+([HVLR])\s+(.+?)\s*$/;
    const SERVICE_FLAG_PREFIXES = ['p:', 'c:', 'C:', 'f:', 'v:'];

    /* ── small helpers ─────────────────────────────────────── */

    // Python int(): strict integer strings only (no parseFloat truncation).
    function pyInt(value) {
        if (value === null || value === undefined) return null;
        const s = String(value).trim();
        if (!/^[+-]?\d+$/.test(s)) return null;
        const n = parseInt(s, 10);
        return Number.isNaN(n) ? null : n;
    }

    function str(value) {
        return value === null || value === undefined ? '' : String(value);
    }

    function hex(value, width) {
        const n = Number(value) >>> 0;
        return n.toString(16).padStart(width, '0');
    }

    function rep(value) {
        return JSON.stringify(value === undefined ? null : value);
    }

    function namespaceFor(positionTenths) {
        const pos = positionTenths < 0 ? positionTenths + 3600 : positionTenths;
        return (((pos & 0xFFFF) << 16) >>> 0);
    }

    function positionLabel(tenths) {
        const n = pyInt(tenths);
        if (n === null) return '?';
        return (Math.abs(n) / 10).toFixed(1) + '\u00b0 ' + (n < 0 ? 'W' : 'E');
    }

    /* ── CRC-32 (zlib compatible, used for BISS key ids) ───── */

    const CRC_TABLE = (function () {
        const table = new Uint32Array(256);
        for (let n = 0; n < 256; n++) {
            let c = n;
            for (let k = 0; k < 8; k++) {
                c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
            }
            table[n] = c >>> 0;
        }
        return table;
    })();

    function crc32(bytes, seed) {
        let crc = ((seed === undefined ? 0 : seed) ^ 0xFFFFFFFF) >>> 0;
        for (let i = 0; i < bytes.length; i++) {
            crc = (CRC_TABLE[(crc ^ bytes[i]) & 0xFF] ^ (crc >>> 8)) >>> 0;
        }
        return (crc ^ 0xFFFFFFFF) >>> 0;
    }

    function softcamKeyId(sid, pmtPid, streamPid) {
        const bytes = new Uint8Array(6);
        bytes[0] = (sid >>> 8) & 0xFF; bytes[1] = sid & 0xFF;
        bytes[2] = (pmtPid >>> 8) & 0xFF; bytes[3] = pmtPid & 0xFF;
        bytes[4] = (streamPid >>> 8) & 0xFF; bytes[5] = streamPid & 0xFF;
        return crc32(bytes, BISS_CRC_SEED);
    }

    /* ── source parsing (port of parse_transponders) ───────── */

    function parseTransponders(data, log) {
        const txps = new Map();
        for (const entry of data) {
            if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue;

            const match = FREQ_POL_MODE_RE.exec(str(entry.freq_pol_mode));
            if (!match) {
                log('warn', 'Cannot parse freq_pol_mode ' + rep(entry.freq_pol_mode) +
                    ' on txp ' + str(entry.txp_no) + ' - skipped');
                continue;
            }
            const freqMhz = parseFloat(match[1]);
            const pol = match[2];
            const delivery = match[3].toUpperCase();
            const system = delivery.indexOf('S2') !== -1 ? 1 : 0;
            const modToken = delivery.split('/').pop().split(/\s+/).filter(Boolean)[0] || '';
            const modulation = OBJECT_HAS(MODULATION_CODES, modToken)
                ? MODULATION_CODES[modToken] : (system ? 2 : 0);

            if (!OBJECT_HAS(POLARISATION_CODES, pol)) {
                log('warn', 'Unusual polarisation ' + rep(pol) + ' on txp ' + str(entry.txp_no));
            }
            if (delivery.indexOf('PLS') !== -1 || delivery.indexOf('MISO') !== -1) {
                const marker = delivery.indexOf('PLS') !== -1 ? 'PLS' : 'MISO';
                log('warn', 'Txp ' + str(entry.txp_no) + ' (' + str(entry.freq_pol_mode) +
                    ') uses ' + delivery.slice(delivery.indexOf(marker)) +
                    ' - lamedb cannot carry PLS/MISO parameters, add them manually if your image supports it');
            }

            const srFec = str(entry.sr_fec).split(/\s+/).filter(Boolean);
            // onid is '-' on part of the Abertis network; the confirmed
            // original network id there is 1, so fall back to it.
            const parts = str(entry.onid_nid_tid).split('/').map(function (p) { return p.trim(); });
            let onid = 1;
            let tsid = null;
            if (parts.length === 3) {
                tsid = pyInt(parts[2]);
                const parsedOnid = pyInt(parts[0]);
                if (parsedOnid !== null) onid = parsedOnid;
            }
            if (tsid === null) {
                log('warn', 'Cannot parse tsid in onid_nid_tid ' + rep(entry.onid_nid_tid) +
                    ' on txp ' + str(entry.txp_no) + ' - skipped');
                continue;
            }
            if (!srFec.length) {
                log('warn', 'Missing sr_fec on txp ' + str(entry.txp_no));
            }

            const provider = (str(entry.provider) || 'Abertis DTT').trim();
            const providerWords = provider.split(/\s+/).filter(Boolean);
            const tp = {
                txp_no: entry.txp_no === undefined ? '?' : entry.txp_no,
                freq_khz: Math.trunc(freqMhz * 1000 + 0.5),
                freq_label: String(Math.trunc(freqMhz)),
                pol_letter: pol,
                pol: OBJECT_HAS(POLARISATION_CODES, pol) ? POLARISATION_CODES[pol] : 0,
                system: system,
                modulation: modulation,
                delivery: delivery,
                sr_ksps: srFec.length ? pyInt(srFec[0]) || 0 : 0,
                fec: FEC_CODES[srFec.length > 1 ? srFec[1].toUpperCase() : ''] !== undefined
                    ? FEC_CODES[srFec.length > 1 ? srFec[1].toUpperCase() : ''] : 15,
                onid: onid,
                tsid: tsid,
                provider: provider,
                provider_short: providerWords.length ? providerWords[0] : 'Abertis',
                streams: Array.isArray(entry.streams) ? entry.streams : []
            };

            const key = tp.tsid + ':' + tp.onid;
            if (txps.has(key)) {
                txps.get(key).streams = txps.get(key).streams.concat(tp.streams);
            } else {
                txps.set(key, tp);
            }
        }
        return Array.from(txps.values());
    }

    function OBJECT_HAS(obj, key) {
        return Object.prototype.hasOwnProperty.call(obj, key);
    }

    /* ── stream collection (port of collect_streams) ───────── */

    function collectStreams(txps, log) {
        const streams = [];
        const seen = new Map();
        const channelLists = [];

        for (const tp of txps) {
            for (const raw of tp.streams) {
                if (!raw || typeof raw !== 'object') continue;
                const title = str(raw.title);
                const match = STREAM_PID_RE.exec(title);
                if (!match) {
                    channelLists.push(title || '(untitled)');
                    continue;
                }
                const pid = parseInt(match[1], 10);
                const tk = tp.tsid + ':' + tp.onid;
                if (seen.has(pid) && seen.get(pid) !== tk) {
                    log('warn', 'PID ' + pid + ' appears on more than one transponder - ' +
                        'astra/bouquet URLs will collide');
                }
                seen.set(pid, tk);

                const channels = [];
                const sids = new Set();
                for (const ch of (Array.isArray(raw.channels) ? raw.channels : [])) {
                    if (!ch || typeof ch !== 'object') continue;
                    const name = str(ch.name).trim();
                    const sid = pyInt(ch.sid);
                    if (sid === null) continue;
                    if (!name || sid <= 0 || sids.has(sid)) continue;
                    sids.add(sid);
                    const ctype = str(ch.type).trim().toLowerCase();
                    const radio = ctype.indexOf('radio') !== -1 ||
                        (!ctype && !str(ch.vpid).trim());
                    channels.push({ sid: sid, name: name, radio: radio });
                }

                // Stream-level identifiers from the database.
                function idField(key, fallback, lo, hi) {
                    const keys = Array.isArray(key) ? key : [key];
                    let value = '';
                    for (const k of keys) {
                        const rawValue = raw[k];
                        value = rawValue === null || rawValue === undefined ? '' : String(rawValue).trim();
                        if (value && ['N/A', 'NA', '-'].indexOf(value.toUpperCase()) === -1) break;
                        value = '';
                    }
                    if (!value) return fallback;
                    const num = pyInt(value);
                    if (num === null || num < lo || num > hi) {
                        log('warn', 'Invalid ' + keys[0] + ' ' + rep(value) + ' on ' +
                            rep(title) + ' - using ' + fallback);
                        return fallback;
                    }
                    return num;
                }

                // T2MI metadata is optional: N/A, missing or invalid values keep
                // the stream out of T2MI generation entirely (no silent default).
                function t2miField(key, lo, hi) {
                    const keys = Array.isArray(key) ? key : [key];
                    for (const k of keys) {
                        if (!(k in raw)) continue;
                        const rawValue = raw[k];
                        const value = rawValue === null || rawValue === undefined ? '' : String(rawValue).trim();
                        if (!value || ['N/A', 'NA', '-'].indexOf(value.toUpperCase()) !== -1) return null;
                        const num = pyInt(value);
                        if (num === null || num < lo || num > hi) {
                            log('warn', 'Invalid ' + keys[0] + ' ' + rep(value) + ' on ' +
                                rep(title) + ' - T2MI disabled');
                            return null;
                        }
                        return num;
                    }
                    return null;
                }

                const t2miPid = t2miField('t2mi_pid', 0, 0x1FFF);
                const plp = t2miField(['t2mi_plp', 'plp'], 0, 255);

                streams.push({
                    pid: pid,
                    title: title,
                    encryption: str(raw.encryption).toUpperCase(),
                    sid: idField('sid', pid + 1000, 1, 0xFFFF),
                    cpid: idField('cpid', pid + 5000, 0, 0xFFFF),
                    plp: plp,
                    t2mi_pid: t2miPid,
                    t2mi: t2miPid !== null && plp !== null,
                    t2mi_pnr: idField('t2mi_pnr', 0, 0, 0xFFFF),
                    txp: tp,
                    channels: channels
                });
            }
        }

        streams.sort(function (a, b) { return a.pid - b.pid; });

        if (channelLists.length) {
            const unique = Array.from(new Set(channelLists)).sort();
            log('info', 'Skipped ' + channelLists.length + ' whole-transponder channel list(s) ' +
                '(no single PID to relay): ' + unique.join(', '));
        }
        return streams;
    }

    /* ── naming helpers ────────────────────────────────────── */

    function feedServiceName(stream) {
        const tp = stream.txp;
        return tp.provider_short + '@' + tp.freq_label + tp.pol_letter +
            tp.sr_ksps + ' PID ' + stream.pid;
    }

    function providerCode(stream) {
        const short = str(stream.txp.provider_short || 'Abertis').trim();
        return (short.slice(0, 4) || 'PROV').toUpperCase();
    }

    function astraFeedName(stream) {
        const tp = stream.txp;
        return tp.provider_short + '@' + tp.freq_label + tp.pol_letter +
            tp.sr_ksps + 'PID' + stream.pid;
    }

    function t2miTag(stream, t2miPid, plp) {
        const tp = stream.txp;
        const code = providerCode(stream);
        const low = code.toLowerCase();
        const pid = stream.pid;
        const varName = 'f' + tp.freq_label + low + pid + 'pid' + t2miPid + 'plp' + plp;
        const decapName = tp.freq_label + tp.pol_letter + tp.sr_ksps + '@' +
            code + pid + '_PID' + t2miPid + 'PLP' + plp;
        return [varName, decapName, decapName.toLowerCase(),
            low + pid + '_pid' + t2miPid + 'plp' + plp];
    }

    function relayScriptFor(stream, override) {
        return override || str(stream.txp.provider_short).toLowerCase();
    }

    /* ── lamedb ────────────────────────────────────────────── */

    function buildLamedbEntries(txps, streams, position) {
        const ns = namespaceFor(position);
        const tpEntries = [];
        for (const tp of txps) {
            const header = hex(ns, 8) + ':' + hex(tp.tsid, 4) + ':' + hex(tp.onid, 4);
            const sline = '\ts ' + tp.freq_khz + ':' + (tp.sr_ksps * 1000) + ':' +
                tp.pol + ':' + tp.fec + ':' + position + ':2:0:' + tp.system + ':' +
                tp.modulation + ':0:2';
            tpEntries.push([header.split(':'), [header, sline]]);
        }

        const svcEntries = [];
        for (const stream of streams) {
            const tp = stream.txp;
            const header = hex(stream.sid, 4) + ':' + hex(ns, 8) + ':' +
                hex(tp.tsid, 4) + ':' + hex(tp.onid, 4) + ':1:0';
            const block = [header, feedServiceName(stream)];
            const provider = tp.provider_short.toUpperCase();
            if (stream.encryption.indexOf('BISS') !== -1) {
                block.push('p:' + provider + ',c:031fff,c:18' + hex(stream.pid, 4) +
                    ',c:19' + hex(stream.cpid, 4) + ',C:' + BISS_CAID + ',f:01');
            } else {
                block.push('p:' + provider + ',c:031fff,c:18' + hex(stream.pid, 4) +
                    ',c:19' + hex(stream.cpid, 4) + ',f:01');
            }
            svcEntries.push([header.split(':').slice(0, 4), block]);
        }
        return { tpEntries: tpEntries, svcEntries: svcEntries };
    }

    function lamedbText(tpEntries, svcEntries) {
        const lines = ['eDVB services /4/', 'transponders'];
        for (const entry of tpEntries) {
            lines.push.apply(lines, entry[1]);
            lines.push('/');
        }
        lines.push('end', 'services');
        for (const entry of svcEntries) {
            lines.push.apply(lines, entry[1]);
        }
        lines.push('end');
        return lines.join('\n') + '\n';
    }

    /* ── astra.conf ────────────────────────────────────────── */

    function astraText(streams, options, log) {
        const ns = namespaceFor(options.position);
        const header = [
            '-- Astra Slonik Mod', '',
            '-- during revival and testing',
            '-- log.set({ stdout = true, debug = true, syslog = "astra", filename = "/tmp/astrasm.log", })',
            '-- after revival and testing',
            'log.set({ stdout = true, debug = false, syslog = "astra", filename = "/tmp/astra.log" })', '',
            '-- =========================================================================',
            '-- [ SECTION 1: ABERTIS PID CHANNELS ]',
            '-- =========================================================================', '',
            '-- Helper Text: This table maps the Abertis PID to its specific Enigma2 Stream Reference.', '', ''
        ].join('\n');

        const blocks = [];
        for (const stream of streams) {
            const tp = stream.txp;
            const sid = stream.sid;
            const sref = '1:0:1:' + sid.toString(16).toUpperCase() + ':' +
                tp.tsid.toString(16).toUpperCase() + ':' + tp.onid + ':' +
                ns.toString(16).toUpperCase() + ':0:0:0';
            const relay = relayScriptFor(stream, options.relayScript);

            let block = [
                '-- ' + tp.freq_label + ' ' + tp.pol_letter + ' ' + tp.sr_ksps + ' PID' + stream.pid + ' Stream',
                'make_channel({',
                '  name = "' + astraFeedName(stream) + '",',
                '  input = {',
                '    "http://' + options.relayHost + ':' + options.relayPort + '/' + sref + ':",',
                '  },',
                '  transform = {{',
                '    format = "pipe",',
                '      command = "/etc/astra/scripts/' + relay + ' ' + stream.pid + '",',
                '  }},',
                '  output = {',
                '    "http://' + options.outHost + ':' + options.httpPort + '/' + relay + '/pid' + stream.pid + '",',
                '  },',
                '})'
            ].join('\n');

            if (stream.t2mi) {
                const t2miPid = stream.t2mi_pid;
                let plp = stream.plp;
                if (plp === 0 && options.t2miPlp !== null && options.t2miPlp !== undefined) {
                    plp = options.t2miPlp;
                }
                let pnr = stream.t2mi_pnr || 0;
                if (pnr === 0 && options.t2miPnr !== null && options.t2miPnr !== undefined) {
                    pnr = options.t2miPnr;
                }

                const tag = t2miTag(stream, t2miPid, plp);
                const decapVar = tag[0], decapName = tag[1], t2miName = tag[2], outputSeg = tag[3];

                block += '\n\n' + [
                    decapVar + ' = make_t2mi_decap({',
                    '        name = "' + decapName + '",',
                    '        input = "http://' + options.outHost + ':' + options.httpPort + '/' +
                        relay + '/pid' + stream.pid + '",',
                    '        plp = ' + plp + ',',
                    '        pnr = ' + pnr + ',',
                    '        pid = ' + t2miPid + ',',
                    '    })'
                ].join('\n');

                block += '\n\n' + [
                    'make_channel({',
                    '  name = "' + t2miName + '",',
                    '  input = {',
                    '    "t2mi://' + decapVar + '",',
                    '  },',
                    '  output = {',
                    '    "http://' + options.outHost + ':' + options.httpPort + '/' +
                        relay + '/' + outputSeg + '",',
                    '  },',
                    '})'
                ].join('\n');
            }

            blocks.push(block);
        }
        return header + blocks.join('\n\n') + '\n';
    }

    /* ── Enigma2 bouquet ───────────────────────────────────── */

    function bouquetText(streams, options, log) {
        const lines = [BOUQUET_HEADER];
        for (const stream of streams) {
            if (!stream.channels.length) {
                log('info', 'PID ' + stream.pid + ' has no channels in the JSON - ' +
                    'lamedb/astra entries only');
                continue;
            }
            const relay = relayScriptFor(stream, options.relayScript);
            let url;
            if (stream.t2mi) {
                const t2miPid = stream.t2mi_pid;
                let plp = stream.plp;
                if (plp === 0 && options.t2miPlp !== null && options.t2miPlp !== undefined) {
                    plp = options.t2miPlp;
                }
                const outputSeg = t2miTag(stream, t2miPid, plp)[3];
                url = 'http://' + options.outHost + ':' + options.httpPort + '/' + relay + '/' + outputSeg;
            } else {
                url = 'http://' + options.outHost + ':' + options.httpPort + '/' + relay + '/pid' + stream.pid;
            }

            const markerName = '##### ' + stream.txp.provider_short + ' PID ' + stream.pid + ' #####';
            lines.push('#SERVICE 1:64:' + stream.pid.toString(16).toUpperCase() +
                ':0:0:0:0:0:0:0::' + markerName);
            lines.push('#DESCRIPTION ##### ' + feedServiceName(stream) + ' #####');

            // Enigma2 requires colons in URLs to be URL-encoded (uppercase %3A)
            const encoded = url.replace(/:/g, '%3A');
            const groups = [
                ['1', '(TV)', stream.channels.filter(function (c) { return !c.radio; })],
                ['2', '(RADIO)', stream.channels.filter(function (c) { return c.radio; })]
            ];
            for (const group of groups) {
                const svcType = group[0], tag = group[1];
                for (const ch of group[2]) {
                    const name = tag + ' ' + ch.name;
                    lines.push('#SERVICE 1:0:' + svcType + ':' + ch.sid.toString(16).toUpperCase() +
                        ':2D85:12D:0:0:0:0:' + encoded + ':' + name);
                    lines.push('#DESCRIPTION ' + name);
                }
            }
        }
        return lines.join('\n') + '\n';
    }

    /* ── SoftCam.Key ───────────────────────────────────────── */

    function softcamText(streams) {
        const lines = [
            '; SoftCam.Key generated by the Abertis DX Suite',
            '; Key ID = CRC32(seed=0x2600, SID + PMT + stream PID), big-endian',
            '; Dummy BISS key 0000000000000000 - replace with the real BISS keys.',
            '; Format: F <key id> <expiry date> <BISS key> ;comment',
            ''
        ];
        const entries = new Map();
        for (const stream of streams) {
            if (stream.encryption.indexOf('BISS') === -1) continue;
            const keyId = softcamKeyId(stream.sid, stream.cpid, stream.pid);
            if (!entries.has(keyId)) entries.set(keyId, []);
            entries.get(keyId).push(feedServiceName(stream));
        }
        for (const entry of entries.entries()) {
            lines.push('F ' + hex(entry[0], 8).toUpperCase() + ' 00000000 ' +
                DUMMY_BISS_KEY + ' ;' + entry[1].join(' / '));
        }
        return lines.join('\n') + '\n';
    }

    /* ── existing lamedb merge / edit ──────────────────────── */

    function isHex(value) {
        return value.length > 0 && /^[0-9a-fA-F]+$/.test(value);
    }

    function parseLamedb(text) {
        const lines = text.split('\n');
        const header = lines.length ? lines[0] : '';
        if (header.indexOf('eDVB services') !== 0) {
            throw new Error('Not an Enigma2 lamedb (header ' + rep(header) + ')');
        }
        if (header.indexOf('/4/') === -1) {
            throw new Error('Unsupported lamedb format ' + rep(header) + ' (only v4 /4/ supported)');
        }

        let idx = 1;
        const tpBlocks = new Map();
        if (idx < lines.length && lines[idx].trim() === 'transponders') idx++;
        while (idx < lines.length) {
            const line = lines[idx];
            if (!line.trim()) { idx++; continue; }
            if (line.trim() === 'end') { idx++; break; }
            const tpParts = line.trim().split(':');
            if (!(tpParts.length >= 3 && isHex(tpParts[0]) && isHex(tpParts[1]) && isHex(tpParts[2]))) {
                idx++;
                continue;
            }
            const block = [line];
            idx++;
            while (idx < lines.length && lines[idx].charAt(0) === '\t') {
                block.push(lines[idx]);
                idx++;
            }
            if (idx < lines.length && lines[idx].trim() === '/') idx++;
            tpBlocks.set(tpParts.slice(0, 3).join(':'), block);
        }

        const svcBlocks = new Map();
        if (idx < lines.length && lines[idx].trim() === 'services') idx++;
        while (idx < lines.length) {
            const line = lines[idx];
            if (!line.trim()) { idx++; continue; }
            if (line.trim() === 'end') break;
            const svcParts = line.trim().split(':');
            if (!(svcParts.length >= 5 && svcParts[0] && svcParts[1] && svcParts[2] && svcParts[3] &&
                isHex(svcParts[0] + svcParts[1] + svcParts[2] + svcParts[3]))) {
                idx++;
                continue;
            }
            const block = [line];
            idx++;
            if (idx < lines.length && lines[idx].trim() &&
                !SERVICE_FLAG_PREFIXES.some(function (p) { return lines[idx].indexOf(p) === 0; })) {
                block.push(lines[idx]);
                idx++;
            }
            while (idx < lines.length &&
                SERVICE_FLAG_PREFIXES.some(function (p) { return lines[idx].indexOf(p) === 0; })) {
                block.push(lines[idx]);
                idx++;
            }
            svcBlocks.set(svcParts.slice(0, 4).join(':'), block);
        }
        return { tpBlocks: tpBlocks, svcBlocks: svcBlocks };
    }

    function normalizeTpBlock(block) {
        if (!block || !block.length) return block;
        const header = block[0].trim();
        let sline = '';
        for (const line of block.slice(1)) {
            const st = line.trim();
            if (st.indexOf('s ') === 0 || (!line.endsWith('\t') && line.indexOf('\t') !== -1)) {
                sline = line.charAt(0) === '\t' ? line : '\t' + st;
                break;
            }
        }
        if (!sline.trim()) {
            for (const line of block.slice(1)) {
                if (line.trim()) {
                    sline = line.charAt(0) === '\t' ? line : '\t' + line.trim();
                    break;
                }
            }
        }
        return [header, sline.trim() ? sline : ''];
    }

    function normalizeSvcBlock(block) {
        if (!block || !block.length) return block;
        const header = block[0].trim();
        let name = '';
        const flags = [];
        for (const line of block.slice(1)) {
            const st = line.trim();
            if (!st) continue;
            if (SERVICE_FLAG_PREFIXES.some(function (p) { return st.indexOf(p) === 0; })) {
                flags.push(st);
            } else if (!name) {
                name = st;
            } else {
                name = (name + ' ' + st).trim();
            }
        }
        return [header, name, flags.length ? flags.join(',') : ''];
    }

    function serviceName(block) {
        let name = '';
        if (block.length > 1) {
            const candidate = block[1];
            if (candidate.trim() &&
                !SERVICE_FLAG_PREFIXES.some(function (p) { return candidate.indexOf(p) === 0; })) {
                name = candidate.trim();
            }
        }
        return name;
    }

    function mergeIntoText(text, tpEntries, svcEntries) {
        const parsed = parseLamedb(text);
        const tpBlocks = parsed.tpBlocks;
        const svcBlocks = parsed.svcBlocks;

        const generatedTpKeys = new Set(tpEntries.map(function (e) { return e[0].join(':'); }));
        const originalTpKeys = new Set(tpBlocks.keys());
        const originalSvcKeys = new Set(svcBlocks.keys());

        let removedTp = 0;
        for (const key of Array.from(tpBlocks.keys())) {
            if (generatedTpKeys.has(key)) { tpBlocks.delete(key); removedTp++; }
        }
        let removedSvc = 0;
        for (const key of Array.from(svcBlocks.keys())) {
            if (serviceName(svcBlocks.get(key)).toLowerCase().indexOf('abertis') !== -1) {
                svcBlocks.delete(key);
                removedSvc++;
            }
        }

        let replaced = 0;
        for (const entry of tpEntries) if (originalTpKeys.has(entry[0].join(':'))) replaced++;
        for (const entry of svcEntries) if (originalSvcKeys.has(entry[0].join(':'))) replaced++;

        for (const entry of tpEntries) tpBlocks.set(entry[0].join(':'), entry[1]);
        for (const entry of svcEntries) svcBlocks.set(entry[0].join(':'), entry[1]);

        const out = ['eDVB services /4/', 'transponders'];
        for (const block of tpBlocks.values()) {
            out.push.apply(out, normalizeTpBlock(block));
            out.push('/');
        }
        out.push('end', 'services');
        for (const block of svcBlocks.values()) {
            out.push.apply(out, normalizeSvcBlock(block));
        }
        out.push('end');

        return {
            text: out.join('\n') + '\n',
            transponders: tpBlocks.size,
            services: svcBlocks.size,
            replaced: replaced,
            removedTp: removedTp,
            removedSvc: removedSvc
        };
    }

    /* ── top-level generation ──────────────────────────────── */

    function defaults() {
        return {
            url: DEFAULT_JSON_URL,
            outDir: '.',
            bouquetName: 'userbouquet.abertis.tv',
            lamedbMode: 'standalone',
            lamedbFormat: 'v4',
            position: -300,
            relayHost: 'localhost',
            relayPort: 8001,
            outHost: '0.0.0.0',
            httpPort: 9999,
            relayScript: '',
            t2miPlp: '',
            t2miPnr: '',
            softcamEnabled: false,
            softcamPath: ''
        };
    }

    function normalizeOptions(options) {
        const out = Object.assign(defaults(), options || {});
        const toInt = function (value) {
            const n = pyInt(value);
            return n === null ? null : n;
        };
        out.position = toInt(out.position);
        out.relayPort = toInt(out.relayPort);
        out.httpPort = toInt(out.httpPort);
        out.t2miPlp = str(out.t2miPlp).trim() === '' ? null : toInt(out.t2miPlp);
        out.t2miPnr = str(out.t2miPnr).trim() === '' ? null : toInt(out.t2miPnr);
        out.outDir = str(out.outDir) || '.';
        out.relayHost = str(out.relayHost);
        out.outHost = str(out.outHost);
        out.relayScript = str(out.relayScript).trim();
        out.bouquetName = str(out.bouquetName).trim() || 'userbouquet.abertis.tv';
        out.softcamPath = str(out.softcamPath).trim();
        out.lamedbMode = out.lamedbMode === 'merge' || out.lamedbMode === 'edit'
            ? out.lamedbMode : 'standalone';
        return out;
    }

    function validateOptions(options) {
        const problems = [];
        if (!options.outDir.trim()) problems.push('Output folder is empty.');
        if (options.position === null || options.position < -1800 || options.position > 1800) {
            problems.push('Orbital position must be an integer between -1800 and 1800 (tenths of a degree).');
        }
        if (options.relayPort === null || options.relayPort < 1 || options.relayPort > 65535) {
            problems.push('Streamrelay port must be a TCP port between 1 and 65535.');
        }
        if (options.httpPort === null || options.httpPort < 1 || options.httpPort > 65535) {
            problems.push('astra HTTP port must be a TCP port between 1 and 65535.');
        }
        if (!options.relayHost.trim() || !options.outHost.trim()) {
            problems.push('Relay host and astra output host must not be empty.');
        }
        if (str(options.t2miPlp).trim() !== '' && options.t2miPlp === null) {
            problems.push('T2MI PLP override must be an integer.');
        }
        if (str(options.t2miPnr).trim() !== '' && options.t2miPnr === null) {
            problems.push('T2MI program number override must be an integer.');
        }
        return problems;
    }

    function zipPath(outDir, name) {
        let dir = str(outDir).trim().replace(/\\/g, '/').replace(/^\/+/, '');
        while (dir.length > 1 && dir.endsWith('/')) dir = dir.slice(0, -1);
        if (!dir || dir === '.') return name.replace(/^\/+/, '');
        return dir + '/' + name.replace(/^\/+/, '');
    }

    function generate(input) {
        const logs = [];
        const log = function (level, message) { logs.push({ level: level, message: message }); };
        const options = normalizeOptions(input || {});
        const data = input && input.data;

        try {
            const problems = validateOptions(options);
            if (!Array.isArray(data)) {
                problems.unshift('Unexpected JSON layout - expected a list of transponders.');
            }
            if (problems.length) {
                return { ok: false, error: problems.join(' '), problems: problems, logs: logs };
            }

            const txps = parseTransponders(data, log);
            const streams = collectStreams(txps, log);
            if (!txps.length || !streams.length) {
                return {
                    ok: false,
                    error: 'No transponders/streams found in the source data.',
                    problems: ['No transponders/streams found in the source data.'],
                    logs: logs
                };
            }

            const providers = {};
            for (const stream of streams) {
                const provider = stream.txp.provider;
                providers[provider] = (providers[provider] || 0) + 1;
            }
            const dates = Array.from(new Set(data
                .filter(function (t) { return t && typeof t === 'object' && !Array.isArray(t); })
                .map(function (t) { return str(t.date).trim(); })
                .filter(Boolean))).sort();
            const totalChannels = streams.reduce(function (sum, s) { return sum + s.channels.length; }, 0);

            log('info', 'Parsed ' + txps.length + ' transponders, ' + streams.length + ' streams, ' +
                totalChannels + ' channels' + (dates.length ? ' (data date: ' + dates.join(', ') + ')' : ''));
            Object.keys(providers).sort().forEach(function (provider) {
                log('info', '  ' + provider + ': ' + providers[provider] + ' stream(s)');
            });

            const overrides = streams.filter(function (s) {
                return s.sid !== s.pid + 1000 || s.cpid !== s.pid + 5000;
            });
            if (overrides.length) {
                log('info', overrides.length + ' stream(s) with custom sid/cpid values from the JSON:');
                overrides.forEach(function (s) {
                    log('info', '  PID ' + s.pid + '  SID ' + s.sid + '  CPID ' + s.cpid);
                });
            }

            const built = buildLamedbEntries(txps, streams, options.position);
            const files = [];
            let lamedbInfo = null;

            if (options.lamedbMode === 'standalone') {
                const content = lamedbText(built.tpEntries, built.svcEntries);
                files.push({
                    name: 'lamedb',
                    path: zipPath(options.outDir, 'lamedb'),
                    content: content,
                    description: built.tpEntries.length + ' transponders, ' +
                        built.svcEntries.length + ' feed services (v4)'
                });
                log('info', 'lamedb: ' + built.tpEntries.length + ' transponders, ' +
                    built.svcEntries.length + ' feed services (standalone, v4)');
            } else {
                if (!input || typeof input.lamedbText !== 'string' || !input.lamedbText.trim()) {
                    return {
                        ok: false,
                        error: options.lamedbMode === 'merge'
                            ? 'Merge mode needs an existing lamedb file (upload one or load the site copy).'
                            : 'Edit mode needs an existing lamedb file (upload one or load the site copy).',
                        problems: ['No existing lamedb provided for ' + options.lamedbMode + ' mode.'],
                        logs: logs
                    };
                }
                const merged = mergeIntoText(input.lamedbText, built.tpEntries, built.svcEntries);
                const baseName = str(input.lamedbName).trim().split(/[\\/]/).pop() || 'lamedb';
                files.push({
                    name: baseName,
                    path: zipPath(options.outDir, baseName),
                    content: merged.text,
                    description: merged.transponders + ' transponders, ' + merged.services +
                        ' services (' + merged.replaced + ' replaced' +
                        (options.lamedbMode === 'edit'
                            ? ', ' + merged.removedTp + ' transponders removed, ' +
                              merged.removedSvc + ' abertis services removed'
                            : '') + ')'
                });
                lamedbInfo = merged;
                log('info', (options.lamedbMode === 'merge' ? 'Merged' : 'Edited') + ' ' + baseName +
                    ': ' + merged.transponders + ' transponders / ' + merged.services +
                    ' services in total (' + merged.replaced + ' replaced' +
                    (options.lamedbMode === 'edit'
                        ? ', ' + merged.removedTp + ' transponders removed, ' +
                          merged.removedSvc + ' abertis services removed'
                        : '') + ')');
            }

            files.push({
                name: 'astra.conf',
                path: zipPath(options.outDir, 'astra.conf'),
                content: astraText(streams, options, log),
                description: streams.length + ' channels'
            });
            log('info', 'astra.conf: ' + streams.length + ' channels');

            files.push({
                name: options.bouquetName,
                path: zipPath(options.outDir, options.bouquetName),
                content: bouquetText(streams, options, log),
                description: totalChannels + ' services'
            });
            log('info', options.bouquetName + ': ' + totalChannels + ' services');

            if (options.softcamEnabled) {
                const skName = options.softcamPath
                    ? options.softcamPath.split(/[\\/]/).filter(Boolean).pop()
                    : 'SoftCam.Key';
                const bissCount = streams.filter(function (s) {
                    return s.encryption.indexOf('BISS') !== -1;
                }).length;
                files.push({
                    name: skName,
                    path: zipPath(options.outDir, skName),
                    content: softcamText(streams),
                    description: bissCount + ' dummy BISS entries'
                });
                log('info', skName + ': ' + bissCount + ' dummy BISS entries');
            }

            return {
                ok: true,
                logs: logs,
                options: options,
                stats: {
                    transponders: txps.length,
                    streams: streams.length,
                    channels: totalChannels,
                    tv: streams.reduce(function (sum, s) {
                        return sum + s.channels.filter(function (c) { return !c.radio; }).length;
                    }, 0),
                    radio: streams.reduce(function (sum, s) {
                        return sum + s.channels.filter(function (c) { return c.radio; }).length;
                    }, 0),
                    providers: Object.keys(providers).length,
                    files: files.length
                },
                files: files,
                lamedbInfo: lamedbInfo
            };
        } catch (error) {
            return {
                ok: false,
                error: error && error.message ? error.message : String(error),
                problems: [error && error.message ? error.message : String(error)],
                logs: logs
            };
        }
    }

    global.DXSuite = {
        DEFAULT_JSON_URL: DEFAULT_JSON_URL,
        defaults: defaults,
        generate: generate,
        namespaceFor: namespaceFor,
        positionLabel: positionLabel,
        zipPath: zipPath,
        // exported for tests / debugging
        _internals: {
            crc32: crc32,
            softcamKeyId: softcamKeyId,
            parseTransponders: parseTransponders,
            collectStreams: collectStreams,
            buildLamedbEntries: buildLamedbEntries,
            lamedbText: lamedbText,
            astraText: astraText,
            bouquetText: bouquetText,
            softcamText: softcamText,
            mergeIntoText: mergeIntoText
        }
    };
})(typeof window !== 'undefined' ? window : globalThis);
