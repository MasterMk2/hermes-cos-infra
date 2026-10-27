#!/usr/bin/env bash
# hermes-timeout-diag.sh -- READ-ONLY evidence collection for the Discord DM
# timeouts ("応答に時間がかかりすぎたため中断しました") and the attention alert
# "gateway: Hermes が応答を返せていません".
#
#   sudo bash hermes-timeout-diag.sh [YYYY-MM-DD] [HH:MM-HH:MM]
#        defaults: today (Asia/Tokyo)  12:50-15:40 (Asia/Tokyo)
#   env: HIST_DAYS=21     history depth for trend tables
#        SKIP_RUN_API=1   do not GET /v1/runs/<id> from Hermes (that step reads
#                         the gateway's Hermes API key as root and never prints it)
#        OUTBASE=/tmp     where the report + a 0700 temp dir are written
#
# What it never does: restart/stop/reload anything, docker exec, write under
# /srv or /etc, print a credential, print a message body, prompt or answer.
# Free-text log lines from Hermes/bridge are keyword-filtered, truncated, have
# Japanese runs replaced by <ja:N>, and are withheld entirely when they carry a
# content/input/prompt marker. Discord ids, LINE ids, private IPs and long
# tokens are masked in the final output. Review the report before sharing it.
#
# Hypotheses each section tests:
#   H1 provider chain latency: bridge CLI timeout (120s) + Hermes retry/fallback
#      (+ premium Fable/Astra tier) exceeds the gateway's 240s budget
#   H2 session/context growth: one Hermes session per conversation forever
#      (gw-discord-*), prompt grows, compression, bridge argv >128KiB (E2BIG)
#   H3 abandoned runs: the gateway never cancels a timed-out run; the next
#      message lands on the same session while the old run may still be active
#   H4 USB storage I/O stall on /srv/hermes (Hermes data, Docker data-root,
#      gateway DB all on it)
#   H5 host pressure: memory/swap/OOM, CPU, thermal/undervoltage, orphan CLIs
#   H6 deployed code/config differs from the repo snapshot (premium routing
#      branch, different timeouts/concurrency)
#   H7 monitoring artifact: the 14:20 "復旧" is the 3600s hermes_failing window
#      expiring, not a Hermes success
set -uo pipefail
umask 077
export LC_ALL=C.UTF-8 2>/dev/null || true

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root: sudo bash $0 [YYYY-MM-DD] [HH:MM-HH:MM]" >&2
  exit 1
fi

DAY="${1:-$(TZ=Asia/Tokyo date +%F)}"
WIN="${2:-12:50-15:40}"
HIST_DAYS="${HIST_DAYS:-21}"
OUTBASE="${OUTBASE:-/tmp}"

FROM_EP=$(date -d "TZ=\"Asia/Tokyo\" $DAY ${WIN%-*}" +%s) || { echo "bad date/window" >&2; exit 1; }
TO_EP=$(date -d "TZ=\"Asia/Tokyo\" $DAY ${WIN#*-}" +%s) || { echo "bad date/window" >&2; exit 1; }
NOW_EP=$(date +%s)
HIST_EP=$(( NOW_EP - HIST_DAYS * 86400 ))
WEEK_EP=$(( NOW_EP - 7 * 86400 ))
J_FROM=$(date -d "@$FROM_EP" '+%F %T')      # journalctl takes host-local time
J_TO=$(date -d "@$TO_EP" '+%F %T')
J_HIST=$(date -d "@$HIST_EP" '+%F %T')
J_WEEK=$(date -d "@$WEEK_EP" '+%F %T')
FROM_UTC=$(date -u -d "@$FROM_EP" +%FT%T)  # gateway/attention DBs store UTC ISO
TO_UTC=$(date -u -d "@$TO_EP" +%FT%T)
D_FROM=$(date -u -d "@$FROM_EP" +%FT%TZ)   # docker --since/--until
D_TO=$(date -u -d "@$TO_EP" +%FT%TZ)

# ---- paths and units, all taken from hermes-cos-infra / hermes-pi-setup ----
GW=/srv/hermes/channel-gateway
GWPY=$GW/venv/bin/python
GWDB=$GW/state/gateway.db
GWCONF=$GW/config/config.yaml
GWLOGS=$GW/logs
SOCK=$GW/run/proactive.sock
AG=/srv/hermes/antigravity
AGCONF=$AG/config/bridge.yaml
ATT=/srv/hermes/attention
ATTPY=$ATT/venv/bin/python
ATTCONF=$ATT/config/config.yaml
HD=/srv/hermes/hermes-data
HSESS=$HD/sessions
HCONF=$HD/config.yaml
HLOGS=$HD/logs
BRIDGE_HEALTH=http://172.18.0.1:8765/health
KNOWN_UNITS="channel-gateway hermes-antigravity attention-engine hermes-classifier-broker"

PY="$GWPY"; [ -x "$PY" ] || PY="$(command -v python3)"
have() { command -v "$1" >/dev/null 2>&1; }

mkdir -p "$OUTBASE"
TMPD=$(mktemp -d "$OUTBASE/hermes-diag.XXXXXX") || exit 1
trap 'rm -rf "$TMPD"' EXIT
OUT="$OUTBASE/hermes-diag-$(date +%Y%m%d-%H%M%S).txt"
TL="$TMPD/timeline.tsv"; : > "$TL"

# --------------------------------------------------------------- helper.py
cat > "$TMPD/h.py" <<'PYEOF'
"""Helpers for hermes-timeout-diag.sh. stdin -> stdout. No writes, no network
except `runapi` (GET /v1/runs/<id>) and only when the shell asks for it."""
import json, re, sys, time
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
CJK = re.compile(r'[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]+')
CONTENT = re.compile(
    r"""['"](content|input|output|text|prompt|instructions|messages|body|query|arguments|args)['"]\s*:"""
    r"""|\b(content|input|output|prompt|instructions|text|body|query|arguments)\s*="""
    r"""|\b(User|Assistant|Human|System)\s*:|\[Tool Result|\[System Instruction""", re.I)
STDERR = re.compile(r'(process failed with code -?\d+):.*')

def clean(s, limit=220):
    s = ANSI.sub('', s).replace('\t', ' ').strip()
    s = STDERR.sub(r'\1: <cli stderr/stdout elided>', s)
    s = CJK.sub(lambda m: f'<ja:{len(m.group(0))}>', s)
    m = CONTENT.search(s)
    if m:
        return f"{s[:min(m.start(), 90)]} [rest withheld: may carry message content; {len(s)} chars]"
    return s[:limit] + ('...' if len(s) > limit else '')

SNOW = re.compile(r'(?<![\d.])\d{15,25}(?![\d.])')
LINEU = re.compile(r'\bU[0-9a-f]{32}\b')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
BEARER = re.compile(r'(?i)(bearer\s+)\S+')
KEYISH = re.compile(r'\b(sk|ghp|gho|github_pat|xox[abp]|AIza)[-_A-Za-z0-9]{10,}')
LONG = re.compile(r'(?<![A-Za-z0-9_\-/])[A-Za-z0-9_\-]{40,}(?![A-Za-z0-9_\-])')
IPV4 = re.compile(r'\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b')

def _ip(m):
    a, b, c, _ = m.groups()
    if a in ('127', '0') or (a, b, c) == ('172', '18', '0'):
        return m.group(0)
    return f'{a}.{b}.x.x'

def mask(s):
    s = BEARER.sub(r'\1<redacted>', s)
    s = KEYISH.sub('<key>', s)
    s = LINEU.sub(lambda m: 'U..' + m.group(0)[-4:], s)
    s = SNOW.sub(lambda m: '..' + m.group(0)[-4:], s)
    s = EMAIL.sub('<email>', s)
    s = LONG.sub(lambda m: f'<tok{len(m.group(0))}>', s)
    return IPV4.sub(_ip, s)

CATS = [
    ('timeout', r'time[ds]?[ _-]?out|timed out|deadline'),
    ('fallback', r'fall(ing)?[ _-]?back|fail[ -]?over|switch(ing)? (to )?(provider|model)'),
    ('retry', r'\bretr(y|ying|ies)\b|backoff'),
    ('compress', r'compress|compact|summari[sz]'),
    ('context', r'context.?(length|window)|too many tokens|token limit'),
    ('ratelimit', r'rate.?limit|\b429\b|quota|resource.?exhausted'),
    ('http5xx', r'\b50[0-4]\b|bad gateway|service unavailable'),
    ('error', r'\berror\b|exception|traceback'),
    ('e2big', r'argument list too long|errno 7\b|e2big|prompt too large'),
    ('cli_call', r'cli call model='),
    ('cli_timeout', r'cli timed out after'),
    ('cli_failed', r'antigravity cli failed'),
    ('req', r'"post /v1/chat/completions'),
    ('req_ok', r'"post /v1/chat/completions[^"]*" 200'),
    ('req_err', r'"post /v1/chat/completions[^"]*" (4\d\d|5\d\d)'),
    ('delegate', r'delegating tool request'),
    ('mcp', r'\bmcp\b'),
    ('cancel', r'cancel|interrupt'),
    ('lock', r'\block(ed)?\b|\bbusy\b|already running|concurrent'),
    ('premium', r'fable|astra|claude|codex|premium'),
    ('usb_reset', r'usb .*reset|reset (high|super)-speed'),
    ('io_error', r'i/o error|blk_update_request|buffer i/o'),
    ('blocked', r'blocked for more than|hung_task'),
    ('undervolt', r'under-?voltage'),
    ('oom', r'out of memory|oom|killed process'),
    ('gw_processing', r'gateway processing gw_'),
    ('gw_timeout_reply', r'delivered gw_\S+ .*chars=42\b'),
    ('gw_unavailable', r'hermes unavailable for'),
    ('gw_rejected', r'hermes rejected'),
    ('gw_recovered', r'recovered stale'),
    ('gw_run_end', r'hermes run \S+ for gw_\S+ ended'),
    ('gw_deadline', r'at the deadline'),
    ('gw_status_unreadable', r'hermes status unreadable|no longer knew the run|lost run for'),
    ('hb_blocked', r'heartbeat blocked'),
]
CATS = [(n, re.compile(p, re.I)) for n, p in CATS]

def cats(s):
    return ','.join(n for n, rx in CATS if rx.search(s))

J_RE = re.compile(r'^(\d{9,11}\.\d+)\s+\S+\s+(.*)$')          # journalctl -o short-unix
D_RE = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z\s?(.*)$')  # docker logs -t

def parse_line(line):
    m = J_RE.match(line)
    if m:
        return float(m.group(1)), m.group(2)
    m = D_RE.match(line)
    if m:
        d = datetime.strptime(m.group(1), '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
        return d.timestamp() + (float(m.group(2)) if m.group(2) else 0.0), m.group(3)
    return None, line

def cmd_tsv(src, kw=''):
    rx = re.compile(kw, re.I) if kw else None
    for line in sys.stdin:
        ep, msg = parse_line(line.rstrip('\n'))
        if ep is None or (rx and not rx.search(msg)):
            continue
        print(f"{ep:.3f}\t{src}\t{clean(msg)}\t{cats(msg)}")

def _rows(frm, to):
    for line in sys.stdin:
        p = line.rstrip('\n').split('\t')
        if len(p) < 3:
            continue
        try:
            ep = float(p[0])
        except ValueError:
            continue
        if frm <= ep <= to:
            yield ep, p[1], p[2], (p[3] if len(p) > 3 else '')

def cmd_timeline(frm, to, maxper=400):
    rows = sorted(_rows(frm, to))
    per, dropped = {}, {}
    for ep, src, text, _ in rows:
        per[src] = per.get(src, 0) + 1
        if per[src] > maxper:
            dropped[src] = dropped.get(src, 0) + 1
            continue
        t = datetime.fromtimestamp(ep, JST).strftime('%m-%d %H:%M:%S')
        print(f"{t} {src:<3} {text}")
    for s, n in dropped.items():
        print(f"(timeline: {n} more {s} lines not shown)")
    if not rows:
        print("(no events in window)")

def cmd_counts(frm, to, bucket):
    agg = {}
    for ep, src, _, c in _rows(frm, to):
        if bucket >= 86400:
            k = datetime.fromtimestamp(ep, JST).strftime('%Y-%m-%d')
        else:
            b = int(ep // bucket * bucket)
            k = datetime.fromtimestamp(b, JST).strftime('%m-%d %H:%M')
        d = agg.setdefault((k, src), {'lines': 0})
        d['lines'] += 1
        for n in filter(None, c.split(',')):
            d[n] = d.get(n, 0) + 1
    if not agg:
        print("   (none)")
    for (k, src), d in sorted(agg.items()):
        print(f"   {k} {src:<3} " + ' '.join(f"{n}={v}" for n, v in d.items()))

def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else None

GW_LINE = re.compile(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})\s+\w+\s+(\S+)\s+(.*)$')
PROC = re.compile(r'^processing (gw_[0-9a-f]+) platform=(\w+)')
DELIV = re.compile(r'^delivered (gw_[0-9a-f]+) platform=(\w+) mode=(\S+) ok=(\w+) detail=(\S*) chars=(\d+)')
UNAV = re.compile(r'^hermes (unavailable for|rejected) (gw_[0-9a-f]+): (.*)$')
# logged only by the durable-wait fix: how long Hermes itself took, and how big the turn was
RUN_END = re.compile(r'^hermes run \S+ for (gw_[0-9a-f]+) ended status=(\S+) elapsed=(\S+?)s in=(\S+) out=(\S+)')
ANOM = re.compile(r'unavailable|rejected|recovered stale|failed|traceback|heartbeat blocked|'
                  r'shutting down|bot starting|bot ready|listening|could not record|ERROR|WARNING|Exception', re.I)
CLASS = {42: 'TIMEOUT', 26: 'UNAVAILABLE', 29: 'INTERNAL', 0: 'RESUME/none'}

def cmd_gwparse(frm, to):
    """Gateway journal (short-unix) and gateway.log* lines -> per-request durations."""
    seen, events, anomalies = set(), [], []
    for line in sys.stdin:
        line = line.rstrip('\n')
        ep, msg = parse_line(line)
        m = GW_LINE.search(msg)
        if not m:
            continue
        if ep is None:   # gateway.log* file line: host-local asctime
            ep = time.mktime(time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')) + int(m.group(2)) / 1000
        body = m.group(4)
        key = (body[:80], int(ep))
        if key in seen or (body[:80], int(ep) - 1) in seen or (body[:80], int(ep) + 1) in seen:
            continue
        seen.add(key)
        events.append((ep, m.group(3), body))
        if frm <= ep <= to and ANOM.search(body) and not DELIV.search(body):
            anomalies.append((ep, clean(body)))
    events.sort()
    proc, reqs, runs = {}, [], []
    for ep, _, body in events:
        e = RUN_END.search(body)
        if e:
            runs.append((ep,) + e.groups())
            continue
        p = PROC.search(body)
        if p:
            proc[p.group(1)] = ep
            continue
        d = DELIV.search(body)
        if d:
            rid, plat, mode, ok, detail, chars = d.groups()
            st = proc.get(rid)
            reqs.append(dict(rid=rid, plat=plat, mode=mode, ok=ok, detail=detail, chars=int(chars),
                             cls=CLASS.get(int(chars), 'ANSWER?'), start=st, end=ep,
                             dur=(ep - st) if st else None))
            continue
        u = UNAV.search(body)
        if u:
            reqs.append(dict(rid=u.group(2), plat='?', mode='-', ok='-', detail=clean(u.group(3), 60),
                             chars=-1, cls='HERMES_' + u.group(1).split()[0].upper(),
                             start=proc.get(u.group(2)), end=ep, dur=None))
    if events:
        print(f"   log coverage: {datetime.fromtimestamp(events[0][0], JST):%Y-%m-%d %H:%M} .. "
              f"{datetime.fromtimestamp(events[-1][0], JST):%Y-%m-%d %H:%M} JST, {len(events)} lines")
    else:
        print("   (no gateway log lines found)")
    print("\n   -- requests in window (processing -> delivered; chars=42 is the fixed timeout text)")
    print(f"   {'processing':<15} {'delivered':<15} {'dur_s':>7} {'class':<14} {'plat':<8} ok    detail  rid")
    def jt(e): return datetime.fromtimestamp(e, JST).strftime('%m-%d %H:%M:%S') if e else '-'
    shown = 0
    for r in reqs:
        if frm <= (r['start'] or r['end']) <= to or frm <= r['end'] <= to:
            shown += 1
            dur = f"{r['dur']:.1f}" if r['dur'] is not None else '-'
            print(f"   {jt(r['start']):<15} {jt(r['end']):<15} {dur:>7} {r['cls']:<14} {r['plat']:<8} "
                  f"{r['ok']:<5} {r['detail']}  {r['rid']}")
    done = {r['rid'] for r in reqs}
    for rid, st in sorted(proc.items(), key=lambda kv: kv[1]):
        if rid not in done and frm <= st <= to:
            shown += 1
            print(f"   {jt(st):<15} {'(never)':<15} {'-':>7} {'NO_DELIVERY':<14} {'-':<8} -     -  {rid}")
    if not shown:
        print("   (none)")
    print("\n   -- anomaly lines in window")
    for ep, b in anomalies[:200]:
        print(f"   {jt(ep)} {b}")
    if not anomalies:
        print("   (none)")
    def num(v):
        try:
            return float(v)
        except ValueError:
            return None
    print("\n   -- Hermes runs ended in window (durable-wait fix only; in/out = Hermes usage tokens)")
    shown = 0
    for ep, rid, status, el, tin, tout in runs:
        if frm <= ep <= to:
            shown += 1
            print(f"   {jt(ep):<15} {status:<12} elapsed_s={el:<8} in={tin:<8} out={tout:<6} {rid}")
    if not shown:
        print("   (none: the deployed gateway predates the fix, or nothing finished in the window)")
    rdays = {}
    for ep, rid, status, el, tin, tout in runs:
        d = rdays.setdefault(datetime.fromtimestamp(ep, JST).strftime('%Y-%m-%d'), {'el': [], 'in': [], 'st': {}})
        if num(el) is not None: d['el'].append(num(el))
        if num(tin) is not None: d['in'].append(num(tin))
        d['st'][status] = d['st'].get(status, 0) + 1
    if rdays:
        print(f"   {'day':<10} {'runs':>4} {'el_p50':>7} {'el_p90':>7} {'el_max':>7} {'in_p50':>8} {'in_max':>8}  statuses")
        g = lambda v: f"{v:.0f}" if v is not None else '-'
        for k in sorted(rdays):
            d = rdays[k]
            print(f"   {k:<10} {sum(d['st'].values()):>4} {g(pct(d['el'], .5)):>7} {g(pct(d['el'], .9)):>7} "
                  f"{g(max(d['el']) if d['el'] else None):>7} {g(pct(d['in'], .5)):>8} "
                  f"{g(max(d['in']) if d['in'] else None):>8}  "
                  + ' '.join(f"{c}={n}" for c, n in sorted(d['st'].items())))
    print("\n   -- per day (JST): counts by reply class; durations processing->delivered")
    days = {}
    for r in reqs:
        if r['chars'] < 0 and r['cls'].startswith('HERMES_'):
            k = datetime.fromtimestamp(r['end'], JST).strftime('%Y-%m-%d')
            # Two statements: in `a[k] = days[k]...` the right side runs first,
            # before setdefault has created days[k] (KeyError on the Pi).
            d = days.setdefault(k, {'n': 0, 'cls': {}, 'ans': [], 'all': []})
            d['cls'][r['cls']] = d['cls'].get(r['cls'], 0) + 1
            continue
        k = datetime.fromtimestamp(r['end'], JST).strftime('%Y-%m-%d')
        d = days.setdefault(k, {'n': 0, 'cls': {}, 'ans': [], 'all': []})
        d['n'] += 1
        d['cls'][r['cls']] = d['cls'].get(r['cls'], 0) + 1
        if r['dur'] is not None:
            d['all'].append(r['dur'])
            if r['cls'] == 'ANSWER?':
                d['ans'].append(r['dur'])
    print(f"   {'day':<10} {'n':>4} {'ans_p50':>8} {'ans_p90':>8} {'ans_max':>8} {'all_max':>8}  classes")
    for k in sorted(days):
        d = days[k]
        f = lambda v: f"{v:.0f}" if v is not None else '-'
        print(f"   {k:<10} {d['n']:>4} {f(pct(d['ans'], .5)):>8} {f(pct(d['ans'], .9)):>8} "
              f"{f(max(d['ans']) if d['ans'] else None):>8} {f(max(d['all']) if d['all'] else None):>8}  "
              + ' '.join(f"{c}={n}" for c, n in sorted(d['cls'].items())))

BR_CALL = re.compile(r'cli call model=(\S+) status=(\d+) outcome=(\S+) duration=([\d.]+)s prompt_bytes=(\d+)')
def cmd_bridgecalls(frm, to):
    """Bridge per-call lines (logged only by the observability change)."""
    win, days = [], {}
    for line in sys.stdin:
        ep, msg = parse_line(line.rstrip('\n'))
        m = BR_CALL.search(msg) if ep is not None else None
        if not m:
            continue
        model, status, outcome, dur, size = m.group(1), int(m.group(2)), m.group(3), float(m.group(4)), int(m.group(5))
        d = days.setdefault(datetime.fromtimestamp(ep, JST).strftime('%Y-%m-%d'), {'dur': [], 'size': [], 'out': {}})
        d['dur'].append(dur); d['size'].append(size); d['out'][outcome] = d['out'].get(outcome, 0) + 1
        if frm <= ep <= to:
            win.append((ep, model, status, outcome, dur, size))
    if not days:
        print("   (no 'cli call' lines: the deployed bridge predates the per-call log)")
        return
    f = lambda v: f"{v:.0f}" if v is not None else '-'
    print(f"   {'day':<10} {'calls':>5} {'dur_p50':>7} {'dur_p90':>7} {'dur_max':>7} {'bytes_p50':>9} {'bytes_max':>9}  outcomes")
    for k in sorted(days):
        d = days[k]
        print(f"   {k:<10} {len(d['dur']):>5} {f(pct(d['dur'], .5)):>7} {f(pct(d['dur'], .9)):>7} {f(max(d['dur'])):>7} "
              f"{f(pct(d['size'], .5)):>9} {f(max(d['size'])):>9}  " + ' '.join(f"{o}={n}" for o, n in sorted(d['out'].items())))
    print("   -- in window (several calls inside one Hermes turn = retries/fallback/tool rounds; sizes near 131072 = E2BIG risk)")
    for ep, model, status, outcome, dur, size in win[:150]:
        print(f"   {datetime.fromtimestamp(ep, JST):%H:%M:%S} {status} {outcome:<16} {dur:>6.1f}s {size:>8} B  {model}")
    if not win:
        print("   (none)")

CLS_RE = re.compile(r'Classified \S+ in (\d+(?:\.\d+)?)s')
def cmd_classifier(frm, to):
    win, days = [], {}
    for line in sys.stdin:
        ep, msg = parse_line(line.rstrip('\n'))
        if ep is None:
            continue
        m = CLS_RE.search(msg)
        err = 'Classification error' in msg or 'Bridge HTTP' in msg or 'timed out' in msg.lower()
        k = datetime.fromtimestamp(ep, JST).strftime('%Y-%m-%d')
        d = days.setdefault(k, {'lat': [], 'err': 0})
        if m:
            d['lat'].append(float(m.group(1)))
        if err:
            d['err'] += 1
        if frm <= ep <= to and (m or err):
            win.append((ep, float(m.group(1)) if m else None, err))
    print(f"   {'day':<10} {'n':>5} {'p50_s':>6} {'p90_s':>6} {'max_s':>6} errors")
    for k in sorted(days):
        d = days[k]
        f = lambda v: f"{v:.1f}" if v is not None else '-'
        print(f"   {k:<10} {len(d['lat']):>5} {f(pct(d['lat'], .5)):>6} {f(pct(d['lat'], .9)):>6} "
              f"{f(max(d['lat']) if d['lat'] else None):>6} {d['err']}")
    if not days:
        print("   (no classifier-broker lines)")
    print("   -- in window")
    for ep, lat, err in win[:120]:
        t = datetime.fromtimestamp(ep, JST).strftime('%H:%M:%S')
        print(f"   {t} " + (f"latency={lat:.1f}s" if lat is not None else "") + (" ERROR" if err else ""))
    if not win:
        print("   (none)")

def cmd_filelog(frm, to, kw):
    """Hermes file logs: timezone of the leading timestamp is unknown (container),
    so a line is kept if it falls in the window read as UTC or as JST."""
    rx = re.compile(kw, re.I)
    ts = re.compile(r'^(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)')
    n = 0
    for line in sys.stdin:
        line = line.rstrip('\n')
        m = ts.match(line)
        if not m or not rx.search(line):
            continue
        base = datetime.strptime(f"{m.group(1)} {m.group(2)}", '%Y-%m-%d %H:%M:%S')
        e_utc = base.replace(tzinfo=timezone.utc).timestamp()
        e_jst = base.replace(tzinfo=JST).timestamp()
        tag = 'asUTC' if frm <= e_utc <= to else ('asJST' if frm <= e_jst <= to else None)
        if not tag:
            continue
        n += 1
        if n <= 150:
            print(f"   [{tag}] {clean(line)}")
    print(f"   ({n} matching lines in window)")

def cmd_bridgehealth():
    try:
        d = json.load(sys.stdin)
    except Exception:
        print("   bridge /health: no JSON"); return
    m = d.get('metrics') or {}
    le = m.get('last_error')
    print(f"   status={d.get('status')} model={d.get('configured_model')} authenticated={d.get('authenticated')} "
          f"tool_calling={d.get('tool_calling_support')} uptime_s={d.get('uptime_seconds')}")
    print(f"   total={m.get('total_requests')} ok={m.get('successful_requests')} failed={m.get('failed_requests')} "
          f"last_latency_s={m.get('last_latency_seconds')} "
          f"last_error_class={(str(le).split(':')[0][:70]) if le else None}")

SENS = re.compile(r'key|token|secret|passw|auth|credential|cookie|bearer|webhook', re.I)

def _walk(o, path, want, out):
    if isinstance(o, dict):
        for k, v in o.items():
            p = f"{path}.{k}" if path else str(k)
            if SENS.search(str(k)):
                out.append(f"{p} = <{'set' if v else 'empty'}; not shown>")
                continue
            if EXCL.search(str(k)):
                out.append(f"{p} = <free text; not shown>")
                continue
            _walk(v, p, want, out)
    elif isinstance(o, list):
        if o and all(not isinstance(x, (dict, list)) for x in o) and want.search(path):
            out.append(f"{path} = {clean(str(o), 160)}")
        else:
            for i, v in enumerate(o[:30]):
                _walk(v, f"{path}[{i}]", want, out)
    elif want.search(path):
        s = str(o)
        if re.search(r'[A-Za-z0-9_\-]{32,}', s) and not re.search(r'[/.:]', s):
            s = '<long token-like value not shown>'
        out.append(f"{path} = {clean(s, 120)}")

def _load_yaml(path):
    try:
        import yaml
    except ImportError:
        print("   (PyYAML not available to this python)"); return None
    try:
        with open(path, encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        print(f"   cannot read {path}: {type(exc).__name__}"); return None
    if not isinstance(data, dict):
        print(f"   {path}: top level is not a mapping"); return None
    return data

EXCL = re.compile(r'prompt|personality|persona|instruction|soul|memory|profile|bio|note|description', re.I)

def cmd_yamlpick(path, mode):
    data = _load_yaml(path)
    if data is None:
        return
    if mode == 'gw':
        h = data.get('hermes') or {}
        base = str(h.get('base_url', '(default http://127.0.0.1:8642)'))
        print(f"   hermes.base_url = {base}")
        for k in ('model', 'soft_timeout_seconds', 'absolute_timeout_seconds', 'poll_interval_seconds',
                  'poll_request_timeout_seconds', 'heartbeat_seconds', 'unavailable_grace_seconds',
                  'session_prefix'):
            print(f"   hermes.{k} = {h.get(k, '(default)')}")
        if str(h.get('absolute_timeout_seconds')) == '240':
            print("   NOTE absolute_timeout_seconds is pinned to the old 240 here: deploy.sh never touches"
                  " config.yaml, so after the durable-wait fix this must be raised by hand (sample: 480)")
        for k, v in (data.get('limits') or {}).items():
            print(f"   limits.{k} = {v}")
        for sec in ('discord', 'line', 'proactive'):
            print(f"   {sec}.enabled = {(data.get(sec) or {}).get('enabled')}")
        print("   (code defaults: 597c897 absolute_timeout_seconds=240; durable-wait fix 480, poll_request_timeout"
              "_seconds=10 heartbeat_seconds=60 unavailable_grace_seconds=90; both poll_interval_seconds=2"
              " max_active_requests=2)")
        return
    if mode == 'bridge':
        for k in ('host', 'port', 'binary_path', 'default_model', 'timeout_seconds', 'allow_tool_emulation',
                  'allow_delegated_mcp', 'supported_models'):
            print(f"   {k} = {data.get(k, '(default)')}")
        print(f"   auth_token inline = {'yes (not shown)' if data.get('auth_token') else 'no'}")
        print("   (repo default timeout_seconds=120)")
        return
    if mode == 'att':
        h = data.get('health') or {}
        print(f"   health.sustained_seconds.gateway = {(h.get('sustained_seconds') or {}).get('gateway', '(default 900)')}")
        print(f"   health.queue_stuck_seconds = {h.get('queue_stuck_seconds', '(default 900)')}")
        print(f"   schedule.scan_interval_seconds = {(data.get('schedule') or {}).get('scan_interval_seconds', '(default 300)')}")
        print(f"   sources.hermes_health_url = {(data.get('sources') or {}).get('hermes_health_url', '(default empty)')!r}")
        return
    want = re.compile(r'model|provider|fallback|timeout|retr|compress|threshold|context|turn|reason|effort|'
                      r'base_url|rout|smart|delegat|api_mode|stream|session|reset|idle|summar|auxiliar|'
                      r'premium|fable|astra|claude|codex|max_tokens|concurren|parallel', re.I)
    out = []
    for k, v in data.items():
        if str(k) in ('mcp_servers',):
            names = list(v.keys()) if isinstance(v, dict) else v
            out.append(f"mcp_servers = {clean(str(names), 200)}")
            continue
        _walk(v, str(k), want, out)
    for line in out[:250]:
        print("   " + line)
    if not out:
        print("   (no matching keys)")

def cmd_yamlget(path, dotted):
    try:
        import yaml
        with open(path, encoding='utf-8') as f:
            o = yaml.safe_load(f) or {}
        for p in dotted.split('.'):
            o = o.get(p) if isinstance(o, dict) else None
        print(o if o is not None else '')
    except Exception:
        print('')

def cmd_runapi(conf, ids):
    """GET /v1/runs/<id>, print status/usage/timestamps only. Never output/input."""
    import urllib.request, urllib.error
    cfg = _load_yaml(conf)
    if cfg is None:
        return
    base = str((cfg.get('hermes') or {}).get('base_url') or 'http://127.0.0.1:8642').rstrip('/')
    keyfile = (((cfg.get('secrets') or {}).get('hermes_api_key') or {}).get('fallback_file')
               or '/etc/hermes-secrets/channel-gateway-hermes-key')
    try:
        with open(keyfile, encoding='utf-8') as f:
            key = f.read().strip()
    except Exception as exc:
        print(f"   cannot read the gateway's Hermes key ({type(exc).__name__}); skipped"); return
    keep = ('status', 'model', 'created_at', 'started_at', 'completed_at', 'finished_at', 'updated_at',
            'ended_at', 'duration', 'duration_ms', 'duration_seconds', 'elapsed', 'replayed')
    for rid in ids:
        if not re.fullmatch(r'[A-Za-z0-9_\-]{1,100}', rid):
            print(f"   {rid[:20]}: skipped (unexpected id shape)"); continue
        req = urllib.request.Request(f"{base}/v1/runs/{rid}", headers={'Authorization': 'Bearer ' + key})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                code, body = r.status, json.loads(r.read(4_000_000) or b'{}')
        except urllib.error.HTTPError as exc:
            code, body = exc.code, {}
        except Exception as exc:
            print(f"   {rid}: {type(exc).__name__}"); continue
        out = {k: body.get(k) for k in keep if k in body}
        if isinstance(body.get('usage'), dict):
            out['usage'] = {k: v for k, v in body['usage'].items() if isinstance(v, (int, float))}
        err = body.get('error')
        if err:
            out['error_type'] = (err.get('type') or err.get('code')) if isinstance(err, dict) else type(err).__name__
        out['output_chars'] = len(body.get('output') or '') if isinstance(body.get('output'), str) else None
        print(f"   {rid}: http={code} {json.dumps(out, ensure_ascii=True)} keys={sorted(body.keys())}")

def main():
    a = sys.argv[1:]
    c = a[0]
    if c == 'mask':
        for line in sys.stdin:
            sys.stdout.write(mask(line))
    elif c == 'clean':
        for line in sys.stdin:
            print(clean(line.rstrip('\n')))
    elif c == 'tsv':
        cmd_tsv(a[1], a[2] if len(a) > 2 else '')
    elif c == 'timeline':
        cmd_timeline(float(a[1]), float(a[2]))
    elif c == 'counts':
        cmd_counts(float(a[1]), float(a[2]), int(a[3]))
    elif c == 'gwparse':
        cmd_gwparse(float(a[1]), float(a[2]))
    elif c == 'bridgecalls':
        cmd_bridgecalls(float(a[1]), float(a[2]))
    elif c == 'classifier':
        cmd_classifier(float(a[1]), float(a[2]))
    elif c == 'filelog':
        cmd_filelog(float(a[1]), float(a[2]), a[3])
    elif c == 'bridgehealth':
        cmd_bridgehealth()
    elif c == 'yamlpick':
        cmd_yamlpick(a[1], a[2])
    elif c == 'yamlget':
        cmd_yamlget(a[1], a[2])
    elif c == 'runapi':
        cmd_runapi(a[1], a[2:])
    elif c == 'jsonflat':
        try:
            d = json.load(sys.stdin)
            for k, v in (d.items() if isinstance(d, dict) else []):
                print(f"   {k} = {clean(str(v), 160)}")
        except Exception:
            print("   (no JSON)")

if __name__ == '__main__':
    main()
PYEOF
H() { "$PY" -I "$TMPD/h.py" "$@"; }

sec() { printf '\n################ %s\n' "$*"; }
sub() { printf -- '-- %s\n' "$*"; }
ind() { sed 's/^/   /'; }

# ---------------------------------------------------------------- the report
main() {
sec "S0 context"
echo "   window: $DAY ${WIN} Asia/Tokyo  = host-local $J_FROM .. $J_TO  = UTC $FROM_UTC .. $TO_UTC"
echo "   host tz: $(timedatectl show -p Timezone --value 2>/dev/null || date +%Z)  now: $(date '+%F %T %Z')"
echo "   kernel: $(uname -r)  up since: $(uptime -s 2>/dev/null)  load: $(cut -d' ' -f1-3 /proc/loadavg)"
echo "   hung_task_timeout_secs: $(cat /proc/sys/kernel/hung_task_timeout_secs 2>/dev/null || echo 'n/a (kernel cannot report blocked tasks)')"
sub "boots (a reboot inside the window changes everything)"
journalctl --list-boots --no-pager 2>/dev/null | tail -4 | ind
echo "   journal: $(journalctl --disk-usage 2>/dev/null)"

sec "S1 [H6] deployed code/config vs repo snapshot hermes-cos-infra@597c897"
sub "sha256 (first 12) of deployed files vs 597c897, the code the 2026-10-27 timeouts ran on"
echo "   (once the durable-wait fix is deployed the gateway files show DIFF here; the checkout table below is then the one to read)"
while read -r want rel dest; do
  if [ -f "$dest" ]; then
    got=$(sha256sum "$dest" | cut -c1-64)
    st=$([ "$got" = "$want" ] && echo MATCH || echo DIFF)
    printf '   %-5s %-28s %s  mtime=%s\n' "$st" "$rel" "${got:0:12}" "$(date -r "$dest" '+%F %T')"
  else
    printf '   %-5s %-28s (not present at %s)\n' MISS "$rel" "$dest"
  fi
done <<'EOF'
1a4d7936fb450cd000bb8cc590a926fbd5812286fc1aa2a0062c45ce06efc003 gw_canonical.py /srv/hermes/channel-gateway/app/gw_canonical.py
ccc0fe8ae6c31895e70febc68a9614463497cfc031f0d829d913c10c8320a5a1 gw_config.py /srv/hermes/channel-gateway/app/gw_config.py
fcb36397e55681a976c395a2d579afad48db4b0b809284204f6ae06b7954110d gw_discord.py /srv/hermes/channel-gateway/app/gw_discord.py
aad859e4e47c0ea8394851ca8248dddf86d14529ca58329002220fd4589b576b gw_format.py /srv/hermes/channel-gateway/app/gw_format.py
c15a3975a0f011104fc349b764f5fa2ebb63e6c829d77ddd0a146851c5e32fd1 gw_hermes.py /srv/hermes/channel-gateway/app/gw_hermes.py
4a9559b1add7f0a7f23ef3fe58c5b2082c734d8ebdca68db68875bdbfc58b7a7 gw_proactive.py /srv/hermes/channel-gateway/app/gw_proactive.py
6fc409cbb2e6b8d02e9e329147c38286f45666edfc3371db9511d49b37d892e7 gw_schema.py /srv/hermes/channel-gateway/app/gw_schema.py
e2cacb360dd8d0de41aafac0da4e451b50f7285f0a02aaf222d73d0c038512b7 gw_service.py /srv/hermes/channel-gateway/app/gw_service.py
09959c87b6c72f865d5ce0a554f9907844a98500e028c97cd612c3f0e02a64c8 gw_store.py /srv/hermes/channel-gateway/app/gw_store.py
de59e8b502f27a4a66df02e79e9729df71a2fda142a534f62a401432eccf512b bridge_server.py /srv/hermes/antigravity/app/bridge_server.py
7f34820add0f2c7f5e6665589a2c7ced21f57ffb6d811120eb71df62b1fb544c bridge_config.py /srv/hermes/antigravity/app/bridge_config.py
576e0855d7e949be0fa16b0fe0c6c6eb895337f1c6ef67399cf689c3a5a581c5 att_health.py /srv/hermes/attention/app/att_health.py
65e0b7370b1bcd7cd8adba65b37960425f6f776818a5092c9d21ee624cf1bcce channel-gateway.service /etc/systemd/system/channel-gateway.service
a17ae3482e21bb210ea7d561a6f24ea702a3134290bc8018b38e5afb9f4e2b97 hermes-antigravity.service /etc/systemd/system/hermes-antigravity.service
EOF
# Run from a checkout (src/channel-gateway/bin/), the script can also say whether
# the deployed files are the ones that checkout would deploy. Read-only: cmp + git.
REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." 2>/dev/null && pwd)
if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/src/channel-gateway/app/gw_service.py" ]; then
  # As the checkout's owner, never root: a root `git status` refreshes .git/index
  # as root:root 0600 (umask 077), locking the owner out, and runs repo-configured
  # hooks (core.fsmonitor) as root. --no-optional-locks: no index write at all.
  REPO_OWNER=$(stat -c %U "$REPO_ROOT" 2>/dev/null)
  rgit() { timeout 30 sudo -u "${REPO_OWNER:-nobody}" git --no-optional-locks -C "$REPO_ROOT" "$@" 2>/dev/null; }
  REV=$(rgit rev-parse --short HEAD || echo '?')
  if ST=$(rgit status --porcelain --untracked-files=no); then
    DIRTY=$(printf '%s' "$ST" | grep -c .)
  else
    DIRTY="? (git status as $REPO_OWNER failed)"
  fi
  sub "deployed vs this checkout ($REPO_ROOT @ $REV, $DIRTY locally modified tracked files)"
  for f in "$REPO_ROOT"/src/channel-gateway/app/*.py; do
    printf '%s %s\n' "$f" "$GW/app/$(basename "$f")"
  done > "$TMPD/s1map"
  cat >> "$TMPD/s1map" <<EOF
$REPO_ROOT/src/security_patterns.py $GW/app/security_patterns.py
$REPO_ROOT/src/antigravity-bridge/app/bridge_server.py $AG/app/bridge_server.py
$REPO_ROOT/src/antigravity-bridge/app/bridge_config.py $AG/app/bridge_config.py
$REPO_ROOT/src/attention/app/att_health.py $ATT/app/att_health.py
$REPO_ROOT/src/channel-gateway/systemd/channel-gateway.service /etc/systemd/system/channel-gateway.service
$REPO_ROOT/src/antigravity-bridge/systemd/hermes-antigravity.service /etc/systemd/system/hermes-antigravity.service
EOF
  while read -r src dest; do
    rel=${src#"$REPO_ROOT"/}
    if [ ! -f "$dest" ]; then st=MISS
    elif cmp -s "$src" "$dest"; then st=MATCH
    else st=DIFF; fi
    printf '   %-5s %s\n' "$st" "$rel"
  done < "$TMPD/s1map"
else
  echo "   (not run from a hermes-cos-infra checkout: deployed-vs-checkout comparison skipped)"
fi
sub "files in deployed app dirs that are NOT in the repo snapshot (e.g. premium routing)"
for f in "$GW"/app/*.py; do
  [ -e "$f" ] || continue
  case "$(basename "$f")" in
    gw_canonical.py|gw_config.py|gw_discord.py|gw_format.py|gw_hermes.py|gw_line.py|gw_line_client.py|gw_proactive.py|gw_quota.py|gw_schema.py|gw_service.py|gw_status.py|gw_store.py|security_patterns.py) ;;
    *) echo "   EXTRA $f  mtime=$(date -r "$f" '+%F %T')";;
  esac
done
for f in "$AG"/app/*.py; do
  [ -e "$f" ] || continue
  case "$(basename "$f")" in bridge_server.py|bridge_config.py) ;; *) echo "   EXTRA $f";; esac
done
sub "unit drop-ins (override timeouts/env without touching the unit file)"
for u in $KNOWN_UNITS; do
  printf '   %-26s %s\n' "$u" "$(systemctl show "$u" -p FragmentPath -p DropInPaths --no-pager 2>/dev/null | paste -sd' ')"
done
sub "effective gateway config ($GWCONF) -- timeouts/limits only"
H yamlpick "$GWCONF" gw
sub "effective bridge config ($AGCONF)"
H yamlpick "$AGCONF" bridge
echo "   unit Environment: $(systemctl show hermes-antigravity -p Environment --value 2>/dev/null \
     | sed -E 's/((TOKEN|KEY|SECRET)[A-Z_]*=)[^ ]+/\1<not shown>/g')"
sub "attention health thresholds ($ATTCONF)"
H yamlpick "$ATTCONF" att

sec "S2 [all] gw-status.sh snapshot (deployed, read-only; body-free by design)"
if [ -x "$GW/bin/gw-status.sh" ] || [ -f "$GW/bin/gw-status.sh" ]; then
  timeout 60 bash "$GW/bin/gw-status.sh" 2>&1 | ind
else
  echo "   $GW/bin/gw-status.sh not deployed"
fi

sec "S3 [H1 H3 H6 H7] channel-gateway log: per-request processing->delivered, ${HIST_DAYS}d trend"
echo "   unit: $(systemctl show channel-gateway -p ActiveState -p SubState -p NRestarts -p ExecMainStartTimestamp -p MemoryCurrent -p MemoryPeak --no-pager 2>/dev/null | paste -sd' ')"
{ journalctl -u channel-gateway --since "$J_HIST" -o short-unix --no-pager 2>/dev/null
  for f in "$GWLOGS"/gateway.log*; do [ -f "$f" ] && cat "$f"; done
} > "$TMPD/gw.log"
H gwparse "$FROM_EP" "$TO_EP" < "$TMPD/gw.log"
journalctl -u channel-gateway --since "$J_FROM" --until "$J_TO" -o short-unix --no-pager 2>/dev/null \
  | grep -E 'processing gw_|delivered gw_|waiting on hermes|ended status=|deadline|unreadable|no longer knew|unavailable|rejected|recovered|failed|heartbeat|Traceback|ERROR|WARNING|Started|Stopped|exited' \
  | H tsv GW >> "$TL"

sec "S4 [H1 H3 H4 H6 H7] gateway.db read-only (as channelgateway, mode=ro, query_only, no text columns)"
if [ -f "$GWDB" ] && id channelgateway >/dev/null 2>&1; then
  ls -l --time-style='+%F %T' "$GWDB" "$GWDB"-wal "$GWDB"-shm 2>/dev/null | awk '{print "   "$5" "$6" "$7" "$8}'
  timeout 120 sudo -u channelgateway "$GWPY" -I - "$GWDB" "$FROM_UTC" "$TO_UTC" "$HIST_DAYS" <<'PYEOF' 2>&1 | ind
import sqlite3, sys
from datetime import datetime, timezone, timedelta
db, f_utc, t_utc, days = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
JST = timezone(timedelta(hours=9))
# the timeout text before and after the durable-wait fix (both 42 chars, see S3)
TIMEOUT = '\u5fdc\u7b54\u306b\u6642\u9593\u304c\u304b\u304b\u308a\u3059\u304e\u305f\u305f\u3081\u4e2d\u65ad\u3057\u307e\u3057\u305f\u3002\u5185\u5bb9\u3092\u5206\u3051\u3066\u805e\u304d\u76f4\u3059\u3068\u901a\u308b\u3053\u3068\u304c\u3042\u308a\u307e\u3059\u3002'
TIMEOUT2 = '\u5fdc\u7b54\u306b\u6642\u9593\u304c\u304b\u304b\u308a\u3059\u304e\u305f\u305f\u3081\u4e2d\u65ad\u3057\u307e\u3057\u305f\u3002\u5c11\u3057\u6642\u9593\u3092\u304a\u3044\u3066\u3001\u3082\u3046\u4e00\u5ea6\u805e\u3044\u3066\u304f\u3060\u3055\u3044\u3002'
UNAVAIL = '\u3044\u307e\u5fdc\u7b54\u3067\u304d\u307e\u305b\u3093\u3002\u5fa9\u65e7\u5f8c\u306b\u3082\u3046\u4e00\u5ea6\u304a\u8a66\u3057\u304f\u3060\u3055\u3044\u3002'
INTERNAL = '\u51e6\u7406\u4e2d\u306b\u5185\u90e8\u30a8\u30e9\u30fc\u304c\u767a\u751f\u3057\u307e\u3057\u305f\u3002\u3082\u3046\u4e00\u5ea6\u304a\u8a66\u3057\u304f\u3060\u3055\u3044\u3002'

def iso(s):
    if not s: return None
    try:
        d = datetime.fromisoformat(str(s))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None
def jt(d): return d.astimezone(JST).strftime('%m-%d %H:%M:%S') if d else '-'
def dt(a, b): return f"{(b - a).total_seconds():.1f}" if a and b else '-'
def pct(xs, q):
    xs = sorted(xs); return xs[min(len(xs)-1, int(round(q*(len(xs)-1))))] if xs else None
def table(rows, cols):
    if not rows: print('(none)'); return
    w = [max(len(c), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    print('  '.join(c.ljust(w[i]) for i, c in enumerate(cols)))
    for r in rows: print('  '.join(str(r[i]).ljust(w[i]) for i in range(len(cols))))

try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
except Exception as exc:
    print(f"cannot open read-only: {type(exc).__name__}: {exc}"); sys.exit(0)

# run_started_at exists only once the durable-wait fix has opened the DB
RSA = ("i.run_started_at" if any(r[1] == 'run_started_at' for r in
       con.execute("PRAGMA table_info(inbound_messages)")) else "NULL")
CLS = ("CASE WHEN o.body IN (?, ?) THEN 'TIMEOUT' WHEN o.body=? THEN 'UNAVAILABLE' WHEN o.body=? THEN 'INTERNAL'"
       " WHEN o.body='' THEN 'PURGED' ELSE 'ANSWER' END")
Q = ("SELECT i.gateway_request_id rid, i.platform, i.state, i.attempts, i.last_error, i.hermes_run_id run_id,"
     f" i.platform_timestamp pts, i.received_at, i.completed_at, {RSA} rsa, length(i.text) in_chars,"
     f" (SELECT {CLS} FROM outbound_messages o WHERE o.gateway_request_id=i.gateway_request_id AND o.chunk_index=0) cls,"
     " (SELECT MIN(o.created_at) FROM outbound_messages o WHERE o.gateway_request_id=i.gateway_request_id) prep,"
     " (SELECT MAX(o.sent_at) FROM outbound_messages o WHERE o.gateway_request_id=i.gateway_request_id) sent,"
     " (SELECT COUNT(*) FROM outbound_messages o WHERE o.gateway_request_id=i.gateway_request_id) chunks,"
     " (SELECT group_concat(DISTINCT o.state) FROM outbound_messages o WHERE o.gateway_request_id=i.gateway_request_id) ostates"
     " FROM inbound_messages i WHERE i.received_at >= ? AND i.received_at < ? ORDER BY i.received_at, i.rowid")

print("-- inbound in window. lag=received-platform_ts (ingest delay); reply_s=reply prepared-received;")
print("   run_s=reply prepared-Hermes run started (only recorded by the durable-wait fix);")
print("   claimed_at is cleared on settle (gw_store.set_state), so claim time comes from the S3 journal")
rows = con.execute(Q, (TIMEOUT, TIMEOUT2, UNAVAIL, INTERNAL, f_utc, t_utc)).fetchall()
out = []
for r in rows:
    rec, pts, prep, sent = iso(r['received_at']), iso(r['pts']), iso(r['prep']), iso(r['sent'])
    out.append((jt(rec), r['platform'], r['state'], r['attempts'], dt(pts, rec), dt(rec, prep),
                dt(iso(r['rsa']), prep), dt(rec, sent),
                r['cls'] or '-', r['chunks'], r['ostates'] or '-', r['in_chars'] if r['in_chars'] is not None else '-',
                (r['last_error'] or '-')[:40], r['run_id'] or '-', r['rid']))
table(out, ['received', 'plat', 'state', 'att', 'lag_s', 'reply_s', 'run_s', 'sent_s', 'class', 'chunks', 'out_states',
            'in_chars', 'last_error', 'hermes_run_id', 'rid'])

print("\n-- audit_events in window (categories only)")
table([(jt(iso(r['at'])), r['event'], r['platform'] or '-', (r['detail'] or '-')[:60], r['gateway_request_id'] or '-')
       for r in con.execute("SELECT at, event, platform, detail, gateway_request_id FROM audit_events"
                            " WHERE at >= ? AND at < ? ORDER BY id", (f_utc, t_utc))],
      ['at', 'event', 'plat', 'detail', 'rid'])

print("\n-- delivery_attempts in window")
table([(jt(iso(r['attempted_at'])), r['mode'] or '-', r['outcome'], r['status_code'] or '-',
        (r['error_category'] or '-')[:40], r['rid'])
       for r in con.execute("SELECT d.attempted_at, d.mode, d.outcome, d.status_code, d.error_category,"
                            " o.gateway_request_id rid FROM delivery_attempts d JOIN outbound_messages o"
                            " ON o.outbound_id=d.outbound_id WHERE d.attempted_at >= ? AND d.attempted_at < ?"
                            " ORDER BY d.id", (f_utc, t_utc))],
      ['at', 'mode', 'outcome', 'code', 'category', 'rid'])

print("\n-- metrics (last_hermes_success_at / last_hermes_failure_at drive att_health hermes_failing)")
for r in con.execute("SELECT key, value FROM metrics ORDER BY key"):
    v = r['value']; d = iso(v) if isinstance(v, str) and 'T' in v else None
    print(f"   {r['key']:<28} {v}" + (f"   (= {jt(d)} JST)" if d else ''))

print("\n-- conversation_state: message_count = turns sent into ONE Hermes session (gw_store.claim_next)")
table([(r['platform'], '..' + str(r['conversation_id'])[-4:], ('..' + str(r['hermes_session_id'])[-4:]) if r['hermes_session_id'] else '-',
        r['message_count'], jt(iso(r['last_message_at'])), r['active_request_id'] or '-', jt(iso(r['active_since'])))
       for r in con.execute("SELECT * FROM conversation_state ORDER BY platform")],
      ['plat', 'conv', 'session', 'message_count', 'last_message', 'active_req', 'active_since'])

print("\n-- queue right now")
table([(r['state'], r['n']) for r in con.execute("SELECT state, COUNT(*) n FROM inbound_messages GROUP BY state")],
      ['state', 'n'])
table([(r['rid'], r['platform'], r['state'], r['attempts'], jt(iso(r['received_at'])), jt(iso(r['claimed_at'])),
        (r['last_error'] or '-')[:40])
       for r in con.execute("SELECT gateway_request_id rid, platform, state, attempts, received_at, claimed_at,"
                            " last_error FROM inbound_messages WHERE state NOT IN ('RESPONDED','FAILED','CANCELLED')"
                            " ORDER BY received_at")],
      ['rid', 'plat', 'state', 'att', 'received', 'claimed', 'last_error'])

print(f"\n-- per day (JST), last {days}d: reply class by body==fixed error text (bodies purged after retention -> PURGED)")
since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
agg = {}
for r in con.execute(Q.replace("WHERE i.received_at >= ? AND i.received_at < ?", "WHERE i.received_at >= ?"),
                     (TIMEOUT, TIMEOUT2, UNAVAIL, INTERNAL, since)):
    rec, prep = iso(r['received_at']), iso(r['prep'])
    if not rec: continue
    k = (rec.astimezone(JST).strftime('%Y-%m-%d'), r['platform'])
    a = agg.setdefault(k, {'n': 0, 'cls': {}, 'ans': [], 'inl': []})
    a['n'] += 1
    c = r['cls'] or 'NO_REPLY'
    a['cls'][c] = a['cls'].get(c, 0) + 1
    if c == 'ANSWER' and prep: a['ans'].append((prep - rec).total_seconds())
    if r['in_chars'] is not None: a['inl'].append(r['in_chars'])
f = lambda v: f"{v:.0f}" if v is not None else '-'
print(f"   {'day':<10} {'plat':<8} {'n':>4} {'ans_p50_s':>9} {'ans_p90_s':>9} {'ans_max_s':>9} {'in_p50':>6}  classes")
for k in sorted(agg):
    a = agg[k]
    print(f"   {k[0]:<10} {k[1]:<8} {a['n']:>4} {f(pct(a['ans'], .5)):>9} {f(pct(a['ans'], .9)):>9} "
          f"{f(max(a['ans']) if a['ans'] else None):>9} {f(pct(a['inl'], .5)):>6}  "
          + ' '.join(f"{c}={n}" for c, n in sorted(a['cls'].items())))
try:
    print("\n-- PRAGMA quick_check:", con.execute("PRAGMA quick_check").fetchone()[0])
except Exception as exc:
    print("\n-- PRAGMA quick_check failed:", type(exc).__name__)
PYEOF
else
  echo "   $GWDB not present or user channelgateway missing"
fi

sec "S5 [H2 H3] Hermes run status for the window's hermes_run_id values (GET /v1/runs/<id>; output never printed)"
RUNIDS=""
if [ -f "$GWDB" ] && id channelgateway >/dev/null 2>&1; then
  RUNIDS=$(timeout 60 sudo -u channelgateway "$GWPY" -I - "$GWDB" "$FROM_UTC" "$TO_UTC" <<'PYEOF' 2>/dev/null
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=15)
con.execute("PRAGMA query_only=ON")
ids = [r[0] for r in con.execute("SELECT hermes_run_id FROM inbound_messages WHERE received_at >= ? AND"
       " received_at < ? AND hermes_run_id IS NOT NULL ORDER BY received_at", (sys.argv[2], sys.argv[3]))]
print(' '.join(ids))
PYEOF
)
fi
echo "   run ids in window: ${RUNIDS:-(none recorded)}"
if [ -n "$RUNIDS" ] && [ "${SKIP_RUN_API:-0}" != 1 ]; then
  # shellcheck disable=SC2086
  timeout 120 "$PY" -I "$TMPD/h.py" runapi "$GWCONF" $RUNIDS
  echo "   (status=completed + large usage.input_tokens => slow-but-finishing run on a big context [H2];"
  echo "    still running/started hours later => hung run [H3/H4]; 404 run_not_found => Hermes lost it, e.g. restart)"
fi
BASE=$(H yamlget "$GWCONF" hermes.base_url); BASE=${BASE:-http://127.0.0.1:8642}
sub "Hermes /health right now (latency)"
code=$(curl -s -m 15 -o "$TMPD/hh.json" -w '%{http_code} %{time_total}s' "$BASE/health" 2>/dev/null)
echo "   $BASE/health -> ${code:-no answer}"; head -c 400 "$TMPD/hh.json" 2>/dev/null | H clean | ind

sec "S6 [H1 H3 H5 H6] Hermes container (name: hermes)"
if have docker; then
  docker inspect hermes --format '   status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}} failing_streak={{.State.Health.FailingStreak}}{{else}}none{{end}} restarts={{.RestartCount}} started={{.State.StartedAt}} finished={{.State.FinishedAt}} oom_killed={{.State.OOMKilled}} exit={{.State.ExitCode}}' 2>&1
  docker inspect hermes --format '   image={{.Config.Image}} id={{.Image}} mem_limit={{.HostConfig.Memory}} restart_policy={{.HostConfig.RestartPolicy.Name}}' 2>&1 | cut -c1-200
  img=$(docker inspect -f '{{.Image}}' hermes 2>/dev/null)
  [ -n "$img" ] && docker image inspect "$img" --format '   image created={{.Created}} labels={{json .Config.Labels}}' 2>/dev/null | cut -c1-400
  docker stats --no-stream --format '   cpu={{.CPUPerc}} mem={{.MemUsage}} ({{.MemPerc}}) blkio={{.BlockIO}} pids={{.PIDs}}' hermes 2>&1
  echo "   docker root: $(docker info --format '{{.DockerRootDir}} driver={{.Driver}}' 2>/dev/null)"
  sub "processes in the container (comm only, no argv)"
  docker top hermes -eo pid,ppid,etimes,pcpu,rss,stat,comm 2>&1 | head -40 | ind
  sub "docker events for hermes in window (restart/oom/health)"
  timeout 20 docker events --since "$D_FROM" --until "$D_TO" --filter container=hermes \
    --format '{{.Time}} {{.Action}}' 2>&1 | head -50 | while read -r t a; do
      echo "   $(date -d "@$t" '+%F %T' 2>/dev/null || echo "$t") $a"; done
  sub "other containers"
  docker ps -a --format '   {{.Names}}  {{.Image}}  {{.Status}}' 2>&1 | cut -c1-160
else
  echo "   docker CLI not found"
fi
journalctl -u docker --since "$J_FROM" --until "$J_TO" -o short-unix --no-pager 2>/dev/null | H tsv DK >> "$TL"

sec "S7 [H1 H2 H3] Hermes logs (keyword-filtered, sanitized; no prompt/answer text)"
HKW='error|exception|traceback|warn|timeout|timed out|deadline|fall(ing)? ?back|failover|retry|backoff|compress|compact|summari|context|token|rate.?limit|429|50[0-4]|overload|unavailable|refused|reset by peer|argument list|cancel|interrupt|busy|lock|mcp|oom|killed|provider|model|antigravity|bridge|8765|commandcode|opencode|fable|astra|claude|codex|gemini|glm|route|routing|premium|run_|/v1/runs|session'
if have docker; then
  # raw lines are never stored: streamed straight into the sanitizer
  timeout 90 docker logs -t --since "$D_FROM" --until "$D_TO" hermes 2>&1 | H tsv HM "$HKW" > "$TMPD/hm.tsv"
  HM_TOTAL=$(timeout 90 docker logs -t --since "$D_FROM" --until "$D_TO" hermes 2>&1 | wc -l)
  echo "   window lines: $HM_TOTAL total, $(wc -l < "$TMPD/hm.tsv") keyword-matching (into the S15 timeline)"
  cat "$TMPD/hm.tsv" >> "$TL"
  sub "category counts per 10 min in window"
  H counts "$FROM_EP" "$TO_EP" 600 < "$TMPD/hm.tsv"
  if [ -n "$RUNIDS" ]; then
    sub "lines mentioning the window's run ids (start/finish of the abandoned runs, if Hermes logs them)"
    printf '%s\n' $RUNIDS > "$TMPD/runids"
    timeout 90 docker logs -t --since "$D_FROM" hermes 2>&1 | grep -F -f "$TMPD/runids" | H tsv HM \
      | H timeline "$FROM_EP" "$NOW_EP" | head -80
  fi
  sub "daily category counts, last 7 days (docker json-file keeps only ~30MB)"
  timeout 120 docker logs -t --since 168h hermes 2>&1 | H tsv HM "$HKW" | H counts "$WEEK_EP" "$NOW_EP" 86400
fi
sub "files in $HLOGS (size, mtime)"
find "$HLOGS" -maxdepth 1 -type f -printf '   %10s  %TY-%Tm-%Td %TH:%TM  %f\n' 2>/dev/null | sort -k4 | head -40
for f in "$HLOGS"/agent.log* "$HLOGS"/errors.log* "$HLOGS"/gateway.log*; do
  [ -f "$f" ] || continue
  sub "$f (window, keyword-filtered; leading timestamp tz unknown -> tagged asUTC/asJST)"
  H filelog "$FROM_EP" "$TO_EP" "$HKW" < "$f"
done

sec "S8 [H2] Hermes session storage (sizes/mtimes only; names masked)"
if [ -d "$HSESS" ]; then
  echo "   $HSESS: $(find "$HSESS" -type f 2>/dev/null | wc -l) files, $(du -sh "$HSESS" 2>/dev/null | cut -f1)"
  sub "gateway sessions (gw-discord-*/gw-line-*)"
  find "$HSESS" -maxdepth 3 -type f \( -name '*gw-discord*' -o -name '*gw-line*' \) \
    -printf '%s\t%TY-%Tm-%Td %TH:%TM\t%P\n' 2>/dev/null | sort -nr | head -20 \
    | awk -F'\t' '{printf "   %12d B  %s  %s\n",$1,$2,$3}'
  sub "15 largest files"
  find "$HSESS" -maxdepth 3 -type f -printf '%s\t%TY-%Tm-%Td %TH:%TM\t%P\n' 2>/dev/null | sort -nr | head -15 \
    | awk -F'\t' '{printf "   %12d B  %s  %s\n",$1,$2,$3}'
  sub "files modified inside the window (each Hermes turn rewrites these on the USB disk)"
  find "$HSESS" -maxdepth 3 -type f -newermt "@$FROM_EP" ! -newermt "@$TO_EP" \
    -printf '%s\t%TY-%Tm-%Td %TH:%TM:%TS\t%P\n' 2>/dev/null | sort -k2 | head -30 \
    | awk -F'\t' '{printf "   %12d B  %s  %s\n",$1,substr($2,1,19),$3}'
else
  echo "   $HSESS not found"
fi
sub "top-level db/json files in $HD (Hermes session DB, if any)"
find "$HD" -maxdepth 1 -type f \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' -o -name '*.json' -o -name '*.jsonl' \) \
  ! -name 'auth.json' -printf '   %12s B  %TY-%Tm-%Td %TH:%TM  %f\n' 2>/dev/null
echo "   (bridge argv limit: one argument > 131072 bytes fails execve with E2BIG; see S9 e2big counts)"
sub "Hermes config.yaml (model/provider/fallback/timeout/compression/session keys only; secrets not shown)"
H yamlpick "$HCONF" hermes

sec "S9 [H1 H2 H5] Antigravity bridge (hermes-antigravity.service) and its other caller"
echo "   unit: $(systemctl show hermes-antigravity -p ActiveState -p SubState -p NRestarts -p ExecMainStartTimestamp -p MemoryCurrent -p MemoryPeak -p TasksCurrent --no-pager 2>/dev/null | paste -sd' ')"
curl -s -m 8 "$BRIDGE_HEALTH" 2>/dev/null | H bridgehealth
sub "processes of hermesantigravity (comm only; argv holds the prompt and is NOT printed). ppid=1 + large etimes = orphan"
ps -u hermesantigravity -o pid,ppid,etimes,pcpu,rss,stat,wchan:20,comm 2>/dev/null | ind
echo "   count by comm: $(ps -u hermesantigravity -o comm= 2>/dev/null | sort | uniq -c | tr -s ' ' | paste -sd',')"
sub "established connections right now (8642 = Hermes API, 8765 = bridge)"
echo "   :8642 $(ss -Htn state established '( sport = :8642 or dport = :8642 )' 2>/dev/null | wc -l)   :8765 $(ss -Htn state established '( sport = :8765 or dport = :8765 )' 2>/dev/null | wc -l)"
BRKW='timed out|failed|exception|error|errno|argument list|too large|cli call|parse|delegating|POST /|running on|stopping|traceback|started|stopped|exited|killed|code='
journalctl -u hermes-antigravity --since "$J_FROM" --until "$J_TO" -o short-unix --no-pager 2>/dev/null \
  | H tsv BR "$BRKW" > "$TMPD/br.tsv"
cat "$TMPD/br.tsv" >> "$TL"
sub "bridge category counts per 10 min in window (req=Hermes+broker calls; cli_timeout = 120s subprocess kill)"
H counts "$FROM_EP" "$TO_EP" 600 < "$TMPD/br.tsv"
sub "bridge CLI calls: duration and prompt size per call (content-free per-call log)"
journalctl -u hermes-antigravity --since "$(date -d "@$((NOW_EP-14*86400))" '+%F %T')" -o short-unix --no-pager 2>/dev/null \
  | H bridgecalls "$FROM_EP" "$TO_EP"
sub "bridge daily counts, last 14 days"
journalctl -u hermes-antigravity --since "$(date -d "@$((NOW_EP-14*86400))" '+%F %T')" -o short-unix --no-pager 2>/dev/null \
  | H tsv BR "$BRKW" | H counts "$((NOW_EP-14*86400))" "$NOW_EP" 86400
sub "independent latency probe: hermes-classifier-broker 'Classified ... in Ns' (small prompts through the same bridge)"
journalctl -u hermes-classifier-broker --since "$(date -d "@$((NOW_EP-14*86400))" '+%F %T')" -o short-unix --no-pager 2>/dev/null \
  | H classifier "$FROM_EP" "$TO_EP"
journalctl -u hermes-classifier-broker --since "$J_FROM" --until "$J_TO" -o short-unix --no-pager 2>/dev/null \
  | H tsv CB 'classified|error|timed out|warning' >> "$TL"

sec "S10 [H1 H6] premium routing (Fable/Astra) discovery -- not in either repo"
sub "service units matching hermes|antigravity|premium|fable|astra|claude|codex|router"
systemctl list-units --type=service --all --no-legend --plain --no-pager 2>/dev/null | awk '{print $1}' \
  | grep -Ei 'hermes|antigravity|premium|fable|astra|claude|codex|router|routing' | while read -r u; do
    printf '   %-40s %s\n' "$u" "$(systemctl show "$u" -p ActiveState -p NRestarts -p ExecMainStartTimestamp --no-pager 2>/dev/null | paste -sd' ')"
    case " $KNOWN_UNITS " in *" ${u%.service} "*) continue;; esac
    journalctl -u "$u" --since "$J_FROM" --until "$J_TO" -o short-unix --no-pager 2>/dev/null \
      | H tsv "U:${u%.service}" 'timeout|timed out|error|fail|fallback|exit|killed|exception|retry' | tee -a "$TL" \
      | cut -f3 | head -15 | ind
  done
sub "listening TCP sockets"
ss -Hltnp 2>/dev/null | awk '{print "   "$4"  "$6}' | sed -E 's/,fd=[0-9]+//g' | sort -u | head -40
sub "config/app files mentioning fable|astra (names only)"
timeout 60 grep -rIlE -i 'claude-fable|fable-5|gpt-6-astra|astra' /etc/systemd/system "$GW/app" "$GW/config" \
  "$AG/app" "$AG/config" "$HCONF" "$HD/skills" "$HD/tools" /srv/hermes/*/config 2>/dev/null | sort -u | head -40 | ind
sub "processes named like the premium CLIs (comm only)"
ps -eo user:18,pid,ppid,etimes,pcpu,rss,stat,comm --no-headers 2>/dev/null \
  | awk 'tolower($8) ~ /claude|codex|fable|astra|antigrav|gemini|node/' | head -30 | ind

sec "S11 [H4] storage: USB disk under /srv/hermes"
SRCDEV=$(findmnt -no SOURCE /srv/hermes 2>/dev/null)
echo "   /srv/hermes: $(findmnt -no SOURCE,FSTYPE,OPTIONS /srv/hermes 2>/dev/null)"
echo "   /          : $(findmnt -no SOURCE,FSTYPE,OPTIONS / 2>/dev/null)"
df -hT / /srv/hermes 2>/dev/null | ind; df -i /srv/hermes 2>/dev/null | tail -1 | ind
lsblk -o NAME,SIZE,TYPE,TRAN,MODEL,MOUNTPOINT 2>/dev/null | ind
have lsusb && lsusb -t 2>/dev/null | ind
BDEV=$(lsblk -no PKNAME "$SRCDEV" 2>/dev/null | head -1); BDEV=${BDEV:-$(basename "${SRCDEV:-sda}")}
echo "   block device: $BDEV  scheduler: $(cat /sys/block/"$BDEV"/queue/scheduler 2>/dev/null)  scsi timeout: $(cat /sys/block/"$BDEV"/device/timeout 2>/dev/null)s  inflight(r w): $(cat /sys/block/"$BDEV"/inflight 2>/dev/null)"
sub "5 s /proc/diskstats sample (current state only)"
ds() { awk -v d="$1" '$3==d {print $4, $7, $8, $11, $12, $13}' /proc/diskstats; }
for d in "$BDEV" mmcblk0; do
  a=$(ds "$d"); [ -n "$a" ] || continue
  sleep 5; b=$(ds "$d")
  read -r r1 rm1 w1 wm1 _ t1 <<<"$a"; read -r r2 rm2 w2 wm2 q2 t2 <<<"$b"
  dr=$((r2-r1)); dw=$((w2-w1))
  echo "   $d: r/s=$((dr/5)) w/s=$((dw/5)) r_await_ms=$([ $dr -gt 0 ] && echo $(((rm2-rm1)/dr)) || echo -) w_await_ms=$([ $dw -gt 0 ] && echo $(((wm2-wm1)/dw)) || echo -) util%=$(((t2-t1)/50)) in_flight=$q2"
done
have iostat && iostat -dxk 2 3 2>/dev/null | tail -n +3 | ind
sub "direct (uncached) 4 KiB read latency of gateway.db, 3 samples"
[ -f "$GWDB" ] && for i in 1 2 3; do
  s=$(date +%s%N); timeout 30 dd if="$GWDB" of=/dev/null bs=4096 count=1 iflag=direct status=none 2>/dev/null; rc=$?; e=$(date +%s%N)
  echo "   #$i $(( (e-s)/1000 )) us rc=$rc"
done
sub "PSI (cumulative stall since boot; avg10/60/300 = now)"
for f in io memory cpu; do
  if [ -r /proc/pressure/$f ]; then sed "s/^/   $f /" /proc/pressure/$f; else echo "   $f: PSI not available"; fi
done
sub "tasks in D state now (comm/wchan only)"
ps -eo pid,stat,etimes,wchan:32,comm --no-headers 2>/dev/null | awk '$2 ~ /^D/' | head -30 | ind
KPAT='usb [0-9]+-[0-9.]+:|uas|usb-storage|i/o error|buffer i/o|blk_update_request|critical (medium|target) error|ext4-fs (error|warning)|jbd2|blocked for more than|hung_task|xhci|over-current|under-?voltage|voltage normali|throttl|out of memory|oom-kill|oom_reaper|killed process|memory cgroup out of memory|scsi .*(timeout|abort|reset)|\[sd[a-z]\]|read-only|mmc[0-9]'
journalctl -k --since "$J_FROM" --until "$J_TO" -o short-unix --no-pager 2>/dev/null | grep -Ei "$KPAT" | H tsv KR >> "$TL"
sub "kernel messages in window"
journalctl -k --since "$J_FROM" --until "$J_TO" -o short-unix --no-pager 2>/dev/null | grep -Ei "$KPAT" \
  | H tsv KR | H timeline "$FROM_EP" "$TO_EP" | head -120
sub "kernel daily counts, last ${HIST_DAYS} days (persistent journal only)"
journalctl -k --since "$J_HIST" -o short-unix --no-pager 2>/dev/null | grep -Ei "$KPAT" | H tsv KR \
  | H counts "$HIST_EP" "$NOW_EP" 86400
sub "dmesg ring buffer (current boot), last matches"
dmesg -T 2>/dev/null | grep -Ei "$KPAT" | tail -40 | H clean | ind

sec "S12 [H5] memory / CPU / thermal / power"
free -m | ind
grep -E 'MemTotal|MemAvailable|SwapTotal|SwapFree|Dirty|Writeback:|Committed_AS' /proc/meminfo | ind
cat /proc/swaps 2>/dev/null | ind
sub "vmstat 1 5 (b = blocked on I/O, wa = iowait, si/so = swap)"
vmstat 1 5 2>/dev/null | ind
for u in $KNOWN_UNITS; do
  printf '   %-26s %s\n' "$u" "$(systemctl show "$u" -p MemoryCurrent -p MemoryPeak -p MemoryMax -p TasksCurrent --no-pager 2>/dev/null | paste -sd' ')"
done
sub "top RSS processes (comm only)"
ps -eo user:16,pid,etimes,pcpu,rss,comm --sort=-rss --no-headers 2>/dev/null | head -15 | ind
if have vcgencmd; then
  T=$(vcgencmd get_throttled 2>/dev/null); echo "   $T   $(vcgencmd measure_temp 2>/dev/null)   $(vcgencmd measure_clock arm 2>/dev/null)   $(vcgencmd measure_volts core 2>/dev/null)"
  case "${T#throttled=}" in 0x[0-9a-fA-F]*) v=$(( ${T#throttled=} ));; *) v=0;; esac
  for b in "0 under-voltage NOW" "1 freq-capped NOW" "2 throttled NOW" "3 soft-temp-limit NOW" "16 under-voltage OCCURRED since boot" "17 freq-capped OCCURRED" "18 throttled OCCURRED" "19 soft-temp-limit OCCURRED"; do
    n=${b%% *}; [ $(( (v >> n) & 1 )) -eq 1 ] && echo "   get_throttled bit $n: ${b#* }"
  done
else
  echo "   vcgencmd not found"
fi
for z in /sys/class/thermal/thermal_zone*/temp; do [ -r "$z" ] && echo "   $z = $(( $(cat "$z") / 1000 )) C"; done

sec "S13 [H7] attention engine"
echo "   unit: $(systemctl show attention-engine -p ActiveState -p NRestarts -p ExecMainStartTimestamp -p MemoryCurrent --no-pager 2>/dev/null | paste -sd' ')"
if [ -S "$SOCK" ]; then
  echo "   proactive socket /internal/health -> $(curl -s -m 5 -o /dev/null -w '%{http_code}' --unix-socket "$SOCK" http://localhost/internal/health 2>/dev/null)"
  sub "gateway /internal/liveness (exactly what att_health.probe_gateway reads)"
  curl -s -m 10 --unix-socket "$SOCK" http://localhost/internal/liveness 2>/dev/null | H jsonflat
else
  echo "   $SOCK not present"
fi
ATTDB=$(sed -n 's/^[[:space:]]*state_db:[[:space:]]*//p' "$ATTCONF" 2>/dev/null | head -1); ATTDB=${ATTDB:-$ATT/state/attention.db}
if [ -f "$ATTDB" ]; then
  ATTOWN=$(stat -c %U "$ATTDB")
  sub "attention.db read-only as $ATTOWN: gateway episodes, scan durations"
  timeout 120 sudo -u "$ATTOWN" "$PY" -I - "$ATTDB" "$FROM_UTC" "$TO_UTC" <<'PYEOF' 2>&1 | ind
import sqlite3, sys
from datetime import datetime, timezone, timedelta
db, f_utc, t_utc = sys.argv[1:4]
JST = timezone(timedelta(hours=9))
def iso(s):
    try:
        d = datetime.fromisoformat(str(s)); return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception: return None
def jt(s):
    d = iso(s); return d.astimezone(JST).strftime('%m-%d %H:%M:%S') if d else '-'
def pct(xs, q):
    xs = sorted(xs); return xs[min(len(xs)-1, int(round(q*(len(xs)-1))))] if xs else None
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
con.row_factory = sqlite3.Row
con.execute("PRAGMA query_only=ON")
lo = (datetime.fromisoformat(f_utc).replace(tzinfo=timezone.utc) - timedelta(days=1)).isoformat()
print("-- notification_episodes (gateway/hermes) since window-1d. detail 'last success <ts>' = metric at alert time")
for r in con.execute("SELECT * FROM notification_episodes WHERE first_degraded_at >= ? ORDER BY first_degraded_at", (lo,)):
    print(f"   {r['service']:<10} {r['reason']:<17} {r['state']:<9} first={jt(r['first_degraded_at'])} "
          f"last={jt(r['last_degraded_at'])} obs={r['observations']} notified={jt(r['notified_at'])} "
          f"recovered={jt(r['recovered_at'])} recovery_notified={jt(r['recovery_notified_at'])} "
          f"detail={(r['detail'] or '')[:70]}")
print("-- scan_runs in window (duration_ms spikes = the engine's own SQLite reads on /srv/hermes were slow [H4])")
for r in con.execute("SELECT started_at, duration_ms, ok, signals_seen, delivered, failed, error FROM scan_runs"
                     " WHERE started_at >= ? AND started_at < ? ORDER BY id", (f_utc, t_utc)):
    print(f"   {jt(r['started_at'])} dur_ms={r['duration_ms']} ok={r['ok']} seen={r['signals_seen']} "
          f"delivered={r['delivered']} failed={r['failed']} err={(r['error'] or '')[:50]}")
print("-- scan duration per day (JST), last 14d")
days = {}
since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
for r in con.execute("SELECT started_at, duration_ms, ok FROM scan_runs WHERE started_at >= ?", (since,)):
    d = iso(r['started_at'])
    if not d: continue
    k = d.astimezone(JST).strftime('%Y-%m-%d')
    a = days.setdefault(k, {'d': [], 'bad': 0})
    if r['duration_ms'] is not None: a['d'].append(r['duration_ms'])
    if not r['ok']: a['bad'] += 1
for k in sorted(days):
    a = days[k]
    print(f"   {k} n={len(a['d'])} p50={pct(a['d'], .5)} p90={pct(a['d'], .9)} max={max(a['d']) if a['d'] else '-'} failed_scans={a['bad']}")
PYEOF
  sub "att_status.py --days 2 (deployed; the LINE budget call in att-status.sh is skipped: it can hit LINE's API and write the gateway quota cache)"
  [ -f "$ATT/app/att_status.py" ] && timeout 60 sudo -u "$ATTOWN" "$ATTPY" "$ATT/app/att_status.py" --db "$ATTDB" --days 2 2>&1 | ind
else
  echo "   $ATTDB not found"
fi
journalctl -u attention-engine --since "$J_FROM" --until "$J_TO" -o short-unix --no-pager 2>/dev/null \
  | H tsv AT 'health|episode|degrad|recover|probe|gateway|hermes|error|traceback|warn|deliver|scan' >> "$TL"

sec "S14 [H4 H5] systemd jobs/timers overlapping the window (background I/O and CPU competitors)"
systemctl list-timers --all --no-pager 2>/dev/null | grep -Ei 'hermes|line|gmail|calendar|brief|tentative|attention|discord|docker|apt|man-db|logrotate|fstrim' | cut -c1-170 | ind
journalctl _PID=1 --since "$J_FROM" --until "$J_TO" -o short-unix --no-pager 2>/dev/null \
  | grep -vE 'session-[0-9]+\.scope|user@|user-runtime-dir|Reached target|Stopped target|ssh' \
  | H tsv SY 'started|finished|stopped|failed|consumed|deactivated|starting|stopping|timeout|killed' >> "$TL"

sec "S15 unified timeline, window (JST). GW=gateway BR=bridge HM=hermes CB=classifier KR=kernel AT=attention SY=systemd DK=dockerd"
H timeline "$FROM_EP" "$TO_EP" < "$TL"

sec "S16 how to read this"
cat <<'EOF'
   * S3/S4 on the 597c897 code (what ran on 2026-10-27): one worker task served every conversation
     and claim_next skipped a conversation with a PROCESSING row, so requests ran strictly one at a
     time. Two timeout replies in the same minute are impossible unless S1 shows DIFF or the S1
     timeout values differ. Expect processing->delivered ~= 240 s + POST latency per timeout.
   * After the durable-wait fix: S3 'Hermes runs ended' gives Hermes's own elapsed time and usage
     tokens per run, S4 run_s the same from the DB. elapsed near the deadline (480 s default) with
     status=running -> Hermes is still slow [H1-H5]; answers arriving after 240 s that used to be
     thrown away are now delivered, so TIMEOUT counts should drop even before the root cause is fixed.
   * S4 lag_s large (> a few s) on the 597c897 code: the Discord event loop was blocked before
     enqueue (gw_discord._handle ran SQLite on the loop) -> storage stall [H4]. S3 'heartbeat
     blocked' lines say the same.
   * S5 run status completed with big input_tokens, S8 gw-discord session file in the MB range,
     S4 message_count high, S3/S4 per-day answer p50 rising over days -> context growth [H2].
     S9 e2big>0 -> the bridge cannot exec the CLI at all for that session [H2].
   * S9 cli_timeout at ~120 s after a 'delegating' line, then a second call or fallback provider in
     S7 -> 2 x 120 s >= 240 s gateway budget [H1]. classifier-broker latency also high in the window
     -> the bridge/host was slow for everyone [H1/H4/H5]; normal -> Hermes-side [H2/H3].
   * S5 runs still 'running' and S9/S6 showing many concurrent CLI/agent processes or ppid=1 orphans
     -> abandoned runs piling up [H3/H5].
   * S11/S15 KR lines (usb reset, I/O error, blocked for more than) inside the window, S13 scan
     dur_ms spikes, S11 D-state tasks -> USB I/O stall [H4]. hung_task_timeout_secs=0 means the
     kernel could not have logged blocked tasks at all.
   * S12 get_throttled under-voltage OCCURRED + usb resets -> power-induced USB stall [H4/H5].
   * S13: if the gateway episode closed (~14:20) while S4 metrics show last_hermes_success_at still
     before 13:11, the 'recovered' message was the 3600 s window in att_health.py:172-181 expiring,
     not a Hermes success [H7].
EOF
}

main 2>&1 | "$PY" -I -u "$TMPD/h.py" mask | tee "$OUT"
chmod 600 "$OUT"
echo
echo "report: $OUT  (masked; still review before sharing)"
