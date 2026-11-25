#!/usr/bin/env python3
"""Real AF sourcing agent: takes a full podcast episode, finds its strongest self-contained moments and cuts each one into
a raw clip for the clipper agents (the AI editors in flywheel.py), who stylize and finish it for each platform.

Matt, 2026-11-23: "we're going to have a sourcing agent, meaning they take the full episode and they do what I just
described. And then we have clipper agents, which are the video editors. Their responsibility is to stylize and finish
the clip for distribution on the various platforms."

  sourcing.py episodes                      episodes in the exports folder and whether each has been sourced
  sourcing.py run                           source the newest unsourced episode (the loop calls this; one per run)
  sourcing.py episode <drive id|file> [--title "Ep 1076 QAF"]   source one episode now
  sourcing.py held                          clips waiting for a human to approve them
  sourcing.py approve <clip id> [note] | reject <clip id> <why>   hand a clip to the editors, or reject it (the reason teaches the agent)
  sourcing.py learn                         rewrite the sourcing profile from every review (runs after each review on its own)

Flow: download (one episode at a time; deleted afterwards) -> transcribe with word timings (whisper-cli) -> pick moments
(headless Claude, judged against the hard exclusions, content profile, performance playbook and high performers) ->
snap each to whole sentences and move the cut points into silence -> cut raw clips (no crop, captions or look: that is
the editors' job) -> review folder -> (approved) the approved folder, or the editors' show folder once Matt turns on
handoff_to_editors. auto_approve skips the review step.
Settings: config.json "sourcing"; the episodes folder id lives in config.local.json (episodes_folder_id).
"""
import datetime as dt, fcntl, glob, html, json, os, re, shutil, subprocess, sys, tempfile, time, traceback
import numpy as np

import flywheel as fw

CFG, WORK = fw.CFG, fw.WORK
S = dict({'enabled': True, 'review_folder': 'AI Sourced - Review', 'approved_folder': 'AI Sourced - Review/AI Sourced Approved',
          'handoff_to_editors': False, 'auto_approve': False,
          'max_clips': 40, 'min_seconds': 60, 'max_seconds': 180, 'since_episode': 0, 'model': None}, **CFG.get('sourcing', {}))
EP_NAME = re.compile(r'^Ep\s*(\d+(?:\.\d+)?)\s+([A-Za-z]+)', re.I)   # "Ep 1076 QAF.mp4", "Ep 1042 CTI_SP.mp4", "Ep 1034 CTI Live.mp4"
END = re.compile(r'[.?!]["\')\]]*$')
SRC_DIR = os.path.join(WORK, 'sourcing')


def db():
    c = fw.db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS episodes(
      id INTEGER PRIMARY KEY, ref TEXT UNIQUE, title TEXT, episode REAL, show TEXT, duration REAL, status TEXT,
      note TEXT, picks INTEGER, started REAL, finished REAL);
    CREATE TABLE IF NOT EXISTS sourced(
      id INTEGER PRIMARY KEY, episode_id INTEGER REFERENCES episodes(id), path TEXT, name TEXT, start REAL, end REAL,
      duration REAL, subject TEXT, first_line TEXT, last_line TEXT, why TEXT, payoff TEXT, topical INTEGER,
      risk_flags TEXT, confidence INTEGER, transcript TEXT, status TEXT, review_note TEXT, created REAL, reviewed REAL);
    ''')
    return c


def root():
    return os.path.expanduser(CFG['source_root'])


def approved_dir(show):
    """Where an approved moment goes. While Matt fine-tunes the agent it stays out of the editors' reach in the approved
    folder (Matt, 2026-11-25: "So they don't go to the editing team yet. I want to fine tune the sourcing agent before we
    hand it off to video editors"). With handoff_to_editors on, it goes into the show's clip folder for the editors."""
    return os.path.join(root(), show_folder(show) if S['handoff_to_editors'] else S['approved_folder'])


def show_folder(show):
    """The editors' clip folder for a show (QAF -> QAF, CTI -> CTI): approved moments go where Natalie's clips go, with
    the same <Episode>_<Show>_<Subject>.mp4 names, so the editors pick them up on their next pass (Matt, 2026-11-23)."""
    for f in CFG['source_folders']:
        if f.lower() == show.lower():
            return f
    raise ValueError(f'no clip folder for show {show!r} (source_folders: {", ".join(CFG["source_folders"])})')


# ---------- finding episodes ----------
def listing():
    """Episode files in the exports folder, newest first: [{ref (Drive id), title, episode, show}].
    The folder is link-shared, so its public listing works without a Google login or the Drive app (and from background
    jobs, unlike the Drive mount). Subfolders (Hot Takes and older layouts) are skipped."""
    fid = CFG.get('episodes_folder_id')
    if not fid:
        sys.exit('set episodes_folder_id in config.local.json')
    page = subprocess.run(['curl', '-sSL', '--max-time', '60', f'https://drive.google.com/embeddedfolderview?id={fid}'],
                          capture_output=True, text=True, check=True).stdout
    out = []
    for ref, href, title in re.findall(r'<div class="flip-entry" id="entry-([^"]+)".*?<a href="([^"]+)".*?flip-entry-title">([^<]*)<', page, re.S):
        title = html.unescape(title).strip()
        m = EP_NAME.match(title)
        if '/file/' in href and m and title.lower().endswith(('.mp4', '.mov')):
            out.append({'ref': ref, 'title': os.path.splitext(title)[0].strip(), 'episode': float(m[1]), 'show': m[2].upper()})
    if not out and 'flip-entry' not in page:
        raise RuntimeError('episodes folder listing came back empty; has its sharing changed?')
    return sorted(out, key=lambda e: -e['episode'])


def episodes_cmd():
    c = db()
    done = {r['ref']: r for r in c.execute('SELECT * FROM episodes')}
    for e in listing()[:25]:
        r = done.get(e['ref'])
        state = f"{r['status']} ({r['picks'] or 0} clips)" if r else ('not sourced' if e['episode'] >= S['since_episode'] else 'before since_episode')
        print(f"{e['episode']:>7g} {e['show']:5} {e['title']:28} {state}")


def fetch(ref, dst):
    """Download one episode by its Drive id; resumes, and checks the byte count so a cut-off download is never used."""
    url = f'https://drive.usercontent.google.com/download?id={ref}&export=download&confirm=t'
    head = subprocess.run(['curl', '-sSIL', url], capture_output=True, text=True, check=True).stdout
    size = int(re.findall(r'(?im)^content-length:\s*(\d+)', head)[-1])
    free = shutil.disk_usage(os.path.dirname(dst)).free
    if os.path.exists(dst) and os.path.getsize(dst) == size:
        return dst
    have = os.path.getsize(dst + '.part') if os.path.exists(dst + '.part') else 0
    if free < size - have + 5e9:
        raise RuntimeError(f'not enough disk for the episode: {(size - have) / 1e9:.1f} GB needed + 5 GB spare, {free / 1e9:.1f} GB free')
    subprocess.run(['curl', '-sSL', '--retry', '5', '-C', '-', '-o', dst + '.part', url], check=True)
    if os.path.getsize(dst + '.part') != size:
        raise IOError(f'incomplete episode download: {os.path.getsize(dst + ".part")} of {size} bytes')
    os.replace(dst + '.part', dst)
    return dst


# ---------- transcript ----------
def transcribe(src, out):
    """Word-level transcript (same whisper settings as the editors' prep) and the 16 kHz audio for finding silences."""
    wav = os.path.join(out, 'audio.wav')
    if not os.path.exists(os.path.join(out, 'words.json')):
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', wav], check=True)
        subprocess.run(['whisper-cli', '-m', os.path.expanduser(CFG['whisper_model']), '-f', wav, '-np', '-ml', '1', '-sow', '-oj',
                        '-of', os.path.join(out, 'words')], check=True, capture_output=True)
    elif not os.path.exists(wav):
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', wav], check=True)
    toks = [{'text': t['text'], 'from': t['offsets']['from'] / 1000, 'to': t['offsets']['to'] / 1000}
            for t in json.load(open(os.path.join(out, 'words.json')))['transcription']]
    return [t for t in toks if t['text'].strip() and not t['text'].strip().startswith('[')], wav


def sentences(toks):
    """Group words into sentences on end punctuation; a long silence also ends one (whisper sometimes drops a full stop)."""
    out, cur = [], []
    for i, t in enumerate(toks):
        if cur and t['from'] - toks[i - 1]['to'] > 2.0:
            out.append(cur)
            cur = []
        cur.append(t)
        if END.search(t['text'].strip()):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [{'start': s[0]['from'], 'end': s[-1]['to'], 'text': ''.join(w['text'] for w in s).strip()} for s in out]


def mmss(t):
    return f'{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}' if t >= 3600 else f'{int(t // 60)}:{int(t % 60):02d}'


# ---------- picking moments ----------
PICK_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['clips', 'passed_over'],
    'properties': {
        'clips': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['start', 'end', 'subject', 'why', 'payoff', 'standalone', 'topical', 'risk_flags', 'confidence'],
            'properties': {
                'start': {'type': 'integer', 'description': 'number of the first sentence kept'},
                'end': {'type': 'integer', 'description': 'number of the last sentence kept'},
                'subject': {'type': 'string', 'description': '1-3 words in CamelCase, like GetSerious, SnapBenefits, GTAVI'},
                'why': {'type': 'string', 'description': 'why this moment will hold a scrolling viewer'},
                'payoff': {'type': 'string', 'description': 'the line it lands on'},
                'standalone': {'type': 'string', 'description': 'why it needs no earlier context'},
                'topical': {'type': 'boolean', 'description': 'tied to a news event or date that will go stale'},
                'risk_flags': {'type': 'array', 'items': {'type': 'string'}},
                'confidence': {'type': 'integer', 'description': '1-10, how sure you are it performs'}}}},
        'passed_over': {'type': 'string', 'description': 'strong-sounding moments you left out and why, briefly'}}}


def spec_section(a, b):
    spec = open(fw.SPEC).read()
    return spec[spec.index(a):spec.index(b)]


def high_performers(n=12):
    rows = db().execute('SELECT title, category, views, duration, transcript FROM refs WHERE transcript IS NOT NULL ORDER BY views DESC LIMIT ?', (n,)).fetchall()
    return '\n'.join(f"- \"{r['title']}\" ({r['category']}, {r['views']:,} views, "
                     f"{r['duration'] or 0:.0f} s): \"{(r['transcript'] or '')[:700]}\"" for r in rows) or '(none yet)'


def sourcing_lessons(n=40):
    """Matt's review of earlier moments (approvals are noted as well as rejections), then what the editors did with
    the approved ones. Fed into every episode's picks so the agent learns what to source."""
    c = db()
    rows = c.execute("SELECT * FROM sourced WHERE status IN ('rejected','approved') ORDER BY reviewed DESC LIMIT ?", (n,)).fetchall()
    line = lambda r: f"- {r['name']} ({r['duration']:.0f} s): \"{(r['transcript'] or '')[:220]}\"" + (f" | reason: {r['review_note']}" if r['review_note'] else '')
    out = []
    rej, ok = [r for r in rows if r['status'] == 'rejected'], [r for r in rows if r['status'] == 'approved']
    if rej:
        out.append('Moments Matt REJECTED, with his reason. Learn the pattern and stop sourcing moments like these:')
        out += [line(r) for r in rej]
    if ok:
        out.append('Moments Matt APPROVED. Keep sourcing moments like these:')
        out += [line(r) for r in ok]
    later = c.execute("SELECT s.name, e.status, e.note, f.reason FROM sourced s JOIN raw_clips rc ON rc.path=s.path JOIN edits e ON e.raw_id=rc.id "
                      "LEFT JOIN feedback f ON f.edit_id=e.id WHERE e.status IN ('excluded','skipped','rejected') ORDER BY e.id DESC LIMIT ?", (n,)).fetchall()
    if later:
        out.append('Approved moments the editors later dropped or Matt rejected as a finished edit:')
        out += [f"- {r['name']}: editors {r['status']}: {r['reason'] or r['note'] or ''}"[:300] for r in later]
    return '\n'.join(out) or '(no decisions on sourced moments yet)'


# ---------- the sourcing profile: what Matt approves and rejects, rewritten after every review ----------
PROFILE = os.path.join(fw.CONTEXT, 'AI Editors - Sourcing Profile.md')  # private project notes, not the public repo
PROFILE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['profile', 'changed'],
                  'properties': {'profile': {'type': 'string'}, 'changed': {'type': 'string'}}}


def sourcing_profile():
    return open(PROFILE).read() if os.path.exists(PROFILE) else '(empty: no review decisions yet)'


def update_profile():
    """Rewrite the sourcing profile from every review of the agent's moments. Matt, 2026-11-23: "When I approve a clip,
    I want the sourcing agent to take note of the fact that I approve that clip. And if I reject it, I want it to ask me
    why I rejected it so it can learn what to source moving forward.\""""
    c = db()
    rows = c.execute("SELECT s.*, e.title FROM sourced s JOIN episodes e ON e.id=s.episode_id WHERE s.status IN ('approved','rejected') "
                     "ORDER BY s.reviewed").fetchall()
    if not rows:
        return
    later = {r['name']: f"editors {r['status']}: {r['reason'] or r['note'] or ''}" for r in c.execute(
        "SELECT s.name, e.status, e.note, f.reason FROM sourced s JOIN raw_clips rc ON rc.path=s.path JOIN edits e ON e.raw_id=rc.id "
        "LEFT JOIN feedback f ON f.edit_id=e.id WHERE e.status IN ('excluded','skipped','rejected','approved','posted')")}
    dec = '\n'.join(f"- {r['status'].upper()} | {r['title']} | {r['name']} ({r['duration']:.0f} s) | Matt's reason: {r['review_note'] or '(none)'}"
                    f" | agent's pitch: {r['why']} | flags: {r['risk_flags']} | later: {later.get(r['name'], '-')}"
                    f" | transcript: \"{(r['transcript'] or '')[:700]}\"" for r in rows[-150:])
    out = fw.ask_claude(f"""You keep the SOURCING PROFILE for the Real AF sourcing agent, which goes through full podcast episodes
(Andy Frisella) and pulls 1-3 minute raw moments for AI editors to repurpose. Matt reviews every moment; the profile tells
the agent what he approves and what he rejects, so over time almost every moment it pulls gets approved.

{MATT_BRIEF}

Rewrite the profile from ALL review decisions below. Be specific and practical: name the kinds of moments, deliveries,
structures and subjects (e.g. "Q&A answers that leave out the listener's question: rejected, the moment loses its context"),
not broad categories. Treat Matt's typed reasons as the strongest evidence; infer from approvals carefully. Note how many
decisions back each line, e.g. (3 rejected). Where something is borderline in Matt's own words, say so. Don't invent
preferences. The Output Spec's hard exclusions always apply and are not repeated here. Keep everything under
"## Matt's own rules" exactly as written. Markdown, under about 450 words, these sections:
## Source more of
## Don't source
## Borderline: pull only when it's strong
## Matt's own rules

Current profile:
{sourcing_profile()}

Review decisions, oldest first:
{dec}""", PROFILE_SCHEMA, timeout=600)
    body = out['profile'].strip()
    if body.startswith('# '):
        body = body.split('\n', 1)[-1].lstrip()
    head = ("# AI Editors: Sourcing Profile\n\nWhat Matt approves and rejects from the sourcing agent, rewritten automatically after "
            f"each review (last {dt.datetime.now():%Y-%m-%d %H:%M}). The agent reads it before every episode. Edit freely; text under "
            "\"Matt's own rules\" is kept as is.\n\n")
    open(PROFILE, 'w').write(head + body + '\n')
    fw.log(f"sourcing profile updated: {out['changed'][:300]}")


def learn():
    lock = open(os.path.join(WORK, '.sourcing-profile.lock'), 'w')
    fcntl.flock(lock, fcntl.LOCK_EX)
    update_profile()


MATT_BRIEF = """Matt, 2026-11-23: "these are meant to be segmented moments that could be turned into a variety of different
things. So these aren't just 30-second clips from the show. They are meant to be raw moments that can be repurposed. So
your job is essentially to take out anything of no substance. The clips should realistically be between 1 minute to 3
minutes long of moments. So what we basically want to do is, if there's anything that could potentially be a clip that is
not outside of the guardrails of what we want shared on short form, then it should be clipped out of the show and moved to
the next phase of approval... these raw clips, which are going to be 16:9, not vertical, they need to have the leeway to be
edited into different things. So for X, we're going to be posting them pretty much directly onto X with a caption... But
we also want the ability to take that same clip and make it vertical and stylize it any way we want to and trim it down
for short form consumption. So we're basically pulling moments from the show, not necessarily short form clips." """


def pick_prompt(ep, sents):
    lo, hi, n = S['min_seconds'], S['max_seconds'], S['max_clips']
    numbered = '\n'.join(f"[{i} {mmss(s['start'])}] {s['text']}" for i, s in enumerate(sents))
    return f"""You are the SOURCING AGENT for Real AF's AI content network (Andy Frisella's podcast). You go through the whole
episode and pull out every MOMENT worth repurposing. Each becomes a raw 16:9 clip that a human approves; then it is posted
almost as is on X with a caption, and clipper agents (the AI editors) cut it down, make it vertical and stylize it for
short form. Output JSON only.

{MATT_BRIEF}

Episode: {ep['title']} (show {ep['show']}), {mmss(ep['duration'])} long. The transcript is automatic, numbered by
sentence with its start time. Andy is the host; other voices are co-hosts, guests or listener questions.

Segment the episode into moments. Be thorough: anything of substance that stays inside the guardrails below becomes a
moment (typically 10-30 per episode, at most {n}). What you leave out is what has no substance: ad reads and sponsor
segments, intros and outros, housekeeping, plugs, small talk, dead air, tangents that go nowhere. Each moment:
- Is ONE complete thought, story, answer or argument: one topic from its natural start to its natural end.
- Runs about {lo}-{hi} seconds (1 to 3 minutes; use the timestamps, the next sentence's time shows where the last one
  ends). A longer stretch on one topic splits into separate moments at a natural turn, each standing on its own.
- STARTS at the beginning of a sentence and ENDS at the end of one (Matt: never start mid-sentence, never cut a sentence
  off). Give the first and last sentence numbers; the cut is made exactly there.
- STANDS ALONE: someone who never saw the episode understands it. Start where the topic starts (in Q&A episodes, the
  listener question that sets it up); no "like I said" or "that"/"he" pointing back outside the moment.
- Opens as cleanly as the conversation allows (skip throat-clearing like "so anyway") and ends on its landing line,
  before the conversation drifts to the next topic.
- Is mostly Andy's take or story. Keep the co-host's lines that belong to it.
- Never overlaps another moment.
Order them best first by how well they'd do (the playbook and high performers show what wins). confidence says how strong
each is; a weaker moment that is still substantive and inside the guardrails still goes in, with a lower confidence.

{spec_section('## Hard exclusions', '## Hook rules')}
Any moment that touches a hard exclusion is never picked, even in passing. When unsure, leave it out.

Content profile: what Matt approves and rejects, learned from every review decision. Skip what he rejects. "Unclear"
subjects are still pulled, with a risk_flag so the human approving them sees it.
{fw.content_profile()}

Performance playbook (what made past clips win, from every editor's posts and Matt's high performers):
{fw.read_playbook()['network'].strip() or '(no lessons yet)'}

Matt's highest-performing short clips (views in brackets), for what kind of material wins once cut down:
{high_performers()}

Sourcing profile: what Matt approves and rejects from YOUR earlier moments, rewritten after every review. Follow it closely;
his reasons are the most direct guidance you have.
{sourcing_profile()}

Your most recent reviewed moments, with Matt's reasons:
{sourcing_lessons()}

Fields: subject = 1-3 words in CamelCase naming the moment (Natalie's style: GetSerious, SnapBenefits, GTAVI). why = what
makes it worth posting. payoff = the line it lands on. standalone = why it needs no context. topical = depends on a news
event or date that will go stale. risk_flags = subject matter a human should see first (politics, a named private
person, medical or legal claims, a line that reads badly out of context); swearing is never a flag. confidence 1-10.
passed_over: substantive stretches you left out and why (guardrails, no clean start or end), in a sentence or two.

Transcript:
{numbered}"""


# ---------- cutting ----------
class Quiet:
    """10 ms loudness envelope of the episode audio, to move cut points into the quietest nearby moment."""
    def __init__(self, wav):
        a = np.memmap(wav, dtype=np.int16, mode='r', offset=44)
        hop = 160
        n = len(a) // hop
        rms = np.empty(n, dtype=np.float32)
        for k in range(0, n, 360000):  # an hour at a time keeps memory small
            blk = a[k * hop:min(n, k + 360000) * hop].astype(np.float32).reshape(-1, hop) / 32768
            rms[k:k + len(blk)] = np.sqrt(np.mean(blk ** 2, axis=1) + 1e-12)
        self.db = 20 * np.log10(rms)
        self.speech = float(np.percentile(self.db, 90))

    def at(self, t, lo, hi):
        i0, i1 = max(0, int(lo * 100)), min(len(self.db) - 1, int(hi * 100))
        if i1 <= i0:
            return t
        seg = self.db[i0:i1 + 1] + 0.008 * np.abs(np.arange(i0, i1 + 1) - t * 100)  # the deepest dip wins; slight pull toward t
        return (i0 + int(np.argmin(seg))) / 100

    def level(self, t):
        i = int(t * 100)
        return float(self.db[max(0, i - 2):i + 3].min()) - self.speech


def cut(src, start, end, out):
    tmp = out + '.part.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{start:.3f}', '-i', src, '-t', f'{end - start:.3f}', '-map', '0:v:0', '-map', '0:a:0',
                    '-c:v', 'libx264', '-crf', '18', '-preset', 'fast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '256k', '-ar', '48000',
                    '-movflags', '+faststart', tmp], check=True, stdin=subprocess.DEVNULL)
    shutil.move(tmp, out)


def _w(t):
    return re.sub(r"[^a-z0-9' ]", ' ', (t or '').lower().replace('-', ' ')).split()


def heard(path, tail=False, secs=5):
    """What whisper hears in the first (or last) few seconds of a cut clip."""
    with tempfile.TemporaryDirectory() as d:
        pos = ['-sseof', f'-{secs}'] if tail else ['-t', str(secs)]
        subprocess.run(['ffmpeg', '-v', 'error', '-y'] + pos + ['-i', path, '-ac', '1', '-ar', '16000', f'{d}/a.wav'], check=True)
        subprocess.run(['whisper-cli', '-m', os.path.expanduser(CFG['whisper_model']), '-f', f'{d}/a.wav', '-np', '-otxt', '-of', f'{d}/o'],
                       check=True, capture_output=True)
        return open(f'{d}/o.txt').read()


def edge(path, first_line, last_line):
    """Check a cut by ear (Matt: never start mid-sentence, never cut a sentence off). Returns ('ok' | 'clipped' |
    'unclear') for the start and the end. Whisper timings are approximate, so a cut between two sentences with no pause
    can shave a word; this catches it."""
    def judge(want, got):
        if not want or not got:
            return 'unclear'
        i0 = got.index(want[0]) if want[0] in got else None
        i1 = got.index(want[1]) if len(want) > 1 and want[1] in got else None
        if i0 is not None and i0 <= 1:
            return 'ok'
        if i1 == 0:
            return 'clipped'             # heard from the second word on
        return 'ok' if i0 is not None else 'unclear'   # a word or two of run-in before the sentence is fine
    s = judge(_w(first_line)[:2], _w(heard(path))[:10])
    e = judge(_w(last_line)[::-1][:2], _w(heard(path, tail=True, secs=4))[::-1][:10])
    return s, e


def overlaps(a, b):
    return min(a[1], b[1]) - max(a[0], b[0]) > 0.25 * min(a[1] - a[0], b[1] - b[0])


def natalie_spans(c, episode):
    """Natalie's clips from the same episode, matched to the episode transcript by their opening words, so the agent
    doesn't hand the editors a moment she already cut."""
    return [(r['transcript'] or '') for r in c.execute("SELECT p.transcript FROM raw_clips r JOIN preps p ON p.raw_id=r.id "
                                                        "WHERE r.episode=? AND r.path NOT IN (SELECT path FROM sourced)", (episode,))]


# ---------- one episode ----------
def source(ref, title=None, repick=False):
    c = db()
    lock = open(os.path.join(WORK, '.sourcing.lock'), 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return fw.log('sourcing already running; skipping')
    local = os.path.exists(ref)
    if local and not title:
        title = os.path.splitext(os.path.basename(ref))[0]
    m = EP_NAME.match(title or '')
    if not m:
        sys.exit(f'episode title must look like "Ep 1076 QAF": {title!r}')
    ep = {'ref': os.path.abspath(ref) if local else ref, 'title': title, 'episode': float(m[1]), 'show': m[2].upper()}
    c.execute('INSERT OR IGNORE INTO episodes(ref,title,episode,show,status,started) VALUES(?,?,?,?,?,?)',
              (ep['ref'], title, ep['episode'], ep['show'], 'processing', time.time()))
    c.execute("UPDATE episodes SET status='processing', note=NULL, started=? WHERE ref=?", (time.time(), ep['ref']))
    c.commit()
    row_id = c.execute('SELECT id FROM episodes WHERE ref=?', (ep['ref'],)).fetchone()[0]
    work = os.path.join(SRC_DIR, str(row_id))
    os.makedirs(work, exist_ok=True)
    dl, ok = None, False
    try:
        t0 = time.time()
        if local:
            src = ref
        else:
            fw.log(f'sourcing {title}: downloading')
            os.makedirs(os.path.join(WORK, 'episodes'), exist_ok=True)
            src = dl = fetch(ref, os.path.join(WORK, 'episodes', f'{ref}.mp4'))
        w, h, fps, dur = fw.probe(src)
        ep['duration'] = dur
        fw.log(f'sourcing {title}: transcribing {mmss(dur)} ({w}x{h})')
        toks, wav = transcribe(src, work)
        sents = sentences(toks)
        json.dump(sents, open(os.path.join(work, 'sentences.json'), 'w'))
        fw.log(f'sourcing {title}: {len(sents)} sentences; picking moments')
        picks_p = os.path.join(work, 'picks.json')
        if os.path.exists(picks_p) and not repick:  # re-cutting an episode keeps its picks
            picks = json.load(open(picks_p))
        else:
            prompt = pick_prompt(ep, sents)
            open(os.path.join(work, 'prompt.txt'), 'w').write(prompt)
            model_was = CFG.get('decide_model')
            CFG['decide_model'] = S['model'] or model_was
            try:
                picks = fw.ask_claude(prompt, PICK_SCHEMA, timeout=1200)  # a whole episode in, 10-30 moments out
            finally:
                CFG['decide_model'] = model_was
            json.dump(picks, open(picks_p, 'w'), indent=1)
        quiet = Quiet(wav)
        theirs = natalie_spans(c, ep['episode'])
        handoff = approved_dir(ep['show'])
        dest = handoff if S['auto_approve'] else os.path.join(root(), S['review_folder'])
        os.makedirs(dest, exist_ok=True)
        ep_num = f"{ep['episode']:g}"
        kept, made = [], 0
        for p in picks['clips']:
            a, b = max(0, p['start']), min(len(sents) - 1, p['end'])
            if b < a:
                continue
            text = ' '.join(s['text'] for s in sents[a:b + 1])
            s0, e0 = sents[a]['start'], sents[b]['end']
            nxt = sents[b + 1]['start'] if b + 1 < len(sents) else dur
            prv = sents[a - 1]['end'] if a > 0 else 0
            # into the gap before the first word and after the last; whisper's boundaries run late or early by a few
            # hundred ms, so look a little past the neighbouring sentence's edge for the real gap
            start = quiet.at(s0, max(prv - 0.2, s0 - 0.6), s0 + 0.08)
            end = quiet.at(e0, e0 - 0.08, min(nxt + 0.2, e0 + 0.6))
            why_not = None
            if fw.EXCLUDE_WORDS.search(text):
                why_not = f"hard exclusion: {fw.EXCLUDE_WORDS.search(text)[0]}"
            elif not (S['min_seconds'] - 3 <= end - start <= S['max_seconds'] + 5):
                why_not = f'{end - start:.0f} s is outside {S["min_seconds"]}-{S["max_seconds"]} s'
            elif any(overlaps((start, end), k) for k in kept):
                why_not = 'overlaps a better pick'
            elif any(fw.same_moment(text, t) for t in theirs):
                why_not = "Natalie already clipped this moment"
            if why_not:
                fw.log(f"sourcing {title}: dropped {p['subject']} ({mmss(s0)}): {why_not}")
                continue
            subject = re.sub(r'[^A-Za-z0-9]', '', p['subject'])[:40] or f'Moment{made + 1}'
            name = f"{ep_num}_{ep['show']}_{subject}.mp4"
            k = 2
            while os.path.exists(os.path.join(dest, name)) or os.path.exists(os.path.join(handoff, name)):
                name = f"{ep_num}_{ep['show']}_{subject}{k}.mp4"
                k += 1
            out = os.path.join(dest, name)
            cut(src, start, end, out)
            flags = list(p['risk_flags'])
            for attempt in range(3):  # listen to the edges; a shaved word moves the cut into the previous/next gap
                es, ee = edge(out, sents[a]['text'], sents[b]['text'])
                if es != 'clipped' and ee != 'clipped':
                    break
                if attempt == 2:
                    break
                if es == 'clipped':
                    start = quiet.at(start - 0.25, start - 0.8, start - 0.06)
                if ee == 'clipped':
                    end = quiet.at(end + 0.25, end + 0.06, end + 0.8)
                fw.log(f"sourcing {title}: {name} {'start' if es == 'clipped' else 'end'} clipped a word; re-cutting at {mmss(start)}-{mmss(end)}")
                cut(src, start, end, out)
            if 'clipped' in (es, ee) or 'unclear' in (es, ee):
                flags.append(f"check the {' and '.join(x for x, v in (('start', es), ('end', ee)) if v != 'ok')}: the cut may clip a word")
            kept.append((start, end))
            made += 1
            c.execute('INSERT INTO sourced(episode_id,path,name,start,end,duration,subject,first_line,last_line,why,payoff,topical,risk_flags,'
                      'confidence,transcript,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (row_id, out, name, start, end, end - start, subject, sents[a]['text'], sents[b]['text'], p['why'], p['payoff'],
                       int(p['topical']), json.dumps(flags), p['confidence'], text,
                       'approved' if S['auto_approve'] else 'held', time.time()))
            c.commit()
            fw.log(f"sourcing {title}: {name} {mmss(start)}-{mmss(end)} ({end - start:.0f} s, cut points "
                   f"{quiet.level(start):.0f}/{quiet.level(end):.0f} dB vs speech) -> {('editors' if S['handoff_to_editors'] else 'approved') if S['auto_approve'] else 'review'}")
        c.execute("UPDATE episodes SET status='done', duration=?, picks=?, finished=?, note=? WHERE id=?",
                  (dur, made, time.time(), picks['passed_over'][:1000], row_id))
        c.commit()
        fw.log(f'sourcing {title}: {made} clips in {(time.time() - t0) / 60:.0f} min')
        ok = True
        return made
    except Exception as ex:
        c.execute("UPDATE episodes SET status='failed', note=? WHERE id=?", (str(ex)[:500], row_id))
        c.commit()
        fw.log(f'sourcing {title} ERROR {ex}\n{traceback.format_exc()}')
        raise
    finally:
        # disk is tight: the episode and its audio go once it's sourced (a failed one is kept a day for the retry);
        # the transcript and picks stay (small) for learning
        for p in ([dl] if ok else []) + [os.path.join(work, 'audio.wav')]:
            if p and os.path.exists(p):
                os.remove(p)


def run():
    """One loop step: source the newest episode not yet sourced (at most one per run)."""
    if not S['enabled'] or not CFG.get('episodes_folder_id'):
        return
    for p in glob.glob(os.path.join(WORK, 'episodes', '*')):  # downloads left by a failed run
        if time.time() - os.path.getmtime(p) > 86400:
            os.remove(p)
    c = db()
    seen = {r['ref']: r['status'] for r in c.execute('SELECT ref, status FROM episodes')}
    for e in listing():
        if e['episode'] < S['since_episode']:
            break
        if seen.get(e['ref']) in ('done', 'processing'):
            continue
        if seen.get(e['ref']) == 'failed' and c.execute("SELECT COUNT(*) FROM episodes WHERE ref=? AND started>?",
                                                       (e['ref'], time.time() - 6 * 3600)).fetchone()[0]:
            continue  # failed in the last 6 hours: wait before retrying
        return source(e['ref'], e['title'])


# ---------- human review of the raw clips ----------
def held():
    for r in db().execute("SELECT s.*, e.title FROM sourced s JOIN episodes e ON e.id=s.episode_id WHERE s.status='held' ORDER BY s.id"):
        print(f"{r['id']:4} {r['name']:36} {r['duration']:4.0f} s  {r['first_line'][:70]}" + (f"  ⚑ {', '.join(json.loads(r['risk_flags']))}" if json.loads(r['risk_flags'] or '[]') else ''))


def review(clip_id, action, note=''):
    """Approve moves the clip to the approved folder (or, once handoff_to_editors is on, into the show's clip folder where
    the editors pick it up, dated now so it counts as fresh supply); the approval is noted for the agent's next picks. Reject deletes the file and
    keeps the reason, which the agent reads before its next picks."""
    c = db()
    r = c.execute('SELECT * FROM sourced WHERE id=?', (clip_id,)).fetchone()
    if not r or r['status'] != 'held':
        sys.exit(f'clip {clip_id} is not waiting for review')
    if action == 'approve':
        ep = c.execute('SELECT show FROM episodes WHERE id=?', (r['episode_id'],)).fetchone()
        folder = approved_dir(ep['show'])
        os.makedirs(folder, exist_ok=True)
        name, k = r['name'], 2
        while os.path.exists(os.path.join(folder, name)):  # never overwrite one of Natalie's clips
            name = re.sub(r'\d*\.mp4$', f'{k}.mp4', r['name'])
            k += 1
        dest = os.path.join(folder, name)
        shutil.move(r['path'], dest)
        os.utime(dest)  # dated at approval (fresh supply, once it reaches the editors)
        c.execute("UPDATE sourced SET status='approved', path=?, name=?, review_note=?, reviewed=? WHERE id=?", (dest, name, note, time.time(), clip_id))
    else:
        if not note.strip():
            sys.exit('say why it was rejected')
        if os.path.exists(r['path']):
            os.remove(r['path'])
        c.execute("UPDATE sourced SET status='rejected', review_note=?, reviewed=? WHERE id=?", (note, time.time(), clip_id))
    c.commit()
    # learn in the background so the dashboard isn't kept waiting
    subprocess.Popen([sys.executable, os.path.abspath(__file__), 'learn'], cwd=fw.HERE, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=open(os.path.join(os.path.dirname(fw.LOG), 'triage.out'), 'a'), stderr=subprocess.STDOUT)
    fw.log(f"sourced clip {clip_id} {r['name']} -> " + (f"approved -> {os.path.relpath(dest, root())}" if action == 'approve' else 'rejected')
           + (f': {note}' if note else ''))


if __name__ == '__main__':
    a = sys.argv[1:] or ['']
    cmds = {'episodes': episodes_cmd, 'run': run, 'held': held, 'learn': learn,
            'episode': lambda: source(a[1], a[a.index('--title') + 1] if '--title' in a else None, '--repick' in a),
            'approve': lambda: review(int(a[1]), 'approve', ' '.join(a[2:])),
            'reject': lambda: review(int(a[1]), 'reject', ' '.join(a[2:]))}
    cmds.get(a[0], lambda: sys.exit(__doc__))()
