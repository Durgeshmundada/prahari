(() => {
  "use strict";

  const state = { token: "", maxFileBytes: 5 * 1024 * 1024, hasChatSample: false };
  const byId = (id) => document.getElementById(id);
  const accessForm = byId("access-form");
  const chatForm = byId("chat-form");
  const uploadForm = byId("upload-form");
  const messageInput = byId("message");
  const fileInput = byId("file");
  const uploadButton = byId("upload-button");

  function headers(extra = {}) {
    return { ...extra, "X-Prahari-Demo-Token": state.token };
  }

  function setNotice(text, kind = "") {
    const notice = byId("notice");
    notice.textContent = text;
    notice.className = `notice ${kind}`.trim();
  }

  function setConnected(connected) {
    messageInput.disabled = !connected;
    byId("send-button").disabled = !connected;
    fileInput.disabled = !connected;
    uploadButton.disabled = !connected || !state.hasChatSample || !fileInput.files.length;
  }

  function safeFileName(name) {
    let value = name.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 120);
    if (!/^[A-Za-z0-9]/.test(value)) value = `demo-${value}`.slice(0, 120);
    return value === "." || value === ".." || !value ? "demo-file.bin" : value;
  }

  async function responseJson(response) {
    let body = {};
    try { body = await response.json(); } catch (_) { /* bounded generic error below */ }
    if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
    return body;
  }

  function addMessage(text, rtt) {
    const messages = byId("messages");
    const empty = messages.querySelector(".empty");
    if (empty) empty.remove();
    const row = document.createElement("div");
    row.className = "message-row";
    const content = document.createElement("span");
    content.textContent = text;
    const timing = document.createElement("small");
    timing.textContent = `${rtt.toFixed(1)} ms round trip`;
    row.append(content, timing);
    messages.append(row);
    messages.scrollTop = messages.scrollHeight;
  }

  async function checkHealth() {
    try {
      const health = await responseJson(await fetch("/health", { cache: "no-store" }));
      state.maxFileBytes = health.max_file_bytes;
      byId("file-limit").textContent = `${(state.maxFileBytes / 1048576).toFixed(0)} MiB`;
      byId("service-state").innerHTML = "<span></span> Service online";
      byId("service-state").classList.add("online");
    } catch (_) {
      byId("service-state").textContent = "Service unavailable";
      setNotice("Laptop A cannot be reached. Check the private network and firewall.", "error");
    }
  }

  accessForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    state.token = byId("access-code").value;
    try {
      const started = performance.now();
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify({ message: "Connection check" }),
      });
      const body = await responseJson(response);
      const rtt = performance.now() - started;
      const acknowledgement = await fetch("/api/chat/ack", {
        method: "POST",
        headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify({ message_id: body.message_id, rtt_ms: rtt }),
      });
      if (acknowledgement.status !== 204) await responseJson(acknowledgement);
      state.hasChatSample = true;
      setConnected(true);
      messageInput.focus();
      setNotice("Connected. Send a message or choose a file.", "success");
    } catch (error) {
      state.token = "";
      setConnected(false);
      setNotice(error.message, "error");
    }
  });

  chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = messageInput.value.trim();
    if (!message) return;
    byId("send-button").disabled = true;
    try {
      const started = performance.now();
      const body = await responseJson(await fetch("/api/chat", {
        method: "POST",
        headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify({ message }),
      }));
      const rtt = performance.now() - started;
      const ack = await fetch("/api/chat/ack", {
        method: "POST",
        headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify({ message_id: body.message_id, rtt_ms: rtt }),
      });
      if (ack.status !== 204) await responseJson(ack);
      state.hasChatSample = true;
      addMessage(body.echo, rtt);
      messageInput.value = "";
      uploadButton.disabled = !fileInput.files.length;
      setNotice("Message returned successfully. The timing is ready for the next dashboard record.", "success");
    } catch (error) {
      setNotice(error.message, "error");
    } finally {
      byId("send-button").disabled = false;
    }
  });

  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    byId("file-label").textContent = file ? `${file.name} · ${(file.size / 1024).toFixed(1)} KiB` : "Choose a file";
    uploadButton.disabled = !file || !state.hasChatSample;
    if (file && file.size > state.maxFileBytes) {
      uploadButton.disabled = true;
      setNotice(`File is larger than the ${(state.maxFileBytes / 1048576).toFixed(0)} MiB demo limit.`, "error");
    }
  });

  uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = fileInput.files[0];
    if (!file) return;
    uploadButton.disabled = true;
    setNotice("Transferring, verifying, and publishing measurements…");
    try {
      const body = await responseJson(await fetch("/api/upload", {
        method: "POST",
        headers: headers({
          "Content-Type": "application/octet-stream",
          "X-Prahari-File-Name": safeFileName(file.name),
        }),
        body: file,
      }));
      byId("latency").textContent = `${body.chat_p95_ms.toFixed(1)} ms`;
      byId("throughput").textContent = `${body.throughput_mbps.toFixed(3)} Mbps`;
      byId("bytes").textContent = body.bytes_received.toLocaleString();
      byId("evidence").textContent = body.evidence_saved ? "Saved" : "Not saved";
      setNotice(
        body.evidence_saved
          ? "Transfer verified. Laptop A can now show this run in the PRAHARI dashboard."
          : `Transfer verified, but dashboard evidence was not saved: ${body.evidence_error || "collector disabled"}.`,
        body.evidence_saved ? "success" : "error",
      );
    } catch (error) {
      setNotice(error.message, "error");
    } finally {
      uploadButton.disabled = false;
    }
  });

  setConnected(false);
  checkHealth();
})();
