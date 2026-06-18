class SesameKeypadPanel extends HTMLElement {
  set hass(hass) {
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
    }
    if (this._iframe && this._iframe.contentWindow) {
      this._iframe.contentWindow.hass = hass;
      const event = new CustomEvent("hass-changed", { detail: hass });
      this._iframe.contentWindow.dispatchEvent(event);
    }
  }
}
customElements.define("sesame-keypad-panel", SesameKeypadPanel);
