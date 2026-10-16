import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.util.Base64;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.lang.reflect.Constructor;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * The AI agent's hands and eyes inside Android, run via app_process like AppCatalog and kept running
 * for as long as its adb shell is open:
 *
 *   CLASSPATH=/data/local/tmp/androiddesktop-agent.jar app_process / AgentHelper
 *
 * It reads one JSON request per line on stdin and answers each with one JSON line on stdout:
 * {"id": 1, "cmd": "tree", "display": 7} -> {"id": 1, "ok": true, ...}. Commands:
 *
 *   hello                                  version check
 *   create {name, width, height, dpi}      a virtual display with no window, for a hidden app
 *   release {display}                      ...and away again; its apps close with it
 *   tree {display}                         the display's accessibility tree, as compact JSON
 *   shot {display, format, quality, scale} the latest frame of one of our displays, base64
 *   act {display, index, bounds, action, text}   an accessibility action on a node from the last tree
 *   mark {display}                         how many accessibility events the display has sent so far
 *   settle {display, since, change, quiet, timeout}   after an action: wait for the display to react
 *                                          (an event after `since`), then to be still for `quiet` ms
 *
 * The tree comes from UiAutomation, as `uiautomator dump` does, but stays connected between reads, so a
 * read costs a few accessibility round trips instead of a new process and a second of waiting for idle.
 * Only one UiAutomation may be connected at a time across Android, so it lets go when unused for a while.
 */
public final class AgentHelper {
    static final int VERSION = 2;
    static final long IDLE_RELEASE_MS = 30_000;
    static final int MAX_DEPTH = 80;

    // DisplayManager.VIRTUAL_DISPLAY_FLAG_*: scrcpy's set for its own app displays.
    static final int DISPLAY_FLAGS = 1 // PUBLIC
            | 1 << 3   // OWN_CONTENT_ONLY
            | 1 << 6   // SUPPORTS_TOUCH
            | 1 << 7   // ROTATES_WITH_CONTENT
            | 1 << 8   // DESTROY_CONTENT_ON_REMOVAL: its apps close with it, rather than land on display 0
            | 1 << 10  // TRUSTED
            | 1 << 11  // OWN_DISPLAY_GROUP
            | 1 << 12  // ALWAYS_UNLOCKED
            | 1 << 13  // TOUCH_FEEDBACK_DISABLED
            | 1 << 14; // OWN_FOCUS: keys (Back) reach it without it taking focus from other displays

    // Node flags in the tree: one int per node keeps the JSON small.
    static final int CLICKABLE = 1, LONG_CLICKABLE = 2, EDITABLE = 4, SCROLLABLE = 8, CHECKABLE = 16,
            CHECKED = 32, SELECTED = 64, FOCUSED = 128, ENABLED = 256, PASSWORD = 512, FOCUSABLE = 1024,
            HEADING = 2048, SHOWING_HINT = 4096;

    static final Object lock = new Object();
    static Context context;
    static HandlerThread frameThread;
    static final Map<Integer, Screen> screens = new HashMap<>();
    static UiAutomation automation;
    static HandlerThread automationThread;
    static long lastUse;
    static long connectedAt;
    static final Map<Integer, List<AccessibilityNodeInfo>> lastNodes = new HashMap<>();
    // Accessibility events per display: how many so far, and when the last one came (uptime ms).
    static final Map<Integer, long[]> activity = new ConcurrentHashMap<>();

    /** One of our displays and the frames it draws. */
    static final class Screen {
        VirtualDisplay display;
        ImageReader reader;
        Image latest;
        int width, height;
    }

    public static void main(String[] args) throws Exception {
        OutputStream out = new FileOutputStream(FileDescriptor.out);
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        Thread releaser = new Thread(AgentHelper::releaseWhenIdle, "release-automation");
        releaser.setDaemon(true);
        releaser.start();
        String line;
        while ((line = in.readLine()) != null) {
            if (line.trim().isEmpty()) {
                continue;
            }
            String reply;
            long id = 0;
            try {
                JSONObject request = new JSONObject(line);
                id = request.optLong("id");
                synchronized (lock) {
                    reply = handle(request);
                }
                reply = "{\"id\":" + id + ",\"ok\":true" + (reply.isEmpty() ? "" : "," + reply) + "}";
            } catch (Throwable t) {
                Throwable cause = t.getCause() != null && t instanceof java.lang.reflect.InvocationTargetException
                        ? t.getCause() : t;
                reply = "{\"id\":" + id + ",\"ok\":false,\"error\":" + quote(String.valueOf(cause)) + "}";
            }
            out.write((reply + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();
        }
        // stdin closed: whoever started us is gone. Our displays go with us, and their apps with them.
        synchronized (lock) {
            for (Screen s : screens.values()) {
                s.display.release();
            }
            disconnect();
        }
        System.exit(0);
    }

    static String handle(JSONObject r) throws Exception {
        switch (r.getString("cmd")) {
            case "hello":
                return "\"version\":" + VERSION + ",\"pid\":" + Process.myPid();
            case "create":
                return create(r.getString("name"), r.getInt("width"), r.getInt("height"), r.getInt("dpi"));
            case "release":
                release(r.getInt("display"));
                return "";
            case "tree":
                return tree(r.getInt("display"));
            case "shot":
                return shot(r.getInt("display"), r.optString("format", "jpeg"), r.optInt("quality", 80),
                        r.optDouble("scale", 1.0));
            case "act":
                return act(r);
            case "mark":
                automation();
                return "\"events\":" + activity(r.getInt("display"))[0];
            case "settle":
                return settle(r.getInt("display"), r.optLong("since", -1), r.optLong("change", 800),
                        r.optLong("quiet", 300), r.optLong("timeout", 3000));
            default:
                throw new IllegalArgumentException("unknown command " + r.getString("cmd"));
        }
    }

    // ------------------------------------------------------------------ displays
    static String create(String name, int width, int height, int dpi) throws Exception {
        if (frameThread == null) {
            frameThread = new HandlerThread("frames");
            frameThread.start();
        }
        Screen s = new Screen();
        s.width = width;
        s.height = height;
        s.reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 3);
        // Take every frame as it comes and keep only the newest: a reader nobody drains stalls the display.
        s.reader.setOnImageAvailableListener(reader -> {
            Image image;
            try {
                image = reader.acquireLatestImage();
            } catch (IllegalStateException e) {
                return; // released meanwhile
            }
            if (image == null) {
                return;
            }
            synchronized (s) {
                if (s.latest != null) {
                    s.latest.close();
                }
                s.latest = image;
            }
        }, new Handler(frameThread.getLooper()));
        DisplayManager dm = displayManager();
        s.display = dm.createVirtualDisplay(name, width, height, dpi, s.reader.getSurface(), DISPLAY_FLAGS);
        if (s.display == null) {
            s.reader.close();
            throw new IllegalStateException("Android refused the virtual display");
        }
        int id = s.display.getDisplay().getDisplayId();
        screens.put(id, s);
        return "\"display\":" + id;
    }

    static void release(int id) {
        Screen s = screens.remove(id);
        if (s == null) {
            return;
        }
        s.display.release();
        synchronized (s) {
            if (s.latest != null) {
                s.latest.close();
                s.latest = null;
            }
            s.reader.close();
        }
        lastNodes.remove(id);
    }

    static DisplayManager displayManager() throws Exception {
        Constructor<DisplayManager> c = DisplayManager.class.getDeclaredConstructor(Context.class);
        c.setAccessible(true);
        return c.newInstance(shellContext());
    }

    /** Android checks that the package a caller names is its own: as uid 2000, that's the shell's. */
    static Context shellContext() throws Exception {
        if (context == null) {
            if (Looper.getMainLooper() == null) {
                Looper.prepareMainLooper();
            }
            Class<?> cls = Class.forName("android.app.ActivityThread");
            Object thread = cls.getMethod("systemMain").invoke(null);
            Context system = (Context) cls.getMethod("getSystemContext").invoke(thread);
            context = new ContextWrapper(system) {
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
        return context;
    }

    // ------------------------------------------------------------------ frames
    static String shot(int id, String format, int quality, double scale) throws Exception {
        Screen s = screens.get(id);
        if (s == null) {
            throw new IllegalArgumentException("no frames for display " + id + ": it isn't one of ours");
        }
        Bitmap full;
        int w, h;
        synchronized (s) {
            if (s.latest == null) {
                throw new IllegalStateException("display " + id + " hasn't drawn anything yet");
            }
            Image.Plane plane = s.latest.getPlanes()[0];
            ByteBuffer buffer = plane.getBuffer();
            buffer.rewind();
            w = s.latest.getWidth();
            h = s.latest.getHeight();
            full = Bitmap.createBitmap(plane.getRowStride() / plane.getPixelStride(), h, Bitmap.Config.ARGB_8888);
            full.copyPixelsFromBuffer(buffer);
        }
        Bitmap bitmap = Bitmap.createBitmap(full, 0, 0, w, h);
        if (scale > 0 && scale < 1) {
            bitmap = Bitmap.createScaledBitmap(bitmap, Math.max(1, (int) Math.round(w * scale)),
                    Math.max(1, (int) Math.round(h * scale)), true);
        }
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        boolean png = format.equals("png");
        bitmap.compress(png ? Bitmap.CompressFormat.PNG : Bitmap.CompressFormat.JPEG, quality, bytes);
        return "\"format\":\"" + (png ? "png" : "jpeg") + "\",\"width\":" + bitmap.getWidth() + ",\"height\":"
                + bitmap.getHeight() + ",\"data\":\"" + Base64.encodeToString(bytes.toByteArray(), Base64.NO_WRAP)
                + "\"";
    }

    // ------------------------------------------------------------------ UiAutomation
    static UiAutomation automation() throws Exception {
        lastUse = System.currentTimeMillis();
        if (automation != null) {
            return automation;
        }
        shellContext();
        automationThread = new HandlerThread("uiautomation");
        automationThread.start();
        Class<?> connectionClass = Class.forName("android.app.IUiAutomationConnection");
        Object connection = Class.forName("android.app.UiAutomationConnection").getConstructor().newInstance();
        UiAutomation ua;
        try {
            Constructor<UiAutomation> c = UiAutomation.class.getDeclaredConstructor(Looper.class, connectionClass);
            c.setAccessible(true);
            ua = c.newInstance(automationThread.getLooper(), connection);
        } catch (NoSuchMethodException e) {
            Constructor<UiAutomation> c = UiAutomation.class.getDeclaredConstructor(int.class, Looper.class,
                    connectionClass);
            c.setAccessible(true);
            ua = c.newInstance(0, automationThread.getLooper(), connection);
        }
        try {
            UiAutomation.class.getMethod("connect", int.class)
                    .invoke(ua, UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
        } catch (java.lang.reflect.InvocationTargetException e) {
            automationThread.quitSafely();
            automationThread = null;
            String why = String.valueOf(e.getCause());
            throw new IllegalStateException(why.contains("already") || why.contains("registered")
                    ? "another UiAutomation client is connected (uiautomator, or another agent): " + why : why);
        }
        ua.setOnAccessibilityEventListener(event -> {
            long[] a = activity(event.getDisplayId());
            synchronized (a) {
                a[0]++;
                a[1] = SystemClock.uptimeMillis();
            }
        });
        AccessibilityServiceInfo info = ua.getServiceInfo();
        info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS;
        ua.setServiceInfo(info);
        automation = ua;
        connectedAt = System.currentTimeMillis();
        return ua;
    }

    static void disconnect() {
        if (automation == null) {
            return;
        }
        try {
            UiAutomation.class.getMethod("disconnect").invoke(automation);
        } catch (Exception ignored) {
            // already gone
        }
        automation = null;
        lastNodes.clear();
        automationThread.quitSafely();
        automationThread = null;
    }

    static void releaseWhenIdle() {
        while (true) {
            try {
                Thread.sleep(5_000);
            } catch (InterruptedException e) {
                return;
            }
            synchronized (lock) {
                if (automation != null && System.currentTimeMillis() - lastUse > IDLE_RELEASE_MS) {
                    disconnect();
                }
            }
        }
    }

    static long[] activity(int display) {
        return activity.computeIfAbsent(display, d -> new long[2]);
    }

    /**
     * After an action on a display: wait (up to `change` ms) for the display to react, then for it to be
     * still for `quiet` ms. Only that display's events count, so another app ticking away elsewhere doesn't
     * hold it up, and a tap the app answers a moment later isn't mistaken for "nothing to wait for", as
     * UiAutomation.waitForIdle would when the screen happened to be still as the tap landed.
     */
    static String settle(int display, long since, long change, long quiet, long timeout) throws Exception {
        automation();
        long[] a = activity(display);
        long start = SystemClock.uptimeMillis();
        boolean changed = false;
        while (SystemClock.uptimeMillis() - start < Math.min(change, timeout)) {
            synchronized (a) {
                changed = a[0] > since;
            }
            if (changed) {
                break;
            }
            Thread.sleep(10);
        }
        boolean still = !changed; // nothing happened: it's as still as it will get
        while (changed && SystemClock.uptimeMillis() - start < timeout) {
            long last;
            synchronized (a) {
                last = a[1];
            }
            if (SystemClock.uptimeMillis() - last >= quiet) {
                still = true;
                break;
            }
            Thread.sleep(10);
        }
        lastUse = System.currentTimeMillis();
        return "\"changed\":" + changed + ",\"still\":" + still;
    }

    // ------------------------------------------------------------------ the tree
    static List<AccessibilityWindowInfo> windows(int display) throws Exception {
        UiAutomation ua = automation();
        List<AccessibilityWindowInfo> found = ua.getWindowsOnAllDisplays().get(display);
        // A fresh connection gets the windows of displays other than 0 a moment later.
        while ((found == null || found.isEmpty()) && System.currentTimeMillis() - connectedAt < 3000) {
            Thread.sleep(50);
            found = ua.getWindowsOnAllDisplays().get(display);
        }
        List<AccessibilityWindowInfo> sorted = found == null ? new ArrayList<>() : new ArrayList<>(found);
        Collections.sort(sorted, (a, b) -> Integer.compare(b.getLayer(), a.getLayer())); // top first
        return sorted;
    }

    static String tree(int display) throws Exception {
        List<AccessibilityNodeInfo> nodes = new ArrayList<>();
        StringBuilder sb = new StringBuilder(16384);
        sb.append("\"display\":").append(display).append(",\"windows\":[");
        boolean first = true;
        for (AccessibilityWindowInfo w : windows(display)) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            Rect r = new Rect();
            w.getBoundsInScreen(r);
            sb.append("{\"id\":").append(w.getId()).append(",\"type\":").append(w.getType())
                    .append(",\"layer\":").append(w.getLayer())
                    .append(",\"active\":").append(w.isActive()).append(",\"focused\":").append(w.isFocused())
                    .append(",\"b\":[").append(r.left).append(',').append(r.top).append(',').append(r.right)
                    .append(',').append(r.bottom).append(']');
            CharSequence title = w.getTitle();
            if (title != null) {
                sb.append(",\"title\":").append(quote(title.toString()));
            }
            AccessibilityNodeInfo root = w.getRoot(AccessibilityNodeInfo.FLAG_PREFETCH_DESCENDANTS_HYBRID);
            if (root != null) {
                CharSequence pkg = root.getPackageName();
                if (pkg != null) {
                    sb.append(",\"pkg\":").append(quote(pkg.toString()));
                }
                sb.append(",\"root\":");
                node(root, nodes, sb, 0);
            }
            sb.append('}');
        }
        sb.append(']');
        lastNodes.put(display, nodes);
        return sb.toString();
    }

    static void node(AccessibilityNodeInfo n, List<AccessibilityNodeInfo> nodes, StringBuilder sb, int depth) {
        int index = nodes.size();
        nodes.add(n);
        Rect r = new Rect();
        n.getBoundsInScreen(r);
        sb.append("{\"i\":").append(index);
        field(sb, "c", n.getClassName());
        field(sb, "t", n.getText());
        field(sb, "d", n.getContentDescription());
        field(sb, "r", n.getViewIdResourceName());
        field(sb, "h", n.getHintText());
        field(sb, "s", n.getStateDescription());
        field(sb, "p", n.getPaneTitle());
        field(sb, "e", n.getError());
        sb.append(",\"b\":[").append(r.left).append(',').append(r.top).append(',').append(r.right).append(',')
                .append(r.bottom).append("],\"f\":").append(flags(n));
        int count = depth < MAX_DEPTH ? n.getChildCount() : 0;
        boolean open = false;
        for (int i = 0; i < count; i++) {
            AccessibilityNodeInfo child = n.getChild(i, AccessibilityNodeInfo.FLAG_PREFETCH_DESCENDANTS_HYBRID);
            if (child == null || !child.isVisibleToUser()) {
                continue; // off screen (a list's hidden rows) or hidden: nothing to see or tap
            }
            sb.append(open ? "," : ",\"k\":[");
            open = true;
            node(child, nodes, sb, depth + 1);
        }
        if (open) {
            sb.append(']');
        }
        sb.append('}');
    }

    static int flags(AccessibilityNodeInfo n) {
        return (n.isClickable() ? CLICKABLE : 0) | (n.isLongClickable() ? LONG_CLICKABLE : 0)
                | (n.isEditable() ? EDITABLE : 0) | (n.isScrollable() ? SCROLLABLE : 0)
                | (n.isCheckable() ? CHECKABLE : 0) | (n.isChecked() ? CHECKED : 0)
                | (n.isSelected() ? SELECTED : 0) | (n.isFocused() ? FOCUSED : 0) | (n.isEnabled() ? ENABLED : 0)
                | (n.isPassword() ? PASSWORD : 0) | (n.isFocusable() ? FOCUSABLE : 0)
                | (n.isHeading() ? HEADING : 0) | (n.isShowingHintText() ? SHOWING_HINT : 0);
    }

    // ------------------------------------------------------------------ acting on a node
    static String act(JSONObject r) throws Exception {
        int display = r.getInt("display");
        String action = r.getString("action");
        AccessibilityNodeInfo n = find(display, r.optInt("index", -1), r.optJSONArray("bounds"),
                action.equals("setText") || action.equals("imeEnter"));
        if (n == null) {
            throw new IllegalStateException(r.has("index") || r.has("bounds")
                    ? "that element is gone; read the screen again" : "no text field has focus");
        }
        boolean done;
        switch (action) {
            case "setText": {
                Bundle args = new Bundle();
                args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, r.getString("text"));
                done = n.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
                break;
            }
            case "imeEnter":
                done = n.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId());
                break;
            case "click":
                done = n.performAction(AccessibilityNodeInfo.ACTION_CLICK);
                break;
            case "focus":
                done = n.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
                break;
            case "scrollForward":
                done = n.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD);
                break;
            case "scrollBackward":
                done = n.performAction(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD);
                break;
            default:
                throw new IllegalArgumentException("unknown action " + action);
        }
        n.refresh();
        CharSequence text = n.getText();
        return "\"done\":" + done + ",\"text\":" + quote(text == null ? "" : text.toString());
    }

    /** A node from the last tree, if it's still there and where it was; else the node now at those
     *  bounds; with neither given, the display's focused text field. */
    static AccessibilityNodeInfo find(int display, int index, JSONArray bounds, boolean editable)
            throws Exception {
        List<AccessibilityNodeInfo> nodes = lastNodes.get(display);
        if (index >= 0 && nodes != null && index < nodes.size()) {
            AccessibilityNodeInfo n = nodes.get(index);
            if (n.refresh() && (bounds == null || sameBounds(n, bounds))) {
                return n;
            }
        }
        for (AccessibilityWindowInfo w : windows(display)) {
            AccessibilityNodeInfo root = w.getRoot(AccessibilityNodeInfo.FLAG_PREFETCH_DESCENDANTS_HYBRID);
            if (root == null) {
                continue;
            }
            AccessibilityNodeInfo found = bounds == null
                    ? root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) : search(root, bounds, editable, 0);
            if (found != null && (!editable || found.isEditable())) {
                return found;
            }
        }
        return null;
    }

    static AccessibilityNodeInfo search(AccessibilityNodeInfo n, JSONArray bounds, boolean editable, int depth)
            throws Exception {
        if (sameBounds(n, bounds) && (!editable || n.isEditable())) {
            return n;
        }
        if (depth >= MAX_DEPTH) {
            return null;
        }
        for (int i = 0; i < n.getChildCount(); i++) {
            AccessibilityNodeInfo child = n.getChild(i);
            AccessibilityNodeInfo found = child == null ? null : search(child, bounds, editable, depth + 1);
            if (found != null) {
                return found;
            }
        }
        return null;
    }

    static boolean sameBounds(AccessibilityNodeInfo n, JSONArray b) throws Exception {
        Rect r = new Rect();
        n.getBoundsInScreen(r);
        return r.left == b.getInt(0) && r.top == b.getInt(1) && r.right == b.getInt(2) && r.bottom == b.getInt(3);
    }

    // ------------------------------------------------------------------ JSON
    static void field(StringBuilder sb, String key, CharSequence value) {
        if (value != null && value.length() > 0) {
            sb.append(",\"").append(key).append("\":").append(quote(value.toString()));
        }
    }

    static String quote(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 2).append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    sb.append("\\\"");
                    break;
                case '\\':
                    sb.append("\\\\");
                    break;
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
                default:
                    if (c < 0x20 || c == 0x2028 || c == 0x2029) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        return sb.append('"').toString();
    }
}
