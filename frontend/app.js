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

  // Transient UI-only vote state — never serialised or sent over the wire.
  // pendingVote: null = idle; number[] = cards being staged in the grid.
  // previewReady: true when the player has confirmed their grid selection and
  //   we should render VOTE_PREVIEW rather than the grid.
  var pendingVote = null;
  var previewReady = false;

  function resetPendingVote() {
    pendingVote = null;
    previewReady = false;
  }

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
    var phaseText = state.game.phase.replace(/_/g, " ");
    // Show submission step for TURN_SUBMISSION phase
    if (state.game.phase === "TURN_SUBMISSION" && state.game.submission_step) {
      phaseText = state.game.submission_step === "declaration"
        ? "TURN: DECLARE CARD"
        : "TURN: VOTE";
    }
    pill.textContent = phaseText;
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
    var narId = state.game.narrator_id;
    var confirmed = state.game.narrator_confirmed;
    var amNarrator = narId === state.playerId;

    // ── Narrator who hasn't confirmed yet ──────────────────────────────────
    if (amNarrator && !confirmed) {
      $("main").innerHTML =
        '<div class="panel">' +
        "<label>You have been chosen as storyteller!</label>" +
        '<p style="margin:0.5rem 0 1rem">Accept your role to let the host start the round.</p>' +
        '<button type="button" class="primary" id="btn-confirm-narrator">Accept as storyteller</button>' +
        "</div>";
      $("btn-confirm-narrator").onclick = function () {
        showError("");
        api("/confirm_narrator", {
          game_id: state.gameId,
          player_id: state.playerId,
        })
          .then(function (data) {
            state.game = data.game;
            render();
          })
          .catch(function (e) {
            showError(e.message);
          });
      };
      return;
    }

    // ── Host view ───────────────────────────────────────────────────────────
    if (isHost()) {
      var canPick = actions().indexOf("select_narrator") !== -1;
      var canAdvance = actions().indexOf("next_phase") !== -1;

      if (narId === null || canPick) {
        // No narrator picked yet (or can still pick) — show dropdown
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
          '<div class="panel"><label>Choose the storyteller for this round</label>' +
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
      } else if (!confirmed) {
        // Narrator selected but hasn't confirmed yet
        var narNick = escapeHtml(
          (state.game.players.find(function (p) { return p.id === narId; }) || {}).nickname || "?"
        );
        $("main").innerHTML =
          '<div class="panel">' +
          "<label>Waiting for " + narNick + " to accept the storyteller role…</label>" +
          "</div>";
      } else {
        // Narrator confirmed — host can advance
        var narNick2 = escapeHtml(
          (state.game.players.find(function (p) { return p.id === narId; }) || {}).nickname || "?"
        );
        $("main").innerHTML =
          '<div class="panel">' +
          "<label>" + narNick2 + " accepted as storyteller.</label>" +
          '<button type="button" class="primary" id="btn-next"' +
          (canAdvance ? "" : " disabled") +
          ">Continue to card phase</button>" +
          "</div>";
        $("btn-next").onclick = function () {
          if (!canAdvance) return;
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
      return;
    }

    // ── Non-host, non-narrator ──────────────────────────────────────────────
    if (narId === null) {
      renderWaiting("Waiting for the host to pick the storyteller…");
    } else if (!confirmed) {
      var narNick3 = escapeHtml(
        (state.game.players.find(function (p) { return p.id === narId; }) || {}).nickname || "?"
      );
      renderWaiting("Waiting for " + narNick3 + " to accept the storyteller role…");
    } else {
      var narNick4 = escapeHtml(
        (state.game.players.find(function (p) { return p.id === narId; }) || {}).nickname || "?"
      );
      renderWaiting(narNick4 + " accepted. Waiting for the host to start the round…");
    }
  }

  function renderPlayCards() {
    // Legacy function - redirects to new turn submission flow
    renderTurnSubmission();
  }

  // ---------------------------------------------------------------------------
  // TURN SUBMISSION - Unified declaration + voting wizard
  // ---------------------------------------------------------------------------

  function renderTurnSubmission() {
    var p = me();
    if (!p) {
      renderWaiting("Loading…");
      return;
    }

    var step = state.game.submission_step; // "declaration" or "voting"
    var isNarrator = state.playerId === state.game.narrator_id;

    // Build wizard step indicator
    var declarationStepClass = step === "declaration" ? "active" : "completed";
    var votingStepClass = step === "voting" ? "active" : "";
    var stepIndicator =
      '<div class="wizard-steps">' +
      '<div class="wizard-step ' + declarationStepClass + '">1. Declare card</div>' +
      '<div class="wizard-step ' + votingStepClass + '">2. Vote</div>' +
      '</div>';

    if (step === "declaration") {
      renderDeclarationStep(p, isNarrator, stepIndicator);
    } else if (step === "voting") {
      renderVotingStep(p, isNarrator, stepIndicator);
    } else {
      renderWaiting("Waiting for turn submission to begin…");
    }
  }

  // Declaration sub-step: player declares which card they played
  function renderDeclarationStep(p, isNarrator, stepIndicator) {
    // Already declared - waiting for others
    if (p.card_played != null) {
      var live = state.game.players.filter(function(pl) { return pl.connected; });
      var declared = live.filter(function(pl) { return pl.card_played != null; });
      var waiting = live.length - declared.length;
      
      var statusMsg = waiting > 0
        ? "Waiting for " + waiting + " player" + (waiting === 1 ? "" : "s") + " to declare their card…"
        : "All cards declared! Validating…";

      $("main").innerHTML =
        '<div class="panel">' + stepIndicator +
        '<p class="muted">' + escapeHtml(statusMsg) + '</p>' +
        '<div class="declared-card-banner">' +
        '<div class="card-number">' + escapeHtml(String(p.card_played)) + '</div>' +
        '<div class="card-label">Your declared card</div>' +
        '</div>' +
        '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';
      $("btn-leave").onclick = function() { clearSession(); render(); };
      return;
    }

    // Show card picker grid
    var range = cardRange();
    var canDeclare = actions().indexOf("submit_card") !== -1;

    // Create grid of card numbers (7 columns)
    var gridBtns = "";
    for (var n = range.min; n <= range.max; n++) {
      gridBtns +=
        '<button type="button" data-card="' + n + '"' +
        (canDeclare ? "" : " disabled") +
        '>' + n + '</button>';
    }

    $("main").innerHTML =
      '<div class="panel">' + stepIndicator +
      '<p class="muted">Select the card number you played:</p>' +
      '<div class="card-picker-grid">' + gridBtns + '</div>' +
      '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';

    document.querySelectorAll(".card-picker-grid button").forEach(function(btn) {
      btn.onclick = function() {
        if (btn.disabled) return;
        var cardNum = parseInt(btn.getAttribute("data-card"), 10);
        submitCardDeclaration(cardNum);
      };
    });

    $("btn-leave").onclick = function() { clearSession(); render(); };
  }

  function submitCardDeclaration(cardNumber) {
    showError("");
    api("/submit_card", {
      game_id: state.gameId,
      player_id: state.playerId,
      card_number: cardNumber,
    })
      .then(function(data) {
        state.game = data.game;
        render();
      })
      .catch(function(e) {
        showError(e.message);
      });
  }

  // Voting sub-step: player votes for narrator's card
  function renderVotingStep(p, isNarrator, stepIndicator) {
    var votesPerPlayer = state.game.votes_per_player || 1;
    var myVotes = p.votes || [];
    var myDeclaredCard = p.card_played;

    // Narrator doesn't vote
    if (isNarrator) {
      renderNarratorVotingWait(stepIndicator);
      return;
    }

    // Player already voted and not editing
    if (myVotes.length > 0 && pendingVote === null) {
      renderVoteSubmittedWithBanner(myVotes, myDeclaredCard, stepIndicator);
      return;
    }

    // Vote preview (before confirming)
    if (pendingVote !== null && previewReady) {
      renderVotePreviewWithBanner(myDeclaredCard, stepIndicator);
      return;
    }

    // Show vote grid
    renderVoteGridWithBanner(p, myDeclaredCard, stepIndicator, votesPerPlayer);
  }

  function renderNarratorVotingWait(stepIndicator) {
    var canAdvance = isHost() && actions().indexOf("next_phase") !== -1;
    var label = isHost()
      ? (canAdvance ? "All votes are in — continue when ready." : "Waiting for all players to vote…")
      : "You are the storyteller — waiting for votes.";

    var html = '<div class="panel">' + stepIndicator +
      '<p class="muted">' + escapeHtml(label) + '</p>';

    if (canAdvance) {
      html += '<button type="button" class="primary" id="btn-next" style="margin-top:0.75rem">Continue to reveal</button>';
    }

    html += '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';
    $("main").innerHTML = html;

    var btnNext = $("btn-next");
    if (btnNext) {
      btnNext.onclick = function() {
        if (!isHost() || actions().indexOf("next_phase") === -1) return;
        showError("");
        api("/next_phase", { game_id: state.gameId, player_id: state.playerId })
          .then(function(data) { state.game = data.game; render(); })
          .catch(function(e) { showError(e.message); });
      };
    }

    $("btn-leave").onclick = function() { clearSession(); render(); };
  }

  function renderVoteSubmittedWithBanner(votes, declaredCard, stepIndicator) {
    var canAdvance = isHost() && actions().indexOf("next_phase") !== -1;
    var voteWord = votes.length === 1 ? "vote" : "votes";

    var tiles = votes.map(function(c) {
      return '<div class="vote-preview-card">' + escapeHtml(String(c)) + '</div>';
    }).join("");

    var declaredBanner = declaredCard != null
      ? '<div class="declared-card-banner">' +
        '<div class="card-number">' + escapeHtml(String(declaredCard)) + '</div>' +
        '<div class="card-label">Your played card</div></div>'
      : '';

    var html = '<div class="panel">' + stepIndicator +
      declaredBanner +
      '<p class="muted">Your ' + voteWord + ' (submitted)</p>' +
      '<div class="vote-preview">' + tiles + '</div>' +
      '<button type="button" class="ghost" id="btn-change-vote" style="margin-top:0.75rem">Change vote</button>';

    if (canAdvance) {
      html += '<button type="button" class="primary" id="btn-next" style="margin-top:0.75rem">Continue to reveal</button>';
    }

    html += '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';
    $("main").innerHTML = html;

    $("btn-change-vote").onclick = function() {
      pendingVote = votes.slice();
      previewReady = false;
      render();
    };

    var btnNext = $("btn-next");
    if (btnNext) {
      btnNext.onclick = function() {
        if (!isHost() || actions().indexOf("next_phase") === -1) return;
        showError("");
        api("/next_phase", { game_id: state.gameId, player_id: state.playerId })
          .then(function(data) { state.game = data.game; render(); })
          .catch(function(e) { showError(e.message); });
      };
    }

    $("btn-leave").onclick = function() { clearSession(); render(); };
  }

  function renderVotePreviewWithBanner(declaredCard, stepIndicator) {
    var selection = pendingVote || [];
    var canSubmit = actions().indexOf("update_vote") !== -1 ||
                    actions().indexOf("submit_vote") !== -1;

    var tiles = selection.map(function(c) {
      return '<div class="vote-preview-card">' + escapeHtml(String(c)) + '</div>';
    }).join("");

    var voteWord = selection.length === 1 ? "vote" : "votes";

    var declaredBanner = declaredCard != null
      ? '<div class="declared-card-banner">' +
        '<div class="card-number">' + escapeHtml(String(declaredCard)) + '</div>' +
        '<div class="card-label">Your played card</div></div>'
      : '';

    $("main").innerHTML =
      '<div class="panel">' + stepIndicator +
      declaredBanner +
      '<p class="muted">Confirm your ' + voteWord + '?</p>' +
      '<div class="vote-preview">' + tiles + '</div>' +
      '<button type="button" class="primary" id="btn-confirm"' +
      (canSubmit ? "" : " disabled") + '>Confirm</button>' +
      '<button type="button" class="ghost" id="btn-change">Change</button></div>';

    $("btn-confirm").onclick = function() {
      showError("");
      api("/update_vote", {
        game_id: state.gameId,
        player_id: state.playerId,
        card_numbers: pendingVote,
      })
        .then(function(data) {
          resetPendingVote();
          state.game = data.game;
          render();
        })
        .catch(function(e) { showError(e.message); });
    };

    $("btn-change").onclick = function() {
      previewReady = false;
      render();
    };
  }

  function renderVoteGridWithBanner(p, declaredCard, stepIndicator, votesPerPlayer) {
    var cards = state.game.cards_on_table || [];
    if (!cards.length) {
      renderWaiting("No cards on the table yet.");
      return;
    }

    if (pendingVote === null) pendingVote = [];

    var canVote = actions().indexOf("submit_vote") !== -1 ||
                  actions().indexOf("update_vote") !== -1;
    var myCard = p.card_played;
    var selected = pendingVote;

    var gridInstruction = votesPerPlayer === 1
      ? "Vote for the storyteller's card:"
      : "Vote for up to " + votesPerPlayer + " cards" +
        (selected.length > 0 ? " (" + selected.length + " selected):" : ":");

    var btns = cards.map(function(c) {
      var isOwnCard = c === myCard;
      var isSelected = selected.indexOf(c) !== -1;
      var isDisabled = !canVote || isOwnCard ||
                       (!isSelected && selected.length >= votesPerPlayer);
      var cls = "vote-btn" +
                (isOwnCard ? " own-card" : "") +
                (isSelected ? " selected" : "");
      var label = isOwnCard ? c + "\u00a0(yours)" : String(c);
      return (
        '<button type="button" class="' + cls + '" data-card="' + c + '"' +
        (isDisabled ? " disabled" : "") + '>' + label + '</button>'
      );
    }).join("");

    var confirmBtn = (votesPerPlayer > 1 && selected.length >= 1)
      ? '<button type="button" class="primary" id="btn-confirm-sel" style="margin-top:1rem">' +
        "Confirm " + selected.length + " vote" + (selected.length !== 1 ? "s" : "") +
        "</button>"
      : "";

    var declaredBanner = declaredCard != null
      ? '<div class="declared-card-banner">' +
        '<div class="card-number">' + escapeHtml(String(declaredCard)) + '</div>' +
        '<div class="card-label">Your played card</div></div>'
      : '';

    $("main").innerHTML =
      '<div class="panel">' + stepIndicator +
      declaredBanner +
      '<p class="muted">' + escapeHtml(gridInstruction) + '</p>' +
      '<div class="vote-grid">' + btns + '</div>' +
      confirmBtn +
      '<button type="button" class="ghost" id="btn-leave" style="margin-top:1rem">Leave</button></div>';

    document.querySelectorAll(".vote-btn").forEach(function(btn) {
      btn.onclick = function() {
        if (btn.disabled) return;
        var card = parseInt(btn.getAttribute("data-card"), 10);
        var idx = pendingVote.indexOf(card);
        if (idx !== -1) {
          pendingVote.splice(idx, 1);
        } else {
          pendingVote.push(card);
        }
        if (votesPerPlayer === 1 && pendingVote.length === 1) {
          previewReady = true;
        }
        if (votesPerPlayer > 1 && pendingVote.length >= votesPerPlayer) {
          previewReady = true;
        }
        render();
      };
    });

    var confirmSel = $("btn-confirm-sel");
    if (confirmSel) {
      confirmSel.onclick = function() {
        if (pendingVote.length >= 1) {
          previewReady = true;
          render();
        }
      };
    }

    $("btn-leave").onclick = function() { clearSession(); render(); };
  }

  // ---------------------------------------------------------------------------
  // Legacy renderVote - kept for backwards compatibility if needed
  // ---------------------------------------------------------------------------

  function renderVote() {
    var p = me();
    if (!p) {
      renderWaiting("Loading…");
      return;
    }

    var votesPerPlayer = state.game.votes_per_player || 1;
    var myVotes = p.votes || [];

    // The narrator never votes; show waiting / host-advance panel.
    if (state.playerId === state.game.narrator_id) {
      renderVoteWaiting();
      return;
    }

    // Player already has at least one confirmed vote and is not editing.
    if (myVotes.length > 0 && pendingVote === null) {
      renderVoteSubmitted(myVotes);
      return;
    }

    // VOTE_PREVIEW: selection staged and player confirmed it in the grid.
    if (pendingVote !== null && previewReady) {
      renderVotePreview();
      return;
    }

    // Vote grid (fresh or mid-selection for multi-vote).
    var cards = state.game.cards_on_table || [];
    if (!cards.length) {
      renderWaiting("No cards on the table yet.");
      return;
    }

    // Initialise selection from existing votes when entering "change" mode.
    if (pendingVote === null) pendingVote = [];

    var canVote = actions().indexOf("submit_vote") !== -1 ||
                  actions().indexOf("update_vote") !== -1;
    var myCard = p.card_played;
    var selected = pendingVote; // array (may be empty)

    var gridInstruction = votesPerPlayer === 1
      ? "Vote for one card."
      : "Vote for up to " + votesPerPlayer + " cards" +
        (selected.length > 0 ? " (" + selected.length + " selected)." : ".");

    var btns = cards
      .map(function (c) {
        var isOwnCard = c === myCard;
        var isSelected = selected.indexOf(c) !== -1;
        var isDisabled = !canVote || isOwnCard ||
                         (!isSelected && selected.length >= votesPerPlayer);
        var cls = "vote-btn" +
                  (isOwnCard ? " own-card" : "") +
                  (isSelected ? " selected" : "");
        var label = isOwnCard ? c + "\u00a0(yours)" : String(c);
        return (
          '<button type="button" class="' + cls + '" data-card="' + c + '"' +
          (isDisabled ? " disabled" : "") +
          ">" + label + "</button>"
        );
      })
      .join("");

    // "Confirm selection" button — appears for multi-vote once ≥1 card chosen.
    var confirmBtn = (votesPerPlayer > 1 && selected.length >= 1)
      ? '<button type="button" class="primary" id="btn-confirm-sel" style="margin-top:1rem">' +
        "Confirm " + selected.length + " vote" + (selected.length !== 1 ? "s" : "") +
        "</button>"
      : "";

    $("main").innerHTML =
      '<div class="panel"><p class="muted">' + escapeHtml(gridInstruction) + "</p>" +
      '<div class="vote-grid">' + btns + "</div>" +
      confirmBtn +
      '<button type="button" class="ghost" id="btn-leave" style="margin-top:1rem">Leave</button></div>';

    document.querySelectorAll(".vote-btn").forEach(function (btn) {
      btn.onclick = function () {
        if (btn.disabled) return;
        var card = parseInt(btn.getAttribute("data-card"), 10);
        var idx = pendingVote.indexOf(card);
        if (idx !== -1) {
          // Deselect.
          pendingVote.splice(idx, 1);
        } else {
          pendingVote.push(card);
        }
        // For single-vote, immediately advance to VOTE_PREVIEW on selection.
        if (votesPerPlayer === 1 && pendingVote.length === 1) {
          previewReady = true;
        }
        // For multi-vote, auto-advance when the maximum is reached.
        if (votesPerPlayer > 1 && pendingVote.length >= votesPerPlayer) {
          previewReady = true;
        }
        render();
      };
    });

    var confirmSel = $("btn-confirm-sel");
    if (confirmSel) {
      confirmSel.onclick = function () {
        if (pendingVote.length >= 1) {
          previewReady = true;
          render();
        }
      };
    }

    $("btn-leave").onclick = function () {
      clearSession();
      render();
    };
  }

  // Shown to a non-narrator player whose vote is confirmed. Keeps the selected
  // card(s) visible in large format so the player knows what they voted for.
  function renderVoteSubmitted(votes) {
    var canAdvance = isHost() && actions().indexOf("next_phase") !== -1;
    var voteWord = votes.length === 1 ? "vote" : "votes";

    var tiles = votes
      .map(function (c) {
        return '<div class="vote-preview-card">' + escapeHtml(String(c)) + "</div>";
      })
      .join("");

    var html =
      '<div class="panel">' +
      '<p class="muted">Your ' + voteWord + " (submitted)</p>" +
      '<div class="vote-preview">' + tiles + "</div>" +
      '<button type="button" class="ghost" id="btn-change-vote" style="margin-top:0.75rem">Change vote</button>';

    if (canAdvance) {
      html +=
        '<button type="button" class="primary" id="btn-next" style="margin-top:0.75rem">Continue</button>';
    }

    html += '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';
    $("main").innerHTML = html;

    $("btn-change-vote").onclick = function () {
      pendingVote = votes.slice();
      previewReady = false;
      render();
    };

    var btnNext = $("btn-next");
    if (btnNext) {
      btnNext.onclick = function () {
        if (!isHost() || actions().indexOf("next_phase") === -1) return;
        showError("");
        api("/next_phase", { game_id: state.gameId, player_id: state.playerId })
          .then(function (data) { state.game = data.game; render(); })
          .catch(function (e) { showError(e.message); });
      };
    }

    $("btn-leave").onclick = function () {
      clearSession();
      render();
    };
  }

  // Waiting panel for the narrator (who never votes) during the VOTE phase.
  // Host sees Continue when all active voters have voted.
  function renderVoteWaiting() {
    var canAdvance = isHost() && actions().indexOf("next_phase") !== -1;

    var label = isHost()
      ? (canAdvance ? "All votes are in — continue when ready." : "Waiting for all players to vote…")
      : "You are the storyteller — waiting for the host.";

    var html = '<div class="panel"><p class="muted">' + escapeHtml(label) + "</p>";

    if (canAdvance) {
      html += '<button type="button" class="primary" id="btn-next" style="margin-top:0.75rem">Continue</button>';
    }

    html += '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';
    $("main").innerHTML = html;

    var btnNext = $("btn-next");
    if (btnNext) {
      btnNext.onclick = function () {
        if (!isHost() || actions().indexOf("next_phase") === -1) return;
        showError("");
        api("/next_phase", { game_id: state.gameId, player_id: state.playerId })
          .then(function (data) { state.game = data.game; render(); })
          .catch(function (e) { showError(e.message); });
      };
    }

    $("btn-leave").onclick = function () {
      clearSession();
      render();
    };
  }

  // Renders the VOTE_PREVIEW UI state: selected card(s) shown large with
  // Confirm (submits via update_vote) and Change (back to grid) actions.
  function renderVotePreview() {
    var selection = pendingVote || [];
    var canSubmit = actions().indexOf("update_vote") !== -1 ||
                    actions().indexOf("submit_vote") !== -1;
    var tiles = selection
      .map(function (c) {
        return '<div class="vote-preview-card">' + escapeHtml(String(c)) + "</div>";
      })
      .join("");
    var voteWord = selection.length === 1 ? "vote" : "votes";
    $("main").innerHTML =
      '<div class="panel">' +
      '<p class="muted">Confirm your ' + voteWord + '?</p>' +
      '<div class="vote-preview">' + tiles + "</div>" +
      '<button type="button" class="primary" id="btn-confirm"' +
      (canSubmit ? "" : " disabled") +
      ">Confirm</button>" +
      '<button type="button" class="ghost" id="btn-change">Change</button>' +
      "</div>";

    $("btn-confirm").onclick = function () {
      showError("");
      // update_vote replaces the vote list atomically (works for both first
      // submission and editing). Requires at least one card selected.
      api("/update_vote", {
        game_id: state.gameId,
        player_id: state.playerId,
        card_numbers: pendingVote,
      })
        .then(function (data) {
          resetPendingVote();
          state.game = data.game;
          render();
        })
        .catch(function (e) {
          showError(e.message);
        });
    };

    $("btn-change").onclick = function () {
      // Return to the grid, keeping the current selection visible.
      previewReady = false;
      render();
    };
  }

  function renderRevealVotes() {
    var game = state.game;
    var narratorId = game.narrator_id;

    var rows = game.players
      .map(function (p) {
        var isMe = p.id === state.playerId;
        var isNarrator = p.id === narratorId;

        var nameParts = escapeHtml(p.nickname);
        if (isNarrator) nameParts += ' <span class="muted">(storyteller)</span>';
        if (isMe) nameParts += ' <span class="muted">(you)</span>';

        var pills;
        if (isNarrator) {
          // Narrator's card is not revealed until REVEAL_NARRATOR.
          pills = '<span class="muted">—</span>';
        } else {
          var votes = p.votes || [];
          if (votes.length === 0) {
            pills = '<span class="muted">—</span>';
          } else {
            pills = votes
              .map(function (c) {
                return '<span class="reveal-pill' + (isMe ? " my-vote" : "") + '">' +
                  escapeHtml(String(c)) + "</span>";
              })
              .join("");
          }
        }

        return (
          '<div class="reveal-row"><span>' +
          nameParts +
          '</span><span class="reveal-pills">' +
          pills +
          "</span></div>"
        );
      })
      .join("");

    var canAdvance = isHost() && actions().indexOf("next_phase") !== -1;
    var hostBtn = isHost()
      ? '<button type="button" class="primary" id="btn-next"' +
        (canAdvance ? "" : " disabled") +
        ">Continue</button>"
      : "";

    $("main").innerHTML =
      '<div class="panel"><p class="muted">Who voted what</p>' +
      rows +
      hostBtn +
      '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';

    var btnNext = $("btn-next");
    if (btnNext) {
      btnNext.onclick = function () {
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

  function renderScoring(step) {
    var game = state.game;
    var deltas = step === "base" ? (game.last_base_delta || {}) : (game.last_bonus_delta || {});
    var label = step === "base" ? "Base points this round" : "Bonus points this round";

    var rows = game.players
      .slice()
      .sort(function (a, b) {
        var da = deltas[a.id] || 0;
        var db = deltas[b.id] || 0;
        return db - da || a.id.localeCompare(b.id);
      })
      .map(function (p) {
        var delta = deltas[p.id] || 0;
        var name = escapeHtml(p.nickname) + (p.id === state.playerId ? " (you)" : "");
        var pts = (delta > 0 ? "+" : "") + delta;
        return (
          '<div class="score-row"><span>' +
          name +
          "</span><strong" +
          (delta === 0 ? ' class="muted"' : "") +
          ">" +
          pts +
          "</strong></div>"
        );
      })
      .join("");

    var canNext = isHost() && actions().indexOf("next_phase") !== -1;
    var hostBtn = isHost()
      ? '<button type="button" class="primary" id="btn-next"' +
        (canNext ? "" : " disabled") +
        ">Continue</button>"
      : "";

    $("main").innerHTML =
      '<div class="panel"><p class="muted">' +
      label +
      "</p>" +
      rows +
      hostBtn +
      '<button type="button" class="ghost" id="btn-leave">Leave</button></div>';

    var btnNext = $("btn-next");
    if (btnNext) {
      btnNext.onclick = function () {
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
    // Reset pending vote when leaving TURN_SUBMISSION or when step changes
    if (ph !== "TURN_SUBMISSION") resetPendingVote();
    if (ph === "LOBBY") {
      renderLobby();
    } else if (ph === "SELECT_NARRATOR") {
      renderSelectNarrator();
    } else if (ph === "TURN_SUBMISSION") {
      renderTurnSubmission();
    } else if (ph === "REVEAL_VOTES") {
      renderRevealVotes();
    } else if (ph === "SCORE_BASE") {
      renderScoring("base");
    } else if (ph === "SCORE_BONUS") {
      renderScoring("bonus");
    } else if (
      ph === "REVEAL_NARRATOR" ||
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
