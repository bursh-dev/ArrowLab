package com.arrowlab.phone

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import android.view.Surface
import java.io.File
import java.nio.ByteBuffer
import java.util.concurrent.ConcurrentLinkedDeque

/**
 * Continuously drains a MediaCodec H.264 encoder into a rolling ring buffer of
 * encoded NAL units keyed by presentation timestamp. On demand, muxes a recent
 * window into an in-memory MP4 suitable for `POST /api/shot`.
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

    private val ringBuffer = ConcurrentLinkedDeque<Nal>()
    private val bufferMaxUs = (bufferSeconds * 1_000_000).toLong()
    private var encoder: MediaCodec? = null
    private var inputSurface: Surface? = null
    private var drainThread: Thread? = null
    @Volatile private var running = false
    @Volatile private var encoderFormat: MediaFormat? = null

    fun getInputSurface(): Surface? = inputSurface

    fun start() {
        if (running) return
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
        running = true
        drainThread = Thread({ drainLoop() }, "ShotRecorder-drain").also { it.start() }
        onEvent("encoder started ${width}x${height}@${fps} ${bitrate / 1_000_000} Mbps", false)
    }

    fun stop() {
        if (!running) return
        running = false
        drainThread?.join(1000)
        try { encoder?.stop() } catch (_: Throwable) {}
        try { encoder?.release() } catch (_: Throwable) {}
        try { inputSurface?.release() } catch (_: Throwable) {}
        encoder = null
        inputSurface = null
        drainThread = null
        ringBuffer.clear()
        encoderFormat = null
    }

    private fun drainLoop() {
        val enc = encoder ?: return
        val info = MediaCodec.BufferInfo()
        while (running) {
            val idx = try {
                enc.dequeueOutputBuffer(info, 10_000)
            } catch (_: Throwable) { break }
            when {
                idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    encoderFormat = enc.outputFormat
                    onEvent("encoder format ready", false)
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
                        trimBuffer()
                    }
                    enc.releaseOutputBuffer(idx, false)
                    if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) break
                }
            }
        }
    }

    private fun trimBuffer() {
        val newest = ringBuffer.peekLast()?.ptsUs ?: return
        while (true) {
            val oldest = ringBuffer.peekFirst() ?: break
            if (newest - oldest.ptsUs > bufferMaxUs) {
                ringBuffer.pollFirst()
            } else {
                break
            }
        }
    }

    fun sliceLastSeconds(seconds: Double): ByteArray? {
        val format = encoderFormat ?: run {
            onEvent("slice: encoder format not ready yet", true)
            return null
        }
        val frames = ringBuffer.toList()
        if (frames.isEmpty()) {
            onEvent("slice: ring buffer empty", true)
            return null
        }
        val newest = frames.last().ptsUs
        val cutStart = newest - (seconds * 1_000_000).toLong()
        val startIdx = frames.indexOfFirst { it.isKey && it.ptsUs >= cutStart }
        if (startIdx < 0) {
            onEvent("slice: no key frame in window (buffer too short?)", true)
            return null
        }
        val slice = frames.subList(startIdx, frames.size)
        val basePts = slice.first().ptsUs

        val out = File(cacheDir, "shot-${System.currentTimeMillis()}.mp4")
        return try {
            val muxer = MediaMuxer(out.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
            val track = muxer.addTrack(format)
            muxer.start()
            val maxSize = slice.maxOf { it.bytes.size }
            val bb = ByteBuffer.allocate(maxSize)
            val bufInfo = MediaCodec.BufferInfo()
            for (nal in slice) {
                bb.clear()
                bb.put(nal.bytes)
                bb.flip()
                bufInfo.offset = 0
                bufInfo.size = nal.bytes.size
                bufInfo.presentationTimeUs = nal.ptsUs - basePts
                bufInfo.flags = if (nal.isKey) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0
                muxer.writeSampleData(track, bb, bufInfo)
            }
            muxer.stop()
            muxer.release()
            val bytes = out.readBytes()
            onEvent("slice: ${slice.size} frames, ${bytes.size / 1024} KB", false)
            bytes
        } catch (e: Throwable) {
            onEvent("mux error: ${e.message}", true)
            null
        } finally {
            out.delete()
        }
    }
}
