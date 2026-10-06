/* 值班台前端：全部数据来自真实 HTTP API，每 2 秒轮询一次。 */
"use strict";

const $ = (id) => document.getElementById(id);
let selectedId = null;

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  const data = await res.json();
    if (!res.ok) throw { status: res.status, data };
  return data;
}

async function loadRules() {
  const r = await api("GET", "/api/rules");
  $("rules-input").value = JSON.stringify(r.rules.mask_fields);
  $("rules-msg").textContent = "";
}

async function saveRules() {
  const el = $("rules-msg");
  try {
    const parsed = JSON.parse($("rules-input").value || "[]");
    const r = await api("PUT", "/api/rules", { rules: { mask_fields: parsed } });
    el.className = "msg ok";
    el.textContent = `当前规则已更新并规范化为：${JSON.stringify(r.rules.mask_fields)}`;
    await loadRules();
  } catch (e) {
    el.className = "msg err";
    el.textContent = `规则保存失败：${(e.data && e.data.detail) || e.message || e}`;
  }
}

async function submitExport() {
  const btn = $("submit-btn");
  btn.disabled = true;
  const el = $("submit-msg");
  try {
    const records = JSON.parse($("records-input").value);
    const r = await api("POST", "/api/exports", { records });
    if (r.outcome === "created") {
      el.className = "msg ok";
      el.textContent =
        `已受理。\n稳定导出标识：${r.export_id}\n首次回执：${r.receipt_id}\n` +
        `冻结规则：${JSON.stringify(r.frozen_rules.mask_fields)}`;
      selectedId = r.export_id;
    } else if (r.outcome === "duplicate") {
      el.className = "msg ok";
      el.textContent = `重传：已返回首次回执 ${r.receipt_id}（不产生第二个工件）`;
    }
    refresh();
  } catch (e) {
    if (e.status === 409) {
      el.className = "msg err";
      el.textContent =
        `冲突 (409)：业务键相同但 ${e.data.rules_changed ? "规则快照" : ""}` +
        `${e.data.input_changed ? "记录" : ""} 不同，已拒绝并保留原有证据。\n` +
        `原导出：${e.data.existing_export_id}（${e.data.existing_stage}）`;
    } else {
      el.className = "msg err";
      el.textContent = `提交失败：${(e.data && e.data.detail) || e.message || e}`;
    }
  } finally {
    btn.disabled = false;
  }
}

function stagePill(stage) {
  return `<span class="pill stage-${stage}">${stage}</span>`;
}

async function refresh() {
  try {
    const r = await api("GET", "/api/exports");
    const rows = r.exports;
    const tb = $("export-rows");
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="5" style="color:var(--muted)">暂无导出</td></tr>';
    } else {
      tb.innerHTML = rows.map((it) => `
        <tr>
          <td class="mono">${it.export_id}<br><span style="color:var(--muted)">${it.receipt_id}</span></td>
          <td>${stagePill(it.stage)} <span style="color:var(--muted)">(attempts ${it.attempts})</span></td>
          <td>${(it.frozen_rules_summary.mask_fields || []).map((f) =>
            `<span class="rules-chip">${f}</span>`).join("")}</td>
          <td class="mono" style="max-width:280px; overflow:hidden; text-overflow:ellipsis">
            ${it.content_sha256 ? it.content_sha256.slice(0, 24) + "…" : "—"}<br>
            ${it.artifact_name ? `<a href="/api/exports/${it.export_id}/artifact" target="_blank">${it.artifact_name}</a>` : "—"}
          </td>
          <td>
            <button class="ghost" style="margin:0" onclick="showDetail('${it.export_id}')">阶段日志</button>
            ${it.stage === "PUBLISHED"
              ? `<a href="/api/exports/${it.export_id}/artifact"><button class="ghost" style="margin:0">下载</button></a>`
              : `<button class="ghost" style="margin:0" disabled title="未核验发布前不可下载">锁</button>`}
          </td>
        </tr>`).join("");
    }
    if (selectedId && rows.some((x) => x.export_id === selectedId)) showDetail(selectedId);
  } catch (e) {
    /* 轮询失败时下一周期自动重试 */
  }
}

async function showDetail(id) {
  selectedId = id;
  try {
    const s = await api("GET", `/api/exports/${id}`);
    $("detail").style.display = "block";
    $("detail-title").textContent = `${s.export_id} · ${s.stage}`;
    $("detail-timeline").innerHTML = s.stages
      .map((st) => `<li><span class="mono">${st.stage}</span> ${st.note || ""}</li>`)
      .join("");
  } catch (e) { /* ignore */ }
}

loadRules();
refresh();
setInterval(refresh, 2000);
