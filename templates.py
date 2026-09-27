"""
templates.py — HTML rendering for Broadcast Hub
================================================
Pure functions only — no FastAPI, no asyncio, no global state.
Each function receives exactly the data it needs and returns an HTML string.

Called from the route handlers in broadcast_hub.py after they have gathered
state from the shared dicts/locks.
"""

import json
import time

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fmt_elapsed(s: int) -> str:
    h, m = divmod(s, 3600)
    m, sec = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _label(key: str) -> str:
    """Return a human-readable label for an input key.

    Keys are ``"board-channel"`` (e.g. ``"0-1"``).
    """
    b, i = key.split("-", 1)
    return f"Board {b} · Input {i}"


# NOTE: _viewers_cell_html is intentionally not defined here.
# The canonical implementation lives in broadcast_hub.py and is called
# directly from the route handler. Keeping a second copy here caused
# silent divergence — removed to avoid maintenance confusion.


# ---------------------------------------------------------------------------
# render_mobile
# ---------------------------------------------------------------------------
# Parameters:
#   all_ids  : list of input key strings in display order   e.g. ["0-1", "0-2"]
#   live_ids : keys currently running in active_inputs
#   hls_ids  : keys currently running in active_hls

def render_mobile(all_ids: list, live_ids: list, hls_ids: list) -> str:

    inputs_info = [
        {"id": i, "live": i in live_ids, "hls": i in hls_ids, "label": _label(i)}
        for i in all_ids
    ]

    def make_card(inp):
        i        = inp["id"]
        is_live  = inp["live"]
        is_hls   = inp["hls"]
        lbl      = inp["label"]
        card_cls          = "card" + (" hls-on" if is_hls else "")
        placeholder_style = "display:none" if is_hls else ""
        no_sig            = "" if is_live else '<div class="no-sig">No Signal</div>'
        video_tag  = f'<video id="vid-{i}" playsinline muted controls style="display:none"></video>'
        cover_cls  = "loading-cover" + ("" if is_hls else " gone")
        cover_lbl  = "Buffering…" if is_hls else "Idle"
        status_badge = (
            '<div class="status-badge live"><div class="blink"></div> Live</div>'
            if is_live else
            '<div class="status-badge offline">○ Offline</div>'
        )
        hls_badge = '<div class="hls-badge"><div class="hls-dot"></div> HLS</div>' if is_hls else ""
        play_btn  = f'<button class="btn btn-play" id="playbtn-{i}" onclick="startHLS(\'{i}\')">&#9654; Play</button>'
        stop_btn  = (
            f'<button class="btn btn-stop" id="stopbtn-{i}" onclick="stopHLS(\'{i}\')">&#9632; Stop</button>'
            if is_hls else
            f'<button class="btn btn-stop btn-stop-dim" id="stopbtn-{i}" disabled>&#9632; Stop</button>'
        )
        sub = ("h264_qsv · HLS streaming" if is_hls
               else ("h264_qsv · ready" if is_live else "Tap Play to start feed"))
        return f"""
  <div class="{card_cls}" id="card-{i}">
    <div class="card-media" id="media-{i}">
      <div class="card-placeholder" id="placeholder-{i}" style="{placeholder_style}">
        <div class="big-num">{i}</div>{no_sig}
      </div>
      {video_tag}
      <div class="{cover_cls}" id="cover-{i}">
        <div class="spinner"></div>
        <div class="spinner-lbl" id="cover-lbl-{i}">{cover_lbl}</div>
      </div>
      {status_badge}{hls_badge}
    </div>
    <div class="card-foot">
      <div>
        <div class="card-title">{lbl}</div>
        <div class="card-sub" id="sub-{i}">{sub}</div>
      </div>
      <div class="btn-row">{play_btn}{stop_btn}</div>
    </div>
  </div>"""

    cards_html      = "".join(make_card(inp) for inp in inputs_info)
    already_hls_js  = str([inp["id"] for inp in inputs_info if inp["hls"]]).replace("'", '"')

    return (
        "<!DOCTYPE html><html data-theme='dark'><head>"
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        "<title>Broadcast Hub</title>"
        '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&display=swap" rel="stylesheet">'
        '<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>'
        "<style>"
        "*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}"
        ":root,[data-theme=dark]{"
        "--bg:#090d1a;--bg-topbar:rgba(9,13,26,.97);--surface:#0b0f22;"
        "--border:#1c2540;--text:#c0cce8;--muted:#3a4870;"
        "--accent:#00e5ff;--accent-dim:rgba(0,229,255,.18);--accent-bdr:rgba(0,229,255,.4);"
        "--live:#ff0066;--live-bg:rgba(255,0,102,.15);--live-bdr:rgba(255,0,102,.4);"
        "--card-bg:#0b0f22;--thumb-bg:#060810;--thumb-num:#1c2540;"
        "--btn-play-bg:#00e5ff;--btn-play-color:#000;"
        "--btn-stop-bg:rgba(255,0,102,.15);--btn-stop-bdr:rgba(255,0,102,.35);--btn-stop-color:#ff0066;"
        "}"
        "[data-theme=mono]{"
        "--bg:#100e06;--bg-topbar:rgba(12,10,4,.98);--surface:#0c0a04;"
        "--border:#2a1e08;--text:#e8d0a0;--muted:#5a3818;"
        "--accent:#ff6600;--accent-dim:rgba(255,102,0,.18);--accent-bdr:rgba(255,102,0,.45);"
        "--live:#ff2200;--live-bg:rgba(255,34,0,.15);--live-bdr:rgba(255,34,0,.4);"
        "--card-bg:#0c0a04;--thumb-bg:#070604;--thumb-num:#2a1e08;"
        "--btn-play-bg:#ff6600;--btn-play-color:#fff;"
        "--btn-stop-bg:rgba(255,34,0,.15);--btn-stop-bdr:rgba(255,34,0,.35);--btn-stop-color:#ff2200;"
        "}"
        "[data-theme=light]{"
        "--bg:#f3f5fa;--bg-topbar:rgba(26,31,56,.98);--surface:#fff;"
        "--border:#c8cedd;--text:#1a1f38;--muted:#6878a8;"
        "--accent:#4d9fff;--accent-dim:rgba(77,159,255,.15);--accent-bdr:rgba(77,159,255,.45);"
        "--live:#dc2626;--live-bg:rgba(220,38,38,.1);--live-bdr:rgba(220,38,38,.35);"
        "--card-bg:#fff;--thumb-bg:#edf0f8;--thumb-num:#c0c8de;"
        "--btn-play-bg:#4d9fff;--btn-play-color:#fff;"
        "--btn-stop-bg:rgba(220,38,38,.1);--btn-stop-bdr:rgba(220,38,38,.3);--btn-stop-color:#dc2626;"
        "}"

        "body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;padding-bottom:30px;max-width:480px;margin:0 auto}"
        ".topbar{padding:16px 16px 12px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg-topbar);backdrop-filter:blur(14px);display:flex;align-items:center;justify-content:space-between;z-index:50}"
        ".logo{font-family:'Inter',sans-serif;font-weight:900;font-style:italic;font-size:21px;text-transform:uppercase;color:var(--text)}"
        ".logo span{color:var(--accent)}"
        ".desktop-link{color:var(--muted);font-size:11px;text-decoration:none;text-transform:uppercase;letter-spacing:.1em}"
        ".section-lbl{padding:16px 14px 8px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.15em;color:var(--muted)}"
        ".cards{padding:0 10px;display:flex;flex-direction:column;gap:12px}"
        ".card{background:var(--card-bg);border:1px solid var(--border);border-radius:4px;overflow:hidden;transition:border-color .2s}"
        ".card.hls-on{border-color:var(--accent-bdr)}"
        ".card-media{position:relative;width:100%;aspect-ratio:16/9;background:var(--thumb-bg);overflow:hidden}"
        ".card-media video{width:100%;height:100%;object-fit:contain;display:block;background:#000}"
        ".card-placeholder{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:6px}"
        ".card-placeholder .big-num{font-family:'Inter',sans-serif;font-weight:900;font-size:36px;color:var(--thumb-num);line-height:1}"
        ".card-placeholder .no-sig{font-family:'Inter',sans-serif;font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:.15em;color:var(--muted)}"
        ".status-badge{position:absolute;top:10px;left:10px;z-index:5;font-family:'Inter',sans-serif;font-weight:900;font-size:11px;letter-spacing:.1em;text-transform:uppercase;display:flex;align-items:center;gap:5px;padding:3px 9px;border-radius:4px}"
        ".status-badge.live{background:var(--live-bg);border:1px solid var(--live-bdr);color:var(--live)}"
        ".status-badge.offline{color:var(--muted)}"
        ".blink{width:6px;height:6px;border-radius:50%;background:var(--live);animation:blink 1.4s ease-in-out infinite}"
        "@keyframes blink{0%,100%{opacity:1}50%{opacity:.15}}"
        ".hls-badge{position:absolute;top:10px;right:10px;z-index:5;background:var(--accent-dim);border:1px solid var(--accent-bdr);color:var(--accent);font-family:'Inter',sans-serif;font-weight:900;font-size:10px;letter-spacing:.1em;text-transform:uppercase;padding:3px 9px;border-radius:4px;display:flex;align-items:center;gap:4px}"
        ".hls-dot{width:5px;height:5px;border-radius:50%;background:var(--accent);animation:blink 1.4s ease-in-out infinite}"
        ".loading-cover{position:absolute;inset:0;z-index:4;background:rgba(0,0,0,.7);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;transition:opacity .4s}"
        ".loading-cover.gone{opacity:0;pointer-events:none}"
        ".spinner{width:30px;height:30px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite}"
        "@keyframes spin{to{transform:rotate(360deg)}}"
        ".spinner-lbl{font-family:'Inter',sans-serif;font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted)}"
        ".card-foot{padding:12px 14px;display:flex;align-items:center;justify-content:space-between;gap:10px;border-top:1px solid var(--border)}"
        ".card-title{font-family:'Inter',sans-serif;font-weight:900;font-size:14px;text-transform:uppercase;letter-spacing:.03em;color:var(--text)}"
        ".card-sub{font-size:11px;color:var(--muted);margin-top:2px;font-weight:500}"
        ".btn-row{display:flex;gap:8px;flex-shrink:0}"
        ".btn{font-family:'Inter',sans-serif;font-weight:900;font-size:13px;text-transform:uppercase;letter-spacing:.07em;padding:9px 20px;border-radius:4px;border:none;cursor:pointer;transition:opacity .15s;white-space:nowrap}"
        ".btn:active{opacity:.7}"
        ".btn-play{background:var(--btn-play-bg);color:var(--btn-play-color)}"
        ".btn-stop{background:var(--btn-stop-bg);border:1px solid var(--btn-stop-bdr)!important;color:var(--btn-stop-color)}"
        ".btn-stop-dim{opacity:.22;cursor:not-allowed;pointer-events:none}"
        "</style>"
        "<script>"
        "(function(){"
        "try{var t=localStorage.getItem('bh-theme');"
        "if(t&&['dark','mono','light'].includes(t))document.documentElement.setAttribute('data-theme',t);"
        "}catch(e){}"
        "})();"
        "</script>"
        "</head><body>"
        '<div class="topbar"><div class="logo">Broadcast<span>Hub</span></div>'
        '<a href="/" class="desktop-link">Desktop ↗</a></div>'
        '<div class="section-lbl">Live Inputs</div>'
        '<div class="cards">' + cards_html + "</div>"
        "<script>"
        "const _hlsPlayers={};"
        "function wireVideo(id){"
        "  const vid=document.getElementById('vid-'+id);"
        "  const cover=document.getElementById('cover-'+id);"
        "  const placeholder=document.getElementById('placeholder-'+id);"
        "  const url='/hls/'+id+'/index.m3u8';"
        "  vid.style.display='block';"
        "  if(placeholder)placeholder.style.display='none';"
        "  if(_hlsPlayers[id]&&_hlsPlayers[id]!=='native'){_hlsPlayers[id].destroy();}"
        "  if(Hls.isSupported()){"
        "    const h=new Hls({lowLatencyMode:true,maxBufferLength:10,liveSyncDurationCount:2});"
        "    _hlsPlayers[id]=h;"
        "    h.loadSource(url);"
        "    h.attachMedia(vid);"
        "    h.on(Hls.Events.MANIFEST_PARSED,()=>{"
        "      vid.play().catch(()=>{});"
        "      if(cover)cover.classList.add('gone');"
        "    });"
        "    h.on(Hls.Events.ERROR,(e,d)=>{"
        "      if(d.fatal){"
        "        if(cover){cover.classList.remove('gone');document.getElementById('cover-lbl-'+id).textContent='Reconnecting…';}"
        "        setTimeout(()=>wireVideo(id),3000);"
        "      }"
        "    });"
        "  } else if(vid.canPlayType('application/vnd.apple.mpegurl')){"
        "    _hlsPlayers[id]='native';"
        "    vid.src=url;"
        "    vid.addEventListener('loadedmetadata',()=>vid.play().catch(()=>{}),{once:true});"
        "    vid.addEventListener('playing',()=>{if(cover)cover.classList.add('gone');},{once:true});"
        "    vid.addEventListener('error',()=>{"
        "      if(cover){cover.classList.remove('gone');document.getElementById('cover-lbl-'+id).textContent='Reconnecting…';}"
        "      setTimeout(()=>wireVideo(id),3000);"
        "    });"
        "  } else {"
        "    document.getElementById('cover-lbl-'+id).textContent='Not supported';"
        "  }"
        "}"
        "async function pollReady(id,cb){"
        "  const url='/hls/'+id+'/index.m3u8';"
        "  for(let i=0;i<30;i++){"
        "    try{"
        "      const r=await fetch(url,{cache:'no-store'});"
        "      if(r.ok){"
        "        const txt=await r.text();"
        "        if(txt.split('\\n').some(l=>l.trim()&&!l.startsWith('#'))){cb();return;}"
        "      }"
        "    }catch(e){}"
        "    await new Promise(r=>setTimeout(r,500));"
        "  }"
        "  cb();"
        "}"
        "async function startHLS(id){"
        "  const playBtn=document.getElementById('playbtn-'+id);"
        "  if(playBtn){playBtn.textContent='…';playBtn.disabled=true;}"
        "  const cover=document.getElementById('cover-'+id);"
        "  const lbl=document.getElementById('cover-lbl-'+id);"
        "  if(cover)cover.classList.remove('gone');"
        "  if(lbl)lbl.textContent='Starting…';"
        "  try{await fetch('/hls/'+id+'/index.m3u8',{cache:'no-store'});}catch(e){}"
        "  if(lbl)lbl.textContent='Waiting for segments…';"
        "  pollReady(id,()=>{"
        "    document.getElementById('card-'+id).classList.add('hls-on');"
        "    const sub=document.getElementById('sub-'+id);"
        "    if(sub)sub.textContent='h264_qsv · HLS streaming';"
        "    if(lbl)lbl.textContent='Buffering…';"
        "    wireVideo(id);"
        "    if(playBtn){playBtn.textContent='▶ Play';playBtn.disabled=false;}"
        "    const stopBtn=document.getElementById('stopbtn-'+id);"
        "    if(stopBtn){"
        "      stopBtn.disabled=false;"
        "      stopBtn.classList.remove('btn-stop-dim');"
        "      stopBtn.onclick=()=>stopHLS(id);"
        "    }"
        "  });"
        "}"
        "async function stopHLS(id){"
        "  const stopBtn=document.getElementById('stopbtn-'+id);"
        "  if(stopBtn){stopBtn.textContent='…';stopBtn.disabled=true;}"
        "  const vid=document.getElementById('vid-'+id);"
        "  if(_hlsPlayers[id]&&_hlsPlayers[id]!=='native'){_hlsPlayers[id].destroy();delete _hlsPlayers[id];}"
        "  if(vid){vid.pause();vid.removeAttribute('src');vid.load();vid.style.display='none';}"
        "  await fetch('/hls/stop/'+id,{method:'POST'});"
        "  document.getElementById('card-'+id).classList.remove('hls-on');"
        "  const cover=document.getElementById('cover-'+id);"
        "  const lbl=document.getElementById('cover-lbl-'+id);"
        "  if(cover)cover.classList.add('gone');"
        "  const placeholder=document.getElementById('placeholder-'+id);"
        "  if(placeholder)placeholder.style.display='';"
        "  const sub=document.getElementById('sub-'+id);"
        "  if(sub)sub.textContent='Tap Play to start feed';"
        "  if(stopBtn){stopBtn.textContent='■ Stop';stopBtn.disabled=true;stopBtn.classList.add('btn-stop-dim');}"
        "}"
        f"document.addEventListener('DOMContentLoaded',()=>{{"
        f"  for(const id of {already_hls_js}){{"
        "    pollReady(id,()=>wireVideo(id));"
        "  }"
        "});"
        "</script></body></html>"
    )



# ---------------------------------------------------------------------------
# render_multiview
# ---------------------------------------------------------------------------

def render_multiview(all_ids: list, live_ids: list, cfg: dict) -> str:
    """Render the quad multiviewer page.

    Parameters:
      all_ids  : ordered list of all configured input key strings
      live_ids : keys currently running in active_inputs
      cfg      : snapshot of input_config dict (for signal/encoder info)
    """

    ids_js = str(all_ids).replace("'", '"')
    live_js = str(live_ids).replace("'", '"')

    # Build label map for JS
    label_map = {k: _label(k) for k in all_ids}
    label_js = json.dumps(label_map)

    # Build encoder/signal info map
    meta_map = {
        k: {
            "encoder": cfg.get(k, {}).get("encoder", ""),
            "signal":  cfg.get(k, {}).get("signal", "UNKNOWN"),
            "desc":    cfg.get(k, {}).get("desc", ""),
        }
        for k in all_ids
    }
    meta_js = json.dumps(meta_map)

    return f"""<!DOCTYPE html>
<html data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Multiview — Broadcast Hub</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&display=swap" rel="stylesheet">
<script>
  (function(){{
    try {{
      var t = localStorage.getItem('bh-theme');
      if (t && ['dark','mono','light'].includes(t))
        document.documentElement.setAttribute('data-theme', t);
    }} catch(e) {{}}
  }})();
</script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root, [data-theme="dark"] {{
    --bg:          #090d1a;
    --bg-topbar:   rgba(9,13,26,.97);
    --surface:     #0b0f22;
    --border:      #1c2540;
    --border-hi:   #2a3560;
    --text:        #c0cce8;
    --muted:       #3a4870;
    --dim:         #1c2540;
    --accent:      #00e5ff;
    --accent-dim:  rgba(0,229,255,.15);
    --accent-bdr:  rgba(0,229,255,.35);
    --live:        #ff0066;
    --live-bg:     rgba(255,0,102,.15);
    --live-bdr:    rgba(255,0,102,.35);
    --cell-bg:     #0b0f22;
    --thumb-bg:    #040609;
    --thumb-num:   #0e1428;
    --foot-bg:     #0b0f22;
  }}
  [data-theme="mono"] {{
    --bg:          #100e06;
    --bg-topbar:   rgba(12,10,4,.98);
    --surface:     #0c0a04;
    --border:      #2a1e08;
    --border-hi:   #3a2810;
    --text:        #e8d0a0;
    --muted:       #5a3818;
    --dim:         #2a1e08;
    --accent:      #ff6600;
    --accent-dim:  rgba(255,102,0,.15);
    --accent-bdr:  rgba(255,102,0,.4);
    --live:        #ff2200;
    --live-bg:     rgba(255,34,0,.15);
    --live-bdr:    rgba(255,34,0,.4);
    --cell-bg:     #0c0a04;
    --thumb-bg:    #070604;
    --thumb-num:   #1a1208;
    --foot-bg:     #0c0a04;
  }}
  [data-theme="light"] {{
    --bg:          #f3f5fa;
    --bg-topbar:   rgba(26,31,56,.98);
    --surface:     #fff;
    --border:      #c8cedd;
    --border-hi:   #b0b8d0;
    --text:        #1a1f38;
    --muted:       #6878a8;
    --dim:         #e4e8f4;
    --accent:      #4d9fff;
    --accent-dim:  rgba(77,159,255,.12);
    --accent-bdr:  rgba(77,159,255,.4);
    --live:        #dc2626;
    --live-bg:     rgba(220,38,38,.1);
    --live-bdr:    rgba(220,38,38,.35);
    --cell-bg:     #fff;
    --thumb-bg:    #edf0f8;
    --thumb-num:   #c0c8de;
    --foot-bg:     #fff;
  }}

  @keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:.15}} }}
  @keyframes scan  {{ 0%{{top:-100%}} 100%{{top:200%}} }}

  body {{
    background: var(--bg); color: var(--text);
    font-family: 'Inter', sans-serif;
    display: flex; flex-direction: column;
    height: 100vh; overflow: hidden;
  }}

  /* ── Topbar ── */
  .topbar {{
    padding: 11px 18px; border-bottom: 1px solid var(--border);
    background: var(--bg-topbar); backdrop-filter: blur(14px);
    display: flex; align-items: center; gap: 12px; flex-shrink: 0;
  }}
  .logo {{ font-weight: 900; font-style: italic; font-size: 16px;
           text-transform: uppercase; color: var(--text); text-decoration: none; }}
  .logo span {{ color: var(--accent); }}
  .page-title {{ font-size: 10px; font-weight: 700; text-transform: uppercase;
                 letter-spacing: .14em; color: var(--muted); }}
  .spacer {{ flex: 1; }}
  .nav-link {{
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .08em; color: var(--muted); text-decoration: none;
    padding: 5px 10px; border-radius: 4px; border: 1px solid var(--border);
    transition: color .15s;
  }}
  .nav-link:hover {{ color: var(--text); }}
  .nav-link.active {{ color: var(--accent); background: var(--accent-dim); border-color: var(--accent-bdr); }}

  /* ── Grid ── */
  #mv-grid {{
    flex: 1; display: grid;
    grid-template-columns: 1fr 1fr;
    grid-template-rows: 1fr 1fr;
    gap: 3px; background: var(--bg); padding: 3px;
    min-height: 0;
  }}

  /* Layout variants */
  #mv-grid.layout-1x1 {{ grid-template-columns: 1fr; grid-template-rows: 1fr; }}
  #mv-grid.layout-1x1 .mv-cell:not(:first-child) {{ display: none; }}

  #mv-grid.layout-1plus3 {{ grid-template-columns: 2fr 1fr; grid-template-rows: 1fr 1fr; }}
  #mv-grid.layout-1plus3 .mv-cell:first-child {{ grid-row: 1 / 3; }}

  #mv-grid.layout-2plus2 {{ grid-template-columns: 1fr 1fr; grid-template-rows: auto 1fr; }}

  /* ── Cell ── */
  .mv-cell {{
    background: var(--cell-bg); border: 1px solid var(--border);
    border-radius: 4px; display: flex; flex-direction: column;
    overflow: hidden; transition: border-color .2s; min-height: 0;
  }}
  .mv-cell.live {{ border-color: var(--accent-bdr); }}

  /* ── Video area ── */
  .mv-thumb {{
    flex: 1; background: var(--thumb-bg);
    display: flex; align-items: center; justify-content: center;
    position: relative; overflow: hidden; min-height: 0;
  }}
  .mv-thumb video {{
    width: 100%; height: 100%; object-fit: contain;
    display: none; background: #000;
  }}
  .mv-placeholder {{
    position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
  }}
  .mv-num {{
    font-size: 28px; font-weight: 900; font-style: italic;
    color: var(--thumb-num); user-select: none;
  }}
  .mv-scan {{
    position: absolute; left: 0; right: 0; height: 2px;
    background: var(--accent-dim);
    animation: scan 3s linear infinite;
    display: none;
  }}
  .mv-cell.live .mv-scan {{ display: block; }}
  .mv-nosig {{
    position: absolute; bottom: 8px; left: 0; right: 0; text-align: center;
    font-size: 8px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .14em; color: var(--muted);
  }}

  /* Badges */
  .mv-badge {{
    position: absolute; font-size: 9px; font-weight: 900;
    text-transform: uppercase; letter-spacing: .07em;
    padding: 2px 7px; border-radius: 3px;
    display: flex; align-items: center; gap: 4px;
  }}
  .mv-live-b {{ top: 7px; left: 7px; background: var(--live-bg); border: 1px solid var(--live-bdr); color: var(--live); }}
  .mv-sig-b  {{ top: 7px; right: 7px; background: var(--accent-dim); border: 1px solid var(--border); color: var(--muted); font-size: 8px; }}
  .mv-rec-b  {{ bottom: 7px; left: 7px; background: var(--live-bg); border: 1px solid var(--live-bdr); color: var(--live); }}
  .blink-dot {{ width: 5px; height: 5px; border-radius: 50%; background: currentColor;
                animation: blink 1.4s ease-in-out infinite; }}

  /* ── Cell footer ── */
  .mv-foot {{
    padding: 6px 9px; border-top: 1px solid var(--border);
    display: flex; align-items: center; gap: 7px;
    background: var(--foot-bg); flex-shrink: 0;
  }}
  .mv-title {{ font-size: 11px; font-weight: 900; text-transform: uppercase;
               letter-spacing: .03em; color: var(--text); }}
  .mv-sub   {{ font-size: 10px; color: var(--muted); margin-top: 1px; }}
  .mv-sel {{
    font-size: 10px; font-weight: 700; background: var(--dim);
    border: 1px solid var(--border); color: var(--text);
    padding: 4px 6px; border-radius: 3px; outline: none;
    font-family: 'Inter', sans-serif; cursor: pointer; max-width: 140px; flex-shrink: 0;
  }}
  .mv-sel:focus {{ border-color: var(--accent); }}
  .mv-btn {{
    font-size: 9px; font-weight: 900; text-transform: uppercase; letter-spacing: .07em;
    padding: 4px 9px; border-radius: 3px; cursor: pointer;
    font-family: 'Inter', sans-serif; flex-shrink: 0;
    background: var(--dim); border: 1px solid var(--border); color: var(--muted);
    transition: opacity .15s;
  }}
  .mv-btn:hover {{ opacity: .8; }}
  .mv-btn.full {{ border-color: var(--accent-bdr); color: var(--accent); background: var(--accent-dim); }}

  /* ── Controls bar ── */
  .ctrl-bar {{
    padding: 9px 18px; border-top: 1px solid var(--border);
    background: var(--surface);
    display: flex; align-items: center; gap: 10px; flex-shrink: 0;
  }}
  .ctrl-lbl {{ font-size: 9px; font-weight: 700; text-transform: uppercase;
               letter-spacing: .12em; color: var(--muted); }}
  .layout-btn {{
    font-size: 9px; font-weight: 900; text-transform: uppercase; letter-spacing: .07em;
    padding: 5px 11px; border-radius: 3px; border: 1px solid var(--border);
    background: var(--dim); color: var(--muted); cursor: pointer;
    font-family: 'Inter', sans-serif; transition: opacity .15s;
  }}
  .layout-btn:hover {{ opacity: .8; }}
  .layout-btn.on {{ background: var(--accent-dim); border-color: var(--accent-bdr); color: var(--accent); }}
  .ctrl-div {{ width: 1px; height: 20px; background: var(--border); margin: 0 2px; }}
  .ctrl-btn {{
    font-size: 9px; font-weight: 900; text-transform: uppercase; letter-spacing: .07em;
    padding: 5px 11px; border-radius: 3px; border: 1px solid var(--border);
    background: var(--dim); color: var(--muted); cursor: pointer;
    font-family: 'Inter', sans-serif; transition: opacity .15s;
  }}
  .ctrl-btn:hover {{ opacity: .8; }}
  .live-count {{ font-size: 9px; font-weight: 700; text-transform: uppercase;
                 letter-spacing: .1em; color: var(--accent); }}
  .back-lnk {{ font-size: 10px; font-weight: 700; text-transform: uppercase;
               letter-spacing: .08em; color: var(--muted); text-decoration: none;
               margin-left: auto; transition: color .15s; }}
  .back-lnk:hover {{ color: var(--text); }}
</style>
<script src="https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.min.js"></script>
</head>
<body>

<div class="topbar">
  <a href="/" class="logo">Broadcast<span>Hub</span></a>
  <span class="page-title">/ Multiview</span>
  <div class="spacer"></div>
  <a href="/" class="nav-link">Dashboard</a>
  <a href="/mobile" class="nav-link">Mobile ↗</a>
  <a href="/multiview" class="nav-link active">Multiview</a>
  <a href="/logs" class="nav-link">Log ↗</a>
</div>

<div id="mv-grid">
  <div class="mv-cell" id="cell-0">
    <div class="mv-thumb" id="thumb-0">
      <div class="mv-scan"></div>
      <div class="mv-placeholder" id="placeholder-0"><div class="mv-num" id="num-0">—</div></div>
      <video id="video-0" playsinline muted></video>
      <div class="mv-badge mv-live-b" id="live-badge-0" style="display:none"><span class="blink-dot"></span> Live</div>
      <div class="mv-badge mv-sig-b" id="sig-badge-0"></div>
      <div class="mv-badge mv-rec-b" id="rec-badge-0" style="display:none"><span class="blink-dot"></span> <span id="rec-timer-0"></span></div>
    </div>
    <div class="mv-foot">
      <div style="flex:1;min-width:0">
        <div class="mv-title" id="title-0">—</div>
        <div class="mv-sub" id="sub-0">Select an input</div>
      </div>
      <select class="mv-sel" id="sel-0" onchange="assignInput(0, this.value)">
        <option value="">— None —</option>
      </select>
      <button class="mv-btn" id="play-btn-0" onclick="playCell(0)" title="Play">&#9654; Play</button>
      <button class="mv-btn" id="stop-btn-0" onclick="stopPlayer(0)" title="Stop" style="display:none">&#9632; Stop</button>
      <button class="mv-btn full" onclick="toggleFullscreen(0)" title="Fullscreen">⛶</button>
    </div>
  </div>

  <div class="mv-cell" id="cell-1">
    <div class="mv-thumb" id="thumb-1">
      <div class="mv-scan"></div>
      <div class="mv-placeholder" id="placeholder-1"><div class="mv-num" id="num-1">—</div></div>
      <video id="video-1" playsinline muted></video>
      <div class="mv-badge mv-live-b" id="live-badge-1" style="display:none"><span class="blink-dot"></span> Live</div>
      <div class="mv-badge mv-sig-b" id="sig-badge-1"></div>
      <div class="mv-badge mv-rec-b" id="rec-badge-1" style="display:none"><span class="blink-dot"></span> <span id="rec-timer-1"></span></div>
    </div>
    <div class="mv-foot">
      <div style="flex:1;min-width:0">
        <div class="mv-title" id="title-1">—</div>
        <div class="mv-sub" id="sub-1">Select an input</div>
      </div>
      <select class="mv-sel" id="sel-1" onchange="assignInput(1, this.value)">
        <option value="">— None —</option>
      </select>
      <button class="mv-btn" id="play-btn-1" onclick="playCell(1)" title="Play">&#9654; Play</button>
      <button class="mv-btn" id="stop-btn-1" onclick="stopPlayer(1)" title="Stop" style="display:none">&#9632; Stop</button>
      <button class="mv-btn full" onclick="toggleFullscreen(1)" title="Fullscreen">⛶</button>
    </div>
  </div>

  <div class="mv-cell" id="cell-2">
    <div class="mv-thumb" id="thumb-2">
      <div class="mv-scan"></div>
      <div class="mv-placeholder" id="placeholder-2"><div class="mv-num" id="num-2">—</div></div>
      <video id="video-2" playsinline muted></video>
      <div class="mv-badge mv-live-b" id="live-badge-2" style="display:none"><span class="blink-dot"></span> Live</div>
      <div class="mv-badge mv-sig-b" id="sig-badge-2"></div>
      <div class="mv-badge mv-rec-b" id="rec-badge-2" style="display:none"><span class="blink-dot"></span> <span id="rec-timer-2"></span></div>
    </div>
    <div class="mv-foot">
      <div style="flex:1;min-width:0">
        <div class="mv-title" id="title-2">—</div>
        <div class="mv-sub" id="sub-2">Select an input</div>
      </div>
      <select class="mv-sel" id="sel-2" onchange="assignInput(2, this.value)">
        <option value="">— None —</option>
      </select>
      <button class="mv-btn" id="play-btn-2" onclick="playCell(2)" title="Play">&#9654; Play</button>
      <button class="mv-btn" id="stop-btn-2" onclick="stopPlayer(2)" title="Stop" style="display:none">&#9632; Stop</button>
      <button class="mv-btn full" onclick="toggleFullscreen(2)" title="Fullscreen">⛶</button>
    </div>
  </div>

  <div class="mv-cell" id="cell-3">
    <div class="mv-thumb" id="thumb-3">
      <div class="mv-scan"></div>
      <div class="mv-placeholder" id="placeholder-3"><div class="mv-num" id="num-3">—</div></div>
      <video id="video-3" playsinline muted></video>
      <div class="mv-badge mv-live-b" id="live-badge-3" style="display:none"><span class="blink-dot"></span> Live</div>
      <div class="mv-badge mv-sig-b" id="sig-badge-3"></div>
      <div class="mv-badge mv-rec-b" id="rec-badge-3" style="display:none"><span class="blink-dot"></span> <span id="rec-timer-3"></span></div>
    </div>
    <div class="mv-foot">
      <div style="flex:1;min-width:0">
        <div class="mv-title" id="title-3">—</div>
        <div class="mv-sub" id="sub-3">Select an input</div>
      </div>
      <select class="mv-sel" id="sel-3" onchange="assignInput(3, this.value)">
        <option value="">— None —</option>
      </select>
      <button class="mv-btn" id="play-btn-3" onclick="playCell(3)" title="Play">&#9654; Play</button>
      <button class="mv-btn" id="stop-btn-3" onclick="stopPlayer(3)" title="Stop" style="display:none">&#9632; Stop</button>
      <button class="mv-btn full" onclick="toggleFullscreen(3)" title="Fullscreen">⛶</button>
    </div>
  </div>
</div>

<div class="ctrl-bar">
  <span class="ctrl-lbl">Layout</span>
  <button class="layout-btn on" id="lb-2x2"     onclick="setLayout('2x2')">2 × 2</button>
  <button class="layout-btn"   id="lb-1plus3"   onclick="setLayout('1plus3')">1 + 3</button>
  <button class="layout-btn"   id="lb-1x1"      onclick="setLayout('1x1')">1 × 1</button>
  <div class="ctrl-div"></div>
  <button class="ctrl-btn" onclick="muteAll(true)">Mute All</button>
  <button class="ctrl-btn" onclick="muteAll(false)">Unmute All</button>
  <div class="ctrl-div"></div>
  <span class="live-count" id="live-count">0 live</span>
  <a class="back-lnk" href="/">← Dashboard</a>
</div>

<script>
  const ALL_IDS   = {ids_js};
  const LIVE_IDS  = {live_js};
  const LABELS    = {label_js};
  const META      = {meta_js};

  // ── State ────────────────────────────────────────────────────────────────
  const assignments = [null, null, null, null];  // input_id per quadrant
  const players     = [null, null, null, null];  // mpegts player per quadrant
  let   recTimers   = {{}};                        // input_id -> started_at (from SSE)
  let   liveSet     = new Set(LIVE_IDS);
  let   recSet      = {{}};                        // input_id -> elapsed seconds

  // ── Build dropdowns ──────────────────────────────────────────────────────
  function buildSelects() {{
    for (let q = 0; q < 4; q++) {{
      const sel = document.getElementById('sel-' + q);
      sel.innerHTML = '<option value="">— None —</option>';
      for (const id of ALL_IDS) {{
        const opt = document.createElement('option');
        opt.value = id;
        opt.textContent = LABELS[id] || id;
        sel.appendChild(opt);
      }}
    }}
    // Auto-assign first inputs on first load if nothing saved
    const saved = loadSaved();
    if (saved) {{
      saved.forEach((id, q) => {{ if (id) assignInput(q, id, true); }});
    }} else {{
      ALL_IDS.slice(0, 4).forEach((id, q) => assignInput(q, id, true));
    }}
  }}

  // ── Persistence ──────────────────────────────────────────────────────────
  function saveSaved() {{
    try {{ localStorage.setItem('bh-mv', JSON.stringify(assignments)); }} catch(e) {{}}
  }}
  function loadSaved() {{
    try {{
      const v = localStorage.getItem('bh-mv');
      return v ? JSON.parse(v) : null;
    }} catch(e) {{ return null; }}
  }}

  // ── Assign input to quadrant ─────────────────────────────────────────────
  function assignInput(q, inputId, skipSave) {{
    stopPlayer(q);
    assignments[q] = inputId || null;
    document.getElementById('sel-' + q).value = inputId || '';
    if (!skipSave) saveSaved();
    updateCell(q);
    // Don't auto-start — user presses Play per cell
  }}

  // ── Update cell UI ────────────────────────────────────────────────────────
  function updateCell(q) {{
    const id    = assignments[q];
    const cell  = document.getElementById('cell-' + q);
    const isLive = id && liveSet.has(id);
    const m      = id ? (META[id] || {{}}) : {{}};

    document.getElementById('num-' + q).textContent   = id || '—';
    document.getElementById('title-' + q).textContent = id ? (LABELS[id] || id) : '—';

    const sub = document.getElementById('sub-' + q);
    if (!id)       sub.textContent = 'Select an input';
    else if (!isLive) sub.textContent = 'Offline — waiting for signal';
    else           sub.textContent = (m.encoder || '') + (m.desc ? ' · ' + m.desc : '') + ' · ready';

    cell.classList.toggle('live', !!isLive);

    // Live badge
    const lb = document.getElementById('live-badge-' + q);
    lb.style.display = isLive ? 'flex' : 'none';

    // Signal badge
    const sb = document.getElementById('sig-badge-' + q);
    sb.textContent = (id && m.desc) ? m.desc : '';

    // Recording badge
    const rb = document.getElementById('rec-badge-' + q);
    const isRec = id && recSet[id] !== undefined;
    rb.style.display = isRec ? 'flex' : 'none';

    // Placeholder visibility
    const vid = document.getElementById('video-' + q);
    const ph  = document.getElementById('placeholder-' + q);
    if (vid.style.display === 'block') {{ ph.style.display = 'none'; }}
    else {{ ph.style.display = ''; }}
  }}

  // ── Play / Stop per cell ─────────────────────────────────────────────────
  function playCell(q) {{
    const id = assignments[q];
    if (!id) return;
    const vid = document.getElementById('video-' + q);
    const ph  = document.getElementById('placeholder-' + q);
    if (players[q]) {{ players[q].destroy(); players[q] = null; }}
    const p = mpegts.createPlayer({{ type: 'mpegts', isLive: true, url: '/multiview-feed/' + id }});
    p.attachMediaElement(vid);
    p.load();
    vid.play().catch(() => {{}});
    vid.style.display = 'block';
    ph.style.display  = 'none';
    players[q] = p;
    _setPlayBtns(q, true);
  }}

  function _setPlayBtns(q, playing) {{
    const pb = document.getElementById('play-btn-' + q);
    const sb = document.getElementById('stop-btn-' + q);
    if (pb) pb.style.display = playing ? 'none' : '';
    if (sb) sb.style.display = playing ? ''     : 'none';
  }}

  function stopPlayer(q) {{
    if (players[q]) {{ players[q].destroy(); players[q] = null; }}
    const vid = document.getElementById('video-' + q);
    const ph  = document.getElementById('placeholder-' + q);
    if (vid) {{ vid.pause(); vid.src = ''; vid.style.display = 'none'; }}
    if (ph)  ph.style.display = '';
    _setPlayBtns(q, false);
  }}

  // ── Mute all / unmute all ────────────────────────────────────────────────
  function muteAll(muted) {{
    for (let q = 0; q < 4; q++) {{
      const v = document.getElementById('video-' + q);
      if (v) v.muted = muted;
    }}
  }}

  // ── Layout ───────────────────────────────────────────────────────────────
  let currentLayout = '2x2';
  function setLayout(layout) {{
    currentLayout = layout;
    const grid = document.getElementById('mv-grid');
    grid.className = 'layout-' + layout;
    document.querySelectorAll('.layout-btn').forEach(b => b.classList.remove('on'));
    document.getElementById('lb-' + layout.replace('+','plus'))?.classList.add('on');
    try {{ localStorage.setItem('bh-mv-layout', layout); }} catch(e) {{}}
  }}

  // ── Fullscreen (expand cell 0 in 1x1 mode) ───────────────────────────────
  function toggleFullscreen(q) {{
    if (currentLayout === '1x1' && assignments[0] === assignments[q]) {{
      setLayout('2x2'); return;
    }}
    // Swap clicked quadrant to position 0 then go 1x1
    const id = assignments[q];
    assignInput(q, assignments[0], true);
    assignInput(0, id, true);
    saveSaved();
    setLayout('1x1');
  }}

  // ── SSE stats — live status + recording timers ────────────────────────────
  let recIntervalId = null;

  function fmtElapsed(s) {{
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
    return [h,m,sec].map(v=>String(v).padStart(2,'0')).join(':');
  }}

  function tickRecTimers() {{
    for (let q = 0; q < 4; q++) {{
      const id = assignments[q];
      if (id && recSet[id] !== undefined) {{
        recSet[id]++;
        const el = document.getElementById('rec-timer-' + q);
        if (el) el.textContent = fmtElapsed(recSet[id]);
      }}
    }}
  }}

  (function connectSSE() {{
    const es = new EventSource('/api/stats');
    es.onmessage = e => {{
      try {{
        const d = JSON.parse(e.data);
        const newLive = new Set(d.input_ids?.filter(id => d.inputs?.[id]) || []);

        // Detect live changes — only stop players, never block starts
        for (let q = 0; q < 4; q++) {{
          const id = assignments[q];
          if (!id) continue;
          const wasLive = liveSet.has(id);
          const isLive  = newLive.has(id);
          // Only stop if input went away AND player is running
          if (wasLive && !isLive && players[q]) stopPlayer(q);
        }}
        liveSet = newLive;

        // Recording timers
        const newRec = {{}};
        for (const r of (d.recordings || [])) newRec[r.input_id] = r.elapsed;
        recSet = newRec;

        // Update live count
        const liveCount = [...new Set(assignments.filter(id => id && liveSet.has(id)))].length;
        document.getElementById('live-count').textContent = liveCount + ' live';

        // Refresh all cells
        for (let q = 0; q < 4; q++) updateCell(q);

      }} catch(err) {{ console.warn('SSE', err); }}
    }};
    es.onerror = () => {{ es.close(); setTimeout(connectSSE, 3000); }};
  }})();

  // ── Init ─────────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {{
    // Restore layout
    try {{
      const l = localStorage.getItem('bh-mv-layout');
      if (l) setLayout(l);
    }} catch(e) {{}}

    buildSelects();

    // Tick recording timers every second
    recIntervalId = setInterval(tickRecTimers, 1000);
  }});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# render_dashboard
# ---------------------------------------------------------------------------
# Parameters:
#   live_inputs       : snapshot of active_inputs dict
#   cfg               : snapshot of input_config dict
#   current_input_ids : ordered list of active input key strings
#   recordings        : list of dicts {id, label, input_id, fmt, path, elapsed, duration}
#   scheduled         : list of dicts {id, label, input_id, fmt, path, start, duration}
#   hls_active        : snapshot of active_hls dict
#   base_url          : str  e.g. "http://192.168.1.10:6502"
#   should_be_live    : snapshot of SHOULD_BE_LIVE dict  (for fault/restart badges)
#   format_ext        : FORMAT_EXT constant dict

def render_channels(all_ids: list, cfg: dict) -> str:
    """Render the standalone Channels page: tuner/device mapping (which
    ADB/Roku remote IP is plugged into which physical input) plus the
    channel list, add/edit/delete, Test Tune, and Release actions.

    Split out from the main dashboard because channel management is
    occasional configuration work, not something that needs to compete
    for space with the live-monitoring panels (fan control, telemetry,
    recording) on every page load.

    Parameters:
      all_ids : ordered list of all configured input key strings
      cfg     : snapshot of input_config dict (for remote_type/remote_ip)
    """
    device_rows_html = "\n".join(
        f"""
        <div class="device-row" data-input-id="{iid}">
          <span class="device-row-label">{_label(iid)}</span>
          <select class="dl-input device-remote-type" style="width:120px" onchange="onDeviceRemoteTypeChange('{iid}')">
            <option value="none" {"selected" if cfg.get(iid, {}).get("remote_type", "none") == "none" else ""}>No Remote</option>
            <option value="adb"  {"selected" if cfg.get(iid, {}).get("remote_type") == "adb"  else ""}>ADB (Android TV)</option>
            <option value="roku" {"selected" if cfg.get(iid, {}).get("remote_type") == "roku" else ""}>Roku (ECP)</option>
          </select>
          <input class="dl-input device-remote-ip" type="text" style="flex:1"
            placeholder="Device IP (e.g. 192.168.1.130)"
            value="{cfg.get(iid, {}).get('remote_ip', '')}"
            {"disabled" if cfg.get(iid, {}).get("remote_type", "none") == "none" else ""}>
          <button class="btn q-btn" style="font-size:10px;padding:5px 10px" onclick="saveDeviceRemote('{iid}')">Save</button>
          <span class="device-row-status" id="device-status-{iid}"></span>
        </div>"""
        for iid in all_ids
    )

    return f"""<!DOCTYPE html>
<html data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Channels — Broadcast Hub</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&display=swap" rel="stylesheet">
<script>
  (function(){{
    try {{
      var t = localStorage.getItem('bh-theme');
      if (t && ['dark','mono','light'].includes(t))
        document.documentElement.setAttribute('data-theme', t);
    }} catch(e) {{}}
  }})();
</script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root, [data-theme="dark"] {{
    --bg:          #090d1a;
    --bg-topbar:   rgba(9,13,26,.97);
    --surface:     #0b0f22;
    --border:      #1c2540;
    --border-hi:   #2a3560;
    --text:        #c0cce8;
    --muted:       #3a4870;
    --dim:         #1c2540;
    --accent:      #00e5ff;
    --accent-dim:  rgba(0,229,255,.15);
    --accent-bdr:  rgba(0,229,255,.35);
    --live:        #ff0066;
    --purple:      #b464ff;
    --green:       #4dc8a0;
  }}
  [data-theme="mono"] {{
    --bg:          #100e06;
    --bg-topbar:   rgba(12,10,4,.98);
    --surface:     #0c0a04;
    --border:      #2a1e08;
    --border-hi:   #3a2810;
    --text:        #e8d0a0;
    --muted:       #5a3818;
    --dim:         #2a1e08;
    --accent:      #ff6600;
    --accent-dim:  rgba(255,102,0,.15);
    --accent-bdr:  rgba(255,102,0,.4);
    --live:        #ff2200;
    --purple:      #d896ff;
    --green:       #88cc00;
  }}
  [data-theme="light"] {{
    --bg:          #f3f5fa;
    --bg-topbar:   rgba(26,31,56,.98);
    --surface:     #fff;
    --border:      #c8cedd;
    --border-hi:   #b0b8d0;
    --text:        #1a1f38;
    --muted:       #6878a8;
    --dim:         #e4e8f4;
    --accent:      #4d9fff;
    --accent-dim:  rgba(77,159,255,.12);
    --accent-bdr:  rgba(77,159,255,.4);
    --live:        #dc2626;
    --purple:      #8a5fd6;
    --green:       #059669;
  }}

  body {{
    background: var(--bg); color: var(--text);
    font-family: 'Inter', sans-serif;
  }}

  .topbar {{
    padding: 11px 18px; border-bottom: 1px solid var(--border);
    background: var(--bg-topbar); backdrop-filter: blur(14px);
    display: flex; align-items: center; gap: 12px;
    position: sticky; top: 0; z-index: 50;
  }}
  .logo {{ font-weight: 900; font-style: italic; font-size: 16px;
           text-transform: uppercase; color: var(--text); text-decoration: none; }}
  .logo span {{ color: var(--accent); }}
  .page-title {{ font-size: 10px; font-weight: 700; text-transform: uppercase;
                 letter-spacing: .14em; color: var(--muted); }}
  .spacer {{ flex: 1; }}
  .nav-link {{
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .08em; color: var(--muted); text-decoration: none;
    padding: 5px 10px; border-radius: 4px; border: 1px solid var(--border);
    transition: color .15s;
  }}
  .nav-link:hover {{ color: var(--text); }}
  .nav-link.active {{ color: var(--accent); background: var(--accent-dim); border-color: var(--accent-bdr); }}

  .btn {{
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 11px;
    text-transform: uppercase; letter-spacing: .07em;
    padding: 8px 14px; border-radius: 4px; border: none;
    cursor: pointer; transition: opacity .15s, background .15s;
    white-space: nowrap; text-decoration: none;
    display: inline-flex; align-items: center; justify-content: center;
  }}
  .btn:active {{ opacity: .7; }}
  .q-btn {{
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 9px;
    text-transform: uppercase; letter-spacing: .1em;
    background: var(--accent-dim); border: 1px solid var(--accent-bdr);
    color: var(--accent); padding: 4px 9px; border-radius: 4px;
    cursor: pointer; transition: background .15s; white-space: nowrap;
  }}
  .q-btn:hover {{ background: rgba(0,229,255,.22); }}

  .overlay {{
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.82); backdrop-filter: blur(6px);
    z-index: 999; align-items: center; justify-content: center;
  }}
  .overlay.show {{ display: flex !important; }}
  .modal {{
    background: var(--surface); border: 1px solid var(--border-hi);
    border-radius: 4px; padding: 24px 28px;
    max-width: 95vw; max-height: 95vh; overflow-y: auto;
  }}
  .modal-hdr {{
    display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px;
  }}
  .modal-title {{ font-weight: 900; font-size: 15px; text-transform: uppercase; letter-spacing: .06em; color: var(--text); }}
  .modal-close {{ background: none; border: none; color: var(--muted); font-size: 24px; cursor: pointer; padding: 0; line-height: 1; }}
  .modal-close:hover {{ color: var(--text); }}

  .dl-grid {{
    display: grid; grid-template-columns: 90px 1fr; gap: 8px 10px; align-items: center;
  }}
  .dl-lbl {{
    font-size: 9px; font-weight: 900; text-transform: uppercase;
    letter-spacing: .1em; color: var(--muted); text-align: right;
  }}
  .dl-input {{
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    font-family: 'Inter', sans-serif; font-size: 12px; padding: 6px 9px;
    border-radius: 4px; outline: none; width: 100%;
  }}
  .dl-input:focus {{ border-color: var(--accent); }}
  .dl-input:disabled {{ opacity: .4; }}

  .page-wrap {{ max-width: 920px; margin: 0 auto; padding: 20px 16px 60px; }}
  .section-header {{ margin: 28px 0 10px; }}
  .section-lbl {{
    font-size: 11px; font-weight: 900; text-transform: uppercase;
    letter-spacing: .1em; color: var(--muted);
  }}
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; padding: 14px 16px;
  }}
  .device-row {{
    display: flex; align-items: center; gap: 8px;
    padding: 8px 0; border-bottom: 1px solid var(--border);
  }}
  .device-row:last-child {{ border-bottom: none; }}
  .device-row-label {{
    width: 110px; flex-shrink: 0; font-size: 12px; color: var(--text);
    font-family: 'Courier New', monospace;
  }}
  .device-row-status {{ font-size: 10px; color: var(--muted); min-width: 60px; text-align: right; }}
  .channel-row {{
    display: flex; align-items: center; gap: 10px;
    padding: 8px 10px; background: var(--bg); border: 1px solid var(--border);
    border-radius: 4px; margin-bottom: 6px;
  }}
</style>
</head>
<body>

<div class="topbar">
  <a href="/" class="logo">Broadcast<span>Hub</span></a>
  <span class="page-title">/ Channels</span>
  <div class="spacer"></div>
  <a href="/" class="nav-link">Dashboard</a>
  <a href="/mobile" class="nav-link">Mobile ↗</a>
  <a href="/multiview" class="nav-link">Multiview</a>
  <a href="/channels" class="nav-link active">Channels</a>
  <a href="/logs" class="nav-link">Log ↗</a>
</div>

<div class="page-wrap">

  <div class="section-header"><div class="section-lbl">&#128246; Tuner Devices</div></div>
  <div class="card">
    <div style="font-size:11px;color:var(--muted);margin-bottom:10px">
      Link each physical input to the IP address of the Android/Roku device plugged into it.
      This is the same tuner pool the Channels section below auto-selects from when tuning —
      any input with a remote configured here is available as a tuner.
    </div>
    <div id="device-rows-container">
      {device_rows_html}
    </div>
  </div>

  <div class="section-header"><div class="section-lbl">&#9881; Provider Timing Settings</div></div>
  <div class="card">
    <div style="font-size:11px;color:var(--muted);margin-bottom:10px">
      These three timing values are safe to adjust and won't break tuning if set wrong —
      just make it faster/slower or less/more tolerant. Everything else about how a
      provider is tuned (deep-link format, app package, keycodes) is fixed in code,
      since those have to match the app exactly or tuning silently fails.
    </div>
    <div id="provider-settings-container"></div>
  </div>

  <div class="section-header"><div class="section-lbl">&#9881; Streaming Behavior</div></div>
  <div class="card">
    <div style="display:flex;align-items:flex-start;gap:12px">
      <input type="checkbox" id="same-client-eviction-toggle" style="margin-top:3px"
        onchange="saveStreamSettings()">
      <div>
        <label for="same-client-eviction-toggle" style="font-size:12px;font-weight:700;cursor:pointer">
          Stop old channel when the same client switches
        </label>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">
          On: correct for a single viewer browsing channels in a player like VLC —
          the previous channel is stopped the moment a new one is requested, instead of
          waiting up to 30-40 seconds to be noticed as abandoned.<br>
          Off: required if you use a DVR that records multiple channels at once
          (e.g. Channels DVR) — every recording comes from the same server, and with
          this on, starting a new one would incorrectly stop every other recording
          already in progress.
        </div>
      </div>
    </div>
    <div style="display:flex;align-items:flex-start;gap:12px;margin-top:16px">
      <input type="checkbox" id="edid-refresh-toggle" style="margin-top:3px"
        onchange="saveStreamSettings()">
      <div>
        <label for="edid-refresh-toggle" style="font-size:12px;font-weight:700;cursor:pointer">
          Refresh EDID on every tune
        </label>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">
          Re-writes the input's configured EDID right before every tune, forcing the
          connected device to re-negotiate its HDMI output. Confirmed via a live test to
          fix distorted/garbled audio — a manual EDID reload while watching a corrupted
          stream caused the video to visibly resync and the audio to come out clean.<br>
          Adds a small delay to every tune (typically under a second). Turn off to
          isolate it during testing, or if a particular setup doesn't need it.
        </div>
      </div>
    </div>
    <div style="display:flex;align-items:flex-start;gap:12px;margin-top:16px">
      <input type="checkbox" id="stray-sleep-toggle" style="margin-top:3px"
        onchange="saveStreamSettings()">
      <div>
        <label for="stray-sleep-toggle" style="font-size:12px;font-weight:700;cursor:pointer">
          Sleep devices that wake up on their own
        </label>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">
          Every device-sleep mechanism in this app is reactive — scoped to devices we
          ourselves tuned. A box that reboots on its own (e.g. a firmware update) and
          wakes up showing whatever channel it defaults to is otherwise completely
          invisible to us and would stay awake indefinitely, with no client ever having
          asked for it. This checks every tuner-pool device on the interval below and
          puts it back to sleep if it's awake with no tune of ours behind it.
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:8px">
          <label for="stray-sweep-interval" style="font-size:11px;color:var(--muted)">
            Check every
          </label>
          <input type="number" id="stray-sweep-interval" min="15" step="5" value="60"
            style="width:70px;font-size:11px;padding:3px 6px"
            onchange="saveStreamSettings()">
          <span style="font-size:11px;color:var(--muted)">seconds (minimum 15)</span>
        </div>
      </div>
    </div>
    <div id="stream-settings-status" style="font-size:10px;color:var(--muted);margin-top:8px"></div>
  </div>

  <div class="section-header">
    <div class="section-lbl">&#128225; Channels <span id="tuner-pool-info" style="font-weight:400;text-transform:none;letter-spacing:0;margin-left:8px"></span></div>
  </div>
  <div class="card">
    <div id="channels-list-container" style="display:flex;flex-direction:column;gap:6px"></div>
    <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
      <button class="btn q-btn" style="font-size:10px;padding:5px 12px"
        onclick="openChannelEditor(null)">+ Add Channel</button>
      <button class="btn q-btn" style="font-size:10px;padding:5px 12px"
        onclick="openM3uImport()">&#8593; Import M3U</button>
      <button class="btn q-btn" style="font-size:10px;padding:5px 12px"
        onclick="openRawM3uEditor()">&#128196; View / Edit Raw M3U</button>
      <button class="btn q-btn" style="font-size:10px;padding:5px 12px;color:var(--live);border-color:var(--live)"
        onclick="clearAllChannels()">&#128465; Clear All</button>
      <button class="btn q-btn" style="font-size:10px;padding:5px 12px;margin-left:auto"
        onclick="copyExportUrl()">&#8595; Copy Export URL</button>
    </div>
    <div id="export-url-status" style="font-size:10px;color:var(--muted);margin-top:6px"></div>
  </div>

</div>

<!-- Raw M3U view/edit modal -->
<div class="overlay" id="raw-m3u-overlay">
  <div class="modal" style="width:640px">
    <div class="modal-hdr">
      <div class="modal-title">Raw M3U</div>
      <button class="modal-close" onclick="closeRawM3uEditor()">&times;</button>
    </div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:10px">
      This is the M3U currently being served at the Export URL, generated from your
      configured channels (disabled channels are left out). Edit it directly and use
      one of the two buttons below, or just use it as a reference.
    </div>
    <textarea id="raw-m3u-text" rows="16" class="dl-input"
      style="font-family:'Courier New',monospace;font-size:11px;width:100%;resize:vertical"></textarea>
    <div id="raw-m3u-status" style="font-size:11px;color:var(--muted);margin-top:8px;min-height:14px"></div>
    <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end;flex-wrap:wrap">
      <button class="btn q-btn" onclick="closeRawM3uEditor()">Close</button>
      <button class="btn q-btn" onclick="submitRawM3uUpdate(false)">Update From This Text</button>
      <button class="btn q-btn" style="color:var(--live);border-color:var(--live)"
        onclick="submitRawM3uUpdate(true)">Replace All From This Text</button>
    </div>
  </div>
</div>

<!-- M3U import modal -->
<div class="overlay" id="m3u-import-overlay">
  <div class="modal" style="width:600px">
    <div class="modal-hdr">
      <div class="modal-title">Import M3U</div>
      <button class="modal-close" onclick="closeM3uImport()">&times;</button>
    </div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:10px">
      Paste an ah4c-style M3U playlist below. Existing channels with matching
      IDs are updated; everything else is added. Nothing is removed.
    </div>
    <textarea id="m3u-import-text" rows="12" class="dl-input"
      style="font-family:'Courier New',monospace;font-size:11px;width:100%;resize:vertical"
      placeholder="#EXTM3U&#10;#EXTINF:-1 channel-number=&quot;77&quot;,MeTV&#10;http://.../play/tuner/METV~83321f4e-..."></textarea>
    <div class="dl-grid" style="margin-top:10px">
      <label class="dl-lbl">Provider</label>
      <select class="dl-input" id="m3u-import-provider"></select>
    </div>
    <div id="m3u-import-status" style="font-size:11px;color:var(--muted);margin-top:8px;min-height:14px"></div>
    <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end">
      <button class="btn q-btn" onclick="closeM3uImport()">Cancel</button>
      <button class="btn q-btn" onclick="submitM3uImport()">Import</button>
    </div>
  </div>
</div>

<!-- Channel editor modal -->
<div class="overlay" id="channel-editor-overlay">
  <div class="modal" style="width:480px">
    <div class="modal-hdr">
      <div class="modal-title" id="channel-editor-title">Add Channel</div>
      <button class="modal-close" onclick="closeChannelEditor()">&times;</button>
    </div>
    <input type="hidden" id="ce-orig-id" value="">
    <div class="dl-grid">
      <label class="dl-lbl">Channel ID</label>
      <input class="dl-input" id="ce-channel-id" type="text" placeholder="TNTHD~acf51074-...">
      <label class="dl-lbl">Display Name</label>
      <input class="dl-input" id="ce-display-name" type="text" placeholder="TNT HD">
      <label class="dl-lbl">Guide Number</label>
      <input class="dl-input" id="ce-guide-number" type="text" placeholder="13.1">
      <label class="dl-lbl">Guide Station ID (optional)</label>
      <input class="dl-input" id="ce-guide-station" type="text" inputmode="numeric" placeholder="Gracenote station id, e.g. 158131">
      <label class="dl-lbl">Provider</label>
      <select class="dl-input" id="ce-provider"></select>
      <label class="dl-lbl">Call Sign</label>
      <input class="dl-input" id="ce-callsign" type="text" placeholder="TNTHD">
      <label class="dl-lbl">Content ID</label>
      <input class="dl-input" id="ce-content-id" type="text" placeholder="acf51074-6940-81d8-2355-c2eb610e0afc">
      <label class="dl-lbl">Settle Time (s)</label>
      <input class="dl-input" id="ce-settle-time" type="number" min="5" max="120" value="20">
    </div>
    <div id="ce-status" style="font-size:11px;color:var(--muted);margin-top:8px;min-height:14px"></div>
    <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end">
      <button class="btn q-btn" onclick="closeChannelEditor()">Cancel</button>
      <button class="btn q-btn" onclick="saveChannelEditor()">Save</button>
    </div>
  </div>
</div>

<script>
  function showToast(msg, ok=true) {{
    let t = document.getElementById('_toast');
    if (!t) {{
      t = document.createElement('div');
      t.id = '_toast';
      t.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);' +
        'padding:10px 18px;border-radius:6px;font-size:12px;z-index:9999;transition:opacity .3s;color:#fff';
      document.body.appendChild(t);
    }}
    t.style.background = ok ? '#1a7a4c' : '#a83232';
    t.textContent = msg;
    t.style.opacity = '1';
    clearTimeout(t._hideTimer);
    t._hideTimer = setTimeout(() => {{ t.style.opacity = '0'; }}, 3500);
  }}

  // ── Tuner device mapping ─────────────────────────────────────────────────────

  function onDeviceRemoteTypeChange(inputId) {{
    const row = document.querySelector(`.device-row[data-input-id="${{inputId}}"]`);
    const type = row.querySelector('.device-remote-type').value;
    row.querySelector('.device-remote-ip').disabled = (type === 'none');
  }}

  async function saveDeviceRemote(inputId) {{
    const row = document.querySelector(`.device-row[data-input-id="${{inputId}}"]`);
    const remoteType = row.querySelector('.device-remote-type').value;
    const remoteIp   = row.querySelector('.device-remote-ip').value.trim();
    const status     = document.getElementById(`device-status-${{inputId}}`);
    const fd = new FormData();
    fd.append('remote_type', remoteType);
    fd.append('remote_ip', remoteIp);
    try {{
      const res  = await fetch(`/input/${{encodeURIComponent(inputId)}}/set_remote`, {{method: 'POST', body: fd}});
      const data = await res.json();
      if (data.ok) {{
        if (status) status.textContent = 'Saved ✓';
        showToast(`${{inputId}} remote saved`);
        refreshChannelsList();
      }} else {{
        if (status) status.textContent = data.error || 'Failed';
        showToast(data.error || 'Save failed', false);
      }}
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  // ── Provider timing settings ─────────────────────────────────────────────────

  const PROVIDER_FIELD_LABELS = {{
    settle_after_ready_secs:  {{ label: 'Settle Delay (s)',       hint: 'Pause after the app reports playing, before capture starts. Covers any residual loading transition.' }},
    readiness_timeout_secs:   {{ label: 'Readiness Timeout (s)',  hint: "How long to wait for the app to report it is playing before giving up and trying the next tuner." }},
    heartbeat_interval_secs:  {{ label: 'Heartbeat Interval (s)', hint: "How often a keepalive is sent during playback. Keep below the app's own inactivity timeout (DirecTV: 5 min / 300s)." }},
  }};

  async function loadProviderSettings() {{
    const container = document.getElementById('provider-settings-container');
    if (!container) return;
    try {{
      const res  = await fetch('/channels/provider_settings');
      const data = await res.json();
      if (!data.ok) return;
      const ranges = data.field_ranges;
      container.innerHTML = Object.entries(data.providers).map(([key, p]) => {{
        const fields = Object.entries(p.values).map(([field, val]) => {{
          const meta  = PROVIDER_FIELD_LABELS[field] || {{ label: field, hint: '' }};
          const range = ranges[field] || {{}};
          return `
          <div style="display:grid;grid-template-columns:140px 1fr;gap:8px;align-items:start;margin-bottom:8px">
            <label class="dl-lbl" style="text-align:left;padding-top:6px" title="${{meta.hint}}">${{meta.label}}</label>
            <div>
              <input class="dl-input provider-field-input" type="number"
                data-provider="${{key}}" data-field="${{field}}"
                min="${{range.min}}" max="${{range.max}}" step="${{range.step}}" value="${{val}}"
                style="width:120px">
              <div style="font-size:10px;color:var(--muted);margin-top:3px">${{meta.hint}}</div>
            </div>
          </div>`;
        }}).join('');
        return `
        <div style="margin-bottom:10px;border-bottom:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:8px;padding-bottom:10px;cursor:pointer"
            onclick="toggleProviderCard('${{key}}')">
            <span id="provider-chevron-${{key}}" style="font-size:10px;color:var(--muted);transition:transform .15s;display:inline-block">&#9656;</span>
            <span style="font-size:12px;font-weight:700">${{p.label}}</span>
          </div>
          <div id="provider-body-${{key}}" style="display:none;padding-bottom:14px">
            ${{fields}}
            <button class="btn q-btn" style="font-size:10px;padding:5px 12px" onclick="saveProviderSettings('${{key}}')">Save</button>
            <span id="provider-status-${{key}}" style="font-size:10px;color:var(--muted);margin-left:8px"></span>
          </div>
        </div>`;
      }}).join('');
    }} catch(e) {{}}
  }}

  function toggleProviderCard(providerKey) {{
    const body    = document.getElementById(`provider-body-${{providerKey}}`);
    const chevron = document.getElementById(`provider-chevron-${{providerKey}}`);
    if (!body) return;
    const isOpen = body.style.display !== 'none';
    body.style.display = isOpen ? 'none' : 'block';
    if (chevron) chevron.style.transform = isOpen ? 'rotate(0deg)' : 'rotate(90deg)';
  }}

  async function saveProviderSettings(providerKey) {{
    const inputs = document.querySelectorAll(`.provider-field-input[data-provider="${{providerKey}}"]`);
    const status = document.getElementById(`provider-status-${{providerKey}}`);
    const fd = new FormData();
    inputs.forEach(inp => fd.append(inp.dataset.field, inp.value));
    try {{
      const res  = await fetch(`/channels/provider_settings/${{encodeURIComponent(providerKey)}}`, {{method: 'POST', body: fd}});
      const data = await res.json();
      if (data.ok) {{
        if (status) status.textContent = 'Saved ✓';
        showToast(`${{providerKey}} settings saved`);
        loadProviderSettings();
      }} else {{
        if (status) status.textContent = data.error || 'Failed';
        showToast(data.error || 'Save failed', false);
      }}
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  async function loadStreamSettings() {{
    try {{
      const res  = await fetch('/channels/stream_settings');
      const data = await res.json();
      if (data.ok) {{
        const evictionBox = document.getElementById('same-client-eviction-toggle');
        if (evictionBox) evictionBox.checked = data.same_client_eviction;
        const edidBox = document.getElementById('edid-refresh-toggle');
        if (edidBox) edidBox.checked = data.edid_refresh_on_tune;
        const strayBox = document.getElementById('stray-sleep-toggle');
        if (strayBox) strayBox.checked = data.stray_device_sleep;
        const intervalInput = document.getElementById('stray-sweep-interval');
        if (intervalInput) intervalInput.value = data.stray_device_sweep_interval_secs;
      }}
    }} catch(e) {{}}
  }}

  async function saveStreamSettings() {{
    const status = document.getElementById('stream-settings-status');
    const evictionBox = document.getElementById('same-client-eviction-toggle');
    const edidBox = document.getElementById('edid-refresh-toggle');
    const strayBox = document.getElementById('stray-sleep-toggle');
    const intervalInput = document.getElementById('stray-sweep-interval');
    const fd = new FormData();
    fd.append('same_client_eviction', evictionBox.checked ? 'true' : 'false');
    fd.append('edid_refresh_on_tune', edidBox.checked ? 'true' : 'false');
    fd.append('stray_device_sleep', strayBox.checked ? 'true' : 'false');
    fd.append('stray_device_sweep_interval_secs', intervalInput.value || '60');
    try {{
      const res  = await fetch('/channels/stream_settings', {{method: 'POST', body: fd}});
      const data = await res.json();
      if (data.ok) {{
        if (intervalInput) intervalInput.value = data.stray_device_sweep_interval_secs;
        if (status) status.textContent = 'Saved ✓';
        showToast('Streaming behavior settings saved');
      }} else {{
        if (status) status.textContent = 'Save failed';
        showToast('Save failed', false);
      }}
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  // ── Channels ──────────────────────────────────────────────────────────────────

  let _channelProviders = [];

  async function _loadChannelProviders() {{
    if (_channelProviders.length) return;
    try {{
      const res  = await fetch('/channels/providers');
      const data = await res.json();
      if (data.ok) {{
        _channelProviders = data.providers;
        const sel = document.getElementById('ce-provider');
        if (sel) {{
          sel.innerHTML = _channelProviders.map(p =>
            `<option value="${{p.key}}">${{p.label}}</option>`
          ).join('');
        }}
      }}
    }} catch(e) {{}}
  }}

  async function refreshChannelsList() {{
    const container = document.getElementById('channels-list-container');
    if (!container) return;
    try {{
      const res  = await fetch('/channels/list');
      const data = await res.json();
      if (!data.ok) return;
      if (data.channels.length === 0) {{
        container.innerHTML = `<div style="font-size:11px;color:var(--muted)">No channels configured yet.</div>`;
      }} else {{
        container.innerHTML = data.channels.map(ch => {{
          const isEnabled = ch.enabled !== false;
          const tunedBadge = ch.currently_tuned
            ? `<span style="color:#4dc8a0;font-size:9px;font-weight:700">\u25cf TUNED: ${{ch.tuned_input_id}}</span>`
            : `<span style="font-size:10px;color:var(--muted)">idle</span>`;
          const stopBtn = ch.currently_tuned
            ? `<button class="btn q-btn" style="font-size:9px;padding:3px 8px" onclick="releaseTuner('${{ch.tuned_input_id}}')">\u25a0 Stop</button>`
            : '';
          const toggleLabel = isEnabled ? 'Disable' : 'Enable';
          const toggleStyle = isEnabled
            ? 'font-size:9px;padding:3px 8px'
            : 'font-size:9px;padding:3px 8px;color:#4dc8a0;border-color:#4dc8a0';
          const rowStyle = isEnabled ? '' : 'opacity:.45';
          return `
          <div class="channel-row" style="${{rowStyle}}">
            <span style="font-family:'Courier New',monospace;font-size:11px;color:var(--muted);width:44px;flex-shrink:0">${{ch.guide_number||'\u2014'}}</span>
            <span style="flex:1;font-size:12px">${{ch.display_name}}</span>
            ${{isEnabled ? tunedBadge : '<span style="font-size:10px;color:var(--muted)">disabled</span>'}}
            <button class="btn q-btn" style="${{toggleStyle}}" onclick="toggleChannel('${{ch.channel_id}}')">${{toggleLabel}}</button>
            <button class="btn q-btn" style="font-size:9px;padding:3px 8px" onclick="testTuneChannel('${{ch.channel_id}}')" ${{isEnabled ? '' : 'disabled'}}>\u25b6 Test</button>
            ${{stopBtn}}
            <button class="btn q-btn" style="font-size:9px;padding:3px 8px" onclick='openChannelEditor(${{JSON.stringify(ch)}})'>Edit</button>
            <button class="btn q-btn" style="font-size:9px;padding:3px 8px" onclick="deleteChannel('${{ch.channel_id}}')">Delete</button>
          </div>`;
        }}).join('');
      }}
      const poolInfo = document.getElementById('tuner-pool-info');
      if (poolInfo) poolInfo.textContent = `Tuner pool: ${{data.tuner_pool_size - data.tuner_pool_busy}}/${{data.tuner_pool_size}} free`;
    }} catch(e) {{}}
  }}

  async function openChannelEditor(ch) {{
    await _loadChannelProviders();
    document.getElementById('channel-editor-title').textContent = ch ? 'Edit Channel' : 'Add Channel';
    document.getElementById('ce-orig-id').value       = ch ? ch.channel_id : '';
    document.getElementById('ce-channel-id').value    = ch ? ch.channel_id : '';
    document.getElementById('ce-channel-id').disabled = !!ch;
    document.getElementById('ce-display-name').value  = ch ? ch.display_name : '';
    document.getElementById('ce-guide-number').value  = ch ? ch.guide_number : '';
    document.getElementById('ce-guide-station').value = ch ? (ch.guide_station_id || '') : '';
    document.getElementById('ce-provider').value      = ch ? ch.provider : '';
    document.getElementById('ce-callsign').value      = ch ? ch.callsign : '';
    document.getElementById('ce-content-id').value    = ch ? ch.content_id : '';
    document.getElementById('ce-settle-time').value   = ch ? ch.settle_time_secs : 20;
    document.getElementById('ce-status').textContent  = '';
    document.getElementById('channel-editor-overlay').classList.add('show');
  }}

  function closeChannelEditor() {{
    document.getElementById('channel-editor-overlay').classList.remove('show');
  }}

  async function saveChannelEditor() {{
    const status = document.getElementById('ce-status');
    const channelId = document.getElementById('ce-channel-id').value.trim();
    if (!channelId) {{ status.textContent = 'Channel ID is required'; return; }}
    const fd = new FormData();
    fd.append('channel_id',        channelId);
    fd.append('display_name',      document.getElementById('ce-display-name').value.trim());
    fd.append('guide_number',      document.getElementById('ce-guide-number').value.trim());
    fd.append('guide_station_id',  document.getElementById('ce-guide-station').value.trim());
    fd.append('provider',          document.getElementById('ce-provider').value);
    fd.append('callsign',          document.getElementById('ce-callsign').value.trim());
    fd.append('content_id',        document.getElementById('ce-content-id').value.trim());
    fd.append('settle_time_secs',  document.getElementById('ce-settle-time').value || '20');
    try {{
      const res  = await fetch('/channels/save', {{method: 'POST', body: fd}});
      const data = await res.json();
      if (data.ok) {{
        closeChannelEditor();
        refreshChannelsList();
        showToast('Channel saved');
      }} else {{
        status.textContent = data.error || 'Save failed';
      }}
    }} catch(e) {{ status.textContent = 'Network error'; }}
  }}

  async function deleteChannel(channelId) {{
    if (!confirm('Delete this channel mapping?')) return;
    try {{
      await fetch(`/channels/delete/${{encodeURIComponent(channelId)}}`, {{method: 'POST'}});
      refreshChannelsList();
      showToast('Channel deleted');
    }} catch(e) {{ showToast('Delete failed', false); }}
  }}

  async function toggleChannel(channelId) {{
    try {{
      const res  = await fetch(`/channels/toggle/${{encodeURIComponent(channelId)}}`, {{method: 'POST'}});
      const data = await res.json();
      if (data.ok) {{
        showToast(data.enabled ? 'Channel enabled' : 'Channel disabled');
        refreshChannelsList();
      }} else {{
        showToast(data.error || 'Toggle failed', false);
      }}
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  async function testTuneChannel(channelId) {{
    showToast(`Tuning ${{channelId}}… this can take up to the configured settle time`);
    try {{
      const res  = await fetch(`/channels/test_tune/${{encodeURIComponent(channelId)}}`, {{method: 'POST'}});
      const data = await res.json();
      if (data.ok) {{
        const msg = data.already_tuned
          ? 'Already tuned to this channel'
          : (data.ready ? 'Tuned and ready (device confirmed playback)' : 'Tune fired but readiness gate timed out — app may not be playing');
        showToast(msg, data.ready !== false);
      }} else {{
        showToast(data.error || 'Test tune failed', false);
      }}
      refreshChannelsList();
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  async function releaseTuner(inputId) {{
    try {{
      const res  = await fetch(`/channels/release/${{encodeURIComponent(inputId)}}`, {{method: 'POST'}});
      const data = await res.json();
      showToast(data.ok ? `Released tuner ${{inputId}}` : (data.error || 'Release failed'), data.ok);
      refreshChannelsList();
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  // ── M3U import ────────────────────────────────────────────────────────────────

  async function openM3uImport() {{
    await _loadChannelProviders();
    const sel = document.getElementById('m3u-import-provider');
    if (sel && _channelProviders.length) {{
      sel.innerHTML = _channelProviders.map(p => `<option value="${{p.key}}">${{p.label}}</option>`).join('');
    }}
    document.getElementById('m3u-import-status').textContent = '';
    document.getElementById('m3u-import-overlay').classList.add('show');
  }}

  function closeM3uImport() {{
    document.getElementById('m3u-import-overlay').classList.remove('show');
  }}

  async function submitM3uImport() {{
    const status = document.getElementById('m3u-import-status');
    const text = document.getElementById('m3u-import-text').value;
    if (!text.trim()) {{ status.textContent = 'Paste an M3U playlist first'; return; }}
    const fd = new FormData();
    fd.append('m3u_text', text);
    fd.append('provider', document.getElementById('m3u-import-provider').value);
    status.textContent = 'Importing…';
    try {{
      const res  = await fetch('/channels/import_m3u', {{method: 'POST', body: fd}});
      const data = await res.json();
      if (data.ok) {{
        status.textContent = `Imported ${{data.imported}} channel(s) ✓`;
        showToast(`Imported ${{data.imported}} channels`);
        refreshChannelsList();
        setTimeout(closeM3uImport, 1200);
      }} else {{
        status.textContent = data.error || 'Import failed';
      }}
    }} catch(e) {{ status.textContent = 'Network error'; }}
  }}

  function copyExportUrl() {{
    const url = `${{window.location.origin}}/channels/export.m3u`;
    const status = document.getElementById('export-url-status');
    navigator.clipboard.writeText(url).then(() => {{
      if (status) status.textContent = `Copied: ${{url}}`;
      showToast('Export URL copied to clipboard');
    }}).catch(() => {{
      if (status) status.textContent = url;
      showToast('Could not auto-copy — URL shown below', false);
    }});
  }}

  // ── Raw M3U view / edit ──────────────────────────────────────────────────────

  async function openRawM3uEditor() {{
    const status = document.getElementById('raw-m3u-status');
    const textarea = document.getElementById('raw-m3u-text');
    textarea.value = 'Loading current M3U...';
    status.textContent = '';
    document.getElementById('raw-m3u-overlay').classList.add('show');
    try {{
      const res  = await fetch('/channels/export.m3u');
      const text = await res.text();
      textarea.value = text;
    }} catch(e) {{
      textarea.value = '';
      status.textContent = 'Could not load current M3U';
    }}
  }}

  function closeRawM3uEditor() {{
    document.getElementById('raw-m3u-overlay').classList.remove('show');
  }}

  async function submitRawM3uUpdate(replace) {{
    const status = document.getElementById('raw-m3u-status');
    const text = document.getElementById('raw-m3u-text').value;
    if (!text.trim()) {{ status.textContent = 'Nothing to import — the text box is empty'; return; }}
    if (replace) {{
      const sure = confirm('This will delete every channel not present in this text and replace the whole list. Continue?');
      if (!sure) return;
    }}
    const fd = new FormData();
    fd.append('m3u_text', text);
    fd.append('provider', 'directv_now');
    fd.append('replace', replace ? 'true' : 'false');
    status.textContent = replace ? 'Replacing...' : 'Updating...';
    try {{
      const res  = await fetch('/channels/import_m3u', {{method: 'POST', body: fd}});
      const data = await res.json();
      if (data.ok) {{
        status.textContent = `${{data.replaced ? 'Replaced with' : 'Updated'}} ${{data.imported}} channel(s)`;
        showToast(`${{data.imported}} channel(s) ${{data.replaced ? 'loaded' : 'updated'}}`);
        refreshChannelsList();
        setTimeout(closeRawM3uEditor, 1200);
      }} else {{
        status.textContent = data.error || 'Import failed';
      }}
    }} catch(e) {{ status.textContent = 'Network error'; }}
  }}

  async function clearAllChannels() {{
    const sure = confirm('Delete every configured channel? This cannot be undone.');
    if (!sure) return;
    try {{
      const res  = await fetch('/channels/clear_all', {{method: 'POST'}});
      const data = await res.json();
      showToast(data.ok ? `Cleared ${{data.cleared}} channel(s)` : 'Clear failed', data.ok);
      refreshChannelsList();
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  refreshChannelsList();
  loadProviderSettings();
  loadStreamSettings();
</script>
</body>
</html>"""



def render_dashboard(
    live_inputs:            dict,
    cfg:                    dict,
    current_input_ids:      list,
    recordings:             list,
    scheduled:              list,
    hls_active:             dict,
    base_url:               str,
    should_be_live:         dict,
    format_ext:             dict,
    labels:                 dict = None,
    available_encoders:     list = None,
    available_audio_codecs: list = None,
    channel_layouts:        list = None,
    encoder_presets:        dict = None,
) -> str:
    # Fall back to key-derived label if caller didn't supply the dict
    def lbl(key):
        if labels and key in labels:
            return labels[key]
        b, i = key.split("-", 1)
        return f"Board {b} · Input {i}"

    # Encoder options for dropdowns
    _encoders = available_encoders or [{"value": "h264_qsv", "label": "Intel QSV (iGPU)"}]
    def _encoder_options(selected):
        return "\n".join(
            f'<option value="{e["value"]}"{"  selected" if e["value"] == selected else ""}>{e["label"]}</option>'
            for e in _encoders
        )

    # Audio codec options
    _audio_codecs = available_audio_codecs or [{"value": "aac", "label": "AAC", "max_ch": 2}]
    def _audio_codec_options(selected):
        return "\n".join(
            f'<option value="{c["value"]}"{"  selected" if c["value"] == selected else ""}>{c["label"]}</option>'
            for c in _audio_codecs
        )

    # Channel layout options
    _ch_layouts = channel_layouts or [
        {"value": "stereo", "label": "Stereo (2ch)"},
        {"value": "5.1",    "label": "5.1 Surround"},
        {"value": "7.1",    "label": "7.1 Surround"},
        {"value": "8ch",    "label": "8 ch (raw)"},
        {"value": "16ch",   "label": "16 ch (raw)"},
    ]
    def _ch_layout_options(selected):
        return "\n".join(
            f'<option value="{cl["value"]}"{"  selected" if cl["value"] == selected else ""}>{cl["label"]}</option>'
            for cl in _ch_layouts
        )

    # ── Per-input card HTML (server-side initial render) ──────────────────────
    def make_card(i):
        is_live     = i in live_inputs
        is_hls      = i in hls_active
        is_faulted  = should_be_live.get(i, {}).get("faulted", False)
        restart_cnt = should_be_live.get(i, {}).get("restart_count", 0)
        q_val       = cfg.get(i, {}).get("q", 25)
        signal      = cfg.get(i, {}).get("signal", "UNKNOWN")
        desc        = cfg.get(i, {}).get("desc", "")
        driver      = cfg.get(i, {}).get("driver", "magewell")
        encoder     = cfg.get(i, {}).get("encoder", _encoders[0]["value"] if _encoders else "h264_qsv")
        card_label  = lbl(i)

        # Card border class
        if is_hls:
            card_cls = "card hls-on"
        elif is_live:
            card_cls = "card live"
        else:
            card_cls = "card"

        # Thumbnail badges
        live_badge = (
            '<div class="thumb-badge live-badge"><span class="blink-dot"></span> Live</div>'
            if is_live else ""
        )
        hls_badge = (
            '<div class="thumb-badge hls-badge-th"><span class="blink-dot hls-dot-col"></span> HLS</div>'
            if is_hls else ""
        )
        no_sig = "" if is_live else '<div class="no-sig">No Signal</div>'

        # Status subtitle
        if is_faulted:
            sub_text = f"⚠ Faulted · {restart_cnt} restarts"
            sub_color = "color:#fb923c"
        elif is_hls:
            sub_text = f"{encoder} · {desc or 'signal ok'} · HLS"
            sub_color = ""
        elif is_live:
            sub_text = f"{encoder} · {desc or 'signal ok'} · ready"
            sub_color = ""
        else:
            sub_text = "Offline — waiting for signal"
            sub_color = ""

        # Viewers chip
        import time as _time
        now = _time.time()
        viewer_count = live_inputs[i].get("viewer_count", 0) if is_live else 0
        viewer_lbl = f'{viewer_count} Viewer{"s" if viewer_count != 1 else ""}'
        safe_id = i.replace("-", "_")
        if is_live:
            viewer_rows = ""
            for vw in live_inputs[i].get("viewers", []):
                elapsed = int(now - vw["connected_at"])
                viewer_rows += f'<div class="vd-row"><span class="vd-ip">{vw["ip"]}</span><span class="vd-dur">{_fmt_elapsed(elapsed)}</span></div>'
            if not viewer_rows:
                viewer_rows = '<div class="vd-empty">No direct stream clients</div>'
            viewers_html = f"""<span class="viewers-chip" id="vchip-{safe_id}" onclick="toggleViewerDrawer('{safe_id}')" title="Connected IPs">
                {viewer_lbl} <span class="vchip-caret" id="vcaret-{safe_id}">&#9660;</span>
              </span>
              <div class="viewer-drawer" id="vdrawer-{safe_id}">
                <div class="vd-inner">
                  <div class="vd-header"><span>IP</span><span>Duration</span></div>
                  {viewer_rows}
                </div>
              </div>"""
        else:
            viewers_html = ""

        # System stats
        if is_live:
            s = live_inputs[i]
            stats_html = f'<span class="sys-stat" id="resources-{i}">{s["stats"]["cpu"]:.1f}% CPU · {s["stats"]["mem"]:.1f}MB</span>'
        else:
            stats_html = f'<span class="sys-stat" id="resources-{i}" style="display:none"></span>'

        # HW signal pill
        if signal == "LOCKED":
            sig_html = f'<span class="sig-pill sig-locked">⬤ {desc or "Signal OK"}</span>'
        elif signal not in ("UNKNOWN", ""):
            sig_html = f'<span class="sig-pill sig-other">◯ {signal}</span>'
        else:
            sig_html = ""

        # Encoder selector row
        encoder_select_html = f"""<div class="enc-row">
              <span class="q-label">ENC</span>
              <select class="enc-select" id="enc-select-{i}" onchange="submitEncoder('{i}')">
                {_encoder_options(encoder)}
              </select>
            </div>"""


        if driver == "magewell":
            mw_cfg           = cfg.get(i, {})
            saved_preset     = mw_cfg.get("preset",          "")
            saved_la         = mw_cfg.get("lookahead",        35)
            saved_gop        = mw_cfg.get("gop_secs",         1.5)
            saved_copy_th    = mw_cfg.get("copy_threads",     None)
            saved_vbuf       = mw_cfg.get("video_buffers",    16)
            saved_ehf        = mw_cfg.get("extra_hw_frames",  32)
            saved_p010       = mw_cfg.get("p010",             False)
            saved_noa        = mw_cfg.get("no_audio",         False)
            saved_dev        = mw_cfg.get("vaapi_device",     "")
            saved_edid          = mw_cfg.get("edid_path",    "")
            saved_edid_refresh  = mw_cfg.get("edid_refresh", False)
            mw_panel = f"""<div class="dl-panel" id="mwpanel-{i}">
              <div class="dl-panel-hdr" onclick="toggleMwPanel('{i}')">
                &#9881; Magewell Config <span class="vchip-caret" id="mwcaret-{i}">&#9660;</span>
              </div>
              <div class="dl-panel-body" id="mwbody-{i}" style="display:none">

                <div class="dl-section-lbl">Encoder</div>
                <div class="dl-grid">
                  <label class="dl-lbl">Preset</label>
                  <select class="dl-input" id="mw-preset-{i}">
                    <option value="">— default —</option>
                  </select>
                  <label class="dl-lbl">Lookahead</label>
                  <div class="mw-la-row">
                    <input class="dl-input mw-la-input" id="mw-la-{i}" type="range"
                      min="0" max="60" value="{saved_la}"
                      oninput="document.getElementById('mw-la-val-{i}').textContent=this.value">
                    <span class="mw-la-val" id="mw-la-val-{i}">{saved_la}</span>
                  </div>
                  <label class="dl-lbl" title="GOP size in seconds. 0 to disable.">GOP secs</label>
                  <input class="dl-input" id="mw-gop-{i}" type="number"
                    min="0" max="10" step="0.5" value="{saved_gop}" placeholder="1.5">
                  <label class="dl-lbl">Device</label>
                  <input class="dl-input" id="mw-dev-{i}" type="text"
                    value="{saved_dev}" placeholder="renderD128">
                </div>

                <div class="dl-section-lbl" style="margin-top:10px">Buffer Tuning</div>
                <div class="dl-grid">
                  <label class="dl-lbl" title="--copy-threads: number of GPU copy worker threads (magewell2ts v5-rc+ only). Leave blank to use the binary's own default (2).">Copy Threads</label>
                  <input class="dl-input" id="mw-copyth-{i}" type="number"
                    min="1" max="16" value="{saved_copy_th if saved_copy_th else ''}" placeholder="default (2)">
                  <label class="dl-lbl" title="--video-buffers: RAM queue depth">RAM Bufs</label>
                  <input class="dl-input" id="mw-vbuf-{i}" type="number"
                    min="1" max="256" value="{saved_vbuf}" placeholder="16">
                  <label class="dl-lbl" title="--extra-hw-frames: extra HW frames (min 32)">HW Extra</label>
                  <input class="dl-input" id="mw-ehf-{i}" type="number"
                    min="32" max="256" value="{saved_ehf}" placeholder="32">
                </div>
                <div class="dl-lfe-hint" style="margin-top:5px;font-size:10px">
                  Copy Threads replaces the old GPU Bufs setting — magewell2ts v5-rc+ removed
                  --gpu-buffers in favor of a per-thread GPU copy pool. Leave blank on older
                  binaries or to use the built-in default. Each thread holds a small fixed
                  buffer pool internally, so 2-4 threads is normally plenty.
                </div>

                <div class="dl-section-lbl" style="margin-top:10px">Options</div>
                <label class="dl-lfe-row">
                  <input type="checkbox" id="mw-p010-{i}" {"checked" if saved_p010 else ""}>
                  <span>p010 — 10-bit encoding <span class="dl-lfe-hint">(better quality, HDR sources)</span></span>
                </label>
                <label class="dl-lfe-row">
                  <input type="checkbox" id="mw-noa-{i}" {"checked" if saved_noa else ""}>
                  <span>Video only — no audio</span>
                </label>
                <button class="btn q-btn" style="margin-top:10px;width:100%"
                  onclick="submitMwCfg('{i}')">Apply &amp; Restart</button>

                <div class="dl-section-lbl" style="margin-top:14px">EDID</div>
                <div class="mw-edid-status" id="mw-edid-status-{i}"></div>
                <div class="dl-fmt-row" style="margin-top:5px">
                  <select class="dl-input dl-fmt-select" id="mw-edid-sel-{i}"
                    onchange="mwEdidSelChange('{i}')">
                    <option value="">— select a .bin file —</option>
                  </select>
                  <button class="dl-refresh-btn" id="mw-edid-refresh-{i}"
                    onclick="mwLoadEdidList('{i}')" title="Scan for EDID files">&#8635;</button>
                </div>
                <input class="dl-input" id="mw-edid-path-{i}" type="text"
                  value="{saved_edid}" placeholder="or paste full path to .bin"
                  style="margin-top:5px">
                <div class="dl-lfe-hint" style="margin-top:4px;font-size:10px;color:var(--muted)">
                  Saved EDID is written automatically each time the input starts.
                  EDID does not survive a reboot without this.
                </div>
                <label class="dl-lfe-row" style="margin-top:6px">
                  <input type="checkbox" id="mw-edid-refresh-chk-{i}"
                    {"checked" if saved_edid_refresh else ""}>
                  <span>Periodic EDID refresh
                    <span class="dl-lfe-hint">(every 25s when idle — enable for Eco cards)</span>
                  </span>
                </label>
                <div style="display:flex;gap:6px;margin-top:8px">
                  <button class="btn q-btn" style="flex:1" onclick="mwReadEdid('{i}')">
                    &#128065; Read Current
                  </button>
                  <button class="btn q-btn" style="flex:1;background:var(--accent-dim);border-color:var(--accent-bdr)"
                    onclick="mwWriteEdid('{i}')">
                    &#128229; Write EDID
                  </button>
                </div>
                <div class="mw-edid-console" id="mw-edid-console-{i}" style="display:none"></div>
              </div>
            </div>"""

        # Q control is always visible (Decklink CBR mode removed)
        q_row_style = ""

        # Action buttons
        hls_btn = (
            f"<form action='/hls/stop/{i}' method='post' style='display:inline'>"
            f"<button type='submit' class='btn btn-hls-stop'>Stop HLS</button></form>"
            if is_hls else
            f"<button class='btn btn-hls' onclick=\"startHLS('{i}')\">Start HLS</button>"
        )

        card_opacity = "" if (is_live or is_hls) else " style='opacity:.5'"

        return f"""
  <div class="{card_cls}" id="card-{i}"{card_opacity}>
    <div class="card-body">
      <div class="card-thumb" id="thumb-{i}">
        <div class="thumb-num">{i}</div>
        {no_sig}
        {live_badge}
        {hls_badge}
      </div>
      <div class="card-info">
        <div class="card-title">{card_label} {sig_html}</div>
        <div class="card-sub" id="sub-{i}" style="{sub_color}">{sub_text}</div>
        <div class="card-meta">
          <div id="viewers-{i}">{viewers_html}</div>
          {stats_html}
          <div class="q-row" id="qrow-{i}"{q_row_style}>
            <span class="q-label">Q</span>
            <input class="q-input" id="q-input-{i}" type="number" min="1" max="51" value="{q_val}"
              onkeydown="if(event.key==='Enter') submitQ('{i}')">
            <button class="q-btn" onclick="submitQ('{i}')">Set</button>
          </div>
          {encoder_select_html}
        </div>
        {mw_panel}
      </div>
      <div class="btn-row" id="ctrl-{i}">
        <button class="btn btn-preview" onclick="openPreview('{i}')">Preview</button>
        <a href="/play/{i}" class="btn btn-vlc">VLC</a>
        {hls_btn}
        <button class="btn btn-record-open" onclick="openRecord('{i}')">&#9210; Rec</button>
      </div>
    </div>
  </div>"""

    cards_html = "".join(make_card(i) for i in current_input_ids)

    # ── HLS nodes section ─────────────────────────────────────────────────────
    hls_section = ""
    if hls_active:
        hls_rows = ""
        for k in hls_active:
            hls_rows += f"""
      <div class="hls-url-row">
        <div>
          <div class="hls-node-title">{lbl(k)}</div>
          <div class="hls-url">{base_url}/hls/{k}/index.m3u8</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-shrink:0">
          <a href="/mobile" target="_blank" class="btn btn-mobile-link">Mobile ↗</a>
          <form action="/hls/stop/{k}" method="post" style="display:inline">
            <button type="submit" class="btn btn-hls-stop">Stop HLS</button>
          </form>
        </div>
      </div>"""
        hls_section = f"""
    <div class="section-header">
      <div class="section-lbl">Active HLS Nodes</div>
    </div>
    <div class="hls-list">{hls_rows}</div>"""

    # ── Recordings section ────────────────────────────────────────────────────
    rec_section = ""
    if recordings:
        rec_rows = ""
        for r in recordings:
            dur_txt = f" / {_fmt_elapsed(r['duration'])}" if r['duration'] else ""
            rec_rows += f"""
      <div class="rec-row">
        <div>
          <div class="rec-title">{r['label']}</div>
          <div class="rec-path">{r['path']}</div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;flex-shrink:0">
          <span class="rec-timer" id="rec-elapsed-{r['id']}">● REC {_fmt_elapsed(r['elapsed'])}{dur_txt}</span>
          <span class="rec-fmt">{r['fmt']}</span>
          <form action="/record/stop/{r['id']}" method="post" style="display:inline">
            <button type="submit" class="btn btn-stop-rec">Stop</button>
          </form>
        </div>
      </div>"""
        rec_section = f"""
    <div class="section-header">
      <div class="section-lbl">Active Recordings</div>
    </div>
    <div class="rec-list">{rec_rows}</div>"""

    # ── Scheduled jobs section ────────────────────────────────────────────────
    sched_section = ""
    if scheduled:
        sched_rows = ""
        for j in scheduled:
            sched_rows += f"""
      <div class="rec-row">
        <div>
          <div class="rec-title">{j['label']}</div>
          <div class="rec-path">{j['path']}</div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;flex-shrink:0">
          <span class="sched-time">{j['start']}</span>
          <span class="rec-fmt">{_fmt_elapsed(j['duration'])} · {j['fmt']}</span>
          <form action="/schedule/cancel/{j['id']}" method="post" style="display:inline">
            <button type="submit" class="btn btn-hls-stop">Cancel</button>
          </form>
        </div>
      </div>"""
        sched_section = f"""
    <div class="section-header">
      <div class="section-lbl">Scheduled Jobs</div>
    </div>
    <div class="rec-list">{sched_rows}</div>"""

    # ── Format / input option dropdowns ──────────────────────────────────────
    format_options = "\n".join(
        f'<option value="{k}">{v[1].upper()} ({k})</option>' for k, v in format_ext.items()
    )
    input_options = "\n".join(
        f'<option value="{i}">{lbl(i)}</option>' for i in current_input_ids
    )

    # ── JS meta dict ─────────────────────────────────────────────────────────
    meta_js_dict = {
        i: {
            "label":        lbl(i),
            "signal":       cfg.get(i, {}).get("signal", "UNKNOWN"),
            "desc":         cfg.get(i, {}).get("desc", ""),
            "adb_ip":       cfg.get(i, {}).get("adb_ip", ""),
            "remote_type":  cfg.get(i, {}).get("remote_type", "adb" if cfg.get(i, {}).get("adb_ip") else "none"),
            "remote_ip":    cfg.get(i, {}).get("remote_ip",   cfg.get(i, {}).get("adb_ip", "")),
            "adb_home":     cfg.get(i, {}).get("adb_home",    False),
            "driver":       cfg.get(i, {}).get("driver", "magewell"),
            "encoder":      cfg.get(i, {}).get("encoder", _encoders[0]["value"] if _encoders else "h264_qsv"),
            "quality_mode": cfg.get(i, {}).get("quality_mode", "cqp"),
        }
        for i in current_input_ids
    }
    meta_js = json.dumps(meta_js_dict)
    encoders_js = json.dumps(_encoders)

    return f"""<!DOCTYPE html>
<html data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Broadcast Hub</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;700&family=Inter:wght@400;500;700;900&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.min.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  /* ── Theme: Neon Ops (default dark) ── */
  :root, [data-theme="dark"] {{
    --bg:           #090d1a;
    --bg-topbar:    rgba(9,13,26,.97);
    --surface:      #0b0f22;
    --border:       #1c2540;
    --border-hi:    #2a3560;
    --text:         #c0cce8;
    --muted:        #3a4870;
    --dim:          #1c2540;
    --accent:       #00e5ff;
    --accent-dim:   rgba(0,229,255,.18);
    --accent-bdr:   rgba(0,229,255,.4);
    --live:         #ff0066;
    --live-bg:      rgba(255,0,102,.18);
    --live-bdr:     rgba(255,0,102,.45);
    --blue:         #0070ff;
    --orange:       #ff8800;
    --purple:       #aa00ff;
    --green:        #00e5a0;
    --btn-preview-bg: rgba(0,229,160,.15);
    --btn-vlc-bg:     rgba(255,136,0,.15);
    --btn-rec-bg:     rgba(170,0,255,.15);
  }}

  /* ── Theme: Broadcast Orange (warm dark) ── */
  [data-theme="mono"] {{
    --bg:           #100e06;
    --bg-topbar:    rgba(12,10,4,.98);
    --surface:      #0c0a04;
    --border:       #2a1e08;
    --border-hi:    #3a2810;
    --text:         #e8d0a0;
    --muted:        #5a3818;
    --dim:          #2a1e08;
    --accent:       #ff6600;
    --accent-dim:   rgba(255,102,0,.18);
    --accent-bdr:   rgba(255,102,0,.45);
    --live:         #ff2200;
    --live-bg:      rgba(255,34,0,.18);
    --live-bdr:     rgba(255,34,0,.45);
    --blue:         #ffaa00;
    --orange:       #ff6600;
    --purple:       #ff3388;
    --green:        #88cc00;
    --btn-preview-bg: rgba(136,204,0,.15);
    --btn-vlc-bg:     rgba(255,102,0,.15);
    --btn-rec-bg:     rgba(255,51,136,.15);
  }}

  /* ── Theme: Studio Pro (light) ── */
  [data-theme="light"] {{
    --bg:           #f3f5fa;
    --bg-topbar:    rgba(26,31,56,.98);
    --surface:      #ffffff;
    --border:       #c8cedd;
    --border-hi:    #b0b8d0;
    --text:         #1a1f38;
    --muted:        #6878a8;
    --dim:          #e4e8f4;
    --accent:       #4d9fff;
    --accent-dim:   rgba(77,159,255,.15);
    --accent-bdr:   rgba(77,159,255,.45);
    --live:         #dc2626;
    --live-bg:      rgba(220,38,38,.12);
    --live-bdr:     rgba(220,38,38,.4);
    --blue:         #4d9fff;
    --orange:       #d97706;
    --purple:       #7c3aed;
    --green:        #059669;
    --btn-preview-bg: rgba(5,150,105,.12);
    --btn-vlc-bg:     rgba(217,119,6,.12);
    --btn-rec-bg:     rgba(124,58,237,.12);
  }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
    padding-bottom: 60px;
  }}

  /* ── Topbar ── */
  .topbar {{
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    position: sticky; top: 0;
    background: var(--bg-topbar);
    backdrop-filter: blur(14px);
    display: flex; align-items: center; justify-content: space-between;
    z-index: 50;
  }}
  .logo {{ font-weight: 900; font-style: italic; font-size: 21px; text-transform: uppercase; }}
  .logo span {{ color: var(--accent); }}
  .topbar-right {{ display: flex; align-items: center; gap: 16px; }}
  .mobile-link {{
    color: var(--muted); font-size: 11px; text-decoration: none;
    text-transform: uppercase; letter-spacing: .1em; transition: color .15s;
  }}
  .mobile-link:hover {{ color: var(--text); }}

  /* ── Driver alert banner ── */
  .driver-banner {{
    display: none;
    background: rgba(255,140,0,.08);
    border-bottom: 1px solid rgba(255,140,0,.3);
    padding: 10px 20px;
    align-items: center; gap: 12px;
    font-size: 12px;
  }}
  .driver-banner.show {{ display: flex; }}
  .driver-banner-icon {{ font-size: 18px; flex-shrink: 0; }}
  .driver-banner-text {{ flex: 1; color: #ff8c00; font-weight: 700; line-height: 1.4; }}
  .driver-banner-text span {{ font-weight: 400; color: var(--muted); }}
  .btn-driver-fix {{
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 10px;
    text-transform: uppercase; letter-spacing: .1em;
    padding: 7px 16px; border-radius: 4px; border: none; cursor: pointer;
    background: rgba(255,140,0,.15); color: #ff8c00;
    border: 1px solid rgba(255,140,0,.35); transition: opacity .15s; flex-shrink: 0;
  }}
  .btn-driver-fix:hover {{ opacity: .8; }}
  .sse-dot {{
    display: flex; align-items: center; gap: 5px;
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .12em; color: var(--dim); transition: opacity .4s;
  }}
  .sse-dot .dot {{
    width: 6px; height: 6px; border-radius: 50%; background: var(--accent);
    animation: blink 1.4s ease-in-out infinite;
  }}

  /* ── Page layout ── */
  .page {{ max-width: 1100px; margin: 0 auto; padding: 0 14px; }}

  .section-header {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 20px 0 10px;
  }}
  .section-lbl {{
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .18em; color: #3a3a3a;
  }}

  /* ── Cards ── */
  .cards {{ display: flex; flex-direction: column; gap: 10px; }}

  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px; overflow: hidden;
    transition: border-color .2s;
  }}
  .card.live    {{ border-color: var(--live-bdr); }}
  .card.hls-on  {{ border-color: var(--accent-bdr); }}

  .card-body {{
    padding: 14px 16px;
    display: flex; align-items: center; gap: 14px;
  }}

  /* Signal thumbnail */
  .card-thumb {{
    width: 116px; height: 66px; flex-shrink: 0;
    background: #060606; border: 1px solid var(--border-hi);
    border-radius: 3px;
    display: flex; align-items: center; justify-content: center;
    position: relative; overflow: hidden;
  }}
  .thumb-num {{
    font-weight: 900; font-size: 20px; color: #1c1c1c; line-height: 1;
    font-style: italic;
  }}
  .no-sig {{
    position: absolute; bottom: 5px; left: 0; right: 0; text-align: center;
    font-size: 8px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .15em; color: #2a2a2a;
  }}
  .thumb-badge {{
    position: absolute; top: 4px;
    font-size: 8px; font-weight: 900; text-transform: uppercase;
    letter-spacing: .08em; padding: 2px 6px; border-radius: 3px;
    display: flex; align-items: center; gap: 3px;
  }}
  .live-badge  {{ left: 4px; background: var(--live-bg); border: 1px solid var(--live-bdr); color: var(--live); }}
  .hls-badge-th {{ right: 4px; background: var(--accent-dim); border: 1px solid var(--accent-bdr); color: var(--accent); }}
  .blink-dot   {{ width: 5px; height: 5px; border-radius: 50%; background: currentColor; animation: blink 1.4s ease-in-out infinite; }}
  .hls-dot-col {{ background: var(--accent); }}

  /* Card info */
  .card-info {{ flex: 1; min-width: 0; }}
  .card-title {{
    font-weight: 900; font-size: 14px; text-transform: uppercase;
    letter-spacing: .03em; color: #e8e8e8;
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  }}
  .card-sub   {{ font-size: 11px; color: var(--muted); margin-top: 3px; font-weight: 500; }}
  .card-meta  {{ margin-top: 9px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}

  /* Signal pills */
  .sig-pill {{
    font-size: 9px; font-weight: 900; text-transform: uppercase;
    letter-spacing: .08em; padding: 2px 7px; border-radius: 3px;
  }}
  .sig-locked {{ background: rgba(100,220,80,.08); border: 1px solid rgba(100,220,80,.2); color: var(--green); }}
  .sig-other  {{ background: rgba(255,255,255,.04); border: 1px solid #2a2a2a; color: #555; }}

  /* Viewers chip + drawer */
  .viewers-chip {{
    display: inline-flex; align-items: center; gap: 5px;
    font-weight: 800; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
    color: var(--blue); background: rgba(74,158,255,.08);
    border: 1px solid rgba(74,158,255,.2);
    padding: 3px 9px; border-radius: 4px; cursor: pointer;
    transition: background .15s; white-space: nowrap; user-select: none;
  }}
  .viewers-chip:hover {{ background: rgba(74,158,255,.15); }}
  .vchip-caret {{ font-size: 9px; opacity: .5; transition: transform .2s; display: inline-block; }}
  .vchip-caret.open {{ transform: rotate(180deg); }}
  .viewer-drawer {{
    max-height: 0; overflow: hidden; opacity: 0;
    transition: max-height .25s cubic-bezier(.4,0,.2,1), opacity .2s;
  }}
  .viewer-drawer.open {{ max-height: 300px; opacity: 1; }}
  .vd-inner {{ margin-top: 6px; background: #0a0a0a; border: 1px solid #2a2a2a; border-radius: 4px; overflow: hidden; min-width: 220px; }}
  .vd-header {{ display: grid; grid-template-columns: 1fr auto; padding: 5px 10px; background: #111; border-bottom: 1px solid #1e1e1e; font-size: 9px; font-weight: 800; letter-spacing: .18em; text-transform: uppercase; color: #3a3a3a; }}
  .vd-row    {{ display: grid; grid-template-columns: 1fr auto; padding: 6px 10px; border-bottom: 1px solid #151515; align-items: center; gap: 14px; }}
  .vd-row:last-child {{ border-bottom: none; }}
  .vd-ip  {{ font-family: 'Courier New', monospace; font-size: 11px; font-weight: 700; color: #b0c4d8; }}
  .vd-dur {{ font-family: 'Courier New', monospace; font-size: 10px; color: #3a3a3a; text-align: right; }}
  .vd-empty {{ padding: 7px 10px; font-size: 11px; color: #333; font-style: italic; }}

  /* System stats */
  .sys-stat {{ font-family: 'Courier New', monospace; font-size: 10px; color: #3a3a3a; }}

  /* Q control */
  .q-row   {{ display: flex; align-items: center; gap: 5px; }}
  .q-label {{ font-size: 9px; font-weight: 900; text-transform: uppercase; letter-spacing: .12em; color: #3a3a3a; }}
  .q-input {{
    background: #111; border: 1px solid #2a2a2a; color: var(--accent);
    font-weight: 900; font-size: 13px; text-align: center;
    width: 44px; padding: 4px; border-radius: 4px; outline: none;
    font-family: 'Inter', sans-serif; -moz-appearance: textfield;
  }}
  .q-input::-webkit-inner-spin-button,
  .q-input::-webkit-outer-spin-button {{ -webkit-appearance: none; }}
  .q-input:focus {{ border-color: var(--accent); box-shadow: 0 0 0 2px rgba(232,255,71,.1); }}
  .q-btn {{
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 9px;
    text-transform: uppercase; letter-spacing: .1em;
    background: var(--accent-dim); border: 1px solid var(--accent-bdr);
    color: var(--accent); padding: 4px 9px; border-radius: 4px;
    cursor: pointer; transition: background .15s; white-space: nowrap;
  }}
  .q-btn:hover {{ background: rgba(232,255,71,.18); }}

  /* Action buttons */
  .btn-row {{ display: flex; gap: 7px; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; }}
  .btn {{
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 11px;
    text-transform: uppercase; letter-spacing: .07em;
    padding: 8px 14px; border-radius: 4px; border: none;
    cursor: pointer; transition: opacity .15s, background .15s;
    white-space: nowrap; text-decoration: none;
    display: inline-flex; align-items: center; justify-content: center;
  }}
  .btn:active {{ opacity: .7; }}
  .btn-preview     {{ background: var(--btn-preview-bg, rgba(100,220,80,.15)); border: 1px solid var(--green) !important; color: var(--green); }}
  .btn-preview:hover {{ opacity: .82; }}
  .btn-vlc         {{ background: var(--btn-vlc-bg, rgba(255,140,0,.15)); border: 1px solid var(--orange) !important; color: var(--orange); }}
  .btn-vlc:hover   {{ opacity: .82; }}
  .btn-hls         {{ background: var(--accent-dim); border: 1px solid var(--accent-bdr) !important; color: var(--accent); }}
  .btn-hls:hover   {{ opacity: .82; }}
  .btn-hls-stop    {{ background: var(--dim); border: 1px solid var(--border-hi) !important; color: var(--muted); }}
  .btn-hls-stop:hover {{ opacity: .82; }}
  .btn-record-open {{ background: var(--btn-rec-bg, rgba(180,100,255,.15)); border: 1px solid var(--purple) !important; color: var(--purple); }}
  .btn-record-open:hover {{ opacity: .82; }}
  .btn-mobile-link {{ background: var(--accent-dim); border: 1px solid var(--accent-bdr) !important; color: var(--muted); font-size: 10px; padding: 6px 10px; }}
  .btn-stop-rec    {{ background: var(--live-bg); border: 1px solid var(--live-bdr) !important; color: var(--live); }}
  .btn-stop-rec:hover {{ opacity: .82; }}
  .btn-schedule-open {{ background: var(--accent-dim); border: 1px solid var(--accent-bdr) !important; color: var(--accent); }}
  .btn-schedule-open:hover {{ opacity: .82; }}
  .btn-manage {{
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 10px;
    text-transform: uppercase; letter-spacing: .1em;
    background: var(--accent-dim); border: 1px solid var(--accent-bdr);
    color: var(--accent); padding: 7px 13px; border-radius: 4px;
    cursor: pointer; transition: opacity .15s;
  }}
  .btn-manage:hover {{ opacity: .82; }}

  /* ── HLS nodes list ── */
  .hls-list {{ display: flex; flex-direction: column; gap: 8px; }}
  .hls-url-row {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 4px; padding: 12px 16px;
    display: flex; align-items: center; justify-content: space-between; gap: 14px;
  }}
  .hls-node-title {{ font-weight: 900; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #e8e8e8; margin-bottom: 3px; }}
  .hls-url {{ font-family: 'Courier New', monospace; font-size: 11px; color: var(--accent); word-break: break-all; }}

  /* ── Recordings / scheduled rows ── */
  .rec-list {{ display: flex; flex-direction: column; gap: 8px; }}
  .rec-row  {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 4px; padding: 11px 16px;
    display: flex; align-items: center; justify-content: space-between; gap: 14px;
  }}
  .rec-title {{ font-weight: 900; font-size: 13px; color: #e8e8e8; text-transform: uppercase; letter-spacing: .03em; }}
  .rec-path  {{ font-family: 'Courier New', monospace; font-size: 10px; color: var(--muted); margin-top: 2px; word-break: break-all; }}
  .rec-timer {{ font-family: 'Courier New', monospace; font-size: 11px; font-weight: 700; color: var(--live); white-space: nowrap; }}
  .sched-time {{ font-size: 11px; font-weight: 700; color: var(--accent); white-space: nowrap; }}
  .rec-fmt   {{ font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: #3a3a3a; white-space: nowrap; }}

  /* ── Gateway + pipeline ── */
  .gateway-card {{
    background: var(--surface); border: 1px solid var(--accent-bdr);
    border-radius: 4px; padding: 13px 16px;
    display: flex; align-items: center; gap: 16px;
  }}
  .gateway-url {{ font-family: 'Courier New', monospace; font-size: 13px; color: var(--accent); flex: 1; }}
  .gateway-sub {{ font-size: 11px; color: var(--muted); margin-top: 2px; }}

  /* Telemetry panel */
  .telemetry-card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 4px; padding: 12px 16px;
  }}
  .telemetry-grid {{
    display: flex; flex-wrap: wrap; gap: 10px;
  }}
  .telemetry-tile {{
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 4px; padding: 8px 12px;
    min-width: 90px; flex: 1;
  }}
  .telemetry-label {{
    font-size: 9px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .08em; color: var(--muted); margin-bottom: 4px;
  }}
  .telemetry-value {{
    font-size: 18px; font-family: 'Courier New', monospace;
    font-weight: 700; line-height: 1;
  }}
  .telemetry-sub {{
    font-size: 10px; color: var(--muted); margin-top: 3px;
  }}
  .telemetry-bar-track {{
    height: 4px; background: var(--border); border-radius: 2px;
    margin-top: 6px; overflow: hidden;
  }}
  .telemetry-bar-fill {{
    height: 100%; border-radius: 2px;
    transition: width .5s ease, background-color .5s ease;
  }}
  .temp-cool  {{ color: #4dc8a0; }}
  .temp-warm  {{ color: #f0c040; }}
  .temp-hot   {{ color: #f06060; }}
  .fan-active {{ color: var(--accent); }}
  .fan-off    {{ color: var(--muted); }}

  .pipeline-card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 4px; padding: 12px 16px;
    display: flex; align-items: center; gap: 10px;
  }}
  .pipeline-select {{
    flex: 1; background: #111; border: 1px solid var(--border-hi);
    color: var(--text); font-family: 'Inter', sans-serif;
    font-weight: 700; font-size: 13px; padding: 8px 10px;
    border-radius: 4px; outline: none;
  }}

  /* ── Overlays / modals ── */
  .overlay {{
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.82); backdrop-filter: blur(6px);
    z-index: 999; align-items: center; justify-content: center;
  }}
  .overlay.show {{ display: flex !important; }}
  .modal {{
    background: var(--surface); border: 1px solid var(--border-hi);
    border-radius: 4px; padding: 24px 28px;
    max-width: 95vw; max-height: 95vh; overflow-y: auto;
  }}
  .modal-hdr {{
    display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px;
  }}
  .modal-title {{ font-weight: 900; font-size: 15px; text-transform: uppercase; letter-spacing: .06em; color: #e8e8e8; }}
  .modal-close {{ background: none; border: none; color: #444; font-size: 24px; cursor: pointer; padding: 0; line-height: 1; }}
  .modal-close:hover {{ color: #888; }}
  .modal-video {{ width: 100%; aspect-ratio: 16/9; background: #000; display: block; }}

  /* Confirm box */
  .confirm-box {{ background: var(--surface); border: 1px solid var(--border-hi); border-radius: 4px; padding: 28px 32px; max-width: 400px; width: 90%; text-align: center; }}

  /* Form inputs inside modals */
  .form-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 16px; }}
  .form-grid input, .form-grid select {{
    background: #111; border: 1px solid #2a2a2a; color: var(--text);
    font-family: 'Inter', sans-serif; font-size: 13px; padding: 10px 12px;
    border-radius: 4px; outline: none;
  }}
  .form-grid input:focus, .form-grid select:focus {{ border-color: #444; }}
  .span2 {{ grid-column: 1 / -1; }}
  .btn-engage {{
    width: 100%; padding: 11px; font-family: 'Inter', sans-serif;
    font-weight: 900; font-size: 13px; text-transform: uppercase; letter-spacing: .08em;
    background: rgba(180,100,255,.12); border: 1px solid rgba(180,100,255,.3);
    color: var(--purple); border-radius: 4px; cursor: pointer; transition: background .15s;
  }}
  .btn-engage:hover {{ background: rgba(180,100,255,.2); }}
  .adb-home-row {{
    display: flex; align-items: center; gap: 8px;
    font-size: 11px; color: var(--muted); cursor: pointer;
    padding: 4px 0;
  }}
  .adb-home-row input[type=checkbox] {{ width: 14px; height: 14px; flex-shrink: 0; cursor: pointer; }}
  [data-theme="light"] .adb-home-row {{ color: #666; }}
  .btn-enqueue {{
    width: 100%; padding: 11px; font-family: 'Inter', sans-serif;
    font-weight: 900; font-size: 13px; text-transform: uppercase; letter-spacing: .08em;
    background: var(--accent-dim); border: 1px solid var(--accent-bdr);
    color: var(--accent); border-radius: 4px; cursor: pointer; transition: background .15s;
  }}
  .btn-enqueue:hover {{ background: rgba(232,255,71,.18); }}
  .dur-hms {{ display: flex; align-items: center; gap: 5px; }}
  .dur-field {{
    background: #111; border: 1px solid #2a2a2a; color: var(--text);
    font-weight: 700; font-size: 14px; text-align: center; width: 50px;
    padding: 9px 4px; border-radius: 4px; outline: none; -moz-appearance: textfield;
    font-family: 'Inter', sans-serif;
  }}
  .dur-field::-webkit-inner-spin-button, .dur-field::-webkit-outer-spin-button {{ -webkit-appearance: none; }}
  .dur-sep {{ color: #3a3a3a; font-weight: 900; font-size: 16px; }}

  /* ADB remote */
  .adb-panel {{ background: #0a0a0a; border: 1px solid var(--border); border-radius: 4px; padding: 14px; }}
  .adb-ip-input {{
    width: 100%; background: #111; border: 1px solid #2a2a2a; color: #e8e8e8;
    font-size: 12px; font-family: 'Courier New', monospace; padding: 6px 10px;
    border-radius: 4px; outline: none; margin-bottom: 8px;
  }}
  .adb-save-btn {{
    width: 100%; font-family: 'Inter', sans-serif; font-weight: 900; font-size: 10px;
    text-transform: uppercase; letter-spacing: .08em;
    background: rgba(74,158,255,.08); border: 1px solid rgba(74,158,255,.2);
    color: var(--blue); padding: 6px; border-radius: 4px; cursor: pointer;
  }}
  .adb-remote {{ display: flex; flex-direction: column; align-items: center; gap: 6px; margin-top: 14px; }}
  .adb-row {{ display: flex; gap: 6px; }}
  .adb-btn {{
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 13px;
    background: #111; border: 1px solid #2a2a2a; color: #c0c0c0;
    border-radius: 4px; cursor: pointer; display: flex; align-items: center;
    justify-content: center; transition: background .1s, color .1s, transform .07s;
    user-select: none;
  }}
  .adb-btn:hover {{ background: #1e1e1e; color: #fff; }}
  .adb-btn:active {{ transform: scale(.93); background: #2a2a2a; }}
  .adb-dpad   {{ width: 44px; height: 44px; font-size: 17px; }}
  .adb-center {{ width: 44px; height: 44px; font-size: 10px; background: rgba(232,255,71,.06); border-color: rgba(232,255,71,.2); color: var(--accent); }}
  .adb-action {{ height: 34px; padding: 0 12px; font-size: 10px; }}
  .adb-home {{ background: rgba(74,158,255,.06); border-color: rgba(74,158,255,.2); color: var(--blue); }}
  .adb-back {{ background: rgba(255,140,0,.06); border-color: rgba(255,140,0,.2); color: var(--orange); }}
  .adb-status {{ font-size: 10px; color: #2a2a2a; font-family: 'Courier New', monospace; text-align: center; min-height: 14px; margin-top: 8px; transition: color .3s; }}
  .adb-status.ok  {{ color: var(--blue); }}
  .adb-status.err {{ color: var(--live); }}

  /* Manage overlay list */
  .manage-list {{ max-height: 360px; overflow-y: auto; margin-bottom: 16px; }}
  .manage-item {{
    display: flex; gap: 10px; align-items: center; padding: 9px 12px;
    background: #111; border: 1px solid #1e1e1e; border-radius: 4px;
    margin-bottom: 5px; cursor: pointer; font-size: 13px; font-weight: 700;
    color: #e8e8e8;
  }}
  .manage-footer {{ display: flex; gap: 8px; justify-content: flex-end; }}
  .btn-abort  {{ background: #111; border: 1px solid #2a2a2a !important; color: #666; }}
  .btn-commit {{ background: rgba(100,220,80,.1); border: 1px solid rgba(100,220,80,.25) !important; color: var(--green); }}

  /* Toast */
  #hub-toast {{
    display: none; position: fixed; bottom: 24px; left: 50%;
    transform: translateX(-50%); background: var(--surface);
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 12px;
    padding: 9px 20px; border-radius: 4px; z-index: 9999;
    transition: opacity .25s; pointer-events: none; text-transform: uppercase;
    letter-spacing: .06em;
  }}

  /* ── Driver reinstall modal ── */
  .driver-console {{
    background: #020202; border: 1px solid #1e1e1e; border-radius: 4px;
    padding: 12px 14px; height: 280px; overflow-y: auto;
    font-family: 'Courier New', monospace; font-size: 11px; line-height: 1.6;
    color: #aaa; margin: 14px 0;
  }}
  .driver-console .dc-line {{ margin: 0; white-space: pre-wrap; word-break: break-all; }}
  .driver-console .dc-ok   {{ color: #64dc50; }}
  .driver-console .dc-err  {{ color: #ff4444; }}
  .driver-console .dc-info {{ color: #4a9eff; }}
  .driver-path-row {{
    display: flex; gap: 8px; align-items: stretch; margin-bottom: 10px;
  }}
  .driver-path-input {{
    flex: 1; background: #111; border: 1px solid #2a2a2a; color: var(--text);
    font-family: 'Inter', sans-serif; font-size: 12px;
    padding: 9px 12px; border-radius: 4px; outline: none;
  }}
  .driver-path-input:focus {{ border-color: #ff8c00; }}
  .btn-driver-save {{
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 10px;
    text-transform: uppercase; letter-spacing: .1em; padding: 0 14px;
    border-radius: 4px; border: 1px solid rgba(255,140,0,.3);
    background: rgba(255,140,0,.1); color: #ff8c00; cursor: pointer;
    white-space: nowrap; transition: opacity .15s;
  }}
  .btn-driver-save:hover {{ opacity: .8; }}
  .btn-driver-run {{
    width: 100%; font-family: 'Inter', sans-serif; font-weight: 900;
    font-size: 12px; text-transform: uppercase; letter-spacing: .08em;
    padding: 12px; border-radius: 4px; border: none; cursor: pointer;
    background: #ff8c00; color: #000; transition: opacity .15s;
  }}
  .btn-driver-run:hover {{ opacity: .88; }}
  .btn-driver-run:disabled {{ opacity: .35; cursor: not-allowed; }}

  /* ── Folder browser modal ── */
  .browser-toolbar {{
    display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
  }}
  .browser-crumb {{
    flex: 1; font-family: 'Courier New', monospace; font-size: 11px;
    color: #ff8c00; background: #0a0a0a; border: 1px solid #2a2a2a;
    padding: 6px 10px; border-radius: 4px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; direction: rtl;
    text-align: left;
  }}
  .btn-browser-up {{
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 11px;
    padding: 6px 12px; border-radius: 4px; border: 1px solid #2a2a2a;
    background: #111; color: var(--muted); cursor: pointer; flex-shrink: 0;
    transition: color .15s;
  }}
  .btn-browser-up:hover {{ color: var(--text); }}
  .btn-browser-up:disabled {{ opacity: .3; cursor: not-allowed; }}
  .browser-list {{
    background: #060606; border: 1px solid #1e1e1e; border-radius: 4px;
    height: 320px; overflow-y: auto;
  }}
  .browser-item {{
    display: flex; align-items: center; gap: 10px;
    padding: 8px 12px; border-bottom: 1px solid #111;
    cursor: pointer; transition: background .1s; font-size: 12px;
  }}
  .browser-item:last-child {{ border-bottom: none; }}
  .browser-item:hover {{ background: rgba(255,255,255,.04); }}
  .browser-item.has-sh {{ color: #ff8c00; }}
  .browser-item .bi-icon {{ font-size: 14px; flex-shrink: 0; width: 18px; text-align: center; }}
  .browser-item .bi-name {{ flex: 1; font-family: 'Courier New', monospace; }}
  .bi-badge {{
    font-size: 8px; font-weight: 900; text-transform: uppercase;
    letter-spacing: .1em; padding: 2px 6px; border-radius: 3px;
    background: rgba(255,140,0,.12); border: 1px solid rgba(255,140,0,.3);
    color: #ff8c00; flex-shrink: 0;
  }}
  .browser-select-btn {{
    width: 100%; margin-top: 10px;
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 11px;
    text-transform: uppercase; letter-spacing: .08em;
    padding: 10px; border-radius: 4px; border: none; cursor: pointer;
    background: rgba(255,140,0,.15); color: #ff8c00;
    border: 1px solid rgba(255,140,0,.35); transition: opacity .15s;
  }}
  .browser-select-btn:hover {{ opacity: .8; }}
  .browser-select-btn:disabled {{ opacity: .3; cursor: not-allowed; }}

  /* ── Theme toggle button ── */
  .theme-toggle {{
    display: flex; align-items: center; gap: 5px;
    background: var(--accent-dim); border: 1px solid var(--accent-bdr);
    color: var(--text); font-family: 'Inter', sans-serif;
    font-weight: 900; font-size: 9px; text-transform: uppercase; letter-spacing: .12em;
    padding: 5px 10px; border-radius: 4px; cursor: pointer;
    transition: background .15s; white-space: nowrap;
  }}
  .theme-toggle:hover {{ background: var(--accent-bdr); }}
  .theme-toggle .theme-icon {{ font-size: 12px; }}

  /* ── Studio Pro (light) theme overrides ── */
  [data-theme="light"] body              {{ background: #f3f5fa; }}
  [data-theme="light"] .card.live        {{ border-left: 3px solid rgba(77,159,255,.5); }}
  [data-theme="light"] .card-thumb       {{ background: #edf0f8; border-color: #c8cedd; }}
  [data-theme="light"] .thumb-num        {{ color: #c0c8de; }}
  [data-theme="light"] .vd-inner         {{ background: #f8f9fd; border-color: #c8cedd; }}
  [data-theme="light"] .vd-header        {{ background: #edf0f8; border-color: #c8cedd; color: #6878a8; }}
  [data-theme="light"] .vd-ip            {{ color: #4d9fff; }}
  [data-theme="light"] .vd-dur           {{ color: #b0b8d0; }}
  [data-theme="light"] .vd-empty         {{ color: #b0b8d0; }}
  [data-theme="light"] .sys-stat         {{ color: #b0b8d0; }}
  [data-theme="light"] .q-input          {{ background: #fff; border-color: #c8cedd; color: #1a1f38; }}
  [data-theme="light"] .q-btn            {{ background: rgba(77,159,255,.08); border-color: rgba(77,159,255,.2); color: #4d9fff; }}
  [data-theme="light"] .q-btn:hover      {{ background: rgba(77,159,255,.16); }}
  [data-theme="light"] .adb-panel        {{ background: #f8f9fd; border-color: #c8cedd; }}
  [data-theme="light"] .adb-ip-input     {{ background: #fff; border-color: #c8cedd; color: #1a1f38; }}
  [data-theme="light"] .adb-save-btn     {{ background: rgba(77,159,255,.08); border-color: rgba(77,159,255,.2); color: #4d9fff; }}
  [data-theme="light"] .adb-btn          {{ background: #edf0f8; border-color: #c8cedd; color: #1a1f38; }}
  [data-theme="light"] .adb-btn:hover    {{ background: #dde2f0; color: #000; }}
  [data-theme="light"] .adb-center       {{ background: rgba(77,159,255,.1); border-color: rgba(77,159,255,.25); color: #1a6fd4; }}
  [data-theme="light"] .adb-home         {{ background: rgba(77,159,255,.08); border-color: rgba(77,159,255,.2); color: #4d9fff; }}
  [data-theme="light"] .adb-back         {{ background: rgba(217,119,6,.06); border-color: rgba(217,119,6,.2); color: #d97706; }}
  [data-theme="light"] .adb-status       {{ color: #b0b8d0; }}
  [data-theme="light"] .adb-status.ok    {{ color: #4d9fff; }}
  [data-theme="light"] .adb-status.err   {{ color: #dc2626; }}
  [data-theme="light"] .manage-item      {{ background: #f8f9fd; border-color: #c8cedd; color: #1a1f38; }}
  [data-theme="light"] .manage-item:hover {{ background: #edf0f8; }}
  [data-theme="light"] .btn-abort        {{ background: #edf0f8; border-color: #c8cedd !important; color: #6878a8; }}
  [data-theme="light"] .btn-commit       {{ background: rgba(5,150,105,.08); border-color: rgba(5,150,105,.25) !important; color: #059669; }}
  [data-theme="light"] .form-grid input,
  [data-theme="light"] .form-grid select {{ background: #fff; border-color: #c8cedd; color: #1a1f38; }}
  [data-theme="light"] .pipeline-select  {{ background: #fff; border-color: #c8cedd; color: #1a1f38; }}
  [data-theme="light"] .confirm-box      {{ background: #fff; border-color: #c8cedd; }}
  [data-theme="light"] .modal            {{ background: #fff; border-color: #c8cedd; }}
  [data-theme="light"] .modal-close      {{ color: #b0b8d0; }}
  [data-theme="light"] .modal-close:hover {{ color: #6878a8; }}
  [data-theme="light"] #hub-toast        {{ background: #fff; border-color: #c8cedd; color: #1a1f38; }}
  [data-theme="light"] .sig-locked       {{ background: rgba(5,150,105,.08); border-color: rgba(5,150,105,.2); color: #059669; }}
  [data-theme="light"] .sig-other        {{ background: #edf0f8; border-color: #c8cedd; color: #6878a8; }}
  [data-theme="light"] .section-lbl      {{ color: #b0b8d0; }}
  [data-theme="light"] .btn-manage       {{ background: rgba(77,159,255,.06); border-color: rgba(77,159,255,.15); color: #4d9fff; }}
  [data-theme="light"] .btn-manage:hover {{ background: rgba(77,159,255,.12); color: #1a6fd4; }}
  [data-theme="light"] .rescan-btn       {{ background: rgba(77,159,255,.06); border-color: rgba(77,159,255,.15); color: #4d9fff; }}

  /* ── Broadcast Orange (mono) theme overrides ── */
  [data-theme="mono"] .card-thumb        {{ background: #070604; border-color: #2a1e08; }}
  [data-theme="mono"] .card-sub          {{ letter-spacing: .03em; font-size: 10px; }}
  [data-theme="mono"] .sig-locked        {{ background: rgba(255,102,0,.1); border-color: rgba(255,102,0,.25); color: #ff6600; }}
  [data-theme="mono"] .sig-other         {{ background: rgba(255,102,0,.04); border-color: #2a1e08; color: #5a3818; }}
  [data-theme="mono"] .viewers-chip      {{ background: rgba(255,102,0,.08); border-color: rgba(255,102,0,.2); color: #ff6600; }}
  [data-theme="mono"] .viewers-chip:hover {{ background: rgba(255,255,255,.1); }}
  [data-theme="mono"] .vd-inner          {{ background: #050505; border-color: #1c1c1c; }}
  [data-theme="mono"] .vd-header         {{ background: #0a0a0a; border-color: #1c1c1c; }}
  [data-theme="mono"] .vd-ip             {{ color: #999; }}
  [data-theme="mono"] .q-input           {{ background: #080808; border-color: #1c1c1c; color: #fff; }}
  [data-theme="mono"] .q-btn             {{ background: rgba(255,255,255,.05); border-color: rgba(255,255,255,.14); color: #aaa; }}
  [data-theme="mono"] .adb-btn           {{ background: #0a0a0a; border-color: #1c1c1c; color: #888; }}
  [data-theme="mono"] .adb-btn:hover     {{ background: #111; color: #fff; }}
  [data-theme="mono"] .adb-center        {{ background: rgba(255,255,255,.06); border-color: rgba(255,255,255,.18); color: #ddd; }}
  [data-theme="mono"] .adb-home          {{ background: rgba(255,255,255,.04); border-color: rgba(255,255,255,.12); color: #aaa; }}
  [data-theme="mono"] .adb-back          {{ background: rgba(255,255,255,.04); border-color: rgba(255,255,255,.12); color: #888; }}
  [data-theme="mono"] .manage-item       {{ background: #0a0a0a; border-color: #1c1c1c; color: #ccc; }}
  [data-theme="mono"] .btn-abort         {{ background: #0a0a0a; border-color: #1c1c1c !important; color: #555; }}
  [data-theme="mono"] .btn-commit        {{ background: rgba(255,255,255,.07); border-color: rgba(255,255,255,.2) !important; color: #ccc; }}
  [data-theme="mono"] .form-grid input,
  [data-theme="mono"] .form-grid select  {{ background: #080808; border-color: #1c1c1c; color: #fff; }}
  [data-theme="mono"] .pipeline-select   {{ background: #080808; border-color: #1c1c1c; color: #fff; }}
  [data-theme="mono"] .confirm-box       {{ background: #050505; border-color: #222; }}
  [data-theme="mono"] .modal             {{ background: #050505; border-color: #1c1c1c; }}
  [data-theme="mono"] #hub-toast         {{ background: #050505; }}
  [data-theme="mono"] .btn-hls           {{ background: rgba(255,255,255,.07); border-color: rgba(255,255,255,.2) !important; color: #ccc; }}
  [data-theme="mono"] .btn-preview       {{ background: rgba(255,255,255,.04); border-color: rgba(255,255,255,.12) !important; color: #888; }}
  [data-theme="mono"] .btn-vlc           {{ background: rgba(255,255,255,.04); border-color: rgba(255,255,255,.12) !important; color: #777; }}
  [data-theme="mono"] .btn-record-open   {{ background: rgba(255,255,255,.04); border-color: rgba(255,255,255,.1) !important; color: #666; }}
  [data-theme="mono"] .btn-stop-rec      {{ background: rgba(255,255,255,.04); border-color: rgba(255,255,255,.12) !important; color: #888; }}
  [data-theme="mono"] .btn-manage        {{ background: rgba(255,255,255,.04); border-color: rgba(255,255,255,.1); color: #555; }}
  [data-theme="mono"] .btn-manage:hover  {{ background: rgba(255,255,255,.08); color: #aaa; }}
  [data-theme="mono"] .gateway-card      {{ border-color: rgba(255,255,255,.12); }}
  [data-theme="mono"] .hls-url-row       {{ background: #080808; border-color: #1c1c1c; }}
  [data-theme="mono"] .rec-row           {{ background: #080808; border-color: #1c1c1c; }}

  /* ── Encoder selector ── */
  .enc-row   {{ display: flex; align-items: center; gap: 5px; }}
  .enc-select {{
    background: #111; border: 1px solid #2a2a2a; color: var(--accent);
    font-weight: 700; font-size: 11px; padding: 4px 6px; border-radius: 4px;
    outline: none; font-family: 'Inter', sans-serif; cursor: pointer;
    max-width: 160px;
  }}
  .enc-select:focus {{ border-color: var(--accent); box-shadow: 0 0 0 2px rgba(232,255,71,.1); }}

  .dl-panel {{ margin-top: 10px; }}
  .dl-panel-hdr {{
    font-size: 10px; font-weight: 900; text-transform: uppercase; letter-spacing: .1em;
    color: var(--purple); cursor: pointer; display: flex; align-items: center;
    gap: 6px; padding: 5px 0; user-select: none;
  }}
  .dl-panel-hdr:hover {{ color: #c78fff; }}
  .dl-panel-body {{ padding-top: 8px; }}
  .dl-section-lbl {{
    font-size: 9px; font-weight: 900; text-transform: uppercase; letter-spacing: .15em;
    color: #3a3a3a; margin-bottom: 5px;
  }}
  .dl-grid {{
    display: grid; grid-template-columns: 56px 1fr; gap: 5px 8px; align-items: center;
  }}
  .dl-fmt-row {{
    display: flex; align-items: center; gap: 6px; margin-bottom: 8px;
  }}
  .dl-fmt-row .dl-lbl {{ text-align: right; flex-shrink: 0; }}
  .dl-fmt-select {{ flex: 1; }}
  .dl-refresh-btn {{
    flex-shrink: 0; width: 26px; height: 26px;
    background: rgba(180,100,255,.08); border: 1px solid rgba(180,100,255,.25);
    color: var(--purple); border-radius: 4px; cursor: pointer;
    font-size: 14px; display: flex; align-items: center; justify-content: center;
    transition: background .15s;
  }}
  .dl-refresh-btn:hover {{ background: rgba(180,100,255,.2); }}
  .dl-refresh-btn:disabled {{ opacity: .35; cursor: not-allowed; }}
  .dl-lfe-row {{
    display: flex; align-items: flex-start; gap: 7px; margin-top: 8px;
    font-size: 11px; color: var(--muted); cursor: pointer; line-height: 1.4;
  }}
  .dl-lfe-row input[type=checkbox] {{ flex-shrink: 0; margin-top: 2px; cursor: pointer; }}
  .dl-lfe-hint {{ color: #3a3a3a; font-size: 10px; }}
  .dl-audio-warn {{
    margin-top: 6px; padding: 5px 8px; border-radius: 4px;
    background: rgba(255,140,0,.08); border: 1px solid rgba(255,140,0,.2);
    color: var(--orange); font-size: 10px; line-height: 1.4;
  }}
  .mw-la-row {{
    display: flex; align-items: center; gap: 8px;
  }}
  .mw-la-input {{ flex: 1; cursor: pointer; accent-color: var(--purple); }}
  .mw-la-val {{
    font-size: 11px; font-family: 'Courier New', monospace;
    color: var(--accent); min-width: 22px; text-align: right;
  }}
  .mw-edid-status {{
    font-size: 10px; min-height: 14px;
    font-family: 'Courier New', monospace; transition: color .3s;
  }}
  .mw-edid-status.ok  {{ color: var(--green); }}
  .mw-edid-status.err {{ color: var(--live); }}
  .mw-edid-console {{
    margin-top: 8px;
    background: #020202; border: 1px solid #1e1e1e; border-radius: 4px;
    padding: 8px 10px; max-height: 200px; overflow-y: auto;
    font-family: 'Courier New', monospace; font-size: 10px;
    line-height: 1.6; color: #aaa; white-space: pre-wrap; word-break: break-all;
  }}
  [data-theme="light"] .dl-lfe-row  {{ color: #666; }}
  [data-theme="light"] .dl-lfe-hint {{ color: #bbb; }}
  .dl-lbl {{
    font-size: 9px; font-weight: 900; text-transform: uppercase;
    letter-spacing: .1em; color: #3a3a3a; text-align: right;
  }}
  .dl-input {{
    background: #111; border: 1px solid #2a2a2a; color: var(--text);
    font-family: 'Inter', sans-serif; font-size: 11px; padding: 5px 8px;
    border-radius: 4px; outline: none; width: 100%;
  }}
  .dl-input:focus {{ border-color: var(--purple); }}

  /* Light/mono theme overrides for new elements */
  [data-theme="light"] .enc-select    {{ background: #f8f8f8; border-color: #ccc; color: #111; }}
  [data-theme="light"] .dl-input      {{ background: #f8f8f8; border-color: #ccc; color: #111; }}
  [data-theme="mono"]  .enc-select    {{ background: #080808; border-color: #1c1c1c; color: #fff; }}
  [data-theme="mono"]  .dl-input      {{ background: #080808; border-color: #1c1c1c; color: #fff; }}
  [data-theme="mono"]  .dl-panel-hdr  {{ color: #888; }}

  @keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:.15}} }}
  @keyframes rowIn  {{ from{{opacity:0;transform:translateY(-5px)}} to{{opacity:1;transform:none}} }}
  @keyframes rowOut {{ from{{opacity:1}} to{{opacity:0;transform:translateX(16px)}} }}
  .row-entering {{ animation: rowIn  .22s ease forwards; }}
  .row-leaving  {{ animation: rowOut .18s ease forwards; pointer-events: none; }}
</style>

<script>
  // ── Shared helpers ──────────────────────────────────────────────────────────
  function fmtElapsed(s) {{
    const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), sec=s%60;
    return [h,m,sec].map(v=>String(v).padStart(2,'0')).join(':');
  }}
  function hmsToDuration(prefix) {{
    const h=parseInt(document.getElementById(prefix+'-h').value)||0;
    const m=parseInt(document.getElementById(prefix+'-m').value)||0;
    const s=parseInt(document.getElementById(prefix+'-s').value)||0;
    return h*3600+m*60+s;
  }}

  let knownInputIds   = {json.dumps(current_input_ids)};
  let inputMeta       = {meta_js};
  let availEncoders    = {encoders_js};  // [{{value, label}}, ...]
  let availAudioCodecs = {json.dumps(available_audio_codecs or [{"value":"aac","label":"AAC","max_ch":2}])};
  let availChLayouts   = {json.dumps(channel_layouts or [{"value":"stereo","label":"Stereo (2ch)","capture_ch":2,"out_ch":2}])};
  let encoderPresets   = {json.dumps(encoder_presets or {})};

  // ── Viewer drawer ────────────────────────────────────────────────────────────
  function toggleViewerDrawer(safeId) {{
    const d=document.getElementById('vdrawer-'+safeId);
    const c=document.getElementById('vcaret-'+safeId);
    if(!d) return;
    const open=!d.classList.contains('open');
    d.classList.toggle('open',open);
    if(c) c.classList.toggle('open',open);
  }}

  function buildViewerCell(inputId, info) {{
    if(!info) return '';
    const safeId=inputId.replace(/-/g,'_');
    const count=info.viewers||0;
    const list=info.viewer_list||[];
    const label=count+' Viewer'+(count!==1?'s':'');
    let rows='';
    for(const vw of list) rows+=`<div class="vd-row"><span class="vd-ip">${{vw.ip}}</span><span class="vd-dur">${{fmtElapsed(vw.elapsed)}}</span></div>`;
    if(!rows) rows='<div class="vd-empty">No direct stream clients</div>';
    return `<span class="viewers-chip" id="vchip-${{safeId}}" onclick="toggleViewerDrawer('${{safeId}}')">${{label}} <span class="vchip-caret" id="vcaret-${{safeId}}">&#9660;</span></span>
      <div class="viewer-drawer" id="vdrawer-${{safeId}}"><div class="vd-inner"><div class="vd-header"><span>IP</span><span>Duration</span></div>${{rows}}</div></div>`;
  }}

  // ── SSE live updates ─────────────────────────────────────────────────────────
  const _prevState = {{}};

  function buildCtrlCell(id, isLive, isHls) {{
    const hlsBtn = isHls
      ? `<form action='/hls/stop/${{id}}' method='post' style='display:inline'><button type='submit' class='btn btn-hls-stop'>Stop HLS</button></form>`
      : `<button class='btn btn-hls' onclick="startHLS('${{id}}')">Start HLS</button>`;
    return `<button class="btn btn-preview" onclick="openPreview('${{id}}')">Preview</button>
      <a href="/play/${{id}}" class="btn btn-vlc">VLC</a>
      ${{hlsBtn}}
      <button class="btn btn-record-open" onclick="openRecord('${{id}}')">&#9210; Rec</button>`;
  }}

  function buildInputCard(id) {{
    const m=inputMeta[id]||{{}};
    const encOpts=availEncoders.map(e=>`<option value="${{e.value}}"${{e.value===(m.encoder||'')?' selected':''}}>${{e.label}}</option>`).join('');
    return `<div class="card row-entering" id="card-${{id}}" style="opacity:.5">
      <div class="card-body">
        <div class="card-thumb" id="thumb-${{id}}">
          <div class="thumb-num" style="font-style:italic">${{id}}</div>
        </div>
        <div class="card-info">
          <div class="card-title">${{m.label||id}}</div>
          <div class="card-sub" id="sub-${{id}}">Offline — waiting for signal</div>
          <div class="card-meta">
            <div id="viewers-${{id}}"></div>
            <span class="sys-stat" id="resources-${{id}}" style="display:none"></span>
            <div class="q-row" id="qrow-${{id}}">
              <span class="q-label">Q</span>
              <input class="q-input" id="q-input-${{id}}" type="number" min="1" max="51" value="25"
                onkeydown="if(event.key==='Enter')submitQ('${{id}}')">
              <button class="q-btn" onclick="submitQ('${{id}}')">Set</button>
            </div>
            <div class="enc-row">
              <span class="q-label">ENC</span>
              <select class="enc-select" id="enc-select-${{id}}" onchange="submitEncoder('${{id}}')">${{encOpts}}</select>
            </div>
          </div>
        </div>
        <div class="btn-row" id="ctrl-${{id}}">${{buildCtrlCell(id,false,false)}}</div>
      </div>
    </div>`;
  }}

  function applyStats(data) {{
    const inputs=data.inputs||{{}}, hlsIds=data.hls||[], recs=data.recordings||[];
    const qCfg=data.q||{{}}, sseIds=data.input_ids||[], sseMeta=data.meta||{{}};

    // ── Driver missing banner — checked first so a JS error below can't hide it ──
    const banner = document.getElementById('driver-banner');
    if (banner) banner.classList.toggle('show', !!data.driver_missing);
    if (data.installer_path) {{
      const inp = document.getElementById('driver-path-input');
      if (inp && !inp.value) inp.value = data.installer_path;
    }}

    const ind=document.getElementById('sse-indicator');
    if(ind){{ ind.style.opacity='1'; clearTimeout(ind._t); ind._t=setTimeout(()=>ind.style.opacity='.3',3000); }}

    Object.assign(inputMeta, sseMeta);

    // Update available encoders/codecs lists if server sent them
    if(data.available_encoders)      availEncoders    = data.available_encoders;
    if(data.available_audio_codecs)  availAudioCodecs = data.available_audio_codecs;
    if(data.channel_layouts)         availChLayouts   = data.channel_layouts;
    if(data.encoder_presets)         encoderPresets   = data.encoder_presets;

    // Add new inputs
    for(const id of sseIds) {{
      if(!knownInputIds.includes(id)) {{
        knownInputIds.push(id);
        const container=document.getElementById('cards-container');
        if(container) container.insertAdjacentHTML('beforeend', buildInputCard(id));
        updateInputSelects(sseIds);
      }}
    }}
    // Remove gone inputs
    for(const id of [...knownInputIds]) {{
      if(!sseIds.includes(id)) {{
        knownInputIds=knownInputIds.filter(x=>x!==id);
        const card=document.getElementById('card-'+id);
        if(card){{ card.classList.add('row-leaving'); setTimeout(()=>card.remove(),200); }}
        updateInputSelects(sseIds);
      }}
    }}

    for(const id of sseIds) {{
      const info=inputs[id], isLive=!!info, isHls=hlsIds.includes(id);
      const m=sseMeta[id]||{{}}, safeId=id.replace(/-/g,'_');
      const driver=m.driver||'magewell', encoder=m.encoder||'h264_qsv';

      const card=document.getElementById('card-'+id);
      if(card) {{
        card.classList.toggle('live', isLive && !isHls);
        card.classList.toggle('hls-on', isHls);
        card.style.opacity=(isLive||isHls)?'1':'0.5';
      }}

      // Live / HLS / driver thumb badges
      const thumb=document.getElementById('thumb-'+id);
      if(thumb) {{
        let badges='';
        if(isLive)  badges+=`<div class="thumb-badge live-badge"><span class="blink-dot"></span> Live</div>`;
        if(isHls)   badges+=`<div class="thumb-badge hls-badge-th"><span class="blink-dot hls-dot-col"></span> HLS</div>`;
        if(!isLive) badges+=`<div class="no-sig">No Signal</div>`;
        Array.from(thumb.children).forEach(c=>{{ if(!c.classList.contains('thumb-num')) c.remove(); }});
        thumb.insertAdjacentHTML('beforeend', badges);
      }}

      // Subtitle — use actual encoder name instead of hardcoded h264_qsv
      const sub=document.getElementById('sub-'+id);
      if(sub) {{
        const isFaulted=m.faulted||false, restarts=m.restarts||0;
        if(isFaulted)        sub.textContent=`⚠ Faulted · ${{restarts}} restarts`;
        else if(isHls)       sub.textContent=`${{encoder}} · ${{m.desc||'signal ok'}} · HLS`;
        else if(isLive)      sub.textContent=`${{encoder}} · ${{m.desc||'signal ok'}} · ready`;
        else                 sub.textContent='Offline — waiting for signal';
        sub.style.color=isFaulted?'#fb923c':'';
      }}

      // Viewers
      const vc=document.getElementById('viewers-'+id);
      if(vc) {{
        const wasOpen=document.getElementById('vdrawer-'+safeId)?.classList.contains('open')||false;
        vc.innerHTML=buildViewerCell(id,info);
        if(wasOpen && isLive) {{
          document.getElementById('vdrawer-'+safeId)?.classList.add('open');
          document.getElementById('vcaret-'+safeId)?.classList.add('open');
        }}
      }}

      // Stats
      const rc=document.getElementById('resources-'+id);
      if(rc) {{
        if(isLive) {{ rc.textContent=`${{info.cpu}}% CPU · ${{info.mem}}MB`; rc.style.display=''; }}
        else       {{ rc.style.display='none'; }}
      }}

      // Q value
      const qi=document.getElementById('q-input-'+id);
      if(qi && document.activeElement!==qi && qCfg[id]!==undefined) qi.value=qCfg[id];

      // Encoder select — sync without wiping user interaction
      const es=document.getElementById('enc-select-'+id);
      if(es && document.activeElement!==es && m.encoder) es.value=m.encoder;

      // Q row is always visible (Decklink CBR mode removed)
      const qrow=document.getElementById('qrow-'+id);
      if(qrow) qrow.style.display='';

      // Ctrl buttons (only rebuild on state change)
      const prev=_prevState[id]||{{}};
      if(prev.isLive!==isLive || prev.isHls!==isHls) {{
        const ctrl=document.getElementById('ctrl-'+id);
        if(ctrl) ctrl.innerHTML=buildCtrlCell(id,isLive,isHls);
        _prevState[id]={{isLive,isHls}};
      }}
    }}

    // Update recording timers
    recs.forEach(r=>{{
      const el=document.getElementById('rec-elapsed-'+r.id);
      if(el) el.textContent='● REC '+fmtElapsed(r.elapsed)+(r.duration?' / '+fmtElapsed(r.duration):'');
    }});
    if (data.fan_profile) _ccUpdateBadge(data.fan_profile);
  }}

  function updateInputSelects(ids) {{
    const sel=document.getElementById('rs-input-select');
    if(!sel) return;
    const cur=sel.value;
    sel.innerHTML=ids.map(id=>`<option value="${{id}}"${{id===cur?' selected':''}}>${{inputMeta[id]?.label||id}}</option>`).join('');
  }}

  document.addEventListener('DOMContentLoaded', () => {{
    (function connectSSE() {{
      const es=new EventSource('/api/stats');
      es.onmessage=e=>{{ try{{ applyStats(JSON.parse(e.data)); }}catch(err){{ console.warn('SSE',err); }} }};
      es.onerror=()=>{{ es.close(); setTimeout(connectSSE,3000); }};
    }})();
  }});

  // ── Driver reinstall modal ───────────────────────────────────────────────────
  let _driverSSE = null;

  function openDriverModal() {{
    document.getElementById('driver-overlay').classList.add('show');
  }}
  function closeDriverModal() {{
    document.getElementById('driver-overlay').classList.remove('show');
    if (_driverSSE) {{ _driverSSE.close(); _driverSSE = null; }}
  }}

  async function saveDriverPath() {{
    const path = document.getElementById('driver-path-input').value.trim();
    if (!path) {{ showToast('Enter a path first', false); return; }}
    const fd = new FormData();
    fd.append('installer_path', path);
    try {{
      const res  = await fetch('/admin/driver/set-path', {{method:'POST', body:fd}});
      const data = await res.json();
      if (data.ok) showToast('Installer path saved');
      else showToast(data.error || 'Failed to save path', false);
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  function dcAppend(text, cls='') {{
    const con = document.getElementById('driver-console');
    const p   = document.createElement('p');
    p.className = 'dc-line' + (cls ? ' '+cls : '');
    p.textContent = text;
    con.appendChild(p);
    con.scrollTop = con.scrollHeight;
  }}

  function runDriverInstall() {{
    const path = document.getElementById('driver-path-input').value.trim();
    if (!path) {{ showToast('Enter the installer path first', false); return; }}

    const con = document.getElementById('driver-console');
    con.innerHTML = '';
    const btn = document.getElementById('driver-run-btn');
    btn.disabled = true;
    btn.textContent = '⏳ Running…';

    // Save the path first, then stream the install
    const fd = new FormData();
    fd.append('installer_path', path);
    fetch('/admin/driver/set-path', {{method:'POST', body:fd}})
      .then(r => r.json())
      .then(data => {{
        if (!data.ok) {{
          dcAppend('✗ ' + (data.error || 'Failed to save path'), 'dc-err');
          btn.disabled = false; btn.textContent = '▶ Run Installer';
          return;
        }}
        // Open SSE stream for live output
        if (_driverSSE) _driverSSE.close();
        _driverSSE = new EventSource('/admin/driver/reinstall-stream');

        _driverSSE.addEventListener('line', ev => {{
          const text = ev.data;
          const cls  = text.startsWith('✓') ? 'dc-ok'
                     : text.startsWith('✗') ? 'dc-err'
                     : text.startsWith('Running') ? 'dc-info' : '';
          dcAppend(text, cls);
        }});

        _driverSSE.addEventListener('done', ev => {{
          _driverSSE.close(); _driverSSE = null;
          btn.disabled = false;
          try {{
            const result = JSON.parse(ev.data);
            if (result.ok && !result.reboot_required) {{
              btn.textContent = '✓ Done — driver loaded';
              document.getElementById('driver-banner').classList.remove('show');
              showToast('Magewell driver reinstalled successfully');
            }} else if (result.ok && result.reboot_required) {{
              btn.textContent = '↺ Reboot required';
              dcAppend('A reboot is required to complete the installation.', 'dc-info');
              showToast('Driver installed — please reboot', false);
            }} else {{
              btn.textContent = '▶ Run Installer';
              showToast('Installer failed — check the console output', false);
            }}
          }} catch(e) {{
            btn.disabled = false; btn.textContent = '▶ Run Installer';
          }}
        }});

        _driverSSE.onerror = () => {{
          dcAppend('Connection lost — check the Log viewer for details.', 'dc-err');
          _driverSSE.close(); _driverSSE = null;
          btn.disabled = false; btn.textContent = '▶ Run Installer';
        }};
      }})
      .catch(e => {{
        dcAppend('Network error: ' + e, 'dc-err');
        btn.disabled = false; btn.textContent = '▶ Run Installer';
      }});
  }}

  // ── Folder browser ──────────────────────────────────────────────────────────
  let _browserCurrentPath = '/';
  let _browserHasSh       = false;

  async function openFolderBrowser() {{
    // Start from current path value or home directory
    const current = document.getElementById('driver-path-input').value.trim();
    const startPath = current || '/home';
    document.getElementById('browser-overlay').classList.add('show');
    await browseTo(startPath);
  }}

  function closeFolderBrowser() {{
    document.getElementById('browser-overlay').classList.remove('show');
  }}

  async function browseTo(path) {{
    const list = document.getElementById('browser-list');
    list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--muted);font-size:12px">Loading…</div>';

    try {{
      const res  = await fetch('/admin/browse?path=' + encodeURIComponent(path));
      const data = await res.json();

      _browserCurrentPath = data.path;
      _browserHasSh       = data.has_install_sh;

      // Update crumb
      document.getElementById('browser-crumb').textContent = data.path;

      // Up button — disable at filesystem root
      const upBtn = document.getElementById('browser-up-btn');
      upBtn.disabled = !data.parent;

      // Select button — only enable if install.sh is here
      const selBtn = document.getElementById('browser-select-btn');
      selBtn.disabled = !data.has_install_sh;
      selBtn.textContent = data.has_install_sh
        ? '✓ Select This Folder (install.sh found)'
        : 'Select This Folder (no install.sh here)';
      selBtn.style.background    = data.has_install_sh ? 'rgba(255,140,0,.2)' : '';
      selBtn.style.borderColor   = data.has_install_sh ? 'rgba(255,140,0,.5)' : '';

      // Render directory list
      if (data.dirs.length === 0) {{
        list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--muted);font-size:12px">No subdirectories</div>';
        return;
      }}

      list.innerHTML = data.dirs.map(d => `
        <div class="browser-item${{d.has_install_sh ? ' has-sh' : ''}}"
             onclick="browseTo('${{d.path.replace(/'/g, "\\'")}}')">
          <span class="bi-icon">${{d.has_install_sh ? '📦' : '📁'}}</span>
          <span class="bi-name">${{d.name}}</span>
          ${{d.has_install_sh ? '<span class="bi-badge">install.sh</span>' : ''}}
        </div>`).join('');

    }} catch(e) {{
      list.innerHTML = '<div style="padding:20px;text-align:center;color:#ff4444;font-size:12px">Failed to load directory</div>';
    }}
  }}

  function browseUp() {{
    // Navigate to parent by stripping last path component
    const parent = _browserCurrentPath.split('/').slice(0, -1).join('/') || '/';
    browseTo(parent);
  }}

  function selectBrowserPath() {{
    if (!_browserHasSh) return;
    document.getElementById('driver-path-input').value = _browserCurrentPath;
    closeFolderBrowser();
    showToast('Path selected — click Save Path to confirm');
  }}

  // ── HLS start ────────────────────────────────────────────────────────────────
  async function startHLS(inputId) {{
    await fetch('/hls/'+inputId+'/index.m3u8');
    location.reload();
  }}

  // ── Manage inputs overlay ────────────────────────────────────────────────────
  let _manageHiddenSet  = new Set();
  let _manageShowHidden = false;

  async function openManage() {{
    const btn = document.getElementById('manage-refresh-btn');
    if (btn) {{ btn.disabled = true; btn.textContent = 'Scanning…'; }}
    let data;
    try {{
      const res = await fetch('/inputs/list');
      data = await res.json();
    }} catch(e) {{
      showToast('Failed to load inputs', false);
      if (btn) {{ btn.disabled = false; btn.textContent = '↺ Refresh'; }}
      return;
    }}
    _manageHiddenSet = new Set(data.inputs.filter(i => i.hidden).map(i => i.key));
    _renderManageList(data.inputs);
    if (btn) {{ btn.disabled = false; btn.textContent = '↺ Refresh'; }}
    document.getElementById('manage-overlay').classList.add('show');
  }}

  function _renderManageList(inputs) {{
    const list       = document.getElementById('manage-list');
    const toggleBtn  = document.getElementById('manage-show-hidden-btn');
    const hiddenCount = inputs.filter(i => i.hidden).length;
    if (toggleBtn) {{
      toggleBtn.textContent = _manageShowHidden ? '▲ Hide restored' : `▼ Show hidden (${{hiddenCount}})`;
      toggleBtn.style.display = hiddenCount > 0 ? '' : 'none';
    }}
    list.innerHTML = inputs.map(inp => {{
      const dimmed   = (inp.signal === 'NONE' || inp.signal === 'UNKNOWN') ? 'opacity:0.6;' : '';
      if (inp.hidden) {{
        if (!_manageShowHidden) return '';
        return `<div class="manage-item manage-item-hidden" style="opacity:0.45;gap:8px;cursor:default">
          <span style="flex:1;font-size:11px">${{inp.label}}</span>
          <span style="color:#3a3a3a;font-weight:700;font-size:10px">[${{inp.signal}}]</span>
          <button class="btn btn-manage" style="font-size:9px;padding:3px 9px;white-space:nowrap"
            onclick="restoreInput('${{inp.key}}')">&#8629; Restore</button>
        </div>`;
      }}
      return `<label class="manage-item" style="${{dimmed}}">
        <input type="checkbox" value="${{inp.key}}" ${{inp.active ? 'checked' : ''}}>
        <span style="flex:1">${{inp.label}}</span>
        <span style="color:#3a3a3a;font-weight:700;font-size:10px">[${{inp.signal}}]</span>
      </label>`;
    }}).join('');
  }}

  async function refreshManage() {{
    const btn = document.getElementById('manage-refresh-btn');
    if (btn) {{ btn.disabled = true; btn.textContent = 'Scanning…'; }}
    try {{
      const res  = await fetch('/inputs/rescan', {{method: 'POST'}});
      const data = await res.json();
      if (data.remapped && data.remapped.length > 0) {{
        const msgs = data.remapped.map(r => `${{r.from}} → ${{r.to}} (${{r.reason}})`);
        showToast('Remapped: ' + msgs.join(', '));
      }} else if (data.added && data.added.length > 0) {{
        showToast('Found ' + data.added.length + ' new input(s)');
      }}
    }} catch(e) {{}}
    await openManage();
  }}

  function closeManage() {{
    document.getElementById('manage-overlay').classList.remove('show');
    _manageShowHidden = false;
  }}

  function toggleManageHidden() {{
    _manageShowHidden = !_manageShowHidden;
    openManage();
  }}

  async function restoreInput(key) {{
    try {{
      const res  = await fetch('/input/' + key + '/restore', {{method: 'POST'}});
      const data = await res.json();
      if (data.ok) {{
        showToast('Input restored — reloading…');
        setTimeout(() => location.reload(), 800);
      }} else {{
        showToast(data.error || 'Restore failed', false);
      }}
    }} catch(e) {{
      showToast('Network error', false);
    }}
  }}

  async function applyManage() {{
    const checked   = new Set(Array.from(document.querySelectorAll('#manage-list input:checked')).map(i => i.value));
    const allKeys   = Array.from(document.querySelectorAll('#manage-list input[type=checkbox]')).map(i => i.value);
    const newHidden = new Set([..._manageHiddenSet]);
    for (const k of allKeys) {{
      if (checked.has(k)) newHidden.delete(k);
      else newHidden.add(k);
    }}
    await fetch('/inputs/apply', {{method:'POST', body:JSON.stringify({{active:Array.from(checked), hidden:Array.from(newHidden)}}), headers:{{'Content-Type':'application/json'}}}});
    location.reload();
  }}

  // ── Q control ────────────────────────────────────────────────────────────────
  let _pendingQ=null;
  function submitQ(inputId) {{
    const val=parseInt(document.getElementById('q-input-'+inputId).value);
    if(isNaN(val)||val<1||val>51){{ alert('Q must be 1–51'); return; }}
    const card=document.getElementById('card-'+inputId);
    const isLive=card&&card.classList.contains('live');
    if(isLive) {{
      _pendingQ={{inputId,val}};
      document.getElementById('confirm-msg').textContent=`Set ${{inputMeta[inputId]?.label||inputId}} to Q:${{val}}? Input will restart briefly.`;
      document.getElementById('confirm-overlay').classList.add('show');
    }} else {{ doSetQ(inputId,val); }}
  }}
  function confirmSetQ() {{
    document.getElementById('confirm-overlay').classList.remove('show');
    if(_pendingQ){{ doSetQ(_pendingQ.inputId,_pendingQ.val); _pendingQ=null; }}
  }}
  function cancelSetQ() {{ document.getElementById('confirm-overlay').classList.remove('show'); _pendingQ=null; }}

  async function doSetQ(inputId,val) {{
    const fd=new FormData(); fd.append('q',val);
    try {{
      const res=await fetch('/input/'+inputId+'/set_q',{{method:'POST',body:fd}});
      const data=await res.json();
      if(data.ok) showToast(data.restarted?`Q:${{val}} — restarting…`:`Q:${{val}} saved`);
      else showToast('Failed to set Q',false);
    }} catch(e) {{ showToast('Network error',false); }}
  }}

  // ── Toast ────────────────────────────────────────────────────────────────────
  function showToast(msg,ok=true) {{
    const t=document.getElementById('hub-toast');
    t.textContent=msg;
    t.style.border=ok?'1px solid rgba(232,255,71,.3)':'1px solid rgba(255,59,59,.3)';
    t.style.color=ok?'#e8ff47':'#ff3b3b';
    t.style.display='block'; t.style.opacity='1';
    clearTimeout(t._t);
    t._t=setTimeout(()=>{{ t.style.opacity='0'; setTimeout(()=>t.style.display='none',300); }},2600);
  }}

  // ── Encoder control ──────────────────────────────────────────────────────────
  async function submitEncoder(inputId) {{
    const sel = document.getElementById('enc-select-'+inputId);
    if (!sel) return;
    const encoder = sel.value;
    const card = document.getElementById('card-'+inputId);
    const isLive = card && card.classList.contains('live');
    if (isLive) {{
      if (!confirm(`Switch ${{inputMeta[inputId]?.label||inputId}} to ${{encoder}}?\\nThe input will restart briefly.`)) {{
        sel.value = inputMeta[inputId]?.encoder || sel.value;
        return;
      }}
    }}
    const fd = new FormData(); fd.append('encoder', encoder);
    try {{
      const res = await fetch('/input/'+inputId+'/set_encoder', {{method:'POST', body:fd}});
      const data = await res.json();
      if (data.ok) showToast(data.restarted ? `Encoder → ${{encoder}} · restarting…` : `Encoder → ${{encoder}} saved`);
      else showToast(data.error || 'Failed to set encoder', false);
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  // ── ADB ──────────────────────────────────────────────────────────────────────
  var _activeAdbInputId=null, _adbKeyboardActive=false;
  const KEY_MAP={{'ArrowUp':'up','ArrowDown':'down','ArrowLeft':'left','ArrowRight':'right','Enter':'enter','Backspace':'back'}};

  // ── Unified remote ───────────────────────────────────────────────────────────

  function onRemoteTypeChange() {{
    const sel = document.getElementById('remote-type-sel');
    const ipRow = document.getElementById('remote-ip-row');
    const btnRow = document.getElementById('remote-buttons');
    const isNone = sel.value === 'none';
    ipRow.style.display  = isNone ? 'none' : '';
    btnRow.style.display = isNone ? 'none' : '';
    // Update IP field placeholder
    const field = document.getElementById('adb-ip-field');
    if (field) field.placeholder = sel.value === 'roku' ? 'Roku IP (e.g. 192.168.1.150)' : 'Device IP';
  }}

  async function saveRemote(inputId) {{
    const type = document.getElementById('remote-type-sel').value;
    const ip   = document.getElementById('adb-ip-field').value.trim();
    if (type !== 'none' && !ip) {{ showToast('Enter an IP address first', false); return; }}
    const btn = document.querySelector('.adb-save-btn');
    if (btn) {{ btn.textContent = 'Connecting…'; btn.disabled = true; }}
    const fd = new FormData();
    fd.append('remote_type', type);
    fd.append('remote_ip',   ip);
    try {{
      const res  = await fetch(`/input/${{inputId}}/set_remote`, {{method:'POST', body:fd}});
      const data = await res.json();
      const st   = document.getElementById('adb-status');
      if (data.connected) {{
        showToast(`✓ Connected — ${{ip}}`);
        if (st) {{ st.textContent = `linked ${{ip}}`; st.className = 'adb-status ok'; }}
        document.getElementById('remote-buttons').style.display = '';
      }} else {{
        showToast(data.connect_msg || 'Saved (not verified)', false);
        if (st) {{ st.textContent = data.connect_msg || 'not verified'; st.className = 'adb-status err'; }}
      }}
      // Update home checkbox visibility — show for both ADB and Roku
      const homeRow = document.getElementById('rec-adb-home-row');
      if (homeRow) homeRow.style.display = (type !== 'none' && ip) ? '' : 'none';
      // Cache in inputMeta
      if (inputMeta[inputId]) {{
        inputMeta[inputId].remote_type = type;
        inputMeta[inputId].remote_ip   = ip;
        inputMeta[inputId].adb_ip      = type === 'adb' ? ip : '';
      }}
    }} catch(e) {{
      showToast('Network error', false);
    }} finally {{
      if (btn) {{ btn.textContent = 'Link Device'; btn.disabled = false; }}
    }}
  }}

  async function remoteKey(key) {{
    if (!_activeAdbInputId) return;
    const fd = new FormData(); fd.append('key', key);
    try {{
      const res  = await fetch(`/input/${{_activeAdbInputId}}/remote_key`, {{method:'POST', body:fd}});
      const data = await res.json();
      const st   = document.getElementById('adb-status');
      if (data.ok) {{ if (st) {{ st.textContent = `↑ ${{key}}`; st.className = 'adb-status ok'; }} }}
      else          {{ if (st) {{ st.textContent = data.error || 'Error'; st.className = 'adb-status err'; }} }}
    }} catch(e) {{}}
  }}

  // Legacy wrapper — keyboard handler uses this
  async function adbKey(key) {{ await remoteKey(key); }}

  // ── Preview modal ────────────────────────────────────────────────────────────
  const players={{}};
  function startPlayer(videoId,inputId) {{
    const video=document.getElementById(videoId);
    if(players[videoId]){{ players[videoId].destroy(); delete players[videoId]; }}
    const p=mpegts.createPlayer({{type:'mpegts',isLive:true,url:'/preview/'+inputId}});
    p.attachMediaElement(video); p.load(); video.play(); players[videoId]=p;
  }}
  function stopPlayer(vId) {{ if(players[vId]){{ players[vId].destroy(); delete players[vId]; }} }}

  function openPreview(id) {{
    document.getElementById('preview-title').textContent='Signal: '+id;
    document.getElementById('preview-overlay').classList.add('show');
    startPlayer('preview-video',id);
  }}
  function closePreview() {{ document.getElementById('preview-overlay').classList.remove('show'); stopPlayer('preview-video'); }}

  function openRecord(id) {{
    _activeAdbInputId=id; _adbKeyboardActive=true;
    document.getElementById('record-input-id').value=id;
    document.getElementById('record-title').textContent='Record — '+(inputMeta[id]?.label||id);

    // Restore saved remote type and IP
    const meta        = inputMeta[id] || {{}};
    const remoteType  = meta.remote_type || (meta.adb_ip ? 'adb' : 'none');
    const remoteIp    = meta.remote_ip   || meta.adb_ip || '';
    // If we have an IP but type is still 'none' (pre-migration config), infer type
    const effectiveType = (remoteType === 'none' && remoteIp)
      ? (remoteIp === meta.adb_ip ? 'adb' : 'roku')
      : remoteType;
    const sel         = document.getElementById('remote-type-sel');
    const ipField     = document.getElementById('adb-ip-field');
    const ipRow       = document.getElementById('remote-ip-row');
    const btnRow      = document.getElementById('remote-buttons');
    if (sel)     sel.value     = effectiveType;
    if (ipField) ipField.value = remoteIp;
    if (ipField) ipField.placeholder = effectiveType === 'roku' ? 'Roku IP (e.g. 192.168.1.150)' : 'Device IP';
    if (ipRow)   ipRow.style.display  = effectiveType !== 'none' ? '' : 'none';
    if (btnRow)  btnRow.style.display = (effectiveType !== 'none' && remoteIp) ? '' : 'none';

    // Load the user's saved directory from the server
    fetch('/prefs/rec_dir').then(r=>r.json()).then(d=>{{
      document.getElementById('rec-dir').value = d.rec_dir || '';
    }}).catch(()=>{{}});

    // Show/hide home checkbox — relevant for both ADB and Roku
    const homeRow  = document.getElementById('rec-adb-home-row');
    const homeChk  = document.getElementById('rec-adb-home');
    if (homeRow) homeRow.style.display = (effectiveType !== 'none' && remoteIp) ? '' : 'none';
    if (homeChk) homeChk.checked = !!meta.adb_home;

    document.getElementById('record-overlay').classList.add('show');
    startPlayer('record-video',id);
  }}
  function closeRecord() {{ document.getElementById('record-overlay').classList.remove('show'); stopPlayer('record-video'); _adbKeyboardActive=false; }}

  function submitRecordForm() {{
    const dir = document.getElementById('rec-dir').value.trim();
    // Persist directory to the server so it survives reboots and is per-user
    if (dir) {{
      const fd = new FormData(); fd.append('rec_dir', dir);
      fetch('/prefs/rec_dir', {{method:'POST', body:fd}}).catch(()=>{{}});
    }}
    document.getElementById('rec-dur-hidden').value = hmsToDuration('rec-dur');
    document.getElementById('record-form').submit();
  }}

  async function saveDefaultDir() {{
    const dir = document.getElementById('rec-dir').value.trim();
    if (!dir) {{ showToast('Enter a directory first', false); return; }}
    const btn = document.getElementById('save-dir-btn');
    if (btn) {{ btn.textContent = 'Saving…'; btn.disabled = true; }}
    const fd = new FormData(); fd.append('rec_dir', dir);
    try {{
      const res = await fetch('/prefs/rec_dir', {{method:'POST', body:fd}});
      const data = await res.json();
      if (data.ok) showToast('Default directory saved');
      else showToast('Failed to save directory', false);
    }} catch(e) {{ showToast('Network error', false); }}
    finally {{ if (btn) {{ btn.textContent = 'Save Default'; btn.disabled = false; }} }}
  }}

  function openSchedule(id) {{
    document.getElementById('schedule-input-id').value=id;
    document.getElementById('schedule-title').textContent='Schedule — '+(inputMeta[id]?.label||id);
    document.getElementById('schedule-overlay').classList.add('show');
    startPlayer('schedule-video',id);
  }}
  function closeSchedule() {{ document.getElementById('schedule-overlay').classList.remove('show'); stopPlayer('schedule-video'); }}

  function submitScheduleForm() {{ document.getElementById('sched-dur-hidden').value=hmsToDuration('sched-dur'); document.getElementById('schedule-form').submit(); }}

  document.addEventListener('keydown', e=>{{
    if(e.key==='Escape'){{ closePreview(); closeRecord(); closeSchedule(); closeManage(); }}
    const tag = document.activeElement?.tagName?.toLowerCase();
    const isEditable = tag === 'input' || tag === 'textarea' || tag === 'select' || document.activeElement?.isContentEditable;
    if(_adbKeyboardActive && !isEditable && KEY_MAP[e.key]){{ e.preventDefault(); adbKey(KEY_MAP[e.key]); }}
  }});

  // ── Theme toggle ─────────────────────────────────────────────────────────────
  const THEMES = ['dark', 'mono', 'light'];
  const THEME_LABELS = {{ dark: '● Neon Ops', mono: '◐ Broadcast', light: '○ Studio Pro' }};

  function applyTheme(t) {{
    document.documentElement.setAttribute('data-theme', t);
    try {{ localStorage.setItem('bh-theme', t); }} catch(e) {{}}
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = THEME_LABELS[t] || t;
  }}

  function cycleTheme() {{
    const cur = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
    applyTheme(next);
  }}

  // ── Magewell config panel ─────────────────────────────────────────────────
  function toggleMwPanel(inputId) {{
    const body  = document.getElementById('mwbody-'+inputId);
    const caret = document.getElementById('mwcaret-'+inputId);
    if (!body) return;
    const open = body.style.display === 'none';
    body.style.display  = open ? '' : 'none';
    if (caret) caret.style.transform = open ? 'rotate(180deg)' : '';
    // Populate preset dropdown based on current encoder when panel opens
    if (open) mwUpdatePresets(inputId);
    // Scan for available EDID files when panel opens
    if (open) mwLoadEdidList(inputId);
  }}

  // Rebuild the preset dropdown for this input based on its current encoder.
  function mwUpdatePresets(inputId) {{
    const sel  = document.getElementById('mw-preset-'+inputId);
    if (!sel) return;
    const meta    = inputMeta[inputId] || {{}};
    const encoder = meta.encoder || '';
    const presets = encoderPresets[encoder] || [];
    // Preserve current selection if possible
    const current = sel.value;
    sel.innerHTML = '<option value="">— default —</option>';
    for (const p of presets) {{
      const opt = document.createElement('option');
      opt.value = p; opt.textContent = p;
      if (p === current) opt.selected = true;
      sel.appendChild(opt);
    }}
    // Show/hide preset row depending on whether codec has presets
    const row = sel.closest('.dl-grid')?.querySelector('label.dl-lbl');
    sel.style.opacity = presets.length ? '1' : '0.35';
    sel.disabled = !presets.length;
  }}

  async function submitMwCfg(inputId) {{
    const fd = new FormData();
    fd.append('preset',          document.getElementById('mw-preset-'+inputId)?.value || '');
    fd.append('lookahead',       document.getElementById('mw-la-'+inputId)?.value     || '35');
    fd.append('gop_secs',        document.getElementById('mw-gop-'+inputId)?.value    || '1.5');
    fd.append('copy_threads',    document.getElementById('mw-copyth-'+inputId)?.value || '0');
    fd.append('video_buffers',   document.getElementById('mw-vbuf-'+inputId)?.value   || '16');
    fd.append('extra_hw_frames', document.getElementById('mw-ehf-'+inputId)?.value    || '32');
    fd.append('p010',            document.getElementById('mw-p010-'+inputId)?.checked  ? '1' : '0');
    fd.append('no_audio',        document.getElementById('mw-noa-'+inputId)?.checked   ? '1' : '0');
    fd.append('vaapi_device',    document.getElementById('mw-dev-'+inputId)?.value     || '');
    fd.append('edid_path',       document.getElementById('mw-edid-path-'+inputId)?.value.trim() || '');
    fd.append('edid_refresh',    document.getElementById('mw-edid-refresh-chk-'+inputId)?.checked ? '1' : '0');
    try {{
      const res  = await fetch('/input/'+inputId+'/set_magewell_cfg', {{method:'POST', body:fd}});
      const data = await res.json();
      if (data.ok) showToast(data.restarted ? 'Magewell config saved · restarting…' : 'Magewell config saved');
      else showToast(data.error || 'Failed to save', false);
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  // ── EDID management ──────────────────────────────────────────────────────────

  function _mwEdidStatus(inputId, msg, cls='') {{
    const el = document.getElementById('mw-edid-status-'+inputId);
    if (!el) return;
    el.textContent = msg;
    el.className = 'mw-edid-status' + (cls ? ' '+cls : '');
  }}

  function _mwEdidConsole(inputId, text) {{
    const el = document.getElementById('mw-edid-console-'+inputId);
    if (!el) return;
    el.style.display = '';
    el.textContent = text;
    el.scrollTop = el.scrollHeight;
  }}

  // When the dropdown changes, copy the path into the text field
  function mwEdidSelChange(inputId) {{
    const sel  = document.getElementById('mw-edid-sel-'+inputId);
    const path = document.getElementById('mw-edid-path-'+inputId);
    if (sel && path && sel.value) path.value = sel.value;
  }}

  // Scan for .bin files and populate the dropdown
  async function mwLoadEdidList(inputId) {{
    const btn = document.getElementById('mw-edid-refresh-'+inputId);
    const sel = document.getElementById('mw-edid-sel-'+inputId);
    if (btn) {{ btn.disabled = true; btn.textContent = '…'; }}
    _mwEdidStatus(inputId, 'Scanning…');
    try {{
      const res  = await fetch('/input/'+inputId+'/edid/list');
      const data = await res.json();
      if (!data.ok) {{
        _mwEdidStatus(inputId, data.error || 'Scan failed', 'err');
        return;
      }}
      const saved = data.saved_edid || '';
      sel.innerHTML = '<option value="">— select a .bin file —</option>';
      for (const f of data.bins) {{
        const opt = document.createElement('option');
        opt.value = f.path;
        opt.textContent = f.name;
        if (f.path === saved) opt.selected = true;
        sel.appendChild(opt);
      }}
      if (data.bins.length === 0) {{
        _mwEdidStatus(inputId, 'No .bin files found — paste a path manually', 'err');
      }} else {{
        _mwEdidStatus(inputId, data.bins.length + ' file' + (data.bins.length !== 1 ? 's' : '') + ' found', 'ok');
      }}
    }} catch(e) {{
      _mwEdidStatus(inputId, 'Network error', 'err');
    }} finally {{
      if (btn) {{ btn.disabled = false; btn.textContent = '↺'; }}
    }}
  }}

  // Read current EDID from hardware and show decoded output
  async function mwReadEdid(inputId) {{
    _mwEdidStatus(inputId, 'Reading EDID from hardware…');
    _mwEdidConsole(inputId, '');
    try {{
      const res  = await fetch('/input/'+inputId+'/edid/read');
      const data = await res.json();
      if (!data.ok) {{
        _mwEdidStatus(inputId, data.error || 'Read failed', 'err');
        _mwEdidConsole(inputId, data.error || '');
        return;
      }}
      _mwEdidStatus(inputId, 'Read OK · ' + data.size + ' bytes', 'ok');
      _mwEdidConsole(inputId, data.decoded);
    }} catch(e) {{
      _mwEdidStatus(inputId, 'Network error', 'err');
    }}
  }}

  // Write selected/typed EDID path to hardware
  async function mwWriteEdid(inputId) {{
    const path = document.getElementById('mw-edid-path-'+inputId)?.value.trim();
    if (!path) {{
      _mwEdidStatus(inputId, 'Select or enter a .bin path first', 'err');
      return;
    }}
    _mwEdidStatus(inputId, 'Writing EDID…');
    const fd = new FormData();
    fd.append('edid_path', path);
    try {{
      const res  = await fetch('/input/'+inputId+'/edid/write', {{method:'POST', body:fd}});
      const data = await res.json();
      if (!data.ok) {{
        _mwEdidStatus(inputId, data.error || 'Write failed', 'err');
        _mwEdidConsole(inputId, data.error || '');
        return;
      }}
      _mwEdidStatus(inputId, '✓ EDID written successfully', 'ok');
      if (data.output) _mwEdidConsole(inputId, data.output);
      // Also persist the path into the config field so Save picks it up
      const pathEl = document.getElementById('mw-edid-path-'+inputId);
      if (pathEl) pathEl.value = path;
    }} catch(e) {{
      _mwEdidStatus(inputId, 'Network error', 'err');
    }}
  }}

  // Restore saved theme on load
  (function() {{
    try {{
      const saved = localStorage.getItem('bh-theme');
      if (saved && THEMES.includes(saved)) applyTheme(saved);
    }} catch(e) {{}}
  }})();

  // ── System Telemetry ─────────────────────────────────────────────────────────

  function _tempColor(t) {{
    if (t === null || t === undefined) return 'temp-cool';
    if (t >= 85) return 'temp-hot';
    if (t >= 65) return 'temp-warm';
    return 'temp-cool';
  }}

  function _tempBarColor(t) {{
    if (t === null || t === undefined) return '#4dc8a0';
    if (t >= 85) return '#f06060';
    if (t >= 65) return '#f0c040';
    return '#4dc8a0';
  }}

  function _tempBarWidth(t) {{
    if (t === null || t === undefined) return 0;
    return Math.min(100, Math.max(0, (t / 100) * 100));
  }}

  function _updateTempTile(elId, barId, temp) {{
    const el  = document.getElementById(elId);
    const bar = document.getElementById(barId);
    if (!el) return;
    if (temp === null || temp === undefined) {{ el.textContent = '—'; return; }}
    el.textContent = temp.toFixed(1) + '°C';
    el.className = 'telemetry-value ' + _tempColor(temp);
    if (bar) {{
      bar.style.width = _tempBarWidth(temp) + '%';
      bar.style.background = _tempBarColor(temp);
    }}
  }}

  async function _pollTelemetry() {{
    try {{
      const res  = await fetch('/telemetry');
      const data = await res.json();
      if (!data.ok) return;

      // Fixed temp tiles
      _updateTempTile('tel-cpu',  'tel-cpu-bar',  data.temps?.cpu?.value);
      _updateTempTile('tel-igpu', 'tel-igpu-bar', data.temps?.igpu?.value);
      _updateTempTile('tel-arc',  'tel-arc-bar',  data.temps?.arc?.value);

      // Magewell input temps (dynamic)
      const mwContainer = document.getElementById('tel-mw-tiles');
      if (mwContainer) {{
        const mwEntries = Object.entries(data.temps || {{}})
          .filter(([k]) => k.startsWith('mw_'));
        mwContainer.innerHTML = mwEntries.map(([k, v]) => `
          <div class="telemetry-tile">
            <div class="telemetry-label">${{v.label}}</div>
            <div class="telemetry-value ${{_tempColor(v.value)}}">${{v.value?.toFixed(1)+'°C' || '—'}}</div>
            <div class="telemetry-bar-track">
              <div class="telemetry-bar-fill"
                style="width:${{_tempBarWidth(v.value)}}%;background:${{_tempBarColor(v.value)}}"></div>
            </div>
          </div>`).join('');
      }}

      // Fan RPMs
      const fanContainer = document.getElementById('telemetry-fans');
      if (fanContainer && data.fans) {{
        fanContainer.innerHTML = data.fans.map(f => {{
          const active = f.rpm > 100;
          const pct    = Math.min(100, (f.rpm / 3000) * 100);
          return `
          <div class="telemetry-tile">
            <div class="telemetry-label">${{f.label}}</div>
            <div class="telemetry-value ${{active?'fan-active':'fan-off'}}">${{f.rpm}}</div>
            <div class="telemetry-sub">RPM</div>
            <div class="telemetry-bar-track">
              <div class="telemetry-bar-fill"
                style="width:${{pct}}%;background:${{active?'var(--accent)':'var(--border)'}}"></div>
            </div>
          </div>`;
        }}).join('');
      }}
    }} catch(e) {{}}
  }}

  // Poll telemetry every 5 seconds
  _pollTelemetry();
  setInterval(_pollTelemetry, 5000);

  let _ccConfig = {{}};
  let _ccModeList = [];

  // Load CC config from fan_config on page load
  (async function _ccInit() {{
    try {{
      const res  = await fetch('/fans/config');
      const data = await res.json();
      if (!data.ok) return;
      _ccConfig = data.config?.coolercontrol || {{}};
      const chk = document.getElementById('cc-enabled-chk');
      const tok = document.getElementById('cc-token-input');
      if (chk) chk.checked = !!_ccConfig.enabled;
      if (tok) tok.value   = _ccConfig.token || '';
      if (_ccConfig.token) await ccFetchModes(true);  // silent fetch on load
    }} catch(e) {{}}
  }})();

  function ccToggleEnabled(val) {{
    _ccConfig.enabled = val;
  }}

  function ccSetField(key, val) {{
    _ccConfig[key] = val;
  }}

  function ccSetMode(key, uid) {{
    _ccConfig[key] = uid;
  }}

  async function ccFetchModes(silent) {{
    const status = document.getElementById('cc-status');
    if (!silent && status) status.textContent = 'Fetching modes…';
    // Ensure latest token is captured
    const tokEl = document.getElementById('cc-token-input');
    if (tokEl) _ccConfig.token = tokEl.value.trim();
    // Save token+url to backend first so the proxy can use it
    await _ccSaveToBackend(true);
    try {{
      const res  = await fetch('/coolercontrol/modes');
      const data = await res.json();
      if (data.ok && data.modes) {{
        _ccModeList = data.modes;
        _ccPopulateSelects();
        document.getElementById('cc-mode-rows').style.display = '';
        if (!silent && status) status.textContent = `${{data.modes.length}} mode(s) loaded`;
      }} else {{
        if (!silent && status) status.textContent = data.error || 'Failed — check token and URL';
      }}
    }} catch(e) {{
      if (!silent && status) status.textContent = 'Network error';
    }}
  }}

  function _ccPopulateSelects() {{
    for (const key of ['default','streaming','recording','transcoding']) {{
      const sel = document.getElementById(`cc-sel-${{key}}`);
      if (!sel) continue;
      const saved = _ccConfig[`mode_${{key}}`] || '';
      sel.innerHTML = '<option value="">— none —</option>' +
        _ccModeList.map(m =>
          `<option value="${{m.uid}}" ${{m.uid===saved?'selected':''}}>${{m.name}}</option>`
        ).join('');
    }}
  }}

  async function ccActivate(key) {{
    const uid = _ccConfig[key] || document.getElementById(`cc-sel-${{key.replace('mode_','cc-sel-')}}`)?.value;
    if (!uid) {{ showToast('No mode selected', false); return; }}
    const status = document.getElementById('cc-status');
    if (status) status.textContent = `Activating ${{key.replace('mode_','')}} mode…`;
    try {{
      const res  = await fetch(`/coolercontrol/activate/${{uid}}`, {{method:'POST'}});
      const data = await res.json();
      if (data.ok) {{
        if (status) status.textContent = '✓ Mode activated';
        showToast('CoolerControl mode activated');
      }} else {{
        if (status) status.textContent = data.error || 'Activation failed';
        showToast(data.error || 'Failed', false);
      }}
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  async function _ccSaveToBackend(silent) {{
    try {{
      const res  = await fetch('/fans/cc-config', {{
        method:  'POST',
        headers: {{'Content-Type': 'application/json'}},
        body:    JSON.stringify(_ccConfig),
      }});
      const data = await res.json();
      return data.ok;
    }} catch(e) {{ return false; }}
  }}

  async function ccSave() {{
    // Read current select values into config
    for (const key of ['default','streaming','recording','transcoding']) {{
      const sel = document.getElementById(`cc-sel-${{key}}`);
      if (sel) _ccConfig[`mode_${{key}}`] = sel.value;
    }}
    const chk = document.getElementById('cc-enabled-chk');
    if (chk) _ccConfig.enabled = chk.checked;
    const ok = await _ccSaveToBackend(false);
    const status = document.getElementById('cc-status');
    if (ok) {{
      if (status) status.textContent = 'Saved ✓';
      showToast('CoolerControl config saved');
    }} else {{
      showToast('Save failed', false);
    }}
  }}

  // Update CC profile badge from SSE
  function _ccUpdateBadge(profile) {{
    const badge = document.getElementById('cc-profile-badge');
    if (badge && profile) badge.textContent = profile;
  }}

</script>
</head>
<body>

<!-- Topbar -->
<div class="topbar">
  <div class="logo">Broadcast<span>Hub</span></div>
  <div class="topbar-right">
    <div class="sse-dot" id="sse-indicator" style="opacity:.3"><div class="dot"></div> Live</div>
    <button class="theme-toggle" id="theme-toggle" onclick="cycleTheme()">● Neon Ops</button>
    <a href="/mobile" class="mobile-link">Mobile ↗</a>
    <a href="/multiview" class="mobile-link" title="Quad multiviewer">Multiview ↗</a>
    <a href="/channels" class="mobile-link" title="Channel tuning &amp; device mapping">Channels ↗</a>
    <a href="/logs" class="mobile-link" title="Real-time log viewer">Log ↗</a>
    <a href="/settings/password" class="mobile-link" title="Change password">⚙ Password</a>
    <a href="/logout" class="mobile-link" title="Sign out">Sign Out</a>
  </div>
</div>

<!-- Driver missing banner (shown by JS when SSE reports driver_missing=true) -->
<div class="driver-banner" id="driver-banner">
  <div class="driver-banner-icon">⚠</div>
  <div class="driver-banner-text">
    Magewell ProCapture driver not loaded
    <span>— this usually happens after a kernel update.</span>
  </div>
  <button class="btn-driver-fix" onclick="openDriverModal()">Reinstall Driver</button>
</div>

<div class="page">

  <!-- Live Streams -->
  <div class="section-header">
    <div class="section-lbl">Live Streams</div>
    <button class="btn-manage" onclick="openManage()">&#9776; Manage Inputs</button>
  </div>
  <div class="cards" id="cards-container">
    {cards_html}
  </div>

  {hls_section}
  {rec_section}
  {sched_section}

  <!-- Mobile / HLS Gateway -->
  <div class="section-header">
    <div class="section-lbl">Mobile / HLS Gateway</div>
  </div>
  <div class="gateway-card">
    <div style="flex:1">
      <div class="gateway-url">{base_url}/mobile</div>
      <div class="gateway-sub">Start HLS on any input to broadcast to mobile.</div>
    </div>
    <a href="/mobile" target="_blank" class="btn btn-hls" style="flex-shrink:0">Open Mobile ↗</a>
  </div>

  <!-- Record & Pipeline -->
  <div class="section-header">
    <div class="section-lbl">Record &amp; Pipeline</div>
  </div>
  <div class="pipeline-card">
    <select class="pipeline-select" id="rs-input-select">
      {input_options}
    </select>
    <button class="btn btn-record-open" onclick="openRecord(document.getElementById('rs-input-select').value)">&#9210; Record</button>
    <button class="btn btn-schedule-open" onclick="openSchedule(document.getElementById('rs-input-select').value)">&#128337; Schedule</button>
  </div>

  <!-- CoolerControl Integration -->
  <div class="section-header">
    <div class="section-lbl">&#9965; Fan Control <span id="cc-profile-badge" class="vchip" style="font-size:9px;margin-left:6px"></span></div>
  </div>
  <div class="gateway-card" id="cc-section">
    <div style="flex:1">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text);white-space:nowrap">
          <input type="checkbox" id="cc-enabled-chk" onchange="ccToggleEnabled(this.checked)">
          CoolerControl
        </label>
        <input class="dl-input" id="cc-token-input" type="password"
          placeholder="Bearer token (cc_…)" style="flex:1;min-width:180px;font-size:11px"
          onchange="ccSetField('token',this.value)">
        <button class="btn q-btn" style="font-size:10px;padding:5px 10px"
          onclick="ccFetchModes()">↺ Modes</button>
      </div>
      <div id="cc-mode-rows" style="display:none;margin-top:10px">
        <div style="display:grid;grid-template-columns:90px 1fr 90px 1fr;gap:6px;align-items:center;margin-bottom:6px">
          <span style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em">Default</span>
          <select class="dl-input" id="cc-sel-default" style="font-size:11px"
            onchange="ccSetMode('mode_default',this.value)">
            <option value="">— none —</option>
          </select>
          <span style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em">Streaming</span>
          <select class="dl-input" id="cc-sel-streaming" style="font-size:11px"
            onchange="ccSetMode('mode_streaming',this.value)">
            <option value="">— none —</option>
          </select>
          <span style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em">Recording</span>
          <select class="dl-input" id="cc-sel-recording" style="font-size:11px"
            onchange="ccSetMode('mode_recording',this.value)">
            <option value="">— none —</option>
          </select>
          <span style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em">Transcoding</span>
          <select class="dl-input" id="cc-sel-transcoding" style="font-size:11px"
            onchange="ccSetMode('mode_transcoding',this.value)">
            <option value="">— none —</option>
          </select>
        </div>
        <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
          <button class="btn q-btn" style="font-size:10px;padding:4px 10px"
            onclick="ccActivate('mode_default')">▶ Default</button>
          <button class="btn q-btn" style="font-size:10px;padding:4px 10px"
            onclick="ccActivate('mode_streaming')">▶ Streaming</button>
          <button class="btn q-btn" style="font-size:10px;padding:4px 10px"
            onclick="ccActivate('mode_recording')">▶ Recording</button>
          <button class="btn q-btn" style="font-size:10px;padding:4px 10px"
            onclick="ccActivate('mode_transcoding')">▶ Transcoding</button>
          <button class="btn q-btn" style="font-size:10px;padding:4px 10px;margin-left:auto"
            onclick="ccSave()">💾 Save</button>
        </div>
      </div>
      <div id="cc-status" style="font-size:10px;color:var(--muted);margin-top:6px"></div>
    </div>
  </div>

  <!-- System Telemetry -->
  <div class="section-header">
    <div class="section-lbl">&#127777; System Telemetry</div>
  </div>
  <div class="telemetry-card">
    <div style="margin-bottom:8px">
      <span style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)">Temperatures</span>
    </div>
    <div class="telemetry-grid" id="telemetry-temps">
      <div class="telemetry-tile">
        <div class="telemetry-label">CPU</div>
        <div class="telemetry-value temp-cool" id="tel-cpu">—</div>
        <div class="telemetry-bar-track"><div class="telemetry-bar-fill" id="tel-cpu-bar" style="width:0%;background:#4dc8a0"></div></div>
      </div>
      <div class="telemetry-tile">
        <div class="telemetry-label">iGPU</div>
        <div class="telemetry-value temp-cool" id="tel-igpu">—</div>
        <div class="telemetry-bar-track"><div class="telemetry-bar-fill" id="tel-igpu-bar" style="width:0%;background:#4dc8a0"></div></div>
      </div>
      <div class="telemetry-tile">
        <div class="telemetry-label">Arc A310</div>
        <div class="telemetry-value temp-cool" id="tel-arc">—</div>
        <div class="telemetry-bar-track"><div class="telemetry-bar-fill" id="tel-arc-bar" style="width:0%;background:#4dc8a0"></div></div>
      </div>
      <div id="tel-mw-tiles"></div>
    </div>
    <div style="margin-top:12px;margin-bottom:8px">
      <span style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)">Fans</span>
    </div>
    <div class="telemetry-grid" id="telemetry-fans"></div>
  </div>

</div><!-- /page -->

<!-- Toast -->
<div id="hub-toast"></div>

<!-- Confirm Q restart -->
<div class="overlay" id="confirm-overlay">
  <div class="confirm-box">
    <div style="font-weight:900;font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:#e8ff47;margin-bottom:12px">Signal Reset</div>
    <p id="confirm-msg" style="font-size:13px;color:#666;margin-bottom:20px;line-height:1.5"></p>
    <div style="display:flex;gap:8px;justify-content:center">
      <button class="btn btn-abort" onclick="cancelSetQ()">Cancel</button>
      <button class="btn btn-hls" onclick="confirmSetQ()">Apply &amp; Restart</button>
    </div>
  </div>
</div>

<!-- Manage inputs -->
<div class="overlay" id="manage-overlay">
  <div class="modal" style="width:380px">
    <div class="modal-hdr">
      <div class="modal-title">Manage Feeds</div>
      <div style="display:flex;align-items:center;gap:8px">
        <button id="manage-refresh-btn" class="btn btn-manage" style="font-size:10px;padding:5px 11px" onclick="refreshManage()">↺ Refresh</button>
        <button class="modal-close" onclick="closeManage()">&times;</button>
      </div>
    </div>
    <div class="manage-list" id="manage-list"></div>
    <div class="manage-footer" style="flex-direction:column;gap:8px">
      <button id="manage-show-hidden-btn" class="btn btn-manage"
        style="width:100%;font-size:10px;padding:5px 11px;display:none"
        onclick="toggleManageHidden()">▼ Show hidden</button>
      <div style="display:flex;gap:8px;width:100%">
        <button class="btn btn-abort" onclick="closeManage()">Abort</button>
        <button class="btn btn-commit" onclick="applyManage()">Commit</button>
      </div>
    </div>
  </div>
</div>

<!-- Preview modal -->
<div class="overlay" id="preview-overlay">
  <div class="modal" style="width:820px">
    <div class="modal-hdr">
      <div class="modal-title" id="preview-title">Preview</div>
      <button class="modal-close" onclick="closePreview()">&times;</button>
    </div>
    <video id="preview-video" class="modal-video" autoplay controls></video>
  </div>
</div>

<!-- Record modal -->
<div class="overlay" id="record-overlay">
  <div class="modal" style="width:900px">
    <div class="modal-hdr">
      <div class="modal-title" id="record-title">Record</div>
      <button class="modal-close" onclick="closeRecord()">&times;</button>
    </div>
    <div style="display:flex;gap:16px">
      <video id="record-video" class="modal-video" autoplay controls style="flex:1"></video>
      <div style="width:200px;flex-shrink:0">
        <div class="adb-panel">
          <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
            <select id="remote-type-sel" class="dl-input" style="flex:1;font-size:11px"
              onchange="onRemoteTypeChange()">
              <option value="none">No Remote</option>
              <option value="adb">ADB (Android TV)</option>
              <option value="roku">Roku (ECP)</option>
            </select>
          </div>
          <div id="remote-ip-row" style="display:none;margin-bottom:6px">
            <input id="adb-ip-field" type="text" class="adb-ip-input"
              placeholder="Device IP" style="width:100%;box-sizing:border-box">
            <button class="adb-save-btn" style="margin-top:4px;width:100%"
              onclick="saveRemote(_activeAdbInputId)">Link Device</button>
          </div>
          <div class="adb-remote" id="remote-buttons" style="display:none">
            <button class="adb-btn adb-dpad" onclick="remoteKey('up')">▲</button>
            <div class="adb-row">
              <button class="adb-btn adb-dpad" onclick="remoteKey('left')">◀</button>
              <button class="adb-btn adb-dpad adb-center" onclick="remoteKey('enter')">OK</button>
              <button class="adb-btn adb-dpad" onclick="remoteKey('right')">▶</button>
            </div>
            <button class="adb-btn adb-dpad" onclick="remoteKey('down')">▼</button>
            <div class="adb-row" style="margin-top:8px">
              <button class="adb-btn adb-action adb-home" onclick="remoteKey('home')">Home</button>
              <button class="adb-btn adb-action adb-back" onclick="remoteKey('back')">Back</button>
            </div>
          </div>
          <div id="adb-status" class="adb-status"></div>
        </div>
      </div>
    </div>
    <form id="record-form" action="/record/start" method="post" class="form-grid">
      <input type="hidden" name="input_id" id="record-input-id">
      <input name="programme" id="rec-programme" type="text" placeholder="Programme name" required>
      <div style="display:flex;gap:5px;align-items:stretch" class="span2">
        <input name="rec_dir" id="rec-dir" type="text" placeholder="/recordings" style="flex:1;background:#111;border:1px solid #2a2a2a;color:var(--text);font-family:'Inter',sans-serif;font-size:13px;padding:10px 12px;border-radius:4px;outline:none">
        <button type="button" id="save-dir-btn" style="font-family:'Inter',sans-serif;font-weight:900;font-size:9px;text-transform:uppercase;letter-spacing:.1em;padding:0 12px;border-radius:4px;background:rgba(74,158,255,.08);border:1px solid rgba(74,158,255,.2);color:var(--blue);cursor:pointer;white-space:nowrap" onclick="saveDefaultDir()" title="Save this as your default recording directory">Save Default</button>
      </div>
      <select name="fmt" id="rec-fmt">{format_options}</select>
      <div class="dur-hms">
        <input id="rec-dur-h" type="number" value="0" class="dur-field">
        <span class="dur-sep">h</span>
        <input id="rec-dur-m" type="number" value="30" class="dur-field">
        <span class="dur-sep">m</span>
        <input id="rec-dur-s" type="number" value="0" class="dur-field">
        <span class="dur-sep">s</span>
      </div>
      <input type="hidden" name="duration" id="rec-dur-hidden" value="1800">
      <label class="adb-home-row span2" id="rec-adb-home-row">
        <input type="checkbox" name="adb_home" id="rec-adb-home" value="1">
        <span>Return device to Home screen 60s after recording ends</span>
      </label>
      <button type="button" class="btn-engage span2" onclick="submitRecordForm()">Engage Capture</button>
    </form>
  </div>
</div>

<!-- Schedule modal -->
<div class="overlay" id="schedule-overlay">
  <div class="modal" style="width:820px">
    <div class="modal-hdr">
      <div class="modal-title" id="schedule-title">Scheduler</div>
      <button class="modal-close" onclick="closeSchedule()">&times;</button>
    </div>
    <video id="schedule-video" class="modal-video" autoplay controls style="margin-bottom:16px"></video>
    <form id="schedule-form" action="/schedule/add" method="post" class="form-grid">
      <input type="hidden" name="input_id" id="schedule-input-id">
      <input type="hidden" name="duration" id="sched-dur-hidden">
      <input name="label" type="text" placeholder="Job Title">
      <input name="output_path" type="text" placeholder="/recordings/vid.mp4" required>
      <input name="start_time" type="datetime-local" required>
      <div class="dur-hms">
        <input id="sched-dur-h" type="number" value="1" class="dur-field">
        <span class="dur-sep">h</span>
        <input id="sched-dur-m" type="number" value="0" class="dur-field">
        <span class="dur-sep">m</span>
      </div>
      <button type="button" class="btn-enqueue span2" onclick="submitScheduleForm()">Enqueue Event</button>
    </form>
  </div>
</div>
<!-- Folder browser modal -->
<div class="overlay" id="browser-overlay" style="z-index:1100">
  <div class="modal" style="width:500px">
    <div class="modal-hdr">
      <div class="modal-title">📁 Browse for Installer</div>
      <button class="modal-close" onclick="closeFolderBrowser()">&times;</button>
    </div>
    <p style="font-size:11px;color:var(--muted);margin-bottom:10px;line-height:1.5">
      Navigate to the Magewell installer folder (the one containing
      <code style="color:#ff8c00;font-family:'Courier New',monospace">install.sh</code>).
      Folders that contain it are highlighted in orange.
    </p>
    <div class="browser-toolbar">
      <button class="btn-browser-up" id="browser-up-btn" onclick="browseUp()">↑ Up</button>
      <div class="browser-crumb" id="browser-crumb">/</div>
    </div>
    <div class="browser-list" id="browser-list">
      <div style="padding:20px;text-align:center;color:var(--muted);font-size:12px">Loading…</div>
    </div>
    <button class="browser-select-btn" id="browser-select-btn"
            onclick="selectBrowserPath()" disabled>
      Select This Folder
    </button>
  </div>
</div>

<!-- Driver reinstall modal -->
<div class="overlay" id="driver-overlay">
  <div class="modal" style="width:560px">
    <div class="modal-hdr">
      <div class="modal-title">⚠ Reinstall Magewell Driver</div>
      <button class="modal-close" onclick="closeDriverModal()">&times;</button>
    </div>
    <p style="font-size:12px;color:var(--muted);line-height:1.6;margin-bottom:14px">
      The ProCapture kernel module is not loaded. This happens after a kernel update.
      Enter the path to your Magewell installer directory (the folder containing
      <code style="color:#ff8c00;font-family:'Courier New',monospace">install.sh</code>)
      and click <strong>Run Installer</strong>.
    </p>
    <div class="driver-path-row">
      <input class="driver-path-input" id="driver-path-input"
             placeholder="/home/christophe/src/Magewell/ProCaptureForLinux_1.3.4429"
             type="text">
      <button class="btn-driver-save" onclick="openFolderBrowser()">Browse…</button>
      <button class="btn-driver-save" onclick="saveDriverPath()">Save Path</button>
    </div>
    <div class="driver-console" id="driver-console">
      <p class="dc-line dc-info">Ready. Press "Run Installer" to begin.</p>
    </div>
    <button class="btn-driver-run" id="driver-run-btn" onclick="runDriverInstall()">
      ▶ Run Installer
    </button>
  </div>
</div>

</body></html>"""
