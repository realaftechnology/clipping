#!/usr/bin/env python3
"""Word-by-word caption overlay for vertical clips (1080x1920), rendered by render.mjs.
usage: captions.py <words.json from whisper-cli -ml 1 -oj> <out.html> [--y 1250] [--size 100] [--font 'Local Name,Alt'] [--active #FFD400|none] [--animate] [--uncensored]
Groups 1-3 words (max ~14 chars, breaks on punctuation). Static by default; --active colours the spoken word, --animate adds a pop-in."""
import json, sys, re, html
args = sys.argv[1:]
src, out = args[0], args[1]
def opt(k, d):
    return args[args.index(k) + 1] if k in args else d
Y, SIZE, CENSOR = int(opt('--y', 1250)), int(opt('--size', 100)), '--uncensored' not in args
FONT = ','.join(f'local("{n}")' for n in opt('--font', 'Futura Condensed ExtraBold,Futura-CondensedExtraBold').split(','))
ACTIVE = opt('--active', 'none')
ANIM = '--animate' in args
words = []
for s in json.load(open(src))['transcription']:
    w = s['text'].strip()
    if not w: continue
    if CENSOR:
        w = re.sub(r'(?i)\b(sh)i(t\w*)', r'\1*\2', w)
        w = re.sub(r'(?i)\b(f)u(ck\w*)', r'\1*\2', w)
    words.append({'t': s['offsets']['from'] / 1000, 'e': s['offsets']['to'] / 1000, 'w': w.upper()})
# names always spelled right, even in all-caps captions (from Chay's Real AF rulebook, adopted 2026-11-25):
# Andy's company is 1stPhorm; DJ, OGs and Frisella are always written that way
SPELL = {'FIRSTFORM': '1stPhorm', 'FIRSTPHORM': '1stPhorm', '1STPHORM': '1stPhorm', '1STFORM': '1stPhorm', 'DEEJAY': 'DJ', 'D.J.': 'DJ',
         "OG'S": 'OGs', 'OGS': 'OGs', 'FRIZELLA': 'FRISELLA', 'FRESELLA': 'FRISELLA', 'FRISSELLA': 'FRISELLA', 'FRIZZELLA': 'FRISELLA'}
k = 0
while k < len(words):
    m = re.match(r"^(.*?)([.,?!]*)$", words[k]['w'])
    core, tail = m[1], m[2]
    if core == 'FIRST' and k + 1 < len(words) and re.match(r'^(FORM|PHORM)', words[k + 1]['w']):
        words[k + 1]['w'] = '1stPhorm' + re.match(r'^(?:FORM|PHORM)(.*)$', words[k + 1]['w'])[1]
        words[k + 1]['t'] = words[k]['t']
        del words[k]
        continue
    if core in SPELL:
        words[k]['w'] = SPELL[core] + tail
    k += 1
groups, cur = [], []
for w in words:
    if cur and (len(cur) == 3 or len(' '.join(x['w'] for x in cur + [w])) > 14):
        groups.append(cur); cur = []
    cur.append(w)
    if re.search(r'[.,?!]$', w['w']): groups.append(cur); cur = []
if cur: groups.append(cur)
G = []
for i, g in enumerate(groups):
    end = g[-1]['e'] + 0.3
    if i + 1 < len(groups): end = min(max(end, g[-1]['e']), groups[i + 1][0]['t']) if groups[i + 1][0]['t'] - g[-1]['e'] < 0.6 else end
    G.append({'s': g[0]['t'], 'e': end, 'w': [[x['t'], html.escape(re.sub(r'[.,]$', '', x['w']))] for x in g]})
page = f'''<!doctype html><html><head><meta charset="utf-8"><style>
@font-face{{font-family:Cap;src:{FONT}}}
html,body{{margin:0;width:100vw;height:100vh;background:transparent;overflow:hidden}}
#c{{position:absolute;left:60px;right:60px;top:{Y}px;text-align:center;font-family:Cap,Futura,sans-serif;font-size:{SIZE}px;line-height:1.05;
color:#fff;-webkit-text-stroke:{max(6, round(SIZE*0.12))}px #000;paint-order:stroke fill;text-shadow:0 6px 18px rgba(0,0,0,.55);transform-origin:center}}
#c span.on{{color:{ACTIVE}}}</style></head><body><div id="c"></div><script>
const G={json.dumps(G)};const ACT={'true' if ACTIVE!='none' else 'false'},ANIM={'true' if ANIM else 'false'};const c=document.getElementById('c');
const pr=(t,a,d)=>Math.min(1,Math.max(0,(t-a)/d)),oQ=p=>1-Math.pow(1-p,4);
function setTime(t){{const g=G.find(g=>t>=g.s&&t<g.e);if(!g){{c.innerHTML='';return false}}
let a=ACT?0:-1;if(ACT)g.w.forEach((w,i)=>{{if(t>=w[0])a=i}});
c.innerHTML=g.w.map((w,i)=>`<span class="${{i===a?'on':''}}">${{w[1]}}</span>`).join(' ');
if(ANIM){{const s=(0.9+0.1*oQ(pr(t,g.s,0.12))).toFixed(3);c.style.transform=`scale(${{s}})`}}return true}}
function scan(n,fps,step){{let prev=null,keys=[];for(let f=0;f<n;f+=step){{setTime(f/fps);const s=c.outerHTML;if(s!==prev){{keys.push(f);prev=s}}}}return keys}}
window.ready=document.fonts.ready.then(()=>true);
</script></body></html>'''
open(out, 'w').write(page)
print(f'{len(G)} caption groups')
