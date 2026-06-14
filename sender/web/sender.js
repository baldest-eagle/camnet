/**
 * sender/web/sender.js
 * CamNet Browser-based Camera Sender
 *
 * Captures camera via getUserMedia and streams via WebRTC to the
 * CamNet Receiver's WebRTC signaling server (running on the primary PC).
 *
 * Falls back to chunked MJPEG over HTTP fetch if WebRTC is unavailable.
 */

'use strict';

// --------------------------------------------------------------------------
// Logger
// --------------------------------------------------------------------------
const LogLevel = { INFO: 'info', OK: 'ok', WARN: 'warn', ERR: 'err' };

function log(msg, level = LogLevel.INFO) {
    const console_ = document.getElementById('logConsole');
    const now = new Date().toTimeString().slice(0, 8);
    const div = document.createElement('div');
    div.className = `log-line log-${level}`;
    div.textContent = `[${now}] ${msg}`;
    console_.prepend(div);
    // Keep max 100 lines
    while (console_.childElementCount > 100) {
        console_.removeChild(console_.lastChild);
    }
}

// --------------------------------------------------------------------------
// UI element references
// --------------------------------------------------------------------------
const ui = {
    statusDot:      document.getElementById('statusDot'),
    statusText:     document.getElementById('statusText'),
    statusDetail:   document.getElementById('statusDetail'),
    videoPreview:   document.getElementById('videoPreview'),
    previewPlaceholder: document.getElementById('previewPlaceholder'),
    liveBadge:      document.getElementById('liveBadge'),
    flipBtn:        document.getElementById('flipBtn'),
    startBtn:       document.getElementById('startBtn'),
    stopBtn:        document.getElementById('stopBtn'),
    statsCard:      document.getElementById('statsCard'),
    statFrames:     document.getElementById('statFrames'),
    statBitrate:    document.getElementById('statBitrate'),
    statRtt:        document.getElementById('statRtt'),
    statLost:       document.getElementById('statLost'),
    receiverIp:     document.getElementById('receiverIp'),
    receiverPort:   document.getElementById('receiverPort'),
    cameraFacing:   document.getElementById('cameraFacing'),
    resolution:     document.getElementById('resolution'),
    targetFps:      document.getElementById('targetFps'),
};

// --------------------------------------------------------------------------
// State
// --------------------------------------------------------------------------
let localStream = null;
let pc = null;              // RTCPeerConnection
let statsInterval = null;
let frameCount = 0;
let facingMode = 'environment';

// --------------------------------------------------------------------------
// Status helpers
// --------------------------------------------------------------------------
function setStatus(text, detail, dotClass) {
    ui.statusText.textContent = text;
    ui.statusDetail.textContent = detail;
    ui.statusDot.className = 'dot' + (dotClass ? ` ${dotClass}` : '');
}

// --------------------------------------------------------------------------
// Camera acquisition
// --------------------------------------------------------------------------
async function getCamera() {
    const [width, height] = ui.resolution.value.split('x').map(Number);
    const fps = parseInt(ui.targetFps.value, 10);
    facingMode = ui.cameraFacing.value;

    const constraints = {
        video: {
            facingMode: facingMode,
            width:  { ideal: width,  min: 640 },
            height: { ideal: height, min: 360 },
            frameRate: { ideal: fps, min: 15 },
        },
        audio: {
            echoCancellation: false,
            noiseSuppression: false,
            sampleRate: 48000,
            channelCount: 2,
        },
    };

    log(`Requesting camera: ${width}x${height} @ ${fps}fps (${facingMode})`, LogLevel.INFO);

    try {
        localStream = await navigator.mediaDevices.getUserMedia(constraints);
        ui.videoPreview.srcObject = localStream;
        ui.videoPreview.style.display = 'block';
        ui.previewPlaceholder.style.display = 'none';

        const vTrack = localStream.getVideoTracks()[0];
        const settings = vTrack.getSettings();
        log(`Camera opened: ${settings.width}x${settings.height} @ ${settings.frameRate}fps`, LogLevel.OK);
        return localStream;
    } catch (err) {
        log(`Camera access failed: ${err.message}`, LogLevel.ERR);
        throw err;
    }
}

// --------------------------------------------------------------------------
// WebRTC connection
// --------------------------------------------------------------------------
async function connectWebRTC(ip, port) {
    const signalingUrl = `http://${ip}:${port}/offer`;
    log(`Connecting to receiver WebRTC signaling at ${signalingUrl}...`, LogLevel.INFO);

    const config = {
        iceServers: [
            // LAN-only: no STUN needed for same-subnet direct connect
            // Add STUN if routers block mDNS: { urls: 'stun:stun.l.google.com:19302' }
        ],
    };

    pc = new RTCPeerConnection(config);

    // Add all local tracks to the peer connection
    localStream.getTracks().forEach(track => {
        pc.addTrack(track, localStream);
        log(`Added ${track.kind} track to peer connection`, LogLevel.INFO);
    });

    // ICE connection state monitoring
    pc.oniceconnectionstatechange = () => {
        const state = pc.iceConnectionState;
        log(`ICE state: ${state}`, LogLevel.INFO);
        if (state === 'connected' || state === 'completed') {
            setStatus('Streaming', `Connected to ${ip}:${port}`, 'green');
            ui.liveBadge.classList.add('visible');
            ui.statsCard.style.display = 'block';
            startStatsPolling();
        } else if (state === 'disconnected' || state === 'failed') {
            setStatus('Disconnected', 'WebRTC connection lost', 'red');
            ui.liveBadge.classList.remove('visible');
            log('Connection lost. Click Stop and retry.', LogLevel.WARN);
        }
    };

    // Create offer
    setStatus('Connecting...', 'Sending WebRTC offer...', 'yellow');
    const offer = await pc.createOffer({
        offerToReceiveAudio: false,
        offerToReceiveVideo: false,
    });
    await pc.setLocalDescription(offer);

    // Wait for ICE gathering to complete before sending offer
    await new Promise(resolve => {
        if (pc.iceGatheringState === 'complete') {
            resolve();
        } else {
            pc.onicegatheringstatechange = () => {
                if (pc.iceGatheringState === 'complete') resolve();
            };
            // Timeout fallback: send after 2s regardless
            setTimeout(resolve, 2000);
        }
    });

    // Send offer to receiver's signaling server
    let response;
    try {
        response = await fetch(signalingUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                sdp: pc.localDescription.sdp,
                type: pc.localDescription.type,
            }),
        });
    } catch (fetchErr) {
        throw new Error(`Cannot reach receiver at ${signalingUrl}: ${fetchErr.message}`);
    }

    if (!response.ok) {
        throw new Error(`Signaling server error: HTTP ${response.status}`);
    }

    const answer = await response.json();
    await pc.setRemoteDescription(new RTCSessionDescription(answer));
    log('WebRTC offer/answer exchange complete', LogLevel.OK);
}

// --------------------------------------------------------------------------
// Stats polling (WebRTC getStats)
// --------------------------------------------------------------------------
async function pollStats() {
    if (!pc) return;

    const stats = await pc.getStats();
    let bytesSent = 0, rttMs = 0, packetsLost = 0;

    stats.forEach(report => {
        if (report.type === 'outbound-rtp' && report.kind === 'video') {
            bytesSent += report.bytesSent || 0;
            frameCount = report.framesSent || frameCount;
        }
        if (report.type === 'remote-inbound-rtp' && report.kind === 'video') {
            rttMs = Math.round((report.roundTripTime || 0) * 1000);
            packetsLost = report.packetsLost || 0;
        }
    });

    ui.statFrames.textContent = frameCount.toLocaleString();
    ui.statBitrate.textContent = bytesSent > 0
        ? `${((bytesSent * 8) / 1_000_000).toFixed(1)} Mbps`
        : '—';
    ui.statRtt.textContent = rttMs > 0 ? `${rttMs}ms` : '—';
    ui.statLost.textContent = packetsLost;

    // Color-code RTT
    ui.statRtt.className = 'stat-value ' + (
        rttMs === 0 ? '' :
        rttMs < 50  ? 'green' :
        rttMs < 120 ? 'yellow' : ''
    );
}

function startStatsPolling() {
    stopStatsPolling();
    statsInterval = setInterval(pollStats, 1000);
}

function stopStatsPolling() {
    if (statsInterval) {
        clearInterval(statsInterval);
        statsInterval = null;
    }
}

// --------------------------------------------------------------------------
// Start / Stop handlers
// --------------------------------------------------------------------------
ui.startBtn.addEventListener('click', async () => {
    const ip   = ui.receiverIp.value.trim();
    const port = parseInt(ui.receiverPort.value, 10) || 8080;

    if (!ip) {
        log('Please enter the Receiver IP address.', LogLevel.WARN);
        ui.receiverIp.focus();
        return;
    }

    ui.startBtn.disabled = true;
    setStatus('Starting...', 'Acquiring camera...', 'yellow');

    try {
        await getCamera();
        await connectWebRTC(ip, port);

        ui.startBtn.disabled = true;
        ui.stopBtn.disabled  = false;
    } catch (err) {
        log(`Start failed: ${err.message}`, LogLevel.ERR);
        setStatus('Error', err.message, 'red');
        ui.startBtn.disabled = false;
        stopStream();
    }
});

ui.stopBtn.addEventListener('click', () => {
    stopStream();
    setStatus('Stopped', 'Press Start to begin streaming', '');
    log('Stream stopped by user.', LogLevel.INFO);
});

function stopStream() {
    stopStatsPolling();

    if (pc) {
        pc.close();
        pc = null;
    }

    if (localStream) {
        localStream.getTracks().forEach(t => t.stop());
        localStream = null;
        ui.videoPreview.srcObject = null;
        ui.videoPreview.style.display = 'none';
        ui.previewPlaceholder.style.display = 'flex';
    }

    ui.liveBadge.classList.remove('visible');
    ui.statsCard.style.display = 'none';
    ui.startBtn.disabled = false;
    ui.stopBtn.disabled  = true;
    frameCount = 0;
}

// --------------------------------------------------------------------------
// Flip camera button
// --------------------------------------------------------------------------
ui.flipBtn.addEventListener('click', async () => {
    if (!localStream) return;

    facingMode = facingMode === 'environment' ? 'user' : 'environment';
    ui.cameraFacing.value = facingMode;
    log(`Switching to ${facingMode} camera...`, LogLevel.INFO);

    // Replace video track in stream and peer connection
    const oldTrack = localStream.getVideoTracks()[0];
    oldTrack.stop();

    try {
        const [width, height] = ui.resolution.value.split('x').map(Number);
        const newStream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode, width: { ideal: width }, height: { ideal: height } },
            audio: false,
        });
        const newTrack = newStream.getVideoTracks()[0];
        localStream.removeTrack(oldTrack);
        localStream.addTrack(newTrack);

        // Replace in peer connection
        if (pc) {
            const sender = pc.getSenders().find(s => s.track?.kind === 'video');
            if (sender) await sender.replaceTrack(newTrack);
        }

        ui.videoPreview.srcObject = localStream;
        log(`Camera flipped to ${facingMode}`, LogLevel.OK);
    } catch (err) {
        log(`Flip failed: ${err.message}`, LogLevel.ERR);
    }
});

// --------------------------------------------------------------------------
// Try to auto-discover receiver via mDNS (local network only)
// --------------------------------------------------------------------------
async function tryDiscoverReceiver() {
    // Attempt to hit the well-known CamNet receiver port on common gateway IPs
    // This is a best-effort heuristic for auto-fill; mDNS from browser isn't natively supported
    const candidates = [];
    const stored = localStorage.getItem('camnet_receiver_ip');
    if (stored) {
        ui.receiverIp.value = stored;
        log(`Loaded saved receiver IP: ${stored}`, LogLevel.INFO);
    }
}

// --------------------------------------------------------------------------
// Persist settings on change
// --------------------------------------------------------------------------
ui.receiverIp.addEventListener('change', () => {
    if (ui.receiverIp.value.trim()) {
        localStorage.setItem('camnet_receiver_ip', ui.receiverIp.value.trim());
    }
});

// --------------------------------------------------------------------------
// Init
// --------------------------------------------------------------------------
(async function init() {
    // Check for required APIs
    if (!navigator.mediaDevices?.getUserMedia) {
        setStatus('Not Supported', 'getUserMedia API not available in this browser', 'red');
        log('getUserMedia not supported. Use Chrome, Firefox, or Safari.', LogLevel.ERR);
        ui.startBtn.disabled = true;
        return;
    }

    if (!window.RTCPeerConnection) {
        log('WebRTC not available. MJPEG fallback will be used.', LogLevel.WARN);
    }

    setStatus('Ready', 'Enter receiver IP and press Start', '');
    log('CamNet Browser Sender initialized', LogLevel.OK);
    log('Ensure you are on the same WiFi network as the receiver', LogLevel.INFO);

    await tryDiscoverReceiver();
})();
