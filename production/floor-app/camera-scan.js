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

    // Every label this app prints is Code128 (see production/planner/
    // labels.py) -- restricting to that plus the handful of common
    // formats someone might hand-scan keeps ZXing from spending every
    // frame trying ~17 formats (slow on an older phone, and a big part
    // of "takes a while and holding very still" -- confirmed live,
    // 2026-09-29), and stops it confidently decoding a barcode that
    // happens to be some OTHER symbology (e.g. a plain online Code128
    // generator that silently fell back to EAN/UPC for a non-numeric
    // input) as if it were a real scan.
    const hints = new Map();
    hints.set(window.ZXing.DecodeHintType.POSSIBLE_FORMATS, [
      window.ZXing.BarcodeFormat.CODE_128,
      window.ZXing.BarcodeFormat.EAN_13,
      window.ZXing.BarcodeFormat.EAN_8,
      window.ZXing.BarcodeFormat.UPC_A,
      window.ZXing.BarcodeFormat.QR_CODE,
    ]);
    hints.set(window.ZXing.DecodeHintType.TRY_HARDER, true);
    reader = new window.ZXing.BrowserMultiFormatReader(hints);
    try {
      // Instance method, not static -- BrowserCodeReader.listVideoInputDevices
      // (confirmed live, 2026-09-29: threw "is not a function" as a static
      // call, camera modal showed a black screen).
      const devices = await reader.listVideoInputDevices();
      // Prefer the back/environment camera on a phone -- it's the one
      // actually pointed at a barcode, not the selfie camera.
      const backCamera = devices.find((d) => /back|rear|environment/i.test(d.label));
      const deviceId = (backCamera || devices[0])?.deviceId;

      // A closer, higher-resolution feed with continuous autofocus reads
      // a small barcode far more reliably than the browser's low-res
      // default -- phone cameras otherwise often stay focused for
      // general video, not a barcode held close. focusMode is an
      // Android Chrome extension to the constraints spec (not
      // universally supported) -- only added when the browser itself
      // reports it, an unsupported advanced constraint is otherwise
      // silently ignored per spec anyway, this is just belt-and-braces.
      const supported = navigator.mediaDevices?.getSupportedConstraints?.() || {};
      const videoConstraints = {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        facingMode: deviceId ? undefined : { ideal: 'environment' },
        width: { ideal: 1920 },
        height: { ideal: 1080 },
      };
      if (supported.focusMode) {
        videoConstraints.advanced = [{ focusMode: 'continuous' }];
      }

      status.textContent = 'Point the camera at a barcode, filling most of the frame...';
      await reader.decodeFromConstraints({ video: videoConstraints }, video, (result, err) => {
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
