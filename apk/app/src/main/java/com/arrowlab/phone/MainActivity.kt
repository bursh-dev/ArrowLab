package com.arrowlab.phone

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Bundle
import android.text.format.DateFormat
import android.util.Log
import android.view.WindowManager
import android.view.TextureView
import android.view.View
import android.widget.Button
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.Date
import java.util.concurrent.TimeUnit

private const val SERVER_PORT = 8000

/** Cloudflare quick-tunnel URL. When non-null, the APK skips LAN discovery
 *  and routes every request (HTTP + WS) through this base. The tunnel lets
 *  the phone reach the laptop server from any network without USB tether
 *  or being on the same Wi-Fi. Set to null to fall back to LAN discovery. */
// Top-level `val` (not `const val`) because Kotlin disallows nullable
// const declarations. Functionally equivalent for our use.
//
// Currently null: trycloudflare quick tunnels add ~30 s of latency on a
// 200 KB calibration upload, which makes session setup painful. Phone
// stays on the LAN-discovery path (USB tether at 127.0.0.1 first, then
// last-known host, then /24 scan). Re-enable with the URL string once
// we move to a stable Cloudflare named tunnel.
private val REMOTE_BASE_URL: String? = null

class MainActivity : AppCompatActivity() {

    private lateinit var connectBtn: Button
    private lateinit var disconnectBtn: Button
    private lateinit var statusText: TextView
    private lateinit var logText: TextView
    private lateinit var logScroll: ScrollView
    private lateinit var cameraPreview: TextureView
    private lateinit var overlay: AnnotationOverlay

    private var ws: WebSocket? = null
    private val http: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .pingInterval(10, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .build()
    }
    private val discoverHttp: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .callTimeout(800, TimeUnit.MILLISECONDS)
            .connectTimeout(500, TimeUnit.MILLISECONDS)
            .readTimeout(500, TimeUnit.MILLISECONDS)
            .writeTimeout(500, TimeUnit.MILLISECONDS)
            .build()
    }
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    private var userRequestedDisconnect = false
    private var reconnectDelayMs = 1_000L
    private val reconnectHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private var reconnectRunnable: Runnable? = null
    private var connectEpoch = 0L
    /** Full base URL for all requests, including scheme/host/port — e.g.
     *  "http://192.168.1.5:8000" or "https://foo.trycloudflare.com". */
    private var currentBaseUrl: String? = null

    private var cameraController: CameraController? = null

    private val permissionsLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ ->
        // Re-check actual permission state (the launcher's result only
        // contains what we asked for in this launch).
        val cameraOk = ContextCompat.checkSelfPermission(
            this, Manifest.permission.CAMERA
        ) == PackageManager.PERMISSION_GRANTED
        val micOk = ContextCompat.checkSelfPermission(
            this, Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
        if (cameraOk) {
            appendLog(
                "permissions ok: camera${if (micOk) " + mic" else " (no mic)"}",
                ok = true,
            )
            cameraController?.start()
        } else {
            appendLog("camera permission denied — capture disabled", error = true)
        }
        if (!micOk) appendLog("mic permission denied — shots will have no audio", error = true)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        ViewCompat.setOnApplyWindowInsetsListener(findViewById(R.id.main)) { v, insets ->
            val systemBars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            v.setPadding(systemBars.left, systemBars.top, systemBars.right, systemBars.bottom)
            insets
        }

        connectBtn = findViewById(R.id.connectBtn)
        disconnectBtn = findViewById(R.id.disconnectBtn)
        statusText = findViewById(R.id.statusText)
        logText = findViewById(R.id.logText)
        logScroll = findViewById(R.id.logScroll)
        cameraPreview = findViewById(R.id.cameraPreview)
        overlay = findViewById(R.id.overlay)
        findViewById<AspectFrameLayout>(R.id.previewContainer).setAspectRatio(16f / 9f)

        findViewById<Button>(R.id.logToggleBtn).setOnClickListener {
            logScroll.visibility =
                if (logScroll.visibility == View.VISIBLE) View.GONE else View.VISIBLE
        }

        cameraController = CameraController(this, cameraPreview) { msg, err ->
            appendLog(msg, error = err)
        }

        connectBtn.setOnClickListener { startDiscoverAndConnect() }
        disconnectBtn.setOnClickListener {
            userRequestedDisconnect = true
            cancelPendingReconnect()
            ws?.cancel()
            ws = null
            setStatus("disconnected", "#888888")
            disconnectBtn.isEnabled = false
            connectBtn.isEnabled = true
        }

        ensureCameraPermission()
    }

    override fun onDestroy() {
        super.onDestroy()
        cancelPendingReconnect()
        ws?.cancel()
        cameraController?.stop()
        scope.coroutineContext[Job]?.cancel()
    }

    private fun ensureCameraPermission() {
        val needed = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED) needed += Manifest.permission.CAMERA
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) needed += Manifest.permission.RECORD_AUDIO
        if (needed.isEmpty()) {
            cameraController?.start()
        } else {
            permissionsLauncher.launch(needed.toTypedArray())
        }
    }

    private fun startDiscoverAndConnect() {
        cancelPendingReconnect()
        ws?.cancel()
        ws = null
        userRequestedDisconnect = false
        reconnectDelayMs = 1_000L
        connectBtn.isEnabled = false
        disconnectBtn.isEnabled = true

        // Remote-tunnel override: when set, skip LAN discovery and connect
        // straight to the Cloudflare URL. The discovery/probe/last_host
        // machinery stays intact for the null-override fallback case.
        if (REMOTE_BASE_URL != null) {
            currentBaseUrl = REMOTE_BASE_URL
            appendLog("using remote URL $REMOTE_BASE_URL", ok = true)
            setStatus("connecting (remote)...", "#b0b050")
            connect(REMOTE_BASE_URL)
            return
        }

        setStatus("discovering...", "#b0b050")
        scope.launch {
            val host = discoverServer(SERVER_PORT)
            if (host == null) {
                appendLog("discovery: no ArrowLab server found on LAN", error = true)
                setStatus("no server found", "#d05050")
                connectBtn.isEnabled = true
                disconnectBtn.isEnabled = false
                return@launch
            }
            appendLog("discovery: found server at $host:$SERVER_PORT", ok = true)
            currentBaseUrl = "http://$host:$SERVER_PORT"
            if (host != "127.0.0.1") rememberHost(host)
            connect(currentBaseUrl!!)
        }
    }

    private suspend fun discoverServer(port: Int): String? {
        // Try USB reverse-tether first (fast single-probe, no LAN needed).
        if (withContext(Dispatchers.IO) { probe("127.0.0.1", port) }) {
            appendLog("discovery: USB reverx`se-tether hit (127.0.0.1)")
            return "127.0.0.1"
        }
        // Try last-known-good IP (saved on prior successful connect).
        val lastHost = getSharedPreferences("arrowlab", Context.MODE_PRIVATE)
            .getString("last_host", "172.20.214.141")
        if (!lastHost.isNullOrBlank()) {
            if (withContext(Dispatchers.IO) { probe(lastHost, port) }) {
                appendLog("discovery: last-known-good $lastHost hit")
                return lastHost
            }
        }
        val prefixes = getLocalSubnetPrefixes()
        if (prefixes.isEmpty()) {
            appendLog("discovery: no Wi-Fi and no USB tether", error = true)
            return null
        }
        appendLog("discovery: scanning ${prefixes.joinToString { "$it.0/24" }} on port $port...")
        return withContext(Dispatchers.IO) {
            val ips = prefixes.flatMap { p -> (1..254).map { "$p.$it" } }
            val deferreds = ips.map { ip -> async { if (probe(ip, port)) ip else null } }
            deferreds.awaitAll().firstOrNull { it != null }
        }
    }

    private fun rememberHost(host: String) {
        getSharedPreferences("arrowlab", Context.MODE_PRIVATE)
            .edit().putString("last_host", host).apply()
    }

    private fun probe(ip: String, port: Int): Boolean {
        return try {
            val url = "http://$ip:$port/api/session"
            val resp = discoverHttp.newCall(Request.Builder().url(url).build()).execute()
            resp.use {
                if (!it.isSuccessful) return false
                val body = it.body?.string() ?: return false
                body.contains("session_id") &&
                    body.contains("has_annotation") &&
                    body.contains("calibration_frame")
            }
        } catch (_: Exception) {
            false
        }
    }

    private fun getLocalSubnetPrefixes(): List<String> {
        val prefixes = mutableListOf<String>()
        val interfaces = NetworkInterface.getNetworkInterfaces() ?: return prefixes
        for (ni in interfaces) {
            if (!ni.isUp || ni.isLoopback) continue
            for (addr in ni.inetAddresses) {
                if (addr is Inet4Address &&
                    !addr.isLoopbackAddress &&
                    !addr.isLinkLocalAddress
                ) {
                    val ip = addr.hostAddress ?: continue
                    val prefix = ip.substringBeforeLast('.')
                    if (prefix !in prefixes) prefixes.add(prefix)
                }
            }
        }
        return prefixes
    }

    private fun connect(baseUrl: String) {
        val epoch = ++connectEpoch
        // http(s):// → ws(s):// for the WebSocket. OkHttp handles WSS over
        // Cloudflare Tunnel without any extra builder config.
        val url = baseUrl.replaceFirst("http", "ws") + "/ws/phone"
        appendLog("connecting to $url")
        setStatus("connecting...", "#b0b050")

        val req = Request.Builder().url(url).build()
        ws = http.newWebSocket(req, object : WebSocketListener() {
            private fun stale() = epoch != connectEpoch

            override fun onOpen(webSocket: WebSocket, response: Response) {
                runOnUiThread {
                    if (stale()) return@runOnUiThread
                    appendLog("ws open")
                }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                runOnUiThread {
                    if (stale()) return@runOnUiThread
                    handleMessage(text)
                }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                runOnUiThread {
                    if (stale()) return@runOnUiThread
                    appendLog("ws closing: $code $reason")
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                runOnUiThread {
                    if (stale()) return@runOnUiThread
                    appendLog("ws closed: $code $reason")
                    setStatus("disconnected", "#888888")
                    disconnectBtn.isEnabled = false
                    connectBtn.isEnabled = true
                    scheduleReconnectIfNeeded(baseUrl, epoch)
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                runOnUiThread {
                    if (stale()) return@runOnUiThread
                    appendLog("ws failure: ${t.message}", error = true)
                    setStatus("error", "#d05050")
                    disconnectBtn.isEnabled = false
                    connectBtn.isEnabled = true
                    scheduleReconnectIfNeeded(baseUrl, epoch)
                }
            }
        })
    }

    private fun cancelPendingReconnect() {
        reconnectRunnable?.let { reconnectHandler.removeCallbacks(it) }
        reconnectRunnable = null
    }

    private fun scheduleReconnectIfNeeded(baseUrl: String, epoch: Long) {
        if (userRequestedDisconnect) return
        if (epoch != connectEpoch) return
        cancelPendingReconnect()
        appendLog("reconnecting in ${reconnectDelayMs / 1000}s...")
        val r = Runnable {
            if (userRequestedDisconnect) return@Runnable
            if (epoch != connectEpoch) return@Runnable
            val next = (reconnectDelayMs * 2).coerceAtMost(15_000)
            reconnectDelayMs = next
            connect(baseUrl)
        }
        reconnectRunnable = r
        reconnectHandler.postDelayed(r, reconnectDelayMs)
    }

    private fun handleMessage(text: String) {
        appendLog("<- $text")
        val msg = try { JSONObject(text) } catch (_: Exception) { return }
        when (msg.optString("type")) {
            "paired" -> {
                val id = msg.optString("session_id", "?")
                setStatus("paired: $id", "#50b050")
                reconnectDelayMs = 1_000L
            }
            "rejected" -> {
                val reason = msg.optString("reason", "unknown")
                setStatus("rejected: $reason", "#d05050")
                userRequestedDisconnect = true
            }
            "capture_frame" -> {
                // Direct JPEG (~150 KB) from the TextureView. Previously we
                // shipped a 1 s mp4 slice and let the server extract a frame,
                // but that's ~2 MB and 524s through the Cloudflare quick
                // tunnel. JPEG is small enough to clear the tunnel cleanly
                // and the server's calibration endpoint already handles
                // image/jpeg directly (the fake-phone path).
                appendLog("capture_frame received, grabbing JPEG...")
                val cc = cameraController
                if (cc == null) {
                    appendLog("camera not ready", error = true)
                } else cc.captureStillJpeg { bytes ->
                    runOnUiThread {
                        if (bytes == null) {
                            appendLog("calibration jpeg capture failed", error = true)
                        } else {
                            appendLog("captured ${bytes.size / 1024} KB JPEG, uploading...")
                            uploadCalibrationFrame(bytes)
                        }
                    }
                }
            }
            "annotation" -> {
                val corridor = msg.optJSONObject("corridor")
                val target = msg.optJSONObject("target")
                if (corridor == null && target == null) {
                    overlay.clear()
                    appendLog("annotation cleared")
                    return
                }
                val ss = cameraController?.getStillSize()
                if (ss == null) {
                    appendLog("annotation received but camera not ready", error = true)
                    return
                }
                val bboxArr = target?.optJSONArray("bbox")
                val ann = AnnotationOverlay.Annotation(
                    corridorTop = corridor?.optInt("y_top"),
                    corridorBottom = corridor?.optInt("y_bottom"),
                    targetCx = target?.optInt("cx"),
                    targetCy = target?.optInt("cy"),
                    targetR = target?.optInt("r", 0) ?: 0,
                    bbox = bboxArr?.let { IntArray(4) { i -> it.getInt(i) } },
                    imageW = ss.width,
                    imageH = ss.height,
                )
                overlay.setAnnotation(ann)
                appendLog("annotation overlay updated", ok = true)
            }
            "slice" -> {
                appendLog("slice received, cutting 6s from ring buffer...")
                cameraController?.sliceLastSeconds(6.0) { bytes ->
                    runOnUiThread {
                        if (bytes == null) {
                            appendLog("slice failed", error = true)
                        } else {
                            appendLog("slice: ${bytes.size / 1024} KB, uploading...")
                            uploadShot(bytes)
                        }
                    }
                } ?: appendLog("camera not ready for slice", error = true)
            }
            "arm" -> {
                val checker = ShotRecorder.MatchChecker { rPcm, iPcm, sr ->
                    checkSoundMatch(rPcm, iPcm, sr)
                }
                val ok = cameraController?.arm(checker) { bytes, releasePtsUs, impactPtsUs, videoDurationS, releaseSim, impactSim ->
                    // Audio is synced to the video's actual duration in the
                    // mp4 (both tracks end at "now"), and the trigger pool
                    // sleeps `postPadS` after impact before cutting. So:
                    //   impact in mp4  = videoDuration - postPad
                    //   release in mp4 = impact - gap
                    val gapS = (impactPtsUs - releasePtsUs) / 1_000_000.0
                    val postPadS = 0.22
                    val impactInMp4S = videoDurationS - postPadS
                    val releaseInMp4S = impactInMp4S - gapS
                    runOnUiThread {
                        val simStr = "r=${releaseSim?.let { "%.2f".format(it) } ?: "-"}/i=${impactSim?.let { "%.2f".format(it) } ?: "-"}"
                        appendLog(
                            "armed-shot: ${bytes.size / 1024} KB gap=${(gapS * 1000).toInt()} ms dur=${"%.2f".format(videoDurationS)}s sims=$simStr, uploading...",
                            ok = true,
                        )
                    }
                    uploadShot(
                        bytes,
                        releaseS = releaseInMp4S, impactS = impactInMp4S,
                        releaseSim = releaseSim, impactSim = impactSim,
                    )
                } == true
                if (!ok) appendLog("arm failed — camera not ready", error = true)
            }
            "disarm" -> {
                cameraController?.disarm()
            }
        }
    }

    private fun uploadCalibrationFrame(bytes: ByteArray) {
        val baseUrl = currentBaseUrl ?: return
        // No rotation header: captureStillJpeg now extracts the JPEG from
        // an encoder mp4 frame, which is already in the same sensor-native
        // orientation as the shot mp4s the tracker consumes.
        scope.launch(Dispatchers.IO) {
            try {
                val body = bytes.toRequestBody("image/jpeg".toMediaType())
                val req = Request.Builder()
                    .url("$baseUrl/api/calibration-frame")
                    .post(body)
                    .build()
                http.newCall(req).execute().use { resp ->
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            appendLog("calibration frame uploaded", ok = true)
                        } else {
                            appendLog("upload failed: HTTP ${resp.code}", error = true)
                        }
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { appendLog("upload error: ${e.message}", error = true) }
            }
        }
    }

    private fun uploadShot(
        bytes: ByteArray,
        releaseS: Double? = null,
        impactS: Double? = null,
        releaseSim: Double? = null,
        impactSim: Double? = null,
    ) {
        val baseUrl = currentBaseUrl ?: return
        scope.launch(Dispatchers.IO) {
            try {
                val body = bytes.toRequestBody("video/mp4".toMediaType())
                val builder = Request.Builder()
                    .url("$baseUrl/api/shot")
                    .post(body)
                if (releaseS != null) builder.addHeader("X-Arrow-Release-S", "%.6f".format(releaseS))
                if (impactS != null) builder.addHeader("X-Arrow-Impact-S", "%.6f".format(impactS))
                if (releaseSim != null) builder.addHeader("X-Arrow-Release-Sim", "%.6f".format(releaseSim))
                if (impactSim != null) builder.addHeader("X-Arrow-Impact-Sim", "%.6f".format(impactSim))
                http.newCall(builder.build()).execute().use { resp ->
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            appendLog("shot uploaded", ok = true)
                        } else {
                            appendLog("shot upload failed: HTTP ${resp.code}", error = true)
                        }
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { appendLog("shot upload error: ${e.message}", error = true) }
            }
        }
    }

    /** Synchronous probe to /api/sound-match. Called from the trigger-pool
     *  thread inside ShotRecorder; OkHttp's blocking execute() is fine.
     *  On any HTTP/transport failure we return accept=true so a flaky
     *  network doesn't drop real shots — explicit error is logged. */
    private fun checkSoundMatch(
        releasePcm: ByteArray, impactPcm: ByteArray, sampleRate: Int,
    ): ShotRecorder.MatchResult {
        val baseUrl = currentBaseUrl
            ?: return ShotRecorder.MatchResult(accept = true, error = "no_host")
        return try {
            val pcmType = "application/octet-stream".toMediaType()
            val body = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("release", "release.pcm", releasePcm.toRequestBody(pcmType))
                .addFormDataPart("impact", "impact.pcm", impactPcm.toRequestBody(pcmType))
                .addFormDataPart("src_rate", sampleRate.toString())
                .build()
            val req = Request.Builder()
                .url("$baseUrl/api/sound-match")
                .post(body)
                .build()
            http.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) {
                    return ShotRecorder.MatchResult(
                        accept = true, error = "http_${resp.code}",
                    )
                }
                val text = resp.body?.string().orEmpty()
                val json = JSONObject(text)
                ShotRecorder.MatchResult(
                    accept = json.optBoolean("accept", true),
                    releaseSim = if (json.isNull("release_sim")) null else json.optDouble("release_sim"),
                    impactSim = if (json.isNull("impact_sim")) null else json.optDouble("impact_sim"),
                    noTemplate = json.optBoolean("no_template", false),
                    error = if (json.isNull("error")) null else json.optString("error").ifEmpty { null },
                )
            }
        } catch (e: Exception) {
            ShotRecorder.MatchResult(accept = true, error = e.message ?: "exception")
        }
    }

    private fun setStatus(text: String, colorHex: String) {
        statusText.text = text
        statusText.setTextColor(android.graphics.Color.parseColor(colorHex))
    }

    private fun appendLog(line: String, error: Boolean = false, ok: Boolean = false) {
        val ts = DateFormat.format("HH:mm:ss", Date()).toString()
        val prefix = when {
            error -> "[E] "
            ok -> "[+] "
            else -> ""
        }
        // Mirror to logcat so `adb logcat -s ArrowLab` captures everything.
        when {
            error -> Log.e("ArrowLab", line)
            ok -> Log.i("ArrowLab", line)
            else -> Log.d("ArrowLab", line)
        }
        runOnUiThread {
            logText.append("$ts $prefix$line\n")
            logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
        }
    }
}
