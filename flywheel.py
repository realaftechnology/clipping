#!/usr/bin/env python3
"""Real AF AI editors: turns Natalie's raw clips into posts, then learns from results.
Architecture: Projects/Real AF/Context/Thoughts/2026-11-23 Short-Form Content Flywheel - Architecture.md

  flywheel.py scan              register raw clips from the source folders (first run = backlog)
  flywheel.py prep <id|path>    probe, transcribe, plan the 9:16 crop for one raw clip
  flywheel.py status            counts by folder and state
  flywheel.py decide <raw_id> <editor> [--manual file.json]   hook, song, caption (Claude, headless)
  flywheel.py render <edit_id>  render + QA one decided edit
  flywheel.py approve <edit_id> [note] | reject <edit_id> <why>  review gate; reasons teach the editors
  flywheel.py takedown <raw_id|edit:id|post url> [reason]    pull a clip from every account
  flywheel.py accounts          list accounts connected in Zernio (ids for editor.json)
  flywheel.py queue | post | track                              schedule ready edits, publish due posts, pull metrics
  flywheel.py stats             refresh latest post numbers and follower counts for the dashboard
  flywheel.py run               one full unattended pass for every editor (what the scheduler calls)
Output recipe: Context/AI Editors - Output Spec.md. Editors: editors/<editor>/editor.json + style.json.
  flywheel.py learn-performance [--force]   rewrite the performance playbook now (runs daily on its own)
  flywheel.py refs [title]      study new clips in the High Performers folder, then refresh the playbook and content profile
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
    c = sqlite3.connect(DB, timeout=30)
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
    CREATE TABLE IF NOT EXISTS feedback(
      id INTEGER PRIMARY KEY, edit_id INTEGER REFERENCES edits(id), editor TEXT, action TEXT, reason TEXT, at REAL);
    CREATE TABLE IF NOT EXISTS rules(
      id INTEGER PRIMARY KEY, kind TEXT, rule TEXT, reason TEXT, source_edit INTEGER, at REAL, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS refs(
      id INTEGER PRIMARY KEY, path TEXT UNIQUE, category TEXT, title TEXT, views INTEGER, duration REAL, width INTEGER, height INTEGER,
      transcript TEXT, analysis TEXT, analyzed_at REAL, added REAL);
    CREATE TABLE IF NOT EXISTS spread(raw_id INTEGER PRIMARY KEY, from_edit INTEGER, lift REAL, at REAL);
    CREATE TABLE IF NOT EXISTS post_stats(
      post_id INTEGER PRIMARY KEY REFERENCES posts(id), taken_at REAL, views INTEGER, likes INTEGER, comments INTEGER,
      shares INTEGER, saves INTEGER, raw TEXT);
    CREATE TABLE IF NOT EXISTS account_stats(
      account TEXT, platform TEXT, username TEXT, day TEXT, followers INTEGER, taken_at REAL, PRIMARY KEY(account, day));
    ''')
    if 'editor' not in [r[1] for r in c.execute('PRAGMA table_info(rules)')]:
        c.execute('ALTER TABLE rules ADD COLUMN editor TEXT')  # NULL = every editor; set only for stylistic rules
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
                 'post_caption', 'hashtags', 'risk_flags', 'caption_fixes', 'profile_verdict', 'profile_basis', 'trim'],
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
        'risk_flags': {'type': 'array', 'items': {'type': 'string'}},
        'profile_verdict': {'type': 'string', 'enum': ['approve', 'reject', 'unclear'],
                            'description': 'what the content profile says Matt would do with this clip'},
        'profile_basis': {'type': 'string'},
        'trim': {'type': 'object', 'additionalProperties': False, 'required': ['start', 'end', 'remove', 'summary'], 'properties': {
            'start': {'type': 'integer'}, 'end': {'type': 'integer'}, 'summary': {'type': 'string'},
            'remove': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'required': ['from', 'to', 'why'],
                                                  'properties': {'from': {'type': 'integer'}, 'to': {'type': 'integer'}, 'why': {'type': 'string'}}}}}},
        'caption_fixes': {'type': 'array', 'description': 'corrections to the on-screen caption words',
                          'items': {'type': 'object', 'additionalProperties': False, 'required': ['index', 'text'],
                                    'properties': {'index': {'type': 'integer'}, 'text': {'type': 'string'}}}}}}


def decide_prompt(row, prep_row, ed, pool):
    spec = open(SPEC).read()
    rules = spec[spec.index('## Hook rules'):spec.index('## Choices the editor makes per clip')]
    exclusions = 'FIRST, check the hard exclusions. If any applies, set excluded=true and stop caring about the rest.\n' + \
        spec[spec.index('## Hard exclusions'):spec.index('## Hook rules')]
    playbook = playbook_for(ed['id'])
    songs_txt = '\n'.join(f"- {r['file']} | {r['song']} | {r['mood']} | energy {r['energy']}" for r in pool)
    q3 = '"' * 3
    return f"""You are {ed['name']}, an AI short-form editor for Andy Frisella's Real AF podcast.
Natalie (human editor) cut this raw clip. Decide the text hook, the music bed and the post caption. Output JSON only.

{exclusions}
{rules}
Performance playbook (what made past clips win or flop, learned from every editor's posts):
{playbook}

Content profile: what Matt approves and rejects, learned from every review decision. Applies to every editor.
{content_profile()}

Standing rules learned from review (follow every one):
{standing_rules(ed['id'])}

Review decisions so far (Matt and Natalie):
{review_lessons(ed['id'])}

Raw clip: {os.path.basename(row['path'])} (folder {row['folder']}, {prep_row['duration']:.0f} s, added {(time.time() - row['mtime']) / 86400:.0f} days ago)
{'' if is_fresh(row['mtime']) else "This is an ARCHIVE clip, used only because there's nothing fresh to post. It must be evergreen: always true, not tied to any news, event, season, date or period. If it depends on any time period at all, set topical=true."}
Transcript (auto, may lack punctuation):
{q3}{prep_row['transcript']}{q3}

Songs you may use (instrumentals; pick one whose mood fits; file name must match exactly):
{songs_txt}

Also:
- usable=false only if the clip is unusable (cut mid-thought with no payoff, mostly crosstalk, unclear audio); say why in skip_reason.
- topical=true if it depends on a news event or date that will go stale.
- profile_verdict: judge the clip against the content profile. "approve" only if it clearly matches what Matt has
  approved before; "reject" if it clearly matches what he rejects; otherwise "unclear". profile_basis: the profile line
  you relied on. With "approve", leave risk_flags empty for anything the profile already settles.
- risk_flags: subject matter Natalie should see first (politics, a named private person, medical or legal claims, likely out-of-context reading). Swearing is never a risk flag on its own; only flag what the clip is about or how it could be read. Empty list if none.
- post_caption: one or two plain sentences that extend the hook's question without answering it; no emoji. hashtags: 3 to 5, lowercase, no '#'.
- alt_hooks: 2 more hooks that follow the rules.
- caption_fixes: the on-screen captions are these words (index:word): {caption_tokens(row['id'])}
  Fix only clear transcription mistakes (a misheard or misspelled word, a wrong name) and anything the standing rules
  require: [{{index, text}}], text "" removes the word. Never change how Andy actually talks, and don't censor swearing
  (the captions do that automatically). [] if nothing needs fixing.
- trim: tighten the clip using the same word indices. Matt: "No clip should start mid-sentence, but the editors are
  permitted to trim clips as needed to remove dead space, to remove filler words, repeated words... They are not
  permitted to cut off sentences. We want the pacing to be fluid and smooth. So if you close your eyes, it sounds like it
  could have been said as one cohesive sentence... But they are allowed to cut where it adds to the video."
  start: index of the first word to keep; it MUST begin a sentence (drop a half-sentence the clip opens on, or start at
  0). end: index of the last word to keep; it MUST end a sentence (drop a trailing half-sentence). remove: word ranges
  {{from, to, why}} to cut inside: filler (um, uh, like, you know, I mean), stutters and repeated words, false starts,
  and whole sentences or asides that slow it down, only when what's left still reads as one natural, continuous
  thought. Never remove part of a sentence that leaves it broken or changes its meaning. Never cut the payoff. Dead
  air is shortened automatically; don't list pauses. summary: what you cut, in a few words. If it's already tight:
  start 0 (or the first sentence start), end = last sentence end, remove []."""


def ask_claude(prompt, schema=None):
    cmd = ['claude', '-p', prompt, '--output-format', 'json', '--tools', '', '--no-session-persistence',
           '--setting-sources', '', '--json-schema', json.dumps(schema or DECISION_SCHEMA)]
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
    if d.get('caption_fixes') and d.get('usable') and not d['excluded']:
        fix_captions(raw_id, d['caption_fixes'])
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
    elif d.get('profile_verdict') == 'reject':
        status, note = 'skipped', f"content profile: Matt rejects clips like this ({d.get('profile_basis', '')})"
    elif d['topical'] and age_days > CFG.get('topical_max_age_days', 14):
        status, note = 'skipped', f'topical clip, {age_days:.0f} days old'
    elif d['risk_flags'] or (not d['opening_raises_question'] and d.get('profile_verdict') != 'approve'):
        # once the content profile says Matt approves clips like this, a non-question opening no longer holds it
        note = '; '.join(d['risk_flags'] + ([] if d['opening_raises_question'] or d.get('profile_verdict') == 'approve'
                                            else ['opening does not raise a question']))
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
    d = json.loads(e['decision'])
    env, src_dur = dict(os.environ), pr['duration']
    if d.get('trim'):
        t = tighten(e, row, pr, d['trim'])
        if t:
            env.update(TRIM_SRC=t['src'], TRIM_WORDS=t['words'], TRIM_DUR=str(t['duration']))
            src_dur = t['duration']
    subprocess.run([os.path.join(HERE, 'lib', 'render_clip.sh'), e['editor'], str(e['raw_id']), e['hook'], e['song'], str(e['music_start'] or 0), out],
                   check=True, capture_output=True, env=env, stdin=subprocess.DEVNULL)
    q = qa(out, src_dur)
    safe = not d['risk_flags'] and (d['opening_raises_question'] or d.get('profile_verdict') == 'approve') and not d['topical'] and not e['note']
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


def review(edit_id, action, reason=''):
    """A human's review decision. Every rejection keeps its reason, and so does approving a clip the editor had
    flagged; both feed the editors' next decisions (review_lessons), so fewer clips get rejected over time."""
    c = db()
    e = c.execute('SELECT editor, note FROM edits WHERE id=?', (edit_id,)).fetchone()
    if not e:
        sys.exit(f'no edit {edit_id}')
    status = 'approved' if action == 'approve' else 'rejected'
    c.execute('UPDATE edits SET status=?, updated=? WHERE id=?', (status, time.time(), edit_id))
    if action == 'reject' or e['note']:
        c.execute('INSERT INTO feedback(edit_id,editor,action,reason,at) VALUES(?,?,?,?,?)', (edit_id, e['editor'], action, reason.strip(), time.time()))
    c.commit()
    log(f"edit {edit_id} -> {status}" + (f': {reason.strip()}' if reason.strip() else ''))
    # learn in the background so the dashboard isn't kept waiting: triage a rejection, then refresh the content profile
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    if action == 'reject' or e['note']:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), 'learn', str(edit_id), action], cwd=HERE, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=open(os.path.join(os.path.dirname(LOG), 'triage.out'), 'a'), stderr=subprocess.STDOUT)


def review_lessons(eid, n=25):
    """Past review decisions, for the decide prompt."""
    c = db()
    rows = c.execute("SELECT f.*, e.hook, e.note, p.transcript FROM feedback f JOIN edits e ON e.id=f.edit_id "
                     "LEFT JOIN preps p ON p.raw_id=e.raw_id ORDER BY f.at DESC LIMIT ?", (n * 2,)).fetchall()
    rej = [r for r in rows if r['action'] == 'reject'][:n]
    ok = [r for r in rows if r['action'] == 'approve' and not (r['note'] or '').startswith('Re-edited')][:10]
    tag = lambda r: '' if r['editor'] == eid else f" ({r['editor']})"
    out = []
    if rej:
        out.append('Clips a human REJECTED, with their reason. Learn the pattern: skip clips like these (usable=false) '
                   'or write the hook differently, so this doesn\'t happen again:')
        out += [f"- hook \"{r['hook']}\"{tag(r)}; clip: \"{(r['transcript'] or '')[:160]}\"; reason: {r['reason'] or '(none given)'}" for r in rej]
    if ok:
        out.append('Clips a human APPROVED even though they were flagged. These flags were too cautious; don\'t raise them for similar clips:')
        out += [f"- hook \"{r['hook']}\"{tag(r)}; flagged: {r['note']}" + (f"; note: {r['reason']}" if r['reason'] else '') for r in ok]
    return '\n'.join(out) or '(no review decisions yet)'


def set_status(edit_id, status):
    c = db()
    c.execute('UPDATE edits SET status=?, updated=? WHERE id=?', (status, time.time(), edit_id))
    c.commit()
    log(f'edit {edit_id} -> {status}')


# ---------- learning from a rejection ----------
def _change(with_id):
    props = {'applies': {'type': 'boolean'}, 'hook': {'type': 'string'}, 'song': {'type': 'string'},
             'caption_fixes': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'required': ['index', 'text'],
                                                          'properties': {'index': {'type': 'integer'}, 'text': {'type': 'string'}}}},
             'what_changed': {'type': 'string'}}
    if with_id:
        props = {'edit_id': {'type': 'integer'}, **props}
    return {'type': 'object', 'additionalProperties': False, 'required': list(props), 'properties': props}


TRIAGE_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['kind', 'scope', 'summary', 'rule', 'rejected', 'others'],
    'properties': {
        'kind': {'type': 'string', 'enum': ['edit', 'scrap']},
        'scope': {'type': 'string', 'enum': ['all', 'style']},
        'summary': {'type': 'string'},
        'rule': {'type': 'string'},
        'rejected': _change(False),
        'others': {'type': 'array', 'items': _change(True)},
    },
}


def standing_rules(eid=None, n=40):
    """Rules for every editor, plus this editor's own stylistic ones (Matt: only style differs between editors)."""
    rows = db().execute('SELECT kind, rule FROM rules WHERE active=1 AND (editor IS NULL OR editor=?) ORDER BY at DESC LIMIT ?', (eid, n)).fetchall()
    return '\n'.join(f"- [{'editing' if r['kind'] == 'edit' else 'clip choice'}] {r['rule']}" for r in rows) or '(none yet)'


def words_path(raw_id):
    return os.path.join(WORK, 'prep', str(raw_id), 'words.json')


def caption_tokens(raw_id):
    """The caption words exactly as rendered, numbered so fixes can point at them."""
    toks = json.load(open(words_path(raw_id)))['transcription']
    return ' '.join(f"{i}:{t['text'].strip()}" for i, t in enumerate(toks) if t['text'].strip())


def fix_captions(raw_id, fixes):
    p = words_path(raw_id)
    keep = p.replace('words.json', 'words.orig.json')
    if not os.path.exists(keep):
        shutil.copy(p, keep)
    d = json.load(open(p))
    for f in fixes:
        if 0 <= f['index'] < len(d['transcription']):
            d['transcription'][f['index']]['text'] = (' ' + f['text'].strip()) if f['text'].strip() else ''
    json.dump(d, open(p, 'w'))


def triage_prompt(rej, reason, others, pool):
    spec = open(SPEC).read()
    rules = spec[spec.index('## Hook rules'):spec.index('## What posts automatically')]
    def item(e):
        d = json.loads(e['decision'])
        return (f"edit_id {e['id']} | hook: \"{e['hook']}\" | song: {e['song']} | post caption: \"{d.get('post_caption', '')}\"\n"
                f"  caption words (index:word, as shown on screen in caps): {caption_tokens(e['raw_id'])}")
    songs_txt = '\n'.join(f"- {r['file']} | {r['mood']}" for r in pool)
    return f"""You run quality control for AI short-form editors of Andy Frisella's Real AF podcast.
Matt (the owner) just REJECTED a finished clip and said why. Work out what he meant and act on it.

His reason: \"\"\"{reason}\"\"\"

The rejected clip:
{item(rej)}

1. kind: "edit" if the problem is in how the clip was edited (hook text, typo or wrong word in captions, music choice,
   anything an editor can change); "scrap" if the clip itself is the problem (boring, no value, wrong subject, weak
   moment): no edit can save it.
   scope: "style" only if it's purely about this editor's look (fonts, colours, caption or hook styling, music taste);
   everything else (subject matter, quality, spelling, hooks, clip choice) is "all": it applies to every editor.
2. summary: one plain sentence saying what he objected to.
3. rule: one standing rule, at most two sentences, that every editor will follow on all future clips so this doesn't
   happen again. For "scrap", which clips to skip; for "edit", what to do differently. Keep it exactly as broad as his
   reason: don't add requirements he didn't state.
4. rejected: for "edit", the fix for this clip: applies=true and set only what changes (hook: new hook text or "";
   song: a file name from the song list or ""; caption_fixes: [{{index, text}}] to correct caption words, text "" deletes
   the word; what_changed: short plain description). Change only what his reason covers; never rewrite how Andy talks
   ("gonna", "ya know" etc. stay unless he said otherwise). If the problem isn't actually in this clip, applies=false
   and say so. If the fix needs something these tools can't change (crop, trim,
   caption style, fonts), applies=false and say so in what_changed. For "scrap": applies=false, everything else empty.
5. others: check EVERY clip below that is still waiting for review.
   - For "edit": applies=true only if the same problem is clearly in that clip (cite it in what_changed), with its own
     fix (same fields, same narrow scope). When in doubt, applies=false: a needless re-edit costs Matt a review.
   - For "scrap": applies=true if it's similar enough that Matt would likely scrap it too; what_changed says why; no fixes.
   Return one entry per clip, with its edit_id.

Hook rules (any new hook must follow them):
{rules}

Song list (file | mood):
{songs_txt}

Clips waiting for review:
""" + ('\n'.join(item(e) for e in others) or '(none)')


def reedit(e, ch, why):
    """New version of an edit with the given fixes, rendered and sent back to review."""
    c = db()
    d = json.loads(e['decision'])
    hook, song, mstart = e['hook'], e['song'], e['music_start']
    if ch.get('hook'):
        hook = d['hook'] = ch['hook']
    if ch.get('song') and ch['song'] in {r['file'] for r in songs()} and ch['song'] != e['song']:
        try:
            mstart, song = music_start(ch['song']), ch['song']
            d['song'] = song
        except ValueError:
            pass
    if ch.get('caption_fixes'):
        fix_captions(e['raw_id'], ch['caption_fixes'])
    v = (c.execute('SELECT MAX(version) FROM edits WHERE raw_id=? AND editor=?', (e['raw_id'], e['editor'])).fetchone()[0] or 0) + 1
    note = f"Re-edited ({why}): {ch.get('what_changed', '')}".strip()
    cur = c.execute('INSERT INTO edits(raw_id,editor,version,status,decision,hook,song,music_start,note,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (e['raw_id'], e['editor'], v, 'decided', json.dumps(d), hook, song, mstart, note, time.time(), time.time()))
    if e['status'] in ('ready', 'qa_failed'):
        c.execute("UPDATE edits SET status='superseded', updated=? WHERE id=?", (time.time(), e['id']))
    c.commit()
    render(cur.lastrowid)  # the note keeps it in review: a human sees every re-edit
    return cur.lastrowid


def triage(edit_id):
    lock = open(os.path.join(WORK, '.triage.lock'), 'w')
    fcntl.flock(lock, fcntl.LOCK_EX)  # one rejection at a time
    c = db()
    rej = c.execute('SELECT * FROM edits WHERE id=?', (edit_id,)).fetchone()
    fb = c.execute("SELECT reason FROM feedback WHERE edit_id=? AND action='reject' ORDER BY at DESC LIMIT 1", (edit_id,)).fetchone()
    if not rej or not fb or not fb['reason']:
        return log(f'triage edit {edit_id}: no rejection reason, nothing to do')
    others = c.execute("SELECT * FROM edits WHERE status IN ('ready','qa_failed') AND id!=? ORDER BY id", (edit_id,)).fetchall()
    t = ask_claude(triage_prompt(rej, fb['reason'], others, songs()), TRIAGE_SCHEMA)
    c.execute('INSERT INTO rules(kind,rule,reason,source_edit,at,editor) VALUES(?,?,?,?,?,?)',
              (t['kind'], t['rule'], fb['reason'], edit_id, time.time(), rej['editor'] if t['scope'] == 'style' else None))
    c.commit()
    log(f"triage edit {edit_id}: {t['kind']} - {t['summary']} | new rule: {t['rule']}")
    by_id = {e['id']: e for e in others}
    if t['kind'] == 'edit':
        if t['rejected'].get('applies'):
            try:
                log(f"triage: edit {edit_id} re-edited as edit {reedit(rej, t['rejected'], f'you rejected edit {edit_id}')}")
            except Exception as ex:
                log(f'triage: re-edit of {edit_id} failed: {ex}')
        else:
            log(f"triage: edit {edit_id} can't be fixed automatically: {t['rejected'].get('what_changed', '')}")
        for o in t['others']:
            e = by_id.get(o.get('edit_id'))
            if e and o.get('applies'):
                try:
                    log(f"triage: same problem in edit {e['id']}; re-edited as edit {reedit(e, o, f'same issue as edit {edit_id}')}")
                except Exception as ex:
                    log(f"triage: re-edit of {e['id']} failed: {ex}")
    else:
        for o in t['others']:
            e = by_id.get(o.get('edit_id'))
            if e and o.get('applies'):
                flag = f"looks like edit {edit_id} you scrapped: {o.get('what_changed', '')}".strip()
                c.execute('UPDATE edits SET note=? WHERE id=?', ('; '.join(x for x in (e['note'], flag) if x), e['id']))
                log(f"triage: edit {e['id']} flagged as similar to scrapped edit {edit_id}")
        c.commit()


# ---------- the content profile: what Matt approves and rejects, for every editor ----------
PROFILE = os.path.join(HERE, '..', 'Context', 'AI Editors - Content Profile.md')  # private project notes, not the public repo


def content_profile():
    return open(PROFILE).read() if os.path.exists(PROFILE) else '(empty: no review decisions yet)'


PROFILE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['profile', 'changed'],
                  'properties': {'profile': {'type': 'string'}, 'changed': {'type': 'string'}}}


def update_profile():
    """Rewrite the content profile from every review decision so far. Matt's goal: editors so tuned that almost nothing
    needs review, and what does almost always gets approved."""
    c = db()
    rows = c.execute("SELECT f.action, f.reason, f.editor, e.hook, e.note, p.transcript FROM feedback f JOIN edits e ON e.id=f.edit_id "
                     "LEFT JOIN preps p ON p.raw_id=e.raw_id ORDER BY f.at").fetchall()
    dec = '\n'.join(f"- {r['action'].upper()} | hook \"{r['hook']}\" | editor flagged: {r['note'] or 'nothing'} | Matt's reason: {r['reason'] or '(none)'}"
                    f" | clip: \"{(r['transcript'] or '')[:400]}\"" for r in rows[-150:])
    prompt = f"""You keep the CONTENT PROFILE for a network of AI short-form editors posting Andy Frisella clips.
It tells every editor what Matt (the owner) approves and what he rejects, so the editors can decide on their own:
skip what he'd reject, post what he'd approve, and only send him what is genuinely new or unclear.
Matt: "I want the video editing models to learn that certain political things are fine and other political things are not
fine. So it's not just politics or out of context or controversial. It's certain things and not other things."

Matt's written rules always win: the hard exclusions and the hook rules in the Output Spec (text hooks must raise a
question) are never relaxed here. Never list as approved anything a hard exclusion covers (any mention of Israel, Jews,
Gaza and the rest), even when a saved high performer or an approval includes it; leave it out. The editor's flag "opening does not raise a question" is about Andy's first spoken
line, not the text hook. Reasons Matt or Natalie typed outweigh what you infer from an approval.

Rewrite the profile from all review decisions below. Be specific: name the kinds of takes, topics and framings, not broad
categories ("criticising both parties' tribalism: approved" rather than "politics: mixed"). Every line must be backed by
the decisions; don't invent preferences. Note how many decisions back each line, e.g. (2 approved). Keep anything under
"## Matt's own rules" exactly as written. Markdown, under about 500 words, these sections:
## Approved: post without review
## Rejected: skip these clips
## Unclear: still send to review
## Quality standards
## Matt's own rules

Matt's saved high performers (clips he keeps as winners, so their content counts as approved; strong evidence for which
political takes are fine; weight by views, 100k = 1, 1M = 2, 10M = 3):
{json.dumps([{k: r[k] for k in ('title', 'folder', 'views', 'weight', 'topic', 'take')} for r in refs_for_prompt()])[:60000]}

Current profile:
{content_profile()}

Review decisions, oldest first (Matt's or Natalie's; the editor's own flag shows what it was worried about):
{dec or '(none)'}"""
    out = ask_claude(prompt, PROFILE_SCHEMA)
    body = out['profile'].strip()
    if body.startswith('# '):
        body = body.split('\n', 1)[-1].lstrip()
    head = ("# AI Editors: Content Profile\n\nRewritten automatically after each review decision "
            f"(last {dt.datetime.now():%Y-%m-%d %H:%M}). Applies to every editor. Edit freely; "
            "text under \"Matt's own rules\" is kept as is.\n\n")
    open(PROFILE, 'w').write(head + body + '\n')
    log(f"content profile updated: {out['changed']}")


def learn(edit_id, action):
    if action == 'reject':
        triage(edit_id)
    lock = open(os.path.join(WORK, '.profile.lock'), 'w')
    fcntl.flock(lock, fcntl.LOCK_EX)
    update_profile()


# ---------- learning from performance (Matt, 2026-11-23) ----------
# "if editor 1 has a video that goes viral and there's a pattern that starts to play out of similar videos going viral
# and similar videos flopping, then all 9 other editors should also learn from that information without changing their
# stylistic choices"
PLAYBOOK = os.path.join(HERE, '..', 'Context', 'AI Editors - Performance Playbook.md')  # private notes, not the public repo
NETWORK_H, STYLE_H = '## Every editor', '## Style notes: '


def read_playbook():
    """{'network': text, 'style': {editor: text}} from the markdown (Matt can edit it)."""
    out = {'network': '', 'style': {}}
    if not os.path.exists(PLAYBOOK):
        return out
    cur = None
    for line in open(PLAYBOOK).read().split('\n'):
        if line.startswith(NETWORK_H):
            cur = 'network'
            continue
        if line.startswith(STYLE_H):
            cur = line[len(STYLE_H):].strip()
            out['style'][cur] = ''
            continue
        if line.startswith('# '):
            cur = None
        elif cur == 'network':  # '###' (and stray '##') sub-headings stay inside their section
            out['network'] += line + '\n'
        elif cur:
            out['style'][cur] += line + '\n'
    return out


def has_lessons():
    return bool(re.search(r'^\s*- ', read_playbook()['network'], re.M))


def playbook_for(eid):
    pb = read_playbook()
    style = pb['style'].get(eid, '').strip()
    return ((pb['network'].strip() or '(no lessons yet)') +
            (f"\n\nYour own style notes (music and look only; never change your style because of another editor's):\n{style}" if style else ''))


def perf_rows(c):
    """One row per posted edit with its views relative to that account's usual (lift), plus what the clip was."""
    now = time.time()
    posts = c.execute("SELECT p.id, p.edit_id, p.editor, p.platform, p.account, p.posted_at, s.views, s.likes, s.comments, s.shares, s.saves "
                      "FROM posts p JOIN post_stats s ON s.post_id=p.id WHERE p.status='posted' AND s.views IS NOT NULL").fetchall()
    base = {}
    for p in posts:
        if now - p['posted_at'] > 48 * 3600:
            base.setdefault(p['account'], []).append(p['views'])
    med = {a: max(1, statistics.median(v)) for a, v in base.items()}
    by_edit = {}
    for p in posts:
        g = by_edit.setdefault(p['edit_id'], {'edit_id': p['edit_id'], 'editor': p['editor'], 'posted_at': p['posted_at'], 'platforms': {}})
        g['platforms'][p['platform']] = {k: p[k] for k in ('views', 'likes', 'comments', 'shares', 'saves')}
        if p['account'] in med:
            g['platforms'][p['platform']]['lift'] = round(p['views'] / med[p['account']], 2)
    out = []
    for eid, g in by_edit.items():
        e = c.execute("SELECT e.hook, e.song, e.decision, r.folder, r.subject, r.mtime, pr.duration, pr.transcript FROM edits e "
                      "JOIN raw_clips r ON r.id=e.raw_id LEFT JOIN preps pr ON pr.raw_id=e.raw_id WHERE e.id=?", (eid,)).fetchone()
        lifts = [x['lift'] for x in g['platforms'].values() if 'lift' in x]
        d = json.loads(e['decision'] or '{}')
        out.append({**g, 'lift': round(statistics.mean(lifts), 2) if lifts else None, 'age_h': round((now - g['posted_at']) / 3600),
                    'hook': e['hook'], 'song': e['song'], 'mood': d.get('mood'), 'folder': e['folder'], 'subject': e['subject'],
                    'duration': round(e['duration'] or 0), 'posted': dt.datetime.fromtimestamp(g['posted_at']).strftime('%a %H:%M'),
                    'kind': 'fresh' if g['posted_at'] - (e['mtime'] or 0) < CFG.get('fresh_days', 7) * 86400 else 'evergreen',
                    'transcript': (e['transcript'] or '')[:500]})
    return out


PERF_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['network', 'style', 'spread', 'changed'], 'properties': {
    'network': {'type': 'string'},
    'style': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'required': ['editor', 'notes'],
                                         'properties': {'editor': {'type': 'string'}, 'notes': {'type': 'string'}}}},
    'spread': {'type': 'array', 'items': {'type': 'integer'}},
    'changed': {'type': 'string'}}}


def learn_performance(force=False):
    """Daily: rewrite the performance playbook from every editor's results."""
    c = db()
    rows = perf_rows(c)
    judged = [r for r in rows if r['lift'] is not None and r['age_h'] >= 48]
    need = CFG.get('learn_min_posts', 5)
    stamp = os.path.join(WORK, 'last_learn.json')
    json.dump({'at': time.time(), 'judged': len(judged)}, open(stamp, 'w'))
    refs_ = refs_for_prompt()
    if len(judged) < need and not force and not refs_:
        if not os.path.exists(PLAYBOOK):
            open(PLAYBOOK, 'w').write(f"# AI Editors: Performance Playbook\n\nNot enough results yet: lessons start once {need} posts are at least "
                                      f"2 days old.\n\n{NETWORK_H}\n\n")
        return log(f'learn: {len(judged)} posts old enough to judge (need {need}); playbook unchanged')
    styles = {e['id']: json.load(open(os.path.join(e['dir'], 'style.json'))) for e in editors()}
    prompt = f"""You keep the PERFORMANCE PLAYBOOK for a network of AI short-form editors posting Andy Frisella clips. Each editor
runs its own brand set of accounts in its own style. Matt (the owner): "if editor 1 has a video that goes viral and
there's a pattern that starts to play out of similar videos going viral and similar videos flopping, then all 9 other
editors should also learn from that information without changing their stylistic choices, like their style."

Below is every post. lift = views divided by that account's usual (median) views, so 1.0 is normal, 3.0 is three times
normal (a winner), 0.3 is a flop. Judge winners and flops by lift, not raw views. Posts under 48 h old are early: use
them only as weak signals.

Write:
- network: lessons for EVERY editor about the content: which topics, takes, kinds of moments, hook patterns, spoken
  openings, clip lengths, fresh vs archive clips and posting times win or flop. Markdown bullets. Tag each lesson with its
  evidence: "(seen once)" is a hunch; "(pattern: 3 wins)" is a rule. Never turn one viral post into a rule. Keep lessons
  that still hold; drop ones the data now contradicts. Never contradict Matt's written rules (hook rules, hard
  exclusions, content profile): performance can't make a rejected kind of clip acceptable. Under about 400 words.
  Use '###' for any sub-headings (never '#' or '##'). Matt's trimming rule: a clip never starts mid-sentence or cuts a
  sentence off (editors may trim filler, repeats and dead air); write opening lessons within that rule.
- style: per editor, bullets only about music and look that its own results support (style is per editor and is never
  copied from another editor). Empty string when there's nothing yet.
- spread: edit ids of clear winners (lift of 3 or more, at least 48 h old) that other editors should re-cut in their own style.
- changed: one or two sentences on what changed since the current playbook.

Editors and their styles: {json.dumps({k: {x: v[x] for x in ('caption_font', 'hook_font') if x in v} for k, v in styles.items()})}

Proven high performers: finished clips from Real AF accounts, cut by human editors, with their real view counts (Matt's
"High Performers" folder). They're strong evidence about CONTENT: topic, take, opening, hook, structure, length. Their
captions and look come from other editors, so never take style lessons from them. Weight each one by its views (the
weight field: 100k = 1, 1M = 2, 10M = 3): a lesson from the 12M clip counts for far more than one from a 100k clip, and a
lesson shared by several big clips is the strongest of all. Tag lessons with the views behind them, e.g.
"(3 high performers, 15.6M views)", and order lessons by that weight. Where your own posts and these agree, say so; where there are no posts yet, the lessons rest on these.
{json.dumps(refs_)[:150000] if refs_ else '(none yet)'}

Current playbook:
{open(PLAYBOOK).read() if os.path.exists(PLAYBOOK) else '(none)'}

Your editors' posts (newest first):
{json.dumps(sorted(rows, key=lambda r: -r['posted_at']), default=str)[:60000]}"""
    out = ask_claude(prompt, PERF_SCHEMA)
    md = [f"# AI Editors: Performance Playbook\n",
          f"Rewritten daily from every editor's results and the High Performers folder (last {dt.datetime.now():%Y-%m-%d %H:%M}, "
          f"{len(judged)} posts judged, {len(refs_)} high performers). "
          f"Content lessons apply to every editor; style notes apply only to their own editor.\n",
          f"{NETWORK_H}\n", out['network'].strip() + '\n']
    for st in out['style']:
        if st['notes'].strip():
            md += [f"{STYLE_H}{st['editor']}\n", st['notes'].strip() + '\n']
    open(PLAYBOOK, 'w').write('\n'.join(md))
    for eid in out['spread']:
        r = next((x for x in rows if x['edit_id'] == eid), None)
        raw = c.execute('SELECT raw_id FROM edits WHERE id=?', (eid,)).fetchone()
        if r and raw:
            c.execute('INSERT OR IGNORE INTO spread VALUES(?,?,?,?)', (raw[0], eid, r['lift'], time.time()))
    c.commit()
    log(f"learn: playbook updated ({len(judged)} posts judged, {len(refs_)} high performers): {out['changed']}")


def learn_due():
    p = os.path.join(WORK, 'last_learn.json')
    return not os.path.exists(p) or time.time() - json.load(open(p))['at'] > 20 * 3600


# ---------- proven winners: Matt's "High Performers" folder ----------
# Finished clips from Real AF accounts with their view counts in the file name, e.g. "Big Problem (1.6M).mp4".
# Every editor studies them for content (topic, take, opening, hook, structure); never for style, which stays per editor.
def parse_views(name):
    m = re.search(r'\(\s*([\d.]+)\s*([kKmM]?)\s*\+?\s*\)', name)
    if not m:
        return None
    return int(float(m.group(1)) * {'k': 1e3, 'm': 1e6, '': 1}[m.group(2).lower()])


def scan_refs():
    root = os.path.expanduser(CFG.get('references_root', ''))
    if not root or not os.path.isdir(root):
        return log(f'refs: no High Performers folder at {root!r}')
    c, n = db(), 0
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith(('.mp4', '.mov')):
                path = os.path.join(dirpath, fn)
                title = re.sub(r'\s*\([^)]*\)\s*$', '', os.path.splitext(fn)[0]).strip()
                cat = os.path.relpath(dirpath, root).split(os.sep)[0] if dirpath != root else ''
                cur = c.execute('INSERT OR IGNORE INTO refs(path,category,title,views,added) VALUES(?,?,?,?,?)', (path, cat, title, parse_views(fn), time.time()))
                n += cur.rowcount
    c.commit()
    log(f'refs: {n} new high performers registered')


REF_SCHEMA = {'type': 'object', 'additionalProperties': False,
              'required': ['hook_text', 'captions', 'look', 'topic', 'take', 'opening_line', 'structure', 'why_it_worked', 'lessons'],
              'properties': {k: {'type': 'string'} for k in ('hook_text', 'captions', 'look', 'topic', 'take', 'opening_line', 'structure', 'why_it_worked')} |
              {'lessons': {'type': 'array', 'items': {'type': 'string'}}}}


def analyze_ref(rid):
    c = db()
    r = c.execute('SELECT * FROM refs WHERE id=?', (rid,)).fetchone()
    work = os.path.join(WORK, 'refs', str(rid))
    os.makedirs(work, exist_ok=True)
    clip = os.path.join(work, 'clip' + os.path.splitext(r['path'])[1])
    if not os.path.exists(clip) or os.path.getsize(clip) != os.path.getsize(r['path']):
        shutil.copy(r['path'], clip + '.part')
        if os.path.getsize(clip + '.part') != os.path.getsize(r['path']):
            raise RuntimeError(f'incomplete copy of {r["path"]}')
        os.replace(clip + '.part', clip)
    w, h, _, dur = probe(clip)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', clip, '-vn', '-ac', '1', '-ar', '16000', f'{work}/audio.wav'], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(['whisper-cli', '-m', os.path.expanduser(CFG['whisper_model']), '-f', f'{work}/audio.wav', '-np', '-otxt', '-of', f'{work}/words'],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    transcript = open(f'{work}/words.txt').read().strip()
    frames = []
    for k, t in enumerate([0.4, 2.0, dur * 0.4, dur * 0.75]):
        fp = f'{work}/frame{k}.jpg'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{t:.2f}', '-i', clip, '-frames:v', '1', '-vf', 'scale=540:-2', fp], check=True, stdin=subprocess.DEVNULL)
        frames.append(os.path.basename(fp))
    prompt = f"""Study a proven short-form clip from a Real AF account (Andy Frisella). It got {r['views']:,} views.
Title Matt gave it: "{r['title']}" (folder: {r['category']}). {dur:.0f} s, {w}x{h}.

First Read the frames in this folder ({', '.join(frames)}: 0.4 s, 2 s, 40% and 75% through) to see the on-screen hook
text, captions and look. Then, with the transcript, explain what made it work.

Transcript (auto, may lack punctuation):
\"\"\"{transcript}\"\"\"

Fields: hook_text (any title/hook text on screen, exactly; "" if none), captions (caption styling), look (framing, crop,
graphics, anything visual), topic, take (Andy's actual position in one sentence), opening_line (first spoken sentence),
structure (how the clip builds, e.g. claim > story > payoff), why_it_worked (specific), lessons (3-6 short content
lessons another editor could apply to a different clip; content only, not visual style)."""
    cmd = ['claude', '-p', prompt, '--output-format', 'json', '--tools', 'Read', '--allowedTools', 'Read', '--no-session-persistence',
           '--setting-sources', '', '--json-schema', json.dumps(REF_SCHEMA)]
    out = json.loads(subprocess.run(cmd, capture_output=True, text=True, cwd=work, timeout=600).stdout or '{}')
    if out.get('is_error') or 'structured_output' not in out:
        raise RuntimeError(f"analysis failed: {str(out.get('result'))[:300]}")
    c.execute('UPDATE refs SET duration=?, width=?, height=?, transcript=?, analysis=?, analyzed_at=? WHERE id=?',
              (dur, w, h, transcript, json.dumps(out['structured_output']), time.time(), rid))
    c.commit()
    os.remove(clip)  # keep the transcript, frames and analysis; the video stays in Drive
    log(f"refs: analysed \"{r['title']}\" ({r['views']:,} views): {out['structured_output']['why_it_worked'][:140]}")


def refs_cmd(only=None):
    """Register the High Performers folder, analyse what's new, then refresh the playbook and the content profile."""
    scan_refs()
    c = db()
    todo = c.execute('SELECT id, title FROM refs WHERE analyzed_at IS NULL' + (' AND title LIKE ?' if only else '') + ' ORDER BY views DESC',
                     ((f'%{only}%',) if only else ())).fetchall()
    for r in todo:
        try:
            analyze_ref(r['id'])
        except Exception as ex:
            log(f"refs: {r['title']} failed: {ex}")
    if todo:
        learn_performance(force=True)
        update_profile()


def refs_for_prompt(limit=60):
    # Matt: "They should be weighted based on their view counts." weight = log scale: 100k -> 1, 1M -> 2, 10M -> 3
    import math
    rows = db().execute('SELECT * FROM refs WHERE analyzed_at IS NOT NULL ORDER BY views DESC LIMIT ?', (limit,)).fetchall()
    return [{'title': r['title'], 'folder': r['category'], 'views': r['views'],
             'weight': round(max(0.5, 1 + math.log10(max(r['views'] or 1, 1) / 1e5)), 2), 'seconds': round(r['duration'] or 0),
             **{k: (v[:700] if k == 'why_it_worked' else v) for k, v in json.loads(r['analysis']).items() if k not in ('captions', 'look')}}
            for r in rows]


# ---------- trimming (Matt, 2026-11-23) ----------
END = re.compile(r'[.?!]["\')\]]*$')


def snap_trim(tokens, plan):
    """Enforce whole sentences: start on a sentence start, end on a sentence end; drop cuts outside the kept range."""
    words = [t['text'].strip() for t in tokens]
    n = len(words)
    start = max(0, min(plan.get('start', 0), n - 1))
    while start > 0 and not END.search(words[start - 1] or '.'):  # back up to the start of this sentence
        start -= 1
    while start < n and not words[start]:
        start += 1
    end = max(start, min(plan.get('end', n - 1), n - 1))
    while end < n - 1 and not END.search(words[end] or ''):     # run on to the end of this sentence
        end += 1
    remove = [r for r in plan.get('remove', []) if start <= r['from'] <= r['to'] <= end]
    return {'start': start, 'end': end, 'remove': remove}


def tighten(e, row, pr, plan):
    """Tightened source for one edit (lib/trim.py). Returns {'src', 'words', 'duration'} or None if untouched."""
    words_p = os.path.join(WORK, 'prep', str(e['raw_id']), 'words.json')
    tokens = json.load(open(words_p))['transcription']
    p = snap_trim(tokens, plan)
    if p['start'] == 0 and p['end'] >= len(tokens) - 1 and not p['remove'] and not plan.get('dead_air', True):
        return None
    work = os.path.join(WORK, 'trim', str(e['id']))
    os.makedirs(work, exist_ok=True)
    json.dump(p, open(os.path.join(work, 'plan.json'), 'w'))
    src = local_copy(row)
    crop = json.loads(pr['crop']) if pr['crop'] and pr['crop'] != 'null' else None
    cmd = [sys.executable, os.path.join(HERE, 'lib', 'trim.py'), src, words_p, os.path.join(work, 'plan.json'), work]
    if crop:
        cmd += ['--crop', f"{crop['x']},{crop['w']}"]
    r = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip().splitlines()[-1])
    log(f"edit {e['id']} trimmed {pr['duration']:.1f}s -> {r['duration']:.1f}s ({r['cuts']} cuts): {plan.get('summary', '')}")
    return {'src': os.path.join(work, 'source.mov'), 'words': os.path.join(work, 'words.json'), 'duration': r['duration']}


# ---------- queue / post / track ----------
def backend(name):
    sys.path.insert(0, HERE)
    return importlib.import_module(f'backends.{name}')


# ---------- cadence (Matt, 2026-11-23) ----------
# At least one post a day on every account. Volume above one a day only when there are enough fresh clips to last
# until the next expected episode (Mon, Tue, Fri). A day with no fresh clip gets one evergreen clip from the archive,
# a different one for each editor.
DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def is_fresh(mtime):
    return mtime > time.time() - CFG.get('fresh_days', 7) * 86400


def days_to_next_episode(day):
    """Days from `day` (counted) up to the next expected episode day (not counted). Mon->1 (Tue), Tue->3 (Fri), Fri->3 (Mon)."""
    eps = {DAYS.index(d) for d in CFG.get('episode_days', ['Mon', 'Tue', 'Fri'])}
    n, d = 1, day + dt.timedelta(days=1)
    while d.weekday() not in eps and n < 7:
        n, d = n + 1, d + dt.timedelta(days=1)
    return n


def spread(slots, q):
    """q of the editor's daily slots, evenly spread: 1 -> mid-day, 2 -> late morning and late afternoon, 5 -> all."""
    s = sorted(slots)
    return [s[int((i + 0.5) * len(s) / q)] for i in range(q)]


def at(day, hm):
    h, m = map(int, hm.split(':'))
    return dt.datetime.combine(day, dt.time(h, m)).timestamp()


def fresh_supply(c, eid):
    """Fresh clips this editor can still post: approved and unposted, plus fresh raw clips it hasn't edited yet."""
    ready = c.execute("SELECT COUNT(*) FROM edits e JOIN raw_clips r ON r.id=e.raw_id WHERE e.editor=? AND e.status='approved' AND r.mtime>?",
                      (eid, time.time() - CFG.get('fresh_days', 7) * 86400)).fetchone()[0]
    todo = c.execute("SELECT COUNT(*) FROM raw_clips r WHERE r.mtime>? AND r.id NOT IN (SELECT raw_id FROM blocked) "
                     "AND NOT EXISTS (SELECT 1 FROM edits e WHERE e.raw_id=r.id AND e.editor=?)",
                     (time.time() - CFG.get('fresh_days', 7) * 86400, eid)).fetchone()[0]
    return ready + todo


def daily_quota(c, ed, day, posted_today_fresh):
    days = days_to_next_episode(day)
    total = posted_today_fresh + fresh_supply(c, ed['id'])
    return max(1, min(len(ed['slots']), -(-total // days)))  # ceil: stretch fresh clips to the next episode


def queue(now=None):
    """Plan today's posts for every editor: fresh clips by quota, else one evergreen clip so no day is empty."""
    c = db()
    now = now or time.time()
    today = dt.date.fromtimestamp(now)
    t0, t1 = at(today, '00:00'), at(today + dt.timedelta(days=1), '00:00')
    for ed in editors():
        pfs = [pf for pf in ed.get('platforms', []) if ed['backend'] == 'dryrun' or ed.get('accounts', {}).get(pf)]
        if not pfs or not ed.get('slots'):
            continue
        todays = c.execute("SELECT DISTINCT p.edit_id, p.slot, r.mtime FROM posts p JOIN edits e ON e.id=p.edit_id JOIN raw_clips r ON r.id=e.raw_id "
                           "WHERE p.editor=? AND p.slot>=? AND p.slot<? AND p.status IN ('queued','posted','publishing')", (ed['id'], t0, t1)).fetchall()
        quota = daily_quota(c, ed, today, sum(1 for r in todays if r['mtime'] and is_fresh(r['mtime'])))
        approved = c.execute("SELECT e.id, e.raw_id, r.mtime FROM edits e JOIN raw_clips r ON r.id=e.raw_id WHERE e.editor=? AND e.status='approved' "
                             "ORDER BY r.mtime DESC, e.id", (ed['id'],)).fetchall()
        # an archive clip can be used by several editors, but never by two of them on the same day
        others_today = {r[0] for r in c.execute("SELECT e.raw_id FROM posts p JOIN edits e ON e.id=p.edit_id WHERE p.editor!=? AND p.slot>=? "
                                                "AND p.slot<? AND p.status IN ('queued','posted','publishing')", (ed['id'], t0, t1))}
        fresh = [e['id'] for e in approved if is_fresh(e['mtime'])]
        evergreen = [e['id'] for e in approved if not is_fresh(e['mtime']) and e['raw_id'] not in others_today]
        taken = {r['slot'] for r in todays}
        open_slots = [t for t in (at(today, hm) for hm in spread(ed['slots'], quota)) if t > now + 300 and t not in taken]
        planned = []
        need = quota - len(todays)
        for t in open_slots:
            if need <= 0 or not fresh:
                break
            planned.append((fresh.pop(0), t))
            need -= 1
        if not todays and not planned and evergreen:  # nothing fresh today: one evergreen clip keeps the day covered
            later = [t for t in (at(today, hm) for hm in sorted(ed['slots'])) if t > now + 300 and t not in taken]
            planned.append((evergreen.pop(0), (open_slots or later or [now + 600])[0]))
        for eid, slot in planned:
            for pf in pfs:
                c.execute('INSERT INTO posts(edit_id,editor,platform,account,slot,backend,status) VALUES(?,?,?,?,?,?,?)',
                          (eid, ed['id'], pf, ed.get('accounts', {}).get(pf, ''), slot, ed['backend'], 'queued'))
            c.execute("UPDATE edits SET status='queued', updated=? WHERE id=?", (time.time(), eid))
            kind = 'evergreen' if eid not in [e['id'] for e in approved if is_fresh(e['mtime'])] else 'fresh'
            log(f"{ed['id']} queue edit {eid} ({kind}; {quota}/day until next episode) for {dt.datetime.fromtimestamp(slot):%a %d %b %H:%M} on {', '.join(pfs)}")
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
def next_fresh(c, eid):
    """Newest fresh raw clip this editor hasn't edited."""
    return c.execute("SELECT r.id FROM raw_clips r WHERE r.mtime>? AND r.id NOT IN (SELECT raw_id FROM blocked) "
                     "AND NOT EXISTS (SELECT 1 FROM edits e WHERE e.raw_id=r.id AND e.editor=?) ORDER BY r.mtime DESC LIMIT 1",
                     (time.time() - CFG.get('fresh_days', 7) * 86400, eid)).fetchone()


def next_evergreen(c, eid):
    """Archive clip this editor hasn't used. Order: another editor's winner (spread), then, once the playbook has
    lessons, the candidate that best fits them; otherwise newest. Other editors may use the same clip, just not on the
    same day (queue() enforces that); clips another editor currently has lined up go last to avoid clashes."""
    rows = c.execute("SELECT r.id, r.folder, r.episode, r.subject, (r.id IN (SELECT raw_id FROM spread)) hot FROM raw_clips r "
                     "WHERE r.mtime<=? AND r.id NOT IN (SELECT raw_id FROM blocked) "
                     "AND NOT EXISTS (SELECT 1 FROM edits e WHERE e.raw_id=r.id AND e.editor=?) "
                     "ORDER BY hot DESC, EXISTS (SELECT 1 FROM edits o WHERE o.raw_id=r.id AND o.editor!=? AND o.status IN ('approved','queued')), "
                     "r.mtime DESC LIMIT 30", (time.time() - CFG.get('fresh_days', 7) * 86400, eid, eid)).fetchall()
    if not rows:
        return None
    if rows[0]['hot'] or not has_lessons():
        return rows[0]
    try:
        pick = ask_claude(f"""Pick the archive clip most likely to perform, judging by the performance playbook below.
You only have each clip's file subject (Natalie's short title), show folder and episode number. Return its id.

Performance playbook:
{playbook_for(eid)}

Candidates (id | folder | episode | subject):
""" + '\n'.join(f"{r['id']} | {r['folder']} | {r['episode']} | {re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', r['subject'] or '')}" for r in rows),
                          {'type': 'object', 'additionalProperties': False, 'required': ['id', 'why'],
                           'properties': {'id': {'type': 'integer'}, 'why': {'type': 'string'}}})
        hit = next((r for r in rows if r['id'] == pick['id']), None)
        if hit:
            log(f"{eid} evergreen pick raw {hit['id']} ({hit['subject']}): {pick['why'][:160]}")
            return hit
    except Exception as ex:
        log(f'{eid} evergreen pick fell back to newest: {ex}')
    return rows[0]


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
            # every fresh clip gets edited; archive clips only to keep a small evergreen reserve for days with nothing fresh
            count = lambda sts: c.execute(f"SELECT COUNT(*) FROM edits WHERE editor=? AND status IN ({sts})", (ed['id'],)).fetchone()[0]
            reserve = lambda: c.execute("SELECT COUNT(*) FROM edits e JOIN raw_clips r ON r.id=e.raw_id WHERE e.editor=? AND e.status='approved' "
                                        "AND r.mtime<=?", (ed['id'], time.time() - CFG.get('fresh_days', 7) * 86400)).fetchone()[0]
            while tries < 4 and count("'ready'") < ed.get('max_review', 10):
                tries += 1
                r = next_fresh(c, ed['id']) or (next_evergreen(c, ed['id']) if reserve() < CFG.get('evergreen_reserve', 3) else None)
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
    if learn_due():  # once a day, after fresh stats
        try:
            learn_performance()
        except Exception as ex:
            log(f'learn ERROR {ex}\n{traceback.format_exc()}')
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
    cmds = {'takedown': lambda: takedown(a[1], ' '.join(a[2:])), 'removed': lambda: removed(a[1]), 'scan': scan, 'status': status, 'accounts': accounts, 'reconcile': reconcile, 'queue': queue, 'post': post, 'track': track, 'stats': stats, 'run': run, 'triage': lambda: triage(int(a[1])), 'learn': lambda: learn(int(a[1]), a[2]), 'profile': update_profile, 'learn-performance': lambda: learn_performance('--force' in a), 'refs': lambda: refs_cmd(a[1] if len(a) > 1 else None),
            'prep': lambda: prep(a[1]), 'render': lambda: render(int(a[1])),
            'approve': lambda: review(int(a[1]), 'approve', ' '.join(a[2:])), 'reject': lambda: review(int(a[1]), 'reject', ' '.join(a[2:])),
            'decide': lambda: decide(int(a[1]), a[2], a[a.index('--manual') + 1] if '--manual' in a else None)}
    cmds.get(a[0], lambda: sys.exit(__doc__))()
