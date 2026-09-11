// Interactive Web Application Logic for Sesame Hardware Simulator

class SesameSimulatorUI {
  constructor() {
    this.ws = null;
    this.audioCtx = null;
    this.initAudio();
    this.initElements();
    this.bindEvents();
    this.connectWebSocket();
  }

  initAudio() {
    try {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      this.audioCtx = new AudioContext();
    } catch (e) {
      console.warn("Web Audio API not supported", e);
    }
  }

  playBeep(freq = 2400, duration = 0.1) {
    if (!this.audioCtx) return;
    if (this.audioCtx.state === 'suspended') {
      this.audioCtx.resume();
    }
    const osc = this.audioCtx.createOscillator();
    const gain = this.audioCtx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(freq, this.audioCtx.currentTime);
    gain.gain.setValueAtTime(0.15, this.audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01, this.audioCtx.currentTime + duration);
    osc.connect(gain);
    gain.connect(this.audioCtx.destination);
    osc.start();
    osc.stop(this.audioCtx.currentTime + duration);
  }

  playKeyClick() {
    this.playBeep(1800, 0.04);
  }

  initElements() {
    // Lock Elements
    this.lockStateBadge = document.getElementById("lock-state-badge");
    this.thumbturnDisc = document.getElementById("thumbturn-disc");
    this.angleReadout = document.getElementById("angle-readout");
    this.dialProgressArc = document.getElementById("dial-progress-arc");
    this.motorActivity = document.getElementById("motor-activity");
    this.motorStatusText = document.getElementById("motor-status-text");
    this.btnLock = document.getElementById("btn-lock");
    this.btnUnlock = document.getElementById("btn-unlock");
    this.manualSlider = document.getElementById("manual-angle-slider");
    this.sliderValLabel = document.getElementById("slider-val-label");

    // Keypad Elements
    this.lcdBuffer = document.getElementById("lcd-buffer");
    this.buzzerWave = document.getElementById("buzzer-wave");
    this.ledRed = document.getElementById("led-red");
    this.ledBlue = document.getElementById("led-blue");
    this.ledGreen = document.getElementById("led-green");
    this.fpSensor = document.getElementById("fingerprint-sensor");
    this.nfcSensor = document.getElementById("nfc-card-sensor");
    this.keyButtons = document.querySelectorAll(".key-btn");

    // Terminal log
    this.terminalLog = document.getElementById("terminal-log");
    this.btnClearLog = document.getElementById("btn-clear-log");
    this.connPill = document.getElementById("conn-status-pill");
  }

  bindEvents() {
    // Lock buttons
    this.btnLock.addEventListener("click", () => this.sendAction("lock"));
    this.btnUnlock.addEventListener("click", () => this.sendAction("unlock"));

    // Angle slider
    this.manualSlider.addEventListener("input", (e) => {
      const val = parseFloat(e.target.value);
      this.sliderValLabel.textContent = `${val}°`;
      this.updateLockVisuals({ angle: val });
    });

    this.manualSlider.addEventListener("change", (e) => {
      const val = parseFloat(e.target.value);
      this.sendAction("manual_angle", { angle: val });
    });

    // Keypad matrix buttons
    this.keyButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.getAttribute("data-key");
        this.playKeyClick();
        this.sendAction("key_press", { key });
      });
    });

    // Sensors
    this.fpSensor.addEventListener("click", () => {
      this.playKeyClick();
      this.sendAction("fingerprint", { match: true });
    });

    this.nfcSensor.addEventListener("click", () => {
      this.playKeyClick();
      this.sendAction("card", { uid: "E004010203040506" });
    });

    // Clear log
    this.btnClearLog.addEventListener("click", () => {
      this.terminalLog.innerHTML = "";
    });
  }

  connectWebSocket() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${window.location.host}/ws`;

    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this.appendLog({
        time: new Date().toTimeString().split(" ")[0],
        source: "WS",
        message: "Connected to simulation host",
      });
    };

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        this.handleServerMessage(msg);
      } catch (err) {
        console.error("WS Parse error", err);
      }
    };

    this.ws.onclose = () => {
      this.appendLog({
        time: new Date().toTimeString().split(" ")[0],
        source: "WS",
        message: "Disconnected. Reconnecting in 2s...",
      });
      setTimeout(() => this.connectWebSocket(), 2000);
    };
  }

  sendAction(action, payload = {}) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ action, ...payload }));
    }
  }

  handleServerMessage(msg) {
    if (msg.type === "init") {
      if (msg.lock) this.updateLockVisuals(msg.lock);
      if (msg.keypad) this.updateKeypadVisuals(msg.keypad);
      if (msg.log) {
        msg.log.forEach((entry) => this.appendLog(entry));
      }
    } else if (msg.type === "state_update") {
      if (msg.device === "lock") {
        this.updateLockVisuals(msg.state);
      } else if (msg.device === "keypad") {
        this.updateKeypadVisuals(msg.state);
      }
    } else if (msg.type === "log") {
      this.appendLog(msg.data);
    }
  }

  updateLockVisuals(lock) {
    if (lock.angle !== undefined) {
      const angle = lock.angle;
      this.thumbturnDisc.style.transform = `rotate(${angle}deg)`;
      this.angleReadout.textContent = `${angle.toFixed(1)}°`;
      this.sliderValLabel.textContent = `${Math.round(angle)}°`;
      this.manualSlider.value = Math.round(angle);

      // SVG Arc progress (circumference = 2 * PI * 105 ~ 660)
      const offset = 660 - (angle / 360) * 660;
      this.dialProgressArc.style.strokeDashoffset = offset;
    }

    if (lock.is_locked !== undefined) {
      if (lock.is_locked) {
        this.lockStateBadge.textContent = "LOCKED";
        this.lockStateBadge.classList.remove("unlocked");
      } else {
        this.lockStateBadge.textContent = "UNLOCKED";
        this.lockStateBadge.classList.add("unlocked");
      }
    }

    if (lock.motor_running !== undefined) {
      if (lock.motor_running) {
        this.motorActivity.classList.add("active");
        this.motorStatusText.textContent = "Motor Driving...";
      } else {
        this.motorActivity.classList.remove("active");
        this.motorStatusText.textContent = "Motor Idle";
      }
    }

    if (lock.connected !== undefined) {
      this.connPill.textContent = lock.connected ? "BLE CONNECTED" : "BLE ADVERTISING";
      this.connPill.style.color = lock.connected ? "var(--accent-emerald)" : "var(--accent-cyan)";
    }
  }

  updateKeypadVisuals(keypad) {
    // LCD buffer
    if (keypad.keypad_buffer !== undefined) {
      const buf = keypad.keypad_buffer;
      if (buf.length === 0) {
        this.lcdBuffer.textContent = "_ _ _ _";
      } else {
        this.lcdBuffer.textContent = buf.split("").join(" ");
      }
    }

    // Buzzer visualizer & audio
    if (keypad.buzzer !== undefined) {
      if (keypad.buzzer) {
        this.buzzerWave.classList.add("active");
        this.playBeep(2600, 0.1);
      } else {
        this.buzzerWave.classList.remove("active");
      }
    }

    // LEDs
    if (keypad.led_red !== undefined) {
      this.ledRed.classList.toggle("active", keypad.led_red);
    }
    if (keypad.led_blue !== undefined) {
      this.ledBlue.classList.toggle("active", keypad.led_blue);
    }
    if (keypad.led_green !== undefined) {
      this.ledGreen.classList.toggle("active", keypad.led_green);
    }
  }

  appendLog(entry) {
    const el = document.createElement("div");
    let cls = "system";
    if (entry.source && entry.source.includes("Lock")) cls = "lock";
    if (entry.source && entry.source.includes("Touch")) cls = "keypad";

    el.className = `log-entry ${cls}`;
    el.innerHTML = `
      <span class="log-time">${entry.time || ""}</span>
      <span class="log-src">[${entry.source || "LOG"}]</span>
      <span class="log-msg">${entry.message || ""}</span>
    `;

    this.terminalLog.appendChild(el);
    this.terminalLog.scrollTop = this.terminalLog.scrollHeight;
  }
}

window.addEventListener("DOMContentLoaded", () => {
  window.simulatorUI = new SesameSimulatorUI();
});
