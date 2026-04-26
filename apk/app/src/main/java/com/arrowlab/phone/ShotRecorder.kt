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

    /** Raw S16LE PCM mono frame at sample rate [audioSampleRate], with the
     * pts of the first sample. Parallel to [audioBuffer] — kept separately
     * so we can extract a window around any pts for the sound-match
     * pre-filter without having to decode AAC. */
    private data class PcmChunk(val bytes: ByteArray, val ptsUs: Long)

    private val ringBuffer = ConcurrentLinkedDeque<Nal>()
    private val audioBuffer = ConcurrentLinkedDeque<AudioSample>()
    private val pcmBuffer = ConcurrentLinkedDeque<PcmChunk>()
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
    @Volatile private var armedCallback: ArmedCallback? = null
    @Volatile private var matchChecker: MatchChecker? = null
    private val triggerPool = java.util.concurrent.Executors.newSingleThreadExecutor { r ->
        Thread(r, "ShotRecorder-trigger").also { it.isDaemon = true }
    }

    // Calibration recording: when [calibrationAccumulator] is non-null, every
    // PCM chunk read in [pumpAudio] is appended to it. Once we've collected
    // [calibrationTargetBytes] bytes (= durationS * sampleRate * 2 for s16le
    // mono), we fire [calibrationCallback] and clear. Independent of the
    // ring buffers — fresh capture each invocation, no rolling.
    @Volatile private var calibrationAccumulator: java.io.ByteArrayOutputStream? = null
    @Volatile private var calibrationTargetBytes: Int = 0
    @Volatile private var calibrationCallback: ((ByteArray?, Int) -> Unit)? = null

    /** Result of a sound-match check against the server's per-session
     *  templates. On network/HTTP error the caller should return
     *  `accept = true` so transient outages don't drop real shots. */
    data class MatchResult(
        val accept: Boolean,
        val releaseSim: Double? = null,
        val impactSim: Double? = null,
        val noTemplate: Boolean = false,
        val error: String? = null,
    )

    /** Synchronous sound-match probe. Called from the trigger pool thread,
     *  so a blocking HTTP call is fine. The PCM blobs are raw S16LE mono
     *  at [sampleRate], 300 ms each. */
    fun interface MatchChecker {
        fun check(releasePcm: ByteArray, impactPcm: ByteArray, sampleRate: Int): MatchResult
    }

    fun interface ArmedCallback {
        fun onShot(
            bytes: ByteArray,
            releasePtsUs: Long,
            impactPtsUs: Long,
            videoDurationS: Double,
            releaseSim: Double?,
            impactSim: Double?,
        )
    }

    /** Capture [durationS] seconds of raw S16LE mono PCM at [audioSampleRate]
     * by siphoning the existing audio pump into a fresh accumulator. The
     * callback fires once with the full byte array + sample rate. Independent
     * of armed mode, doesn't disturb the AAC encoder or the rolling buffers.
     * Caller is expected to upload the result to the server. */
    fun recordCalibrationAudio(durationS: Double, onDone: (ByteArray?, Int) -> Unit) {
        if (calibrationAccumulator != null) {
            onDone(null, 0)
            return
        }
        val targetBytes = (audioSampleRate * 2 * durationS).toInt()
        calibrationTargetBytes = targetBytes
        calibrationCallback = onDone
        calibrationAccumulator = java.io.ByteArrayOutputStream(targetBytes)
    }

    fun arm(matchChecker: MatchChecker?, onShot: ArmedCallback) {
        armedCallback = onShot
        this.matchChecker = matchChecker
        onsetDetector.arm { releasePts, impactPts ->
            val postPadMs = 220L
            triggerPool.execute {
                try {
                    Thread.sleep(postPadMs)
                } catch (_: InterruptedException) {}

                // Sound-match pre-filter. Reject silently on a confident
                // miss (cough/door/etc.); proceed on accept or transient
                // failure (no template stored, network error, decode
                // failure on the server). Sims (which may be null on
                // those non-strict-accept paths) are passed through to
                // the upload so the shot record carries them.
                var releaseSim: Double? = null
                var impactSim: Double? = null
                val checker = this.matchChecker
                if (checker != null) {
                    val pcmDur = 0.30
                    val rPcm = slicePcmAround(releasePts, pcmDur)
                    val iPcm = slicePcmAround(impactPts, pcmDur)
                    if (rPcm == null || iPcm == null) {
                        onEvent(
                            "sound-match: pcm slice failed (rel=${rPcm?.size ?: -1}B imp=${iPcm?.size ?: -1}B), proceeding anyway",
                            true,
                        )
                    } else {
                        val match = checker.check(rPcm, iPcm, audioSampleRate)
                        releaseSim = match.releaseSim
                        impactSim = match.impactSim
                        if (!match.accept) {
                            onEvent(
                                "sound-match REJECT r=%s i=%s%s".format(
                                    match.releaseSim?.let { "%.2f".format(it) } ?: "-",
                                    match.impactSim?.let { "%.2f".format(it) } ?: "-",
                                    if (match.error != null) " err=${match.error}" else "",
                                ),
                                false,
                            )
                            return@execute
                        }
                        val tag = when {
                            match.error != null -> "(error=${match.error}, accepting)"
                            match.noTemplate -> "(no template, accepting)"
                            else -> "r=%.2f i=%.2f".format(match.releaseSim ?: 0.0, match.impactSim ?: 0.0)
                        }
                        onEvent("sound-match accept: $tag", false)
                    }
                }

                // Minimum 1.5 s so the slice always contains a video keyframe
                // (encoder emits one every 1 s).
                val gapS = (impactPts - releasePts) / 1_000_000.0
                val durationS = (gapS + 0.4).coerceAtLeast(1.5)
                val result = sliceLastSeconds(durationS)
                if (result != null) {
                    armedCallback?.onShot(
                        result.bytes, releasePts, impactPts, result.videoDurationS,
                        releaseSim, impactSim,
                    )
                }
            }
        }
        onEvent("armed: listening for release->impact", false)
    }

    fun disarm() {
        onsetDetector.disarm()
        armedCallback = null
        matchChecker = null
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
        pcmBuffer.clear()
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
            // If a calibration recording is active, siphon the same chunk
            // into its accumulator. Capped at calibrationTargetBytes —
            // we trim the last chunk and fire the callback when we cross
            // the threshold.
            calibrationAccumulator?.let { acc ->
                val target = calibrationTargetBytes
                val haveBefore = acc.size()
                if (haveBefore >= target) return@let
                val remaining = target - haveBefore
                val take = if (read <= remaining) read else remaining
                acc.write(pcm, 0, take)
                if (acc.size() >= target) {
                    val bytes = acc.toByteArray()
                    val cb = calibrationCallback
                    calibrationCallback = null
                    calibrationAccumulator = null
                    calibrationTargetBytes = 0
                    cb?.invoke(bytes, audioSampleRate)
                }
            }
            // Stash a copy of the raw PCM in the ring so the sound-match
            // pre-filter can extract a window around any pts later. The
            // 4096-byte chunk is reused next iteration, hence the copy.
            val pcmCopy = pcm.copyOf(read)
            pcmBuffer.add(PcmChunk(pcmCopy, ptsUs))
            trimRing(pcmBuffer) { it.ptsUs }
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

    /** Mux only the latest keyframe from the ring buffer into a 1-frame mp4.
     *  Used for calibration: server extracts the JPEG via ffmpeg in ms, no
     *  on-phone decode needed. Returned bytes are typically 100-300 KB at
     *  1080p — small enough for Cloudflare tunnel uploads. */
    fun sliceLastKeyframeMp4(): ByteArray? {
        val vFormat = encoderFormat ?: run {
            onEvent("calib slice: video format not ready", true)
            return null
        }
        val frames = ringBuffer.toList()
        val keyIdx = frames.indexOfLast { it.isKey }
        if (keyIdx < 0) {
            onEvent("calib slice: no keyframe in ring", true)
            return null
        }
        val key = frames[keyIdx]
        val out = File(cacheDir, "calib-${System.currentTimeMillis()}.mp4")
        return try {
            val muxer = MediaMuxer(out.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
            val track = muxer.addTrack(vFormat)
            muxer.start()
            val bb = ByteBuffer.wrap(key.bytes)
            val info = MediaCodec.BufferInfo()
            info.offset = 0
            info.size = key.bytes.size
            info.presentationTimeUs = 0
            info.flags = MediaCodec.BUFFER_FLAG_KEY_FRAME
            muxer.writeSampleData(track, bb, info)
            muxer.stop()
            muxer.release()
            out.readBytes()
        } catch (e: Throwable) {
            onEvent("calib mux: ${e.message}", true)
            null
        } finally {
            try { out.delete() } catch (_: Throwable) {}
        }
    }

    /** Concatenate every PCM chunk that overlaps [centerPtsUs ± durationS/2]
     *  into a single S16LE blob, trimmed to the window. Returns null if
     *  the ring doesn't cover the window at all. The window may be slightly
     *  shorter than requested at the head/tail of the ring; the server
     *  windows + zero-pads to a fixed length anyway. */
    fun slicePcmAround(centerPtsUs: Long, durationS: Double): ByteArray? {
        val halfUs = (durationS * 500_000).toLong()
        val startPts = centerPtsUs - halfUs
        val endPts = centerPtsUs + halfUs
        val chunks = pcmBuffer.toList()
        if (chunks.isEmpty()) return null
        val out = java.io.ByteArrayOutputStream()
        val bytesPerSample = 2  // PCM_16BIT mono
        for (chunk in chunks) {
            val chunkStartPts = chunk.ptsUs
            val chunkSamples = chunk.bytes.size / bytesPerSample
            val chunkEndPts = chunkStartPts + (chunkSamples * 1_000_000L / audioSampleRate)
            if (chunkEndPts < startPts || chunkStartPts > endPts) continue
            val effStart = maxOf(chunkStartPts, startPts)
            val effEnd = minOf(chunkEndPts, endPts)
            val sampleOff = ((effStart - chunkStartPts) * audioSampleRate / 1_000_000L).toInt()
            val sampleLen = ((effEnd - effStart) * audioSampleRate / 1_000_000L).toInt()
            if (sampleLen <= 0) continue
            val byteOff = sampleOff * bytesPerSample
            val byteLen = sampleLen * bytesPerSample
            val maxLen = (chunk.bytes.size - byteOff).coerceAtLeast(0)
            val copyLen = minOf(byteLen, maxLen)
            if (copyLen > 0) out.write(chunk.bytes, byteOff, copyLen)
        }
        return if (out.size() == 0) null else out.toByteArray()
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
