"""Interactive HTML UI template for server-side TeraBox verification.

Provides live browser streaming, click/drag interaction for slider puzzles, and direct-link resolution.
"""

VERIFICATION_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TeraBox Server-Side Verification</title>
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #1e293b;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --primary: #3b82f6;
      --primary-hover: #2563eb;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --border: #334155;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 1.5rem;
    }
    .container {
      max-width: 1000px;
      width: 100%;
      background: var(--card-bg);
      border-radius: 12px;
      border: 1px solid var(--border);
      padding: 1.5rem;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid var(--border);
    }
    .title { font-size: 1.25rem; font-weight: 600; }
    .status-badge {
      padding: 0.25rem 0.75rem;
      border-radius: 9999px;
      font-size: 0.85rem;
      font-weight: 500;
      background: rgba(59, 130, 246, 0.2);
      color: var(--primary);
      border: 1px solid var(--primary);
    }
    .instruction {
      font-size: 0.9rem;
      color: var(--text-muted);
      margin-bottom: 1rem;
    }
    .screen-wrapper {
      position: relative;
      width: 100%;
      background: #000;
      border-radius: 8px;
      overflow: hidden;
      border: 1px solid var(--border);
      cursor: crosshair;
      user-select: none;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 480px;
    }
    #screen-img {
      width: 100%;
      height: auto;
      display: block;
      pointer-events: none;
    }
    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      margin-top: 1rem;
      align-items: center;
    }
    .input-box {
      flex: 1;
      min-width: 220px;
      padding: 0.6rem 0.85rem;
      background: #0f172a;
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 6px;
      font-size: 0.9rem;
    }
    .btn {
      padding: 0.6rem 1.2rem;
      border-radius: 6px;
      border: none;
      font-weight: 500;
      font-size: 0.9rem;
      cursor: pointer;
      transition: background 0.2s;
    }
    .btn-primary { background: var(--primary); color: white; }
    .btn-primary:hover { background: var(--primary-hover); }
    .btn-success { background: var(--success); color: white; }
    .btn-success:hover { filter: brightness(1.1); }
    .timer { font-size: 0.85rem; color: var(--text-muted); margin-left: auto; }
    .result-panel {
      margin-top: 1rem;
      padding: 1rem;
      border-radius: 6px;
      background: #0f172a;
      border: 1px solid var(--border);
      display: none;
    }
    .result-link {
      word-break: break-all;
      color: var(--primary);
      text-decoration: none;
    }
    .drag-indicator {
      position: absolute;
      border: 2px dashed var(--primary);
      background: rgba(59, 130, 246, 0.2);
      pointer-events: none;
      display: none;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="title">🔐 TeraBox Verification Session</div>
      <div class="status-badge" id="status-badge">Verifying in Server Browser</div>
    </div>

    <div class="instruction">
      Complete the puzzle/CAPTCHA below directly on the server's browser. Click or drag sliders as needed.
    </div>

    <div class="screen-wrapper" id="screen-container">
      <img id="screen-img" src="/verification/{{SESSION_ID}}/screenshot" alt="Live Server Browser Stream">
      <div class="drag-indicator" id="drag-indicator"></div>
    </div>

    <div class="controls">
      <input type="text" id="key-input" class="input-box" placeholder="Type text here to send keys to page...">
      <button class="btn btn-primary" id="btn-send-keys">Send Keys</button>
      <button class="btn btn-success" id="btn-complete">I Have Completed Verification</button>
      <div class="timer" id="timer">Session: Loading...</div>
    </div>

    <div class="result-panel" id="result-panel"></div>
  </div>

  <script>
    const sessionId = "{{SESSION_ID}}";
    const screenContainer = document.getElementById("screen-container");
    const screenImg = document.getElementById("screen-img");
    const dragIndicator = document.getElementById("drag-indicator");
    const statusBadge = document.getElementById("status-badge");
    const resultPanel = document.getElementById("result-panel");
    const timerEl = document.getElementById("timer");

    let isDragging = false;
    let startX = 0, startY = 0;
    let autoRefresh = true;

    // Viewport dimensions used by server
    const VW = 1280;
    const VH = 800;

    function getCoords(e) {
      const rect = screenImg.getBoundingClientRect();
      const clientX = e.clientX || (e.touches && e.touches[0].clientX);
      const clientY = e.clientY || (e.touches && e.touches[0].clientY);
      const scaleX = VW / rect.width;
      const scaleY = VH / rect.height;
      return {
        x: Math.round((clientX - rect.left) * scaleX),
        y: Math.round((clientY - rect.top) * scaleY),
        screenX: clientX - rect.left,
        screenY: clientY - rect.top
      };
    }

    screenContainer.addEventListener("mousedown", (e) => {
      isDragging = true;
      const pos = getCoords(e);
      startX = pos.x;
      startY = pos.y;
      dragIndicator.style.left = pos.screenX + "px";
      dragIndicator.style.top = pos.screenY + "px";
      dragIndicator.style.width = "0px";
      dragIndicator.style.height = "0px";
      dragIndicator.style.display = "block";
    });

    window.addEventListener("mousemove", (e) => {
      if (!isDragging) return;
      const pos = getCoords(e);
      const left = Math.min(pos.screenX, (startX * screenImg.clientWidth) / VW);
      const top = Math.min(pos.screenY, (startY * screenImg.clientHeight) / VH);
      const w = Math.abs(pos.screenX - (startX * screenImg.clientWidth) / VW);
      const h = Math.abs(pos.screenY - (startY * screenImg.clientHeight) / VH);
      dragIndicator.style.left = left + "px";
      dragIndicator.style.top = top + "px";
      dragIndicator.style.width = w + "px";
      dragIndicator.style.height = h + "px";
    });

    window.addEventListener("mouseup", async (e) => {
      if (!isDragging) return;
      isDragging = false;
      dragIndicator.style.display = "none";
      const pos = getCoords(e);
      const dist = Math.hypot(pos.x - startX, pos.y - startY);

      if (dist < 8) {
        // Click
        await fetch(`/verification/${sessionId}/click`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ x: startX, y: startY })
        });
      } else {
        // Drag / Slide
        await fetch(`/verification/${sessionId}/drag`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ from_x: startX, from_y: startY, to_x: pos.x, to_y: pos.y })
        });
      }
      refreshScreen();
    });

    document.getElementById("btn-send-keys").addEventListener("click", async () => {
      const input = document.getElementById("key-input");
      if (!input.value) return;
      await fetch(`/verification/${sessionId}/type`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: input.value })
      });
      input.value = "";
      refreshScreen();
    });

    function refreshScreen() {
      if (!autoRefresh) return;
      screenImg.src = `/verification/${sessionId}/screenshot?t=` + Date.now();
    }

    setInterval(refreshScreen, 1200);

    async function checkStatus() {
      try {
        const res = await fetch(`/api/verification/session/${sessionId}`);
        if (res.status === 410) {
          statusBadge.innerText = "Session Expired";
          statusBadge.style.color = "var(--danger)";
          timerEl.innerText = "Expired";
          autoRefresh = false;
          return;
        }
        const data = await res.json();
        if (data.expires_in_seconds !== undefined) {
          const m = Math.floor(data.expires_in_seconds / 60);
          const s = data.expires_in_seconds % 60;
          timerEl.innerText = `Remaining: ${m}m ${s < 10 ? '0' : ''}${s}s`;
        }
      } catch (err) {}
    }
    setInterval(checkStatus, 3000);
    checkStatus();

    document.getElementById("btn-complete").addEventListener("click", async () => {
      statusBadge.innerText = "Resolving Direct Link...";
      resultPanel.style.display = "block";
      resultPanel.innerHTML = "<em>Retrying direct-link resolution in server-side session...</em>";

      try {
        const res = await fetch("/api/verification/complete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId })
        });
        const data = await res.json();

        if (res.status === 200 && data.status === "success") {
          statusBadge.innerText = "Verified & Direct Link Resolved!";
          statusBadge.style.color = "var(--success)";
          let html = `<strong>🎉 Resolution Success!</strong><br><br>`;
          (data.files || []).forEach(f => {
            html += `<div><strong>${f.filename || 'File'}</strong> (${f.size_bytes || 0} bytes)<br>`;
            html += `<a class="result-link" href="${f.direct_link || f.download_link}" target="_blank">Direct Download URL</a></div><br>`;
          });
          resultPanel.innerHTML = html;
        } else if (res.status === 409) {
          statusBadge.innerText = "Verification Still Required";
          statusBadge.style.color = "var(--warning)";
          resultPanel.innerHTML = `<span style="color:var(--warning)">⚠️ TeraBox verification challenge is still pending in the browser. Please solve the puzzle/captcha on the screen above, then click Complete again.</span>`;
        } else {
          statusBadge.innerText = "Resolution Incomplete";
          resultPanel.innerHTML = `<span style="color:var(--danger)">Error: ${data.message || data.error}</span>`;
        }
      } catch (err) {
        resultPanel.innerHTML = `<span style="color:var(--danger)">Network error: ${err.message}</span>`;
      }
    });
  </script>
</body>
</html>
"""


def render_verification_ui(session_id: str) -> str:
    """Render the HTML UI with the specific session ID embedded."""
    return VERIFICATION_HTML_TEMPLATE.replace("{{SESSION_ID}}", session_id)
