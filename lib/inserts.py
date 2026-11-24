#!/usr/bin/env python3
"""Find stretches where the picture leaves the normal podcast shot: a video Andy reacts to, a split screen, a graphic.

usage: inserts.py <video>   ->  prints [{"start": s, "end": e}] (seconds)
Every 0.5 s a tiny grey frame is compared with the clip's usual look (the median frame); frames that differ a lot belong
to an insert. Runs shorter than 1 s are ignored; gaps under 1 s between runs are joined.
"""
import json, subprocess, sys
import numpy as np

src = sys.argv[1]
W, H, STEP = 64, 36, 0.5
raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', src, '-vf', f'fps={1 / STEP},scale={W}:{H},format=gray', '-f', 'rawvideo', '-'],
                     capture_output=True, check=True).stdout
f = np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W).astype(np.float32)
base = np.median(f, axis=0)
d = np.abs(f - base).mean(axis=(1, 2))
thr = max(18.0, np.median(d) * 3)          # well beyond normal movement in the shot
hot = d > thr
runs, i = [], 0
while i < len(hot):
    if hot[i]:
        j = i
        while j < len(hot) and hot[j]:
            j += 1
        runs.append([i * STEP, j * STEP])
        i = j
    else:
        i += 1
merged = []
for r in runs:
    if merged and r[0] - merged[-1][1] < 1.0:
        merged[-1][1] = r[1]
    else:
        merged.append(r)
print(json.dumps([{'start': round(a, 1), 'end': round(b, 1)} for a, b in merged if b - a >= 1.0]))
