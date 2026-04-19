package com.arrowlab.phone

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.util.AttributeSet
import android.view.View

/**
 * Transparent overlay drawn on top of the camera preview. Renders the
 * per-session corridor + target + bbox so the operator can verify the
 * annotation against the real scene.
 */
class AnnotationOverlay @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyle: Int = 0,
) : View(context, attrs, defStyle) {

    data class Annotation(
        val corridorTop: Int? = null,
        val corridorBottom: Int? = null,
        val targetCx: Int? = null,
        val targetCy: Int? = null,
        val targetR: Int = 0,
        val bbox: IntArray? = null,
        val imageW: Int = 0,
        val imageH: Int = 0,
    )

    private var annotation: Annotation? = null

    private val corridorPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#00e5ff")
        style = Paint.Style.STROKE
        strokeWidth = 4f
    }
    private val targetRingPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#ffa500")
        style = Paint.Style.STROKE
        strokeWidth = 4f
    }
    private val targetDotPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#ff4040")
        style = Paint.Style.FILL
    }
    private val bboxPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.YELLOW
        style = Paint.Style.STROKE
        strokeWidth = 3f
        pathEffect = DashPathEffect(floatArrayOf(16f, 8f), 0f)
    }

    fun setAnnotation(a: Annotation?) {
        annotation = a
        invalidate()
    }

    fun clear() = setAnnotation(null)

    override fun onDraw(canvas: Canvas) {
        val a = annotation ?: return
        if (a.imageW <= 0 || a.imageH <= 0) return
        val sx = width.toFloat() / a.imageW
        val sy = height.toFloat() / a.imageH
        val savg = (sx + sy) / 2f

        if (a.corridorTop != null && a.corridorBottom != null) {
            val yt = a.corridorTop * sy
            val yb = a.corridorBottom * sy
            canvas.drawLine(0f, yt, width.toFloat(), yt, corridorPaint)
            canvas.drawLine(0f, yb, width.toFloat(), yb, corridorPaint)
        }
        if (a.targetCx != null && a.targetCy != null) {
            val cx = a.targetCx * sx
            val cy = a.targetCy * sy
            if (a.targetR > 0) {
                canvas.drawCircle(cx, cy, a.targetR * savg, targetRingPaint)
            }
            canvas.drawCircle(cx, cy, 6f, targetDotPaint)
        }
        a.bbox?.let { b ->
            canvas.drawRect(b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy, bboxPaint)
        }
    }
}
