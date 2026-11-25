#!/bin/zsh
# Example render stage: raw clip (prepped) -> 1080x1920 post with captions, hook title, music bed.
# usage: render_clip.sh <editor> <raw_id> "<HOOK TITLE>" <music file in catalog> <music start s> <out.mp4>
# Look comes from editors/<editor>/style.json; fonts must be in fonts.json.
set -e
HERE=${0:A:h}; ROOT=${HERE:h}; WORK=${AI_EDITORS_WORK:-$HOME/Movies/AI Editors}  # AI_EDITORS_WORK: a test copy
editor=$1; id=$2; hook=$3; song=$4; mstart=$5; out=$6
STYLE=$ROOT/editors/$editor/style.json; [[ -f $STYLE ]] || { echo "no style: $STYLE"; exit 1; }
sty() { python3 -c "import json,sys;s=json.load(open(sys.argv[1]));f=json.load(open(sys.argv[2]));k=sys.argv[3];v=s[k];print(','.join(f[v]) if k.endswith('_font') else v)" "$STYLE" "$ROOT/fonts.json" $1; }
MUSIC="$(cd "$ROOT" && python3 -c "import flywheel,os;print(os.path.expanduser(flywheel.CFG['music_dir']))")/$song"
read src cx cw dur fps <<<$(python3 -c "
import sqlite3,json,os;W=os.environ.get('AI_EDITORS_WORK') or os.path.expanduser('~/Movies/AI Editors');c=sqlite3.connect(os.path.join(W,'flywheel.db'))
p,cr,d,f=c.execute('select r.path,p.crop,p.duration,p.fps from raw_clips r join preps p on p.raw_id=r.id where r.id=$id').fetchone()
import glob;lc=glob.glob(os.path.join(W,'cache','$id.*'));p=lc[0] if lc and not lc[0].endswith('.part') else p
cr=json.loads(cr) if cr!='null' else None
print(p.replace(' ','\x01'),cr['x'] if cr else -1,cr['w'] if cr else -1,d,f)")
src=${src//$'\x01'/ }
# tightened version from lib/trim.py (the editor's trim plan): its own video+voice, length and re-timed words
WORDS="$WORK/prep/$id/words.json"
if [[ -n $TRIM_SRC ]]; then src=$TRIM_SRC; dur=$TRIM_DUR; WORDS=$TRIM_WORDS; fi
[[ -n $TRIM_VERTICAL ]] && cx=-1   # trim.py already built the vertical frame (speaker crop + shown inserts)
job=$WORK/render/$id; rm -rf "$job"; mkdir -p "$job"
python3 "$HERE/captions.py" "$WORDS" "$job/overlay.html" --font "$(sty caption_font)" --size $(sty caption_size) --y $(sty caption_y) --active "$(sty caption_active)" $([[ $(sty caption_animate) == True ]] && echo --animate) >/dev/null
# hook title: native TikTok/Instagram-style text box (sentence case, as typed), static, inside the captions page
python3 - "$job/overlay.html" "$hook" "$(sty hook_font)" $(sty hook_size) $(sty hook_top) "$(sty hook_bg)" "$(sty hook_color)" <<'PY'
import sys,html;p,h,font,size,top,bg,fg=sys.argv[1:];s=open(p).read()
src=','.join(f'local("{n}")' for n in font.split(','))
# Two stacked layers: the black boxes (text invisible) underneath, the white text on top. Line boxes overlap so a
# two-line hook reads as one joined box like Instagram's text background, without a box covering the line above.
css=(f'@font-face{{font-family:Hook;src:{src}}}.hk{{position:absolute;left:110px;right:110px;top:{top}px;text-align:center}}'
     f'.hk span{{display:inline;font-family:Hook,sans-serif;font-size:{size}px;line-height:1.2;padding:10px 22px 9px;'
     f'-webkit-box-decoration-break:clone;box-decoration-break:clone;border-radius:12px}}'
     f'#hkb span{{background:{bg};color:transparent}}#hkt span{{color:{fg}}}')
t=html.escape(h)
s=s.replace('</style>',css+'</style>').replace('<div id="c">',f'<div class="hk" id="hkb"><span>{t}</span></div><div class="hk" id="hkt"><span>{t}</span></div><div id="c">')
open(p,'w').write(s)
PY
frames=$(python3 -c "print(int($dur*$fps))")
node "$HERE/render.mjs" "$job/overlay.html" "$job" --frames $frames --fps $fps --step 2 --size 1080x1920 >/dev/null
if [[ $cx -ge 0 ]]; then vf="crop=${cw}:ih:${cx}:0,scale=1080:1920:flags=lanczos,setsar=1"; else vf="scale=1080:1920,setsar=1"; fi
# song "none": no music bed, the voice alone (Matt, 2026-11-24: "this video doesn't need music")
VIDF="[0:v]${vf}[b];[1:v]scale=in_range=pc:out_range=tv:out_color_matrix=bt709,format=yuva420p[o];[b][o]overlay=0:0:format=yuv420:eof_action=repeat[v]"
if [[ $song == none ]]; then
  MIN=(); AF="[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,alimiter=limit=0.89,aresample=48000[a]"
else
  MIN=(-ss $mstart -i "$MUSIC")
  AF="[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[va];[2:a]atrim=0:${dur},asetpts=N/SR/TB,loudnorm=I=-27:TP=-3,afade=t=in:d=1,afade=t=out:st=$((dur-1.5)):d=1.5[ma];\
[va][ma]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.89,aresample=48000[a]"
fi
ffmpeg -v error -y -i "$src" -f concat -safe 0 -i "$job/overlay.ffconcat" "${MIN[@]}" -filter_complex "${VIDF};${AF}" \
 -map "[v]" -map "[a]" -frames:v $frames -c:v libx264 -crf 20 -preset medium -pix_fmt yuv420p \
 -color_primaries bt709 -color_trc bt709 -colorspace bt709 -c:a aac -b:a 192k -movflags +faststart "$out"
echo "$out"
