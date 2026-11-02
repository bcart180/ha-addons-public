/* CallCatch — list + detail */
(() => {
  const $ = (id) => document.getElementById(id);
  const listEl = $("vm-list");
  const emptyEl = $("empty");
  const subEl = $("list-sub");
  const viewList = $("view-list");
  const viewDetail = $("view-detail");
  const audio = $("audio");
  const scrubber = $("scrubber");

  let items = [];
  let current = null;
  let mediaPlayer = "";
  let editMode = false;
  let selected = new Set();
  let pollTimer = null;
  let toastTimer = null;
  let toastUndo = null;
  const viewNumbers = $("view-numbers");

  const phoneRe = /(?<!\d)(\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4})(?!\d)/g;

  function esc(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function linkPhones(text) {
    return esc(text).replace(phoneRe, (m) => {
      const d = m.replace(/\D/g, "");
      const tel = d.length === 10 ? "1" + d : d;
      if (tel.length < 11) return esc(m);
      return `<a href="tel:+${tel}">${esc(m)}</a>`;
    });
  }

  // Display-only. Stored hangup text is left as-is; the share sheet still copies it.
  // Speaker prefixes are display labels. house / assistant / agent → Assistant,
  // caller → Caller. The sentence after the colon is unchanged, so "his assistant"
  // stays as spoken. Saved transcripts that still say House: keep working.
  function mapSpeakerLine(line) {
    const m = /^([ \t]*)(house|assistant|agent|caller)[ \t]*:[ \t]*(.*)$/i.exec(line);
    if (!m) return line;
    const name = m[2].toLowerCase() === "caller" ? "Caller" : "Assistant";
    return m[3] ? `${m[1]}${name}: ${m[3]}` : `${m[1]}${name}:`;
  }

  function mapSpeakerText(text) {
    return String(text).split(/\r?\n/).map(mapSpeakerLine).join("\n");
  }

  function spokenDuration(raw) {
    const total = Math.floor(Number(raw));
    if (!Number.isFinite(total) || total < 0) return null;
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    const parts = [];
    if (minutes > 0) parts.push(minutes === 1 ? "1 minute" : `${minutes} minutes`);
    if (seconds > 0 || minutes === 0) {
      parts.push(seconds === 1 ? "1 second" : `${seconds} seconds`);
    }
    return parts.join(" ");
  }

  function polishTranscript(text) {
    const lines = String(text).split(/\r?\n/);
    // The hangup "Source:" line names internal plumbing (and the old add-on), not the caller: drop it.
    return lines.filter((line) => !/^Source:\s*(twilio_house_voice|CallCatch)\s*$/i.test(line)).map((line) => {
      const dur = /^Duration_s:\s*([0-9.]+)\s*$/i.exec(line);
      if (dur) {
        const spoken = spokenDuration(dur[1]);
        if (spoken) return `Duration: ${spoken}`;
      }
      if (/^TRANSCRIPT:?\s*$/i.test(line)) return "Transcript";
      return mapSpeakerLine(line);
    }).join("\n");
  }

  function fmt(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  async function api(path, opts) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts && opts.headers) },
      ...opts,
    });
    let body = null;
    try { body = await r.json(); } catch (_) { body = null; }
    if (!r.ok) throw new Error((body && body.error) || `HTTP ${r.status}`);
    return body;
  }

  // STIR/SHAKEN is shown as a signal. It never blocks or hides a message.
  function stirLabel(stir) {
    if (!stir || !stir.status || stir.status === "absent") return "";
    if (stir.status === "passed") return `Caller ID verified by carrier (${stir.level || "?"})`;
    if (stir.status === "failed") return "Caller ID failed carrier verification";
    return "Caller ID not verified";
  }

  function showToast(text, undo) {
    clearTimeout(toastTimer);
    toastUndo = undo || null;
    $("toast-text").textContent = text;
    $("toast-undo").classList.toggle("hidden", !undo);
    $("toast").classList.remove("hidden");
    toastTimer = setTimeout(() => { $("toast").classList.add("hidden"); toastUndo = null; }, 10000);
  }

  async function trustAdd(list, number) {
    return api("api/trust/add", { method: "POST", body: JSON.stringify({ list, number }) });
  }

  async function trustRemove(list, number) {
    return api("api/trust/remove", { method: "POST", body: JSON.stringify({ list, number }) });
  }

  // Add, then offer an undo that restores whatever list the number was on.
  async function trustAddWithUndo(list, number, label) {
    const res = await trustAdd(list, number);
    const entry = (res && res.entry && res.entry.number) || number;
    const previous = res && res.previous;
    showToast(`${label} ${entry}`, async () => {
      await trustRemove(list, entry);
      if (previous) await trustAdd(previous, entry);
      showToast(`Undone: ${entry}`);
      if (!viewNumbers.classList.contains("hidden")) await loadNumbers();
    });
    return res;
  }

  function renderNumbers(listName, rows) {
    const ul = $(`numbers-${listName}`);
    ul.innerHTML = "";
    if (!rows.length) {
      ul.innerHTML = `<li class="empty-row">None</li>`;
      return;
    }
    rows.forEach((row) => {
      const li = document.createElement("li");
      li.innerHTML = `<span>${esc(row.number)}${row.note ? ` <span class="dim tiny">${esc(row.note)}</span>` : ""}</span>
        <button type="button" class="linkish">Remove</button>`;
      li.querySelector("button").addEventListener("click", async () => {
        try {
          await trustRemove(listName, row.number);
          showToast(`Removed ${row.number}`, async () => {
            await trustAdd(listName, row.number);
            await loadNumbers();
          });
          await loadNumbers();
        } catch (e) { showToast(e.message); }
      });
      ul.appendChild(li);
    });
  }

  async function loadNumbers() {
    try {
      const data = await api("api/trust");
      renderNumbers("block", data.block || []);
      renderNumbers("allow", data.allow || []);
      $("numbers-sub").textContent = `Allowed callers skip screening. Blocked callers are rejected before they ring.${data.blocked_calls ? ` ${data.blocked_calls} rejected since the last restart.` : ""}`;
    } catch (e) {
      $("numbers-sub").textContent = e.message === "trust-not-configured"
        ? "Set voice_trust_url and voice_trust_token in the CallCatch options to edit these lists."
        : `Error: ${e.message}`;
      renderNumbers("block", []);
      renderNumbers("allow", []);
    }
  }

  const CONTACT_STATE = {
    ok: "OK", disabled: "iCloud sync off", not_configured: "Apple ID or app password not set",
    auth: "iCloud rejected the sign-in", http: "iCloud error", network: "network error",
    parse: "unexpected iCloud reply", no_addressbook: "no address book found",
    empty: "no numbers found; directory unchanged", write: "could not write caller_names.json",
    too_large: "file too large", bad_upload: "not a .vcf file",
  };

  function contactsLine(d) {
    const when = d.last_success ? new Date(d.last_success * 1000).toLocaleString() : "never";
    const src = d.source === "upload" ? "upload" : d.source === "icloud" ? "iCloud" : "";
    const state = CONTACT_STATE[d.state] || d.state || "";
    return `Last sync: ${when}${src ? ` (${src})` : ""} · ${d.numbers || 0} numbers from ${d.contacts || 0} contacts · ${d.directory_entries || 0} in directory · Status: ${state}`;
  }

  async function loadContacts() {
    try {
      const d = await api("api/contacts/status");
      $("contacts-status").textContent = contactsLine(d);
      $("btn-contacts-sync").disabled = !(d.enabled && d.configured);
    } catch (e) {
      $("contacts-status").textContent = `Contacts: ${e.message}`;
    }
  }

  $("btn-contacts-sync").addEventListener("click", async () => {
    $("contacts-status").textContent = "Syncing…";
    try { await api("api/contacts/sync", { method: "POST", body: "{}" }); } catch (_) { /* status shows reason */ }
    loadContacts();
  });

  $("contacts-file").addEventListener("change", async (ev) => {
    const f = ev.target.files && ev.target.files[0];
    ev.target.value = "";
    if (!f) return;
    if (f.size > 5 * 1024 * 1024) { showToast("File too large (5 MB max)"); return; }
    $("contacts-status").textContent = "Importing…";
    try {
      await api("api/contacts/upload", { method: "POST", body: f, headers: { "Content-Type": "text/vcard" } });
      showToast("Contacts imported");
    } catch (e) { showToast(CONTACT_STATE[e.message] || e.message); }
    loadContacts();
  });

  const shell = $("app");

  function showNumbers() {
    shell.classList.add("numbers-open");
    shell.classList.remove("has-detail");
    viewList.classList.add("hidden");
    viewDetail.classList.add("hidden");
    viewNumbers.classList.remove("hidden");
    loadNumbers();
    loadContacts();
  }

  function phoneSvg() {
    return `<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M6.6 10.8c1.4 2.8 3.8 5.1 6.6 6.6l2.2-2.2c.3-.3.7-.4 1.1-.2 1.2.4 2.5.6 3.8.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1C10.6 21 3 13.4 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.6.6 3.8.1.4 0 .8-.3 1.1L6.6 10.8z"/></svg>`;
  }

  const URGENCY_LABEL = { urgent: "Urgent", low: "Low", normal: "Normal" };
  function urgencyPill(s) {
    const u = s && URGENCY_LABEL[s.urgency] ? s.urgency : "";
    return u && u !== "normal" ? `<span class="urgency-pill ${u}">${URGENCY_LABEL[u]}</span>` : "";
  }

  function renderSummary(v) {
    const box = $("d-summary");
    const s = v.summary;
    if (!s || !(s.name || s.reason || s.callback)) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    const u = URGENCY_LABEL[s.urgency] ? s.urgency : "normal";
    const pill = $("s-urgency");
    pill.textContent = URGENCY_LABEL[u];
    pill.className = `urgency-pill ${u}`;
    $("s-name").textContent = s.name || "Not stated";
    $("s-name").classList.toggle("dim", !s.name);
    $("s-reason").textContent = s.reason || "Not stated";
    $("s-reason").classList.toggle("dim", !s.reason);
    const cb = $("s-callback");
    if (s.callback_tel) { cb.href = s.callback_tel; cb.textContent = s.callback_display || s.callback; cb.classList.remove("dim"); }
    else { cb.removeAttribute("href"); cb.textContent = "None given"; cb.classList.add("dim"); }
    $("s-note").textContent = s.source === "voice"
      ? "Summarized during the call. Check the transcript."
      : "Auto-summary from the transcript. Check the transcript.";
  }

  function renderList() {
    listEl.innerHTML = "";
    if (!items.length) {
      emptyEl.classList.remove("hidden");
      subEl.textContent = "No voicemails";
      return;
    }
    emptyEl.classList.add("hidden");
    const unheard = items.filter((x) => !x.heard).length;
    subEl.textContent = `${items.length} message${items.length === 1 ? "" : "s"}${unheard ? ` · ${unheard} new` : ""}`;

    items.forEach((v, idx) => {
      const li = document.createElement("li");
      li.className = "vm-row" + (v.heard ? "" : " unheard") + (current && current.voicemail_id === v.voicemail_id ? " active" : "");
      li.dataset.index = String(idx);
      const avatarClass = v.business ? "avatar square" : "avatar";
      const left = editMode
        ? `<input type="checkbox" class="edit-check" data-id="${esc(v.voicemail_id)}" ${selected.has(v.voicemail_id) ? "checked" : ""} aria-label="Select" />`
        : `<div class="${avatarClass}" aria-hidden="true"><svg viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M12 12a5 5 0 1 0-5-5 5 5 0 0 0 5 5zm0 2c-4.4 0-8 2-8 4.5V21h16v-2.5C20 14 16.4 14 12 14z"/></svg></div>`;
      const cb = v.tel && !editMode
        ? `<a class="cb-btn" href="${esc(v.tel)}" title="Call back" aria-label="Call back">${phoneSvg()}</a>`
        : `<span></span>`;
      li.innerHTML = `
        ${left}
        <div class="main">
          <div class="top">
            <div class="name">${v.heard ? "" : '<span class="dot-unheard"></span>'}${esc(v.caller_title)}</div>
            <div class="date">${esc(v.date_display)}</div>
          </div>
          <div class="snippet"><span class="snippet-badge">P</span>${urgencyPill(v.summary)}<span>${esc((v.summary && v.summary.reason) || mapSpeakerText(v.snippet || "No transcript"))}</span></div>
        </div>
        ${cb}`;
      const cbLink = li.querySelector(".cb-btn");
      if (cbLink) cbLink.addEventListener("click", (ev) => ev.stopPropagation());
      li.addEventListener("click", (ev) => {
        if (editMode) {
          const id = v.voicemail_id;
          if (selected.has(id)) selected.delete(id); else selected.add(id);
          renderList();
          return;
        }
        openDetail(idx);
      });
      listEl.appendChild(li);
    });
  }

  function showList() {
    current = null;
    shell.classList.remove("has-detail", "numbers-open");
    renderList();
    viewNumbers.classList.add("hidden");
    audio.pause();
    audio.removeAttribute("src");
    viewDetail.classList.add("hidden");
    viewList.classList.remove("hidden");
  }

  function openDetail(idx) {
    const v = items[idx];
    if (!v) return;
    current = v;
    shell.classList.add("has-detail");
    shell.classList.remove("numbers-open");
    viewNumbers.classList.add("hidden");
    viewList.classList.add("hidden");
    viewDetail.classList.remove("hidden");
    listEl.querySelectorAll(".vm-row").forEach((row) => {
      row.classList.toggle("active", row.dataset.index === String(idx));
    });

    $("d-when").textContent = `${v.caller_title === v.caller_pretty ? ((v.summary && v.summary.name) || "Unknown") : v.caller_title} — ${v.timestamp_display || v.date_display}`;
    const num = $("d-number");
    num.textContent = v.caller_pretty || v.caller_title;
    num.href = v.tel || "#";
    const stirEl = $("d-stir");
    const stirText = stirLabel(v.stir);
    stirEl.textContent = stirText;
    stirEl.className = `stir-badge ${(v.stir && v.stir.status) || ""}${stirText ? "" : " hidden"}`;
    $("btn-allow").classList.toggle("hidden", !v.caller_digits);
    const callTop = $("btn-call-top");
    if (v.tel) { callTop.href = v.tel; callTop.classList.remove("hidden"); }
    else { callTop.removeAttribute("href"); }

    renderSummary(v);
    $("d-transcript").innerHTML = linkPhones(polishTranscript(v.transcript || v.snippet || "No transcript"));

    // Contacts suggest: pick first linked phone in transcript that differs from caller
    const matches = [...String(v.transcript || "").matchAll(phoneRe)].map((m) => m[1]);
    const suggest = $("suggest");
    if (matches.length) {
      const pick = matches.find((m) => m.replace(/\D/g, "").slice(-10) !== String(v.caller_digits || "").slice(-10)) || matches[0];
      $("suggest-name").textContent = "Suggested";
      $("suggest-num").textContent = pick;
      suggest.classList.remove("hidden");
    } else {
      suggest.classList.add("hidden");
    }

    // Audio
    $("t-cur").textContent = "00:00";
    $("t-rem").textContent = v.duration_sec ? `-${fmt(v.duration_sec)}` : "-00:00";
    scrubber.value = 0;
    const hint = $("audio-hint");
    if (v.has_audio && v.call_id) {
      audio.src = `api/audio/${encodeURIComponent(v.call_id)}`;
      hint.textContent = mediaPlayer
        ? "Scrubber plays here. Speaker sends to the configured media player."
        : "Plays here.";
    } else {
      audio.removeAttribute("src");
      hint.textContent = mediaPlayer
        ? "No recording yet."
        : "No recording yet. Playback stays in this app.";
    }
    setPlaying(false);

    // mark heard (best-effort)
    if (!v.heard) {
      api("api/mark_heard", {
        method: "POST",
        body: JSON.stringify({ voicemail_id: v.voicemail_id, short_id: v.short_id || undefined }),
      }).then(() => { v.heard = true; }).catch(() => {});
    }
  }

  function setPlaying(on) {
    $("icon-play").classList.toggle("hidden", on);
    $("icon-pause").classList.toggle("hidden", !on);
  }

  async function refresh() {
    try {
      const data = await api("api/voicemails");
      items = data.voicemails || [];
      mediaPlayer = data.media_player || "";
      if (data.mock) subEl.dataset.mock = "1";
      renderList();
      if (current) {
        const again = items.find((x) => x.voicemail_id === current.voicemail_id);
        if (!again) showList();
      }
    } catch (e) {
      subEl.textContent = `Error: ${e.message}`;
    }
  }

  // Events
  $("btn-back").addEventListener("click", showList);
  $("btn-edit").addEventListener("click", () => {
    editMode = !editMode;
    selected.clear();
    $("btn-edit").setAttribute("aria-pressed", editMode ? "true" : "false");
    $("btn-edit").textContent = editMode ? "Done" : "Edit";
    renderList();
  });

  $("btn-clear-all").addEventListener("click", async () => {
    if (!confirm("Clear the entire voicemail Inbox?")) return;
    await api("api/clear_all", { method: "POST", body: "{}" });
    showList();
    await refresh();
  });

  $("btn-play").addEventListener("click", async () => {
    if (!audio.src) {
      // No in-app file yet; ask the configured speaker if one is set.
      if (current && current.call_id) {
        try {
          await api("api/play", { method: "POST", body: JSON.stringify({ call_id: current.call_id }) });
          $("audio-hint").textContent = "Sent play to the configured speaker.";
        } catch (e) {
          $("audio-hint").textContent = e.message;
        }
      }
      return;
    }
    if (audio.paused) { await audio.play(); setPlaying(true); }
    else { audio.pause(); setPlaying(false); }
  });

  $("btn-back15").addEventListener("click", () => {
    if (!audio.src) return;
    audio.currentTime = Math.max(0, audio.currentTime - 15);
  });

  $("btn-speaker").addEventListener("click", async () => {
    if (!current || !current.call_id) {
      $("audio-hint").textContent = "No call_id for speaker play.";
      return;
    }
    try {
      await api("api/play", { method: "POST", body: JSON.stringify({ call_id: current.call_id }) });
      $("audio-hint").textContent = "Playing on the configured speaker…";
    } catch (e) {
      $("audio-hint").textContent = e.message;
    }
  });

  $("btn-trash").addEventListener("click", async () => {
    if (!current) return;
    if (!confirm("Delete this voicemail?")) return;
    await api("api/clear_one", {
      method: "POST",
      body: JSON.stringify({ voicemail_id: current.voicemail_id }),
    });
    showList();
    await refresh();
  });

  $("btn-share").addEventListener("click", async () => {
    if (!current) return;
    const text = `${current.caller_title}\n${current.caller_pretty}\n\n${current.transcript || current.snippet || ""}`;
    try {
      if (navigator.share) await navigator.share({ title: "Voicemail", text });
      else {
        await navigator.clipboard.writeText(text);
        $("audio-hint").textContent = "Transcript copied.";
      }
    } catch (_) {}
  });

  $("btn-add-contact").addEventListener("click", async () => {
    if (!current) return;
    const line = `${current.caller_title}\t${current.caller_pretty}`;
    try { await navigator.clipboard.writeText(line); } catch (_) {}
    if (current.tel) location.href = current.tel;
    $("audio-hint").textContent = "Number copied — add in Phone/Contacts.";
  });

  $("btn-suggest-add").addEventListener("click", () => $("btn-add-contact").click());

  // Report Spam blocks the number (future calls are rejected by CallCatch Voice).
  // The voicemail itself stays, so Undo is a true undo. Trash still deletes it.
  $("btn-spam").addEventListener("click", async () => {
    if (!current) return;
    const number = current.caller_pretty || current.caller_digits;
    if (!current.caller_digits) { $("audio-hint").textContent = "No caller number to block."; return; }
    if (!confirm(`Block ${number}? Future calls from this number will be rejected before they ring.`)) return;
    try {
      await trustAddWithUndo("block", current.caller_digits, "Blocked");
    } catch (e) {
      $("audio-hint").textContent = e.message === "trust-not-configured"
        ? "Blocking needs voice_trust_url and voice_trust_token in the CallCatch options."
        : `Could not block: ${e.message}`;
    }
  });

  $("btn-allow").addEventListener("click", async () => {
    if (!current || !current.caller_digits) return;
    try {
      await trustAddWithUndo("allow", current.caller_digits, "Always allowing");
    } catch (e) {
      $("audio-hint").textContent = `Could not allow: ${e.message}`;
    }
  });

  $("toast-undo").addEventListener("click", async () => {
    const fn = toastUndo;
    toastUndo = null;
    $("toast").classList.add("hidden");
    if (fn) {
      try { await fn(); } catch (e) { showToast(`Undo failed: ${e.message}`); }
    }
  });

  $("btn-numbers").addEventListener("click", showNumbers);
  $("btn-numbers-back").addEventListener("click", showList);
  $("numbers-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const number = $("numbers-input").value.trim();
    const list = $("numbers-list").value;
    if (!number) return;
    try {
      await trustAddWithUndo(list, number, list === "block" ? "Blocked" : "Allowed");
      $("numbers-input").value = "";
      await loadNumbers();
    } catch (e) { showToast(e.message); }
  });

  audio.addEventListener("timeupdate", () => {
    const d = audio.duration || current?.duration_sec || 0;
    const c = audio.currentTime || 0;
    $("t-cur").textContent = fmt(c);
    $("t-rem").textContent = d ? `-${fmt(Math.max(0, d - c))}` : "-00:00";
    if (d) scrubber.value = String((c / d) * 100);
  });
  audio.addEventListener("ended", () => setPlaying(false));
  audio.addEventListener("play", () => setPlaying(true));
  audio.addEventListener("pause", () => setPlaying(false));
  scrubber.addEventListener("input", () => {
    const d = audio.duration || current?.duration_sec || 0;
    if (!d || !audio.src) return;
    audio.currentTime = (Number(scrubber.value) / 100) * d;
  });

  refresh();
  pollTimer = setInterval(refresh, 15000);
})();
