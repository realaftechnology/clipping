#!/usr/bin/env python3
"""Tighten a raw clip: drop filler, repeats, false starts and dead air, never a partial sentence.

Matt, 2026-11-23: "No clip should start mid-sentence, but the editors are permitted to trim clips as needed to remove
dead space, to remove filler words, repeated words... They are not permitted to cut off sentences. We want the pacing
to be fluid and smooth. So if you close your eyes, it sounds like it could have been said as one cohesive sentence."

usage: trim.py <source video> <words.json> <plan.json> <out dir> [--crop X,W]
plan.json: {"start": i, "end": j, "remove": [{"from": a, "to": b}]}   (word indices into words.json, inclusive)
writes <out dir>/source.mp4 (tightened video + voice), <out dir>/words.json (re-timed), prints {"duration": s, "cuts": n}

How cuts stay smooth:
- Word timings from whisper are approximate, so every cut point is moved into the quietest moment near it (measured
  from the audio), never inside a word.
- Pauses longer than 0.45 s are shortened to a natural ~0.2 s breath instead of removed.
- Each joint gets a 15 ms audio fade so there are no clicks; the music bed (added later) runs continuously over them.
- Where a cut removes more than ~0.6 s, the framing switches between normal and a 7% punch-in around the speaker, so
  the jump reads as a deliberate camera change; small pause trims keep the same framing so it doesn't flicker.
"""
import json, os, statistics, subprocess, sys, tempfile
import numpy as np

src, words_p, plan_p, out = sys.argv[1:5]
crop = None
inserts = json.loads(sys.argv[sys.argv.index('--inserts') + 1]) if '--inserts' in sys.argv else []  # from lib/inserts.py
in_insert = lambda t: any(x['start'] - 0.2 <= t <= x['end'] + 0.2 for x in inserts)
if '--crop' in sys.argv:
    cx, cw = map(int, sys.argv[sys.argv.index('--crop') + 1].split(','))
    crop = (cx, cw)
plan = json.load(open(plan_p))
toks = json.load(open(words_p))
T = toks['transcription']

# audio envelope, 10 ms frames
pcm = subprocess.run(['ffmpeg', '-v', 'error', '-i', src, '-ac', '1', '-ar', '16000', '-f', 's16le', '-'], capture_output=True, check=True).stdout
a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
hop = 160
n = len(a) // hop
rms = np.sqrt(np.mean(a[:n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
db = 20 * np.log10(rms)
speech = np.percentile(db, 90)
quiet = db < speech - 26            # well below normal speech level
dur = len(a) / 16000


def quietest(t, lo, hi):
    """Time of the quietest 10 ms frame in [lo, hi] (seconds), preferring frames near t."""
    i0, i1 = max(0, int(lo * 100)), min(n - 1, int(hi * 100))
    if i1 <= i0:
        return t
    seg = db[i0:i1 + 1] + 0.02 * np.abs(np.arange(i0, i1 + 1) - t * 100)  # small pull toward t
    return (i0 + int(np.argmin(seg))) / 100


def wt(i, k):
    return T[i]['offsets'][k] / 1000


start, end = plan['start'], plan['end']
removed = set()
for r in plan.get('remove', []):
    removed.update(range(max(start, r['from']), min(end, r['to']) + 1))
keep = [i for i in range(start, end + 1) if i not in removed and T[i]['text'].strip()]
if not keep:
    sys.exit('nothing left to keep')

# runs of consecutive kept words -> time segments, boundaries moved into silence
runs, cur = [], [keep[0]]
for i in keep[1:]:
    if i == cur[-1] + 1 or all(not T[k]['text'].strip() for k in range(cur[-1] + 1, i)):
        cur.append(i)
    else:
        runs.append(cur)
        cur = [i]
runs.append(cur)
def spoken(i, step):
    """Nearest word with text from index i in direction step (skips blank tokens), or None."""
    while 0 <= i < len(T):
        if T[i]['text'].strip():
            return i
        i += step
    return None


# Matt, 2026-11-24, on a posted clip: "This video skips at 'if you play it' and at 'f*ckin a year'. It plays the same
# frame or two twice. Like a CD skip. That needs to be fixed and never happen again." Cause: a cut point may move up to
# 0.3 s after a kept word / 0.25 s before the next one to land in silence; when the removed word was shorter than that,
# the two pieces overlapped and the overlap played twice. So each cut point may only move into silence on its own side
# of the removed words, never into them, and pieces may never overlap (checked again below before anything is cut).
segs = []
for r in runs:
    s0, e0 = wt(r[0], 'from'), wt(r[-1], 'to')
    lo, hi = s0 - 0.25, e0 + 0.3
    p_, n_ = spoken(r[0] - 1, -1), spoken(r[-1] + 1, 1)
    if p_ is not None and p_ in removed:  # removed word(s) just before: start after they end
        lo = max(lo, wt(p_, 'to') - 0.02)
    if n_ is not None and n_ in removed:  # removed word(s) just after: end before they begin
        hi = min(hi, wt(n_, 'from') + 0.02)
    s = quietest(s0, min(lo, s0), s0 + 0.08)
    e = quietest(e0, e0 - 0.05, max(hi, e0))
    segs.append([max(0, s), min(dur, e)])
merged = [segs[0]]
for s, e in segs[1:]:
    if s < merged[-1][1] + 0.034:  # less than a frame apart (or overlapping): too small to be a cut, play straight through
        merged[-1][1] = max(merged[-1][1], e)
    else:
        merged.append([s, e])
segs = merged

# shorten long pauses inside each segment to a natural breath
final = []
for s, e in segs:
    i, last = int(s * 100), s
    i_end = int(e * 100)
    while i < i_end:
        if quiet[i]:
            j = i
            while j < i_end and quiet[j]:
                j += 1
            word_inside = any(i / 100 - 0.1 <= wt(w_, 'from') <= j / 100 + 0.05 for w_ in keep)  # never shave off a quiet word
            if (j - i) / 100 > 0.45 and i / 100 > s + 0.05 and j / 100 < e - 0.05 and not in_insert(i / 100) and not in_insert(j / 100) \
                    and not word_inside:
                final.append([last, i / 100 + 0.12])
                last = j / 100 - 0.08
            i = j
        else:
            i += 1
    final.append([last, e])
final = [[round(a_, 3), round(b_, 3)] for a_, b_ in final if b_ - a_ > 0.12]
# hard guard: time only ever moves forward, so nothing is ever played twice
for (a1, b1), (a2, b2) in zip(final, final[1:]):
    if a2 < b1:
        sys.exit(f'refusing to cut: piece at {a2}s starts before the previous piece ends ({b1}s) - it would play twice')

# re-time the words onto the new timeline. Every kept word stays in the captions: whisper's word times run long (they
# swallow the pause after a word), so a word is placed by where it starts, and one whose start fell inside a shortened
# pause goes to the start of the next piece (that's where its sound is).
starts, offset = [], 0.0
for s, e in final:
    starts.append((s, e, offset))
    offset += e - s
new_t = []
for i in keep:
    f_, t_ = wt(i, 'from'), wt(i, 'to')
    seg = next((x for x in starts if x[0] - 0.05 <= f_ < x[1]), None) or next((x for x in starts if x[0] >= f_), starts[-1])
    s, e, off = seg
    a_ = max(f_, s) - s + off
    b_ = max(a_ + 0.08, min(t_, e) - s + off)
    new_t.append({'text': T[i]['text'], 'offsets': {'from': int(a_ * 1000), 'to': int(b_ * 1000)}})
for k in range(1, len(new_t)):  # keep times in order
    if new_t[k]['offsets']['from'] < new_t[k - 1]['offsets']['from']:
        new_t[k]['offsets']['from'] = new_t[k - 1]['offsets']['from']
json.dump({'transcription': new_t}, open(f'{out}/words.json', 'w'))

# cut: per segment trim + fades. Landscape clips stay landscape (render_clip.sh crops to the speaker) unless an insert
# (another camera on a guest, a video Andy reacts to) is kept: then the output is assembled vertically here.
# Matt, 2026-11-24: "It needs to be consistent all around. The way it starts is the way it has to be throughout the clip.
# If it's going to show the entire frame, have it show the entire frame throughout the entire clip. If it's going to crop
# it, then crop the entire video. Don't switch back and forth." So every shot gets the same 9:16 crop, each centred on
# its own subject: the speaker crop for the main camera; for an insert, the face on screen in that shot, or the middle
# of the frame when several people share it (the "cruise the internet" layout has the played video in the middle).
FACES = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'bin', 'faces')


def insert_x(x0, x1, W, H, cw):
    """Left edge of a 9:16 crop for the insert shown between x0 and x1 seconds."""
    centre = (W - cw) / 2
    with tempfile.TemporaryDirectory() as t:
        subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(x0), '-to', str(x1), '-i', src, '-vf', 'fps=2,scale=640:-2', '-q:v', '5',
                        f'{t}/f%04d.jpg'], check=True, stdin=subprocess.DEVNULL)
        try:
            frames = json.loads(subprocess.run([FACES, t], capture_output=True, text=True, check=True).stdout)
        except Exception:
            return int(centre) // 2 * 2
    fs = sorted(f['x'] + f['w'] / 2 for fr in frames for f in fr['faces'] if f['c'] > 0.5 and f['h'] > 0.08)
    if not fs:
        return int(centre) // 2 * 2
    if fs[-1] - fs[0] > 0.2:  # more than one person/panel on screen: keep the middle
        return int(centre) // 2 * 2
    cx = statistics.median(fs) * W
    return int(min(max(cx - cw / 2, 0), W - cw)) // 2 * 2


probe = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
                                   '-of', 'json', src], capture_output=True, text=True, check=True).stdout)['streams'][0]
W, H = probe['width'], probe['height']
fx = (crop[0] + crop[1] / 2) if crop and crop[0] >= 0 else W / 2
fy = H * 0.38
Z = 1.07
landscape = bool(crop and crop[0] >= 0)
pieces = []  # (start, end, is_insert) with each segment split at insert edges
for s_, e_ in final:
    cuts = sorted({s_, e_} | {t for x in inserts for t in (x['start'], x['end']) if s_ < t < e_})
    for a_, b_ in zip(cuts, cuts[1:]):
        if b_ - a_ > 0.04:
            pieces.append((a_, b_, any(x['start'] <= (a_ + b_) / 2 <= x['end'] for x in inserts)))
vertical = landscape and any(p_[2] for p_ in pieces)
parts, zoomed, prev_end, ins_x = [], False, None, {}
for k, (s, e, ins) in enumerate(pieces):
    v = f'[0:v]trim={s}:{e},setpts=PTS-STARTPTS'
    if prev_end is not None and s - prev_end >= 0.6:  # switch framing only where a real chunk was cut
        zoomed = not zoomed
    prev_end = e
    if vertical and ins:
        xi = next(i_ for i_, x in enumerate(inserts) if x['start'] <= (s + e) / 2 <= x['end'])
        if xi not in ins_x:
            ins_x[xi] = insert_x(inserts[xi]['start'], inserts[xi]['end'], W, H, crop[1])
        v += f',crop={crop[1] // 2 * 2}:{H // 2 * 2}:{ins_x[xi]}:0,scale=1080:1920:flags=lanczos'
    elif vertical:
        cw_ = crop[1] / (Z if zoomed else 1)
        ch_ = H / (Z if zoomed else 1)
        x_ = fx - cw_ / 2 if not zoomed else fx - cw_ / 2
        y_ = 0 if not zoomed else fy * (1 - 1 / Z)
        v += f',crop={int(cw_) // 2 * 2}:{int(ch_) // 2 * 2}:{max(0, int(x_))}:{int(y_)},scale=1080:1920:flags=lanczos'
    elif zoomed:
        cw_, ch_ = int(W / Z) // 2 * 2, int(H / Z) // 2 * 2
        v += f',crop={cw_}:{ch_}:{int(fx * (1 - 1 / Z))}:{int(fy * (1 - 1 / Z))},scale={W}:{H}:flags=lanczos'
    v += f',setsar=1,format=yuv420p[v{k}]'
    d = e - s
    fa = min(0.015, d / 4)
    au = f'[0:a]atrim={s}:{e},asetpts=PTS-STARTPTS,afade=t=in:d={fa},afade=t=out:st={d - fa:.3f}:d={fa}[a{k}]'
    parts += [v, au]
fc = ';'.join(parts) + ';' + ''.join(f'[v{k}][a{k}]' for k in range(len(pieces))) + f'concat=n={len(pieces)}:v=1:a=1[v][a]'
subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-filter_complex', fc, '-map', '[v]', '-map', '[a]',
                '-c:v', 'libx264', '-crf', '14', '-preset', 'fast', '-c:a', 'pcm_s16le', f'{out}/source.mov'], check=True,
               stdin=subprocess.DEVNULL)
print(json.dumps({'duration': round(offset, 3), 'cuts': len(final) - 1, 'segments': final, 'vertical': vertical,
                  'inserts_kept': sum(1 for p_ in pieces if p_[2])}))
