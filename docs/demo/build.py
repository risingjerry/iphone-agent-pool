#!/usr/bin/env python3
"""Build docs/demo.gif: render HTML frames with headless Chrome, assemble with ffmpeg."""
import subprocess, pathlib, html
H = pathlib.Path(__file__).parent
OUT = H / "out"; OUT.mkdir(exist_ok=True)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
W, HT = 1160, 640
PROMPT = "Use the iPhone to calculate 12×34 in Calculator"
KEYS = {"1": (58, 648), "2": (149, 648), "×": (332, 465), "3": (240, 648), "4": (58, 557), "=": (332, 740)}
TAPS = ["1", "2", "×", "3", "4", "="]
# phone screen shown in CSS px: 232 wide -> scale = 232/390
SW = 232; SC = SW / 390

def frame(chars, lines, taps, img, ripple, done):
    typed = html.escape(PROMPT[:chars])
    cur = '<span class="cur"></span>' if lines == 0 and not done else ""
    L = []
    if lines >= 1: L.append('<div class="l"><b class="g">●</b> iphone12 · <b>pua_ready</b> <i>→ ready</i> <span class="d">(wireless · CoreDevice tunnel)</span></div>')
    if lines >= 2: L.append('<div class="l"><b class="g">●</b> <b>pua_launch_app</b> <span class="d">com.apple.calculator</span> <b class="g">✓</b></div>')
    if lines >= 3:
        t = ' <span class="d">·</span> '.join(f'<span class="k">{html.escape(k)}</span>' for k in TAPS[:taps])
        ok = ' <b class="g">✓</b>' if taps == 6 else ""
        L.append(f'<div class="l"><b class="g">●</b> <b>pua_tap</b> {t}{ok}</div>')
    if lines >= 4: L.append('<div class="l"><b class="g">●</b> Result on screen: <b class="w">408</b> <b class="g">✓</b></div>')
    if done: L.append('<div class="l done">Done — 12 × 34 = <b>408</b> on iphone12 <span class="d">(no cable)</span></div>')
    rip = ""
    if ripple:
        x, y = KEYS[ripple]
        rip = f'<div class="rip" style="left:{x*SC}px;top:{y*SC}px"></div>'
    return f"""<!doctype html><meta charset=utf-8><style>
*{{box-sizing:border-box;margin:0}}
body{{width:{W}px;height:{HT}px;background:radial-gradient(900px 600px at 20% 0%,#1d2233,#0b0d14 70%);font-family:-apple-system,'SF Pro Text',Helvetica,sans-serif;color:#e6e8ee;position:relative;overflow:hidden}}
.term{{position:absolute;left:36px;top:36px;width:690px;height:510px;background:#14161f;border-radius:16px;border:1px solid #2a2e3d;box-shadow:0 24px 60px rgba(0,0,0,.55)}}
.bar{{height:40px;border-bottom:1px solid #23273a;display:flex;align-items:center;padding:0 16px;gap:8px}}
.bar i{{width:12px;height:12px;border-radius:50%;display:block}}
.bar span{{margin-left:12px;color:#7d8399;font-size:13px}}
.body{{padding:26px 28px;font:15.5px/1.5 'SF Mono',Menlo,monospace}}
.p{{display:flex;gap:10px;margin-bottom:22px;color:#fff;font-size:17px}}
.p .a{{color:#d97757;font-weight:700}}
.cur{{display:inline-block;width:9px;height:19px;background:#d97757;vertical-align:-3px;margin-left:2px}}
.l{{margin:9px 0;color:#c9cdda}} .d{{color:#6f758b}} i{{font-style:normal;color:#9aa1b8}}
b{{font-weight:600;color:#e6e8ee}} .g{{color:#4ade80}} .w{{color:#fff}}
.k{{display:inline-block;min-width:22px;text-align:center;background:#2b2f42;border-radius:6px;padding:0 6px;color:#ffb347}}
.done{{margin-top:26px;padding:12px 14px;border-radius:10px;background:#16261d;border:1px solid #1f4d33;color:#bff5d3;font-size:16px}}
.done b{{color:#fff}}
.phone{{position:absolute;left:{756}px;top:36px;width:{SW+28}px;height:{round(SW*2532/1170)+28}px;background:#05060a;border-radius:46px;border:2px solid #3a3f55;box-shadow:0 24px 60px rgba(0,0,0,.6),inset 0 0 0 5px #0e1018;padding:14px}}
.scr{{position:relative;width:{SW}px;height:{round(SW*2532/1170)}px;border-radius:34px;overflow:hidden;background:#000}}
.scr img{{width:100%;height:100%;display:block}}
.rip{{position:absolute;width:52px;height:52px;margin:-26px 0 0 -26px;border-radius:50%;background:rgba(255,255,255,.35);border:2px solid rgba(255,255,255,.8);box-shadow:0 0 18px rgba(255,255,255,.5)}}
.cap{{position:absolute;left:36px;right:36px;bottom:26px;height:34px;display:flex;align-items:center;justify-content:center;gap:10px;border-radius:17px;background:#151826;border:1px solid #262a3c;color:#a3a9bf;font-size:14px}}
.cap b{{color:#fff}} .dot{{width:8px;height:8px;border-radius:50%;background:#4ade80;display:inline-block}}
</style>
<div class="term"><div class="bar"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i><span>claude — iphone-agent-pool</span></div>
<div class="body"><div class="p"><span class="a">&gt;</span><span>{typed}{cur}</span></div>{''.join(L)}</div></div>
<div class="phone"><div class="scr"><img src="p{img:02d}.png">{rip}</div></div>
<div class="cap"><span class="dot"></span><b>iphone-agent-pool</b> · real iPhone 12 · Wi-Fi, no USB</div>"""

# (chars, lines, taps, img, ripple, done, duration_s)
S = [(0, 0, 0, 0, None, False, 0.6)]
n = len(PROMPT); step = 5
for c in list(range(step, n, step)) + [n]:
    S.append((c, 0, 0, 0, None, False, 0.14))
S.append((n, 0, 0, 0, None, False, 0.5))
S.append((n, 1, 0, 0, None, False, 0.9))
S.append((n, 2, 0, 0, None, False, 0.9))
for i, k in enumerate(TAPS):
    S.append((n, 3, i, i, k, False, 0.3))      # ripple on current screen, key not yet listed
    S.append((n, 3, i + 1, i + 1, None, False, 0.65))
S.append((n, 4, 6, 6, None, False, 0.9))
S.append((n, 4, 6, 6, None, True, 2.6))

cc = ["concat"]
lst = []
for i, (ch, li, tp, im, rp, dn, dur) in enumerate(S):
    f = H / f"f{i:03d}.html"
    f.write_text(frame(ch, li, tp, im, rp, dn))
    png = OUT / f"f{i:03d}.png"
    subprocess.run([CHROME, "--headless=new", f"--screenshot={png}", f"--window-size={W},{HT}",
                    "--force-device-scale-factor=2", "--hide-scrollbars", f"file://{f}"],
                   check=True, capture_output=True)
    lst += [f"file '{png}'", f"duration {dur}"]
lst.append(f"file '{OUT / f'f{len(S)-1:03d}.png'}'")
(OUT / "list.txt").write_text("\n".join(lst))
vf = "scale=1160:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(OUT / "list.txt"),
                "-vf", vf, "-loop", "0", str(H.parent / "demo.gif")], check=True)
print(len(S), "frames", sum(s[-1] for s in S), "s")
