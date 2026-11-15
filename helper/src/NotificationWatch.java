import android.app.ActivityOptions;
import android.app.Notification;
import android.app.PendingIntent;
import android.app.RemoteInput;
import android.content.AttributionSource;
import android.content.ComponentName;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.Icon;
import android.os.Build;
import android.os.Bundle;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.Process;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import android.util.Base64;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Android's notifications, for Windows to show as its own. Run via app_process like AgentHelper and
 * kept running for as long as its adb shell is open:
 *
 *   CLASSPATH=/data/local/tmp/androiddesktop-notify.jar app_process / NotificationWatch
 *
 * It registers a notification listener as a system service, as SystemUI does (the shell holds
 * STATUS_BAR_SERVICE, though not ACCESS_NOTIFICATIONS), reads the active notifications through it once a
 * second and prints one
 * JSON line per change: {"event": "posted", "key": ..., "package": ..., "title": ..., "text": ...} for a
 * new or updated one ("initial": true for those already there when it started), {"event": "removed",
 * "key": ...} for one that went away. Commands come in on stdin, one JSON line each, answered with
 * {"id": n, "ok": true} or {"id": n, "ok": false, "error": ...}:
 *
 *   open {key, display}   run the notification's tap action (its contentIntent) on that display
 *   reply {key, text}     answer through its reply action (RemoteInput), as Android's shade does
 *
 * Prints {"event": "hello", "version": n} first.
 */
public final class NotificationWatch {
    static final int VERSION = 1;
    static final long POLL_MS = 1000;
    static final int LARGE_ICON_PX = 96;
    static final Pattern IMPORTANCE = Pattern.compile("importance=(-?\\d+) key=(.*?): Notification\\(");

    static final Object out = new Object();
    static OutputStream stdout;
    static Context context;
    static NotificationListenerService listener;
    static Object manager;  // INotificationManager
    static Object token;  // the listener's INotificationListener, which the manager knows it by
    static Method active;
    static final Map<String, StatusBarNotification> current = new HashMap<>();
    static final Map<String, Long> posted = new HashMap<>();  // key -> postTime last reported
    static final Map<String, Integer> importance = new HashMap<>();

    public static void main(String[] args) throws Exception {
        stdout = new FileOutputStream(FileDescriptor.out);
        context = shellContext();
        listener = register();
        emit(new JSONObject().put("event", "hello").put("version", VERSION));
        Thread poller = new Thread(NotificationWatch::poll, "poll");
        poller.setDaemon(true);
        poller.start();

        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        String line;
        while ((line = in.readLine()) != null) {
            if (line.trim().isEmpty()) {
                continue;
            }
            long id = 0;
            JSONObject reply = new JSONObject();
            try {
                JSONObject request = new JSONObject(line);
                id = request.optLong("id");
                reply.put("id", id);
                handle(request);
                reply.put("ok", true);
            } catch (Throwable t) {
                Throwable cause = t instanceof java.lang.reflect.InvocationTargetException && t.getCause() != null
                        ? t.getCause() : t;
                reply.put("id", id).put("ok", false).put("error", String.valueOf(cause));
            }
            emit(reply);
        }
        System.exit(0);  // stdin closed: whoever started us is gone
    }

    static void handle(JSONObject request) throws Exception {
        String cmd = request.getString("cmd");
        StatusBarNotification sbn;
        synchronized (current) {
            sbn = current.get(request.getString("key"));
        }
        if (sbn == null) {
            throw new IllegalArgumentException("that notification is gone");
        }
        Notification n = sbn.getNotification();
        if (cmd.equals("open")) {
            if (n.contentIntent == null) {
                throw new IllegalStateException("it has no tap action");
            }
            ActivityOptions options = ActivityOptions.makeBasic();
            if (request.has("display")) {
                options.setLaunchDisplayId(request.getInt("display"));
            }
            if (Build.VERSION.SDK_INT >= 34) {
                // The shell may start activities from the background; say we lend that to this intent.
                // From Android 16, ALLOWED only lends it while the sender is visible, which the shell
                // never is; ALLOW_ALWAYS lends it regardless.
                options.setPendingIntentBackgroundActivityStartMode(Build.VERSION.SDK_INT >= 36
                        ? ActivityOptions.MODE_BACKGROUND_ACTIVITY_START_ALLOW_ALWAYS
                        : ActivityOptions.MODE_BACKGROUND_ACTIVITY_START_ALLOWED);
            }
            n.contentIntent.send(context, 0, null, null, null, null, options.toBundle());
        } else if (cmd.equals("reply")) {
            Notification.Action action = replyAction(n);
            if (action == null) {
                throw new IllegalStateException("it has no reply action");
            }
            RemoteInput input = freeForm(action);
            Intent intent = new Intent().addFlags(Intent.FLAG_RECEIVER_FOREGROUND);
            Bundle results = new Bundle();
            results.putCharSequence(input.getResultKey(), request.getString("text"));
            RemoteInput.addResultsToIntent(action.getRemoteInputs(), intent, results);
            RemoteInput.setResultsSource(intent, RemoteInput.SOURCE_FREE_FORM_INPUT);
            action.actionIntent.send(context, 0, intent);
        } else {
            throw new IllegalArgumentException("unknown command " + cmd);
        }
    }

    // ------------------------------------------------------------------ watching
    static void poll() {
        boolean first = true;
        while (true) {
            try {
                // Not listener.getActiveNotifications(), which swallows errors and returns null.
                Object slice = active.invoke(manager, token, null, 0);  // 0: TRIM_FULL
                @SuppressWarnings("unchecked")
                java.util.List<StatusBarNotification> list = (java.util.List<StatusBarNotification>)
                        slice.getClass().getMethod("getList").invoke(slice);
                Set<String> seen = new HashSet<>();
                boolean askedImportance = false;
                for (StatusBarNotification sbn : list) {
                    String key = sbn.getKey();
                    seen.add(key);
                    Long before = posted.get(key);
                    synchronized (current) {
                        current.put(key, sbn);
                    }
                    if (before != null && before == sbn.getPostTime()) {
                        continue;
                    }
                    posted.put(key, sbn.getPostTime());
                    if (!importance.containsKey(key) && !askedImportance) {
                        readImportance();  // once a round, for every new notification at once
                        askedImportance = true;
                    }
                    emit(describe(sbn, first, before != null));
                }
                for (String key : new HashSet<>(posted.keySet())) {
                    if (!seen.contains(key)) {
                        posted.remove(key);
                        importance.remove(key);
                        synchronized (current) {
                            current.remove(key);
                        }
                        emit(new JSONObject().put("event", "removed").put("key", key));
                    }
                }
                first = false;
            } catch (Throwable t) {
                Throwable cause = t instanceof java.lang.reflect.InvocationTargetException && t.getCause() != null
                        ? t.getCause() : t;
                try {
                    emit(new JSONObject().put("event", "error").put("error", String.valueOf(cause)));
                } catch (Exception ignored) {
                    // nothing more to say
                }
            }
            try {
                Thread.sleep(POLL_MS);
            } catch (InterruptedException e) {
                return;
            }
        }
    }

    /**
     * How much each notification may interrupt (NotificationManager.IMPORTANCE_*), which is its
     * channel's: the shell can't ask for other apps' channels, but `dumpsys notification` lists it
     * beside each notification's key. Below DEFAULT, Android itself shows it without a sound or a
     * heads-up, so Windows shouldn't pop one up either.
     */
    static void readImportance() {
        importance.clear();
        try {
            java.lang.Process p = Runtime.getRuntime().exec(new String[]{"dumpsys", "notification"});
            BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8));
            String line;
            while ((line = r.readLine()) != null) {
                if (line.contains("NotificationRecord(")) {
                    Matcher m = IMPORTANCE.matcher(line);
                    if (m.find()) {
                        importance.put(m.group(2), Integer.parseInt(m.group(1)));
                    }
                }
            }
            p.waitFor();
        } catch (Exception ignored) {
            // unknown: Windows decides without it
        }
    }

    static JSONObject describe(StatusBarNotification sbn, boolean initial, boolean update) throws Exception {
        Notification n = sbn.getNotification();
        Bundle extras = n.extras;
        CharSequence title = extras.getCharSequence(Notification.EXTRA_TITLE_BIG);
        if (title == null) {
            title = extras.getCharSequence(Notification.EXTRA_TITLE);
        }
        CharSequence text = extras.getCharSequence(Notification.EXTRA_BIG_TEXT);
        if (text == null) {
            text = extras.getCharSequence(Notification.EXTRA_TEXT);
        }
        JSONObject o = new JSONObject()
                .put("event", "posted")
                .put("key", sbn.getKey())
                .put("package", sbn.getPackageName())
                .put("initial", initial)
                .put("update", update)
                .put("when", n.when)
                .put("postTime", sbn.getPostTime())
                .put("flags", n.flags)
                .put("priority", n.priority)
                .put("title", title == null ? "" : title.toString())
                .put("text", text == null ? "" : text.toString())
                .put("sub", str(extras.getCharSequence(Notification.EXTRA_SUB_TEXT)))
                .put("conversation", str(extras.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE)))
                .put("category", n.category == null ? "" : n.category)
                .put("group", sbn.getGroupKey() == null ? "" : sbn.getGroupKey())
                .put("canOpen", n.contentIntent != null)
                .put("canReply", replyAction(n) != null)
                .put("user", sbn.getUserId());
        Integer imp = importance.get(sbn.getKey());
        if (imp != null) {
            o.put("importance", imp);
        }
        Notification.Action reply = replyAction(n);
        if (reply != null) {
            CharSequence label = freeForm(reply).getLabel();
            o.put("replyLabel", label == null ? "" : label.toString());
        }
        if (!initial) {
            String icon = largeIcon(n);
            if (icon != null) {
                o.put("icon", icon);
            }
        }
        return o;
    }

    /** The action Android's shade would show a reply box for: one that takes free-form text. */
    static Notification.Action replyAction(Notification n) {
        if (n.actions == null) {
            return null;
        }
        for (Notification.Action a : n.actions) {
            if (a.actionIntent != null && freeForm(a) != null) {
                return a;
            }
        }
        return null;
    }

    static RemoteInput freeForm(Notification.Action a) {
        RemoteInput[] inputs = a.getRemoteInputs();
        if (inputs == null) {
            return null;
        }
        for (RemoteInput r : inputs) {
            if (r.getAllowFreeFormInput()) {
                return r;
            }
        }
        return null;
    }

    /** The picture beside the text (a sender's photo, an album cover), as a base64 PNG. */
    static String largeIcon(Notification n) {
        try {
            Icon icon = n.getLargeIcon();
            if (icon == null) {
                return null;
            }
            Drawable d = icon.loadDrawable(context);
            if (d == null) {
                return null;
            }
            Bitmap b = Bitmap.createBitmap(LARGE_ICON_PX, LARGE_ICON_PX, Bitmap.Config.ARGB_8888);
            d.setBounds(0, 0, LARGE_ICON_PX, LARGE_ICON_PX);
            d.draw(new Canvas(b));
            ByteArrayOutputStream png = new ByteArrayOutputStream();
            b.compress(Bitmap.CompressFormat.PNG, 100, png);
            return Base64.encodeToString(png.toByteArray(), Base64.NO_WRAP);
        } catch (Throwable t) {
            return null;  // another app's resources we can't load: Windows uses the app's icon
        }
    }

    static String str(CharSequence s) {
        return s == null ? "" : s.toString();
    }

    static void emit(JSONObject o) throws Exception {
        byte[] line = (o.toString() + "\n").getBytes(StandardCharsets.UTF_8);
        synchronized (out) {
            stdout.write(line);
            stdout.flush();
        }
    }

    /**
     * A listener for every user's notifications, registered the way SystemUI registers its own
     * (NotificationListenerService.registerAsSystemService, a system API). The shell may call
     * INotificationManager.registerListener because it holds STATUS_BAR_SERVICE; an ordinary listener
     * would need an app the user approves. Its callbacks are ignored (we poll), so they run on a looper
     * of their own rather than piling up on the main one, which never loops.
     */
    static NotificationListenerService register() throws Exception {
        HandlerThread callbacks = new HandlerThread("listener");
        callbacks.start();
        Looper looper = callbacks.getLooper();
        Context ctx = new ContextWrapper(context) {
            @Override
            public Looper getMainLooper() {
                return looper;
            }
        };
        NotificationListenerService l = new NotificationListenerService() {
        };
        NotificationListenerService.class
                .getMethod("registerAsSystemService", Context.class, ComponentName.class, int.class)
                .invoke(l, ctx, new ComponentName("com.android.shell", NotificationWatch.class.getName()), -1);
        // -1: UserHandle.USER_ALL
        Method m = NotificationListenerService.class.getDeclaredMethod("getNotificationInterface");
        m.setAccessible(true);
        manager = m.invoke(l);
        java.lang.reflect.Field f = NotificationListenerService.class.getDeclaredField("mWrapper");
        f.setAccessible(true);
        token = f.get(l);
        Class<?> listenerInterface = Class.forName("android.service.notification.INotificationListener");
        active = manager.getClass().getMethod("getActiveNotificationsFromListener",
                listenerInterface, String[].class, int.class);
        return l;
    }

    /** As AgentHelper's: Android checks that the package a caller names is its own, the shell's. */
    static Context shellContext() throws Exception {
        if (Looper.getMainLooper() == null) {
            Looper.prepareMainLooper();
        }
        Class<?> cls = Class.forName("android.app.ActivityThread");
        Object thread = cls.getMethod("systemMain").invoke(null);
        Context system = (Context) cls.getMethod("getSystemContext").invoke(thread);
        return new ContextWrapper(system) {
            @Override
            public String getPackageName() {
                return "com.android.shell";
            }

            @Override
            public String getOpPackageName() {
                return "com.android.shell";
            }

            @Override
            public AttributionSource getAttributionSource() {
                return new AttributionSource.Builder(Process.myUid()).setPackageName("com.android.shell").build();
            }

            @Override
            public Context getApplicationContext() {
                return this;
            }
        };
    }
}
