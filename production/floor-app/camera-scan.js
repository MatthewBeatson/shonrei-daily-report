// Camera-based barcode scanning: a FALLBACK input mode for whichever
// scan-input-row is missing its physical Bluetooth/USB scanner at that
// moment -- the keyboard-wedge scanner (see app.js's header comment)
// stays the primary, faster method everywhere. Every camera-scan-btn
// opens the same shared modal, decodes via the phone's own camera using
// ZXing (reads Code128, which is what every label this app prints uses --
// see production/planner/labels.py -- plus QR and a few common 1D
// formats), fills in whichever input requested it, and fires the same
// 'Enter' flow a physical scanner or manual typing already triggers --
// no separate submit path to keep in sync.
//
// Runs its own decode loop rather than ZXing's built-in
// decodeFromConstraints/decodeContinuously -- found live, 2026-09-29,
// that library loop only reschedules its next attempt after one of
// THREE specific "nothing found this frame" exception types
// (NotFoundException/ChecksumException/FormatException); any OTHER
// error thrown mid-decode (plausible with TRY_HARDER hitting an edge
// case on a real blurry/tilted frame) silently kills the loop for
// good -- no crash, no visible error, camera picture keeps showing,
// just never decodes another frame. That matches "still doesn't pick
// it up" exactly. This loop instead ALWAYS reschedules regardless of
// what a frame's decode attempt throws.

(function () {
  const modal = document.getElementById('cameraScanModal');
  const video = document.getElementById('cameraScanVideo');
  const status = document.getElementById('cameraScanStatus');
  const closeBtn = document.getElementById('cameraScanCloseBtn');

  let reader = null;
  let stream = null;
  let stopped = true;
  let attempts = 0;

  function stopScan() {
    stopped = true;
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      stream = null;
    }
    video.srcObject = null;
    video.onclick = null;
    reader = null;
    modal.hidden = true;
  }

  closeBtn.addEventListener('click', stopScan);

  function tick(targetInput) {
    if (stopped) return;
    attempts += 1;
    try {
      const result = reader.decode(video);
      const text = result.getText();
      stopScan();
      targetInput.value = text;
      targetInput.focus();
      // Same as a physical scanner's Enter keystroke -- dispatch a real
      // Enter keydown so every existing listener (one per scan input,
      // see app.js) fires exactly as it already does today.
      targetInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
      return;
    } catch (err) {
      // Expected on almost every frame until a barcode lines up --
      // NOT treated as fatal regardless of what it is (see header
      // comment: that distinction is exactly what was killing the
      // scan loop before).
    }
    status.textContent = `Point the camera at a barcode or QR code, filling most of the frame (tap the picture if it looks blurry)... (${attempts} frames checked, ${video.videoWidth}x${video.videoHeight})`;
    setTimeout(() => tick(targetInput), 120);
  }

  async function startScan(targetInput) {
    if (!window.ZXing) {
      alert("Camera scanning isn't available right now (library failed to load) -- use the keyboard/scanner input instead.");
      return;
    }
    stopScan(); // in case a previous scan is somehow still active
    stopped = false;
    attempts = 0;
    modal.hidden = false;
    status.textContent = 'Starting camera...';

    // Code128 (every label this app prints, see production/planner/
    // labels.py) and QR (easy to test with -- much more tolerant of
    // blur/tilt than a 1D barcode, has its own finder pattern) as the
    // two formats actually worth testing right now, plus common 1D
    // fallbacks. TRY_HARDER: more scan rows/rotation attempts, worth it
    // at 720p (not affordable at the 1080p an earlier attempt tried).
    const hints = new Map();
    hints.set(window.ZXing.DecodeHintType.POSSIBLE_FORMATS, [
      window.ZXing.BarcodeFormat.CODE_128,
      window.ZXing.BarcodeFormat.QR_CODE,
      window.ZXing.BarcodeFormat.EAN_13,
      window.ZXing.BarcodeFormat.EAN_8,
      window.ZXing.BarcodeFormat.UPC_A,
    ]);
    hints.set(window.ZXing.DecodeHintType.TRY_HARDER, true);
    reader = new window.ZXing.BrowserMultiFormatReader(hints);

    try {
      const devices = await reader.listVideoInputDevices();
      // Prefer the back/environment camera on a phone -- it's the one
      // actually pointed at a barcode, not the selfie camera.
      const backCamera = devices.find((d) => /back|rear|environment/i.test(d.label));
      const deviceId = (backCamera || devices[0])?.deviceId;

      // 720p -- confirmed live, 2026-09-29: 1920x1080 made scanning
      // WORSE (this decoder is pure JS, no hardware acceleration --
      // more pixels per attempt means fewer attempts per second, slow
      // enough it never caught a real barcode at all). 720p is plenty
      // for a barcode filling most of the frame, and keeps the loop fast.
      const supported = navigator.mediaDevices?.getSupportedConstraints?.() || {};
      const videoConstraints = {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        facingMode: deviceId ? undefined : { ideal: 'environment' },
        width: { ideal: 1280 },
        height: { ideal: 720 },
      };
      if (supported.focusMode) {
        videoConstraints.advanced = [{ focusMode: 'continuous' }];
      }

      stream = await navigator.mediaDevices.getUserMedia({ video: videoConstraints });
      video.srcObject = stream;
      await video.play();

      // Tap-to-refocus -- re-asserts continuous focusMode on tap, for
      // phones whose camera locks focus once at startup and doesn't
      // re-focus for a barcode held up close afterward (continuous
      // isn't honored by every phone's browser). Best-effort, silently
      // a no-op where unsupported.
      video.onclick = async () => {
        try {
          const track = stream?.getVideoTracks?.()[0];
          if (track && supported.focusMode) {
            await track.applyConstraints({ advanced: [{ focusMode: 'continuous' }] });
          }
        } catch (err) { /* best-effort, ignore */ }
      };

      // ZXing sizes its internal decode canvas ONCE, from the video's
      // width/height at that exact moment, then caches it for every
      // future frame -- confirmed live, 2026-09-29: video.play()
      // resolving does NOT guarantee videoWidth/videoHeight are
      // populated yet on every browser, and starting the decode loop
      // one tick too early locks that canvas in at 0x0 forever, so
      // EVERY frame after that decodes against a blank image no matter
      // what's actually in front of the camera -- explains total
      // failure regardless of format (QR included). Wait for real
      // dimensions before the first decode attempt.
      while (video.videoWidth === 0 && !stopped) {
        await new Promise((resolve) => requestAnimationFrame(resolve));
      }
      if (stopped) return;

      tick(targetInput);
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
