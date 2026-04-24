package com.arrowlab.phone

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Matrix
import android.graphics.RectF
import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraConstrainedHighSpeedCaptureSession
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.MediaCodec
import android.media.MediaCodecList
import android.media.MediaFormat
import android.os.Handler
import android.os.HandlerThread
import android.util.Range
import android.util.Size
import android.view.Surface
import android.view.TextureView
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream
import kotlin.math.max

/**
 * Camera2 pipeline wrapping a high-speed capture session that feeds two outputs:
 *   1. a TextureView preview surface, and
 *   2. a MediaCodec H.264 encoder (inside [ShotRecorder]) that continuously fills
 *      a rolling ring buffer used for shot slicing.
 *
 * Calibration JPEG captures grab a bitmap from the TextureView rather than an
 * ImageReader since the high-speed session only supports preview+encoder surfaces.
 */
class CameraController(
    private val context: Context,
    private val textureView: TextureView,
    private val onEvent: (msg: String, error: Boolean) -> Unit,
) {
    private val manager by lazy {
        context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
    }

    private var cameraDevice: CameraDevice? = null
    private var captureSession: CameraCaptureSession? = null
    private var shotRecorder: ShotRecorder? = null

    private var bgThread: HandlerThread? = null
    private var bgHandler: Handler? = null

    private var sensorOrientation: Int = 0
    private var previewSize: Size = Size(1920, 1080)
    private var fpsRange: Range<Int> = Range(30, 30)
    private var started = false

    fun getStillSize(): Size = previewSize

    fun start() {
        if (started) return
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED) {
            onEvent("camera permission not granted", true)
            return
        }
        started = true
        bgThread = HandlerThread("camera-bg").also { it.start() }
        bgHandler = Handler(bgThread!!.looper)

        if (textureView.isAvailable) {
            openCameraInternal()
        } else {
            textureView.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
                override fun onSurfaceTextureAvailable(st: SurfaceTexture, w: Int, h: Int) {
                    openCameraInternal()
                }
                override fun onSurfaceTextureSizeChanged(st: SurfaceTexture, w: Int, h: Int) {}
                override fun onSurfaceTextureDestroyed(st: SurfaceTexture) = true
                override fun onSurfaceTextureUpdated(st: SurfaceTexture) {}
            }
        }
    }

    fun stop() {
        if (!started) return
        started = false
        try { captureSession?.close() } catch (_: Throwable) {}
        captureSession = null
        try { shotRecorder?.stop() } catch (_: Throwable) {}
        shotRecorder = null
        try { cameraDevice?.close() } catch (_: Throwable) {}
        cameraDevice = null
        bgThread?.quitSafely()
        bgThread = null
        bgHandler = null
    }

    @SuppressLint("MissingPermission")
    private fun openCameraInternal() {
        val rearId = manager.cameraIdList.firstOrNull {
            val c = manager.getCameraCharacteristics(it)
            c.get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
        }
        if (rearId == null) {
            onEvent("no rear camera found", true)
            return
        }
        val chars = manager.getCameraCharacteristics(rearId)
        sensorOrientation = chars.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 0
        val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        if (map == null) {
            onEvent("no stream config map", true)
            return
        }

        val hsSizes = map.highSpeedVideoSizes ?: emptyArray()
        if (hsSizes.isEmpty()) {
            onEvent("device reports no high-speed video sizes", true)
            return
        }
        // Pick largest 16:9 size ≤ 1920x1080; fall back to any largest 16:9 or the first.
        val hsSorted = hsSizes.sortedByDescending { it.width.toLong() * it.height }
        previewSize = hsSorted.firstOrNull {
            val a = it.width.toFloat() / it.height
            kotlin.math.abs(a - 16f / 9f) < 0.02f && it.width <= 1920
        } ?: hsSorted.firstOrNull {
            val a = it.width.toFloat() / it.height
            kotlin.math.abs(a - 16f / 9f) < 0.02f
        } ?: hsSorted[0]

        val ranges = map.getHighSpeedVideoFpsRangesFor(previewSize) ?: emptyArray()
        if (ranges.isEmpty()) {
            onEvent("no high-speed fps ranges for ${previewSize.width}x${previewSize.height}", true)
            return
        }
        onEvent(
            "hs ranges for ${previewSize.width}x${previewSize.height}: " +
                ranges.joinToString { "${it.lower}-${it.upper}" },
            false,
        )
        // Probe the H.264 hardware encoder for what it can actually do at this resolution.
        val (encoderMaxFps, encoderMaxBitrate) = probeEncoderLimits(previewSize.width, previewSize.height)
        onEvent(
            "encoder caps at ${previewSize.width}x${previewSize.height}: " +
                "maxFps=$encoderMaxFps maxBitrate=${encoderMaxBitrate / 1_000_000} Mbps",
            false,
        )

        // Prefer fixed-rate (CFR) ranges — Samsung's slow-mo pipeline uses e.g. 240-240,
        // while the variable (VFR) range 30-240 tends to crash the HAL under real load.
        val cfrRanges = ranges.filter { it.lower == it.upper }
        fpsRange = cfrRanges
            .filter { it.upper <= encoderMaxFps }
            .maxByOrNull { it.upper }
            ?: ranges
                .filter { it.upper <= encoderMaxFps }
                .maxByOrNull { it.upper }
            ?: ranges.maxByOrNull { it.upper }!!
        // Bitrate scales with fps so the encoder has headroom. Cap to what the encoder allows.
        val targetBitrate = ((fpsRange.upper.toLong() * 200_000L)
            .coerceAtMost(encoderMaxBitrate.toLong())
            .coerceAtLeast(8_000_000L)).toInt()
        onEvent(
            "high-speed: ${previewSize.width}x${previewSize.height} @ ${fpsRange.lower}-${fpsRange.upper} fps " +
                "bitrate=${targetBitrate / 1_000_000} Mbps",
            false,
        )

        shotRecorder = ShotRecorder(
            width = previewSize.width,
            height = previewSize.height,
            fps = fpsRange.upper,
            bitrate = targetBitrate,
            cacheDir = context.cacheDir,
            onEvent = onEvent,
        ).also { it.start() }

        manager.openCamera(rearId, object : CameraDevice.StateCallback() {
            override fun onOpened(device: CameraDevice) {
                cameraDevice = device
                createHighSpeedSession()
            }
            override fun onDisconnected(device: CameraDevice) {
                device.close()
                cameraDevice = null
            }
            override fun onError(device: CameraDevice, error: Int) {
                onEvent("camera error ${cameraErrorName(error)}", true)
                device.close()
                cameraDevice = null
            }
        }, bgHandler)
    }

    private fun createHighSpeedSession() {
        val device = cameraDevice ?: return
        val st = textureView.surfaceTexture ?: return
        val recorderSurface = shotRecorder?.getInputSurface() ?: run {
            onEvent("encoder surface not ready", true)
            return
        }
        st.setDefaultBufferSize(previewSize.width, previewSize.height)
        val previewSurface = Surface(st)

        @Suppress("DEPRECATION")
        device.createConstrainedHighSpeedCaptureSession(
            listOf(previewSurface, recorderSurface),
            object : CameraCaptureSession.StateCallback() {
                override fun onConfigured(session: CameraCaptureSession) {
                    captureSession = session
                    try {
                        val builder = device.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
                            addTarget(previewSurface)
                            addTarget(recorderSurface)
                            set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fpsRange)
                        }
                        val hs = session as CameraConstrainedHighSpeedCaptureSession
                        val burst = hs.createHighSpeedRequestList(builder.build())
                        hs.setRepeatingBurst(burst, null, bgHandler)
                        textureView.post { configurePreviewTransform() }
                    } catch (e: Throwable) {
                        onEvent("setRepeatingBurst failed: ${e.message}", true)
                    }
                }
                override fun onConfigureFailed(session: CameraCaptureSession) {
                    onEvent("high-speed session config failed", true)
                }
            },
            bgHandler,
        )
    }

    private fun configurePreviewTransform() {
        val activity = context as? Activity ?: return
        val viewW = textureView.width
        val viewH = textureView.height
        if (viewW == 0 || viewH == 0) return

        val rotation = activity.windowManager.defaultDisplay.rotation
        onEvent(
            "preview: sensor=$sensorOrientation° rotation=$rotation " +
                "preview=${previewSize.width}x${previewSize.height} view=${viewW}x${viewH}",
            false,
        )

        val matrix = Matrix()
        val viewRect = RectF(0f, 0f, viewW.toFloat(), viewH.toFloat())
        val bufferRect = RectF(0f, 0f, previewSize.height.toFloat(), previewSize.width.toFloat())
        val centerX = viewRect.centerX()
        val centerY = viewRect.centerY()
        if (rotation == Surface.ROTATION_90 || rotation == Surface.ROTATION_270) {
            bufferRect.offset(centerX - bufferRect.centerX(), centerY - bufferRect.centerY())
            matrix.setRectToRect(viewRect, bufferRect, Matrix.ScaleToFit.FILL)
            val scale = max(
                viewH.toFloat() / previewSize.height,
                viewW.toFloat() / previewSize.width,
            )
            matrix.postScale(scale, scale, centerX, centerY)
            matrix.postRotate(90f * (rotation - 2), centerX, centerY)
        } else if (rotation == Surface.ROTATION_180) {
            matrix.postRotate(180f, centerX, centerY)
        }
        textureView.setTransform(matrix)
    }

    fun captureStillJpeg(onDone: (ByteArray?) -> Unit) {
        val handler = bgHandler
        if (handler == null) { onDone(null); return }
        handler.post {
            try {
                val bitmap = Bitmap.createBitmap(
                    previewSize.width,
                    previewSize.height,
                    Bitmap.Config.ARGB_8888,
                )
                textureView.getBitmap(bitmap)
                val baos = ByteArrayOutputStream()
                bitmap.compress(Bitmap.CompressFormat.JPEG, 85, baos)
                bitmap.recycle()
                onDone(baos.toByteArray())
            } catch (t: Throwable) {
                onEvent("getBitmap failed: ${t.message}", true)
                onDone(null)
            }
        }
    }

    private fun probeEncoderLimits(w: Int, h: Int): Pair<Int, Int> {
        val mimeType = MediaFormat.MIMETYPE_VIDEO_AVC
        val list = MediaCodecList(MediaCodecList.REGULAR_CODECS)
        var bestFps = 30
        var bestBitrate = 16_000_000
        for (info in list.codecInfos) {
            if (!info.isEncoder) continue
            if (mimeType !in info.supportedTypes) continue
            val caps = info.getCapabilitiesForType(mimeType) ?: continue
            val v = caps.videoCapabilities ?: continue
            if (!v.isSizeSupported(w, h)) continue
            val framesRange = v.getSupportedFrameRatesFor(w, h)
            val br = v.bitrateRange
            if (framesRange.upper.toInt() > bestFps) {
                bestFps = framesRange.upper.toInt()
                bestBitrate = br.upper
            }
        }
        return bestFps to bestBitrate
    }

    private fun cameraErrorName(code: Int): String = when (code) {
        CameraDevice.StateCallback.ERROR_CAMERA_IN_USE -> "IN_USE(1)"
        CameraDevice.StateCallback.ERROR_MAX_CAMERAS_IN_USE -> "MAX_IN_USE(2)"
        CameraDevice.StateCallback.ERROR_CAMERA_DISABLED -> "DISABLED(3)"
        CameraDevice.StateCallback.ERROR_CAMERA_DEVICE -> "DEVICE(4)"
        CameraDevice.StateCallback.ERROR_CAMERA_SERVICE -> "SERVICE(5)"
        else -> "unknown($code)"
    }

    fun sliceLastSeconds(seconds: Double, onDone: (ByteArray?) -> Unit) {
        val rec = shotRecorder
        val handler = bgHandler
        if (rec == null || handler == null) { onDone(null); return }
        handler.post {
            val result = rec.sliceLastSeconds(seconds)
            onDone(result?.bytes)
        }
    }

    fun arm(onShot: (bytes: ByteArray, releasePtsUs: Long, impactPtsUs: Long, videoDurationS: Double) -> Unit): Boolean {
        val rec = shotRecorder ?: return false
        rec.arm(onShot)
        return true
    }

    fun disarm() {
        shotRecorder?.disarm()
    }
}
