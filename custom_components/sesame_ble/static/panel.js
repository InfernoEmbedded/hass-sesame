class SesameKeypadPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this._iframe) {
      this.style.display = "block";
      this.style.width = "100%";
      this.style.height = "100vh";
      this.innerHTML = `
        <iframe
          src="/sesame_static/index.html"
          style="width: 100%; height: 100%; border: none; background: var(--primary-background-color, #0d0e15);"
        ></iframe>
      `;
      this._iframe = this.querySelector('iframe');
      if (this._iframe) {
        this._iframe.addEventListener('load', () => {
          this._forwardHass();
        });
      }
    }
    this._forwardHass();
  }

  _forwardHass() {
    if (this._iframe && this._iframe.contentWindow && this._hass) {
      try {
        this._iframe.contentWindow.hass = this._hass;
        const event = new CustomEvent("hass-changed", { detail: this._hass });
        this._iframe.contentWindow.dispatchEvent(event);
      } catch (err) {
        console.error("Error forwarding hass to iframe:", err);
      }
    }
  }
}
customElements.define("sesame-keypad-panel", SesameKeypadPanel);
