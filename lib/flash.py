#!/usr/bin/env python3
"""Flash frames: two picture changes within 3 frames of each other (a frame or two of another camera or crop
flashing between cuts). From Chay's Real AF rulebook, adopted 2026-11-25.

usage: flash.py <video>   prints [time s, ...] of each flash (empty = clean)
"""
import json, re, subprocess, sys


def flashes(path, frames=3, thr=0.12):
    r = subprocess.run(['ffmpeg', '-v', 'info', '-i', path, '-an', '-vf', f"scale=160:-2,select='gt(scene,{thr})',showinfo", '-f', 'null', '-'],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    cuts = [float(m) for m in re.findall(r'pts_time:([\d.]+)', r.stderr)]
    fps = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=avg_frame_rate', '-of', 'csv=p=0', path],
                         capture_output=True, text=True).stdout.strip()
    n, d = (fps.split('/') + ['1'])[:2]
    gap = frames / (float(n) / float(d) if float(d) else 30)
    return [round(a, 2) for a, b in zip(cuts, cuts[1:]) if b - a <= gap + 0.001]


if __name__ == '__main__':
    print(json.dumps(flashes(sys.argv[1])))
