(function () {
  "use strict";

  var STORAGE_GAME = "dixit_game_id";
  var STORAGE_PLAYER = "dixit_player_id";
  var STORAGE_TOKEN = "dixit_recovery_token";

  var WS_CONNECTING = 0;
  var WS_OPEN = 1;
  var WS_CLOSING = 2;
  var WS_CLOSED = 3;

  // How often the client heartbeat fires. Must stay comfortably below
  // HEARTBEAT_TIMEOUT_S (45s) on the server.
  var PING_INTERVAL_MS = 15000;

  var state = {
    game: null,
    gameId: null,
    playerId: null,
    recoveryToken: null,
    reconnecting: false, // true between ws.onopen and first game_state
    ws: null,
    newRoomCode: null,
    reconnectTimer: null,
    pingTimer: null,
  };

  function $(id) {
    return document.getElementById(id);
  }

  function showError(msg) {
    var el = $("error");
    if (!msg) {
      el.textContent = "";
      el.classList.add("hidden");
      return;
    }
    el.textContent = msg;
    el.classList.remove("hidden");
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function phaseIs(expected) {
    return !!(state.game && state.game.phase === expected);
  }

  function api(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) {
          var d = data.detail;
          throw new Error(typeof d === "string" ? d : JSON.stringify(d));
        }
        return data;
      });
    });
  }

  function applyServerGame(msg) {
    if (msg.event === "error") {
      // The server rejects a bad recovery_token with detail "recovery_failed"
      // and immediately closes the socket. Clear the stored session so the
      // join screen can be shown.
      if (msg.detail === "recovery_failed") {
        clearSession();
        showError("Your previous session could not be restored. Please re-join.");
        render();
        return;
      }
      showError(msg.detail || "Error");
      return;
    }
    if (msg.event === "pong") {
      // Server-only liveness signal, nothing to render.
      return;
    }
    if (msg.event === "game_error") {
      // Server has already reset the relevant state (e.g. PLAY_CARDS round on
      // duplicate cards). Render the new state, then surface the error to the
      // user as a popup so it can't be missed.
      if (msg.game) state.game = msg.game;
      showError("");
      render();
      try {
        window.alert(msg.message || "An error occurred. The round has been reset.");
      } catch (e) {}
      return;
    }
    if (msg.game) {
      state.game = msg.game;
      state.reconnecting = false;
      showError("");
      render();
    }
  }

  function saveSession() {
    if (state.gameId && state.playerId && state.recoveryToken) {
      localStorage.setItem(STORAGE_GAME, state.gameId);
      localStorage.setItem(STORAGE_PLAYER, state.playerId);
      localStorage.setItem(STORAGE_TOKEN, state.recoveryToken);
    }
  }

  function clearSession() {
    if (state.reconnectTimer) {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
    if (state.pingTimer) {
      clearInterval(state.pingTimer);
      state.pingTimer = null;
    }
    localStorage.removeItem(STORAGE_GAME);
    localStorage.removeItem(STORAGE_PLAYER);
    localStorage.removeItem(STORAGE_TOKEN);
    state.gameId = null;
    state.playerId = null;
    state.recoveryToken = null;
    state.game = null;
    state.reconnecting = false;
    if (state.ws) {
      try {
        state.ws.close();
      } catch (e) {}
      state.ws = null;
    }
  }

  function isHost() {
    return state.game && state.playerId === state.game.host_id;
  }

  function me() {
    if (!state.game || !state.playerId) return null;
    return state.game.players.find(function (p) {
      return p.id === state.playerId;
    });
  }

  // Return the available_actions array sent by the backend.
  // Never empty: the backend always sends at least [] so the frontend
  // never needs to infer which actions are legal.
  function actions() {
    return (state.game && state.game.available_actions) || [];
  }

  // Card bounds from the backend; fallback only for the instant before
  // the first game_state arrives.
  function cardRange() {
    return (state.game && state.game.card_range) || { min: 1, max: 84 };
  }

  function startPings() {
    if (state.pingTimer) clearInterval(state.pingTimer);
    state.pingTimer = setInterval(function () {
      if (
        !state.ws ||
        state.ws.readyState !== WS_OPEN ||
        !state.playerId
      ) {
        return;
      }
      try {
        state.ws.send(
          JSON.stringify({ event: "ping", data: { player_id: state.playerId } })
        );
      } catch (e) {}
    }, PING_INTERVAL_MS);
  }

  function connectWs() {
    if (!state.gameId) return;
    if (
      state.ws &&
      (state.ws.readyState === WS_CONNECTING || state.ws.readyState === WS_OPEN)
    ) {
      return;
    }
    if (state.ws) {
      try {
        state.ws.close();
      } catch (e) {}
      state.ws = null;
    }

    var proto = location.protocol === "https:" ? "wss" : "ws";
    var url = proto + "://" + location.host + "/ws/" + encodeURIComponent(state.gameId);
    var ws = new WebSocket(url);
    state.ws = ws;

    ws.onopen = function () {
      try {
        // If we have a recovery_token, attempt reconnect first. The server
        // answers with a full game_state on success, or "recovery_failed" +
        // close(1008) on mismatch. If we don't have a token we fall back to
        // the passive join_room event (works for already-joined sessions
        // from older clients).
        if (state.playerId && state.recoveryToken) {
          state.reconnecting = true;
          ws.send(
            JSON.stringify({
              event: "reconnect",
              data: {
                player_id: state.playerId,
                recovery_token: state.recoveryToken,
              },
            })
          );
        } else {
          ws.send(JSON.stringify({ event: "join_room", data: {} }));
        }
      } catch (e) {}
      startPings();
    };

    ws.onmessage = function (ev) {
      try {
        applyServerGame(JSON.parse(ev.data));
      } catch (e) {
        showError("Bad message from server");
      }
    };

    ws.onclose = function () {
      if (state.ws === ws) {
        state.ws = null;
      }
      if (state.pingTimer) {
        clearInterval(state.pingTimer);
        state.pingTimer = null;
      }
      if (!state.gameId || !state.playerId) return;
      if (state.reconnectTimer) {
        clearTimeout(state.reconnectTimer);
      }
      state.reconnectTimer = setTimeout(function () {
        state.reconnectTimer = null;
        if (state.gameId && state.playerId) {
          connectWs();
          render();
        }
      }, 2000);
    };
  }

  function renderPhasePill() {
    var pill = $("phase-pill");
    if (!state.game) {
      pill.classList.add("hidden");
      return;
    }
    pill.textContent = state.game.phase.replace(/_/g, " ");
    pill.classList.remove("hidden");
  }

  function renderJoin() {
    var code = state.newRoomCode
      ? '<p class="muted">Share this room code: <strong>' +
        escapeHtml(state.newRoomCode) +
        "</strong></p>"
      : "";
    $("main").innerHTML =
      '<div class="panel">' +
      "<label>Nickname</label>" +
      '<input type="text" id="nick" maxlength="40" autocomplete="nickname" />' +
      "<label>Room code</label>" +
      '<input type="text" id="room" maxlength="32" pattern="[0-9a-fA-F]*" autocomplete="off" />' +
      '<button type="button" class="primary" id="btn-join">Join game</button>' +
      '<button type="button" class="ghost" id="btn-create">Create new game</button>' +
      code +
      "</div>";
    $("btn-join").onclick = function () {
      showError("");
      var nick = $("nick").value.trim();
      var room = $("room").value.trim();
      if (!nick || !room) {
        showError("Enter nickname and room code.");
        return;
      }
      api("/join_game", { game_id: room, nickname: nick })
        .then(function (data) {
          state.gameId = data.game_id;
          state.playerId = data.player_id;
          state.recoveryToken = data.recovery_token;
          state.game = data.game;
          state.newRoomCode = null;
          saveSession();
          connectWs();
          render();
        })
        .catch(function (e) {
          showError(e.message);
        });
    };
    $("btn-create").onclick = function () {
      showError("");
      api("/create_game")
        .then(function (data) {
          state.newRoomCode = data.game_id;
          $("room").value = data.game_id;
          renderJoin();
        })
        .catch(function (e) {
          showError(e.message);
        });
    };
  }

  function renderWaiting(msg) {
    $("main").innerHTML =
      '<div class="panel"><p class="muted">' +
      escapeHtml(msg) +
      '</p><ul class="list" id="plist"></ul><button type="button" class="ghost" id="btn-leave">Leave</button></div>';
    renderPlayerList($("plist"));
    $("btn-leave").onclick = function () {
      clearSession();
      render();
    };
  }

  function renderPlayerList(ul) {
    if (!state.game) return;
    state.game.players.forEach(function (p) {
      var li = document.createElement("li");
      var tag = p.id === state.game.host_id ? " (host)" : "";
      if (p.id === state.playerId) tag += " — you";
      if (p.connected === false) tag += " — offline";
      li.textContent = p.nickname + tag;
      ul.appendChild(li);
    });
  }

  function renderLobby() {
    if (isHost()) {
      var canStart = actions().indexOf("start_game") !== -1;
      $("main").innerHTML =
        '<div class="panel"><p class="muted">You are the host. When everyone has joined, start the game.</p>' +
        '<ul class="list" id="plist"></ul>' +
        '<button type="button" class="primary" id="btn-start"' +
        (canStart ? "" : " disabled") +
        ">Start game</button>" +
        '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';
      renderPlayerList($("plist"));
      $("btn-start").onclick = function () {
        if (actions().indexOf("start_game") === -1) return;
        showError("");
        api("/start_game", { game_id: state.gameId, player_id: state.playerId })
          .then(function (data) {
            state.game = data.game;
            render();
          })
          .catch(function (e) {
            showError(e.message);
          });
      };
      $("btn-leave").onclick = function () {
        clearSession();
        render();
      };
    } else {
      renderWaiting("Waiting for the host to start…");
    }
  }

  function renderSelectNarrator() {
    if (isHost()) {
      var canPick = actions().indexOf("select_narrator") !== -1;
      var opts = state.game.players
        .map(function (p) {
          return (
            '<option value="' +
            escapeHtml(p.id) +
            '">' +
            escapeHtml(p.nickname) +
            "</option>"
          );
        })
        .join("");
      $("main").innerHTML =
        '<div class="panel"><label>Storyteller this round</label>' +
        '<select id="narr"' +
        (canPick ? "" : " disabled") +
        ">" +
        opts +
        '</select><button type="button" class="primary" id="btn-narr"' +
        (canPick ? "" : " disabled") +
        ">Choose storyteller</button>" +
        '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';
      $("btn-narr").onclick = function () {
        if (actions().indexOf("select_narrator") === -1) return;
        showError("");
        var nid = $("narr").value;
        api("/select_narrator", {
          game_id: state.gameId,
          player_id: state.playerId,
          narrator_id: nid,
        })
          .then(function (data) {
            state.game = data.game;
            render();
          })
          .catch(function (e) {
            showError(e.message);
          });
      };
      $("btn-leave").onclick = function () {
        clearSession();
        render();
      };
    } else {
      renderWaiting("Waiting for the host to pick the storyteller…");
    }
  }

  function renderPlayCards() {
    var p = me();
    if (!p) {
      renderWaiting("Loading…");
      return;
    }
    if (p.card_played != null) {
      var allPlayed = actions().indexOf("next_phase") !== -1;
      var label = allPlayed
        ? "All cards played. Continue to voting when ready."
        : "Waiting for other players to play a card…";
      renderHostContinue(label, allPlayed);
      return;
    }
    var range = cardRange();
    var canPlay = actions().indexOf("submit_card") !== -1;
    $("main").innerHTML =
      '<div class="panel"><label>Your card number (' +
      range.min +
      "–" +
      range.max +
      ')</label>' +
      '<input type="number" id="cardn" min="' +
      range.min +
      '" max="' +
      range.max +
      '" step="1" inputmode="numeric"' +
      (canPlay ? "" : " disabled") +
      " />" +
      '<button type="button" class="primary" id="btn-card"' +
      (canPlay ? "" : " disabled") +
      ">Play card</button>" +
      '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';
    $("btn-card").onclick = function () {
      if (actions().indexOf("submit_card") === -1) return;
      showError("");
      var r = cardRange();
      var n = parseInt($("cardn").value, 10);
      if (isNaN(n) || n < r.min || n > r.max) {
        showError("Enter a valid card number.");
        return;
      }
      api("/submit_card", {
        game_id: state.gameId,
        player_id: state.playerId,
        card_number: n,
      })
        .then(function (data) {
          state.game = data.game;
          render();
        })
        .catch(function (e) {
          showError(e.message);
        });
    };
    $("btn-leave").onclick = function () {
      clearSession();
      render();
    };
  }

  function renderVote() {
    var p = me();
    if (!p) {
      renderWaiting("Loading…");
      return;
    }
    // The narrator never votes; if they're also the host they still need a
    // Continue button to move VOTE -> REVEAL_VOTES once everyone has voted.
    if (state.playerId === state.game.narrator_id) {
      var allVoted = actions().indexOf("next_phase") !== -1;
      var nlabel = allVoted
        ? "All votes are in. Continue to reveal."
        : "You are the storyteller — wait while others vote.";
      renderHostContinue(nlabel, allVoted);
      return;
    }
    if (p.vote != null) {
      var allVoted2 = actions().indexOf("next_phase") !== -1;
      var vlabel = allVoted2
        ? "All votes are in. Continue to reveal."
        : "Waiting for other votes…";
      renderHostContinue(vlabel, allVoted2);
      return;
    }
    var cards = state.game.cards_on_table || [];
    if (!cards.length) {
      renderWaiting("No cards on the table yet.");
      return;
    }
    var canVote = actions().indexOf("submit_vote") !== -1;
    var myCard = p.card_played;
    var btns = cards
      .map(function (c) {
        var isOwnCard = c === myCard;
        var cls = "vote-btn" + (isOwnCard ? " own-card" : "");
        var isDisabled = !canVote || isOwnCard;
        var label = isOwnCard ? c + "\u00a0(yours)" : String(c);
        return (
          '<button type="button" class="' + cls + '" data-card="' +
          c + '"' +
          (isDisabled ? " disabled" : "") +
          ">" +
          label +
          "</button>"
        );
      })
      .join("");
    $("main").innerHTML =
      '<div class="panel"><p class="muted">Vote for one card.</p>' +
      '<div class="vote-grid">' +
      btns +
      '</div><button type="button" class="ghost" id="btn-leave" style="margin-top:1rem">Leave</button></div>';
    document.querySelectorAll(".vote-btn").forEach(function (btn) {
      btn.onclick = function () {
        if (actions().indexOf("submit_vote") === -1) return;
        if (btn.disabled) return;
        showError("");
        var card = parseInt(btn.getAttribute("data-card"), 10);
        api("/submit_vote", {
          game_id: state.gameId,
          player_id: state.playerId,
          card_number: card,
        })
          .then(function (data) {
            state.game = data.game;
            render();
          })
          .catch(function (e) {
            showError(e.message);
          });
      };
    });
    $("btn-leave").onclick = function () {
      clearSession();
      render();
    };
  }

  function renderHostContinue(label, canAdvanceOverride) {
    // The backend tells us whether next_phase is available; the frontend
    // only adds the identity check (is this player the host?).
    var phaseAllowed = isHost() && actions().indexOf("next_phase") !== -1;
    var canAdvance =
      phaseAllowed &&
      (typeof canAdvanceOverride === "boolean" ? canAdvanceOverride : true);
    if (isHost()) {
      $("main").innerHTML =
        '<div class="panel"><p class="muted">' +
        escapeHtml(label) +
        '</p><button type="button" class="primary" id="btn-next"' +
        (canAdvance ? "" : " disabled") +
        ">Continue</button>" +
        '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';
      $("btn-next").onclick = function () {
        if (!isHost() || actions().indexOf("next_phase") === -1) return;
        showError("");
        api("/next_phase", { game_id: state.gameId, player_id: state.playerId })
          .then(function (data) {
            state.game = data.game;
            render();
          })
          .catch(function (e) {
            showError(e.message);
          });
      };
      $("btn-leave").onclick = function () {
        clearSession();
        render();
      };
    } else {
      renderWaiting(label);
    }
  }

  function renderLeaderboard() {
    var rows = state.game.players
      .slice()
      .sort(function (a, b) {
        return b.score - a.score || a.id.localeCompare(b.id);
      })
      .map(function (p) {
        return (
          '<div class="score-row"><span>' +
          escapeHtml(p.nickname) +
          (p.id === state.playerId ? " (you)" : "") +
          "</span><strong>" +
          p.score +
          "</strong></div>"
        );
      })
      .join("");
    var canNext = isHost() && actions().indexOf("next_phase") !== -1;
    var hostBtn = isHost()
      ? '<button type="button" class="primary" id="btn-next"' +
        (canNext ? "" : " disabled") +
        ">Next</button>"
      : "";
    $("main").innerHTML =
      '<div class="panel"><p class="muted">Scores this game</p>' +
      rows +
      hostBtn +
      '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';
    var next = $("btn-next");
    if (next) {
      next.onclick = function () {
        if (!isHost() || actions().indexOf("next_phase") === -1) return;
        showError("");
        api("/next_phase", { game_id: state.gameId, player_id: state.playerId })
          .then(function (data) {
            state.game = data.game;
            render();
          })
          .catch(function (e) {
            showError(e.message);
          });
      };
    }
    $("btn-leave").onclick = function () {
      clearSession();
      render();
    };
  }

  function renderReconnecting() {
    $("main").innerHTML =
      '<div class="panel"><p class="muted">Reconnecting…</p>' +
      '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';
    $("btn-leave").onclick = function () {
      clearSession();
      render();
    };
  }

  function render() {
    renderPhasePill();
    if (!state.gameId || !state.playerId) {
      renderJoin();
      return;
    }
    if (
      !state.ws ||
      state.ws.readyState === WS_CLOSING ||
      state.ws.readyState === WS_CLOSED
    ) {
      connectWs();
    }
    if (!state.game) {
      if (state.reconnecting || state.recoveryToken) {
        renderReconnecting();
      } else {
        $("main").innerHTML =
          '<div class="panel"><p class="muted">Connecting…</p>' +
          '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';
        $("btn-leave").onclick = function () {
          clearSession();
          render();
        };
      }
      return;
    }

    var ph = state.game.phase;
    if (ph === "LOBBY") {
      renderLobby();
    } else if (ph === "SELECT_NARRATOR") {
      renderSelectNarrator();
    } else if (ph === "PLAY_CARDS") {
      renderPlayCards();
    } else if (ph === "VOTE") {
      renderVote();
    } else if (
      ph === "REVEAL_VOTES" ||
      ph === "REVEAL_NARRATOR" ||
      ph === "SCORE_BASE" ||
      ph === "SCORE_BONUS" ||
      ph === "NEXT_ROUND"
    ) {
      renderHostContinue("Follow the table in the room. Host advances when ready.");
    } else if (ph === "LEADERBOARD") {
      renderLeaderboard();
    } else {
      renderWaiting("This phase is not shown in the companion yet.");
    }
  }

  function boot() {
    var gid = localStorage.getItem(STORAGE_GAME);
    var pid = localStorage.getItem(STORAGE_PLAYER);
    var tok = localStorage.getItem(STORAGE_TOKEN);
    if (gid && pid) {
      state.gameId = gid;
      state.playerId = pid;
      state.recoveryToken = tok;
      state.game = null;
      state.reconnecting = !!tok;
    }
    render();
  }

  boot();
})();
