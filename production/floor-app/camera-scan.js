// Camera-based barcode scanning: a FALLBACK input mode for whichever
// scan-input-row is missing its physical Bluetooth/USB scanner at that
// moment -- the keyboard-wedge scanner (see app.js's header comment)
// stays the primary, faster method everywhere. Every camera-scan-btn
// opens the same shared modal, decodes via the phone's own camera using
// ZXing (reads Code128, which is what every label this app prints uses --
// see production/planner/labels.py -- plus common 1D/QR formats for
// anything scanned that this app didn't print itself), fills in
// whichever input requested it, and fires the same 'Enter' flow a
// physical scanner or manual typing already triggers -- no separate
// submit path to keep in sync.

(function () {
  const modal = document.getElementById('cameraScanModal');
  const video = document.getElementById('cameraScanVideo');
  const status = document.getElementById('cameraScanStatus');
  const closeBtn = document.getElementById('cameraScanCloseBtn');

  let reader = null;
  let activeTargetInput = null;

  function stopScan() {
    if (reader) {
      try { reader.reset(); } catch (err) { /* already stopped */ }
      reader = null;
    }
    modal.hidden = true;
    activeTargetInput = null;
  }

  closeBtn.addEventListener('click', stopScan);

  async function startScan(targetInput) {
    if (!window.ZXing) {
      alert("Camera scanning isn't available right now (library failed to load) -- use the keyboard/scanner input instead.");
      return;
    }
    activeTargetInput = targetInput;
    modal.hidden = false;
    status.textContent = 'Starting camera...';

    reader = new window.ZXing.BrowserMultiFormatReader();
    try {
      const devices = await window.ZXing.BrowserCodeReader.listVideoInputDevices();
      // Prefer the back/environment camera on a phone -- it's the one
      // actually pointed at a barcode, not the selfie camera.
      const backCamera = devices.find((d) => /back|rear|environment/i.test(d.label));
      const deviceId = (backCamera || devices[0])?.deviceId;

      status.textContent = 'Point the camera at a barcode...';
      await reader.decodeFromVideoDevice(deviceId, video, (result, err) => {
        if (result) {
          const text = result.getText();
          stopScan();
          targetInput.value = text;
          targetInput.focus();
          // Same as a physical scanner's Enter keystroke -- dispatch a
          // real Enter keydown so every existing listener (one per scan
          // input, see app.js) fires exactly as it already does today.
          targetInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
        }
        // NotFoundException fires continuously while nothing's in frame
        // yet -- not an error, just "still looking".
      });
    } catch (err) {
      status.textContent = err?.name === 'NotAllowedError'
        ? 'Camera access denied -- allow camera access for this site, or use the keyboard/scanner input instead.'
        : `Couldn't start the camera: ${err.message || err}`;
    }
  }

  document.querySelectorAll('.camera-scan-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = document.getElementById(btn.dataset.target);
      if (target) startScan(target);
    });
  });
})();
