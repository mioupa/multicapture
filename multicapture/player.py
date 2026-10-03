import json
import time

from .cdp import CDPError

STATE_RETRIES = 4

FIND_JS = r"""
(() => {
  const found = [];
  const visit = (root, frames) => {
    root.querySelectorAll('video').forEach(v => found.push({v, frames}));
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) visit(el.shadowRoot, frames); });
    root.querySelectorAll('iframe,frame').forEach(f => {
      try { if (f.contentDocument) visit(f.contentDocument, frames.concat([f])); } catch (e) {}
    });
  };
  visit(document, []);
  const info = found.map(x => {
    const r = x.v.getBoundingClientRect();
    return {x, area: Math.max(0, r.width) * Math.max(0, r.height), dur: isFinite(x.v.duration) ? x.v.duration : 0};
  });
  const withDur = info.filter(i => i.dur > 0);
  const pool = withDur.length ? withDur : info;
  if (!pool.length) return {found: false, count: 0};
  pool.sort((a, b) => b.area - a.area);
  const main = pool[0];
  const group = main.dur > 0 ? info.filter(i => i.dur > 0 && Math.abs(i.dur - main.dur) <= Math.max(2, main.dur * 0.01)) : [main];
  window.__mc = {main: main.x.v, videos: group.map(i => i.x.v), frames: main.x.frames};
  return {found: true, duration: main.dur, area: main.area, count: info.length, group: group.length,
          currentTime: main.x.v.currentTime, src: (main.x.v.currentSrc || '').slice(0, 200)};
})()
"""

KICK_JS = r"""
(() => {
  const vids = Array.from(document.querySelectorAll('video'));
  vids.forEach(v => { try { v.muted = false; const p = v.play(); if (p) p.catch(() => {}); } catch (e) {} });
  const words = ['play', '再生', 'start', 'watch'];
  const cands = Array.from(document.querySelectorAll('button,[role=button],a,div,span'))
    .filter(el => {
      const t = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '') + ' ' + (el.getAttribute('class') || '')).toLowerCase();
      const r = el.getBoundingClientRect();
      return r.width > 20 && r.height > 20 && words.some(w => t.includes(w));
    });
  if (cands.length) { cands.sort((a, b) => { const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect(); return rb.width * rb.height - ra.width * ra.height; }); cands[0].click(); }
  return {videos: vids.length, clicked: cands.length > 0};
})()
"""

STATE_JS = r"""
(() => {
  const m = window.__mc && window.__mc.main;
  if (!m || !m.isConnected) return {ok: false};
  let ahead = 0;
  for (let i = 0; i < m.buffered.length; i++) {
    if (m.buffered.start(i) <= m.currentTime + 0.1 && m.buffered.end(i) > m.currentTime) ahead = m.buffered.end(i) - m.currentTime;
  }
  return {ok: true, t: m.currentTime, paused: m.paused, ended: m.ended, ready: m.readyState,
          seeking: m.seeking, ahead: ahead, duration: isFinite(m.duration) ? m.duration : 0};
})()
"""

SEEK_JS = r"""
(async (t) => {
  const mc = window.__mc;
  if (!mc) return false;
  mc.videos.forEach(v => { try { v.pause(); } catch (e) {} });
  const m = mc.main;
  const done = new Promise(res => {
    const finish = () => { m.removeEventListener('seeked', finish); res(true); };
    m.addEventListener('seeked', finish);
    setTimeout(() => res(false), 20000);
  });
  mc.videos.forEach(v => { try { v.currentTime = t; } catch (e) {} });
  await done;
  const start = Date.now();
  while (m.readyState < 3 && Date.now() - start < 20000) await new Promise(r => setTimeout(r, 100));
  return Math.abs(m.currentTime - t) < 1.5;
})
"""

PLAY_JS = r"""
(async () => {
  const mc = window.__mc;
  if (!mc) return 'no player';
  mc.videos.forEach(v => { v.muted = false; v.playbackRate = 1; });
  const all = Promise.all(mc.videos.map(v => v.play().then(() => 'ok').catch(e => String(e))));
  const results = await Promise.race([all, new Promise(r => setTimeout(() => r(null), 3000))]);
  if (!results) return 'pending';
  return results[mc.videos.indexOf(mc.main)] || 'ok';
})()
"""

PAUSE_JS = r"""
(() => { const mc = window.__mc; if (mc) mc.videos.forEach(v => { try { v.pause(); } catch (e) {} }); return true; })()
"""

PIN_FN = r"""
function(target) {
  const el = target || (window.__mc && window.__mc.videos.length > 1 ? (() => {
    const vs = window.__mc.videos;
    let node = vs[0];
    while (node && !vs.every(v => node.contains(v))) node = node.parentElement;
    return node || vs[0];
  })() : window.__mc && window.__mc.main);
  if (!el) return false;
  const doc = el.ownerDocument;
  if (!doc.getElementById('__mc_style')) {
    const s = doc.createElement('style');
    s.id = '__mc_style';
    s.textContent = 'html,body{overflow:hidden!important;background:#000!important}' +
      '[data-mc-hide]{visibility:hidden!important}' +
      '[data-mc-path]{transform:none!important;filter:none!important;contain:none!important;perspective:none!important;will-change:auto!important;clip-path:none!important}' +
      '[data-mc-pin]{position:fixed!important;left:0!important;top:0!important;right:auto!important;bottom:auto!important;width:100vw!important;height:100vh!important;' +
      'max-width:none!important;max-height:none!important;min-width:0!important;min-height:0!important;margin:0!important;padding:0!important;border:0!important;' +
      'z-index:2147483647!important;background:#000!important;transform:none!important;visibility:visible!important;opacity:1!important}' +
      'video[data-mc-pin]{object-fit:contain!important}';
    (doc.head || doc.documentElement).appendChild(s);
  }
  el.setAttribute('data-mc-pin', '1');
  let node = el;
  while (node.parentElement) {
    const parent = node.parentElement;
    parent.setAttribute('data-mc-path', '1');
    for (const sib of parent.children) {
      if (sib !== node && !['SCRIPT', 'STYLE', 'LINK', 'HEAD'].includes(sib.tagName)) sib.setAttribute('data-mc-hide', '1');
    }
    node = parent;
  }
  return true;
}
"""

PIN_SAME_ORIGIN_FRAMES_JS = r"""
(() => {
  const mc = window.__mc;
  if (!mc) return false;
  const pin = """ + PIN_FN.strip() + r""";
  pin(null);
  for (let i = mc.frames.length - 1; i >= 0; i--) pin(mc.frames[i]);
  return true;
})()
"""


class Player:
    def __init__(self, browser):
        self.browser = browser
        self.session = None
        self.target_id = None
        self.duration = 0.0
        self._sessions = {}

    def _attach_all(self):
        for info in self.browser.targets():
            if info.get("type") not in ("page", "iframe"):
                continue
            tid = info["targetId"]
            if tid not in self._sessions:
                try:
                    self._sessions[tid] = (self.browser.attach(tid), info)
                except CDPError:
                    pass
        return self._sessions

    def page_session(self):
        for sid, info in self._attach_all().values():
            if info.get("type") == "page":
                return sid
        raise CDPError("ページが見つかりません")

    def navigate(self, url):
        sid = self.page_session()
        self.browser.call("Page.enable", session_id=sid)
        self.browser.call("Page.navigate", {"url": url}, session_id=sid)

    def find(self, timeout=60.0, cancelled=None):
        deadline = time.monotonic() + timeout
        kicked_at = time.monotonic() + 6
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                raise CDPError("中止しました")
            best = None
            for tid, (sid, info) in list(self._attach_all().items()):
                try:
                    res = self.browser.evaluate(sid, FIND_JS, timeout=10)
                except CDPError:
                    continue
                if res and res.get("found") and res.get("duration", 0) > 0:
                    score = res["area"]
                    if best is None or score > best[0]:
                        best = (score, tid, sid, res)
            if best:
                _, self.target_id, self.session, res = best
                self.browser.evaluate(self.session, FIND_JS, timeout=10)
                self.duration = float(res["duration"])
                return res
            if time.monotonic() > kicked_at:
                kicked_at = time.monotonic() + 8
                for sid, _ in list(self._sessions.values()):
                    try:
                        self.browser.evaluate(sid, KICK_JS, timeout=10, user_gesture=True)
                    except CDPError:
                        pass
            time.sleep(1.0)
        raise CDPError("ページ内に動画が見つかりませんでした（ログインが必要か、再生ボタンを押す必要があるかもしれません）")

    def state(self):
        last_error = None
        for _ in range(STATE_RETRIES):
            try:
                return self.browser.evaluate(self.session, STATE_JS, timeout=10) or {"ok": False}
            except CDPError as exc:
                last_error = exc
                time.sleep(0.5)
        raise last_error

    def seek(self, t):
        return bool(self.browser.evaluate(self.session, f"({SEEK_JS})({float(t)!r})", timeout=60))

    def wait_buffered(self, seconds, end, timeout, cancelled=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                return False
            st = self.state()
            if st.get("ok") and (st.get("ahead", 0) >= seconds or st.get("t", 0) + st.get("ahead", 0) >= end - 0.05) and st.get("ready", 0) >= 3:
                return True
            time.sleep(0.1)
        return False

    def play(self):
        return self.browser.evaluate(self.session, PLAY_JS, timeout=20, user_gesture=True)

    def pause(self):
        self.browser.evaluate(self.session, PAUSE_JS, timeout=10)

    def pin_video(self):
        self.browser.evaluate(self.session, PIN_SAME_ORIGIN_FRAMES_JS, timeout=15)
        child = self.target_id
        sessions = self._attach_all()
        for _ in range(8):
            info = sessions.get(child, (None, {}))[1]
            if info.get("type") != "iframe":
                break
            owner = None
            for tid, (sid, pinfo) in sessions.items():
                if tid == child:
                    continue
                try:
                    self.browser.call("DOM.enable", session_id=sid)
                    self.browser.call("DOM.getDocument", {"depth": 0}, session_id=sid)
                    node = self.browser.call("DOM.getFrameOwner", {"frameId": child}, session_id=sid)
                    obj = self.browser.call("DOM.resolveNode", {"backendNodeId": node["backendNodeId"]}, session_id=sid)
                    self.browser.call("Runtime.callFunctionOn", {
                        "objectId": obj["object"]["objectId"], "functionDeclaration": f"function() {{ return ({PIN_FN})(this); }}",
                        "returnByValue": True,
                    }, session_id=sid)
                    owner = tid
                    break
                except (CDPError, KeyError):
                    continue
            if owner is None:
                break
            child = owner
        return True
