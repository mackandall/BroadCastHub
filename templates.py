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

    Magewell keys are ``"board-channel"`` (e.g. ``"0-1"``).
    Decklink keys are ``"dl-N"`` (e.g. ``"dl-0"``).
    """
    if key.startswith("dl-"):
        idx = key.split("-", 1)[1]
        return f"Decklink · Device {idx}"
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
        "<!DOCTYPE html><html><head>"
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        "<title>Broadcast Hub</title>"
        '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&display=swap" rel="stylesheet">'
        '<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>'
        "<style>"
        "*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}"
        "body{background:#080808;color:#f0f0f0;font-family:'Inter',sans-serif;padding-bottom:30px;max-width:480px;margin:0 auto}"
        ".topbar{padding:16px 16px 12px;border-bottom:1px solid #1a1a1a;position:sticky;top:0;background:rgba(8,8,8,.96);backdrop-filter:blur(14px);display:flex;align-items:center;justify-content:space-between;z-index:50}"
        ".logo{font-family:'Inter',sans-serif;font-weight:900;font-style:italic;font-size:21px;text-transform:uppercase}"
        ".logo span{color:#e8ff47}"
        ".desktop-link{color:#444;font-size:11px;text-decoration:none;text-transform:uppercase;letter-spacing:.1em}"
        ".section-lbl{padding:16px 14px 8px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.15em;color:#3a3a3a}"
        ".cards{padding:0 10px;display:flex;flex-direction:column;gap:12px}"
        ".card{background:#0f0f0f;border:1px solid #1e1e1e;border-radius:4px;overflow:hidden;transition:border-color .2s}"
        ".card.hls-on{border-color:rgba(232,255,71,.25)}"
        ".card-media{position:relative;width:100%;aspect-ratio:16/9;background:#060606;overflow:hidden}"
        ".card-media video{width:100%;height:100%;object-fit:contain;display:block;background:#000}"
        ".card-placeholder{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:6px}"
        ".card-placeholder .big-num{font-family:'Inter',sans-serif;font-weight:900;font-size:36px;color:#1c1c1c;line-height:1}"
        ".card-placeholder .no-sig{font-family:'Inter',sans-serif;font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:.15em;color:#2a2a2a}"
        ".status-badge{position:absolute;top:10px;left:10px;z-index:5;font-family:'Inter',sans-serif;font-weight:900;font-size:11px;letter-spacing:.1em;text-transform:uppercase;display:flex;align-items:center;gap:5px;padding:3px 9px;border-radius:4px}"
        ".status-badge.live{background:rgba(255,59,59,.15);border:1px solid rgba(255,59,59,.35);color:#ff3b3b}"
        ".status-badge.offline{color:#333}"
        ".blink{width:6px;height:6px;border-radius:50%;background:#ff3b3b;animation:blink 1.4s ease-in-out infinite}"
        "@keyframes blink{0%,100%{opacity:1}50%{opacity:.15}}"
        ".hls-badge{position:absolute;top:10px;right:10px;z-index:5;background:rgba(232,255,71,.1);border:1px solid rgba(232,255,71,.3);color:#e8ff47;font-family:'Inter',sans-serif;font-weight:900;font-size:10px;letter-spacing:.1em;text-transform:uppercase;padding:3px 9px;border-radius:4px;display:flex;align-items:center;gap:4px}"
        ".hls-dot{width:5px;height:5px;border-radius:50%;background:#e8ff47;animation:blink 1.4s ease-in-out infinite}"
        ".loading-cover{position:absolute;inset:0;z-index:4;background:rgba(0,0,0,.7);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;transition:opacity .4s}"
        ".loading-cover.gone{opacity:0;pointer-events:none}"
        ".spinner{width:30px;height:30px;border:2px solid #1e1e1e;border-top-color:#e8ff47;border-radius:50%;animation:spin .7s linear infinite}"
        "@keyframes spin{to{transform:rotate(360deg)}}"
        ".spinner-lbl{font-family:'Inter',sans-serif;font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:#555}"
        ".card-foot{padding:12px 14px;display:flex;align-items:center;justify-content:space-between;gap:10px;border-top:1px solid #141414}"
        ".card-title{font-family:'Inter',sans-serif;font-weight:900;font-size:14px;text-transform:uppercase;letter-spacing:.03em;color:#e8e8e8}"
        ".card-sub{font-size:11px;color:#444;margin-top:2px;font-weight:500}"
        ".btn-row{display:flex;gap:8px;flex-shrink:0}"
        ".btn{font-family:'Inter',sans-serif;font-weight:900;font-size:13px;text-transform:uppercase;letter-spacing:.07em;padding:9px 20px;border-radius:4px;border:none;cursor:pointer;transition:opacity .15s,background .15s;white-space:nowrap}"
        ".btn:active{opacity:.7}"
        ".btn-play{background:#e8ff47;color:#000}"
        ".btn-stop{background:rgba(180,40,40,.1);border:1px solid rgba(180,40,40,.3)!important;color:#cc4040}"
        ".btn-stop-dim{opacity:.22;cursor:not-allowed;pointer-events:none}"
        "</style></head><body>"
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
        driver_badge = (
            '<div class="thumb-badge driver-badge-dl">DL</div>'
            if driver == "decklink" else
            '<div class="thumb-badge driver-badge-mw">MW</div>'
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

        # Decklink config panel (collapsed by default)
        if driver == "decklink":
            dl_cfg       = cfg.get(i, {})
            saved_acodec = dl_cfg.get("audio_codec",    "aac")
            saved_layout = dl_cfg.get("channel_layout", "stereo")
            saved_lfe    = dl_cfg.get("fix_lfe_swap",   False)
            # Show LFE fix only for surround layouts
            lfe_display  = "" if saved_layout in ("5.1", "7.1", "8ch") else "display:none"

            dl_panel = f"""<div class="dl-panel" id="dlpanel-{i}">
              <div class="dl-panel-hdr" onclick="toggleDlPanel('{i}')">
                &#9881; Decklink Config <span class="vchip-caret" id="dlcaret-{i}">&#9660;</span>
              </div>
              <div class="dl-panel-body" id="dlbody-{i}" style="display:none">

                <div class="dl-section-lbl">Video</div>
                <div class="dl-fmt-row">
                  <label class="dl-lbl">Format</label>
                  <select class="dl-input dl-fmt-select" id="dl-fmt-{i}">
                    <option value="{dl_cfg.get('format_code','hp50')}">{dl_cfg.get('format_code','hp50')} (saved)</option>
                  </select>
                  <button class="dl-refresh-btn" id="dl-fmtbtn-{i}" onclick="loadDlFormats('{i}')" title="Query device for available formats">&#8635;</button>
                </div>
                <div class="dl-fmt-status" id="dl-fmtstatus-{i}"></div>
                <div class="dl-grid">
                  <label class="dl-lbl">Mode</label>
                  <select class="dl-input" id="dl-qmode-{i}" onchange="dlToggleMode('{i}')">
                    <option value="cqp"{"  selected" if dl_cfg.get('quality_mode','cqp')=='cqp' else ''}>CQP (Q value)</option>
                    <option value="cbr"{"  selected" if dl_cfg.get('quality_mode','cqp')=='cbr' else ''}>CBR (bitrate)</option>
                  </select>
                  <label class="dl-lbl" id="dl-vblbl-{i}">Video BR</label>
                  <input class="dl-input" id="dl-vbr-{i}" type="text" value="{dl_cfg.get('video_bitrate','50M')}" placeholder="50M">
                  <label class="dl-lbl">VFilter</label>
                  <input class="dl-input" id="dl-vf-{i}" type="text" value="{dl_cfg.get('video_filter','yadif=1,scale=1920:1080')}" placeholder="yadif=1,scale=1920:1080">
                  <label class="dl-lbl">GOP</label>
                  <input class="dl-input" id="dl-gop-{i}" type="number" value="{dl_cfg.get('gop',90)}" placeholder="90">
                </div>

                <div class="dl-section-lbl" style="margin-top:10px">Audio</div>
                <div class="dl-grid">
                  <label class="dl-lbl">Codec</label>
                  <select class="dl-input" id="dl-acodec-{i}" onchange="dlUpdateAudioOptions('{i}')">
                    {_audio_codec_options(saved_acodec)}
                  </select>
                  <label class="dl-lbl">Layout</label>
                  <select class="dl-input" id="dl-layout-{i}" onchange="dlUpdateAudioOptions('{i}')">
                    {_ch_layout_options(saved_layout)}
                  </select>
                  <label class="dl-lbl">Audio BR</label>
                  <input class="dl-input" id="dl-abr-{i}" type="text" value="{dl_cfg.get('audio_bitrate','128k')}" placeholder="128k">
                </div>
                <label class="dl-lfe-row" id="dl-lfe-row-{i}" style="{lfe_display}">
                  <input type="checkbox" id="dl-lfe-{i}" {"checked" if saved_lfe else ""}>
                  <span>Fix BMD LFE/Center channel swap <span class="dl-lfe-hint">(Intensity cards only)</span></span>
                </label>
                <div class="dl-audio-warn" id="dl-audio-warn-{i}" style="display:none"></div>

                <button class="btn q-btn" style="margin-top:10px;width:100%" onclick="submitDlCfg('{i}')">Apply &amp; Restart</button>
              </div>
            </div>"""
        else:
            dl_panel = ""

        # ── Magewell advanced config panel ──────────────────────────────────
        if driver == "magewell":
            mw_cfg      = cfg.get(i, {})
            saved_preset = mw_cfg.get("preset",       "")
            saved_la     = mw_cfg.get("lookahead",    35)
            saved_p010   = mw_cfg.get("p010",         False)
            saved_noa    = mw_cfg.get("no_audio",     False)
            saved_dev    = mw_cfg.get("vaapi_device", "")
            mw_panel = f"""<div class="dl-panel" id="mwpanel-{i}">
              <div class="dl-panel-hdr" onclick="toggleMwPanel('{i}')">
                &#9881; Magewell Config <span class="vchip-caret" id="mwcaret-{i}">&#9660;</span>
              </div>
              <div class="dl-panel-body" id="mwbody-{i}" style="display:none">
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
                  <label class="dl-lbl">Device</label>
                  <input class="dl-input" id="mw-dev-{i}" type="text"
                    value="{saved_dev}" placeholder="renderD128">
                </div>
                <label class="dl-lfe-row" style="margin-top:8px">
                  <input type="checkbox" id="mw-p010-{i}" {"checked" if saved_p010 else ""}>
                  <span>p010 — 10-bit encoding <span class="dl-lfe-hint">(better quality, HDR sources)</span></span>
                </label>
                <label class="dl-lfe-row">
                  <input type="checkbox" id="mw-noa-{i}" {"checked" if saved_noa else ""}>
                  <span>Video only — no audio</span>
                </label>
                <button class="btn q-btn" style="margin-top:10px;width:100%"
                  onclick="submitMwCfg('{i}')">Apply &amp; Restart</button>
              </div>
            </div>"""
        else:
            mw_panel = ""

        # Q control — hide for decklink CBR mode
        q_row_style = ""
        if driver == "decklink" and cfg.get(i, {}).get("quality_mode") == "cbr":
            q_row_style = " style='display:none'"

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
        {driver_badge}
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
        {dl_panel}
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
            "driver":       cfg.get(i, {}).get("driver", "magewell"),
            "encoder":      cfg.get(i, {}).get("encoder", _encoders[0]["value"] if _encoders else "h264_qsv"),
            "quality_mode": cfg.get(i, {}).get("quality_mode", "cqp"),
        }
        for i in current_input_ids
    }
    meta_js = json.dumps(meta_js_dict)
    encoders_js = json.dumps(_encoders)

    # IDs of Decklink inputs — used to fire format queries at page load
    decklink_ids = [i for i in current_input_ids if cfg.get(i, {}).get("driver") == "decklink"]
    decklink_ids_js = json.dumps(decklink_ids)

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

  /* ── Theme: dark (default) ── */
  :root, [data-theme="dark"] {{
    --bg:           #080808;
    --bg-topbar:    rgba(8,8,8,.96);
    --surface:      #0f0f0f;
    --border:       #1e1e1e;
    --border-hi:    #2a2a2a;
    --text:         #f0f0f0;
    --muted:        #444;
    --dim:          #2a2a2a;
    --accent:       #e8ff47;
    --accent-dim:   rgba(232,255,71,.1);
    --accent-bdr:   rgba(232,255,71,.25);
    --live:         #ff3b3b;
    --live-bg:      rgba(255,59,59,.12);
    --live-bdr:     rgba(255,59,59,.3);
    --blue:         #4a9eff;
    --orange:       #ff8c00;
    --purple:       #b464ff;
    --green:        #64dc50;
  }}

  /* ── Theme: mono (black & white) ── */
  [data-theme="mono"] {{
    --bg:           #000;
    --bg-topbar:    rgba(0,0,0,.97);
    --surface:      #0a0a0a;
    --border:       #222;
    --border-hi:    #333;
    --text:         #fff;
    --muted:        #555;
    --dim:          #333;
    --accent:       #fff;
    --accent-dim:   rgba(255,255,255,.07);
    --accent-bdr:   rgba(255,255,255,.2);
    --live:         #fff;
    --live-bg:      rgba(255,255,255,.07);
    --live-bdr:     rgba(255,255,255,.25);
    --blue:         #aaa;
    --orange:       #ccc;
    --purple:       #bbb;
    --green:        #ddd;
  }}

  /* ── Theme: light ── */
  [data-theme="light"] {{
    --bg:           #f2f2f0;
    --bg-topbar:    rgba(242,242,240,.97);
    --surface:      #fff;
    --border:       #e4e4e2;
    --border-hi:    #d0d0ce;
    --text:         #111;
    --muted:        #999;
    --dim:          #ccc;
    --accent:       #111;
    --accent-dim:   rgba(0,0,0,.05);
    --accent-bdr:   rgba(0,0,0,.15);
    --live:         #c00;
    --live-bg:      rgba(180,0,0,.07);
    --live-bdr:     rgba(180,0,0,.25);
    --blue:         #2563eb;
    --orange:       #d97706;
    --purple:       #7c3aed;
    --green:        #16a34a;
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
  .btn-preview     {{ background: rgba(100,220,80,.08); border: 1px solid rgba(100,220,80,.2) !important; color: var(--green); }}
  .btn-preview:hover {{ background: rgba(100,220,80,.16); }}
  .btn-vlc         {{ background: rgba(255,140,0,.08); border: 1px solid rgba(255,140,0,.2) !important; color: var(--orange); }}
  .btn-vlc:hover   {{ background: rgba(255,140,0,.16); }}
  .btn-hls         {{ background: var(--accent-dim); border: 1px solid var(--accent-bdr) !important; color: var(--accent); }}
  .btn-hls:hover   {{ background: rgba(232,255,71,.2); }}
  .btn-hls-stop    {{ background: rgba(232,255,71,.03); border: 1px solid #2a2a2a !important; color: #555; }}
  .btn-hls-stop:hover {{ color: #888; border-color: #444 !important; }}
  .btn-record-open {{ background: rgba(180,100,255,.08); border: 1px solid rgba(180,100,255,.2) !important; color: var(--purple); }}
  .btn-record-open:hover {{ background: rgba(180,100,255,.16); }}
  .btn-mobile-link {{ background: rgba(232,255,71,.04); border: 1px solid rgba(232,255,71,.15) !important; color: #666; font-size: 10px; padding: 6px 10px; }}
  .btn-stop-rec    {{ background: rgba(255,59,59,.08); border: 1px solid rgba(255,59,59,.2) !important; color: var(--live); }}
  .btn-stop-rec:hover {{ background: rgba(255,59,59,.16); }}
  .btn-schedule-open {{ background: rgba(232,255,71,.06); border: 1px solid rgba(232,255,71,.2) !important; color: var(--accent); }}
  .btn-schedule-open:hover {{ background: rgba(232,255,71,.14); }}
  .btn-manage {{
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 10px;
    text-transform: uppercase; letter-spacing: .1em;
    background: rgba(100,200,100,.06); border: 1px solid rgba(100,200,100,.2);
    color: rgba(100,200,100,.7); padding: 7px 13px; border-radius: 4px;
    cursor: pointer; transition: background .15s, color .15s;
  }}
  .btn-manage:hover {{ background: rgba(100,200,100,.14); color: #6fda6f; }}

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

  /* ── Light theme ── */
  [data-theme="light"] body              {{ background: #f2f2f0; }}
  [data-theme="light"] .card.live        {{ border-left: 3px solid rgba(180,0,0,.35); }}
  [data-theme="light"] .card-thumb       {{ background: #ebebea; border-color: #d8d8d6; }}
  [data-theme="light"] .thumb-num        {{ color: #ccc; }}
  [data-theme="light"] .vd-inner         {{ background: #f8f8f8; border-color: #ddd; }}
  [data-theme="light"] .vd-header        {{ background: #eee; border-color: #ddd; color: #999; }}
  [data-theme="light"] .vd-ip            {{ color: #2563eb; }}
  [data-theme="light"] .vd-dur           {{ color: #bbb; }}
  [data-theme="light"] .vd-empty         {{ color: #bbb; }}
  [data-theme="light"] .sys-stat         {{ color: #bbb; }}
  [data-theme="light"] .q-input          {{ background: #f8f8f8; border-color: #ccc; color: #111; }}
  [data-theme="light"] .q-btn            {{ background: rgba(0,0,0,.05); border-color: rgba(0,0,0,.14); color: #333; }}
  [data-theme="light"] .q-btn:hover      {{ background: rgba(0,0,0,.1); }}
  [data-theme="light"] .adb-panel        {{ background: #f8f8f8; border-color: #ddd; }}
  [data-theme="light"] .adb-ip-input     {{ background: #fff; border-color: #ccc; color: #111; }}
  [data-theme="light"] .adb-save-btn     {{ background: rgba(37,99,235,.07); border-color: rgba(37,99,235,.2); color: #2563eb; }}
  [data-theme="light"] .adb-btn          {{ background: #f0f0f0; border-color: #d0d0d0; color: #333; }}
  [data-theme="light"] .adb-btn:hover    {{ background: #e0e0e0; color: #000; }}
  [data-theme="light"] .adb-center       {{ background: rgba(0,0,0,.06); border-color: rgba(0,0,0,.18); color: #111; }}
  [data-theme="light"] .adb-home         {{ background: rgba(37,99,235,.06); border-color: rgba(37,99,235,.2); color: #2563eb; }}
  [data-theme="light"] .adb-back         {{ background: rgba(217,119,6,.06); border-color: rgba(217,119,6,.2); color: #d97706; }}
  [data-theme="light"] .adb-status       {{ color: #bbb; }}
  [data-theme="light"] .adb-status.ok    {{ color: #2563eb; }}
  [data-theme="light"] .adb-status.err   {{ color: #c00; }}
  [data-theme="light"] .manage-item      {{ background: #f8f8f8; border-color: #e0e0e0; color: #111; }}
  [data-theme="light"] .manage-item:hover {{ background: #f0f0f0; }}
  [data-theme="light"] .btn-abort        {{ background: #f0f0f0; border-color: #ccc !important; color: #555; }}
  [data-theme="light"] .btn-commit       {{ background: rgba(22,163,74,.08); border-color: rgba(22,163,74,.25) !important; color: #15803d; }}
  [data-theme="light"] .form-grid input,
  [data-theme="light"] .form-grid select {{ background: #f8f8f8; border-color: #ccc; color: #111; }}
  [data-theme="light"] .pipeline-select  {{ background: #f8f8f8; border-color: #ccc; color: #111; }}
  [data-theme="light"] .confirm-box      {{ background: #fff; border-color: #ddd; }}
  [data-theme="light"] .modal            {{ background: #fff; border-color: #ddd; }}
  [data-theme="light"] .modal-close      {{ color: #bbb; }}
  [data-theme="light"] .modal-close:hover {{ color: #666; }}
  [data-theme="light"] #hub-toast        {{ background: #fff; border-color: #ddd; color: #111; }}
  [data-theme="light"] .sig-locked       {{ background: rgba(22,163,74,.08); border-color: rgba(22,163,74,.2); color: #15803d; }}
  [data-theme="light"] .sig-other        {{ background: rgba(0,0,0,.04); border-color: #d8d8d6; color: #999; }}
  [data-theme="light"] .section-lbl      {{ color: #bbb; }}
  [data-theme="light"] .btn-manage       {{ background: rgba(0,0,0,.04); border-color: rgba(0,0,0,.1); color: #777; }}
  [data-theme="light"] .btn-manage:hover {{ background: rgba(0,0,0,.08); color: #333; }}
  [data-theme="light"] .rescan-btn       {{ background: rgba(0,0,0,.04); border-color: rgba(0,0,0,.1); color: #777; }}

  /* ── Mono theme ── */
  [data-theme="mono"] .card-thumb        {{ background: #040404; border-color: #1c1c1c; }}
  [data-theme="mono"] .card-thumb::after {{
    content: '';
    position: absolute; inset: 0;
    background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(255,255,255,.025) 2px, rgba(255,255,255,.025) 4px);
    pointer-events: none;
  }}
  [data-theme="mono"] .card-sub          {{ font-family: 'IBM Plex Mono', monospace; letter-spacing: .03em; font-size: 10px; }}
  [data-theme="mono"] .sig-locked        {{ background: rgba(255,255,255,.05); border-color: rgba(255,255,255,.15); color: #ccc; }}
  [data-theme="mono"] .sig-other         {{ background: rgba(255,255,255,.03); border-color: #222; color: #444; }}
  [data-theme="mono"] .viewers-chip      {{ background: rgba(255,255,255,.05); border-color: rgba(255,255,255,.14); color: #888; }}
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

  /* ── Driver badges ── */
  .driver-badge-mw {{
    right: 4px; bottom: 4px;
    background: rgba(74,158,255,.1); border: 1px solid rgba(74,158,255,.25);
    color: var(--blue);
  }}
  .driver-badge-dl {{
    right: 4px; bottom: 4px;
    background: rgba(180,100,255,.1); border: 1px solid rgba(180,100,255,.25);
    color: var(--purple);
  }}

  /* ── Decklink config panel ── */
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
  .dl-fmt-status {{
    font-size: 10px; min-height: 14px; margin-bottom: 6px;
    font-family: 'Courier New', monospace; transition: color .3s;
  }}
  .dl-fmt-status.ok      {{ color: var(--green); }}
  .dl-fmt-status.err     {{ color: var(--live); }}
  .dl-fmt-status.loading {{ color: var(--muted); }}
  .dl-grid {{
    display: grid; grid-template-columns: 56px 1fr; gap: 5px 8px; align-items: center;
  }}
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
    const driver=m.driver||'magewell';
    const drvCls=driver==='decklink'?'driver-badge-dl':'driver-badge-mw';
    const drvTxt=driver==='decklink'?'DL':'MW';
    const encOpts=availEncoders.map(e=>`<option value="${{e.value}}"${{e.value===(m.encoder||'')?' selected':''}}>${{e.label}}</option>`).join('');
    return `<div class="card row-entering" id="card-${{id}}" style="opacity:.5">
      <div class="card-body">
        <div class="card-thumb" id="thumb-${{id}}">
          <div class="thumb-num" style="font-style:italic">${{id}}</div>
          <div class="thumb-badge ${{drvCls}}">${{drvTxt}}</div>
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
        const drvCls = driver==='decklink' ? 'driver-badge-dl' : 'driver-badge-mw';
        const drvTxt = driver==='decklink' ? 'DL' : 'MW';
        badges+=`<div class="thumb-badge ${{drvCls}}">${{drvTxt}}</div>`;
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

      // Q row visibility (hide for Decklink CBR)
      const qrow=document.getElementById('qrow-'+id);
      if(qrow) qrow.style.display=(driver==='decklink' && m.quality_mode==='cbr')?'none':'';

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
      es.onmessage=e=>{{
        try{{
          const data = JSON.parse(e.data);
          console.log('[SSE] driver_missing=', data.driver_missing, 'installer_path=', data.installer_path);
          const banner = document.getElementById('driver-banner');
          console.log('[SSE] banner element=', banner);
          applyStats(data);
        }}catch(err){{ console.warn('SSE error:',err); }}
      }};
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
  let _manageHiddenSet = new Set();

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
    const list = document.getElementById('manage-list');
    list.innerHTML = data.inputs.map(inp => {{
      const dimmed = (inp.signal === 'NONE' || inp.signal === 'UNKNOWN') ? 'opacity:0.6;' : '';
      const drvBadge = inp.driver === 'decklink'
        ? `<span style="color:var(--purple);font-weight:900;font-size:9px;border:1px solid rgba(180,100,255,.3);padding:1px 5px;border-radius:3px">DL</span>`
        : `<span style="color:var(--blue);font-weight:900;font-size:9px;border:1px solid rgba(74,158,255,.3);padding:1px 5px;border-radius:3px">MW</span>`;
      return `<label class="manage-item" style="${{dimmed}}">
        <input type="checkbox" value="${{inp.key}}" ${{inp.active ? 'checked' : ''}}>
        <span style="flex:1">${{inp.label}}</span>
        ${{drvBadge}}
        <span style="color:#3a3a3a;font-weight:700;font-size:10px">[${{inp.signal}}]</span>
      </label>`;
    }}).join('');
    if (btn) {{ btn.disabled = false; btn.textContent = '↺ Refresh'; }}
    document.getElementById('manage-overlay').classList.add('show');
  }}

  async function refreshManage() {{
    const btn = document.getElementById('manage-refresh-btn');
    if (btn) {{ btn.disabled = true; btn.textContent = 'Scanning…'; }}
    try {{ await fetch('/inputs/rescan', {{method: 'POST'}}); }} catch(e) {{}}
    await openManage();
  }}

  function closeManage() {{ document.getElementById('manage-overlay').classList.remove('show'); }}

  async function applyManage() {{
    const checked = new Set(Array.from(document.querySelectorAll('#manage-list input:checked')).map(i => i.value));
    const allKeys = Array.from(document.querySelectorAll('#manage-list input[type=checkbox]')).map(i => i.value);
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

  // ── Decklink config panel ─────────────────────────────────────────────────────
  const _dlFormatsLoaded = new Set();  // track which inputs have been queried

  function toggleDlPanel(inputId) {{
    const body  = document.getElementById('dlbody-'+inputId);
    const caret = document.getElementById('dlcaret-'+inputId);
    if (!body) return;
    const open = body.style.display === 'none';
    body.style.display  = open ? '' : 'none';
    if (caret) caret.style.transform = open ? 'rotate(180deg)' : '';
  }}

  async function loadDlFormats(inputId) {{
    const sel    = document.getElementById('dl-fmt-'+inputId);
    const btn    = document.getElementById('dl-fmtbtn-'+inputId);
    const status = document.getElementById('dl-fmtstatus-'+inputId);
    if (!sel) return;

    if (btn)    {{ btn.disabled = true; btn.textContent = '…'; }}
    if (status) {{ status.textContent = 'Querying device…'; status.className = 'dl-fmt-status loading'; }}

    try {{
      const res  = await fetch('/input/'+inputId+'/decklink_formats');
      const data = await res.json();

      if (!data.ok) {{
        if (status) {{
          status.textContent = data.error || 'Failed to load formats';
          status.className   = 'dl-fmt-status err';
        }}
        return;
      }}

      // Rebuild the select with real options, preserving the saved selection
      const current = data.current || sel.value;
      sel.innerHTML = data.formats.map(f => {{
        const label    = f.mode ? `${{f.label}} (${{f.code}})` : f.label;
        const selected = (f.code === current || f.mode === current) ? ' selected' : '';
        const value    = f.code || f.mode;
        return `<option value="${{value}}"${{selected}}>${{label}}</option>`;
      }}).join('');

      // If nothing matched the saved value, prepend it so it's not silently lost
      if (!data.formats.some(f => f.code === current || f.mode === current)) {{
        sel.insertAdjacentHTML('afterbegin',
          `<option value="${{current}}" selected>${{current}} (saved)</option>`);
      }}

      _dlFormatsLoaded.add(inputId);
      if (status) {{
        status.textContent = `${{data.formats.length}} format${{data.formats.length !== 1 ? 's' : ''}} available`;
        status.className   = 'dl-fmt-status ok';
      }}
    }} catch(e) {{
      if (status) {{ status.textContent = 'Network error'; status.className = 'dl-fmt-status err'; }}
    }} finally {{
      if (btn) {{ btn.disabled = false; btn.textContent = '↺'; }}
    }}
  }}

  function dlToggleMode(inputId) {{
    const mode  = document.getElementById('dl-qmode-'+inputId)?.value;
    const qrow  = document.getElementById('qrow-'+inputId);
    const vblbl = document.getElementById('dl-vblbl-'+inputId);
    const vbr   = document.getElementById('dl-vbr-'+inputId);
    if (qrow)  qrow.style.display  = (mode === 'cbr') ? 'none' : '';
    if (vblbl) vblbl.style.display = (mode === 'cbr') ? '' : 'none';
    if (vbr)   vbr.style.display   = (mode === 'cbr') ? '' : 'none';
  }}

  // Called when audio codec or channel layout changes.
  // Shows a warning if the selected codec can't handle the chosen channel count,
  // and shows/hides the LFE swap option for surround layouts.
  function dlUpdateAudioOptions(inputId) {{
    const codecSel  = document.getElementById('dl-acodec-'+inputId);
    const layoutSel = document.getElementById('dl-layout-'+inputId);
    const lfeRow    = document.getElementById('dl-lfe-row-'+inputId);
    const warnEl    = document.getElementById('dl-audio-warn-'+inputId);
    if (!codecSel || !layoutSel) return;

    const codecVal  = codecSel.value;
    const layoutVal = layoutSel.value;

    // Look up max channels for this codec
    const codecMeta  = availAudioCodecs.find(c => c.value === codecVal) || {{}};
    const maxCh      = codecMeta.max_ch || 2;

    // Look up output channels for the chosen layout
    const layoutMeta = availChLayouts.find(cl => cl.value === layoutVal) || {{}};
    const outCh      = layoutMeta.out_ch || 2;

    // Show/hide LFE swap checkbox — only relevant for surround
    const isSurround = ['5.1','7.1','8ch'].includes(layoutVal);
    if (lfeRow) lfeRow.style.display = isSurround ? '' : 'none';

    // Warn if codec can't handle channel count
    if (warnEl) {{
      if (outCh > maxCh) {{
        warnEl.textContent = `⚠ ${{codecMeta.label || codecVal}} supports max ${{maxCh}} channels — layout will be downmixed.`;
        warnEl.style.display = '';
      }} else {{
        warnEl.style.display = 'none';
      }}
    }}
  }}

  async function submitDlCfg(inputId) {{
    const fd = new FormData();
    fd.append('format_code',    document.getElementById('dl-fmt-'+inputId)?.value    || 'hp50');
    fd.append('quality_mode',   document.getElementById('dl-qmode-'+inputId)?.value  || 'cqp');
    fd.append('video_bitrate',  document.getElementById('dl-vbr-'+inputId)?.value    || '50M');
    fd.append('audio_bitrate',  document.getElementById('dl-abr-'+inputId)?.value    || '128k');
    fd.append('video_filter',   document.getElementById('dl-vf-'+inputId)?.value     || 'yadif=1,scale=1920:1080');
    fd.append('gop',            document.getElementById('dl-gop-'+inputId)?.value    || '90');
    fd.append('audio_codec',    document.getElementById('dl-acodec-'+inputId)?.value || 'aac');
    fd.append('channel_layout', document.getElementById('dl-layout-'+inputId)?.value || 'stereo');
    fd.append('fix_lfe_swap',   document.getElementById('dl-lfe-'+inputId)?.checked ? '1' : '0');
    try {{
      const res  = await fetch('/input/'+inputId+'/set_decklink_cfg', {{method:'POST', body:fd}});
      const data = await res.json();
      if (data.ok) showToast(data.restarted ? 'Decklink config saved · restarting…' : 'Decklink config saved');
      else showToast(data.error || 'Failed to save', false);
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  // ── ADB ──────────────────────────────────────────────────────────────────────
  var _activeAdbInputId=null, _adbKeyboardActive=false;
  const KEY_MAP={{'ArrowUp':'up','ArrowDown':'down','ArrowLeft':'left','ArrowRight':'right','Enter':'enter','Backspace':'back'}};

  async function adbKey(key) {{
    if(!_activeAdbInputId) return;
    const fd=new FormData(); fd.append('key',key);
    try {{
      const res=await fetch(`/input/${{_activeAdbInputId}}/adb_key`,{{method:'POST',body:fd}});
      const data=await res.json();
      const st=document.getElementById('adb-status');
      if(data.ok){{ st.textContent=`↑ ${{key}}`; st.className='adb-status ok'; }}
      else        {{ st.textContent='Error';      st.className='adb-status err'; }}
    }} catch(e) {{}}
  }}

  async function saveAdbIp(inputId) {{
    const ip = document.getElementById('adb-ip-field').value.trim();
    if (!ip) {{ showToast('Enter an IP address first', false); return; }}
    const btn = document.querySelector(`[onclick="saveAdbIp('${{inputId}}')"], .adb-save-btn`);
    if (btn) {{ btn.textContent = 'Connecting…'; btn.disabled = true; }}
    const fd = new FormData(); fd.append('adb_ip', ip);
    try {{
      const res = await fetch(`/input/${{inputId}}/set_adb_ip`, {{method:'POST', body:fd}});
      const data = await res.json();
      if (data.connected) {{
        showToast(`✓ Connected — ${{ip}}`, true);
      }} else if (data.adb_ip && data.connect_msg) {{
        showToast(`Saved — ${{data.connect_msg}}`, false);
      }} else {{
        showToast(data.error || 'Failed to save', false);
      }}
      const st = document.getElementById('adb-status');
      if (st) {{
        st.textContent = data.connected ? `linked ${{ip}}` : (data.connect_msg || 'not connected');
        st.className = 'adb-status ' + (data.connected ? 'ok' : 'err');
      }}
      // Immediately show/hide the adb-home checkbox based on the saved IP
      const homeRow = document.getElementById('rec-adb-home-row');
      if (homeRow) homeRow.style.display = ip ? '' : 'none';
    }} catch(e) {{
      showToast('Network error', false);
    }} finally {{
      if (btn) {{ btn.textContent = 'Link TV'; btn.disabled = false; }}
    }}
  }}

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
    const _adbField = document.getElementById('adb-ip-field');
    _adbField.value = inputMeta[id]?.adb_ip || '';
    // Load the user's saved directory from the server
    fetch('/prefs/rec_dir').then(r=>r.json()).then(d=>{{
      document.getElementById('rec-dir').value = d.rec_dir || '';
    }}).catch(()=>{{}});
    // Show/hide adb-home checkbox — re-check live field value, not stale meta
    function _updateHomeRow() {{
      const homeRow = document.getElementById('rec-adb-home-row');
      if (homeRow) homeRow.style.display = _adbField.value.trim() ? '' : 'none';
    }}
    _updateHomeRow();
    _adbField.oninput = _updateHomeRow;
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
  const THEME_LABELS = {{ dark: '● Dark', mono: '◐ Mono', light: '○ Light' }};

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
    fd.append('preset',       document.getElementById('mw-preset-'+inputId)?.value || '');
    fd.append('lookahead',    document.getElementById('mw-la-'+inputId)?.value     || '35');
    fd.append('p010',         document.getElementById('mw-p010-'+inputId)?.checked  ? '1' : '0');
    fd.append('no_audio',     document.getElementById('mw-noa-'+inputId)?.checked   ? '1' : '0');
    fd.append('vaapi_device', document.getElementById('mw-dev-'+inputId)?.value     || '');
    try {{
      const res  = await fetch('/input/'+inputId+'/set_magewell_cfg', {{method:'POST', body:fd}});
      const data = await res.json();
      if (data.ok) showToast(data.restarted ? 'Magewell config saved · restarting…' : 'Magewell config saved');
      else showToast(data.error || 'Failed to save', false);
    }} catch(e) {{ showToast('Network error', false); }}
  }}

  // Restore saved theme on load
  (function() {{
    try {{
      const saved = localStorage.getItem('bh-theme');
      if (saved && THEMES.includes(saved)) applyTheme(saved);
    }} catch(e) {{}}
  }})();

  // Pre-load Decklink format lists for all Decklink inputs at page load.
  // Runs in parallel — each call is independent and failures are shown
  // inline in the panel status line rather than blocking the page.
  document.addEventListener('DOMContentLoaded', () => {{
    const decklinkIds = {decklink_ids_js};
    for (const id of decklinkIds) {{
      loadDlFormats(id);
    }}
  }});
</script>
</head>
<body>

<!-- Topbar -->
<div class="topbar">
  <div class="logo">Broadcast<span>Hub</span></div>
  <div class="topbar-right">
    <div class="sse-dot" id="sse-indicator" style="opacity:.3"><div class="dot"></div> Live</div>
    <button class="theme-toggle" id="theme-toggle" onclick="cycleTheme()">● Dark</button>
    <a href="/mobile" class="mobile-link">Mobile ↗</a>
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
    <div class="manage-footer">
      <button class="btn btn-abort" onclick="closeManage()">Abort</button>
      <button class="btn btn-commit" onclick="applyManage()">Commit</button>
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
          <input id="adb-ip-field" type="text" class="adb-ip-input" placeholder="ADB IP:5555">
          <button class="adb-save-btn" onclick="saveAdbIp(_activeAdbInputId)">Link TV</button>
          <div class="adb-remote">
            <button class="adb-btn adb-dpad" onclick="adbKey('up')">▲</button>
            <div class="adb-row">
              <button class="adb-btn adb-dpad" onclick="adbKey('left')">◀</button>
              <button class="adb-btn adb-dpad adb-center" onclick="adbKey('enter')">OK</button>
              <button class="adb-btn adb-dpad" onclick="adbKey('right')">▶</button>
            </div>
            <button class="adb-btn adb-dpad" onclick="adbKey('down')">▼</button>
            <div class="adb-row" style="margin-top:8px">
              <button class="adb-btn adb-action adb-home" onclick="adbKey('home')">Home</button>
              <button class="adb-btn adb-action adb-back" onclick="adbKey('back')">Back</button>
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
        <span>Return TV to Home screen 60s after recording ends</span>
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
