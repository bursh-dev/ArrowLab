package com.arrowlab.phone

import android.content.Context
import android.util.AttributeSet
import android.widget.FrameLayout

/**
 * FrameLayout that enforces a width:height aspect ratio, letterboxed inside whatever
 * space its parent gives it. Used to wrap the camera preview + annotation overlay so
 * both render at the correct shape regardless of the actual screen dimensions.
 */
class AspectFrameLayout @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyle: Int = 0,
) : FrameLayout(context, attrs, defStyle) {

    private var aspectRatio = 0f // width / height; 0 disables enforcement

    fun setAspectRatio(ratio: Float) {
        if (ratio <= 0f || ratio == aspectRatio) return
        aspectRatio = ratio
        requestLayout()
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        if (aspectRatio <= 0f) {
            super.onMeasure(widthMeasureSpec, heightMeasureSpec)
            return
        }
        val widthSize = MeasureSpec.getSize(widthMeasureSpec)
        val heightSize = MeasureSpec.getSize(heightMeasureSpec)
        if (widthSize == 0 || heightSize == 0) {
            super.onMeasure(widthMeasureSpec, heightMeasureSpec)
            return
        }
        val currentAspect = widthSize.toFloat() / heightSize
        val newW: Int
        val newH: Int
        if (currentAspect > aspectRatio) {
            newH = heightSize
            newW = (heightSize * aspectRatio).toInt()
        } else {
            newW = widthSize
            newH = (widthSize / aspectRatio).toInt()
        }
        // Constrain children to the aspect-correct size by passing EXACTLY specs.
        super.onMeasure(
            MeasureSpec.makeMeasureSpec(newW, MeasureSpec.EXACTLY),
            MeasureSpec.makeMeasureSpec(newH, MeasureSpec.EXACTLY),
        )
    }
}
