#!/usr/bin/env python3
"""Real AF AI editors: turns Natalie's raw clips into posts, then learns from results.
Architecture: Projects/Real AF/Context/Thoughts/2026-11-23 Short-Form Content Flywheel - Architecture.md

  flywheel.py scan              register raw clips from the source folders (first run = backlog)
  flywheel.py prep <id|path>    probe, transcribe, plan the 9:16 crop for one raw clip
  flywheel.py status            counts by folder and state
  flywheel.py decide <raw_id> <editor> [--manual file.json]   hook, song, caption (Claude, headless)
  flywheel.py render <edit_id>  render + QA one decided edit
  flywheel.py approve <edit_id> | reject <edit_id>             review gate while auto_post is false
  flywheel.py takedown <raw_id|edit:id|post url> [reason]    pull a clip from every account
  flywheel.py accounts          list accounts connected in Zernio (ids for editor.json)
  flywheel.py queue | post | track                              schedule ready edits, publish due posts, pull metrics
  flywheel.py stats             refresh latest post numbers and follower counts for the dashboard
  flywheel.py run               one full unattended pass for every editor (what the scheduler calls)
Output recipe: Context/AI Editors - Output Spec.md. Editors: editors/<editor>/editor.json + style.json.
Still to build: learn (weekly playbook update), real posting backend (backends/<name>.py).
"""
import csv, shutil, datetime as dt, fcntl, glob, importlib, json, os, re, sqlite3, statistics, subprocess, sys, tempfile, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, 'config.json')))
if os.path.exists(os.path.join(HERE, 'config.local.json')):  # this machine's paths; not committed
    CFG.update(json.load(open(os.path.join(HERE, 'config.local.json'))))
WORK = os.path.expanduser(CFG['work_dir'])
DB = os.path.join(WORK, 'flywheel.db')
NAME = re.compile(r'^(\d+(?:\.\d+)?)_(?:([A-Za-z0-9]+?)_)?(.+)\.(mp4|mov)$', re.I)  # 1072_QAF_Subject, 847.5_RealTalk_Ego, 634_MediaLies


def db():
    os.makedirs(WORK, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript('''
    CREATE TABLE IF NOT EXISTS raw_clips(
      id INTEGER PRIMARY KEY, path TEXT UNIQUE, folder TEXT, episode REAL, tag TEXT, subject TEXT,
      mtime REAL, size INTEGER, origin TEXT, first_seen REAL);
    CREATE TABLE IF NOT EXISTS preps(
      raw_id INTEGER PRIMARY KEY REFERENCES raw_clips(id), width INTEGER, height INTEGER, fps REAL, duration REAL,
      orientation TEXT, crop TEXT, flags TEXT, words_path TEXT, transcript TEXT, prepared_at REAL);
    CREATE TABLE IF NOT EXISTS edits(
      id INTEGER PRIMARY KEY, raw_id INTEGER REFERENCES raw_clips(id), editor TEXT, version INTEGER, status TEXT,
      decision TEXT, hook TEXT, song TEXT, music_start REAL, path TEXT, qa TEXT, note TEXT, created REAL, updated REAL,
      UNIQUE(raw_id, editor, version));
    CREATE TABLE IF NOT EXISTS posts(
      id INTEGER PRIMARY KEY, edit_id INTEGER REFERENCES edits(id), editor TEXT, platform TEXT, account TEXT,
      slot REAL, backend TEXT, remote_id TEXT, url TEXT, status TEXT, error TEXT, posted_at REAL);
    CREATE TABLE IF NOT EXISTS blocked(raw_id INTEGER PRIMARY KEY, reason TEXT, at REAL);
    CREATE TABLE IF NOT EXISTS metrics(
      post_id INTEGER REFERENCES posts(id), taken_at REAL, age_h REAL, views INTEGER, likes INTEGER, comments INTEGER,
      shares INTEGER, saves INTEGER, avg_watch REAL, follows INTEGER, raw TEXT);
    CREATE TABLE IF NOT EXISTS post_stats(
      post_id INTEGER PRIMARY KEY REFERENCES posts(id), taken_at REAL, views INTEGER, likes INTEGER, comments INTEGER,
      shares INTEGER, saves INTEGER, raw TEXT);
    CREATE TABLE IF NOT EXISTS account_stats(
      account TEXT, platform TEXT, username TEXT, day TEXT, followers INTEGER, taken_at REAL, PRIMARY KEY(account, day));
    ''')
    return c


def scan():
    c = db()
    first_run = c.execute('SELECT COUNT(*) FROM raw_clips').fetchone()[0] == 0
    newest = c.execute('SELECT MAX(mtime) FROM raw_clips').fetchone()[0] or 0  # older files found later are still backlog
    added = 0
    for folder in CFG['source_folders']:
        base = os.path.join(os.path.expanduser(CFG['source_root']), folder)
        for root, _, files in os.walk(base):
            for f in files:
                m = NAME.match(f)
                if f.startswith('.') or not m:
                    continue
                p = os.path.join(root, f)
                st = os.stat(p)
                cur = c.execute('INSERT OR IGNORE INTO raw_clips(path,folder,episode,tag,subject,mtime,size,origin,first_seen) VALUES(?,?,?,?,?,?,?,?,?)',
                                (p, folder, float(m[1]), m[2] or folder, m[3], st.st_mtime, st.st_size, 'new' if not first_run and st.st_mtime > newest else 'backlog', time.time()))
                added += cur.rowcount
    c.commit()
    print(f'{added} raw clips registered')


def probe(path):
    j = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                   'stream=width,height,avg_frame_rate:stream_side_data=rotation:format=duration', '-of', 'json', path],
                                  capture_output=True, text=True, check=True).stdout)
    s = j['streams'][0]
    w, h = s['width'], s['height']
    if any(abs(sd.get('rotation', 0)) == 90 for sd in s.get('side_data_list', [])):
        w, h = h, w
    n, d = s['avg_frame_rate'].split('/')
    return w, h, float(n) / float(d), float(j['format']['duration'])


def crop_plan(path, w, h, dur):
    """Fixed 9:16 crop centred on the most prominent face. Flags multi-person shots and subjects that drift."""
    cw = int(h * 9 / 16) // 2 * 2
    with tempfile.TemporaryDirectory() as t:
        subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vf', 'fps=1,scale=640:-2', '-q:v', '5', f'{t}/f%04d.jpg'], check=True)
        frames = json.loads(subprocess.run([os.path.join(HERE, 'bin', 'faces'), t], capture_output=True, text=True, check=True).stdout)
    faces = [f for fr in frames for f in fr['faces'] if f['c'] > 0.5 and f['h'] > 0.06]
    flags = []
    if not faces:
        return {'x': (w - cw) // 2, 'w': cw, 'h': h}, ['no_face']
    # cluster face centres along x (people sit side by side in wide shots)
    xs = sorted((f['x'] + f['w'] / 2, f['h']) for f in faces)
    clusters, cur = [], [xs[0]]
    for x in xs[1:]:
        if x[0] - cur[-1][0] > 0.12:
            clusters.append(cur); cur = []
        cur.append(x)
    clusters.append(cur)
    clusters = [cl for cl in clusters if len(cl) >= max(2, 0.2 * len(frames))]
    if not clusters:
        clusters = [[x for x in xs]]
    if len(clusters) > 1:
        flags.append(f'multi_person:{len(clusters)}')
    main = max(clusters, key=lambda cl: len(cl) * statistics.median(f[1] for f in cl))
    cx = statistics.median(f[0] for f in main) * w
    spread = (max(f[0] for f in main) - min(f[0] for f in main)) * w
    if spread > 0.45 * cw:
        flags.append('subject_drifts')
    x = int(min(max(cx - cw / 2, 0), w - cw)) // 2 * 2
    return {'x': x, 'w': cw, 'h': h}, flags


def local_copy(row):
    """Copy the raw clip out of Google Drive into a local cache and check it is complete.
    Drive files can be online-only placeholders; a failed or partial download must raise (and be retried), never be edited."""
    dst = os.path.join(WORK, 'cache', f"{row['id']}{os.path.splitext(row['path'])[1].lower()}")
    want = os.stat(row['path']).st_size
    if not (os.path.exists(dst) and os.path.getsize(dst) == want):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + '.part'
        shutil.copyfile(row['path'], tmp)
        if os.path.getsize(tmp) != want:
            raise IOError(f"incomplete download of {os.path.basename(row['path'])}: {os.path.getsize(tmp)} of {want} bytes")
        os.replace(tmp, dst)
    return dst


def clean_cache(days=2):
    for p in glob.glob(os.path.join(WORK, 'cache', '*')):
        if time.time() - os.path.getmtime(p) > days * 86400:
            os.remove(p)


def prep(arg):
    c = db()
    row = c.execute('SELECT * FROM raw_clips WHERE id=? OR path=?', (arg, arg)).fetchone()
    if not row:
        sys.exit(f'unknown raw clip: {arg}')
    out = os.path.join(WORK, 'prep', str(row['id']))
    os.makedirs(out, exist_ok=True)
    src = local_copy(row)
    w, h, fps, dur = probe(src)
    orientation = 'vertical' if h > w else 'landscape'
    crop, flags = (None, []) if orientation == 'vertical' else crop_plan(src, w, h, dur)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', f'{out}/audio.wav'], check=True)
    subprocess.run(['whisper-cli', '-m', os.path.expanduser(CFG['whisper_model']), '-f', f'{out}/audio.wav', '-np', '-ml', '1', '-sow', '-oj', '-of', f'{out}/words'],
                   check=True, capture_output=True)
    os.remove(f'{out}/audio.wav')
    words = [s['text'].strip() for s in json.load(open(f'{out}/words.json'))['transcription'] if s['text'].strip()]
    transcript = ' '.join(words)
    c.execute('INSERT OR REPLACE INTO preps VALUES(?,?,?,?,?,?,?,?,?,?,?)',
              (row['id'], w, h, fps, dur, orientation, json.dumps(crop), json.dumps(flags), f'{out}/words.json', transcript, time.time()))
    c.commit()
    print(json.dumps({'id': row['id'], 'clip': os.path.basename(row['path']), 'size': f'{w}x{h}@{fps:.2f}', 'duration': round(dur, 1),
                      'orientation': orientation, 'crop': crop, 'flags': flags, 'transcript': transcript[:90] + '…'}))


# ---------- editors ----------
SPEC = os.path.join(HERE, '..', 'Context', 'AI Editors - Output Spec.md')
# Hard exclusion keywords (Matt, 2026-11-23): any mention removes the clip for every editor, before Claude is asked.
EXCLUDE_WORDS = re.compile(r'\b(israel\w*|jew|jews|jewish|judaism|zionis\w*|zion|holocaust|synagogue\w*|rabbi\w*|anti-?semit\w*|'
                           r'rothschild\w*|soros|globalist\w*|kosher|gaza|palestin\w*|netanyahu|idf|mossad|aipac|hamas)\b', re.I)
LOG = os.path.join(WORK, 'logs', 'run.log')


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    open(LOG, 'a').write(line + '\n')


def editors():
    out = []
    for d in sorted(glob.glob(os.path.join(HERE, 'editors', '*', 'editor.json'))):
        e = json.load(open(d))
        e['id'] = os.path.basename(os.path.dirname(d))
        e['dir'] = os.path.dirname(d)
        out.append(e)
    return out


def editor(eid):
    for e in editors():
        if e['id'] == eid:
            return e
    sys.exit(f'unknown editor: {eid}')


def songs():
    return [r for r in csv.DictReader(open(os.path.join(HERE, 'music', 'catalog.csv')))
            if r['primary'] == 'y' and r['vocals'] == 'n' and r['confidence'] == 'high' and r['mood']]


def music_start(fname):
    """Skip silence at the head of a track so the bed is audible from the first second."""
    path = os.path.join(os.path.expanduser(CFG['music_dir']), fname)
    err = subprocess.run(['ffmpeg', '-v', 'info', '-t', '40', '-i', path, '-af', 'silencedetect=n=-45dB:d=0.4', '-f', 'null', '-'],
                         capture_output=True, text=True).stderr
    m = re.search(r'silence_start: (-?[\d.]+)\s.*?silence_end: ([\d.]+)', err, re.S)
    if m and float(m[1]) <= 0.05 and float(m[2]) >= 39:
        raise ValueError(f'silent music file: {fname}')  # e.g. a broken download in the Drive folder
    return round(float(m[2]), 2) if m and float(m[1]) <= 0.05 else 0.0


# ---------- decide ----------
DECISION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['excluded', 'exclusion_reason', 'usable', 'skip_reason', 'topical', 'opening_raises_question', 'hook', 'alt_hooks', 'mood', 'song',
                 'post_caption', 'hashtags', 'risk_flags'],
    'properties': {
        'excluded': {'type': 'boolean', 'description': 'hits a hard exclusion; checked before anything else'},
        'exclusion_reason': {'type': 'string'},
        'usable': {'type': 'boolean'}, 'skip_reason': {'type': 'string'},
        'topical': {'type': 'boolean', 'description': 'depends on a news event that will date'},
        'opening_raises_question': {'type': 'boolean'},
        'hook': {'type': 'string'}, 'alt_hooks': {'type': 'array', 'items': {'type': 'string'}},
        'mood': {'type': 'string', 'enum': ['reflective', 'epic', 'hype', 'dark', 'warm']},
        'song': {'type': 'string', 'description': 'exact file name from the list'},
        'post_caption': {'type': 'string'}, 'hashtags': {'type': 'array', 'items': {'type': 'string'}},
        'risk_flags': {'type': 'array', 'items': {'type': 'string'}}}}


def decide_prompt(row, prep_row, ed, pool):
    spec = open(SPEC).read()
    rules = spec[spec.index('## Hook rules'):spec.index('## Choices the editor makes per clip')]
    exclusions = 'FIRST, check the hard exclusions. If any applies, set excluded=true and stop caring about the rest.\n' + \
        spec[spec.index('## Hard exclusions'):spec.index('## Hook rules')]
    playbook = os.path.join(ed['dir'], 'playbook.md')
    playbook = open(playbook).read() if os.path.exists(playbook) else '(none yet)'
    songs_txt = '\n'.join(f"- {r['file']} | {r['song']} | {r['mood']} | energy {r['energy']}" for r in pool)
    q3 = '"' * 3
    return f"""You are {ed['name']}, an AI short-form editor for Andy Frisella's Real AF podcast.
Natalie (human editor) cut this raw clip. Decide the text hook, the music bed and the post caption. Output JSON only.

{exclusions}
{rules}
This editor's playbook (lessons from past posts):
{playbook}

Raw clip: {os.path.basename(row['path'])} (folder {row['folder']}, {prep_row['duration']:.0f} s)
Transcript (auto, may lack punctuation):
{q3}{prep_row['transcript']}{q3}

Songs you may use (instrumentals; pick one whose mood fits; file name must match exactly):
{songs_txt}

Also:
- usable=false only if the clip is unusable (cut mid-thought with no payoff, mostly crosstalk, unclear audio); say why in skip_reason.
- topical=true if it depends on a news event or date that will go stale.
- risk_flags: subject matter Natalie should see first (politics, a named private person, medical or legal claims, likely out-of-context reading). Swearing is never a risk flag on its own; only flag what the clip is about or how it could be read. Empty list if none.
- post_caption: one or two plain sentences that extend the hook's question without answering it; no emoji. hashtags: 3 to 5, lowercase, no '#'.
- alt_hooks: 2 more hooks that follow the rules."""


def ask_claude(prompt):
    cmd = ['claude', '-p', prompt, '--output-format', 'json', '--tools', '', '--no-session-persistence',
           '--setting-sources', '', '--json-schema', json.dumps(DECISION_SCHEMA)]
    if CFG.get('decide_model'):
        cmd += ['--model', CFG['decide_model']]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=WORK, timeout=300)
    out = json.loads(r.stdout or '{}')
    if out.get('is_error') or 'structured_output' not in out:
        raise RuntimeError(f"claude -p failed: {out.get('result') or r.stderr[:300]}")
    return out['structured_output']


def decide(raw_id, eid, manual=None):
    c = db()
    ed = editor(eid)
    row = c.execute('SELECT * FROM raw_clips WHERE id=?', (raw_id,)).fetchone()
    if not c.execute('SELECT 1 FROM preps WHERE raw_id=?', (raw_id,)).fetchone():
        prep(str(raw_id))
    prep_row = c.execute('SELECT * FROM preps WHERE raw_id=?', (raw_id,)).fetchone()
    recent = [r[0] for r in c.execute('SELECT song FROM edits WHERE editor=? AND raw_id!=? AND song IS NOT NULL ORDER BY id DESC LIMIT 5', (eid, raw_id))]
    pool = [r for r in songs() if r['file'] not in recent]
    hit = EXCLUDE_WORDS.search(prep_row['transcript'] or '')
    if hit and not manual:
        d = {'excluded': True, 'exclusion_reason': f'keyword: {hit[0]}', 'usable': False, 'skip_reason': '', 'topical': False,
             'opening_raises_question': False, 'hook': '', 'alt_hooks': [], 'mood': 'reflective', 'song': '', 'post_caption': '',
             'hashtags': [], 'risk_flags': []}
    else:
        d = json.load(open(manual)) if manual else ask_claude(decide_prompt(row, prep_row, ed, pool))
    d.setdefault('excluded', False)
    if d['excluded']:
        d['usable'] = False
        c.execute('INSERT OR REPLACE INTO blocked VALUES(?,?,?)', (raw_id, 'excluded: ' + d.get('exclusion_reason', ''), time.time()))
    if d['usable'] and d['song'] not in {r['file'] for r in pool}:
        d['song'] = next(r['file'] for r in pool if r['mood'] == d['mood'])  # model named a song not on the list
    mstart = None
    if d['usable']:
        for alt in [d['song']] + [r['file'] for r in pool if r['mood'] == d['mood'] and r['file'] != d['song']]:
            try:
                mstart, d['song'] = music_start(alt), alt
                break
            except ValueError as ex:
                log(f'{eid} {ex}; trying another {d["mood"]} song')
    age_days = (time.time() - row['mtime']) / 86400
    status, note = 'decided', ''
    if d['excluded']:
        status, note = 'excluded', d.get('exclusion_reason', '')
    elif not d['usable']:
        status, note = 'skipped', d['skip_reason']
    elif d['topical'] and age_days > CFG.get('topical_max_age_days', 14):
        status, note = 'skipped', f'topical clip, {age_days:.0f} days old'
    elif d['risk_flags'] or not d['opening_raises_question']:
        note = '; '.join(d['risk_flags'] + ([] if d['opening_raises_question'] else ['opening does not raise a question']))
    v = (c.execute('SELECT MAX(version) FROM edits WHERE raw_id=? AND editor=?', (raw_id, eid)).fetchone()[0] or 0) + 1
    cur = c.execute('INSERT INTO edits(raw_id,editor,version,status,decision,hook,song,music_start,note,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (raw_id, eid, v, status, json.dumps(d), d['hook'], d['song'] if d['usable'] else None,
                     mstart if status == 'decided' else None, note, time.time(), time.time()))
    c.commit()
    log(f"{eid} decide raw {raw_id} -> edit {cur.lastrowid} {status}: {d['hook']!r} / {d['song']} {('(' + note + ')') if note else ''}")
    return cur.lastrowid


# ---------- render + QA ----------
def qa(path, src_dur):
    j = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,width,height:format=duration', '-of', 'json', path],
                                  capture_output=True, text=True, check=True).stdout)
    v = next(s for s in j['streams'] if s['codec_type'] == 'video')
    has_audio = any(s['codec_type'] == 'audio' for s in j['streams'])
    err = subprocess.run(['ffmpeg', '-v', 'info', '-i', path, '-af', 'ebur128', '-f', 'null', '-'], capture_output=True, text=True).stderr
    lufs = float(re.findall(r'I:\s+(-?[\d.]+) LUFS', err)[-1])
    dur = float(j['format']['duration'])
    problems = []
    if (v['width'], v['height']) != (1080, 1920): problems.append(f"size {v['width']}x{v['height']}")
    if abs(dur - src_dur) > 0.5: problems.append(f'duration {dur:.1f}s vs {src_dur:.1f}s')
    if not has_audio: problems.append('no audio')
    if not -16 <= lufs <= -10: problems.append(f'loudness {lufs} LUFS')
    return {'lufs': lufs, 'duration': round(dur, 2), 'problems': problems}


def render(edit_id):
    c = db()
    e = c.execute('SELECT * FROM edits WHERE id=?', (edit_id,)).fetchone()
    row = c.execute('SELECT * FROM raw_clips WHERE id=?', (e['raw_id'],)).fetchone()
    pr = c.execute('SELECT * FROM preps WHERE raw_id=?', (e['raw_id'],)).fetchone()
    ed = editor(e['editor'])
    ep = int(row['episode']) if row['episode'] == int(row['episode']) else row['episode']
    out_dir = os.path.join(WORK, 'out', e['editor'])
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{ep}_{ed['name']}_{row['subject'].strip()}_v{e['version']}.mp4")
    subprocess.run([os.path.join(HERE, 'lib', 'render_clip.sh'), e['editor'], str(e['raw_id']), e['hook'], e['song'], str(e['music_start'] or 0), out],
                   check=True, capture_output=True)
    q = qa(out, pr['duration'])
    d = json.loads(e['decision'])
    safe = not d['risk_flags'] and d['opening_raises_question'] and not d['topical'] and not e['note']
    # Matt, 2026-11-23: "Automated posts should be safest stuff." Anything flagged waits for a human, even with auto_post on.
    status = 'qa_failed' if q['problems'] else ('approved' if ed.get('auto_post') and safe else 'ready')
    c.execute('UPDATE edits SET status=?, path=?, qa=?, updated=? WHERE id=?', (status, out, json.dumps(q), time.time(), edit_id))
    c.commit()
    if status == 'ready' and ed.get('review_dir'):  # copy for human review (e.g. a Drive folder Natalie sees)
        rd = os.path.expanduser(ed['review_dir'])
        os.makedirs(rd, exist_ok=True)
        subprocess.run(['cp', out, rd], check=True)
    log(f"{e['editor']} render edit {edit_id} -> {status} {os.path.basename(out)} {q}")
    return status


def set_status(edit_id, status):
    c = db()
    c.execute('UPDATE edits SET status=?, updated=? WHERE id=?', (status, time.time(), edit_id))
    c.commit()
    log(f'edit {edit_id} -> {status}')


# ---------- queue / post / track ----------
def backend(name):
    sys.path.insert(0, HERE)
    return importlib.import_module(f'backends.{name}')


def next_slots(ed, taken, n):
    """Next n free posting times from the editor's daily slot list (local time)."""
    out, day, now = [], dt.date.today(), time.time()
    while len(out) < n:
        for hm in ed['slots']:
            h, m = map(int, hm.split(':'))
            t = dt.datetime.combine(day, dt.time(h, m)).timestamp()
            if t > now + 300 and t not in taken and t not in out:
                out.append(t)
                if len(out) == n:
                    break
        day += dt.timedelta(days=1)
    return out


def queue():
    c = db()
    for ed in editors():
        pfs = [pf for pf in ed.get('platforms', []) if ed['backend'] == 'dryrun' or ed.get('accounts', {}).get(pf)]
        if not pfs:
            continue  # no connected accounts yet: approved edits wait
        edits = c.execute("SELECT id FROM edits WHERE editor=? AND status='approved' ORDER BY id", (ed['id'],)).fetchall()
        if not edits:
            continue
        taken = {r[0] for r in c.execute("SELECT DISTINCT slot FROM posts WHERE editor=? AND status IN ('queued','posted')", (ed['id'],))}
        for e, slot in zip(edits, next_slots(ed, taken, len(edits))):
            for pf in pfs:
                c.execute('INSERT INTO posts(edit_id,editor,platform,account,slot,backend,status) VALUES(?,?,?,?,?,?,?)',
                          (e['id'], ed['id'], pf, ed.get('accounts', {}).get(pf, ''), slot, ed['backend'], 'queued'))
            c.execute("UPDATE edits SET status='queued', updated=? WHERE id=?", (time.time(), e['id']))
            log(f"{ed['id']} queue edit {e['id']} for {dt.datetime.fromtimestamp(slot):%a %d %b %H:%M} on {', '.join(pfs)}")
    c.commit()


def post():
    """Publish every due edit once, to all of its platforms in one backend call."""
    c = db()
    due = c.execute("SELECT p.*, e.path, e.decision, e.hook FROM posts p JOIN edits e ON e.id=p.edit_id "
                    "WHERE p.status='queued' AND p.slot<=? ORDER BY p.slot", (time.time(),)).fetchall()
    by_edit = {}
    for p in due:
        by_edit.setdefault(p['edit_id'], []).append(p)
    for eid, rows in by_edit.items():
        d = json.loads(rows[0]['decision'])
        caption = d['post_caption'] + '\n\n' + ' '.join('#' + h.lstrip('#') for h in d['hashtags'])
        targets = [{'platform': p['platform'], 'account': p['account']} for p in rows]
        try:
            res = backend(rows[0]['backend']).publish(rows[0]['path'], caption, targets, {'title': rows[0]['hook'], 'idempotency': f'edit-{eid}'})
        except Exception as ex:
            res = {t['platform']: {'status': 'failed', 'error': str(ex)} for t in targets}
        for p in rows:
            r = res.get(p['platform'], {})
            st = local_status(r.get('status'))
            c.execute('UPDATE posts SET status=?, remote_id=?, url=?, error=?, posted_at=? WHERE id=?',
                      (st, r.get('id'), r.get('url'), r.get('error'), time.time(), p['id']))
            log(f"{p['editor']} {st} edit {eid} {p['platform']} via {p['backend']} ({r.get('status')}): {r.get('url') or r.get('id') or ''} {r.get('error') or ''}")
        if not c.execute("SELECT 1 FROM posts WHERE edit_id=? AND status!='posted'", (eid,)).fetchone():
            c.execute("UPDATE edits SET status='posted', updated=? WHERE id=?", (time.time(), eid))
        c.commit()


def local_status(remote):
    return 'posted' if remote == 'published' else 'failed' if remote in ('failed', 'cancelled', None) else 'publishing'


def reconcile():
    """Re-check posts the service was still processing, and fill in post URLs once they exist."""
    c = db()
    rows = c.execute("SELECT * FROM posts WHERE remote_id IS NOT NULL AND (status='publishing' OR (status='posted' AND url IS NULL AND posted_at>?))",
                     (time.time() - 7 * 86400,)).fetchall()
    for p in rows:
        try:
            r = backend(p['backend']).status(p['remote_id']).get(p['platform'])
        except Exception as ex:
            log(f"reconcile post {p['id']} failed: {ex}")
            continue
        if not r:
            continue
        st = local_status(r['status'])
        if st != p['status'] or (r.get('url') and r['url'] != p['url']):
            c.execute('UPDATE posts SET status=?, url=COALESCE(?, url), error=? WHERE id=?', (st, r.get('url'), r.get('error'), p['id']))
            log(f"{p['editor']} edit {p['edit_id']} {p['platform']}: {p['status']} -> {st} {r.get('url') or ''} {r.get('error') or ''}")
        if not c.execute("SELECT 1 FROM posts WHERE edit_id=? AND status!='posted'", (p['edit_id'],)).fetchone():
            c.execute("UPDATE edits SET status='posted', updated=? WHERE id=?", (time.time(), p['edit_id']))
    c.commit()


def takedown(target, reason=''):
    """Pull a clip everywhere: target is a raw clip id, 'edit:<id>', or a post URL.
    Blocks the raw clip from future use, cancels every queued post of it, removes live posts where the API allows,
    and writes a checklist of links that must be deleted by hand (TikTok, Instagram)."""
    c = db()
    if target.startswith('http'):
        r = c.execute('SELECT e.raw_id FROM posts p JOIN edits e ON e.id=p.edit_id WHERE p.url=?', (target,)).fetchone()
    elif target.startswith('edit:'):
        r = c.execute('SELECT raw_id FROM edits WHERE id=?', (int(target[5:]),)).fetchone()
    else:
        r = c.execute('SELECT id AS raw_id FROM raw_clips WHERE id=?', (int(target),)).fetchone()
    if not r:
        sys.exit(f'nothing found for {target}')
    raw = r['raw_id']
    clip = os.path.basename(c.execute('SELECT path FROM raw_clips WHERE id=?', (raw,)).fetchone()[0])
    c.execute('INSERT OR REPLACE INTO blocked VALUES(?,?,?)', (raw, reason, time.time()))
    n = c.execute("UPDATE posts SET status='cancelled' WHERE status='queued' AND edit_id IN (SELECT id FROM edits WHERE raw_id=?)", (raw,)).rowcount
    c.execute("UPDATE edits SET status='taken_down', updated=? WHERE raw_id=? AND status IN ('decided','ready','approved','queued','posted','qa_failed')", (time.time(), raw))
    manual = []
    for p in c.execute("SELECT p.*, e.editor FROM posts p JOIN edits e ON e.id=p.edit_id WHERE e.raw_id=? AND p.status IN ('posted','publishing')", (raw,)).fetchall():
        ed = editor(p['editor'])
        try:
            backend(p['backend']).unpublish(p['remote_id'], p['platform'])
            c.execute("UPDATE posts SET status='removed' WHERE id=?", (p['id'],))
            log(f"takedown: removed {p['platform']} {p['url']}")
        except Exception as ex:
            c.execute("UPDATE posts SET status='remove_by_hand' WHERE id=?", (p['id'],))
            manual.append((p['platform'], ed.get('handles', {}).get(p['platform'], p['account']), p['url'] or f"(no link yet, post {p['remote_id']})"))
    c.commit()
    out = os.path.join(WORK, 'takedowns', f"{dt.datetime.now():%Y-%m-%d_%H%M}_{clip.rsplit('.', 1)[0]}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w') as f:
        f.write(f"# Takedown: {clip}\n\n{reason}\n\nCancelled {n} scheduled posts. Delete these by hand, then run `flywheel.py removed <url>` (or tick here):\n\n")
        f.writelines(f"- [ ] {pf} {h}: {u}\n" for pf, h, u in manual)
    log(f"takedown raw {raw} ({clip}): blocked, {n} queued posts cancelled, {len(manual)} to delete by hand -> {out}")
    for m in manual:
        print('  delete by hand:', *m)


def removed(url):
    c = db()
    c.execute("UPDATE posts SET status='removed' WHERE url=?", (url,))
    c.commit()


def accounts():
    """List the accounts connected in each backend that editors use, to fill editor.json 'accounts'."""
    for name in sorted({e['backend'] for e in editors()} | {'zernio'}):
        try:
            for a in backend(name).list_accounts():
                print(name, a.get('platform'), a.get('_id'), a.get('username') or a.get('displayName') or a.get('name') or '')
        except Exception as ex:
            print(name, 'error:', ex)


def track():
    """Snapshot metrics at about 24 h, 72 h and 7 days after posting."""
    c = db()
    for p in c.execute("SELECT * FROM posts WHERE status='posted' AND remote_id IS NOT NULL").fetchall():
        age = (time.time() - p['posted_at']) / 3600
        have = [r[0] for r in c.execute('SELECT age_h FROM metrics WHERE post_id=?', (p['id'],))]
        due = [h for h in (24, 72, 168) if age >= h and not any(abs(x - h) < h * 0.5 for x in have)]
        if not due:
            continue
        try:
            m = backend(p['backend']).metrics(p['remote_id'], p['platform'], p['account'])
        except Exception as ex:
            log(f"track post {p['id']} failed: {ex}")
            continue
        if m and m.get('url') and not p['url']:
            c.execute('UPDATE posts SET url=? WHERE id=?', (m['url'], p['id']))
        if m:
            c.execute('INSERT INTO metrics VALUES(?,?,?,?,?,?,?,?,?,?,?)', (p['id'], time.time(), age, m.get('views'), m.get('likes'), m.get('comments'),
                                                                            m.get('shares'), m.get('saves'), m.get('avg_watch'), m.get('follows'), json.dumps(m)))
    c.commit()


def stats():
    """Latest lifetime numbers for the dashboard: every post under 30 days old (hourly under 7 days, else every 6 h),
    plus one follower count per account per day. At network scale switch posts to Zernio's analytics delta feed."""
    c = db()
    now = time.time()
    rows = c.execute("SELECT p.*, s.taken_at seen FROM posts p LEFT JOIN post_stats s ON s.post_id=p.id "
                     "WHERE p.status='posted' AND p.remote_id IS NOT NULL AND p.posted_at>?", (now - 30 * 86400,)).fetchall()
    groups = {}
    for p in rows:
        every = 3600 if now - p['posted_at'] < 7 * 86400 else 6 * 3600
        if not p['seen'] or now - p['seen'] >= every:
            groups.setdefault((p['backend'], p['remote_id']), []).append(p)
    for (be, rid), ps in groups.items():
        fn = getattr(backend(be), 'post_stats', None)
        if not fn:
            continue
        try:
            got = fn(rid)
        except Exception as ex:
            log(f'stats {rid} failed: {ex}')
            continue
        for p in ps:
            m = got.get(p['platform'])
            if m:
                c.execute('INSERT OR REPLACE INTO post_stats VALUES(?,?,?,?,?,?,?,?)', (p['id'], now, m.get('views'), m.get('likes'),
                          m.get('comments'), m.get('shares'), m.get('saves'), json.dumps(m)))
                if m.get('url') and not p['url']:
                    c.execute('UPDATE posts SET url=? WHERE id=?', (m['url'], p['id']))
    day = dt.date.today().isoformat()
    for be in {e['backend'] for e in editors()}:
        fn = getattr(backend(be), 'account_info', None)
        if fn and not c.execute('SELECT 1 FROM account_stats WHERE day=? AND taken_at>?', (day, now - 6 * 3600)).fetchone():
            try:
                for a in fn():
                    c.execute('INSERT OR REPLACE INTO account_stats VALUES(?,?,?,?,?,?)', (a['id'], a['platform'], a['username'], day, a['followers'], now))
            except Exception as ex:
                log(f'account stats failed: {ex}')
    c.commit()


# ---------- the unattended pass ----------
def next_raw(c, eid):
    return c.execute("SELECT r.id FROM raw_clips r WHERE NOT EXISTS (SELECT 1 FROM edits e WHERE e.raw_id=r.id AND e.editor=?) "
                     "AND r.id NOT IN (SELECT raw_id FROM blocked) "
                     "ORDER BY (r.origin='new') DESC, r.mtime DESC LIMIT 1", (eid,)).fetchone()


def run():
    lock = open(os.path.join(WORK, '.run.lock'), 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return log('run already in progress; skipping')
    beat = os.path.join(WORK, 'last_run.json')
    started = time.time()
    json.dump({'started': started, 'finished': None}, open(beat, 'w'))
    scan()
    c = db()
    for ed in editors():
        if not ed.get('enabled', True):
            continue
        try:
            for e in c.execute("SELECT id FROM edits WHERE editor=? AND status='decided'", (ed['id'],)).fetchall():
                render(e['id'])
            tries = 0
            # keep `buffer` safe clips lined up; stop making new ones while the review queue is full
            count = lambda sts: c.execute(f"SELECT COUNT(*) FROM edits WHERE editor=? AND status IN ({sts})", (ed['id'],)).fetchone()[0]
            while (tries < 4 and count("'approved','queued'") < ed.get('buffer', 3)
                   and count("'ready'") < ed.get('max_review', 10)):
                tries += 1
                r = next_raw(c, ed['id'])
                if not r:
                    break
                eid = decide(r['id'], ed['id'])
                if c.execute('SELECT status FROM edits WHERE id=?', (eid,)).fetchone()[0] == 'decided':
                    render(eid)
                c.commit()
        except Exception as ex:
            log(f"{ed['id']} ERROR {ex}\n{traceback.format_exc()}")
    for step in (queue, post, reconcile, track, stats, clean_cache):
        try:
            step()
        except Exception as ex:
            log(f'{step.__name__} ERROR {ex}\n{traceback.format_exc()}')
    json.dump({'started': started, 'finished': time.time()}, open(beat, 'w'))


def status():
    c = db()
    for r in c.execute('SELECT folder, origin, COUNT(*) n FROM raw_clips GROUP BY folder, origin ORDER BY folder'):
        print(f"{r['folder']:10} {r['origin']:8} {r['n']}")
    print('prepped:', c.execute('SELECT COUNT(*) FROM preps').fetchone()[0])
    for r in c.execute('SELECT editor, status, COUNT(*) n FROM edits GROUP BY editor, status ORDER BY editor'):
        print(f"edits {r['editor']:10} {r['status']:10} {r['n']}")
    for r in c.execute("SELECT e.id, e.editor, e.status, e.hook, e.note FROM edits e WHERE e.status IN ('ready','qa_failed') ORDER BY e.id"):
        print(f"review edit {r['id']} {r['editor']} {r['status']}: {r['hook']}{'  [' + r['note'] + ']' if r['note'] else ''}")
    for r in c.execute("SELECT p.editor, p.platform, p.status, p.slot, e.hook FROM posts p JOIN edits e ON e.id=p.edit_id "
                       "WHERE p.status NOT IN ('posted') OR p.posted_at>? ORDER BY p.slot", (time.time() - 86400 * 2,)):
        print(f"post  {r['editor']:10} {r['platform']:10} {r['status']:7} {dt.datetime.fromtimestamp(r['slot']):%a %d %b %H:%M}  {r['hook']}")


if __name__ == '__main__':
    a = sys.argv[1:] or ['']
    cmds = {'takedown': lambda: takedown(a[1], ' '.join(a[2:])), 'removed': lambda: removed(a[1]), 'scan': scan, 'status': status, 'accounts': accounts, 'reconcile': reconcile, 'queue': queue, 'post': post, 'track': track, 'stats': stats, 'run': run,
            'prep': lambda: prep(a[1]), 'render': lambda: render(int(a[1])),
            'approve': lambda: set_status(int(a[1]), 'approved'), 'reject': lambda: set_status(int(a[1]), 'rejected'),
            'decide': lambda: decide(int(a[1]), a[2], a[a.index('--manual') + 1] if '--manual' in a else None)}
    cmds.get(a[0], lambda: sys.exit(__doc__))()
