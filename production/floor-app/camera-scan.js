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
// Two library bugs found and worked around here, both confirmed live/
// offline 2026-09-29:
//   1. ZXing's built-in decodeContinuously loop only reschedules its
//      next attempt after one of three specific "nothing found this
//      frame" exception types -- any OTHER error thrown mid-decode
//      silently kills the loop for good, no crash, no visible error.
//      Worked around by running our own setTimeout loop that ALWAYS
//      reschedules regardless of what a frame's attempt throws.
//   2. reader.decode(source) only correctly sizes its internal decode
//      canvas for an HTMLVideoElement or HTMLImageElement source --
//      createCaptureCanvas/drawImageOnCanvas read videoWidth/videoHeight
//      or naturalWidth/naturalHeight, which a plain <canvas> has
//      neither of, so passing a canvas straight to decode() silently
//      fails on EVERY frame no matter what's actually drawn on it
//      (confirmed offline against node-canvas: a canvas source fails
//      even for a clean, undistorted barcode). Anywhere this file needs
//      to decode from a canvas (the debug snapshot, the rotation
//      fallback below) goes through canvasToImage() first, converting
//      to a real <img> -- confirmed offline that path decodes
//      correctly, even through heavy blur/contrast washout.

(function () {
  const modal = document.getElementById('cameraScanModal');
  const video = document.getElementById('cameraScanVideo');
  const status = document.getElementById('cameraScanStatus');
  const closeBtn = document.getElementById('cameraScanCloseBtn');
  const frozenFrame = document.getElementById('cameraScanFrozenFrame');
  const debugBtn = document.getElementById('cameraScanDebugBtn');

  let reader = null;
  let stream = null;
  let stopped = true;
  let paused = false;
  let attempts = 0;
  let currentTargetInput = null;

  function stopScan() {
    stopped = true;
    paused = false;
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      stream = null;
    }
    video.srcObject = null;
    video.onclick = null;
    reader = null;
    modal.hidden = true;
    video.hidden = false;
    frozenFrame.hidden = true;
  }

  closeBtn.addEventListener('click', stopScan);

  function makeReader() {
    const hints = new Map();
    hints.set(window.ZXing.DecodeHintType.POSSIBLE_FORMATS, [
      window.ZXing.BarcodeFormat.CODE_128,
      window.ZXing.BarcodeFormat.QR_CODE,
      window.ZXing.BarcodeFormat.EAN_13,
      window.ZXing.BarcodeFormat.EAN_8,
      window.ZXing.BarcodeFormat.UPC_A,
    ]);
    hints.set(window.ZXing.DecodeHintType.TRY_HARDER, true);
    return new window.ZXing.BrowserMultiFormatReader(hints);
  }

  // See header comment (bug 2) -- decode() only works reliably against
  // a real <img>, not a plain <canvas>.
  function canvasToImage(canvas) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = reject;
      img.src = canvas.toDataURL('image/png');
    });
  }

  // Debug aid: freezes and shows EXACTLY the pixels the decoder works
  // from (drawn from the video element the same way ZXing's own
  // createBinaryBitmap does internally), and separately re-attempts a
  // decode against that frozen frame -- isolates "is the captured image
  // itself too blurry/dark to read" from "is something wrong with the
  // running loop/reader state".
  debugBtn.addEventListener('click', async () => {
    if (stopped || !video.videoWidth) return;
    // Pause the live loop FIRST -- it was still running in the
    // background this whole time and overwriting this handler's own
    // status text within ~120ms (the tick loop's own reschedule delay),
    // before it could ever be read or screenshotted. Confirmed live,
    // 2026-09-29: every debug screenshot sent back showed the ordinary
    // live-scan "frames checked" text, never an actual debug result,
    // because of exactly this.
    paused = true;

    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    frozenFrame.src = canvas.toDataURL('image/png');
    video.hidden = true;
    frozenFrame.hidden = false;

    try {
      const img = await canvasToImage(canvas);
      const result = makeReader().decode(img);
      status.textContent = `Decoded from the frozen frame: "${result.getText()}" -- the live loop should have caught this too. Tap the picture to go back to the camera.`;
    } catch (err) {
      status.textContent = `This exact frozen frame could NOT be decoded (${err?.name || err}). ` +
        'Screenshot this picture and send it over -- that\'s precisely what the decoder is failing on. Tap the picture to go back to the camera.';
    }
    frozenFrame.onclick = () => {
      frozenFrame.hidden = true;
      video.hidden = false;
      paused = false;
      tick(currentTargetInput);
    };
  });

  // Some Android phones' camera/WebView combo hands drawImage() a frame
  // that's rotated relative to what's actually shown on screen (a known
  // class of bug, more common on certain OEM skins). Draws the current
  // video frame rotated by `degrees` and decodes that instead.
  async function decodeRotated(source, degrees) {
    const canvas = document.createElement('canvas');
    const swapped = degrees === 90 || degrees === 270;
    canvas.width = swapped ? source.videoHeight : source.videoWidth;
    canvas.height = swapped ? source.videoWidth : source.videoHeight;
    const ctx = canvas.getContext('2d');
    ctx.translate(canvas.width / 2, canvas.height / 2);
    ctx.rotate((degrees * Math.PI) / 180);
    ctx.drawImage(source, -source.videoWidth / 2, -source.videoHeight / 2);
    const img = await canvasToImage(canvas);
    return makeReader().decode(img);
  }

  async function tick(targetInput) {
    if (stopped || paused) return;
    attempts += 1;

    try {
      const result = reader.decode(video);
      finish(targetInput, result.getText());
      return;
    } catch (err) {
      // Expected on almost every frame until a barcode lines up -- NOT
      // treated as fatal regardless of what it is (see header comment:
      // that distinction is exactly what was killing the scan loop
      // before this rewrite).
    }

    // One rotation candidate per attempt, cycling through 90/180/270 --
    // covers a rotated-capture bug without tripling every frame's cost.
    // (See header comment: this now correctly goes through
    // canvasToImage rather than decoding the canvas directly.)
    const degrees = [90, 180, 270][attempts % 3];
    try {
      const result = await decodeRotated(video, degrees);
      finish(targetInput, result.getText());
      return;
    } catch (err) {
      // Same as above -- just means this rotation didn't match either.
    }

    if (stopped || paused) return;
    status.textContent = `Point the camera at a barcode or QR code, filling most of the frame (tap the picture if it looks blurry)... (${attempts} frames checked, ${video.videoWidth}x${video.videoHeight})`;
    setTimeout(() => tick(targetInput), 120);
  }

  function finish(targetInput, text) {
    stopScan();
    targetInput.value = text;
    targetInput.focus();
    // Same as a physical scanner's Enter keystroke -- dispatch a real
    // Enter keydown so every existing listener (one per scan input, see
    // app.js) fires exactly as it already does today.
    targetInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  }

  async function startScan(targetInput) {
    if (!window.ZXing) {
      alert("Camera scanning isn't available right now (library failed to load) -- use the keyboard/scanner input instead.");
      return;
    }
    stopScan(); // in case a previous scan is somehow still active
    stopped = false;
    paused = false;
    attempts = 0;
    currentTargetInput = targetInput;
    modal.hidden = false;
    status.textContent = 'Starting camera...';
    reader = makeReader();

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
      // future frame -- video.play() resolving does NOT guarantee
      // videoWidth/videoHeight are populated yet on every browser, and
      // starting the decode loop one tick too early locks that canvas
      // in at 0x0 forever. Wait for real dimensions first.
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
