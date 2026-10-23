package com.tab3.nowplaying;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.PorterDuff;
import android.graphics.PorterDuffXfermode;
import android.graphics.Shader;
import android.graphics.Typeface;
import android.text.Layout;
import android.text.StaticLayout;
import android.text.TextPaint;
import android.view.View;

import java.util.ArrayList;
import java.util.List;

/** Синхронный текст: текущая строка яркая и по центру, остальные приглушены, прокрутка плавная. */
public class LyricsView extends View {
    private final List<Long> times = new ArrayList<Long>();
    private final List<String> texts = new ArrayList<String>();
    private final List<StaticLayout> layouts = new ArrayList<StaticLayout>();
    private final TextPaint paint = new TextPaint(Paint.ANTI_ALIAS_FLAG);
    private final Paint fade = new Paint();
    private boolean synced;
    private int current = -1;
    private float offset, target;
    private int builtWidth = -1;
    private float plainProgress;

    public LyricsView(Context c) {
        super(c);
        paint.setColor(0xffffffff);
        paint.setTypeface(Typeface.create("sans-serif", Typeface.BOLD));
        paint.setTextSize(sp(26));
        setLayerType(LAYER_TYPE_SOFTWARE, null); // нужно для маски затухания краёв
        fade.setXfermode(new PorterDuffXfermode(PorterDuff.Mode.DST_IN));
    }

    private float sp(float v) {
        return v * getResources().getDisplayMetrics().scaledDensity;
    }

    /** synced: times != null; иначе обычный текст без таймингов. */
    public void setLyrics(List<Long> t, List<String> lines, boolean isSynced) {
        times.clear();
        texts.clear();
        if (t != null) times.addAll(t);
        texts.addAll(lines);
        synced = isSynced;
        current = -1;
        offset = target = 0;
        builtWidth = -1;
        invalidate();
    }

    public boolean hasLyrics() {
        return !texts.isEmpty();
    }

    /** Позиция трека в мс. */
    public void setPosition(long posMs, long durMs) {
        if (texts.isEmpty()) return;
        if (synced) {
            int idx = -1;
            for (int i = 0; i < times.size(); i++) {
                if (times.get(i) <= posMs + 250) idx = i; else break; // чуть заранее, как в Spotify
            }
            if (idx != current) {
                current = idx;
                invalidate();
            }
        } else if (durMs > 0) {
            plainProgress = Math.max(0, Math.min(1, (float) posMs / durMs));
            invalidate();
        }
    }

    private void build(int width) {
        layouts.clear();
        for (String s : texts) {
            layouts.add(new StaticLayout(s.length() == 0 ? "♪" : s, paint, width,
                    Layout.Alignment.ALIGN_NORMAL, 1.05f, 0, false));
        }
        builtWidth = width;
    }

    @Override
    protected void onDraw(Canvas canvas) {
        int w = getWidth(), h = getHeight();
        if (texts.isEmpty() || w <= 0) return;
        if (builtWidth != w) build(w);
        float gap = sp(14);

        // где должна оказаться текущая строка: на 40% высоты
        float total = 0;
        float[] tops = new float[layouts.size()];
        for (int i = 0; i < layouts.size(); i++) {
            tops[i] = total;
            total += layouts.get(i).getHeight() + gap;
        }
        if (synced) {
            int c = Math.max(current, 0);
            target = tops[c] + layouts.get(c).getHeight() / 2f - h * 0.4f;
            if (current < 0) target = -h * 0.4f;
        } else {
            target = plainProgress * Math.max(0, total - h * 0.6f) - h * 0.1f;
        }
        float diff = target - offset;
        offset += diff * 0.12f;
        boolean moving = Math.abs(diff) > 0.5f;
        if (!moving) offset = target;

        int save = canvas.saveLayer(0, 0, w, h, null, Canvas.ALL_SAVE_FLAG);
        for (int i = 0; i < layouts.size(); i++) {
            float y = tops[i] - offset;
            StaticLayout l = layouts.get(i);
            if (y + l.getHeight() < 0 || y > h) continue;
            int alpha;
            if (!synced) alpha = 200;
            else if (i == current) alpha = 255;
            else alpha = Math.abs(i - current) == 1 ? 120 : 70;
            paint.setAlpha(alpha);
            canvas.save();
            canvas.translate(0, y);
            l.draw(canvas);
            canvas.restore();
        }
        // мягкое затухание сверху и снизу
        fade.setShader(new LinearGradient(0, 0, 0, h,
                new int[]{0x00000000, 0xff000000, 0xff000000, 0x00000000},
                new float[]{0f, 0.15f, 0.8f, 1f}, Shader.TileMode.CLAMP));
        canvas.drawRect(0, 0, w, h, fade);
        canvas.restoreToCount(save);

        if (moving) postInvalidateOnAnimation();
    }
}
