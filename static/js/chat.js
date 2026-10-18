(function () {
  var log = document.getElementById("chat-log");
  var form = document.getElementById("chat-form");
  if (!log || !form) return;
  var status = document.getElementById("chat-status");
  var token = document.querySelector('meta[name="csrf-token"]').content;
  var lastId = Number(log.getAttribute("data-last-id") || "0");

  function setStatus(text, isError) {
    if (!status) return;
    status.textContent = text;
    status.classList.toggle("is-error", Boolean(isError));
  }

  function append(message) {
    if (!message || document.getElementById("message-" + message.id)) return;
    var empty = log.querySelector(".empty-note");
    if (empty) empty.remove();
    var item = document.createElement("article");
    item.className = "chat-message";
    item.id = "message-" + message.id;
    var heading = document.createElement("h3");
    heading.textContent = message.author;
    var time = document.createElement("time");
    time.dateTime = message.created_at;
    time.textContent = new Date(message.created_at).toLocaleString();
    var body = document.createElement("p");
    body.textContent = message.body;
    item.append(heading, time, body);
    log.append(item);
    lastId = Math.max(lastId, message.id);
    log.scrollTop = log.scrollHeight;
  }

  function connect() {
    var protocol = location.protocol === "https:" ? "wss" : "ws";
    var socket = new WebSocket(protocol + "://" + location.host + "/ws/chat");
    socket.addEventListener("message", function (event) {
      var payload = JSON.parse(event.data);
      if (payload.type === "message") append(payload.message);
      if (payload.type === "error") setStatus(payload.error, true);
    });
    socket.addEventListener("close", function () {
      window.setTimeout(connect, 4000);
    });
  }

  window.setInterval(function () {
    fetch("/api/chat/messages?since=" + lastId, { headers: { "Accept": "application/json" } })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (payload) {
        if (!payload || !payload.messages) return;
        payload.messages.forEach(append);
      })
      .catch(function () { setStatus("New messages could not be loaded.", true); });
  }, 5000);

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var data = new FormData(form);
    setStatus("Sending…", false);
    fetch("/chat/messages", {
      method: "POST",
      body: data,
      headers: { "Accept": "application/json", "X-Requested-With": "fetch", "X-CSRF-Token": token }
    }).then(function (response) {
      return response.json().then(function (payload) { return { ok: response.ok, payload: payload }; });
    }).then(function (result) {
      if (!result.ok) {
        setStatus(result.payload.error || "The message was not sent.", true);
        return;
      }
      append(result.payload.message);
      form.elements.body.value = "";
      setStatus("Message sent.", false);
    }).catch(function () {
      form.submit();
    });
  });

  connect();
})();
