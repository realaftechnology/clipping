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
import json, subprocess, sys
import numpy as np

src, words_p, plan_p, out = sys.argv[1:5]
crop = None
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
segs = []
for r in runs:
    s0, e0 = wt(r[0], 'from'), wt(r[-1], 'to')
    s = quietest(s0, s0 - 0.25, s0 + 0.08)
    e = quietest(e0, e0 - 0.05, e0 + 0.3)
    segs.append([max(0, s), min(dur, e)])

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
            if (j - i) / 100 > 0.45 and i / 100 > s + 0.05 and j / 100 < e - 0.05:
                final.append([last, i / 100 + 0.12])
                last = j / 100 - 0.08
            i = j
        else:
            i += 1
    final.append([last, e])
final = [[round(a_, 3), round(b_, 3)] for a_, b_ in final if b_ - a_ > 0.12]

# re-time the words onto the new timeline
new_t, offset = [], 0.0
for s, e in final:
    for i in keep:
        f_, t_ = wt(i, 'from'), wt(i, 'to')
        mid = (f_ + t_) / 2
        if s <= mid < e:
            new_t.append({'text': T[i]['text'], 'offsets': {'from': int((max(f_, s) - s + offset) * 1000), 'to': int((min(t_, e) - s + offset) * 1000)}})
    offset += e - s
json.dump({'transcription': new_t}, open(f'{out}/words.json', 'w'))

# cut: per segment trim + fades, alternate segments punched in 7% around the speaker
probe = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
                                   '-of', 'json', src], capture_output=True, text=True, check=True).stdout)['streams'][0]
W, H = probe['width'], probe['height']
fx = (crop[0] + crop[1] / 2) if crop and crop[0] >= 0 else W / 2
fy = H * 0.38
Z = 1.07
parts, zoomed = [], False
for k, (s, e) in enumerate(final):
    v = f'[0:v]trim={s}:{e},setpts=PTS-STARTPTS'
    if k and s - final[k - 1][1] >= 0.6:  # switch framing only where a real chunk was cut; tiny pause trims keep the shot
        zoomed = not zoomed
    if zoomed:
        cw_, ch_ = int(W / Z) // 2 * 2, int(H / Z) // 2 * 2
        v += f',crop={cw_}:{ch_}:{int(fx * (1 - 1 / Z))}:{int(fy * (1 - 1 / Z))},scale={W}:{H}:flags=lanczos'
    v += f',setsar=1,format=yuv420p[v{k}]'
    d = e - s
    fa = min(0.015, d / 4)
    au = f'[0:a]atrim={s}:{e},asetpts=PTS-STARTPTS,afade=t=in:d={fa},afade=t=out:st={d - fa:.3f}:d={fa}[a{k}]'
    parts += [v, au]
fc = ';'.join(parts) + ';' + ''.join(f'[v{k}][a{k}]' for k in range(len(final))) + f'concat=n={len(final)}:v=1:a=1[v][a]'
subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-filter_complex', fc, '-map', '[v]', '-map', '[a]',
                '-c:v', 'libx264', '-crf', '14', '-preset', 'fast', '-c:a', 'pcm_s16le', f'{out}/source.mov'], check=True,
               stdin=subprocess.DEVNULL)
print(json.dumps({'duration': round(offset, 3), 'cuts': len(final) - 1, 'segments': final}))
