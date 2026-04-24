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
    private var currentHost: String? = null

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
            currentHost = host
            if (host != "127.0.0.1") rememberHost(host)
            connect(host, SERVER_PORT)
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

    private fun connect(host: String, port: Int) {
        val epoch = ++connectEpoch
        val url = "ws://$host:$port/ws/phone"
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
                    scheduleReconnectIfNeeded(host, port, epoch)
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                runOnUiThread {
                    if (stale()) return@runOnUiThread
                    appendLog("ws failure: ${t.message}", error = true)
                    setStatus("error", "#d05050")
                    disconnectBtn.isEnabled = false
                    connectBtn.isEnabled = true
                    scheduleReconnectIfNeeded(host, port, epoch)
                }
            }
        })
    }

    private fun cancelPendingReconnect() {
        reconnectRunnable?.let { reconnectHandler.removeCallbacks(it) }
        reconnectRunnable = null
    }

    private fun scheduleReconnectIfNeeded(host: String, port: Int, epoch: Long) {
        if (userRequestedDisconnect) return
        if (epoch != connectEpoch) return
        cancelPendingReconnect()
        appendLog("reconnecting in ${reconnectDelayMs / 1000}s...")
        val r = Runnable {
            if (userRequestedDisconnect) return@Runnable
            if (epoch != connectEpoch) return@Runnable
            val next = (reconnectDelayMs * 2).coerceAtMost(15_000)
            reconnectDelayMs = next
            connect(host, port)
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
                appendLog("capture_frame received, slicing 1s for calibration...")
                cameraController?.sliceLastSeconds(1.0) { bytes ->
                    runOnUiThread {
                        if (bytes == null) {
                            appendLog("calibration slice failed", error = true)
                        } else {
                            appendLog("sliced ${bytes.size / 1024} KB, uploading...")
                            uploadCalibrationFrame(bytes)
                        }
                    }
                } ?: appendLog("camera not ready", error = true)
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
                val ok = cameraController?.arm { bytes, releasePtsUs, impactPtsUs, videoDurationS ->
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
                        appendLog(
                            "armed-shot: ${bytes.size / 1024} KB gap=${(gapS * 1000).toInt()} ms dur=${"%.2f".format(videoDurationS)}s, uploading...",
                            ok = true,
                        )
                    }
                    uploadShot(bytes, releaseS = releaseInMp4S, impactS = impactInMp4S)
                } == true
                if (!ok) appendLog("arm failed — camera not ready", error = true)
            }
            "disarm" -> {
                cameraController?.disarm()
            }
        }
    }

    private fun uploadCalibrationFrame(bytes: ByteArray) {
        val host = currentHost ?: return
        scope.launch(Dispatchers.IO) {
            try {
                val body = bytes.toRequestBody("video/mp4".toMediaType())
                val req = Request.Builder()
                    .url("http://$host:$SERVER_PORT/api/calibration-frame")
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

    private fun uploadShot(bytes: ByteArray, releaseS: Double? = null, impactS: Double? = null) {
        val host = currentHost ?: return
        scope.launch(Dispatchers.IO) {
            try {
                val body = bytes.toRequestBody("video/mp4".toMediaType())
                val builder = Request.Builder()
                    .url("http://$host:$SERVER_PORT/api/shot")
                    .post(body)
                if (releaseS != null) builder.addHeader("X-Arrow-Release-S", "%.6f".format(releaseS))
                if (impactS != null) builder.addHeader("X-Arrow-Impact-S", "%.6f".format(impactS))
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
