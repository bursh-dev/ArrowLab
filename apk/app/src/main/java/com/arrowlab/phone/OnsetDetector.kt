package com.arrowlab.phone

import android.util.Log
import kotlin.math.abs

/**
 * Streaming onset detector for the bow-release -> arrow-impact acoustic pair.
 * Feeds it S16LE PCM chunks from AudioRecord; it rolls an energy envelope
 * and reports an [onShot] callback every time a release followed by an
 * impact is seen.
 *
 * The state machine:
 *   IDLE -> first loud transient => RELEASE (remember pts)
 *   RELEASE -> within [minGapMs, maxGapMs] another transient => IMPACT (fire callback)
 *   RELEASE -> no impact within maxGapMs => back to IDLE (abandon)
 *
 * Threshold is dynamic (median of last 1s of envelope, scaled). Works without
 * per-session calibration in quiet environments; per-session templates can be
 * layered on later if detection rate drops.
 */
class OnsetDetector(
    private val sampleRate: Int,
    private val minGapMs: Int = 100,
    private val maxGapMs: Int = 500,
    private val envWinMs: Int = 5,
    private val noiseWinMs: Int = 1_000,
    private val thresholdScale: Float = 3.5f,
    private val cooldownMs: Int = 60,
    private val minImpactAbsolute: Float = 0.02f,
    private val impactOverReleaseRatio: Float = 1.5f,
) {
    enum class Phase { IDLE, WAITING_IMPACT }

    private var phase = Phase.IDLE
    private var releasePtsUs: Long = 0
    private var releaseEnv: Float = 0f
    // Intentionally 0 (not Long.MIN_VALUE) — subtracting MIN_VALUE from a
    // positive pts overflows Long and makes the cooldown check fail.
    private var lastEventPtsUs: Long = 0
    private var onShot: ((releasePtsUs: Long, impactPtsUs: Long) -> Unit)? = null
    private var running = false
    // Debug: track the loudest envelope sample we've seen this second.
    private var windowPeakEnv: Float = 0f
    private var windowStartPtsUs: Long = 0

    private val envWinSamples = (sampleRate * envWinMs / 1000).coerceAtLeast(1)
    private val noiseWinSamples = (sampleRate * noiseWinMs / 1000).coerceAtLeast(envWinSamples)
    // Rolling envelope — one sample per `envWinSamples` input frames.
    private val envHistory = FloatArray(noiseWinSamples / envWinSamples)
    private var envIdx = 0
    private var envFilled = 0

    // Short-window running sum for the envelope.
    private var winSum = 0.0
    private var winCount = 0

    fun arm(onShot: (releasePtsUs: Long, impactPtsUs: Long) -> Unit) {
        phase = Phase.IDLE
        releasePtsUs = 0
        lastEventPtsUs = 0
        this.onShot = onShot
        running = true
    }

    fun disarm() {
        running = false
        onShot = null
        phase = Phase.IDLE
        winSum = 0.0
        winCount = 0
        envIdx = 0
        envFilled = 0
        windowPeakEnv = 0f
        windowStartPtsUs = 0
    }

    /**
     * Feed one chunk of S16LE PCM. `chunkStartPtsUs` is the pts of the first
     * sample in [pcm] (same timebase as ShotRecorder's audio ring buffer).
     */
    fun process(pcm: ByteArray, bytesRead: Int, chunkStartPtsUs: Long) {
        if (!running) return
        val cb = onShot ?: return
        var i = 0
        var samplesConsumed = 0L
        while (i + 1 < bytesRead) {
            // Little-endian S16 → signed short.
            val lo = pcm[i].toInt() and 0xff
            val hi = pcm[i + 1].toInt()
            val s = (hi shl 8) or lo
            val absVal = abs(s).toFloat() / 32768f
            winSum += absVal
            winCount++
            if (winCount >= envWinSamples) {
                val env = (winSum / winCount).toFloat()
                onEnvelopeSample(env, chunkStartPtsUs + samplesConsumed * 1_000_000L / sampleRate)
                winSum = 0.0
                winCount = 0
            }
            i += 2
            samplesConsumed++
        }
    }

    private fun onEnvelopeSample(env: Float, envPtsUs: Long) {
        // Dynamic noise floor: mean of envHistory.
        val noise = if (envFilled > 0) {
            var sum = 0f
            for (j in 0 until envFilled) sum += envHistory[j]
            sum / envFilled
        } else 0f
        val threshold = (noise * thresholdScale).coerceAtLeast(0.003f)
        // Require at least 250 ms of envelope history before the detector
        // is allowed to fire — otherwise the first quiet samples after
        // arm() clear a meaningless 0.003 floor and register as shots.
        val warmUpSamples = 50  // 50 * 5 ms = 250 ms
        val warmedUp = envFilled >= warmUpSamples
        val isPeak = warmedUp && env > threshold
        val sinceLastEvent = envPtsUs - lastEventPtsUs
        val cooldown = cooldownMs * 1000L

        if (isPeak && sinceLastEvent > cooldown) {
            lastEventPtsUs = envPtsUs
            when (phase) {
                Phase.IDLE -> {
                    phase = Phase.WAITING_IMPACT
                    releasePtsUs = envPtsUs
                    releaseEnv = env
                    Log.d("ArrowLab", "onset: release? env=%.4f noise=%.4f thr=%.4f".format(env, noise, threshold))
                }
                Phase.WAITING_IMPACT -> {
                    val gapMs = (envPtsUs - releasePtsUs) / 1000L
                    val louderEnough = env >= releaseEnv * impactOverReleaseRatio
                    val absLoudEnough = env >= minImpactAbsolute
                    Log.d(
                        "ArrowLab",
                        "onset: second? env=%.4f gap=%dms lr=%.2fx absOk=%s"
                            .format(env, gapMs, env / releaseEnv.coerceAtLeast(1e-6f), absLoudEnough),
                    )
                    if (gapMs in minGapMs..maxGapMs && louderEnough && absLoudEnough) {
                        onShot?.invoke(releasePtsUs, envPtsUs)
                        phase = Phase.IDLE
                        releasePtsUs = 0
                        releaseEnv = 0f
                    } else if (gapMs > maxGapMs) {
                        // Stale release → let the new loud sample become a fresh release.
                        releasePtsUs = envPtsUs
                        releaseEnv = env
                    }
                    // Otherwise (too close, not loud enough): stay in WAITING
                    // for a later, bigger impact.
                }
            }
        }

        // Debug: dump loudest sample every second while armed so we can see
        // the ambient noise floor and peak amplitudes.
        if (env > windowPeakEnv) windowPeakEnv = env
        if (windowStartPtsUs == 0L) windowStartPtsUs = envPtsUs
        if (envPtsUs - windowStartPtsUs >= 1_000_000L) {
            Log.d(
                "ArrowLab",
                "onset-stats: peak=%.4f noise=%.4f thr=%.4f phase=%s"
                    .format(windowPeakEnv, noise, threshold, phase.name),
            )
            windowPeakEnv = 0f
            windowStartPtsUs = envPtsUs
        }

        // Update history ring.
        envHistory[envIdx] = env
        envIdx = (envIdx + 1) % envHistory.size
        if (envFilled < envHistory.size) envFilled++

        // Abandon stale release.
        if (phase == Phase.WAITING_IMPACT && envPtsUs - releasePtsUs > maxGapMs * 1000L) {
            phase = Phase.IDLE
            releasePtsUs = 0
        }
    }
}
