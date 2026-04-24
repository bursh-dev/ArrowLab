package com.arrowlab.phone

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import android.media.MediaRecorder
import android.os.SystemClock
import android.view.Surface
import java.io.File
import java.nio.ByteBuffer
import java.util.concurrent.ConcurrentLinkedDeque

/**
 * Continuously drains MediaCodec H.264 (video) and AAC-LC (audio) encoders
 * into rolling ring buffers keyed by presentation timestamp (microseconds,
 * SystemClock.elapsedRealtimeNanos-based for both tracks so they share a
 * timebase). On demand, muxes a recent window into an in-memory MP4 with
 * both tracks.
 */
class ShotRecorder(
    private val width: Int,
    private val height: Int,
    private val fps: Int,
    private val bitrate: Int = 16_000_000,
    private val bufferSeconds: Double = 10.0,
    private val cacheDir: File,
    private val onEvent: (String, Boolean) -> Unit,
) {
    private data class Nal(
        val bytes: ByteArray,
        val ptsUs: Long,
        val isKey: Boolean,
        val isConfig: Boolean,
    )

    private data class AudioSample(val bytes: ByteArray, val ptsUs: Long)

    private val ringBuffer = ConcurrentLinkedDeque<Nal>()
    private val audioBuffer = ConcurrentLinkedDeque<AudioSample>()
    private val bufferMaxUs = (bufferSeconds * 1_000_000).toLong()

    // Video
    private var encoder: MediaCodec? = null
    private var inputSurface: Surface? = null
    private var drainThread: Thread? = null
    @Volatile private var encoderFormat: MediaFormat? = null

    // Audio
    private var audioRecord: AudioRecord? = null
    private var audioEncoder: MediaCodec? = null
    private var audioPumpThread: Thread? = null
    private var audioDrainThread: Thread? = null
    @Volatile private var audioFormat: MediaFormat? = null
    private val audioSampleRate = 44_100
    private val audioChannels = 1
    private val audioBitrate = 64_000

    @Volatile private var running = false

    // Armed-continuous mode: feeds the onset detector with live PCM and calls
    // back when a release -> impact pair is seen.
    private val onsetDetector = OnsetDetector(audioSampleRate)
    @Volatile private var armedCallback: ((bytes: ByteArray, releasePtsUs: Long, impactPtsUs: Long, videoDurationS: Double) -> Unit)? = null
    private val triggerPool = java.util.concurrent.Executors.newSingleThreadExecutor { r ->
        Thread(r, "ShotRecorder-trigger").also { it.isDaemon = true }
    }

    fun arm(onShot: (bytes: ByteArray, releasePtsUs: Long, impactPtsUs: Long, videoDurationS: Double) -> Unit) {
        armedCallback = onShot
        onsetDetector.arm { releasePts, impactPts ->
            val postPadMs = 220L
            triggerPool.execute {
                try {
                    Thread.sleep(postPadMs)
                } catch (_: InterruptedException) {}
                // Minimum 1.5 s so the slice always contains a video keyframe
                // (encoder emits one every 1 s).
                val gapS = (impactPts - releasePts) / 1_000_000.0
                val durationS = (gapS + 0.4).coerceAtLeast(1.5)
                val result = sliceLastSeconds(durationS)
                if (result != null) armedCallback?.invoke(result.bytes, releasePts, impactPts, result.videoDurationS)
            }
        }
        onEvent("armed: listening for release->impact", false)
    }

    fun disarm() {
        onsetDetector.disarm()
        armedCallback = null
        onEvent("disarmed", false)
    }

    fun getInputSurface(): Surface? = inputSurface

    fun start() {
        if (running) return
        running = true
        startVideo()
        startAudio()
    }

    fun stop() {
        if (!running) return
        running = false
        disarm()
        triggerPool.shutdown()
        drainThread?.join(1000)
        audioPumpThread?.join(1000)
        audioDrainThread?.join(1000)
        try { encoder?.stop() } catch (_: Throwable) {}
        try { encoder?.release() } catch (_: Throwable) {}
        try { inputSurface?.release() } catch (_: Throwable) {}
        try { audioEncoder?.stop() } catch (_: Throwable) {}
        try { audioEncoder?.release() } catch (_: Throwable) {}
        try { audioRecord?.stop() } catch (_: Throwable) {}
        try { audioRecord?.release() } catch (_: Throwable) {}
        encoder = null
        inputSurface = null
        drainThread = null
        audioRecord = null
        audioEncoder = null
        audioPumpThread = null
        audioDrainThread = null
        ringBuffer.clear()
        audioBuffer.clear()
        encoderFormat = null
        audioFormat = null
    }

    // ---- video ----

    private fun startVideo() {
        val format = MediaFormat.createVideoFormat(
            MediaFormat.MIMETYPE_VIDEO_AVC, width, height
        ).apply {
            setInteger(
                MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface,
            )
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, fps)
            setFloat(MediaFormat.KEY_I_FRAME_INTERVAL, 1f)
        }
        encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC).apply {
            configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            inputSurface = createInputSurface()
            start()
        }
        drainThread = Thread({ drainVideo() }, "ShotRecorder-video-drain").also { it.start() }
        onEvent("video encoder started ${width}x${height}@${fps} ${bitrate / 1_000_000} Mbps", false)
    }

    private fun drainVideo() {
        val enc = encoder ?: return
        val info = MediaCodec.BufferInfo()
        while (running) {
            val idx = try {
                enc.dequeueOutputBuffer(info, 10_000)
            } catch (_: Throwable) { break }
            when {
                idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    encoderFormat = enc.outputFormat
                    onEvent("video encoder format ready", false)
                }
                idx == MediaCodec.INFO_TRY_AGAIN_LATER -> { /* poll again */ }
                idx >= 0 -> {
                    val buf = enc.getOutputBuffer(idx)
                    if (buf == null) {
                        enc.releaseOutputBuffer(idx, false)
                        continue
                    }
                    buf.position(info.offset)
                    buf.limit(info.offset + info.size)
                    val bytes = ByteArray(info.size)
                    buf.get(bytes)
                    val isConfig = (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0
                    val isKey = (info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME) != 0
                    if (!isConfig) {
                        ringBuffer.add(Nal(bytes, info.presentationTimeUs, isKey, false))
                        trimRing(ringBuffer) { it.ptsUs }
                    }
                    enc.releaseOutputBuffer(idx, false)
                    if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) break
                }
            }
        }
    }

    // ---- audio ----

    private fun startAudio() {
        try {
            val channelCfg = AudioFormat.CHANNEL_IN_MONO
            val fmt = AudioFormat.ENCODING_PCM_16BIT
            val minBuf = AudioRecord.getMinBufferSize(audioSampleRate, channelCfg, fmt)
            if (minBuf <= 0) throw IllegalStateException("getMinBufferSize=$minBuf")
            val bufBytes = minBuf.coerceAtLeast(8192) * 2
            @Suppress("MissingPermission")
            audioRecord = AudioRecord(
                MediaRecorder.AudioSource.MIC,
                audioSampleRate,
                channelCfg,
                fmt,
                bufBytes,
            )
            if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
                throw IllegalStateException("AudioRecord not initialized (state=${audioRecord?.state})")
            }
            val mediaFmt = MediaFormat.createAudioFormat(
                MediaFormat.MIMETYPE_AUDIO_AAC, audioSampleRate, audioChannels,
            ).apply {
                setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
                setInteger(MediaFormat.KEY_BIT_RATE, audioBitrate)
                setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, bufBytes)
            }
            audioEncoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC).apply {
                configure(mediaFmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
                start()
            }
            audioRecord?.startRecording()
            audioPumpThread = Thread({ pumpAudio() }, "ShotRecorder-audio-pump").also { it.start() }
            audioDrainThread = Thread({ drainAudio() }, "ShotRecorder-audio-drain").also { it.start() }
            onEvent("audio encoder started ${audioSampleRate} Hz mono AAC ${audioBitrate / 1000} kbps", false)
        } catch (t: Throwable) {
            onEvent("audio disabled: ${t.message}", true)
            try { audioEncoder?.release() } catch (_: Throwable) {}
            try { audioRecord?.release() } catch (_: Throwable) {}
            audioEncoder = null
            audioRecord = null
        }
    }

    private fun pumpAudio() {
        val ar = audioRecord ?: return
        val enc = audioEncoder ?: return
        val startUs = SystemClock.elapsedRealtimeNanos() / 1000L
        var framesRead = 0L
        val pcm = ByteArray(4096)
        val bytesPerFrame = 2 * audioChannels // PCM_16BIT = 2 bytes
        while (running) {
            val read = try { ar.read(pcm, 0, pcm.size) } catch (_: Throwable) { -1 }
            if (read <= 0) continue
            val idx = try { enc.dequeueInputBuffer(10_000) } catch (_: Throwable) { continue }
            if (idx < 0) continue
            val inBuf = try { enc.getInputBuffer(idx) } catch (_: Throwable) { null } ?: continue
            inBuf.clear()
            inBuf.put(pcm, 0, read)
            // PTS of this buffer = start + frames-so-far / sampleRate.
            val ptsUs = startUs + (framesRead * 1_000_000L / audioSampleRate)
            try {
                enc.queueInputBuffer(idx, 0, read, ptsUs, 0)
            } catch (_: Throwable) { break }
            // Feed the live detector with this PCM chunk (no-op when disarmed).
            onsetDetector.process(pcm, read, ptsUs)
            framesRead += (read / bytesPerFrame)
        }
        // Signal EOS to the audio encoder.
        try {
            val idx = enc.dequeueInputBuffer(10_000)
            if (idx >= 0) enc.queueInputBuffer(idx, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
        } catch (_: Throwable) {}
    }

    private fun drainAudio() {
        val enc = audioEncoder ?: return
        val info = MediaCodec.BufferInfo()
        while (running) {
            val idx = try { enc.dequeueOutputBuffer(info, 10_000) } catch (_: Throwable) { break }
            when {
                idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    audioFormat = enc.outputFormat
                    onEvent("audio encoder format ready", false)
                }
                idx == MediaCodec.INFO_TRY_AGAIN_LATER -> { /* poll again */ }
                idx >= 0 -> {
                    val buf = try { enc.getOutputBuffer(idx) } catch (_: Throwable) { null }
                    if (buf == null) {
                        try { enc.releaseOutputBuffer(idx, false) } catch (_: Throwable) {}
                        continue
                    }
                    val isConfig = (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0
                    if (!isConfig && info.size > 0) {
                        buf.position(info.offset)
                        buf.limit(info.offset + info.size)
                        val bytes = ByteArray(info.size)
                        buf.get(bytes)
                        audioBuffer.add(AudioSample(bytes, info.presentationTimeUs))
                        trimRing(audioBuffer) { it.ptsUs }
                    }
                    try { enc.releaseOutputBuffer(idx, false) } catch (_: Throwable) {}
                    if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) break
                }
            }
        }
    }

    // ---- ring management ----

    private inline fun <T> trimRing(buf: ConcurrentLinkedDeque<T>, getPts: (T) -> Long) {
        val newest = getPts(buf.peekLast() ?: return)
        while (true) {
            val oldest = buf.peekFirst() ?: break
            if (newest - getPts(oldest) > bufferMaxUs) buf.pollFirst() else break
        }
    }

    // ---- muxing ----

    /** Result of a slice: the muxed mp4 bytes plus the actual video-track
     *  duration in seconds (what the browser will report). */
    data class SliceResult(val bytes: ByteArray, val videoDurationS: Double)

    fun sliceLastSeconds(seconds: Double): SliceResult? {
        val vFormat = encoderFormat ?: run {
            onEvent("slice: video encoder format not ready yet", true)
            return null
        }
        val videoFrames = ringBuffer.toList()
        if (videoFrames.isEmpty()) {
            onEvent("slice: ring buffer empty", true)
            return null
        }
        val newest = videoFrames.last().ptsUs
        val cutStart = newest - (seconds * 1_000_000).toLong()
        val startIdx = videoFrames.indexOfFirst { it.isKey && it.ptsUs >= cutStart }
        if (startIdx < 0) {
            onEvent("slice: no key frame in window (buffer too short?)", true)
            return null
        }
        val videoSlice = videoFrames.subList(startIdx, videoFrames.size)
        val basePts = videoSlice.first().ptsUs
        val durationUs = newest - basePts

        // Audio: take the LAST `durationUs` of audio (same duration as the
        // video slice, not the requested slice duration). Both tracks in
        // the output end at "now" in real time, so aligning by matching
        // duration keeps them synchronized end-to-end — the two clocks
        // (video = sensor timebase, audio = SystemClock) don't need to
        // share a common zero.
        val aFormat = audioFormat
        val (audioSlice, audioBasePts) = if (aFormat != null) {
            val audioAll = audioBuffer.toList()
            val audioNewest = audioAll.lastOrNull()?.ptsUs
            if (audioNewest == null) {
                emptyList<AudioSample>() to 0L
            } else {
                val audioStart = audioNewest - durationUs
                audioAll.filter { it.ptsUs >= audioStart } to audioStart
            }
        } else emptyList<AudioSample>() to 0L

        val out = File(cacheDir, "shot-${System.currentTimeMillis()}.mp4")
        return try {
            val muxer = MediaMuxer(out.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
            val videoTrack = muxer.addTrack(vFormat)
            val audioTrack = if (aFormat != null) muxer.addTrack(aFormat) else -1
            muxer.start()

            val maxVideo = videoSlice.maxOf { it.bytes.size }
            val bbV = ByteBuffer.allocate(maxVideo)
            val info = MediaCodec.BufferInfo()
            for (nal in videoSlice) {
                bbV.clear(); bbV.put(nal.bytes); bbV.flip()
                info.offset = 0
                info.size = nal.bytes.size
                info.presentationTimeUs = nal.ptsUs - basePts
                info.flags = if (nal.isKey) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0
                muxer.writeSampleData(videoTrack, bbV, info)
            }
            if (audioTrack >= 0 && audioSlice.isNotEmpty()) {
                val maxAudio = audioSlice.maxOf { it.bytes.size }
                val bbA = ByteBuffer.allocate(maxAudio)
                for (s in audioSlice) {
                    bbA.clear(); bbA.put(s.bytes); bbA.flip()
                    info.offset = 0
                    info.size = s.bytes.size
                    info.presentationTimeUs = s.ptsUs - audioBasePts
                    info.flags = 0
                    muxer.writeSampleData(audioTrack, bbA, info)
                }
            }
            muxer.stop()
            muxer.release()
            val bytes = out.readBytes()
            val videoDurationS = durationUs / 1_000_000.0
            onEvent(
                "slice: ${videoSlice.size} v / ${audioSlice.size} a, ${bytes.size / 1024} KB, dur=%.3fs".format(videoDurationS),
                false,
            )
            SliceResult(bytes, videoDurationS)
        } catch (e: Throwable) {
            onEvent("mux error: ${e.message}", true)
            null
        } finally {
            out.delete()
        }
    }
}
