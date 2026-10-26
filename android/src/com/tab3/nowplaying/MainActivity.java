package com.tab3.nowplaying;

import android.animation.ArgbEvaluator;
import android.animation.ValueAnimator;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.BlurMaskFilter;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Typeface;
import android.graphics.drawable.BitmapDrawable;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.TransitionDrawable;
import android.net.DhcpInfo;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.SystemClock;
import android.text.InputType;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.GestureDetector;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.URL;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.Random;

public class MainActivity extends Activity {
    private static final int UDP_PORT = 47800;
    private static final long IDLE_AFTER_PAUSE_MS = 60000;

    private final Handler ui = new Handler();
    private final java.util.concurrent.ExecutorService pool = java.util.concurrent.Executors.newCachedThreadPool();
    private SharedPreferences prefs;
    private volatile String server;        // "ip:port" или null
    private volatile boolean running;
    private volatile int generation;

    private FrameLayout root;
    private GradientDrawable background;
    private int bgColor = 0xff202020;
    private LinearLayout player, idle;
    private ImageView cover;
    private TextView title, artist, unsynced, timeLeft, timeRight, clock, date, status, flashIcon;
    private View dark;                     // чёрная шторка «экран выключен»

    // фон экрана часов: фото или зацикленное видео из папки на ПК
    private FrameLayout bgLayer;
    private ImageView bgImage;
    private android.widget.VideoView bgVideo;
    private volatile String bgId;
    private String bgType;
    private boolean bgReady, bgPlaying;

    // видео за экраном трека: видеошот из Яндекс Музыки (готовит сервер на ПК)
    private FrameLayout clipLayer;
    private android.widget.VideoView clipVideo;
    private volatile String clipId;
    private boolean clipReady, clipPlaying;

    // обновление приложения: новый APK скачивает с GitHub сервер на ПК и отдаёт по сети
    private String updateTried;   // версия, которую уже пробовали поставить (чтобы не по кругу)

    // экран и подсветка
    private static final long DARK_AFTER_IDLE_MS = 30 * 60 * 1000;   // 30 минут без музыки — гасим
    private static final long WAKE_ON_TAP_MS = 60 * 1000;
    private long idleSince, wakeUntil;
    private float appliedBrightness = -2;
    private ProgressLine progress;
    private LyricsView lyrics;

    // последнее состояние с ПК
    private String trackId;
    private int artRev = -1;
    private long shownSecond = -2, shownDuration = -1;
    private boolean playing;
    private long position, duration, receivedAt, pausedSince;
    private long seekHoldUntil;   // после перемотки не верим старой позиции с сервера
    private boolean seekDragging; // палец тянет полоску прогресса
    private boolean lyricsDrag;   // палец листает текст
    private boolean volumeDrag;   // палец меняет громкость (по обложке)
    private int volume = -1, volumeStart;   // громкость ПК 0–100 (-1 — неизвестна)
    private long volumeSentAt;
    private FrameLayout coverFrame;
    private TextView volumeLabel;
    private final Runnable hideVolume = new Runnable() {
        public void run() { volumeLabel.animate().alpha(0f).setDuration(400).start(); }
    };
    private View bgDim;
    private Boolean nightShown;   // какой вид часов сейчас (ночной или нет)
    private int[] bgColors = {0xff202020, 0xff0e0e0e, 0xff0e0e0e};
    private boolean connected;

    private final Random random = new Random();
    private final SimpleDateFormat clockFmt = new SimpleDateFormat("HH:mm", Locale.getDefault());
    private final SimpleDateFormat dateFmt = new SimpleDateFormat("EEEE, d MMMM", new Locale("ru"));

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_FULLSCREEN
                | WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED   // поверх экрана блокировки (без пароля)
                | WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
                | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
        prefs = getSharedPreferences("np", MODE_PRIVATE);
        server = prefs.getString("server", null);
        installCrashRestart();
        new java.io.File(getFilesDir(), "update.apk").delete();   // обновление уже поставлено
        buildUi();
        setContentView(root);
    }

    @Override
    protected void onResume() {
        super.onResume();
        root.setSystemUiVisibility(View.SYSTEM_UI_FLAG_LOW_PROFILE);
        running = true;
        final int gen = ++generation;
        new Thread(new Runnable() { public void run() { networkLoop(gen); } }, "np-net").start();
        ui.post(ticker);
        ui.post(clockTick);
    }

    @Override
    protected void onPause() {
        super.onPause();
        running = false;
        syncBgVideo(false);
        syncClip(false);
        ui.removeCallbacks(ticker);
        ui.removeCallbacks(clockTick);
    }

    // ---------------- интерфейс ----------------

    private int dp(float v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private Typeface fontMedium, fontSemiBold, fontExtraBold;

    /** Inter из assets (похож на SF Pro из Apple Music); если файла нет — системный шрифт. */
    private Typeface font(String name, Typeface fallback) {
        try { return Typeface.createFromAsset(getAssets(), "fonts/Inter-" + name + ".ttf"); }
        catch (Exception e) { android.util.Log.w("NowPlaying", "font " + name + ": " + e); return fallback; }
    }

    private TextView text(float sp, boolean bold, int alpha) {
        if (fontMedium == null) {
            fontMedium = font("Medium", Typeface.DEFAULT);                  // исполнитель, время, дата
            fontSemiBold = font("DisplaySemiBold", Typeface.DEFAULT_BOLD);  // часы
            fontExtraBold = font("DisplayBold", Typeface.DEFAULT_BOLD);     // название и текст песни
        }
        TextView t = new TextView(this);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(Color.argb(alpha, 255, 255, 255));
        t.setTypeface(bold ? fontExtraBold : fontMedium);
        t.setShadowLayer(dp(3), 0, dp(1), 0x66000000);
        return t;
    }

    private void buildUi() {
        root = new FrameLayout(this);
        background = new GradientDrawable(GradientDrawable.Orientation.TL_BR, bgColors);
        root.setBackgroundDrawable(background);
        // жесты: тап — пауза/продолжить, свайп — следующий/предыдущий, долгое нажатие — адрес ПК
        final GestureDetector gestures = new GestureDetector(this, new GestureDetector.SimpleOnGestureListener() {
            @Override public boolean onDown(MotionEvent e) {
                lyricsDrag = false;
                volumeDrag = false;
                lyrics.stopFling();
                return true;
            }
            @Override public boolean onScroll(MotionEvent a, MotionEvent b, float dx, float dy) {
                if (player.getVisibility() != View.VISIBLE || dark.getVisibility() == View.VISIBLE) return false;
                float tx = b.getX() - a.getX(), ty = b.getY() - a.getY();
                if (!lyricsDrag && !volumeDrag && volume >= 0 && inside(coverFrame, a)
                        && Math.abs(ty) >= dp(12) && Math.abs(ty) > Math.abs(tx)) {
                    volumeDrag = true;     // вверх-вниз по обложке — громкость ПК
                    volumeStart = volume;
                }
                if (volumeDrag) {
                    // вся высота обложки — 100%
                    setVolume(volumeStart + Math.round(-ty / cover.getHeight() * 100), false);
                    return true;
                }
                if (!lyrics.hasLyrics()) return false;
                if (!lyricsDrag) {
                    // листаем только вертикальным движением, начатым на тексте
                    if (Math.abs(ty) < dp(12) || Math.abs(ty) < Math.abs(tx) || !inside(lyrics, a)) return false;
                    lyricsDrag = true;
                }
                lyrics.scrollByUser(dy);
                return true;
            }
            @Override public boolean onSingleTapUp(MotionEvent e) {
                if (wakeFromDark()) return true;
                if (player.getVisibility() == View.VISIBLE && lyrics.isBrowsing() && inside(lyrics, e)) {
                    // текст пролистан — тап по строке перематывает на неё
                    long t = lyrics.timeAt(e.getRawY() - screenY(lyrics));
                    if (t >= 0) {
                        seekTo(t);
                        lyrics.endBrowsing();
                    }
                    return true;
                }
                if (player.getVisibility() == View.VISIBLE) {
                    control("toggle");
                    flash(playing ? "❚❚" : "▶");
                }
                return true;
            }
            @Override public boolean onFling(MotionEvent a, MotionEvent b, float vx, float vy) {
                if (wakeFromDark() || player.getVisibility() != View.VISIBLE) return true;
                if (lyricsDrag) {
                    lyrics.flingByUser(vy);
                    return true;
                }
                if (volumeDrag) return true;
                float dx = b.getX() - a.getX();
                if (Math.abs(dx) > dp(100) && Math.abs(vx) > Math.abs(vy)) {
                    control(dx < 0 ? "next" : "prev");
                    flash(dx < 0 ? "⏭" : "⏮");
                }
                return true;
            }
            @Override public void onLongPress(MotionEvent e) { askServer(); }
        });
        root.setOnTouchListener(new View.OnTouchListener() {
            public boolean onTouch(View v, MotionEvent e) {
                if (dragSeek(e)) return true;
                if (volumeDrag && (e.getActionMasked() == MotionEvent.ACTION_UP
                        || e.getActionMasked() == MotionEvent.ACTION_CANCEL)) {
                    setVolume(volume, true);   // последнее значение — точно отправить
                    volumeDrag = false;
                }
                return gestures.onTouchEvent(e);
            }
        });

        // --- экран трека ---
        player = new LinearLayout(this);
        player.setOrientation(LinearLayout.HORIZONTAL);
        player.setGravity(Gravity.CENTER_VERTICAL);
        player.setPadding(dp(36), dp(36), dp(36), dp(36));

        cover = new ImageView(this);
        cover.setScaleType(ImageView.ScaleType.CENTER_CROP);
        cover.setImageDrawable(new ColorDrawable(0x33ffffff));
        // мягкая тень под обложкой: рамка шире обложки на SHADOW с каждой стороны,
        // а отрицательные отступы оставляют обложку на прежнем месте
        int sh = dp(SHADOW);
        coverFrame = new FrameLayout(this);
        coverFrame.setBackgroundDrawable(new BitmapDrawable(getResources(), coverShadow(dp(400), sh)));
        coverFrame.setPadding(sh, sh, sh, sh);
        coverFrame.addView(cover, new FrameLayout.LayoutParams(dp(400), dp(400)));
        volumeLabel = text(22, true, 255);
        volumeLabel.setShadowLayer(0, 0, 0, 0);
        volumeLabel.setGravity(Gravity.CENTER);
        GradientDrawable pill = new GradientDrawable();
        pill.setColor(0xb3000000);
        pill.setCornerRadius(dp(28));
        volumeLabel.setBackgroundDrawable(pill);
        volumeLabel.setPadding(dp(26), dp(12), dp(26), dp(12));
        volumeLabel.setAlpha(0f);
        coverFrame.addView(volumeLabel, new FrameLayout.LayoutParams(-2, -2, Gravity.CENTER));
        LinearLayout.LayoutParams cp = new LinearLayout.LayoutParams(dp(400) + 2 * sh, dp(400) + 2 * sh);
        cp.setMargins(-sh, -sh, -sh, -sh);
        player.addView(coverFrame, cp);

        LinearLayout right = new LinearLayout(this);
        right.setOrientation(LinearLayout.VERTICAL);
        right.setGravity(Gravity.CENTER_VERTICAL);
        LinearLayout.LayoutParams rp = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.MATCH_PARENT, 1);
        rp.leftMargin = dp(36);
        player.addView(right, rp);

        title = text(30, true, 255);
        title.setMaxLines(2);
        title.setEllipsize(TextUtils.TruncateAt.END);
        right.addView(title);
        artist = text(22, false, 200);
        artist.setSingleLine(true);
        artist.setEllipsize(TextUtils.TruncateAt.END);
        right.addView(artist);
        unsynced = text(13, false, 130);
        unsynced.setText("текст без синхронизации");
        unsynced.setVisibility(View.GONE);
        right.addView(unsynced);

        lyrics = new LyricsView(this);
        lyrics.setTypeface(fontExtraBold);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1);
        lp.topMargin = dp(18);
        lp.bottomMargin = dp(12);
        right.addView(lyrics, lp);
        lyrics.setVisibility(View.GONE);

        progress = new ProgressLine(this);
        LinearLayout.LayoutParams pp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(4));
        pp.topMargin = dp(14);
        right.addView(progress, pp);
        LinearLayout times = new LinearLayout(this);
        timeLeft = text(14, false, 170);
        timeRight = text(14, false, 170);
        timeRight.setGravity(Gravity.RIGHT);
        // у мелкого текста у края тень обрезается и выглядит как тёмная палочка — убираем
        timeLeft.setShadowLayer(0, 0, 0, 0);
        timeRight.setShadowLayer(0, 0, 0, 0);
        times.addView(timeLeft, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        times.addView(timeRight, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        right.addView(times);

        // --- видео за экраном трека (под ним) ---
        clipLayer = new FrameLayout(this);
        clipVideo = new android.widget.VideoView(this);
        clipLayer.addView(clipVideo, new FrameLayout.LayoutParams(-1, -1, Gravity.CENTER));
        View clipDim = new View(this);
        clipDim.setBackgroundColor(0x8c000000);   // ~55% затемнение, чтобы текст читался
        clipLayer.addView(clipDim, new FrameLayout.LayoutParams(-1, -1));
        clipLayer.setVisibility(View.GONE);
        root.addView(clipLayer, new FrameLayout.LayoutParams(-1, -1));

        root.addView(player, new FrameLayout.LayoutParams(-1, -1));

        // --- фон для часов (под ними) ---
        bgLayer = new FrameLayout(this);
        bgImage = new ImageView(this);
        bgImage.setScaleType(ImageView.ScaleType.CENTER_CROP);
        bgLayer.addView(bgImage, new FrameLayout.LayoutParams(-1, -1));
        bgVideo = new android.widget.VideoView(this);
        bgVideo.setVisibility(View.GONE);
        bgLayer.addView(bgVideo, new FrameLayout.LayoutParams(-1, -1, Gravity.CENTER));
        bgDim = new View(this);
        bgDim.setBackgroundColor(0x59000000);   // ~35% затемнение, чтобы часы читались
        bgLayer.addView(bgDim, new FrameLayout.LayoutParams(-1, -1));
        bgLayer.setVisibility(View.GONE);
        root.addView(bgLayer, new FrameLayout.LayoutParams(-1, -1));

        // --- часы в простое ---
        idle = new LinearLayout(this);
        idle.setOrientation(LinearLayout.VERTICAL);
        idle.setGravity(Gravity.CENTER);
        clock = text(130, false, 255);
        clock.setTypeface(fontSemiBold);
        clock.setGravity(Gravity.CENTER_HORIZONTAL);
        clock.setIncludeFontPadding(false);   // без лишнего отступа шрифта над цифрами
        date = text(26, false, 190);
        date.setGravity(Gravity.CENTER_HORIZONTAL);
        idle.addView(clock, new LinearLayout.LayoutParams(-2, -2));
        LinearLayout.LayoutParams dlp = new LinearLayout.LayoutParams(-2, -2);
        dlp.topMargin = dp(6);
        idle.addView(date, dlp);
        root.addView(idle, new FrameLayout.LayoutParams(-2, -2, Gravity.CENTER));

        status = text(14, false, 150);
        FrameLayout.LayoutParams sp = new FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL);
        sp.bottomMargin = dp(10);
        root.addView(status, sp);

        flashIcon = text(90, true, 230);
        flashIcon.setTypeface(Typeface.DEFAULT_BOLD); // значков ▶ ❚❚ в Inter нет
        flashIcon.setAlpha(0f);
        root.addView(flashIcon, new FrameLayout.LayoutParams(-2, -2, Gravity.CENTER));

        dark = new View(this);
        dark.setBackgroundColor(0xff000000);
        dark.setVisibility(View.GONE);
        root.addView(dark, new FrameLayout.LayoutParams(-1, -1));

        showIdle(true);
        status.setText("Ищу компьютер…  (долгое нажатие — ввести адрес вручную)");
    }

    private void showIdle(boolean on) {
        idle.setVisibility(on ? View.VISIBLE : View.GONE);
        player.setVisibility(on ? View.GONE : View.VISIBLE);
        bgLayer.setVisibility(on && bgReady ? View.VISIBLE : View.GONE);
        bgVideo.setVisibility(on && bgReady && "video".equals(bgType) ? View.VISIBLE : View.GONE);
    }

    /** Видео фона играет только на экране часов и пока экран не «спит». */
    private void syncBgVideo(boolean shouldPlay) {
        if (!"video".equals(bgType) || !bgReady) return;
        if (shouldPlay && !bgPlaying) { bgVideo.start(); bgPlaying = true; }
        else if (!shouldPlay && bgPlaying) { bgVideo.pause(); bgPlaying = false; }
    }

    private void clearBg() {
        bgReady = false;
        bgType = null;
        bgPlaying = false;
        bgVideo.stopPlayback();
        bgVideo.setVisibility(View.GONE);
        bgImage.setImageDrawable(null);
        bgLayer.setVisibility(View.GONE);
    }

    private void loadBg(final String srv, final String id, final String ext, final String type) {
        pool.execute(new Runnable() {
            public void run() {
                try {
                    final java.io.File dir = getCacheDir();
                    final java.io.File file = new java.io.File(dir, "bg_" + id + "." + ext);
                    if (!file.exists()) download("http://" + srv + "/bg/" + id + "." + ext, file);
                    for (java.io.File f : dir.listFiles()) {   // старые фоны больше не нужны
                        if (f.getName().startsWith("bg_") && !f.equals(file)) f.delete();
                    }
                    final Bitmap bmp = "image".equals(type) ? decodeForScreen(file) : null;
                    ui.post(new Runnable() {
                        public void run() {
                            if (id.equals(bgId)) showBg(type, file, bmp);
                        }
                    });
                } catch (Exception e) {
                    android.util.Log.w("NowPlaying", "background: " + e);
                    bgId = null;  // попробуем снова при следующем опросе
                }
            }
        });
    }

    private String myVersion() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "0";
        }
    }

    /** Сравнение версий вида 1.4.1: >0, если a новее b. */
    static int compareVersions(String a, String b) {
        String[] x = a.split("\\D+"), y = b.split("\\D+");
        for (int i = 0; i < Math.max(x.length, y.length); i++) {
            int p = i < x.length && x[i].length() > 0 ? Integer.parseInt(x[i]) : 0;
            int q = i < y.length && y[i].length() > 0 ? Integer.parseInt(y[i]) : 0;
            if (p != q) return p < q ? -1 : 1;
        }
        return 0;
    }

    /** Скачать новый APK с ПК и поставить: с root — тихо (и запустить снова), без root — окно установки. */
    private void installUpdate(final String srv, final String version) {
        pool.execute(new Runnable() {
            public void run() {
                try {
                    java.io.File file = new java.io.File(getFilesDir(), "update.apk");
                    download("http://" + srv + "/update/app.apk", file);
                    // окну установки (другое приложение) нужен доступ к файлу
                    file.setReadable(true, false);
                    getFilesDir().setExecutable(true, false);
                    android.util.Log.i("NowPlaying", "update " + version + " downloaded");
                    boolean silent = false;
                    try {
                        Process p = Runtime.getRuntime().exec(new String[]{"su", "-c",
                                "pm install -r '" + file.getPath() + "' && am start -n " + getPackageName() + "/.MainActivity"});
                        silent = p.waitFor() == 0;   // при успехе нас перезапустят раньше, чем дойдём сюда
                    } catch (Exception noRoot) {
                        // root нет — покажем обычное окно установки
                    }
                    if (!silent) {
                        final android.net.Uri uri = android.net.Uri.fromFile(file);
                        ui.post(new Runnable() {
                            public void run() {
                                android.content.Intent i = new android.content.Intent(android.content.Intent.ACTION_VIEW);
                                i.setDataAndType(uri, "application/vnd.android.package-archive");
                                i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                                startActivity(i);
                            }
                        });
                    }
                } catch (Exception e) {
                    android.util.Log.w("NowPlaying", "update: " + e);
                    updateTried = null;  // попробуем при следующем опросе
                }
            }
        });
    }

    private static void download(String url, java.io.File file) throws java.io.IOException {
        java.io.File part = new java.io.File(file.getPath() + ".part");
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(5000);
        c.setReadTimeout(30000);
        try {
            InputStream in = c.getInputStream();
            java.io.FileOutputStream out = new java.io.FileOutputStream(part);
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            out.close();
            in.close();
        } finally {
            c.disconnect();
        }
        if (!part.renameTo(file)) throw new java.io.IOException("rename");
    }

    /** Видео на весь экран с обрезкой краёв (как centerCrop у картинки). */
    private void fillScreen(android.widget.VideoView v, android.media.MediaPlayer mp) {
        int vw = mp.getVideoWidth(), vh = mp.getVideoHeight();
        int w = root.getWidth(), h = root.getHeight();
        if (vw > 0 && vh > 0 && w > 0 && h > 0) {
            float k = Math.max((float) w / vw, (float) h / vh);
            v.setLayoutParams(new FrameLayout.LayoutParams((int) (vw * k), (int) (vh * k), Gravity.CENTER));
        }
    }

    private void loadClip(final String srv, final String id) {
        pool.execute(new Runnable() {
            public void run() {
                try {
                    java.io.File dir = getCacheDir();
                    final java.io.File file = new java.io.File(dir, "clip_" + id + ".mp4");
                    if (!file.exists()) download("http://" + srv + "/clip/" + id + ".mp4", file);
                    for (java.io.File f : dir.listFiles()) {   // храним только текущий
                        if (f.getName().startsWith("clip_") && !f.equals(file)) f.delete();
                    }
                    ui.post(new Runnable() {
                        public void run() {
                            if (id.equals(clipId)) showClip(file);
                        }
                    });
                } catch (Exception e) {
                    android.util.Log.w("NowPlaying", "clip: " + e);
                    clipId = null;  // попробуем снова при следующем опросе
                }
            }
        });
    }

    private void showClip(java.io.File file) {
        clipVideo.stopPlayback();
        clipPlaying = false;
        clipVideo.setOnPreparedListener(new android.media.MediaPlayer.OnPreparedListener() {
            public void onPrepared(android.media.MediaPlayer mp) {
                mp.setLooping(true);
                mp.setVolume(0f, 0f);
                fillScreen(clipVideo, mp);
            }
        });
        clipVideo.setOnErrorListener(new android.media.MediaPlayer.OnErrorListener() {
            public boolean onError(android.media.MediaPlayer mp, int what, int extra) {
                android.util.Log.w("NowPlaying", "clip video error " + what + "/" + extra);
                clearClip();   // без системного окна: просто остаётся фон под цвет обложки
                return true;
            }
        });
        clipVideo.setVideoPath(file.getPath());
        clipReady = true;
    }

    private void clearClip() {
        clipReady = false;
        clipPlaying = false;
        clipVideo.stopPlayback();
        clipLayer.setVisibility(View.GONE);
        clipVideo.setVisibility(View.GONE);
    }

    /** Видео трека играет только на экране трека и пока экран не «спит». */
    private void syncClip(boolean shouldPlay) {
        shouldPlay &= clipReady;
        clipLayer.setVisibility(shouldPlay ? View.VISIBLE : View.GONE);
        clipVideo.setVisibility(shouldPlay ? View.VISIBLE : View.GONE);
        if (shouldPlay && !clipPlaying) { clipVideo.start(); clipPlaying = true; }
        else if (!shouldPlay && clipPlaying) { clipVideo.pause(); clipPlaying = false; }
    }

    private Bitmap decodeForScreen(java.io.File f) {
        BitmapFactory.Options o = new BitmapFactory.Options();
        o.inJustDecodeBounds = true;
        BitmapFactory.decodeFile(f.getPath(), o);
        int sample = 1;
        while (o.outWidth / (sample * 2) >= 1024 && o.outHeight / (sample * 2) >= 600) sample *= 2;
        o = new BitmapFactory.Options();
        o.inSampleSize = sample;
        return BitmapFactory.decodeFile(f.getPath(), o);
    }

    private void showBg(String type, java.io.File file, Bitmap bmp) {
        bgVideo.stopPlayback();
        bgPlaying = false;
        bgType = type;
        if ("image".equals(type)) {
            bgVideo.setVisibility(View.GONE);
            bgImage.setImageBitmap(bmp);
            bgReady = bmp != null;
        } else {
            bgImage.setImageDrawable(null);
            bgVideo.setVisibility(idle.getVisibility() == View.VISIBLE ? View.VISIBLE : View.GONE);
            bgVideo.setOnPreparedListener(new android.media.MediaPlayer.OnPreparedListener() {
                public void onPrepared(android.media.MediaPlayer mp) {
                    mp.setLooping(true);
                    mp.setVolume(0f, 0f);
                    fillScreen(bgVideo, mp);
                }
            });
            bgVideo.setOnErrorListener(new android.media.MediaPlayer.OnErrorListener() {
                public boolean onError(android.media.MediaPlayer mp, int what, int extra) {
                    android.util.Log.w("NowPlaying", "background video error " + what + "/" + extra);
                    clearBg();
                    return true;
                }
            });
            bgVideo.setVideoPath(file.getPath());
            bgReady = true;
        }
        bgLayer.setVisibility(idle.getVisibility() == View.VISIBLE && bgReady ? View.VISIBLE : View.GONE);
    }

    private static int darker(int c) {
        float[] hsv = new float[3];
        Color.colorToHSV(c, hsv);
        hsv[2] *= 0.45f;
        return Color.HSVToColor(hsv);
    }

    /** Фон: градиент из трёх цветов обложки (сервер присылает colors), иначе — из одного цвета. */
    private void animateBackground(JSONObject j) {
        int[] to;
        JSONArray cs = j.optJSONArray("colors");
        try {
            if (cs != null && cs.length() == 3) {
                to = new int[]{Color.parseColor(cs.getString(0)), Color.parseColor(cs.getString(1)),
                        Color.parseColor(cs.getString(2))};
            } else {
                int c = Color.parseColor(j.optString("color", "#202020"));
                to = new int[]{c, darker(c), darker(c)};
            }
        } catch (Exception e) {
            return;
        }
        if (java.util.Arrays.equals(to, bgColors)) return;
        final int[] from = bgColors.clone(), target = to;
        final ArgbEvaluator ev = new ArgbEvaluator();
        ValueAnimator a = ValueAnimator.ofFloat(0f, 1f);
        a.setDuration(1200);
        a.addUpdateListener(new ValueAnimator.AnimatorUpdateListener() {
            public void onAnimationUpdate(ValueAnimator va) {
                float f = va.getAnimatedFraction();
                int[] c = new int[3];
                for (int i = 0; i < 3; i++) c[i] = (Integer) ev.evaluate(f, from[i], target[i]);
                background.setColors(c);
            }
        });
        a.start();
        bgColors = to;
        bgColor = to[0];
    }

    private static final int SHADOW = 36;

    /** Картинка тени для обложки size×size с полями pad: размытие ~18 dp, 55%, сдвиг вниз 8 dp. */
    private Bitmap coverShadow(int size, int pad) {
        Bitmap b = Bitmap.createBitmap(size + 2 * pad, size + 2 * pad, Bitmap.Config.ARGB_8888);
        Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
        p.setColor(0x8c000000);
        p.setMaskFilter(new BlurMaskFilter(dp(30), BlurMaskFilter.Blur.NORMAL));
        new Canvas(b).drawRect(pad, pad + dp(8), pad + size, pad + size + dp(8), p);
        return b;
    }

    private void setCover(Bitmap bmp) {
        Drawable from = cover.getDrawable();
        // берём только последнюю картинку, иначе переходы вкладываются друг в друга и копят память
        if (from instanceof TransitionDrawable) from = ((TransitionDrawable) from).getDrawable(1);
        Drawable to = bmp != null ? new BitmapDrawable(getResources(), bmp) : new ColorDrawable(0x33ffffff);
        if (from == null) from = new ColorDrawable(0);
        TransitionDrawable t = new TransitionDrawable(new Drawable[]{from, to});
        t.setCrossFadeEnabled(true);
        cover.setImageDrawable(t);
        t.startTransition(500);
    }

    /** При падении перезапускаемся через 3 с (экран у колонки должен жить сам),
     *  но не чаще трёх раз за 5 минут — чтобы не было бесконечного цикла. */
    private void installCrashRestart() {
        final Thread.UncaughtExceptionHandler system = Thread.getDefaultUncaughtExceptionHandler();
        Thread.setDefaultUncaughtExceptionHandler(new Thread.UncaughtExceptionHandler() {
            public void uncaughtException(Thread th, Throwable ex) {
                try {
                    long now = System.currentTimeMillis();
                    String[] old = prefs.getString("crashes", "").split(",");
                    StringBuilder recent = new StringBuilder();
                    int count = 0;
                    for (String c : old) {
                        try {
                            if (now - Long.parseLong(c) < 5 * 60 * 1000) { recent.append(c).append(','); count++; }
                        } catch (NumberFormatException ignored) { }
                    }
                    prefs.edit().putString("crashes", recent.append(now).toString()).commit();
                    if (count < 3) {
                        android.app.PendingIntent pi = android.app.PendingIntent.getActivity(MainActivity.this, 0,
                                new android.content.Intent(MainActivity.this, MainActivity.class)
                                        .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK), 0);
                        ((android.app.AlarmManager) getSystemService(ALARM_SERVICE))
                                .set(android.app.AlarmManager.RTC, now + 3000, pi);
                    }
                } catch (Throwable ignored) { }
                if (system != null) system.uncaughtException(th, ex);
                else System.exit(2);
            }
        });
    }

    private static boolean isNight() {
        int h = java.util.Calendar.getInstance().get(java.util.Calendar.HOUR_OF_DAY);
        return h >= 23 || h < 8;
    }

    /** Подсветка: днём с музыкой — как в системе; часы — тускло; ночью — ещё тусклее;
     *  30 минут без музыки — чёрный экран с минимальной подсветкой (тап будит на минуту). */
    private void updateScreen(boolean idleNow, long t) {
        boolean night = isNight();
        boolean darkNow = idleNow && t - idleSince > DARK_AFTER_IDLE_MS && t > wakeUntil;
        dark.setVisibility(darkNow ? View.VISIBLE : View.GONE);
        if (nightShown == null || nightShown != night) {
            // ночью часы тёмно-красные (не слепят и не светят в комнату), фон затемнён сильнее
            nightShown = night;
            clock.setTextColor(night ? 0xffa3281e : 0xffffffff);
            date.setTextColor(night ? 0x99a3281e : Color.argb(190, 255, 255, 255));
            bgDim.setBackgroundColor(night ? 0xd9000000 : 0x59000000);
        }
        syncBgVideo(idleNow && !darkNow);
        syncClip(!idleNow && !darkNow);
        float b;
        if (darkNow) b = 0.0f;
        else if (idleNow) b = night ? 0.02f : (bgReady ? 0.35f : 0.15f);  // с фоном днём ярче, иначе его не видно
        else b = night ? 0.10f : -1f;    // -1 — яркость, выбранная в настройках
        if (b != appliedBrightness) {
            WindowManager.LayoutParams lp = getWindow().getAttributes();
            lp.screenBrightness = b;
            getWindow().setAttributes(lp);
            appliedBrightness = b;
        }
    }

    /** Если экран «спит» — будим его на минуту и не считаем касание командой. */
    private boolean wakeFromDark() {
        if (dark.getVisibility() != View.VISIBLE) return false;
        wakeUntil = SystemClock.elapsedRealtime() + WAKE_ON_TAP_MS;
        idleSince = SystemClock.elapsedRealtime() - DARK_AFTER_IDLE_MS + WAKE_ON_TAP_MS;
        return true;
    }

    private void flash(String symbol) {
        flashIcon.setText(symbol);
        flashIcon.animate().cancel();
        flashIcon.setAlpha(1f);
        flashIcon.animate().alpha(0f).setStartDelay(350).setDuration(500).start();
    }

    private void control(final String action) {
        final String s = server;
        if (s == null) return;
        pool.execute(new Runnable() {
            public void run() {
                try { get("http://" + s + "/api/control/" + action, 3000); } catch (Exception ignored) { }
            }
        });
    }

    /** Громкость ПК: подпись на обложке сразу, на ПК — не чаще раза в 80 мс (и всегда последнее). */
    private void setVolume(int level, boolean force) {
        level = Math.max(0, Math.min(100, level));
        volume = level;
        volumeLabel.setText("Громкость  " + level + "%");
        // аниматоры на 4.2 при частых перезапусках сразу ставят конечную прозрачность —
        // поэтому показываем сразу, а гасим один раз через секунду после последнего движения
        volumeLabel.animate().cancel();
        volumeLabel.setAlpha(1f);
        ui.removeCallbacks(hideVolume);
        ui.postDelayed(hideVolume, 1000);
        long t = SystemClock.elapsedRealtime();
        if (force || t - volumeSentAt > 80) {
            volumeSentAt = t;
            control("volume/" + level);
        }
    }

    private void seekTo(long ms) {
        if (duration > 0) ms = Math.min(ms, duration - 1000);
        ms = Math.max(0, ms);
        position = ms;
        receivedAt = SystemClock.elapsedRealtime();
        seekHoldUntil = receivedAt + 2500;
        control("seek/" + ms);
    }

    private float screenY(View v) {
        int[] xy = new int[2];
        v.getLocationOnScreen(xy);
        return xy[1];
    }

    private boolean inside(View v, MotionEvent e) {
        int[] xy = new int[2];
        v.getLocationOnScreen(xy);
        float x = e.getRawX(), y = e.getRawY();
        return x >= xy[0] && x < xy[0] + v.getWidth() && y >= xy[1] && y < xy[1] + v.getHeight();
    }

    /** Перемотка: палец на полоске прогресса (зона касания выше и ниже самой полоски). */
    private boolean dragSeek(MotionEvent e) {
        int action = e.getActionMasked();
        if (action == MotionEvent.ACTION_DOWN) {
            seekDragging = false;
            if (dark.getVisibility() == View.VISIBLE || player.getVisibility() != View.VISIBLE || duration <= 0) return false;
            int[] xy = new int[2];
            progress.getLocationOnScreen(xy);
            float x = e.getRawX(), y = e.getRawY();
            if (x < xy[0] - dp(12) || x > xy[0] + progress.getWidth() + dp(12)
                    || Math.abs(y - (xy[1] + progress.getHeight() / 2f)) > dp(22)) return false;
            seekDragging = true;
        }
        if (!seekDragging) return false;
        int[] xy = new int[2];
        progress.getLocationOnScreen(xy);
        float f = Math.max(0, Math.min(1, (e.getRawX() - xy[0]) / Math.max(1, progress.getWidth())));
        long ms = (long) (f * duration);
        progress.setFraction(f);
        progress.setActive(action != MotionEvent.ACTION_UP && action != MotionEvent.ACTION_CANCEL);
        timeLeft.setText(fmt(ms));
        if (action == MotionEvent.ACTION_UP) {
            seekDragging = false;
            seekTo(ms);
        } else if (action == MotionEvent.ACTION_CANCEL) {
            seekDragging = false;
        }
        return true;
    }

    private static String fmt(long ms) {
        long s = Math.max(0, ms / 1000);
        return String.format(Locale.US, "%d:%02d", s / 60, s % 60);
    }

    private long livePosition() {
        long p = position + (playing ? SystemClock.elapsedRealtime() - receivedAt : 0);
        return duration > 0 ? Math.min(p, duration) : p;
    }

    private final Runnable ticker = new Runnable() {
        public void run() {
            if (trackId != null && !seekDragging) {
                long p = livePosition();
                progress.setFraction(duration > 0 ? (float) p / duration : 0);
                long sec = duration > 0 ? p / 1000 : -1;
                if (sec != shownSecond || duration != shownDuration) {
                    shownSecond = sec;
                    shownDuration = duration;
                    timeLeft.setText(duration > 0 ? fmt(p) : "");
                    timeRight.setText(duration > 0 ? fmt(duration) : "");
                }
                lyrics.setPosition(p, duration);
            }
            long t = SystemClock.elapsedRealtime();
            boolean idleNow = !connected || trackId == null
                    || (!playing && t - pausedSince > IDLE_AFTER_PAUSE_MS);
            if (idleNow != (idle.getVisibility() == View.VISIBLE)) showIdle(idleNow);
            if (!idleNow || idleSince == 0) idleSince = t;
            updateScreen(idleNow, t);
            ui.postDelayed(this, 100);
        }
    };

    private final Runnable clockTick = new Runnable() {
        public void run() {
            Date now = new Date();
            clock.setText(clockFmt.format(now));
            String d = dateFmt.format(now);
            date.setText(d.substring(0, 1).toUpperCase(new Locale("ru")) + d.substring(1));
            if (now.getSeconds() == 0) moveClock(); // раз в минуту часы переезжают — чтобы экран не выгорал
            ui.postDelayed(this, 1000 - System.currentTimeMillis() % 1000);
        }
    };

    /** Плавно переносим часы в случайное место экрана (с отступом от краёв). */
    private void moveClock() {
        if (idle.getVisibility() != View.VISIBLE || idle.getWidth() == 0) return;
        int margin = dp(24);
        int maxX = Math.max(0, (root.getWidth() - idle.getWidth()) / 2 - margin);
        int maxY = Math.max(0, (root.getHeight() - idle.getHeight()) / 2 - margin);
        float x = maxX == 0 ? 0 : random.nextInt(2 * maxX + 1) - maxX;
        float y = maxY == 0 ? 0 : random.nextInt(2 * maxY + 1) - maxY;
        idle.animate().translationX(x).translationY(y).setDuration(4000)
                .setInterpolator(new android.view.animation.AccelerateDecelerateInterpolator()).start();
    }

    private void askServer() {
        final EditText e = new EditText(this);
        e.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        e.setHint("192.168.1.10:47801");
        if (server != null) e.setText(server);
        new AlertDialog.Builder(this)
                .setTitle("Адрес компьютера")
                .setMessage("Оставьте пустым для автопоиска")
                .setView(e)
                .setPositiveButton("OK", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int w) {
                        String s = e.getText().toString().trim();
                        if (s.length() > 0 && !s.contains(":")) s += ":47801";
                        server = s.length() > 0 ? s : null;
                        prefs.edit().putString("server", server).commit();
                    }
                })
                .setNegativeButton("Отмена", null)
                .show();
    }

    // ---------------- сеть ----------------

    private void networkLoop(int gen) {
        int failures = 0;
        while (running && gen == generation) {
            String s = server;
            if (s == null) {
                setStatus("Ищу компьютер…  (долгое нажатие — ввести адрес вручную)");
                s = discover();
                if (s == null) { sleep(3000); continue; }
                server = s;
                prefs.edit().putString("server", s).commit();
            }
            try {
                final JSONObject now = new JSONObject(new String(get("http://" + s + "/api/now", 3000), "UTF-8"));
                failures = 0;
                ui.post(new Runnable() { public void run() { apply(now); } });
                setStatus("");
            } catch (Exception e) {
                failures++;
                setStatus("Нет связи с компьютером " + s);
                if (failures >= 5) {
                    connected = false;
                    if (failures % 5 == 0) {          // попробуем найти ПК заново (мог смениться IP)
                        String found = discover();
                        if (found != null) server = found;
                    }
                }
            }
            sleep(1000);
        }
    }

    private void setStatus(final String text) {
        ui.post(new Runnable() { public void run() { status.setText(text); } });
    }

    private static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignored) { }
    }

    private static byte[] get(String url, int timeout) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(timeout);
        c.setReadTimeout(timeout * 4);
        try {
            if (c.getResponseCode() != 200) throw new Exception("HTTP " + c.getResponseCode());
            InputStream in = c.getInputStream();
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            return out.toByteArray();
        } finally {
            c.disconnect();
        }
    }

    /** UDP-широковещательный поиск ПК в сети. */
    private String discover() {
        DatagramSocket sock = null;
        try {
            sock = new DatagramSocket();
            sock.setBroadcast(true);
            sock.setSoTimeout(1500);
            byte[] msg = "NOWPLAYING_DISCOVER".getBytes("UTF-8");
            List<InetAddress> targets = new ArrayList<InetAddress>();
            targets.add(InetAddress.getByName("255.255.255.255"));
            WifiManager wm = (WifiManager) getSystemService(WIFI_SERVICE);
            DhcpInfo dhcp = wm != null ? wm.getDhcpInfo() : null;
            if (dhcp != null && dhcp.ipAddress != 0) {
                int bc = (dhcp.ipAddress & dhcp.netmask) | ~dhcp.netmask;
                byte[] q = new byte[4];
                for (int k = 0; k < 4; k++) q[k] = (byte) ((bc >> (k * 8)) & 0xff);
                targets.add(InetAddress.getByAddress(q));
            }
            for (InetAddress a : targets) sock.send(new DatagramPacket(msg, msg.length, a, UDP_PORT));
            byte[] buf = new byte[256];
            DatagramPacket p = new DatagramPacket(buf, buf.length);
            sock.receive(p);
            String[] parts = new String(p.getData(), 0, p.getLength(), "UTF-8").trim().split(" ");
            if (parts.length >= 2 && parts[0].equals("NOWPLAYING_HERE")) {
                return p.getAddress().getHostAddress() + ":" + parts[1];
            }
        } catch (Exception ignored) {
        } finally {
            if (sock != null) sock.close();
        }
        return null;
    }

    private void apply(JSONObject j) {
        connected = true;
        JSONObject bg = j.optJSONObject("bg");
        String nbg = bg == null ? null : bg.optString("id", null);
        if (nbg == null ? bgId != null : !nbg.equals(bgId)) {
            bgId = nbg;
            if (nbg == null) clearBg();
            else loadBg(server, nbg, bg.optString("ext", "jpg"), bg.optString("type", "image"));
        }
        if (!volumeDrag && !j.isNull("volume")) volume = j.optInt("volume", -1);
        String nclip = j.isNull("clip") ? null : j.optString("clip", null);
        if (nclip == null ? clipId != null : !nclip.equals(clipId)) {
            clipId = nclip;
            if (nclip == null) {
                clearClip();
                for (java.io.File f : getCacheDir().listFiles()) {   // видео больше не нужно
                    if (f.getName().startsWith("clip_")) f.delete();
                }
            } else {
                clearClip();   // старое видео не показываем под новым треком
                loadClip(server, nclip);
            }
        }
        String apk = j.isNull("apk_version") ? null : j.optString("apk_version", null);
        if (apk != null && !apk.equals(updateTried) && !j.optBoolean("playing")
                && compareVersions(apk, myVersion()) > 0) {
            updateTried = apk;   // ставим, только пока музыка не играет
            installUpdate(server, apk);
        }
        String id = j.isNull("track_id") ? null : j.optString("track_id", null);
        boolean wasPlaying = playing;
        playing = j.optBoolean("playing");
        // сразу после перемотки сервер может ещё отдать старую позицию
        if (SystemClock.elapsedRealtime() > seekHoldUntil) position = j.optLong("position_ms");
        duration = j.optLong("duration_ms");
        receivedAt = SystemClock.elapsedRealtime();
        if (!playing && (wasPlaying || pausedSince == 0)) pausedSince = receivedAt;
        if (playing) pausedSince = receivedAt;

        if (id != null && !id.equals(trackId)) {
            trackId = id;
            title.setText(j.optString("title"));
            String a = j.optString("artist");
            artist.setText(a);
            artist.setVisibility(a.length() > 0 ? View.VISIBLE : View.GONE);
            animateBackground(j);
            lyrics.setLyrics(null, new ArrayList<String>(), false);
            lyrics.setVisibility(View.GONE);
            unsynced.setVisibility(View.GONE);
            loadTrackExtras(server, id);
            setCover(null);
            artRev = -1;
        } else if (id == null) {
            trackId = null;
        } else {
            // цвет приходит чуть позже обложки — обновим, если поменялся
            animateBackground(j);
        }
        int rev = j.optInt("art_rev", 0);
        if (id != null && j.optBoolean("has_art") && rev != artRev) {
            artRev = rev;
            loadCover(server, id);  // сначала маленькая обложка из Windows, потом чёткая из iTunes
        }
    }

    private void loadCover(final String srv, final String id) {
        pool.execute(new Runnable() {
            public void run() {
                try {
                    byte[] img = get("http://" + srv + "/art/" + id + ".jpg?r=" + artRev, 3000);
                    final Bitmap bmp = BitmapFactory.decodeByteArray(img, 0, img.length);
                    ui.post(new Runnable() { public void run() { if (id.equals(trackId) && bmp != null) setCover(bmp); } });
                } catch (Exception ignored) {
                    artRev = -1;  // попробуем ещё раз при следующем опросе
                }
            }
        });
    }

    private void loadTrackExtras(final String srv, final String id) {
        pool.execute(new Runnable() {
            public void run() {
                // текст песни
                for (int i = 0; i < 5 && id.equals(trackId); i++) {
                    try {
                        JSONObject l = new JSONObject(new String(get("http://" + srv + "/api/lyrics/" + id, 15000), "UTF-8"));
                        if (l.optBoolean("pending")) { sleep(2000); continue; }
                        final List<Long> t = new ArrayList<Long>();
                        final List<String> lines = new ArrayList<String>();
                        JSONArray synced = l.optJSONArray("synced");
                        boolean isSynced = synced != null && synced.length() > 0;
                        if (isSynced) {
                            for (int k = 0; k < synced.length(); k++) {
                                JSONObject o = synced.getJSONObject(k);
                                t.add(o.getLong("t"));
                                lines.add(o.optString("text"));
                            }
                        } else {
                            for (String s : l.optString("plain").split("\n")) lines.add(s.trim());
                            if (lines.size() == 1 && lines.get(0).length() == 0) lines.clear();
                        }
                        final boolean fs = isSynced;
                        ui.post(new Runnable() {
                            public void run() {
                                if (!id.equals(trackId)) return;
                                lyrics.setLyrics(fs ? t : null, lines, fs);
                                lyrics.setVisibility(lines.isEmpty() ? View.GONE : View.VISIBLE);
                                unsynced.setVisibility(!fs && !lines.isEmpty() ? View.VISIBLE : View.GONE);
                            }
                        });
                        break;
                    } catch (Exception e) {
                        sleep(2000);
                    }
                }
            }
        });
    }

    /** Тонкая полоска прогресса. */
    static class ProgressLine extends View {
        private final Paint back = new Paint(Paint.ANTI_ALIAS_FLAG), front = new Paint(Paint.ANTI_ALIAS_FLAG);
        private float fraction;

        ProgressLine(android.content.Context c) {
            super(c);
            back.setColor(0x40ffffff);
            front.setColor(0xe6ffffff);
        }

        private boolean active;

        void setFraction(float f) {
            if (Math.abs(f - fraction) > 0.0005f) { fraction = f; invalidate(); }
        }

        /** Пока полоску тянут пальцем, рисуем на ней кружок. */
        void setActive(boolean a) {
            if (a != active) { active = a; invalidate(); }
        }

        @Override
        protected void onDraw(Canvas c) {
            float r = getHeight() / 2f;
            float x = getWidth() * Math.max(0, Math.min(1, fraction));
            c.drawRoundRect(new android.graphics.RectF(0, 0, getWidth(), getHeight()), r, r, back);
            c.drawRoundRect(new android.graphics.RectF(0, 0, x, getHeight()), r, r, front);
            if (active) c.drawCircle(Math.max(r * 3, Math.min(getWidth() - r * 3, x)), r, r * 3, front);
        }
    }
}
