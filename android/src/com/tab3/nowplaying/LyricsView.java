package com.tab3.nowplaying;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Typeface;
import android.text.Layout;
import android.text.StaticLayout;
import android.text.TextPaint;
import android.view.View;

import java.util.ArrayList;
import java.util.List;

/** Синхронный текст: текущая строка яркая и на 40% высоты, остальные приглушены,
 *  прокрутка плавная, края мягко затухают. Рисуется видеокартой, без отдельных слоёв. */
public class LyricsView extends View {
    private final List<Long> times = new ArrayList<Long>();
    private final List<String> texts = new ArrayList<String>();
    private final List<StaticLayout> layouts = new ArrayList<StaticLayout>();
    private final TextPaint paint = new TextPaint(Paint.ANTI_ALIAS_FLAG);
    private float[] tops = new float[0];
    private float totalHeight;
    private boolean synced;
    private int current = -1;
    private float offset;
    private int builtWidth = -1;
    private float plainProgress;
    private long lastFrame;

    public LyricsView(Context c) {
        super(c);
        paint.setColor(0xffffffff);
        paint.setTypeface(Typeface.create("sans-serif", Typeface.BOLD));
        paint.setTextSize(sp(26));
    }

    private float sp(float v) {
        return v * getResources().getDisplayMetrics().scaledDensity;
    }

    public void setTypeface(Typeface tf) {
        paint.setTypeface(tf);
        builtWidth = -1;
        invalidate();
    }

    /** synced: times != null; иначе обычный текст без таймингов. */
    public void setLyrics(List<Long> t, List<String> lines, boolean isSynced) {
        times.clear();
        texts.clear();
        if (t != null) times.addAll(t);
        texts.addAll(lines);
        synced = isSynced;
        current = -1;
        builtWidth = -1;
        offset = Float.NaN; // первый кадр — сразу на место, без пролистывания
        invalidate();
    }

    public boolean hasLyrics() {
        return !texts.isEmpty();
    }

    /** Позиция трека в мс. */
    public void setPosition(long posMs, long durMs) {
        if (texts.isEmpty()) return;
        if (synced) {
            // двоичный поиск последней строки, которая уже началась (чуть заранее, как в Spotify)
            long p = posMs + 250;
            int lo = 0, hi = times.size() - 1, idx = -1;
            while (lo <= hi) {
                int mid = (lo + hi) >>> 1;
                if (times.get(mid) <= p) { idx = mid; lo = mid + 1; } else hi = mid - 1;
            }
            if (idx != current) {
                current = idx;
                invalidate();
            }
        } else if (durMs > 0) {
            float f = Math.max(0, Math.min(1, (float) posMs / durMs));
            if (Math.abs(f - plainProgress) > 0.0005f) {
                plainProgress = f;
                invalidate();
            }
        }
    }

    private void build(int width) {
        layouts.clear();
        float gap = sp(14);
        tops = new float[texts.size()];
        float y = 0;
        for (int i = 0; i < texts.size(); i++) {
            String s = texts.get(i);
            StaticLayout l = new StaticLayout(s.length() == 0 ? "♪" : s, paint, width,
                    Layout.Alignment.ALIGN_NORMAL, 1.05f, 0, false);
            layouts.add(l);
            tops[i] = y;
            y += l.getHeight() + gap;
        }
        totalHeight = y;
        builtWidth = width;
    }

    private float targetOffset(int h) {
        if (synced) {
            if (current < 0) return -h * 0.4f;
            return tops[current] + layouts.get(current).getHeight() / 2f - h * 0.4f;
        }
        return plainProgress * Math.max(0, totalHeight - h * 0.6f) - h * 0.1f;
    }

    @Override
    protected void onDraw(Canvas canvas) {
        int w = getWidth(), h = getHeight();
        if (texts.isEmpty() || w <= 0 || h <= 0) return;
        if (builtWidth != w) build(w);

        float target = targetOffset(h);
        long now = System.nanoTime();
        if (Float.isNaN(offset)) offset = target;
        // плавность не зависит от частоты кадров: ~200 мс на основную часть пути
        float dt = lastFrame == 0 ? 16f : Math.min(64f, (now - lastFrame) / 1e6f);
        lastFrame = now;
        float diff = target - offset;
        offset += diff * (1f - (float) Math.pow(0.88, dt / 16f));
        boolean moving = Math.abs(diff) > 0.5f;
        if (!moving) {
            offset = target;
            lastFrame = 0;
        }

        float fadeZone = h * 0.15f;
        for (int i = 0; i < layouts.size(); i++) {
            StaticLayout l = layouts.get(i);
            float y = tops[i] - offset;
            float bottom = y + l.getHeight();
            if (bottom < 0 || y > h) continue;
            int alpha;
            if (!synced) alpha = 200;
            else if (i == current) alpha = 255;
            else alpha = Math.abs(i - current) == 1 ? 125 : 75;
            // затухание у верхнего и нижнего края
            float edge = Math.min((bottom) / fadeZone, (h - y) / fadeZone);
            if (edge < 1f) alpha = (int) (alpha * Math.max(0f, edge));
            if (alpha <= 0) continue;
            paint.setAlpha(alpha);
            canvas.save();
            canvas.translate(0, y);
            l.draw(canvas);
            canvas.restore();
        }

        if (moving) postInvalidateOnAnimation();
    }
}
