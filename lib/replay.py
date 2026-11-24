#!/usr/bin/env python3
"""Does any moment of a cut clip play twice? (the "CD skip")

Matt, 2026-11-24, on a posted clip: "This video skips at 'if you play it' and at 'f*ckin a year'. It plays the same
frame or two twice. Like a CD skip. That needs to be fixed and never happen again." lib/trim.py now refuses to make
overlapping cuts; this is the independent check on its output, run on the tightened voice track before music is added
(music would mask it). A replay shows up as a stretch of audio that matches, almost sample for sample, the audio a
fixed short time earlier. Lags under 80 ms are ignored (sustained vowels repeat at the pitch period).

usage: replay.py <video>   prints [[time s, replayed s], ...] (empty = clean)
"""
import json, subprocess, sys
import numpy as np


def replays(path, sr=8000, win=0.06, lags=(0.08, 0.6), min_run=3):
    pcm = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:a:0', '-ac', '1', '-ar', str(sr), '-f', 's16le', '-'],
                         capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout
    a = np.frombuffer(pcm, np.int16).astype(np.float32)
    W, step = int(win * sr), int(0.02 * sr)
    loud = np.percentile(np.abs(a), 90)
    L0, L1 = int(lags[0] * sr), int(lags[1] * sr)
    hits = []
    for i in range(L1, len(a) - W, step):
        x = a[i:i + W]
        if np.abs(x).max() < loud * 0.5:
            continue
        # correlation of this window against every window 80-600 ms earlier
        seg = a[i - L1:i - L0 + W]
        frames = np.lib.stride_tricks.sliding_window_view(seg, W)[::2]
        r = frames @ x / (np.linalg.norm(frames, axis=1) * np.linalg.norm(x) + 1e-9)
        k = int(np.argmax(r))
        if r[k] > 0.97:
            hits.append((i / sr, (L1 - 2 * k) / sr))
    # a real replay is several consecutive windows at the same lag
    out, run = [], []
    for t, lag in hits:
        if run and t - run[-1][0] <= 0.021 and abs(lag - run[-1][1]) <= 0.01:
            run.append((t, lag))
        else:
            if len(run) >= min_run:
                out.append([round(run[0][0], 2), round(run[0][1], 2)])
            run = [(t, lag)]
    if len(run) >= min_run:
        out.append([round(run[0][0], 2), round(run[0][1], 2)])
    return out


if __name__ == '__main__':
    print(json.dumps(replays(sys.argv[1])))
